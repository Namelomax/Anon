import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";
import { INVITE_INVALID_MESSAGE, performJoin } from "@/lib/invitations";

export const runtime = "nodejs";

/**
 * POST /api/invite/join — вступление УЖЕ зарегистрированного пользователя в
 * аккаунт по приглашению. В отличие от /api/invite/accept маршрут закрытый:
 * личность берётся из сессии, а не из тела, и совпадение адреса сессии с
 * адресом приглашения проверяется внутри performJoin. Правила и смысл
 * действия (старый аккаунт закрывается) — см. lib/invitations.ts.
 */
export async function POST(req: NextRequest) {
  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;
  if (userId == null || !Number.isFinite(userId)) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }

  let body: { token?: unknown; confirm?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }
  const token = typeof body.token === "string" ? body.token.trim() : "";
  if (!token) {
    return NextResponse.json({ error: INVITE_INVALID_MESSAGE }, { status: 400 });
  }
  if (body.confirm !== true) {
    return NextResponse.json(
      { error: "Подтвердите, что понимаете: текущий аккаунт будет закрыт." },
      { status: 400 },
    );
  }

  try {
    const outcome = await prisma.$transaction((tx) => performJoin(tx, userId, token));
    if (!outcome.ok) {
      return NextResponse.json({ error: outcome.reason }, { status: outcome.status });
    }
    return NextResponse.json({ ok: true, accountName: outcome.accountName });
  } catch (e) {
    console.error("[/api/invite/join] вступление не удалось:", e);
    return NextResponse.json({ error: "Не удалось вступить в аккаунт." }, { status: 500 });
  }
}
