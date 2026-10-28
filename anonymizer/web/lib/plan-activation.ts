import type { PrismaClient } from "@prisma/client";
import { addMonthsClamped } from "./period";

/**
 * Включение оплаченного тарифа — общее для ручной фиксации оплаты
 * (scripts/activate-plan.ts) и онлайн-оплаты (app/api/billing/notify).
 *
 * Два режима:
 *
 *  - ПРОДЛЕНИЕ — оплачен тот же тариф, что у аккаунта, и срок ещё идёт:
 *    paidUntil сдвигается на `months` месяцев, periodAnchorAt НЕ меняется.
 *    Окна квоты считаются от якоря (lib/period.ts), поэтому текущий месяц
 *    дорабатывается со своим остатком, а лимит обновляется в начале каждого
 *    следующего месяца — в том числе месяцев, добавленных продлением.
 *
 *  - НОВЫЙ СРОК — другой тариф, срок истёк или его не было:
 *    periodAnchorAt = `now`, paidUntil = `now` + months. Остаток прежнего
 *    срока и его неизрасходованные страницы сгорают (кабинет предупреждает
 *    об этом перед оплатой); число сгоревших дней возвращается.
 *
 * Плюс аудиторская строка QuotaGrant (pagesTenths=0, reason «Оплата: …»),
 * чтобы платежи были прослеживаемы.
 *
 * Обе записи — ПАКЕТНОЙ транзакцией ($transaction([...])), а не интерактивной:
 * у SQLite одна блокировка записи на всю базу, и интерактивная транзакция,
 * ждущая её за параллельной записью (банк шлёт AUTHORIZED и CONFIRMED
 * одновременно), упиралась в 5-секундный таймаут Prisma и откатывалась.
 */
const DAY_MS = 24 * 60 * 60 * 1000;

export type ActivationResult = {
  mode: "extend" | "new";
  periodAnchorAt: Date;
  paidUntil: Date;
  /** Сколько дней прежнего срока сгорело; 0 — продление, срок истёк или его не было. */
  lostDays: number;
};

/**
 * Сколько целых месяцев от якоря до `until`, если `until` ровно на границе
 * окна (addMonthsClamped(anchor, n) === until); иначе null. Срок, обрезанный
 * не по границе (например, полным возвратом), продлевать или сокращать «от
 * якоря» нельзя. Нужна и возврату (app/api/billing/notify).
 */
export function monthsFromAnchor(anchor: Date, until: Date): number | null {
  const n =
    (until.getUTCFullYear() - anchor.getUTCFullYear()) * 12 +
    (until.getUTCMonth() - anchor.getUTCMonth());
  for (const k of [n, n - 1, n + 1]) {
    if (k > 0 && addMonthsClamped(anchor, k).getTime() === until.getTime()) return k;
  }
  return null;
}

export async function activatePlan(
  db: PrismaClient,
  args: {
    accountId: number;
    planId: number;
    planCode: string;
    months: number;
    note?: string;
    externalPaymentId?: string | null;
    now?: Date;
  },
): Promise<ActivationResult> {
  const now = args.now ?? new Date();
  const account = await db.account.findUniqueOrThrow({ where: { id: args.accountId } });
  const active = account.paidUntil != null && account.paidUntil.getTime() > now.getTime();

  // Продление считаем от ЯКОРЯ (anchor + n + months), а не от paidUntil:
  // цепочка от paidUntil накапливала бы сдвиг дня из-за клампа 31-го числа.
  const paidMonths =
    active && account.planId === args.planId && account.periodAnchorAt
      ? monthsFromAnchor(account.periodAnchorAt, account.paidUntil!)
      : null;

  let result: ActivationResult;
  if (paidMonths != null) {
    const anchor = account.periodAnchorAt!;
    result = {
      mode: "extend",
      periodAnchorAt: anchor,
      paidUntil: addMonthsClamped(anchor, paidMonths + args.months),
      lostDays: 0,
    };
  } else {
    result = {
      mode: "new",
      periodAnchorAt: now,
      paidUntil: addMonthsClamped(now, args.months),
      lostDays: active ? Math.ceil((account.paidUntil!.getTime() - now.getTime()) / DAY_MS) : 0,
    };
  }

  await db.$transaction([
    db.account.update({
      where: { id: args.accountId },
      data: { planId: args.planId, periodAnchorAt: result.periodAnchorAt, paidUntil: result.paidUntil },
    }),
    db.quotaGrant.create({
      data: {
        accountId: args.accountId,
        period: null,
        pagesTenths: 0,
        reason:
          `Оплата: ${args.planCode} на ${args.months} мес.` +
          (result.mode === "extend" ? " (продление)" : "") +
          ` ${args.note ?? ""}`.trimEnd(),
        externalPaymentId: args.externalPaymentId ?? null,
      },
    }),
  ]);
  return result;
}
