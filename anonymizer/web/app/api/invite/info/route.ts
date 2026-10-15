import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";
import {
  INVITE_INVALID_MESSAGE,
  checkJoinEligibility,
  findLiveInvitationByToken,
} from "@/lib/invitations";

export const runtime = "nodejs";

/**
 * GET /api/invite/info?token=... — что показать на странице приглашения.
 * Маршрут открытый (активирующему нечем войти), поэтому отдаёт только то,
 * без чего страница не нарисуется: название аккаунта, адрес и роль из самого
 * приглашения, признак «адрес уже зарегистрирован». Любое недействительное
 * состояние токена — один и тот же 404, как в /api/invite/accept.
 *
 * Если у вызывающего есть сессия, добавляется `joinCheck` — может ли он
 * вступить и, если нет, почему (тот же текст, что вернёт /api/invite/join).
 */
export async function GET(req: NextRequest) {
  const token = req.nextUrl.searchParams.get("token")?.trim() ?? "";
  const notFound = () => NextResponse.json({ error: INVITE_INVALID_MESSAGE }, { status: 404 });
  if (!token) return notFound();

  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;

  const result = await prisma.$transaction(async (tx) => {
    const invitation = await findLiveInvitationByToken(tx, token);
    if (!invitation) return null;
    const account = await tx.account.findUnique({ where: { id: invitation.accountId } });
    // Отключённый аккаунт неотличим от недействительной ссылки.
    if (!account || !account.isActive) return null;
    const existing = await tx.user.findUnique({ where: { email: invitation.email } });

    let joinCheck: { ok: true } | { ok: false; reason: string } | undefined;
    if (userId != null && Number.isFinite(userId)) {
      const check = await checkJoinEligibility(tx, userId, invitation);
      joinCheck = check.ok ? { ok: true } : { ok: false, reason: check.reason };
    }
    return {
      accountName: account.name,
      email: invitation.email,
      role: invitation.role,
      existingUser: existing != null,
      ...(joinCheck ? { joinCheck } : {}),
    };
  });

  return result ? NextResponse.json(result) : notFound();
}
