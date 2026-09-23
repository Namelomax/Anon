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

/**
 * Состояние квоты аккаунта на текущий период — ОДИН источник правды и для
 * отказа на входе (`checkQuota`), и для цифр в личном кабинете
 * (`lib/account-summary.ts`). Считать их в двух местах нельзя: разойдутся —
 * и кабинет будет показывать остаток там, где загрузка уже отказывает.
 *
 * `null` — аккаунта с таким id нет (для accountId из валидной сессии не
 * должно случаться, но вызывающий обязан это обработать).
 *
 * `limitTenths`/`allowanceTenths`/`remainingTenths` равны null у безлимитного
 * аккаунта; `usedTenths` осмыслен всегда — владелец сервиса хочет видеть
 * расход и там, где формального лимита нет.
 */
type AccountWithPlan = NonNullable<Awaited<ReturnType<typeof loadAccount>>>;

export type QuotaState = {
  account: AccountWithPlan;
  period: string;
  usedTenths: number;
  limitTenths: number | null;
  grantsTenths: number;
  allowanceTenths: number | null;
  remainingTenths: number | null;
  exhausted: boolean;
};

function loadAccount(accountId: number) {
  return prisma.account.findUnique({
    where: { id: accountId },
    include: { plan: true },
  });
}

/**
 * Подтянуть расход из биллингового лога перед тем, как на него смотреть.
 *
 * Вызывается и на входе запроса, и при открытии кабинета: свежие цифры нужны
 * РОВНО в момент, когда от них зависит решение "пустить/отказать" или то,
 * что человек читает на экране. Сбой импорта (лог временно недоступен, диск,
 * гонка) НЕ должен блокировать запрос — отказывать в обслуживании из-за
 * того, что не читается файл лога, было бы неверным компромиссом (клиент не
 * виноват), поэтому ошибка только логируется, а дальше работаем по тому, что
 * уже накоплено в счётчике.
 */
async function refreshUsage(): Promise<void> {
  try {
    await importUsageLog();
  } catch (err) {
    console.error(
      "[quota] импорт биллингового лога не удался — использую то, что уже в счётчике:",
      err,
    );
  }
}

export async function getQuotaState(accountId: number): Promise<QuotaState | null> {
  await refreshUsage();

  const account = await loadAccount(accountId);
  if (!account) return null;

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
  const planLimitTenths =
    account.plan.pagesPerMonth != null ? account.plan.pagesPerMonth * 10 : null;
  const limitTenths = account.pageLimitOverrideTenths ?? planLimitTenths;

  // Гранты за этот период плюс бессрочные (period=null) добавляются к
  // лимиту плана/override — это ДОБАВКА, а не замена (см. докстринг
  // QuotaGrant в schema.prisma). Считаем их и для безлимитного аккаунта:
  // цифра идёт в кабинет, даже когда ни на что не влияет.
  const grants = await prisma.quotaGrant.aggregate({
    where: { accountId, OR: [{ period }, { period: null }] },
    _sum: { pagesTenths: true },
  });
  const grantsTenths = grants._sum.pagesTenths ?? 0;

  const allowanceTenths = limitTenths == null ? null : limitTenths + grantsTenths;
  return {
    account,
    period,
    usedTenths: counter.pagesUsedTenths,
    limitTenths,
    grantsTenths,
    allowanceTenths,
    remainingTenths:
      allowanceTenths == null ? null : Math.max(0, allowanceTenths - counter.pagesUsedTenths),
    exhausted: allowanceTenths != null && counter.pagesUsedTenths >= allowanceTenths,
  };
}

export async function checkQuota(accountId: number): Promise<QuotaCheckResult> {
  const state = await getQuotaState(accountId);
  if (!state) {
    // Не должно случаться для accountId из валидной сессии, но не молчим.
    return { ok: false, status: 403, message: "Аккаунт не найден." };
  }
  if (!state.account.isActive) {
    return { ok: false, status: 403, message: "Аккаунт отключён." };
  }
  if (!state.exhausted) {
    return { ok: true };
  }

  const usedPages = (state.usedTenths / 10).toFixed(1);
  const limitPages = ((state.allowanceTenths ?? 0) / 10).toFixed(1);
  return {
    ok: false,
    status: 402,
    message:
      `Лимит страниц на текущий период исчерпан: использовано ${usedPages} из ${limitPages}. ` +
      "Обратитесь к администратору аккаунта для увеличения лимита.",
  };
}
