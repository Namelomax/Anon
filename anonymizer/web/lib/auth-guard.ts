import { NextResponse } from "next/server";
import { auth } from "@/auth";
import { bearerApiKey, identifyApiKey } from "@/lib/api-keys";

/**
 * Требовать ли сессию Auth.js на маршрутах анонимизации/деанонимизации.
 * Дефолт — true (сайт закрыт без входа). `ANONYMIZER_REQUIRE_AUTH=false`
 * оставляет сайт открытым — это переходный режим (например, пока фронтенд
 * ещё не подключил форму логина везде), и цена его в том, что ЛЮБОЙ, кто
 * достучится до этих маршрутов, тратит апстрим-бюджет владельца сервиса без
 * какой-либо привязки к аккаунту/квоте (см. lib/quota.ts — без личности
 * проверка лимита не выполняется вовсе). Держать выключенным на проде долго
 * не стоит.
 */
function authRequired(): boolean {
  return (process.env.ANONYMIZER_REQUIRE_AUTH ?? "true").trim().toLowerCase() !== "false";
}

export type CallerIdentity = { accountId: number; userId: number };

export type ResolveIdentityResult =
  | { identity: CallerIdentity | null; error?: undefined }
  | { identity?: undefined; error: NextResponse };

/**
 * Достаёт личность из явного Bearer-ключа или сессии Auth.js для маршрутов
 * anonymize/deanonymize. Управление аккаунтом по-прежнему требует сессию.
 *
 * - Сессия есть → identity заполнен (accountId/userId для передачи бэкенду
 *   и для проверки квоты).
 * - Сессии нет и ANONYMIZER_REQUIRE_AUTH!=="false" → готовый 401-ответ на
 *   русском, вызывающий код должен вернуть его как есть.
 * - Сессии нет, но авторизация НЕ обязательна (переходный режим) →
 *   identity=null, вызывающий код продолжает работу без биллинговой
 *   привязки (как клиент, предшествующий системе аутентификации, см.
 *   докстринг usage_log.request_context).
 */
export async function resolveIdentity(request?: Request): Promise<ResolveIdentityResult> {
  // Явные учётные данные проверяются строго: неверный ключ не заменяется
  // cookie-сессией или анонимным режимом разработки.
  if (request?.headers.has("authorization")) {
    try {
      const key = bearerApiKey(request);
      const identity = key ? await identifyApiKey(key) : null;
      if (identity) return { identity };
      return { error: NextResponse.json({ error: "API-ключ недействителен или отозван." },
        { status: 401, headers: { "Cache-Control": "no-store", "WWW-Authenticate": "Bearer" } }) };
    } catch {
      return { error: NextResponse.json({ error: "Не удалось проверить API-ключ. Повторите запрос позже." },
        { status: 503, headers: { "Cache-Control": "no-store" } }) };
    }
  }
  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;
  const accountId = typeof session?.accountId === "number" ? session.accountId : null;

  if (userId != null && accountId != null && Number.isSafeInteger(userId) && userId > 0 &&
      Number.isSafeInteger(accountId) && accountId > 0) {
    return { identity: { accountId, userId } };
  }

  if (authRequired()) {
    return {
      error: NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 }),
    };
  }

  return { identity: null };
}
