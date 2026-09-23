import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { prisma } from "@/lib/db";

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
export function inviteUrl(origin: string, token: string): string {
  return `${origin.replace(/\/$/, "")}/invite/${token}`;
}
