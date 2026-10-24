import type { PrismaClient } from "@prisma/client";
import { addMonthsClamped } from "./period";

/**
 * Включение оплаченного тарифа — общее для ручной фиксации оплаты
 * (scripts/activate-plan.ts) и онлайн-оплаты (app/api/billing/notify).
 *
 * planId = план, periodAnchorAt = `now`, paidUntil = `now` + months календарных
 * месяцев; плюс аудиторская строка QuotaGrant (pagesTenths=0, reason «Оплата: …»),
 * чтобы платежи были прослеживаемы.
 *
 * Если у аккаунта срок ещё не истёк, новый срок начинается СЕЙЧАС, а не
 * дописывается к старому: окна квоты привязаны к дате оплаты (lib/period.ts),
 * и оплата — это новая точка отсчёта. Остаток старого срока сгорает — число
 * сгоревших дней возвращается, чтобы вызывающий мог о нём сказать.
 *
 * Обе записи — ПАКЕТНОЙ транзакцией ($transaction([...])), а не интерактивной:
 * у SQLite одна блокировка записи на всю базу, и интерактивная транзакция,
 * ждущая её за параллельной записью (банк шлёт AUTHORIZED и CONFIRMED
 * одновременно), упиралась в 5-секундный таймаут Prisma и откатывалась.
 * Пакет уходит в движок целиком и не держит транзакцию открытой между
 * запросами.
 */
const DAY_MS = 24 * 60 * 60 * 1000;

export type ActivationResult = {
  periodAnchorAt: Date;
  paidUntil: Date;
  /** Сколько дней прежнего срока сгорело; 0 — срок уже истёк или его не было. */
  lostDays: number;
};

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
  const lostDays =
    account.paidUntil && account.paidUntil.getTime() > now.getTime()
      ? Math.ceil((account.paidUntil.getTime() - now.getTime()) / DAY_MS)
      : 0;

  const paidUntil = addMonthsClamped(now, args.months);
  await db.$transaction([
    db.account.update({
      where: { id: args.accountId },
      data: { planId: args.planId, periodAnchorAt: now, paidUntil },
    }),
    db.quotaGrant.create({
      data: {
        accountId: args.accountId,
        period: null,
        pagesTenths: 0,
        reason: `Оплата: ${args.planCode} на ${args.months} мес. ${args.note ?? ""}`.trim(),
        externalPaymentId: args.externalPaymentId ?? null,
      },
    }),
  ]);
  return { periodAnchorAt: now, paidUntil, lostDays };
}
