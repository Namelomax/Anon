/**
 * Тестовая оплата по QR СБП на DEMO-терминале Т-Банка. Тестовый QR не
 * оплатить настоящим банковским приложением — вместо телефона «оплачивает»
 * метод банка SbpPayTest («Создать тестовую платёжную сессию»).
 *
 *   set -a; . ./.env.local; set +a
 *   npx tsx scripts/sbp-test-pay.ts [--order <OrderId>] [--reject | --expire]
 *
 * Порядок: в кабинете нажать кнопку оплаты → на форме банка выбрать СБП
 * (появится QR) → запустить скрипт. Без --order берётся последний платёж,
 * который ещё не оплачен. Дальше всё как при настоящей оплате: банк шлёт
 * уведомление CONFIRMED на /api/billing/notify, тариф включается, форма
 * сама уводит покупателя на SuccessURL.
 *
 * --reject — имитировать отказ (REJECTED), --expire — истечение QR
 * (DEADLINE_EXPIRED). На боевом терминале (без DEMO) скрипт не запускается.
 */
import { PrismaClient } from "@prisma/client";
import { callMethod, tbankConfig } from "../lib/tbank";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

function fail(msg: string): never {
  console.error(`[sbp-test-pay] ${msg}`);
  process.exit(1);
}

async function main() {
  if (!process.env.DATABASE_URL) {
    fail("DATABASE_URL не задан. Выполните: set -a; . ./.env.local; set +a");
  }
  const cfg = tbankConfig();
  if (!cfg) fail("TBANK_TERMINAL_KEY/TBANK_PASSWORD не заданы в .env.local.");
  if (!cfg.terminalKey.endsWith("DEMO")) {
    fail(`терминал ${cfg.terminalKey} боевой — тестовая оплата только на DEMO.`);
  }

  const prisma = new PrismaClient();
  try {
    const orderId = arg("order");
    const payment = orderId
      ? await prisma.payment.findUnique({ where: { orderId } })
      : await prisma.payment.findFirst({
          where: { activatedAt: null, tbankPaymentId: { not: null }, status: { in: ["NEW", "FORM_SHOWED"] } },
          orderBy: { id: "desc" },
        });
    if (!payment?.tbankPaymentId) {
      fail(orderId ? `заказ ${orderId} не найден или не зарегистрирован в банке.` : "неоплаченных платежей нет.");
    }

    const res = await callMethod<{ Success: boolean; ErrorCode: string; Message?: string; Details?: string }>(
      cfg,
      "SbpPayTest",
      {
        PaymentId: payment.tbankPaymentId,
        ...(process.argv.includes("--reject") ? { IsRejected: true } : {}),
        ...(process.argv.includes("--expire") ? { IsDeadlineExpired: true } : {}),
      },
    );
    if (!res.Success) {
      fail(
        `банк отказал (код ${res.ErrorCode}): ${res.Message ?? ""} ${res.Details ?? ""}`.trim() +
          "\nQR должен быть уже показан: на форме банка выберите СБП и запустите скрипт снова.",
      );
    }
    console.log(
      `[sbp-test-pay] заказ ${payment.orderId} (платёж ${payment.tbankPaymentId}, ${payment.planCode} на ` +
        `${payment.months} мес.): тестовая оплата отправлена. Статус придёт уведомлением — ` +
        "см. journalctl --user -u anonymizer-web | grep billing",
    );
  } finally {
    await prisma.$disconnect();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
