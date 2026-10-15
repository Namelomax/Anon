import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import type { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { formatDateRu } from "@/lib/period";

/**
 * Приглашения в аккаунт и учёт мест по тарифу (`Plan.maxUsers`).
 *
 * МЕСТО ЗАНИМАЕТ НЕ ТОЛЬКО ПОЛЬЗОВАТЕЛЬ, НО И ЖИВОЕ ПРИГЛАШЕНИЕ. Иначе лимит
 * обходится тривиально: на тарифе с двумя местами админ выписывает двадцать
 * приглашений (каждое по отдельности «в пределах лимита», потому что
 * пользователей всё ещё один), и при активации в аккаунте оказывается
 * двадцать человек. Поэтому занятые места = активные пользователи + живые
 * приглашения, и проверка стоит В ТОЙ ЖЕ транзакции, что и вставка.
 *
 * Отключённый пользователь (`isActive=false`) место освобождает: это
 * единственный способ пересадить человека на тарифе с фиксированным числом
 * мест, не повышая тариф.
 */

/** Сколько живёт приглашение. Неактивированное просто протухает и освобождает место. */
const INVITE_TTL_DAYS = 7;

/** Длина токена в байтах: 32 случайных байта — перебирать нечем. */
const TOKEN_BYTES = 32;

export type SeatUsage = {
  /** null — тариф без ограничения на число пользователей. */
  limit: number | null;
  /** Название тарифа — для текста отказа в приглашении. */
  planTitle: string;
  activeUsers: number;
  pendingInvitations: number;
  /** activeUsers + pendingInvitations. */
  taken: number;
  /** null при безлимитном тарифе. */
  free: number | null;
};

/** Условие «приглашение ещё живо»: не активировано, не отозвано, не протухло. */
export function pendingInvitationWhere(now: Date = new Date()) {
  return { acceptedAt: null, revokedAt: null, expiresAt: { gt: now } };
}

/**
 * Занятые и свободные места аккаунта. `tx` — чтобы считать внутри той же
 * транзакции, что и вставку (см. шапку файла).
 */
export async function getSeatUsage(
  accountId: number,
  tx: Pick<typeof prisma, "account" | "user" | "invitation"> = prisma,
): Promise<SeatUsage | null> {
  const account = await tx.account.findUnique({
    where: { id: accountId },
    include: { plan: true },
  });
  if (!account) return null;

  const [activeUsers, pendingInvitations] = await Promise.all([
    tx.user.count({ where: { accountId, isActive: true } }),
    tx.invitation.count({ where: { accountId, ...pendingInvitationWhere() } }),
  ]);

  const limit = account.plan.maxUsers;
  const taken = activeUsers + pendingInvitations;
  return {
    limit,
    planTitle: account.plan.title,
    activeUsers,
    pendingInvitations,
    taken,
    free: limit == null ? null : Math.max(0, limit - taken),
  };
}

/** Новый токен приглашения: сама строка (уходит в ссылку) и её хеш (в БД). */
export function newInviteToken(): { token: string; tokenHash: string } {
  const token = randomBytes(TOKEN_BYTES).toString("base64url");
  return { token, tokenHash: hashInviteToken(token) };
}

/**
 * sha-256 от токена. Не argon2: токен — 32 случайных байта, подбирать его
 * нечем, а замедляющий хеш только нагрузил бы проверку (то же решение, что
 * и для ApiKey.keyHash, см. schema.prisma).
 */
export function hashInviteToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/**
 * Сравнение хешей за постоянное время. Строгое `===` на хеше токена — не
 * дыра сама по себе (сравнивается уже хеш, а не секрет), но поиск идёт по
 * уникальному индексу, так что стоимость постоянного времени здесь нулевая,
 * а привычка — правильная.
 */
export function sameHash(a: string, b: string): boolean {
  const left = Buffer.from(a, "utf8");
  const right = Buffer.from(b, "utf8");
  return left.length === right.length && timingSafeEqual(left, right);
}

export function inviteExpiryFrom(now: Date = new Date()): Date {
  return new Date(now.getTime() + INVITE_TTL_DAYS * 24 * 60 * 60 * 1000);
}

/** Ссылка активации для показа пригласившему (один раз — токен мы не храним). */
/**
 * Публичный адрес сайта для ссылок, которые уходят за пределы браузера
 * пригласившего (ссылка-приглашение).
 *
 * НЕ `req.nextUrl.origin`: за nginx Next.js собирает его из собственного
 * адреса привязки, и в ссылку попадало `https://localhost:8010` — протокол из
 * X-Forwarded-Proto, а хост внутренний. Порядок: AUTH_URL (тот же, по
 * которому Auth.js строит редирект после выхода, — один источник правды для
 * обоих), затем заголовки прокси, и только в последнюю очередь nextUrl
 * (локальная разработка без прокси).
 */
export function publicOrigin(req: {
  headers: Headers;
  nextUrl: { origin: string };
}): string {
  const configured = process.env.AUTH_URL?.trim();
  if (configured) return new URL(configured).origin;
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host");
  if (host && !/^(localhost|127\.0\.0\.1)(:\d+)?$/i.test(host)) {
    const proto = req.headers.get("x-forwarded-proto")?.split(",")[0].trim() || "https";
    return `${proto}://${host}`;
  }
  return req.nextUrl.origin;
}

export function inviteUrl(origin: string, token: string): string {
  return `${origin.replace(/\/$/, "")}/invite/${token}`;
}

/** Единый отказ на «нет такого», «активировано», «отозвано», «протухло». */
export const INVITE_INVALID_MESSAGE = "Ссылка приглашения недействительна или истекла.";

type Tx = Prisma.TransactionClient;

/**
 * Живое приглашение по токену из ссылки; null — любое недействительное
 * состояние без различения (подбирать токен нечем, но и подсказывать его
 * состояние незачем).
 */
export async function findLiveInvitationByToken(tx: Tx, token: string) {
  const invitation = await tx.invitation.findUnique({
    where: { tokenHash: hashInviteToken(token) },
  });
  if (
    !invitation ||
    invitation.acceptedAt != null ||
    invitation.revokedAt != null ||
    invitation.expiresAt.getTime() <= Date.now()
  ) {
    return null;
  }
  return invitation;
}

type JoinInvitation = { id: number; accountId: number; email: string; role: string };

export type JoinEligibility =
  | { ok: true }
  | { ok: false; status: 403 | 409; reason: string };

/** Тарифы без «оплаченного срока»: пробный и безлимитный (служебный). */
const NO_PAID_TERM_PLANS = new Set(["trial", "unlimited"]);

/**
 * Может ли УЖЕ зарегистрированный пользователь вступить в аккаунт по
 * приглашению. Вызывается и из POST /api/invite/join (внутри транзакции, где
 * потом идёт запись), и из GET /api/invite/info (чтобы страница показала
 * причину отказа ДО кнопки) — одна функция, чтобы тексты и правила не
 * разошлись.
 *
 * Вступление ЗАКРЫВАЕТ прежний аккаунт пользователя, поэтому правила
 * защищают тех, кого это задело бы: других людей в старом аккаунте и деньги,
 * уже уплаченные за него.
 */
export async function checkJoinEligibility(
  tx: Tx,
  userId: number,
  invitation: JoinInvitation,
): Promise<JoinEligibility> {
  const user = await tx.user.findUnique({
    where: { id: userId },
    include: { account: { include: { plan: true } } },
  });
  if (!user || !user.isActive) {
    return { ok: false, status: 403, reason: "Требуется вход в систему." };
  }
  // Адрес сверяем первым: приглашение личное, чужой сессии нельзя
  // раскрывать ничего про состояние её аккаунта.
  if (user.email.toLowerCase() !== invitation.email.toLowerCase()) {
    return {
      ok: false,
      status: 403,
      reason: `Приглашение выписано на другой адрес. Войдите под ${invitation.email}.`,
    };
  }
  const target = await tx.account.findUnique({ where: { id: invitation.accountId } });
  if (!target || !target.isActive) {
    return { ok: false, status: 403, reason: "Аккаунт отключён." };
  }
  if (user.role === "root") {
    return {
      ok: false,
      status: 403,
      reason: "Эта учётная запись не может вступать в другие аккаунты.",
    };
  }
  if (user.accountId === invitation.accountId) {
    return { ok: false, status: 409, reason: "Вы уже состоите в этом аккаунте." };
  }

  // Правило единственного участника: старый аккаунт будет закрыт, а вместе с
  // ним потеряли бы доступ остальные его активные пользователи; живое
  // приглашение тоже умерло бы вместе с аккаунтом (активация упала бы).
  const [others, pending] = await Promise.all([
    tx.user.count({ where: { accountId: user.accountId, isActive: true, id: { not: user.id } } }),
    tx.invitation.count({ where: { accountId: user.accountId, ...pendingInvitationWhere() } }),
  ]);
  if (others > 0 || pending > 0) {
    return {
      ok: false,
      status: 409,
      reason:
        "Нельзя вступить в другой аккаунт, пока в вашем аккаунте есть другие пользователи " +
        "или неактивированные приглашения. Отключите их или передайте управление.",
    };
  }

  // Неистёкший оплаченный срок: деньги уплачены за аккаунт, который мы
  // собирались закрыть, — автоматического возврата/переноса нет, решает
  // поддержка. Пробный период и истёкший срок отпускаем: терять там нечего.
  const { plan, paidUntil } = user.account;
  if (
    !NO_PAID_TERM_PLANS.has(plan.code) &&
    paidUntil != null &&
    paidUntil.getTime() > Date.now()
  ) {
    return {
      ok: false,
      status: 409,
      reason:
        `В вашем аккаунте действует оплаченный тариф до ${formatDateRu(paidUntil)}. ` +
        "Переход в другой аккаунт сейчас невозможен — напишите в поддержку.",
    };
  }
  return { ok: true };
}

export type JoinOutcome =
  | { ok: true; accountName: string }
  | { ok: false; status: 403 | 404 | 409; reason: string };

/**
 * Вступление существующего пользователя в аккаунт по приглашению. Вызывать
 * ВНУТРИ prisma.$transaction: проверка и запись обязаны идти атомарно, иначе
 * между ними успеет появиться второй пользователь/приглашение.
 *
 * Чего функция НЕ делает, и это решение продукта:
 *  - UsageRecord / QuotaCounter / QuotaGrant остаются у СТАРОГО аккаунта.
 *    История обработки не переносится, а прошлый расход не должен засчитываться
 *    в лимит нового аккаунта.
 *  - Новая запись Consent не создаётся: согласие на обработку ПДн дано при
 *    регистрации и принадлежит человеку, а не аккаунту; плательщик нового
 *    аккаунта — его администратор.
 *
 * Закрытие старого аккаунта не разлогинивает пользователя: auth.ts на каждый
 * запрос читает ТЕКУЩИЙ user.accountId, а после переноса он указывает на новый
 * активный аккаунт — isActive старого уже ни на что не влияет.
 */
export async function performJoin(tx: Tx, userId: number, token: string): Promise<JoinOutcome> {
  const invitation = await findLiveInvitationByToken(tx, token);
  if (!invitation) return { ok: false, status: 404, reason: INVITE_INVALID_MESSAGE };

  const check = await checkJoinEligibility(tx, userId, invitation);
  if (!check.ok) return check;

  const user = await tx.user.findUniqueOrThrow({ where: { id: userId } });
  const target = await tx.account.findUniqueOrThrow({ where: { id: invitation.accountId } });

  await tx.user.update({
    where: { id: userId },
    data: { accountId: invitation.accountId, role: invitation.role },
  });
  await tx.account.update({ where: { id: user.accountId }, data: { isActive: false } });
  await tx.invitation.update({
    where: { id: invitation.id },
    data: { acceptedAt: new Date(), acceptedByUserId: userId },
  });
  return { ok: true, accountName: target.name };
}
