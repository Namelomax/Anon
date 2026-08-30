// Ограничитель попыток входа — контрмера угрозе У-12 «подбор пароля» (см.
// anonymizer/ЮРИДИЧЕСКИЕ_ДОКУМЕНТЫ/10_Авторизация_и_биллинг_правовая_фиксация.md,
// раздел 4: «Ограничение частоты попыток входа; блокировка после серии
// неудач»).
//
// Хранится В ПАМЯТИ ПРОЦЕССА (обычный Map, без БД/Redis — по требованию не
// добавлять зависимость под эту задачу). Из этого следуют два свойства,
// которые нужно понимать при эксплуатации:
//   1. Счётчики сбрасываются при каждом перезапуске/деплое процесса — это
//      приемлемо для однохостового дев/прод-контура, но не защита от
//      атакующего, который умеет форсировать рестарт.
//   2. Если сервис когда-нибудь будет работать больше чем на одном хосте
//      (несколько инстансов Next.js за балансировщиком), счётчик КАЖДОГО
//      инстанса будет независимым, и атакующий получит N попыток НА ХОСТ, а
//      не N попыток суммарно — лимитер тогда нужно перенести в БД
//      (например, отдельная таблица или колонки на User) или в общий кеш.
//
// Числа: 5 неудачных попыток в течение 15 минут → блокировка входа для этого
// email ещё на 15 минут. Эти величины — компромисс между неудобством для
// пользователя, забывшего пароль (не блокируем после первой же опечатки), и
// стоимостью подбора для атакующего (5 попыток в 15 минут — не более
// ~480 попыток в сутки на один email, что делает онлайн-подбор пароля,
// удовлетворяющего требованиям validatePasswordStrength, практически
// бесполезным).
const MAX_FAILURES = 5;
const FAILURE_WINDOW_MS = 15 * 60 * 1000;
const LOCKOUT_MS = 15 * 60 * 1000;

type AttemptState = {
  failures: number;
  firstFailureAt: number;
  lockedUntil: number | null;
};

const attemptsByEmail = new Map<string, AttemptState>();

/**
 * Возвращает остаток блокировки в миллисекундах, если email сейчас
 * заблокирован, иначе null.
 */
export function checkLoginLock(email: string): number | null {
  const state = attemptsByEmail.get(email);
  if (!state?.lockedUntil) return null;
  const remaining = state.lockedUntil - Date.now();
  if (remaining <= 0) {
    // Блокировка истекла — не держим устаревшую запись.
    attemptsByEmail.delete(email);
    return null;
  }
  return remaining;
}

/** Зафиксировать неудачную попытку входа (неверный пароль/email/деактивация). */
export function recordLoginFailure(email: string): void {
  const now = Date.now();
  const state = attemptsByEmail.get(email);
  if (!state || now - state.firstFailureAt > FAILURE_WINDOW_MS) {
    // Первая неудача, либо предыдущее окно уже истекло — начинаем заново.
    attemptsByEmail.set(email, { failures: 1, firstFailureAt: now, lockedUntil: null });
    return;
  }
  state.failures += 1;
  if (state.failures >= MAX_FAILURES) {
    state.lockedUntil = now + LOCKOUT_MS;
  }
}

/** Сбросить счётчик после успешного входа. */
export function recordLoginSuccess(email: string): void {
  attemptsByEmail.delete(email);
}
