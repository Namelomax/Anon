import { createHash, randomBytes } from "node:crypto";
import { isIP } from "node:net";
import nodemailer from "nodemailer";
import { LICENSOR } from "./legal";
import { isValidEmail } from "./password";
import { SITE_URL } from "./site";

export type Lead = { phone: string; company: string; email: string };
const CONTROL = /[\u0000-\u001f\u007f]/;
const MAX_BODY_BYTES = 4096;

function mailbox(value: string): boolean {
  return value.length <= 254 && !CONTROL.test(value) && !/[<>,;"\\]/.test(value) && isValidEmail(value);
}

export function validateLead(value: unknown): Lead | string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "Некорректная заявка.";
  const data = value as Record<string, unknown>;
  if (data.consent !== true) return "Подтвердите согласие на обработку контактов для связи по заявке.";
  if (typeof data.phone !== "string" || data.phone.length > 80 || CONTROL.test(data.phone)) {
    return "Укажите корректный номер телефона.";
  }
  const phone = data.phone.trim();
  const digits = phone.replace(/\D/g, "");
  if (!/^\+?[\d ().-]+$/.test(phone) || digits.length < 10 || digits.length > 15) {
    return "Укажите телефон с кодом города или страны (от 10 до 15 цифр).";
  }
  for (const [field, max] of [["company", 160], ["email", 254]] as const) {
    if (data[field] != null && (typeof data[field] !== "string" ||
        (data[field] as string).length > max || CONTROL.test(data[field] as string))) {
      return field === "company" ? "Название компании должно быть не длиннее 160 символов." : "Укажите корректную почту.";
    }
  }
  const company = typeof data.company === "string" ? data.company.trim() : "";
  const email = typeof data.email === "string" ? data.email.trim() : "";
  if (email && !mailbox(email)) return "Укажите корректную почту или оставьте поле пустым.";
  return { phone, company, email };
}

type MailConfig = { host: string; port: number; user: string; pass: string; from: string; to: string };

export function leadMailConfig(env: Partial<NodeJS.ProcessEnv> = process.env): MailConfig | null {
  const host = env.LEADS_SMTP_HOST?.trim() || "";
  const port = Number(env.LEADS_SMTP_PORT || "465");
  const user = env.LEADS_SMTP_USER?.trim() || "";
  const pass = env.LEADS_SMTP_PASSWORD || "";
  const from = env.LEADS_SMTP_FROM?.trim() || user;
  const to = env.LEADS_TO_EMAIL?.trim() || LICENSOR.email;
  if (!host || CONTROL.test(host) || !Number.isInteger(port) || port < 1 || port > 65535 ||
      !user || CONTROL.test(user) || !pass || !mailbox(from) || !mailbox(to)) return null;
  return { host, port, user, pass, from, to };
}

export function leadMessage(lead: Lead, config: Pick<MailConfig, "from" | "to">, now = new Date()) {
  return {
    from: { name: "Anon", address: config.from },
    to: { address: config.to, name: "" },
    ...(lead.email ? { replyTo: { address: lead.email, name: "" } } : {}),
    subject: "Anon: новая заявка на внедрение в компанию",
    text: [
      "Поступила заявка на внедрение Anon в инфраструктуре компании.",
      "",
      `Телефон: ${lead.phone}`,
      `Компания: ${lead.company || "не указана"}`,
      `Почта: ${lead.email || "не указана"}`,
      "",
      `Время получения (UTC): ${now.toISOString()}`,
      "Согласие на обработку контактов для связи по заявке: подтверждено.",
    ].join("\n"),
    disableFileAccess: true,
    disableUrlAccess: true,
  };
}

export async function sendLeadEmail(lead: Lead, config: MailConfig): Promise<void> {
  const transporter = nodemailer.createTransport({
    host: config.host,
    port: config.port,
    secure: config.port === 465,
    requireTLS: config.port !== 465,
    auth: { user: config.user, pass: config.pass },
    tls: { minVersion: "TLSv1.2" },
    connectionTimeout: 10_000,
    greetingTimeout: 10_000,
    socketTimeout: 15_000,
    dnsTimeout: 5_000,
    logger: false,
    debug: false,
  });
  const info = await transporter.sendMail(leadMessage(lead, config));
  const accepted = (info.accepted || []).some((recipient) =>
    (typeof recipient === "string" ? recipient : recipient.address)?.toLowerCase() === config.to.toLowerCase());
  if (!accepted || info.rejected?.length) throw new Error("SMTP recipient not accepted");
}

// One Next.js process behind nginx. Only salted IP hashes, never contacts, are
// retained in memory. A multi-instance deployment needs a shared rate limiter.
export function createLeadLimiter() {
  const salt = randomBytes(32);
  const sources = new Map<string, { count: number; expires: number }>();
  let total = { count: 0, expires: 0 };
  return (ip: string, now = Date.now()): number => {
    for (const [key, state] of sources) if (state.expires <= now) sources.delete(key);
    if (total.expires <= now) total = { count: 0, expires: now + 60 * 60_000 };
    if (total.count >= 30) return Math.ceil((total.expires - now) / 1000);
    const key = createHash("sha256").update(salt).update(ip).digest("hex");
    const state = sources.get(key) || { count: 0, expires: now + 15 * 60_000 };
    if (state.count >= 5) return Math.ceil((state.expires - now) / 1000);
    if (!sources.has(key) && sources.size >= 10_000) return 60;
    sources.set(key, { ...state, count: state.count + 1 });
    total.count += 1;
    return 0;
  };
}

const rateLimit = createLeadLimiter();
const unavailable = "Не удалось отправить заявку. Попробуйте позже или напишите нам по почте, указанной под формой.";

function reply(status: number, body: object, extraHeaders: Record<string, string> = {}): Response {
  return Response.json(body, { status, headers: { "Cache-Control": "no-store", ...extraHeaders } });
}

export async function handleLeadRequest(
  request: Request,
  send = sendLeadEmail,
  limit = rateLimit,
): Promise<Response> {
  const origins = [SITE_URL, process.env.AUTH_URL, request.url].filter(Boolean).map((url) => {
    try { return new URL(url!).origin; } catch { return ""; }
  });
  const origin = request.headers.get("origin");
  if (!origin || !origins.includes(origin)) return reply(403, { error: "Отправьте заявку с сайта Anon." });
  if (request.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/json") {
    return reply(415, { error: "Некорректный формат заявки." });
  }
  // nginx overwrites X-Real-IP. Do not trust the first, user-controlled XFF
  // entry. Direct access to the Next.js port must be restricted to nginx.
  const address = request.headers.get("x-real-ip") || "";
  const retryAfter = limit(isIP(address) ? address : "unknown");
  if (retryAfter) return reply(429, { error: "Слишком много заявок. Попробуйте позже." }, { "Retry-After": String(retryAfter) });
  if (Number(request.headers.get("content-length")) > MAX_BODY_BYTES) {
    return reply(413, { error: "Заявка слишком большая." });
  }
  const reader = request.body?.getReader();
  if (!reader) return reply(400, { error: "Заполните заявку." });
  const chunks: Uint8Array[] = [];
  let length = 0;
  let data: unknown;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      length += value.byteLength;
      if (length > MAX_BODY_BYTES) {
        await reader.cancel();
        return reply(413, { error: "Заявка слишком большая." });
      }
      chunks.push(value);
    }
    data = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    return reply(400, { error: "Некорректная заявка." });
  }
  if (data && typeof data === "object" && "website" in data &&
      (data as { website?: unknown }).website != null && (data as { website?: unknown }).website !== "") {
    return reply(400, { error: "Не удалось отправить заявку." });
  }
  const lead = validateLead(data);
  if (typeof lead === "string") return reply(400, { error: lead });
  const config = leadMailConfig();
  if (!config) return reply(503, { error: unavailable });
  try {
    await send(lead, config);
  } catch {
    // SMTP errors may contain addresses or authentication details.
    console.error("[leads] SMTP delivery failed");
    return reply(502, { error: unavailable });
  }
  return reply(200, { ok: true });
}
