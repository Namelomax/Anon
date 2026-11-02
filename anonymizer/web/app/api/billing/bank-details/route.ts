import { NextRequest, NextResponse } from "next/server";
import { auth } from "@/auth";
import { prisma } from "@/lib/db";
import { LICENSOR } from "@/lib/legal";
import { bankTransferPurpose, bankTransferRecipient, bankTransferSelection } from "@/lib/bank-transfer";
import type { BankTransferQuote } from "@/lib/bank-transfer-types";

export const runtime = "nodejs";

/** Только чтение. Аккаунт берётся из сессии, цена — из Plan; банковских запросов нет. */
export async function GET(req: NextRequest) {
  const respond = (body: BankTransferQuote | { error: string }, status = 200) =>
    NextResponse.json(body, { status, headers: { "Cache-Control": "no-store" } });
  try {
    const session = await auth();
    const userId = Number(session?.user?.id);
    const accountId = session?.accountId;
    if (!Number.isSafeInteger(userId) || userId <= 0 || typeof accountId !== "number" ||
        !Number.isSafeInteger(accountId) || accountId <= 0) {
      return respond({ error: "Требуется вход в систему." }, 401);
    }
    const user = await prisma.user.findUnique({
      where: { id: userId },
      select: { accountId: true, isActive: true, role: true, account: { select: { isActive: true } } },
    });
    if (!user || !user.isActive || user.accountId !== accountId || !user.account.isActive) {
      return respond({ error: "Аккаунт недоступен." }, 403);
    }
    if (user.role !== "admin" && user.role !== "root") {
      return respond({ error: "Банковская оплата доступна администратору аккаунта." }, 403);
    }
    const url = new URL(req.url);
    const planCode = url.searchParams.get("plan")?.trim() ?? "";
    const term = url.searchParams.get("months");
    const months = term === "1" ? 1 : term === "12" ? 12 : null;
    if (!planCode || planCode.length > 128 || months == null ||
        url.searchParams.getAll("plan").length !== 1 || url.searchParams.getAll("months").length !== 1) {
      return respond({ error: "Выберите тариф и срок: 1 или 12 месяцев." }, 400);
    }
    const plan = await prisma.plan.findUnique({ where: { code: planCode } });
    const selection = plan ? bankTransferSelection(plan, months) : null;
    if (!plan || !selection) {
      return respond({ error: "Тариф недоступен для выбранного срока." }, 400);
    }
    const recipient = bankTransferRecipient();
    if (!recipient) return respond({ configured: false, selection, supportEmail: LICENSOR.email });
    return respond({
      configured: true,
      recipient,
      selection,
      purpose: bankTransferPurpose(accountId, plan.id, months),
      supportEmail: LICENSOR.email,
    });
  } catch {
    return respond({ error: "Не удалось получить реквизиты. Попробуйте ещё раз." }, 503);
  }
}
