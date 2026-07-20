import { request as httpsRequest } from "node:https";
import { request as httpRequest } from "node:http";

/**
 * Call the Python backend, tolerating a slightly-malformed response.
 *
 * The backend is reached through the JupyterHub proxy, which injects a
 * multi-line `Content-Security-Policy` header (literal `\n` inside the value).
 * That is invalid per RFC 9110, so Node's global `fetch` (undici) rejects the
 * whole response with "Invalid header value char". Node's classic http/https
 * client accepts it when `insecureHTTPParser: true` is set, so we use that here
 * instead of `fetch`. (curl and browsers are lenient too, which is why manual
 * checks worked while the Vercel function did not.)
 */
// Connection-establishment error codes worth retrying: these all mean the TCP
// handshake to the backend never completed, so nothing was sent to it and
// retrying is trivially idempotent. Vercel's default region is far from the
// backend VPS (jh.interfonica.cloud), which occasionally shows up as a
// connect-phase ETIMEDOUT/ECONNREFUSED even though the backend is healthy.
const _RETRYABLE_CONNECT_CODES = new Set([
  "ECONNREFUSED",
  "ETIMEDOUT",
  "ENOTFOUND",
  "ENETUNREACH",
  "EHOSTUNREACH",
]);
const _RETRY_DELAYS_MS = [2000, 5000];

function _isRetryableConnectError(err: unknown): boolean {
  const e = err as { code?: string; syscall?: string; _responseStarted?: boolean } | undefined;
  if (!e || e._responseStarted) return false; // response already began: never retry
  if (e.code && _RETRYABLE_CONNECT_CODES.has(e.code)) return true;
  return e.syscall === "connect";
}

function _sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function callBackend(
  url: string,
  bodyJson: string,
  apiKey: string,
  timeoutMs: number,
): Promise<{ status: number; text: string }> {
  let attempt = 0;
  for (;;) {
    try {
      return await _callBackendOnce(url, bodyJson, apiKey, timeoutMs);
    } catch (err) {
      // Only retry pure connect-phase failures — once any response bytes/
      // headers have arrived, `_callBackendOnce` throws a marked error that
      // is never retried, since the backend may already have side effects
      // (or a partially-sent body) in flight.
      if (attempt >= _RETRY_DELAYS_MS.length || !_isRetryableConnectError(err)) {
        throw err;
      }
      await _sleep(_RETRY_DELAYS_MS[attempt]);
      attempt += 1;
    }
  }
}

function _callBackendOnce(
  url: string,
  bodyJson: string,
  apiKey: string,
  timeoutMs: number,
): Promise<{ status: number; text: string }> {
  const u = new URL(url);
  const isHttps = u.protocol === "https:";
  const reqFn = isHttps ? httpsRequest : httpRequest;
  const body = Buffer.from(bodyJson, "utf8");

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Content-Length": String(body.length),
  };
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;

  return new Promise((resolve, reject) => {
    // Tracks whether the response callback has already fired — once it has,
    // the connection was established (request reached the backend), so any
    // later error must propagate as-is and must NOT be retried by the caller.
    let responseStarted = false;
    const req = reqFn(
      {
        protocol: u.protocol,
        hostname: u.hostname,
        port: u.port || (isHttps ? 443 : 80),
        path: u.pathname + u.search,
        method: "POST",
        headers,
        insecureHTTPParser: true,
        timeout: timeoutMs,
      },
      (res) => {
        responseStarted = true;
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c as Buffer));
        res.on("end", () =>
          resolve({ status: res.statusCode || 0, text: Buffer.concat(chunks).toString("utf8") }),
        );
      },
    );
    req.on("timeout", () => req.destroy(new Error(`Backend timeout after ${timeoutMs}ms`)));
    req.on("error", (err) => {
      // Flag mid-response errors so the retry loop above never retries them
      // (see `_isRetryableConnectError`) — once headers arrived, the request
      // reached the backend and retrying could duplicate a side effect.
      if (responseStarted) (err as { _responseStarted?: boolean })._responseStarted = true;
      reject(err);
    });
    req.write(body);
    req.end();
  });
}

/**
 * Turn a thrown value into a message that says what actually went wrong. The
 * real reason often hides in `error.cause` (ECONNREFUSED / ENOTFOUND / ETIMEDOUT
 * / TLS / parser errors), so we walk the cause chain and surface a code + hint.
 */
export function describeError(e: unknown, backendUrl: string): string {
  const err = e as { message?: string; cause?: unknown } | undefined;
  const top = err?.message || String(e);

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
    ETIMEDOUT: "Таймаут — хост, вероятно, недоступен из облака Vercel (firewall).",
    UND_ERR_CONNECT_TIMEOUT: "Таймаут соединения — хост недоступен из облака Vercel.",
    ECONNRESET: "Соединение сброшено сервером/прокси во время запроса.",
    CERT_HAS_EXPIRED: "Просрочен TLS-сертификат бэкенда.",
  };
  const hint = HINTS[code] || "";

  return [
    top,
    code ? `[${code}]` : "",
    detail && detail !== top ? `— ${detail}` : "",
    hint ? `\n${hint}` : "",
    `\nBACKEND_URL=${backendUrl}`,
  ]
    .filter(Boolean)
    .join(" ");
}
