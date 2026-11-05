import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";
import { prisma } from "@/lib/db";
import { issueApiKey } from "@/lib/api-keys";

export const runtime = "nodejs";
const metadata = { id: true, name: true, prefix: true, createdAt: true, lastUsedAt: true, revokedAt: true } as const;
const json = (body: unknown, status = 200) => NextResponse.json(body, { status, headers: { "Cache-Control": "no-store" } });

async function manager() {
  const session = await auth();
  const userId = Number(session?.user?.id), accountId = session?.accountId;
  if (!Number.isSafeInteger(userId) || userId <= 0 || typeof accountId !== "number" ||
      !Number.isSafeInteger(accountId) || accountId <= 0) return json({ error: "Требуется вход в систему." }, 401);
  const user = await prisma.user.findUnique({ where: { id: userId },
    select: { accountId: true, role: true, isActive: true, account: { select: { isActive: true } } } });
  if (!user || user.accountId !== accountId || !user.isActive || !user.account.isActive ||
      !["root", "admin"].includes(user.role)) return json({ error: "Управлять API-ключами может администратор аккаунта." }, 403);
  return { userId, accountId };
}

function sameOrigin(request: NextRequest) {
  const origin = request.headers.get("origin");
  return request.headers.get("sec-fetch-site") !== "cross-site" && (!origin || origin === new URL(request.url).origin);
}

export async function GET() {
  try {
    const caller = await manager();
    if (caller instanceof NextResponse) return caller;
    const keys = await prisma.apiKey.findMany({ where: { user: { accountId: caller.accountId } },
      orderBy: { createdAt: "desc" }, select: metadata });
    return json({ keys });
  } catch { return json({ error: "Не удалось загрузить API-ключи." }, 503); }
}

export async function POST(request: NextRequest) {
  if (!sameOrigin(request)) return json({ error: "Недопустимый источник запроса." }, 403);
  try {
    const caller = await manager();
    if (caller instanceof NextResponse) return caller;
    let body: unknown;
    try { body = await request.json(); } catch { return json({ error: "Некорректный JSON." }, 400); }
    if (!body || typeof body !== "object" || Array.isArray(body)) return json({ error: "Укажите название ключа." }, 400);
    const rawName = (body as { name?: unknown }).name;
    if (rawName != null && typeof rawName !== "string") return json({ error: "Некорректное название ключа." }, 400);
    const name = typeof rawName === "string" ? rawName.trim() : "";
    if (name.length > 80 || /[\u0000-\u001f\u007f]/.test(name)) return json({ error: "Название должно содержать до 80 символов без переносов строк." }, 400);
    const issued = issueApiKey(caller.accountId);
    const apiKey = await prisma.$transaction(async (tx) => {
      const count = await tx.apiKey.count({ where: { user: { accountId: caller.accountId }, revokedAt: null } });
      if (count >= 20) return null;
      return tx.apiKey.create({ data: { userId: caller.userId, name: name || null, keyHash: issued.keyHash, prefix: issued.prefix }, select: metadata });
    });
    if (!apiKey) return json({ error: "Достигнут лимит: 20 активных ключей на аккаунт. Отзовите ненужный ключ." }, 409);
    return json({ key: issued.key, apiKey }, 201);
  } catch { return json({ error: "Не удалось создать API-ключ." }, 503); }
}

export async function DELETE(request: NextRequest) {
  if (!sameOrigin(request)) return json({ error: "Недопустимый источник запроса." }, 403);
  try {
    const caller = await manager();
    if (caller instanceof NextResponse) return caller;
    const query = new URL(request.url).searchParams;
    const id = Number(query.get("id"));
    if (!Number.isSafeInteger(id) || id <= 0 || query.getAll("id").length !== 1) return json({ error: "Некорректный идентификатор ключа." }, 400);
    const where = { id, user: { accountId: caller.accountId } };
    const updated = await prisma.apiKey.updateMany({ where: { ...where, revokedAt: null }, data: { revokedAt: new Date() } });
    if (updated.count === 0 && !await prisma.apiKey.findFirst({ where, select: { id: true } })) return json({ error: "Ключ не найден." }, 404);
    return json({ revoked: true });
  } catch { return json({ error: "Не удалось отозвать API-ключ." }, 503); }
}
