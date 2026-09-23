import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";
import { isValidEmail } from "@/lib/password";
import {
  getSeatUsage,
  inviteExpiryFrom,
  inviteUrl,
  newInviteToken,
  pendingInvitationWhere,
} from "@/lib/invitations";

export const runtime = "nodejs";

/** Роли, которым разрешено приглашать в свой аккаунт. */
const MANAGER_ROLES = new Set(["root", "admin"]);

/** Роли, которые можно выдать приглашением. 'root' — никогда, он заводится сидом. */
const INVITABLE_ROLES = new Set(["admin", "member"]);

type Manager = { userId: number; accountId: number; role: string };

/**
 * Управлять составом аккаунта может только его админ (или root). Проверка
 * идёт по СЕССИИ, а не по телу запроса: accountId из тела позволил бы
 * приглашать в чужой аккаунт.
 */
async function requireManager(): Promise<Manager | NextResponse> {
  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;
  const accountId = typeof session?.accountId === "number" ? session.accountId : null;
  const role = typeof session?.role === "string" ? session.role : "";
  if (userId == null || accountId == null || !Number.isFinite(userId)) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }
  if (!MANAGER_ROLES.has(role)) {
    return NextResponse.json(
      { error: "Управлять пользователями аккаунта может только администратор." },
      { status: 403 },
    );
  }
  return { userId, accountId, role };
}

/**
 * POST /api/account/invitations — выписать приглашение в свой аккаунт.
 *
 * Возвращает ссылку активации ОДИН раз: в БД лежит только хеш токена (см.
 * lib/invitations.ts), восстановить ссылку потом нельзя — только отозвать
 * приглашение и выписать новое.
 *
 * Лимит мест (`Plan.maxUsers`) проверяется ВНУТРИ транзакции вместе со
 * вставкой — иначе два одновременных приглашения на последнее место оба
 * прошли бы проверку.
 */
export async function POST(req: NextRequest) {
  const manager = await requireManager();
  if (manager instanceof NextResponse) return manager;

  let body: { email?: unknown; role?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }

  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const role = typeof body.role === "string" ? body.role : "member";
  if (!isValidEmail(email)) {
    return NextResponse.json({ error: "Некорректный адрес электронной почты." }, { status: 400 });
  }
  if (!INVITABLE_ROLES.has(role)) {
    return NextResponse.json({ error: "Недопустимая роль." }, { status: 400 });
  }

  const { token, tokenHash } = newInviteToken();

  try {
    const created = await prisma.$transaction(async (tx) => {
      const seats = await getSeatUsage(manager.accountId, tx);
      if (!seats) throw new Error("account gone");
      if (seats.free != null && seats.free <= 0) {
        // Разворачиваем в простые поля, а не возвращаем объект seats целиком:
        // сузить union по `in` для вложенного объекта TypeScript не может.
        return { limitReached: true as const, taken: seats.taken, limit: seats.limit };
      }

      // Адрес уже зарегистрирован — приглашать некого: users.email уникален
      // на весь сервис, и активация всё равно упала бы. Отвечаем заранее и
      // не тратим место на заведомо мёртвое приглашение. Это НЕ раскрытие
      // чужой регистрации: приглашать может только админ, и только в свой
      // аккаунт, где состав ему и так виден.
      const existing = await tx.user.findUnique({ where: { email } });
      if (existing) {
        return { alreadyUser: true as const };
      }

      // Живое приглашение на тот же адрес в этот аккаунт — не плодим
      // дубликаты, каждый из которых ест место.
      const duplicate = await tx.invitation.findFirst({
        where: { accountId: manager.accountId, email, ...pendingInvitationWhere() },
      });
      if (duplicate) {
        return { duplicate: true as const };
      }

      const invitation = await tx.invitation.create({
        data: {
          accountId: manager.accountId,
          email,
          role,
          tokenHash,
          createdByUserId: manager.userId,
          expiresAt: inviteExpiryFrom(),
        },
      });
      return { invitation };
    });

    if ("limitReached" in created) {
      return NextResponse.json(
        {
          error:
            `Лимит пользователей по тарифу исчерпан: занято ${created.taken} из ` +
            `${created.limit}. Отключите пользователя, отзовите приглашение или ` +
            "перейдите на тариф с большим числом мест.",
        },
        { status: 402 },
      );
    }
    if ("alreadyUser" in created) {
      return NextResponse.json(
        { error: "Пользователь с таким адресом уже зарегистрирован." },
        { status: 409 },
      );
    }
    if ("duplicate" in created) {
      return NextResponse.json(
        { error: "Приглашение на этот адрес уже выписано и ещё действует." },
        { status: 409 },
      );
    }

    const origin = req.nextUrl.origin;
    return NextResponse.json({
      id: created.invitation.id,
      email: created.invitation.email,
      role: created.invitation.role,
      expiresAt: created.invitation.expiresAt.toISOString(),
      // Единственный раз, когда ссылка существует в открытом виде.
      url: inviteUrl(origin, token),
    });
  } catch (e) {
    console.error("[/api/account/invitations] не удалось создать приглашение:", e);
    return NextResponse.json({ error: "Не удалось создать приглашение." }, { status: 500 });
  }
}

/**
 * DELETE /api/account/invitations?id=... — отозвать приглашение и освободить
 * место. Ссылка после этого не активируется (см. accept-маршрут).
 */
export async function DELETE(req: NextRequest) {
  const manager = await requireManager();
  if (manager instanceof NextResponse) return manager;

  const id = Number(req.nextUrl.searchParams.get("id"));
  if (!Number.isInteger(id) || id <= 0) {
    return NextResponse.json({ error: "Не указано приглашение." }, { status: 400 });
  }

  // updateMany с accountId в условии, а не update по id: приглашение чужого
  // аккаунта не должно отзываться даже по прямому обращению к API.
  const updated = await prisma.invitation.updateMany({
    where: { id, accountId: manager.accountId, acceptedAt: null, revokedAt: null },
    data: { revokedAt: new Date() },
  });
  if (updated.count === 0) {
    return NextResponse.json({ error: "Приглашение не найдено." }, { status: 404 });
  }
  return NextResponse.json({ ok: true });
}
