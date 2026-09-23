import { NextResponse } from "next/server";
import { resolveIdentity } from "@/lib/auth-guard";
import { getAccountSummary } from "@/lib/account-summary";

export const runtime = "nodejs";

/**
 * GET /api/account — сводка личного кабинета: аккаунт, тариф, остаток квоты
 * и последние обработанные документы (см. lib/account-summary.ts).
 *
 * Требует сессию. В переходном режиме без авторизации
 * (ANONYMIZER_REQUIRE_AUTH=false, см. lib/auth-guard.ts) личности нет, а
 * значит нет и аккаунта — отвечаем 404 вместо пустой сводки, чтобы клиент
 * честно показал «кабинет недоступен», а не нули, похожие на настоящие цифры.
 *
 * Ответ помечается no-store: остаток квоты меняется после каждого документа,
 * и закешированная цифра здесь хуже отсутствующей.
 */
export async function GET() {
  const { identity, error } = await resolveIdentity();
  if (error) return error;
  if (!identity) {
    return NextResponse.json(
      { error: "Кабинет доступен только при входе в систему." },
      { status: 404 },
    );
  }

  try {
    const summary = await getAccountSummary(identity.accountId, identity.userId);
    if (!summary) {
      return NextResponse.json({ error: "Аккаунт не найден." }, { status: 404 });
    }
    return NextResponse.json(summary, { headers: { "Cache-Control": "no-store" } });
  } catch (e) {
    console.error("[/api/account] не удалось собрать сводку:", e);
    return NextResponse.json({ error: "Не удалось получить данные аккаунта." }, { status: 500 });
  }
}
