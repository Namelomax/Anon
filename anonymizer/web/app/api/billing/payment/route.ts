import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";

export const runtime = "nodejs";

/**
 * GET /api/billing/payment?order=<OrderId> — состояние платежа для страницы
 * результата (app/payment/result). Только платежи СВОЕГО аккаунта: аккаунт из
 * сессии, а не из запроса. Ничего не меняет — тариф включает только
 * уведомление банка (../notify), страница лишь ждёт его.
 */
export async function GET(req: NextRequest) {
  const session = await auth();
  const accountId = typeof session?.accountId === "number" ? session.accountId : null;
  if (accountId == null) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }
  const orderId = req.nextUrl.searchParams.get("order") ?? "";
  const payment = orderId ? await prisma.payment.findUnique({ where: { orderId } }) : null;
  if (!payment || payment.accountId !== accountId) {
    return NextResponse.json({ error: "Платёж не найден." }, { status: 404 });
  }
  const plan = await prisma.plan.findUnique({ where: { code: payment.planCode } });
  return NextResponse.json(
    {
      status: payment.status,
      activated: payment.activatedAt != null,
      planTitle: plan?.title ?? payment.planCode,
      months: payment.months,
      amountKopecks: payment.amountKopecks,
      refundedKopecks: payment.refundedKopecks,
      accessUntil: payment.accessUntil?.toISOString() ?? null,
      errorCode: payment.errorCode,
    },
    { headers: { "Cache-Control": "no-store" } },
  );
}
