import { prisma } from "@/lib/db";
import { importUsageLog } from "@/lib/usage-import";
import { currentPeriodUtc } from "@/lib/period";

/**
 * Проверка лимита страниц перед постановкой задачи (см. app/api/anonymize/
 * route.ts POST). НЕТ РЕЗЕРВИРОВАНИЯ — см. шапку prisma/schema.prisma:
 * здесь только вопрос "аккаунт уже исчерпал лимит?", ответ "да" останавливает
 * запрос ДО обращения к бэкенду; списание фактического расхода происходит
 * отдельно, импортом лога (см. lib/usage-import.ts).
 */
export type QuotaCheckResult =
  | { ok: true }
  | { ok: false; status: 402 | 403; message: string };

export async function checkQuota(accountId: number): Promise<QuotaCheckResult> {
  // Импорт — здесь, а не по расписанию/крону: свежие цифры счётчика нужны
  // РОВНО в момент, когда от них зависит решение "пустить/отказать". Сбой
  // импорта (лог временно недоступен, диск, гонка) НЕ должен блокировать
  // запрос — отказывать в обслуживании из-за того, что не читается файл
  // лога, было бы неверным компромиссом (клиент не виноват), поэтому ошибка
  // только логируется, а проверка идёт по тому, что уже накоплено в счётчике.
  try {
    await importUsageLog();
  } catch (err) {
    console.error(
      "[quota] импорт биллингового лога не удался — использую то, что уже в счётчике:",
      err,
    );
  }

  const account = await prisma.account.findUnique({
    where: { id: accountId },
    include: { plan: true },
  });
  if (!account) {
    // Не должно случаться для accountId из валидной сессии, но не молчим.
    return { ok: false, status: 403, message: "Аккаунт не найден." };
  }
  if (!account.isActive) {
    return { ok: false, status: 403, message: "Аккаунт отключён." };
  }

  const period = currentPeriodUtc();

  // Счётчик должен существовать ВСЕГДА, даже для безлимитных аккаунтов —
  // владелец сервиса хочет видеть расход и там, где формального лимита нет
  // ("безлимит, но видно сколько").
  const counter = await prisma.quotaCounter.upsert({
    where: { accountId_period: { accountId, period } },
    update: {},
    create: { accountId, period, pagesUsedTenths: 0 },
  });

  // Индивидуальное исключение поверх плана — если задано, оно ЗАМЕЩАЕТ
  // лимит плана целиком (а не складывается с ним), см. комментарий к
  // Account.pageLimitOverrideTenths в schema.prisma.
  const planLimitTenths = account.plan.pagesPerMonth != null ? account.plan.pagesPerMonth * 10 : null;
  const effectiveLimitTenths = account.pageLimitOverrideTenths ?? planLimitTenths;

  if (effectiveLimitTenths == null) {
    // null и на уровне аккаунта, и на уровне плана — безлимитный аккаунт,
    // всегда разрешаем. Счётчик уже гарантированно существует (см. выше).
    return { ok: true };
  }

  // Гранты за этот период плюс бессрочные (period=null) добавляются к
  // лимиту плана/override — это ДОБАВКА, а не замена (см. докстринг
  // QuotaGrant в schema.prisma).
  const grants = await prisma.quotaGrant.aggregate({
    where: { accountId, OR: [{ period }, { period: null }] },
    _sum: { pagesTenths: true },
  });
  const allowanceTenths = effectiveLimitTenths + (grants._sum.pagesTenths ?? 0);

  if (counter.pagesUsedTenths >= allowanceTenths) {
    const usedPages = (counter.pagesUsedTenths / 10).toFixed(1);
    const limitPages = (allowanceTenths / 10).toFixed(1);
    return {
      ok: false,
      status: 402,
      message:
        `Лимит страниц на текущий период исчерпан: использовано ${usedPages} из ${limitPages}. ` +
        "Обратитесь к администратору аккаунта для увеличения лимита.",
    };
  }

  return { ok: true };
}
