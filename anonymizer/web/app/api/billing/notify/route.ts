import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { activatePlan } from "@/lib/plan-activation";
import { callMethod, tbankConfig, verifyToken } from "@/lib/tbank";

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
 * Тариф включается на CONFIRMED (терминал одностадийный, см. lib/tbank.ts)
 * ровно один раз: атомарный захват по `activatedAt = null`, так что повтор
 * уведомления или два одновременных не включат тариф дважды.
 *
 * Возврат: сумму берём из GetState (цена минус остаток), а не из статуса —
 * у СБП и частичный возврат приходит как REFUNDED. Полный возврат закрывает
 * доступ сразу, если срок этого платежа ещё действует; частичный доступ не
 * трогает, сумма лишь видна в кабинете.
 */

/** Статусы, которые не должны перетирать уже подтверждённый платёж (уведомления приходят не по порядку). */
const BEFORE_CONFIRMED = new Set(["NEW", "FORM_SHOWED", "AUTHORIZING", "AUTHORIZED", "3DS_CHECKING", "3DS_CHECKED"]);

/** Возврат или отмена уже проведённого платежа. */
const REFUND_STATUSES = new Set(["REFUNDED", "PARTIAL_REFUNDED", "REVERSED", "PARTIAL_REVERSED"]);

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
  return serialized(() => handle(n));
}

/**
 * Уведомления обрабатываются строго по одному. Банк шлёт AUTHORIZED и
 * CONFIRMED одновременно, а у SQLite одна блокировка записи на всю базу:
 * параллельная обработка только толкалась бы за неё (так однажды и вышло —
 * таймаут транзакции и невключённый тариф). Процесс сайта один, поток
 * уведомлений — единицы в минуту, так что очереди в памяти достаточно.
 */
let queue: Promise<unknown> = Promise.resolve();
function serialized<T>(fn: () => Promise<T>): Promise<T> {
  const run = queue.then(fn, fn);
  queue = run.catch(() => undefined);
  return run;
}

async function handle(n: Record<string, unknown>): Promise<NextResponse> {
  const orderId = typeof n.OrderId === "string" ? n.OrderId : String(n.OrderId ?? "");
  const status = typeof n.Status === "string" ? n.Status : "";
  const amount = Number(n.Amount);
  const tbankPaymentId = n.PaymentId != null ? String(n.PaymentId) : null;
  const errorCode = n.ErrorCode != null ? String(n.ErrorCode) : null;
  // Каждое уведомление — в журнал: без этого отказ банка (REJECTED) не
  // оставляет следов. Pan/CardId не пишем, хоть номер карты и замаскирован.
  console.log(
    `[billing/notify] заказ ${orderId}: ${status}, Success=${String(n.Success)}, ErrorCode=${errorCode}, ` +
      `Amount=${String(n.Amount)}` +
      (n.Message ? `, ${String(n.Message)}` : ""),
  );

  try {
    const payment = await prisma.payment.findUnique({ where: { orderId } });
    if (!payment) {
      console.error(`[billing/notify] неизвестный заказ ${orderId} (статус ${status})`);
      return ok();
    }
    const isRefund = REFUND_STATUSES.has(status);
    const wasRefunded = REFUND_STATUSES.has(payment.status);

    // Сумму сверяем только у оплаты: у возврата в Amount остаток, а не цена.
    if ((status === "CONFIRMED" || status === "AUTHORIZED") && amount !== payment.amountKopecks) {
      // Сумма зафиксирована при создании платежа; расхождение — повод
      // разбираться вручную, но не включать тариф.
      console.error(
        `[billing/notify] заказ ${orderId}: сумма ${amount} ≠ ожидаемой ${payment.amountKopecks}, тариф НЕ включён`,
      );
      if (!payment.activatedAt && !wasRefunded) {
        await prisma.payment.update({
          where: { id: payment.id },
          data: { status: "AMOUNT_MISMATCH", errorCode },
        });
      }
      return ok();
    }

    // Уведомления приходят не по порядку и повторяются: поздний AUTHORIZED
    // не должен перетирать CONFIRMED, а повтор CONFIRMED — возврат.
    const keepStatus =
      (payment.status === "CONFIRMED" && BEFORE_CONFIRMED.has(status)) ||
      (wasRefunded && (status === "CONFIRMED" || BEFORE_CONFIRMED.has(status)));
    await prisma.payment.update({
      where: { id: payment.id },
      data: {
        ...(keepStatus ? {} : { status }),
        errorCode,
        tbankPaymentId: tbankPaymentId ?? payment.tbankPaymentId,
      },
    });

    if (status === "CONFIRMED" && n.Success !== false && !wasRefunded) {
      await activate(payment.id, orderId, tbankPaymentId);
    } else if (isRefund) {
      await applyRefund(payment.id, orderId);
    }
    return ok();
  } catch (e) {
    console.error(`[billing/notify] сбой обработки заказа ${orderId}:`, e);
    return new NextResponse("ERROR", { status: 500 });
  }
}

async function activate(paymentId: number, orderId: string, tbankPaymentId: string | null) {
  const payment = await prisma.payment.findUniqueOrThrow({ where: { id: paymentId } });
  const plan = await prisma.plan.findUnique({ where: { code: payment.planCode } });
  if (!plan) throw new Error(`план ${payment.planCode} из заказа ${orderId} не найден`);
  // Захват платежа — один атомарный UPDATE: из двух одновременных
  // CONFIRMED тариф включит только тот, чей UPDATE сменил строку.
  const claim = await prisma.payment.updateMany({
    where: { id: payment.id, activatedAt: null },
    data: { activatedAt: new Date() },
  });
  if (claim.count === 0) return; // уже включён предыдущим уведомлением
  let activated;
  try {
    activated = await activatePlan(prisma, {
      accountId: payment.accountId,
      planId: plan.id,
      planCode: plan.code,
      months: payment.months,
      note: `Т-Банк, заказ ${orderId}, платёж ${tbankPaymentId ?? "?"}`,
      externalPaymentId: tbankPaymentId,
    });
  } catch (e) {
    // Тариф не включён — снимаем захват, чтобы повтор уведомления
    // (банк повторит его после ответа не-OK) включил тариф заново.
    await prisma.payment.update({ where: { id: payment.id }, data: { activatedAt: null } });
    throw e;
  }
  await prisma.payment.update({
    where: { id: payment.id },
    data: { accessUntil: activated.paidUntil },
  });
  console.log(
    `[billing/notify] аккаунт ${payment.accountId}: тариф ${plan.code} на ${payment.months} мес. ` +
      `до ${activated.paidUntil.toISOString()} (заказ ${orderId})` +
      (activated.lostDays > 0 ? `, сгорело ${activated.lostDays} дн. прежнего срока` : ""),
  );
}

async function applyRefund(paymentId: number, orderId: string) {
  const cfg = tbankConfig()!;
  const payment = await prisma.payment.findUniqueOrThrow({ where: { id: paymentId } });
  if (!payment.tbankPaymentId) throw new Error(`у заказа ${orderId} нет PaymentId`);

  // Остаток после возврата — из GetState: ответ банка по TLS на наш же
  // подписанный запрос, тут сомневаться не в чем. Сбой — исключение → 500 →
  // банк повторит уведомление, и мы спросим ещё раз.
  const state = await callMethod<{ Success: boolean; ErrorCode: string; Amount?: number | string }>(
    cfg,
    "GetState",
    { PaymentId: payment.tbankPaymentId },
  );
  if (!state.Success || state.Amount == null || !Number.isFinite(Number(state.Amount))) {
    throw new Error(`GetState по заказу ${orderId}: код ${state.ErrorCode}`);
  }
  const remaining = Math.max(0, Number(state.Amount));
  const refunded = Math.min(payment.amountKopecks, payment.amountKopecks - remaining);
  if (refunded <= payment.refundedKopecks) return; // этот возврат уже учтён

  await prisma.payment.update({ where: { id: payment.id }, data: { refundedKopecks: refunded } });
  const full = refunded >= payment.amountKopecks;
  console.log(
    `[billing/notify] заказ ${orderId}: возвращено ${refunded} из ${payment.amountKopecks} коп.` +
      (full ? " (полный возврат)" : " (частичный возврат, доступ не меняется)"),
  );
  if (!full || !payment.activatedAt || !payment.accessUntil) return;

  // Полный возврат закрывает доступ, только если срок аккаунта всё ещё тот,
  // что открыл этот платёж: после новой оплаты чужой срок не трогаем.
  const account = await prisma.account.findUniqueOrThrow({ where: { id: payment.accountId } });
  const now = new Date();
  if (
    account.paidUntil &&
    account.paidUntil.getTime() === payment.accessUntil.getTime() &&
    account.paidUntil.getTime() > now.getTime()
  ) {
    await prisma.$transaction([
      prisma.account.update({ where: { id: account.id }, data: { paidUntil: now } }),
      prisma.quotaGrant.create({
        data: {
          accountId: account.id,
          period: null,
          pagesTenths: 0,
          reason: `Возврат: заказ ${orderId}, ${refunded} коп. — доступ закрыт`,
          externalPaymentId: payment.tbankPaymentId,
        },
      }),
    ]);
    console.log(`[billing/notify] аккаунт ${account.id}: полный возврат по заказу ${orderId}, доступ закрыт`);
  } else {
    console.warn(
      `[billing/notify] заказ ${orderId}: полный возврат, но срок аккаунта ${account.id} уже другой — не трогаю`,
    );
  }
}
