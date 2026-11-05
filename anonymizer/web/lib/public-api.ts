import { NextRequest, NextResponse } from "next/server";
import { bearerApiKey } from "@/lib/api-keys";

/** V1 требует ключ даже при наличии cookie и отключённой авторизации в dev. */
export function apiKeyOnly(handler: (request: NextRequest) => Promise<NextResponse>) {
  return async (request: NextRequest) => {
    if (!bearerApiKey(request)) return NextResponse.json({ error: "Передайте API-ключ в Authorization: Bearer <ключ>." },
      { status: 401, headers: { "Cache-Control": "no-store", "WWW-Authenticate": "Bearer" } });
    // Существующий обработчик проверяет хеш, отзыв и владельца через resolveIdentity(request).
    const response = await handler(request);
    response.headers.set("Cache-Control", "no-store");
    return response;
  };
}
