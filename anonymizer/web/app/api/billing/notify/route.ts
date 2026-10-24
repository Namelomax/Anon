import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { activatePlan } from "@/lib/plan-activation";
import { tbankConfig, verifyToken } from "@/lib/tbank";

export const runtime = "nodejs";

/**
 * POST /api/billing/notify — HTTP-уведомления Т-Банка о статусе платежа
 * (NotificationURL из Init, см. lib/tbank.ts). Публичный: сессии у банка нет,
 * подлинность — только по подписи Token.
 *
 * Банк считает уведомление доставленным лишь при ответе 200 с телом «OK» и
 * иначе повторяет его (раз в час сутки, потом раз в сутки месяц). Поэтому
 * «OK» отвечаем и тогда, когда уведомление осмысленно отвергнуто (чужой заказ,
 * не та сумма) — повтор ничего не исправит; не-OK — только при сбое у нас,
 * чтобы банк повторил позже.
 *
 * Тариф включается на CONFIRMED (одностадийная оплата: PayType=O) ровно один
 * раз: условный UPDATE по `activatedAt = null` внутри транзакции, так что
 * повтор уведомления или два одновременных не включат тариф дважды.
 */

/** Статусы, которые не должны перетирать уже подтверждённый платёж (уведомления приходят не по порядку). */
const BEFORE_CONFIRMED = new Set(["NEW", "FORM_SHOWED", "AUTHORIZING", "AUTHORIZED", "3DS_CHECKING", "3DS_CHECKED"]);

function ok() {
  return new NextResponse("OK", { status: 200, headers: { "Content-Type": "text/plain" } });
}

export async function POST(req: NextRequest) {
  const cfg = tbankConfig();
  if (!cfg) {
    console.error("[billing/notify] уведомление пришло, но TBANK_* не настроены");
    return new NextResponse("NOT CONFIGURED", { status: 503 });
  }

  let n: Record<string, unknown>;
  try {
    n = await req.json();
  } catch {
    return new NextResponse("BAD REQUEST", { status: 400 });
  }
  if (!n || typeof n !== "object" || !verifyToken(n, cfg.password)) {
    console.error("[billing/notify] неверная подпись уведомления, отклонено");
    return new NextResponse("BAD TOKEN", { status: 403 });
  }
  if (n.TerminalKey !== cfg.terminalKey) {
    console.error(`[billing/notify] уведомление другого терминала: ${String(n.TerminalKey)}`);
    return ok();
  }

  const orderId = typeof n.OrderId === "string" ? n.OrderId : String(n.OrderId ?? "");
  const status = typeof n.Status === "string" ? n.Status : "";
  const amount = Number(n.Amount);
  const tbankPaymentId = n.PaymentId != null ? String(n.PaymentId) : null;
  const errorCode = n.ErrorCode != null ? String(n.ErrorCode) : null;

  try {
    const payment = await prisma.payment.findUnique({ where: { orderId } });
    if (!payment) {
      console.error(`[billing/notify] неизвестный заказ ${orderId} (статус ${status})`);
      return ok();
    }
    if (amount !== payment.amountKopecks) {
      // Сумма зафиксирована при создании платежа; расхождение — повод
      // разбираться вручную, но не включать тариф.
      console.error(
        `[billing/notify] заказ ${orderId}: сумма ${amount} ≠ ожидаемой ${payment.amountKopecks}, тариф НЕ включён`,
      );
      await prisma.payment.update({
        where: { id: payment.id },
        data: { status: "AMOUNT_MISMATCH", errorCode },
      });
      return ok();
    }

    const keepStatus = payment.status === "CONFIRMED" && BEFORE_CONFIRMED.has(status);
    await prisma.payment.update({
      where: { id: payment.id },
      data: {
        ...(keepStatus ? {} : { status }),
        errorCode,
        tbankPaymentId: tbankPaymentId ?? payment.tbankPaymentId,
      },
    });

    if (status === "CONFIRMED" && n.Success !== false) {
      const plan = await prisma.plan.findUnique({ where: { code: payment.planCode } });
      if (!plan) throw new Error(`план ${payment.planCode} из заказа ${orderId} не найден`);
      const activated = await prisma.$transaction(async (tx) => {
        const claim = await tx.payment.updateMany({
          where: { id: payment.id, activatedAt: null },
          data: { activatedAt: new Date() },
        });
        if (claim.count === 0) return null; // уже включён предыдущим уведомлением
        return activatePlan(tx, {
          accountId: payment.accountId,
          planId: plan.id,
          planCode: plan.code,
          months: payment.months,
          note: `Т-Банк, заказ ${orderId}, платёж ${tbankPaymentId ?? "?"}`,
          externalPaymentId: tbankPaymentId,
        });
      });
      if (activated) {
        console.log(
          `[billing/notify] аккаунт ${payment.accountId}: тариф ${plan.code} на ${payment.months} мес. ` +
            `до ${activated.paidUntil.toISOString()} (заказ ${orderId})` +
            (activated.lostDays > 0 ? `, сгорело ${activated.lostDays} дн. прежнего срока` : ""),
        );
      }
    } else if (payment.activatedAt && ["REFUNDED", "PARTIAL_REFUNDED", "REVERSED"].includes(status)) {
      // Возврат тариф сам не отключает: это решение оператора (activate-plan
      // или правка срока вручную).
      console.warn(
        `[billing/notify] заказ ${orderId}: статус ${status} по уже включённому тарифу — проверьте аккаунт ${payment.accountId}`,
      );
    }
    return ok();
  } catch (e) {
    console.error(`[billing/notify] сбой обработки заказа ${orderId}:`, e);
    return new NextResponse("ERROR", { status: 500 });
  }
}
