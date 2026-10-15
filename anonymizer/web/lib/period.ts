/**
 * Период биллинга. Общий для проверки лимита (lib/quota.ts) и импортёра
 * (lib/usage-import.ts): период записи и период счётчика, которым она гасится,
 * обязаны совпадать по одной и той же формуле.
 *
 * У аккаунта со сроком (пробный/оплаченный) период — календарный месяц от даты
 * оплаты (регистрации): оплата 15.10 10:00 -> окно до 15.11 10:00; у аккаунта
 * без срока (root на безлимитном плане) — календарный месяц 'YYYY-MM' по UTC,
 * как раньше.
 */

export type PeriodWindow = {
  /** Ключ периода для QuotaCounter/UsageRecord/QuotaGrant.period. */
  key: string;
  start: Date;
  /** Конец окна (не включая), обрезанный по paidUntil; null — окна нет (календарный период). */
  end: Date | null;
};

/**
 * `d` + `n` календарных месяцев (UTC): тот же день месяца и время суток. Если в
 * целевом месяце такого дня нет, берётся ПОСЛЕДНИЙ день этого месяца (31.01 + 1
 * -> 28.02/29.02), а не переход в следующий (как сделал бы Date.setUTCMonth).
 *
 * Всегда считается от ЯКОРЯ, а не цепочкой от предыдущего результата: цепочка
 * «31.01 -> 28.02 -> 28.03» навсегда сдвинула бы день платежа, а от якоря
 * получается 28.02, 31.03, 30.04 — день возвращается, когда месяц позволяет.
 */
export function addMonthsClamped(d: Date, n: number): Date {
  const total = d.getUTCFullYear() * 12 + d.getUTCMonth() + n;
  const year = Math.floor(total / 12);
  const month = total - year * 12;
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const out = new Date(d.getTime());
  out.setUTCFullYear(year, month, Math.min(d.getUTCDate(), lastDay));
  return out;
}

/**
 * Окно квоты, в которое попадает момент `at`, для аккаунта с якорем `anchor`.
 *
 * Оферта: «лимит действует один календарный месяц с даты оплаты». Не календарный
 * месяц как таковой (обнуление первого числа): оплатившему 28-го достался бы
 * лимит на три дня за целую цену. Окна идут от даты оплаты, а длина месяца
 * плавает (28-31 сутки) — это осознанно: клиент платит «за месяц», а не за 30
 * суток. Год = 12 окон = та же дата через год. Всё считается в UTC.
 *
 * Окно k покрывает [addMonthsClamped(anchor, k), addMonthsClamped(anchor, k+1))
 * и обрезается по `paidUntil`. Ключ — ISO-UTC начала окна: стабилен, сортируем,
 * однозначен. Для `at` раньше якоря (теоретически: запись до оплаты) k
 * отрицательный — запись уходит в предыдущее «виртуальное» окно, а не падает.
 *
 * `anchor === null` — срока нет: календарный 'YYYY-MM' (см. periodFromDate),
 * end = null.
 */
export function windowFor(
  anchor: Date | null,
  at: Date,
  paidUntil: Date | null = null,
): PeriodWindow {
  if (!anchor) {
    const y = at.getUTCFullYear();
    const m = at.getUTCMonth();
    return { key: periodFromDate(at), start: new Date(Date.UTC(y, m, 1)), end: null };
  }
  // Первое приближение по разнице месяцев, затем подгонка на ±1: из-за
  // клампа и времени суток оно может ошибиться максимум на одно окно.
  let k =
    (at.getUTCFullYear() - anchor.getUTCFullYear()) * 12 +
    (at.getUTCMonth() - anchor.getUTCMonth());
  while (addMonthsClamped(anchor, k).getTime() > at.getTime()) k -= 1;
  while (addMonthsClamped(anchor, k + 1).getTime() <= at.getTime()) k += 1;
  const start = addMonthsClamped(anchor, k);
  let end = addMonthsClamped(anchor, k + 1);
  if (paidUntil && paidUntil.getTime() < end.getTime()) end = paidUntil;
  return { key: start.toISOString(), start, end };
}

/** Текущий календарный период — 'YYYY-MM' по UTC-времени момента вызова. */
export function currentPeriodUtc(): string {
  return periodFromDate(new Date());
}

/**
 * `ts` записи лога (`usage_log.py:_now_iso`) -> Date. Период считается по
 * ВРЕМЕНИ ОБРАБОТКИ, а не импорта: документ, сделанный в последний час окна и
 * импортированный уже в следующем, обязан списаться с того окна, когда он
 * реально был сделан. Кривая метка (не должно случаться) заменяется текущим
 * моментом — лучше отнести запись к текущему периоду, чем уронить импорт.
 */
export function dateFromIso(ts: unknown): Date {
  const d = typeof ts === "string" ? new Date(ts) : null;
  return d && !Number.isNaN(d.getTime()) ? d : new Date();
}

function periodFromDate(d: Date): string {
  const year = d.getUTCFullYear();
  const month = String(d.getUTCMonth() + 1).padStart(2, "0");
  return `${year}-${month}`;
}

/** Дата в UTC как «дд.мм.гггг» — для сообщений об окончании срока. */
export function formatDateRu(d: Date): string {
  const dd = String(d.getUTCDate()).padStart(2, "0");
  const mm = String(d.getUTCMonth() + 1).padStart(2, "0");
  return `${dd}.${mm}.${d.getUTCFullYear()}`;
}
