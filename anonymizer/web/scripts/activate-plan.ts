/**
 * Ручная фиксация оплаты: переводит аккаунт на тариф и открывает оплаченный
 * срок. Онлайн-оплаты нет, поэтому после поступления денег оператор запускает:
 *
 *   set -a; . ./.env.local; set +a
 *   npx tsx scripts/activate-plan.ts --account <id или email пользователя> \
 *     --plan <код плана> --months <1|12> [--note "счёт №…"] [--force]
 *
 * `set -a; . ./.env.local; set +a` обязателен: CLI (в отличие от Next.js) не
 * читает .env.local сам, а без DATABASE_URL Prisma не знает, какую БД
 * открывать (так же устроен deploy/update.sh).
 *
 * Что делает: planId = план, periodAnchorAt = сейчас, paidUntil = сейчас +
 * months календарных месяцев; добавляет аудиторскую строку QuotaGrant (pagesTenths=0,
 * reason «Оплата: …»), чтобы платежи были прослеживаемы. Та же функция
 * (lib/plan-activation.ts) включает тариф при онлайн-оплате через Т-Банк.
 *
 * «Год» = 12 календарных месяцев = та же дата через год; срок укладывается в
 * целое число окон квоты (см. lib/period.ts). В оферте должно быть то же самое
 * («один календарный месяц с даты оплаты»). Если дня нет в целевом месяце
 * (31-е), берётся последний день месяца, счёт всегда от даты оплаты.
 *
 * Если у аккаунта срок ещё не истёк, новый срок начинается СЕЙЧАС, а не
 * дописывается к старому: окна квоты привязаны к дате оплаты, и оплата —
 * это новая точка отсчёта (счётчик нового окна — с нуля). Остаток старого
 * срока при этом сгорает; скрипт печатает предупреждение с числом дней.
 */
import { PrismaClient } from "@prisma/client";
import { activatePlan } from "../lib/plan-activation";

function arg(name: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

function fail(msg: string): never {
  console.error(`[activate-plan] ${msg}`);
  process.exit(1);
}

async function main() {
  if (!process.env.DATABASE_URL) {
    fail("DATABASE_URL не задан. Выполните: set -a; . ./.env.local; set +a");
  }
  const accountArg = arg("account");
  const planCode = arg("plan");
  const monthsArg = arg("months");
  const note = arg("note") ?? "";
  const force = process.argv.includes("--force");
  if (!accountArg || !planCode || !monthsArg) {
    fail('Использование: --account <id|email> --plan <код> --months <1|12> [--note "..."] [--force]');
  }
  const months = Number(monthsArg);
  if (months !== 1 && months !== 12) fail("--months: допустимо только 1 или 12.");

  const prisma = new PrismaClient();
  try {
    const plan = await prisma.plan.findUnique({ where: { code: planCode } });
    if (!plan) {
      const codes = (await prisma.plan.findMany({ select: { code: true } })).map((p) => p.code);
      fail(`неизвестный план "${planCode}". Доступны: ${codes.join(", ")}`);
    }
    if (plan.code === "unlimited" && !force) {
      fail('план "unlimited" служебный (без срока); для осознанного назначения добавьте --force.');
    }

    const account = /^\d+$/.test(accountArg)
      ? await prisma.account.findUnique({ where: { id: Number(accountArg) } })
      : (
          await prisma.user.findUnique({
            where: { email: accountArg.trim().toLowerCase() },
            include: { account: true },
          })
        )?.account ?? null;
    if (!account) fail(`аккаунт "${accountArg}" не найден.`);

    // Справочно: сумма по тарифу за выбранный срок, чтобы сверить с банком.
    const priceKopecks = months === 12 ? plan.priceYearKopecks : plan.priceKopecks;
    console.log(
      priceKopecks == null
        ? `[activate-plan] цена за ${months} мес. для плана ${plan.code} не задана`
        : `[activate-plan] цена по тарифу за ${months} мес.: ${(priceKopecks / 100).toLocaleString("ru-RU")} ₽`,
    );

    const { periodAnchorAt: now, paidUntil, lostDays } = await prisma.$transaction((tx) =>
      activatePlan(tx, {
        accountId: account.id,
        planId: plan.id,
        planCode: plan.code,
        months,
        note,
      }),
    );
    if (lostDays > 0) {
      console.warn(
        `[activate-plan] ВНИМАНИЕ: у аккаунта ${account.id} срок ещё не истёк ` +
          `(оставалось ${lostDays} дн.). Новый срок начался сейчас, остаток старого сгорел.`,
      );
    }

    console.log(
      `[activate-plan] аккаунт ${account.id} (${account.name}): план ${plan.code}, ` +
        `срок ${now.toISOString()} .. ${paidUntil.toISOString()} (${months} мес.)`,
    );
  } finally {
    await prisma.$disconnect();
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
