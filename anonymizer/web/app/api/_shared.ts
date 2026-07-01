/**
 * Turn a thrown value (esp. Node's opaque `TypeError: fetch failed`) into a
 * message that actually says what went wrong. undici hides the real reason in
 * `error.cause` (ECONNREFUSED / ENOTFOUND / ETIMEDOUT / certificate errors),
 * so we walk the cause chain and surface a code + explanation.
 */
export function describeError(e: unknown, backendUrl: string): string {
  const err = e as { message?: string; cause?: unknown } | undefined;
  const top = err?.message || String(e);

  // Unwrap the underlying cause (may be nested).
  let cause = err?.cause as { code?: string; message?: string; cause?: unknown } | undefined;
  const codes: string[] = [];
  let detail = "";
  for (let i = 0; cause && i < 4; i++) {
    if (cause.code) codes.push(cause.code);
    if (cause.message) detail = cause.message;
    cause = cause.cause as typeof cause;
  }

  const code = codes[0] || "";
  const HINTS: Record<string, string> = {
    ENOTFOUND: "DNS не резолвится — проверьте домен в ANONYMIZER_BACKEND_URL.",
    ECONNREFUSED: "Соединение отклонено — бэкенд не слушает этот адрес/порт снаружи.",
    ETIMEDOUT:
      "Таймаут соединения — вероятно, хост доступен только из вашей сети (firewall/allowlist блокирует облако Vercel).",
    UND_ERR_CONNECT_TIMEOUT:
      "Таймаут соединения — хост, скорее всего, недоступен из облака Vercel (firewall/allowlist).",
    ECONNRESET: "Соединение сброшено сервером/прокси во время запроса.",
    CERT_HAS_EXPIRED: "Просрочен TLS-сертификат бэкенда.",
    DEPTH_ZERO_SELF_SIGNED_CERT: "Самоподписанный TLS-сертификат бэкенда.",
    UNABLE_TO_VERIFY_LEAF_SIGNATURE: "Не удалось проверить TLS-цепочку сертификата бэкенда.",
  };
  const hint = HINTS[code] || "";

  const parts = [
    top,
    code ? `[${code}]` : "",
    detail && detail !== top ? `— ${detail}` : "",
    hint ? `\n${hint}` : "",
    `\nBACKEND_URL=${backendUrl}`,
  ].filter(Boolean);
  return parts.join(" ");
}
