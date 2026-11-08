import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { prisma } from "@/lib/db";

/** Только сессия владельца; права и активность перепроверяются в БД. */
export async function requireRoot() {
  const session = await auth();
  const userId = Number(session?.user?.id);
  const accountId = session?.accountId;
  const deny = (error: string, status: number) => NextResponse.json(
    { error }, { status, headers: { "Cache-Control": "no-store" } },
  );
  if (!Number.isSafeInteger(userId) || userId <= 0 ||
      typeof accountId !== "number" || !Number.isSafeInteger(accountId) || accountId <= 0) {
    return deny("Требуется вход в систему.", 401);
  }
  const user = await prisma.user.findUnique({
    where: { id: userId },
    select: { accountId: true, role: true, isActive: true, account: { select: { isActive: true } } },
  });
  if (!user || user.accountId !== accountId || user.role !== "root" ||
      !user.isActive || !user.account.isActive) {
    return deny("Раздел доступен только владельцу сервиса.", 403);
  }
  return { userId, accountId };
}
