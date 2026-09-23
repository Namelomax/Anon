import { NextRequest, NextResponse } from "next/server";
import { hash } from "@node-rs/argon2";
import { prisma } from "@/lib/db";
import { validatePasswordStrength } from "@/lib/password";
import { hashInviteToken } from "@/lib/invitations";

export const runtime = "nodejs";

// @node-rs/argon2 экспортирует Algorithm как `const enum` — при isolatedModules
// (обязателен для Next.js) его нельзя импортировать типобезопасно, поэтому
// числовое значение напрямую (тот же приём, что в /api/register и seed.ts).
const ARGON2ID = 2; // Algorithm.Argon2id

// Та же переменная, что в /api/register: редакция документов, под которой
// фиксируется согласие (см. Consent.documentVersion в schema.prisma).
const LEGAL_DOCS_VERSION = process.env.LEGAL_DOCS_VERSION || "1.0";

function getClientIp(req: NextRequest): string | null {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) return xff.split(",")[0]!.trim();
  return req.headers.get("x-real-ip");
}

/**
 * POST /api/invite/accept — активация приглашения: приглашённый задаёт пароль
 * и САМ отмечает согласие на обработку ПДн.
 *
 * Пользователь и запись согласия создаются здесь, а не в момент выписки
 * приглашения, и это принципиально: согласие даёт субъект, а не админ за
 * него (ЮРИДИЧЕСКИЕ_ДОКУМЕНТЫ/10_Авторизация_биллинг_и_учётные_данные.md,
 * раздел 2 — «форма принятия при активации приглашения»). Поэтому маршрут
 * открытый: у активирующего ещё нет учётной записи, войти ему нечем.
 */
export async function POST(req: NextRequest) {
  let body: { token?: unknown; password?: unknown; consent?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }

  const token = typeof body.token === "string" ? body.token.trim() : "";
  const password = typeof body.password === "string" ? body.password : "";
  const consent = body.consent === true;

  if (!token) {
    return NextResponse.json({ error: "Ссылка приглашения недействительна." }, { status: 400 });
  }
  const passwordError = validatePasswordStrength(password);
  if (passwordError) {
    return NextResponse.json({ error: passwordError }, { status: 400 });
  }
  if (!consent) {
    return NextResponse.json(
      { error: "Активация невозможна без согласия на обработку персональных данных." },
      { status: 400 },
    );
  }

  const passwordHash = await hash(password, { algorithm: ARGON2ID });
  const ip = getClientIp(req);
  const tokenHash = hashInviteToken(token);

  try {
    const outcome = await prisma.$transaction(async (tx) => {
      const invitation = await tx.invitation.findUnique({ where: { tokenHash } });
      // Одинаковый отказ на «нет такого», «уже активировано», «отозвано» и
      // «протухло»: подбирать токен нечем, но и подсказывать его состояние
      // незачем.
      if (
        !invitation ||
        invitation.acceptedAt != null ||
        invitation.revokedAt != null ||
        invitation.expiresAt.getTime() <= Date.now()
      ) {
        return { invalid: true as const };
      }

      const account = await tx.account.findUnique({ where: { id: invitation.accountId } });
      if (!account || !account.isActive) {
        return { accountGone: true as const };
      }

      const existing = await tx.user.findUnique({ where: { email: invitation.email } });
      if (existing) {
        return { alreadyUser: true as const };
      }

      // Проверки свободных мест здесь НЕТ — и это не упущение. Живое
      // приглашение УЖЕ занимает место (см. шапку lib/invitations.ts), а
      // активация лишь превращает его в пользователя: занятых мест ровно
      // столько же, сколько было секунду назад. Отказать тут значило бы
      // наказать приглашённого за то, что админ после выписки включил
      // обратно отключённого пользователя, — при этом мест бы не
      // прибавилось. Лимит держится там, где занятость растёт: на выписке
      // приглашения и на включении пользователя.

      const user = await tx.user.create({
        data: {
          accountId: invitation.accountId,
          email: invitation.email,
          passwordHash,
          role: invitation.role,
          isActive: true,
          createdById: invitation.createdByUserId,
        },
      });
      await tx.consent.create({
        data: {
          userId: user.id,
          kind: "pd_processing",
          documentVersion: LEGAL_DOCS_VERSION,
          ip,
          granted: true,
        },
      });
      await tx.invitation.update({
        where: { id: invitation.id },
        data: { acceptedAt: new Date(), acceptedByUserId: user.id },
      });
      return { email: user.email };
    });

    if ("invalid" in outcome) {
      return NextResponse.json(
        { error: "Ссылка приглашения недействительна или истекла." },
        { status: 404 },
      );
    }
    if ("accountGone" in outcome) {
      return NextResponse.json({ error: "Аккаунт отключён." }, { status: 403 });
    }
    if ("alreadyUser" in outcome) {
      return NextResponse.json(
        { error: "Пользователь с таким адресом уже зарегистрирован." },
        { status: 409 },
      );
    }
    return NextResponse.json({ ok: true, email: outcome.email });
  } catch (e) {
    console.error("[/api/invite/accept] активация не удалась:", e);
    return NextResponse.json({ error: "Не удалось активировать приглашение." }, { status: 500 });
  }
}
