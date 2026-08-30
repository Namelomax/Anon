/**
 * Период биллинга — всегда 'YYYY-MM' в UTC. Общий для проверки лимита
 * (lib/quota.ts) и импортёра (lib/usage-import.ts): период записи и период
 * счётчика, которым она гасится, обязаны совпадать по одной и той же
 * формуле, иначе списание "утечёт" в соседний месяц из-за локального часового
 * пояса хоста.
 */

/** Текущий период — 'YYYY-MM' по UTC-времени момента вызова. */
export function currentPeriodUtc(): string {
  return periodFromDate(new Date());
}

/**
 * Период документа — 'YYYY-MM' по UTC-времени его обработки (`ts` из строки
 * `request_total`, см. `usage_log.py:_now_iso`). Считать период по МОМЕНТУ
 * ИМПОРТА, а не по `ts` записи, было бы неверно: документ, обработанный в
 * последний час месяца и импортированный уже в следующем, обязан списаться
 * с того периода, когда он реально был сделан.
 */
export function periodFromIso(ts: unknown): string {
  const d = typeof ts === "string" ? new Date(ts) : null;
  if (!d || Number.isNaN(d.getTime())) {
    // Не должно случаться для валидной строки usage_log.py, но лучше
    // отнести к текущему периоду, чем уронить импорт на одной кривой записи.
    return currentPeriodUtc();
  }
  return periodFromDate(d);
}

function periodFromDate(d: Date): string {
  const year = d.getUTCFullYear();
  const month = String(d.getUTCMonth() + 1).padStart(2, "0");
  return `${year}-${month}`;
}
