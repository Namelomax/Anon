import { createHash, timingSafeEqual } from "node:crypto";
import { Agent, request } from "node:https";
import { rootCertificates } from "node:tls";
import { RUSSIAN_TRUSTED_ROOT_CA } from "./tbank-ca";

/**
 * Интернет-эквайринг Т-Банка (https://developer.tbank.ru/eacq/): платёжная
 * форма банка по редиректу. Карту вводят на стороне банка — данных карты
 * сервис не видит и не хранит.
 *
 * Поток: POST /api/billing/checkout заводит Payment и вызывает Init → браузер
 * уходит на PaymentURL → банк шлёт уведомление на NotificationURL
 * (/api/billing/notify) → на CONFIRMED тариф включается. SuccessURL — только
 * возврат человека в кабинет: тариф по нему НЕ включается, его может открыть
 * кто угодно.
 *
 * Тест: терминал с суффиксом DEMO работает на боевом адресе
 * securepay.tinkoff.ru и принимает только тестовые карты, например
 * 2200770239097761, срок 12/30, CVV 123 (без 3DS), — список в разделе
 * «Тестовая среда» документации. Для перехода на боевой приём достаточно
 * заменить TBANK_TERMINAL_KEY/TBANK_PASSWORD на ключи терминала без DEMO.
 */

const DEFAULT_API_URL = "https://securepay.tinkoff.ru/v2";
const TIMEOUT_MS = 20_000;

/**
 * HTTPS-агент ТОЛЬКО для запросов к API банка: стандартные корни Node.js плюс
 * корень НУЦ Минцифры (см. lib/tbank-ca.ts). Глобальное доверие процесса не
 * меняется — поэтому здесь node:https с явным агентом, а не global fetch.
 */
const tbankAgent = new Agent({
  ca: [...rootCertificates, RUSSIAN_TRUSTED_ROOT_CA],
  keepAlive: true,
});

function postJson(url: string, json: string): Promise<{ status: number; text: string }> {
  const body = Buffer.from(json, "utf8");
  return new Promise((resolve, reject) => {
    const req = request(
      url,
      {
        method: "POST",
        agent: tbankAgent,
        timeout: TIMEOUT_MS,
        headers: { "Content-Type": "application/json", "Content-Length": String(body.length) },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (c) => chunks.push(c as Buffer));
        res.on("end", () =>
          resolve({ status: res.statusCode || 0, text: Buffer.concat(chunks).toString("utf8") }),
        );
        res.on("error", reject);
      },
    );
    req.on("timeout", () => req.destroy(new Error(`Т-Банк: таймаут ${TIMEOUT_MS} мс`)));
    req.on("error", reject);
    req.end(body);
  });
}

export type TbankConfig = {
  terminalKey: string;
  password: string;
  apiUrl: string;
  /** Система налогообложения для чека (Receipt.Taxation); null — чек не передаётся. */
  taxation: string | null;
  /** Ставка НДС позиции чека (Receipt.Items[].Tax). */
  tax: string;
};

/** null — оплата не настроена: кнопки оплаты скрыты, checkout отвечает 503. */
export function tbankConfig(): TbankConfig | null {
  const terminalKey = process.env.TBANK_TERMINAL_KEY?.trim();
  const password = process.env.TBANK_PASSWORD?.trim();
  if (!terminalKey || !password) return null;
  return {
    terminalKey,
    password,
    apiUrl: (process.env.TBANK_API_URL?.trim() || DEFAULT_API_URL).replace(/\/$/, ""),
    taxation: process.env.TBANK_TAXATION?.trim() || null,
    tax: process.env.TBANK_TAX?.trim() || "none",
  };
}

/**
 * Подпись запроса/уведомления (раздел «Токен» документации): все параметры
 * КОРНЕВОГО уровня, кроме Token и вложенных объектов (DATA, Receipt, Shops),
 * плюс {Password}; сортировка по ключу; конкатенация одних значений; SHA-256 hex.
 * Булевы значения входят строкой "true"/"false", числа — как есть; null не
 * участвует (банк сам их опускает при подписи уведомлений).
 */
export function makeToken(params: Record<string, unknown>, password: string): string {
  const pairs: [string, string][] = [["Password", password]];
  for (const [k, v] of Object.entries(params)) {
    if (k === "Token" || v === null || v === undefined || typeof v === "object") continue;
    pairs.push([k, String(v)]);
  }
  pairs.sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
  return createHash("sha256")
    .update(pairs.map(([, v]) => v).join(""), "utf8")
    .digest("hex");
}

/** Подпись уведомления от банка верна (сравнение за постоянное время). */
export function verifyToken(params: Record<string, unknown>, password: string): boolean {
  const got = typeof params.Token === "string" ? params.Token.toLowerCase() : "";
  const want = makeToken(params, password);
  if (got.length !== want.length) return false;
  return timingSafeEqual(Buffer.from(got), Buffer.from(want));
}

export type InitArgs = {
  orderId: string;
  amountKopecks: number;
  description: string;
  email: string;
  itemName: string;
  successUrl: string;
  failUrl: string;
  notificationUrl: string;
};

export type InitResponse = {
  Success: boolean;
  ErrorCode: string;
  Message?: string;
  Details?: string;
  Status?: string;
  PaymentId?: string | number;
  PaymentURL?: string;
};

/** Метод Init — «Инициировать платёж». */
export async function initPayment(cfg: TbankConfig, a: InitArgs): Promise<InitResponse> {
  const body: Record<string, unknown> = {
    TerminalKey: cfg.terminalKey,
    Amount: a.amountKopecks,
    OrderId: a.orderId,
    // Лимит Description — 140 символов (меньший из лимитов для карты и СБП).
    Description: a.description.slice(0, 140),
    // PayType не передаём: параметр, противоречащий настройкам терминала,
    // срывает платёж. Терминал должен быть ОДНОСТАДИЙНЫМ (ЛК → Магазины →
    // Прием оплаты → «После оплаты заказа»): тариф включается по CONFIRMED,
    // а при двухстадийной оплате деньги лишь холдируются (AUTHORIZED).
    Language: "ru",
    SuccessURL: a.successUrl,
    FailURL: a.failUrl,
    NotificationURL: a.notificationUrl,
    DATA: { OperationInitiatorType: "0", Email: a.email },
  };
  // Чек по 54-ФЗ обязателен, если к терминалу подключена онлайн-касса;
  // без кассы (в т.ч. на DEMO-терминале) Receipt не передаём.
  if (cfg.taxation) {
    body.Receipt = {
      Email: a.email,
      Taxation: cfg.taxation,
      Items: [
        {
          Name: a.itemName.slice(0, 128),
          Price: a.amountKopecks,
          Quantity: 1,
          Amount: a.amountKopecks,
          Tax: cfg.tax,
          PaymentMethod: "full_prepayment",
          PaymentObject: "service",
        },
      ],
    };
  }
  return callMethod<InitResponse>(cfg, "Init", body);
}

/**
 * Вызов метода API банка: подставляет TerminalKey и подпись Token. Вложенные
 * объекты (DATA, Receipt) в подпись не входят — см. makeToken.
 */
export async function callMethod<T = Record<string, unknown>>(
  cfg: TbankConfig,
  method: string,
  params: Record<string, unknown>,
): Promise<T> {
  const body: Record<string, unknown> = { ...params, TerminalKey: cfg.terminalKey };
  body.Token = makeToken(body, cfg.password);
  const { status, text } = await postJson(`${cfg.apiUrl}/${method}`, JSON.stringify(body));
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error(`Т-Банк ${method}: HTTP ${status}, не JSON: ${text.slice(0, 200)}`);
  }
}
