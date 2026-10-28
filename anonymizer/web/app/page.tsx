"use client";

import JSZip from "jszip";
import {
  Braces,
  ChevronDown,
  CircleUser,
  CircleCheckBig,
  Download,
  Eye,
  FileText,
  FileType,
  Info,
  LogIn,
  LogOut,
  KeyRound,
  LoaderCircle,
  Lock,
  Gauge,
  Package,
  PanelLeft,
  PanelLeftClose,
  ShieldCheck,
  TriangleAlert,
  Undo2,
  Users,
  UserPlus,
} from "lucide-react";
import Link from "next/link";
import { signOut, useSession } from "next-auth/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { LEGAL_DOCS } from "@/lib/legal";

type StageKey = "regex" | "corporate" | "ner" | "llm" | "review" | "subject";

type Tab = "anon" | "deanon" | "account";

// Зеркало lib/account-summary.ts: страницы везде в ДЕСЯТЫХ долях (как в БД,
// см. шапку prisma/schema.prisma — страницы и деньги только целыми).
type AccountSummary = {
  user: { email: string; role: string };
  account: { id: number; name: string; isActive: boolean };
  plan: {
    code: string;
    title: string;
    pagesPerMonth: number | null;
    maxUsers: number | null;
    priceKopecks: number;
  };
  quota: {
    period: string;
    unlimited: boolean;
    usedTenths: number;
    limitTenths: number | null;
    grantsTenths: number;
    allowanceTenths: number | null;
    remainingTenths: number | null;
    exhausted: boolean;
    periodStart: string;
    periodEnd: string | null;
    paidUntil: string | null;
    expired: boolean;
  };
  canManage: boolean;
  offers: {
    code: string;
    title: string;
    pagesPerMonth: number | null;
    maxUsers: number | null;
    priceKopecks: number;
    priceYearKopecks: number | null;
  }[];
  payBlockedReason: "role" | "not_configured" | null;
  payments: {
    orderId: string;
    createdAt: string;
    planTitle: string;
    months: number;
    amountKopecks: number;
    refundedKopecks: number;
    status: string;
    activated: boolean;
    accessUntil: string | null;
  }[];
  users: {
    active: number;
    limit: number | null;
    taken: number;
    free: number | null;
    list: {
      id: number;
      email: string;
      role: string;
      isActive: boolean;
      createdAt: string;
      isSelf: boolean;
    }[];
  };
  invitations: {
    id: number;
    email: string;
    role: string;
    createdAt: string;
    expiresAt: string;
  }[];
  periodTotals: { documents: number; chars: number; pagesTenths: number };
  recent: {
    id: number;
    createdAt: string;
    userEmail: string | null;
    chars: number;
    pagesTenths: number;
    billablePagesTenths: number;
    seconds: number;
    ok: boolean;
  }[];
};

type AnonResult = {
  filename: string;
  is_docx: boolean;
  anonymized_text: string;
  mapping: Record<string, string>;
  summary: Record<string, number>;
  elapsed_seconds?: number;
  preexisting_placeholders?: number;
  warnings?: {
    kind: string;
    value?: string;
    context?: string;
    message?: string;
    offset?: number;
    chars?: number;
  }[];
  document_base64: string;
  document_name: string;
  document_mime: string;
  // Откуда взялся отдаваемый документ (см. server.py): "original" — из самого
  // загруженного .docx, "converted" — .doc поднят до .docx через LibreOffice
  // (разметка перенесена), "text" — документ собран заново из текста.
  document_source?: "original" | "converted" | "text";
};

type DeanonResult = {
  is_docx: boolean;
  restored_text: string;
  leftover: string[];
  document_base64: string;
  document_name: string;
  document_mime: string;
};

const STAGE_LABELS: Record<StageKey, string> = {
  regex: "Правила (regex)",
  corporate: "Корпоративные (суммы/договоры)",
  ner: "GLiNER (ФИО, города, организации)",
  llm: "LLM (сложные случаи)",
  review: "LLM-проверка (отсеивает ложные срабатывания)",
  subject: "Предмет договора (наименования товаров и услуг)",
};

// Best-effort cancel of a background job. Used both from the `pagehide`
// handler (with keepalive:true, so the request survives the page going away —
// the browser's beacon API can't be used here since it only supports POST,
// not DELETE) and when a new job replaces one still in flight. Fire-and-
// forget: the caller doesn't need to wait for the backend to acknowledge.
function cancelJob(jobId: string, keepalive = false) {
  fetch(`/api/anonymize?jobId=${encodeURIComponent(jobId)}`, {
    method: "DELETE",
    keepalive,
  }).catch(() => {});
}

// Срок жизни результата во вкладке и окно, в которое задачу ещё можно забрать с
// сервера. ОБЯЗАН совпадать с _JOB_TTL_SECONDS в anonymizer/server.py (10 минут):
// короче — потеряем результат, который сервер ещё держит; длиннее — будем
// ходить за задачей, которой там уже нет.
const JOB_TTL_MS = 10 * 60 * 1000;

// Пережить перезагрузку страницы. ТОЛЬКО sessionStorage (живёт, пока открыта
// вкладка): ни localStorage, ни IndexedDB — результат содержит ключ
// деанонимизации и не должен переживать вкладку. Содержимое результата не
// логируется.
// В обеих записях лежит userId сессии: запись чужого пользователя (или без
// сессии) игнорируется и стирается — результат содержит ключ деанонимизации.
const JOB_KEY = "anon.job.v1"; // {userId, jobId, fileName, startedAt}
const RESULT_KEY = "anon.result.v1"; // {userId, savedAt, expiresAt, fileName, result}
// Больше — не кэшируем (квота sessionStorage ~5 МБ на origin, а строка UTF-16).
// Тогда после перезагрузки результат забирается опросом по jobId в окне TTL.
const RESULT_CACHE_MAX_CHARS = 2_000_000;

const GONE_MESSAGE =
  "Результат больше не доступен: прошло более 10 минут с загрузки документа или страница была закрыта. Загрузите документ заново.";

type SavedJob = { userId: string; jobId: string; fileName: string; startedAt: number };

// Любой доступ к хранилищу — в try/catch: в приватном режиме/при запрете
// cookies sessionStorage бросает, и страница должна работать и без него.
function readJob(userId: string | null): SavedJob | null {
  try {
    const raw = window.sessionStorage.getItem(JOB_KEY);
    if (!raw) return null;
    const j = JSON.parse(raw);
    if (
      userId !== null &&
      j?.userId === userId &&
      typeof j?.jobId === "string" &&
      typeof j?.fileName === "string" &&
      typeof j?.startedAt === "number"
    ) {
      return j as SavedJob;
    }
  } catch {
    /* повреждённая запись или хранилище недоступно */
  }
  clearJob(); // чужая/битая запись
  return null;
}

// Без userId (нет сессии) не пишем: такую запись всё равно никто не прочтёт.
function writeJob(job: Omit<SavedJob, "userId">, userId: string | null): boolean {
  if (userId === null) return false;
  try {
    window.sessionStorage.setItem(JOB_KEY, JSON.stringify({ userId, ...job }));
    return true;
  } catch {
    return false;
  }
}

function clearJob() {
  try {
    window.sessionStorage.removeItem(JOB_KEY);
  } catch {
    /* нечего чистить */
  }
}

function clearCachedResult() {
  try {
    window.sessionStorage.removeItem(RESULT_KEY);
  } catch {
    /* нечего чистить */
  }
}

// Кэш результата, если он есть и не истёк; истёкший/битый сразу удаляется.
function readCachedResult(
  userId: string | null,
): { fileName: string; result: AnonResult } | null {
  try {
    const raw = window.sessionStorage.getItem(RESULT_KEY);
    if (!raw) return null;
    const c = JSON.parse(raw);
    if (
      userId !== null &&
      c?.userId === userId &&
      typeof c?.expiresAt === "number" &&
      c.expiresAt > Date.now() &&
      c.result &&
      typeof c.result.anonymized_text === "string" &&
      typeof c.result.document_base64 === "string"
    ) {
      return { fileName: typeof c.fileName === "string" ? c.fileName : "", result: c.result };
    }
  } catch {
    /* битая запись — удалим ниже */
  }
  clearCachedResult();
  return null;
}

// true — результат сохранён. Не влезло/квота/приватный режим — просто false.
function writeCachedResult(
  userId: string | null,
  fileName: string,
  startedAt: number,
  result: AnonResult,
): boolean {
  const expiresAt = startedAt + JOB_TTL_MS;
  if (userId === null || expiresAt <= Date.now()) return false;
  try {
    const json = JSON.stringify({ userId, savedAt: Date.now(), expiresAt, fileName, result });
    if (json.length > RESULT_CACHE_MAX_CHARS) return false;
    window.sessionStorage.setItem(RESULT_KEY, json);
    return true;
  } catch {
    return false; // QuotaExceededError, SecurityError
  }
}

// Бэкенд не знает задачу (истёк TTL, перезапуск, либо она чужая — для чужой
// бэкенд намеренно отвечает так же).
class JobGoneError extends Error {}
// Связь пропала, но задача могла остаться жива — запись о ней не стираем.
class JobConnectionError extends Error {}

function base64ToBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

/**
 * Опрос фоновой задачи: GET /api/anonymize?jobId=... пока не вернётся
 * {done:true}.
 *
 * Потолка по времени здесь СОЗНАТЕЛЬНО нет. Ждёт браузер, а он ничем не
 * ограничен — задача считается ровно столько, сколько нужно. Единичный сбой
 * сети не хоронит задачу: она живёт на бэкенде независимо от того, доехал ли
 * конкретный GET, поэтому сдаёмся только после серии неудач подряд.
 */
async function pollJob(
  jobId: string,
  onTick: (sec: number) => void,
  startedAt: number = Date.now(),
): Promise<any> {
  let delayMs = 1000;
  let failures = 0;

  for (;;) {
    await new Promise((r) => setTimeout(r, delayMs));
    // Короткие документы успевают за секунду; на длинных разряжаем опрос,
    // чтобы не молотить прокси сотнями запросов.
    delayMs = Math.min(delayMs * 1.4, 5000);
    onTick(Math.round((Date.now() - startedAt) / 1000));

    let resp: Response;
    try {
      resp = await fetch(`/api/anonymize?jobId=${encodeURIComponent(jobId)}`, {
        cache: "no-store",
      });
    } catch {
      if (++failures > 10) throw new JobConnectionError("Связь с сервером потеряна");
      continue;
    }
    failures = 0;

    const raw = await resp.text();
    let data: any = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch {
      continue; // прокси вклинился HTML-заглушкой — спросим ещё раз
    }
    if (resp.status === 404) throw new JobGoneError(GONE_MESSAGE);
    if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
    // A cancelled job is not an error — someone (this tab on unload, or a new
    // job replacing it) asked the backend to stop it. Stop polling quietly.
    if (data?.cancelled) return { cancelled: true };
    if (data?.done) return data;
  }
}

function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function labelOf(placeholder: string): string {
  return placeholder.replace(/^\[|\]$/g, "").replace(/_[^_]*$/, "");
}

// Человекочитаемые подписи для result.warnings[].kind — и для найденных, но
// не скрытых значений (verify.py), и для сбоев отдельных слоёв проверки
// (engine.py/gliner_remote.py/llm.py). Незнакомый (будущий) kind просто
// показывается как есть — см. warningLabel.
const WARNING_KIND_LABELS: Record<string, string> = {
  DIGITS: "Длинный номер",
  EMAIL: "Адрес эл. почты",
  // Вид называет СЛОЙ, который не отработал, а не «фрагмент не проверен»:
  // остальные слои этот же текст разобрали, и маскирование в нём чаще всего
  // выполнено полностью — общая формулировка пугала там, где всё скрыто.
  gliner_chunk_failed: "Фрагмент не проверен слоем GLiNER",
  llm_chunk_failed: "Фрагмент не проверен слоем LLM",
  recheck_chunk_failed: "Перепроверка фрагмента не завершена",
  recall_failed: "Не выполнен поиск пропущенных данных",
  recall_partial: "Поиск пропущенных данных выполнен частично",
  review_failed: "Не выполнена проверка лишних масок",
  review_adjacent_failed: "Не выполнена проверка спорных имён",
  review_short_numbers_failed: "Не выполнена проверка коротких чисел",
  review_person_gate_unavailable: "Недоступна словарная проверка имён",
};

function warningLabel(kind: string): string {
  return WARNING_KIND_LABELS[kind] ?? kind;
}

// Сбой ДОПОЛНИТЕЛЬНОЙ перепроверки (engine.py, recheck_chunk_failed): второй
// проход по уже замаскированному тексту. Основные слои этот фрагмент
// разобрали, маскирование выполнено — ниже обычного была только вероятность
// поймать пропущенное. Поэтому такие сообщения идут отдельной спокойной
// карточкой, а не в жёлтой «проверена не полностью».
const SOFT_WARNING_KINDS = new Set(["recheck_chunk_failed"]);

type FailedWarning = {
  kind: string;
  message?: string;
  offset?: number;
  chars?: number;
};

// Текст режется на куски по несколько тысяч символов, и при обрыве связи
// подряд не отвечают сразу несколько — пять строк «символы X–Y» об одном и
// том же сбое читать невозможно. Соприкасающиеся диапазоны одного вида
// сливаем в один пункт, считая, сколько кусков в него вошло. Записи без
// диапазона (сбой целого слоя, а не куска) не сливаются никогда.
function mergeChunkWarnings(items: FailedWarning[]): (FailedWarning & { count: number })[] {
  const sorted = [...items].sort((a, b) =>
    a.kind === b.kind ? (a.offset ?? 0) - (b.offset ?? 0) : a.kind < b.kind ? -1 : 1,
  );
  const out: (FailedWarning & { count: number })[] = [];
  for (const w of sorted) {
    const prev = out[out.length - 1];
    const mergeable =
      prev &&
      prev.kind === w.kind &&
      prev.offset != null &&
      prev.chars != null &&
      w.offset != null &&
      w.chars != null &&
      w.offset <= prev.offset + prev.chars;
    if (mergeable) {
      prev.chars = Math.max(prev.offset! + prev.chars!, w.offset! + w.chars!) - prev.offset!;
      prev.count += 1;
      continue;
    }
    out.push({ ...w, count: 1 });
  }
  return out;
}

// «символы 12 400–13 200» — офсет/длина есть только у чанковых сбоев
// (gliner_chunk_failed/llm_chunk_failed/recheck_chunk_failed), чтобы
// показать, ГДЕ смотреть. Смещения всегда в координатах ИСХОДНОГО текста:
// для второго прохода их переводит engine._interim_offset_translator.
function warningRange(offset?: number, chars?: number): string | null {
  if (offset == null || chars == null) return null;
  const start = offset.toLocaleString("ru");
  const end = (offset + chars).toLocaleString("ru");
  return `символы ${start}–${end}`;
}

// Список сбоев слоёв — одинаковый в обеих карточках (жёсткой и мягкой),
// различаются только заголовок и пояснение вокруг него.
function WarningList({ items }: { items: (FailedWarning & { count: number })[] }) {
  return (
    <ul style={{ margin: 0, paddingLeft: 20 }}>
      {items.map((w, i) => {
        const range = warningRange(w.offset, w.chars);
        return (
          <li key={i} style={{ marginBottom: 10 }}>
            <strong>{warningLabel(w.kind)}</strong>
            {range && (
              <span className="note">
                {" "}
                ({range}
                {w.count > 1 ? `, фрагментов: ${w.count}` : ""})
              </span>
            )}
            <div className="note" style={{ marginTop: 2 }}>
              {w.message}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

// Полный список форматов живёт в меню, а не в зоне загрузки: там он занимал
// три строки и забивал собой главное действие. Сгруппирован по тому, что
// пользователь получит на выходе (политика — documents.prepare_document).
const FORMAT_GROUPS: { title: string; items: string }[] = [
  {
    title: "Возвращаются в своём формате",
    items: ".docx · .xlsx · .xlsm · .odt · .xml · .txt · .csv · .md · .json",
  },
  {
    title: "Меняют формат",
    items: ".doc → .docx · .xls → .xlsx",
  },
  {
    title: "Только текстом",
    items: ".pdf → .txt",
  },
];

const ROLE_LABELS: Record<string, string> = {
  root: "Владелец сервиса",
  admin: "Администратор аккаунта",
  member: "Пользователь",
};

/** Десятые доли страницы -> «12,4». */
function pages(tenths: number): string {
  return (tenths / 10).toLocaleString("ru", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
}

/** Копейки -> «990» (рублей). */
function rubles(kopecks: number): string {
  return (kopecks / 100).toLocaleString("ru", { maximumFractionDigits: 2 });
}

/** 'YYYY-MM' -> «сентябрь 2026». Период биллинга считается в UTC (lib/period.ts). */
function periodLabel(period: string): string {
  const d = new Date(`${period}-01T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return period;
  return d.toLocaleDateString("ru", { month: "long", year: "numeric", timeZone: "UTC" });
}

/** ISO -> «дд.мм.гггг» по UTC (границы окон квоты считаются в UTC, lib/period.ts). */
function dateRu(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("ru", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** Подпись периода квоты: «период дд.мм.гггг–дд.мм.гггг», у аккаунта без срока — месяц. */
function quotaPeriodLabel(q: {
  period: string;
  periodStart: string;
  periodEnd: string | null;
}): string {
  if (!q.periodEnd) return periodLabel(q.period);
  return `период ${dateRu(q.periodStart)}–${dateRu(q.periodEnd)}`;
}

/** Человекочитаемое состояние онлайн-платежа (статусы Т-Банка, см. app/api/billing/notify). */
function paymentStatusLabel(p: AccountSummary["payments"][number]): string {
  if (p.refundedKopecks > 0) {
    return p.refundedKopecks >= p.amountKopecks ? "Возвращён полностью" : "Частичный возврат";
  }
  if (p.activated) return "Оплачен";
  if (p.status === "CONFIRMED") return "Оплачен, тариф включается";
  if (p.status === "AMOUNT_MISMATCH") return "Сумма не совпала — обратитесь в поддержку";
  if (["REJECTED", "AUTH_FAIL", "DEADLINE_EXPIRED", "CANCELED", "ATTEMPTS_EXPIRED"].includes(p.status)) {
    return "Отклонён";
  }
  if (["REFUNDED", "PARTIAL_REFUNDED", "REVERSED", "PARTIAL_REVERSED"].includes(p.status)) {
    return "Возвращён";
  }
  return "Не завершён";
}

/** Сообщение об истёкшем сроке — тот же текст, что отдаёт checkQuota (lib/quota.ts). */
function expiredMessage(planCode: string, paidUntil: string): string {
  return planCode === "trial"
    ? `Пробный период закончился ${dateRu(paidUntil)}. Чтобы продолжить, оплатите тариф.`
    : `Срок действия тарифа закончился ${dateRu(paidUntil)}. Продлите подписку, чтобы продолжить.`;
}

function extOf(name: string): string {
  const m = /\.[^.]+$/.exec(name);
  return m ? m[0].toLowerCase() : "";
}

// Форматы, у которых документ и есть его текст (см. documents._TEXT_EXT).
const PLAIN_EXT = new Set([".txt", ".csv", ".md", ".log", ".json"]);

// Что сказать про формат результата. Молчим, когда говорить нечего: формат
// тот же и разметка на месте. Смена расширения без пояснения выглядит как
// ошибка, а потерянная разметка — тем более, поэтому оба случая называются
// вслух (document_source приходит с сервера, см. server._run_anonymize_file).
function formatNote(result: AnonResult): string | null {
  const from = extOf(result.filename);
  const to = extOf(result.document_name);
  const layoutLost = result.document_source === "text" && !PLAIN_EXT.has(from);
  if (from === to && !layoutLost) return null;
  if (to === ".txt") {
    return `Из ${from} можно отдать только текст: переписать такой файл, не развалив вёрстку, нечем. Результат сохранён как .txt.`;
  }
  const head = from === to ? "" : `Исходный ${from} сохранён как ${to} — записать ${from} обратно нечем. `;
  return layoutLost
    ? `${head}Разметку перенести не удалось: на сервере нет конвертера LibreOffice, поэтому в документе только текст по абзацам.`
    : `${head}Разметка документа перенесена.`;
}

// Replace each placeholder token with its original value. Placeholders are
// distinct "[LABEL_N]" tokens, so plain split/join is safe (no partial-match
// clashes: "[PERSON_1]" is not a substring of "[PERSON_10]").
function deanonClient(text: string, m: Record<string, string>): string {
  let out = text;
  for (const [ph, orig] of Object.entries(m)) out = out.split(ph).join(orig);
  return out;
}

type GlossaryRow = { id: number; aliases: string; canonical: string };

export default function Home() {
  // Только отображение личности/выход — проверка прав и квоты в этой задаче
  // сознательно не делается (см. спеку: "не строить проверки квоты в этой
  // задаче"), поэтому статус сессии здесь ни на что не влияет, кроме шапки.
  const { data: session, status: sessionStatus } = useSession();
  // Id пользователя сессии — привязка sessionStorage-кэша к человеку. В ref,
  // чтобы колбэки ниже читали актуальное значение без пересоздания.
  const userId = session?.user?.id ?? null;
  const userIdRef = useRef<string | null>(userId);
  userIdRef.current = userId;
  const [tab, setTab] = useState<Tab>("anon");
  // Сводка кабинета: тариф, остаток квоты, история. Нужна не только самому
  // кабинету — по ней в меню показывается остаток, а исчерпанная квота
  // гасит кнопку «Обезличить» ДО загрузки файла (бэкенд всё равно откажет,
  // см. lib/quota.ts, но узнать об этом лучше заранее).
  const [summary, setSummary] = useState<AccountSummary | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  // Управление составом аккаунта. inviteUrl — ссылка последнего выписанного
  // приглашения: она существует в открытом виде ровно один раз (в БД только
  // хеш, см. lib/invitations.ts), поэтому держим её на экране, пока админ её
  // не скопировал.
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "admin">("member");
  const [inviteBusy, setInviteBusy] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);
  const [inviteUrl, setInviteUrl] = useState<string | null>(null);
  const [inviteCopied, setInviteCopied] = useState(false);
  // Онлайн-оплата (Т-Банк, lib/tbank.ts). payKey — какая кнопка ждёт ответа
  // checkout ("plan:months").
  const [payKey, setPayKey] = useState<string | null>(null);
  const [payError, setPayError] = useState<string | null>(null);
  // Словарь терминов аккаунта («Мои термины»): список, формы добавления и
  // массового импорта. Значения терминов конфиденциальны — только в UI.
  const [glTerms, setGlTerms] = useState<GlossaryRow[]>([]);
  const [glMax, setGlMax] = useState(500);
  const [glAliases, setGlAliases] = useState("");
  const [glCanonical, setGlCanonical] = useState("");
  const [glBulk, setGlBulk] = useState("");
  const [glBulkOpen, setGlBulkOpen] = useState(false);
  const [glBusy, setGlBusy] = useState(false);
  const [glError, setGlError] = useState<string | null>(null);
  const [glNote, setGlNote] = useState<string | null>(null);

  // --- Anonymize state ---
  const [file, setFile] = useState<File | null>(null);
  const [stages, setStages] = useState<Record<StageKey, boolean>>({
    regex: true,
    corporate: true,
    ner: true,
    llm: true,
    review: true,
    subject: true,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Секунды с момента постановки задачи — чтобы минуты ожидания не выглядели
  // зависанием.
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<AnonResult | null>(null);
  // Set when the currently displayed job was cancelled rather than errored —
  // rendered as a neutral note, never in the red error style.
  const [cancelled, setCancelled] = useState(false);
  // Нейтральное сообщение (не ошибка): результат пропал с сервера.
  const [notice, setNotice] = useState<string | null>(null);
  // Имя документа в работе — показывается в «обрабатываю», в том числе после
  // перезагрузки, когда объекта File уже нет.
  const [jobName, setJobName] = useState<string | null>(null);
  // Лежит ли показанный результат в sessionStorage (от этого зависит текст
  // под результатом).
  const [resultCached, setResultCached] = useState(false);
  const [drag, setDrag] = useState(false);
  // Боковое меню открыто по умолчанию на любой ширине; на узком экране оно
  // выезжает поверх содержимого и закрывается по выбору пункта.
  const [menuOpen, setMenuOpen] = useState(true);
  const [narrow, setNarrow] = useState(false);
  // Экспериментальные настройки скрыты, пока их не раскроют: в обычной работе
  // слои не трогают, а список галочек забивает меню.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [formatsOpen, setFormatsOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // Id of the job currently being polled, kept in a ref (not just state) so
  // the `pagehide` handler can read the latest value without a stale
  // closure. Cleared as soon as the job reaches a terminal state (done or
  // cancelled) so a finished job is never cancelled by a later unload/new
  // submit.
  const activeJobIdRef = useRef<string | null>(null);
  // Записан ли jobId в sessionStorage. Если да — pagehide задачу НЕ отменяет:
  // перезагрузка страницы обязана её пережить (вкладку от перезагрузки на
  // pagehide не отличить). Если хранилища нет — возобновлять нечем, и
  // отмена на выгрузке остаётся, как раньше.
  const jobPersistedRef = useRef(false);
  // Защита от двойного запуска восстановления: в dev-режиме StrictMode монтирует
  // эффект дважды, а второй опрос той же задачи нам не нужен.
  const restoredRef = useRef(false);
  // Placeholders the user manually chose to KEEP in plain text (undo a mask).
  // Reversible: clicking again re-masks. Everything downstream (preview,
  // mapping.json, downloaded document) is derived from this set.
  const [kept, setKept] = useState<Set<string>>(new Set());
  const [docBusy, setDocBusy] = useState(false);

  // --- Deanonymize state ---
  const [deUseLast, setDeUseLast] = useState(true);
  const [deFile, setDeFile] = useState<File | null>(null);
  const [deMapFile, setDeMapFile] = useState<File | null>(null);
  const [deLoading, setDeLoading] = useState(false);
  const [deError, setDeError] = useState<string | null>(null);
  const [deResult, setDeResult] = useState<DeanonResult | null>(null);
  const deFileRef = useRef<HTMLInputElement>(null);
  const deMapRef = useRef<HTMLInputElement>(null);

  const stem = useMemo(
    () => (result ? result.filename.replace(/\.[^.]+$/, "") : "document"),
    [result],
  );

  useEffect(() => {
    // Ширина нужна только чтобы знать, лежит ли меню ПОВЕРХ содержимого: на
    // узком экране выбор пункта его закрывает, иначе результат остаётся
    // спрятанным. Само меню ширина не открывает и не закрывает — оно открыто
    // по умолчанию, дальше решает пользователь. Порог тот же, что в
    // globals.css. matchMedia, а не resize: событие приходит только при
    // пересечении границы.
    const query = window.matchMedia("(max-width: 900px)");
    setNarrow(query.matches);
    const onChange = (e: MediaQueryListEvent) => setNarrow(e.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const loadSummary = useCallback(async () => {
    try {
      const resp = await fetch("/api/account", { cache: "no-store" });
      if (resp.status === 401 || resp.status === 404) {
        // Не вошёл или работает в режиме без авторизации — кабинета просто
        // нет, и это не ошибка, о которой нужно кричать.
        setSummary(null);
        setSummaryError(null);
        return;
      }
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setSummary(data as AccountSummary);
      setSummaryError(null);
    } catch (e: unknown) {
      setSummaryError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    if (sessionStatus === "authenticated") void loadSummary();
  }, [sessionStatus, loadSummary]);

  // Cancel the in-flight job when the tab is closed, reloaded, or navigated
  // away from. `pagehide` (not `beforeunload`) is used because it also fires
  // on mobile/bfcache navigations. `keepalive: true` on the fetch is what
  // lets the request survive the page going away.
  useEffect(() => {
    const onPageHide = () => {
      const jobId = activeJobIdRef.current;
      if (!jobId || jobPersistedRef.current) return;
      cancelJob(jobId, true);
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, []);

  const onPick = (f: File | null | undefined) => {
    if (!f) return;
    if (!/\.(docx?|pdf|xlsx?|xlsm|xml|rtf|odt|txt|csv|md|json)$/i.test(f.name)) {
      setError("Этот формат не поддерживается — полный список в меню слева (презентации не принимаются)");
      return;
    }
    setError(null);
    setResult(null);
    setCancelled(false);
    setNotice(null);
    setResultCached(false);
    // Новый документ — прежний результат и запись о задаче больше не нужны.
    clearCachedResult();
    clearJob();
    setFile(f);
  };

  const toggle = (k: StageKey) => setStages((s) => ({ ...s, [k]: !s[k] }));

  // Показать готовый результат и попытаться пережить им перезагрузку.
  // Если результат лёг в sessionStorage, запись о задаче больше не нужна; если
  // нет (велик/квота/приватный режим) — запись остаётся, и перезагрузка
  // добирает результат опросом по jobId, пока сервер его держит.
  const showResult = useCallback(
    (res: AnonResult, fileName: string, startedAt: number) => {
      setResult(res);
      setKept(new Set());
      setDeUseLast(true);
      setDeResult(null);
      const cached = writeCachedResult(userIdRef.current, fileName, startedAt, res);
      setResultCached(cached);
      if (cached) clearJob();
      // Документ списан — остаток в меню и кабинете должен это показать
      // сразу, а не после следующей загрузки.
      void loadSummary();
    },
    [loadSummary],
  );

  // Опрос уже поставленной задачи + разбор исхода. Общий для свежего запуска и
  // для возобновления после перезагрузки. «Задача пропала» — нейтральное
  // notice, остальные сбои — красная ошибка.
  const trackJob = useCallback(
    async (jobId: string, fileName: string, startedAt: number) => {
      activeJobIdRef.current = jobId;
      jobPersistedRef.current = writeJob({ jobId, fileName, startedAt }, userIdRef.current);
      try {
        const final = await pollJob(jobId, setElapsed, startedAt);
        // Terminal state reached (done or cancelled) — clear the ref so a
        // later unload/new submit never sends a cancel for this job again.
        activeJobIdRef.current = null;
        if (final?.cancelled) {
          clearJob();
          setCancelled(true);
        } else {
          showResult(final as AnonResult, fileName, startedAt);
        }
      } catch (e: unknown) {
        activeJobIdRef.current = null;
        if (e instanceof JobGoneError) {
          clearJob();
          setNotice(GONE_MESSAGE);
        } else if (e instanceof JobConnectionError) {
          // Запись оставляем: после перезагрузки опрос можно продолжить.
          setError(e.message);
        } else {
          clearJob(); // задача завершилась ошибкой — возобновлять нечего
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    },
    [showResult],
  );

  // Восстановление после перезагрузки (один раз, как только известна сессия:
  // до этого не понять, чей кэш лежит в хранилище). Порядок:
  // 1) кэш результата — показываем сразу, без сети; 2) иначе запись о задаче —
  // возобновляем опрос, если окно TTL ещё не прошло. Хранилища нет — всё это
  // молча пропускается.
  useEffect(() => {
    if (restoredRef.current || sessionStatus === "loading") return;
    restoredRef.current = true;

    // Нет сессии — userId=null: чужие/ничьи записи стираются, не показываются.
    const cached = readCachedResult(userId);
    if (cached) {
      clearJob(); // результат на экране и сохранён — задача не нужна
      setResult(cached.result);
      setResultCached(true);
      return;
    }
    const job = readJob(userId);
    if (!job) {
      if (userId === null) clearJob();
      return;
    }
    if (job.startedAt + JOB_TTL_MS <= Date.now()) {
      clearJob();
      setNotice(GONE_MESSAGE);
      return;
    }
    setLoading(true);
    setJobName(job.fileName);
    setElapsed(Math.max(0, Math.round((Date.now() - job.startedAt) / 1000)));
    void trackJob(job.jobId, job.fileName, job.startedAt).finally(() => setLoading(false));
  }, [sessionStatus, userId, trackJob]);

  const run = useCallback(async () => {
    if (!file) return;
    // A previous job may still be in flight — either genuinely still running,
    // or orphaned client-side after `pollJob` gave up on a flaky connection.
    // Cancel it before starting a new one instead of leaving it to burn
    // shared model capacity for a result nobody will read.
    if (activeJobIdRef.current) {
      cancelJob(activeJobIdRef.current);
      activeJobIdRef.current = null;
    }
    setLoading(true);
    setError(null);
    setCancelled(false);
    setNotice(null);
    setResult(null);
    setResultCached(false);
    setElapsed(0);
    setJobName(file.name);
    // Старт отсчитываем ДО отправки: окно жизни результата считается на
    // сервере от постановки задачи, поэтому так мы его не переоценим.
    const startedAt = Date.now();
    clearCachedResult();
    clearJob();
    jobPersistedRef.current = false;
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("stages", JSON.stringify(stages));
      const resp = await fetch("/api/anonymize", { method: "POST", body: fd });
      // Тело ответа может НЕ быть JSON: платформа отдаёт свои ошибки (413, 504,
      // «An error occurred…») обычным текстом или HTML. Читаем как текст и
      // разбираем защищённо, чтобы наружу шло понятное сообщение.
      const rawBody = await resp.text();
      let data: any = null;
      try {
        data = rawBody ? JSON.parse(rawBody) : null;
      } catch {
        if (resp.status === 413) {
          throw new Error(
            "Файл слишком большой: сервер отклонил запрос (413). Попробуйте уменьшить документ.",
          );
        }
        throw new Error(
          `Сервер вернул не JSON (HTTP ${resp.status}): ${rawBody.slice(0, 200)}`,
        );
      }
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);

      // POST только ПОСТАВИЛ задачу — досматриваем её опросом отсюда, из
      // браузера. Раньше ожидание сидело внутри серверной функции, и её предел
      // становился пределом анонимизации; на Hobby это ещё и ломало деплой
      // («invalid maxDuration for plan»). Браузер же не ограничен ничем.
      if (data?.jobId && !data?.done) {
        await trackJob(data.jobId, file.name, startedAt);
      } else {
        showResult(data as AnonResult, file.name, startedAt);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [file, stages, trackJob, showResult]);

  const toggleKept = (ph: string) =>
    setKept((prev) => {
      const next = new Set(prev);
      if (next.has(ph)) next.delete(ph);
      else next.add(ph);
      return next;
    });

  // Mapping the user chose to revert (placeholder -> original), and the
  // "effective" mapping that stays masked (the key that's actually needed).
  const keptMapping = useMemo(() => {
    const m: Record<string, string> = {};
    if (result) for (const ph of kept) if (ph in result.mapping) m[ph] = result.mapping[ph];
    return m;
  }, [result, kept]);

  const effectiveMapping = useMemo(() => {
    const m: Record<string, string> = {};
    if (result)
      for (const [ph, orig] of Object.entries(result.mapping)) if (!kept.has(ph)) m[ph] = orig;
    return m;
  }, [result, kept]);

  const mappingJson = useMemo(() => JSON.stringify(effectiveMapping, null, 2), [effectiveMapping]);

  // Preview with reverted placeholders substituted back to their originals.
  const previewText = useMemo(
    () => (result ? deanonClient(result.anonymized_text, keptMapping) : ""),
    [result, keptMapping],
  );

  // Build the effective document (reverted placeholders put back). For .txt we
  // do it client-side; for .docx we ask the backend to restore ONLY the kept
  // placeholders in the real .docx (preserving structure), leaving the rest
  // masked. Returns the bytes + filename to download/zip.
  const buildEffectiveDoc = useCallback(async (): Promise<Blob> => {
    if (!result) throw new Error("нет результата");
    if (!result.is_docx) {
      // Простой текст (.txt/.csv/.md/.json) — документ и есть его текст,
      // собираем прямо здесь; тип берём тот, что назвал сервер.
      const txt = deanonClient(result.anonymized_text, keptMapping);
      return new Blob([txt], { type: `${result.document_mime};charset=utf-8` });
    }
    if (kept.size === 0) {
      return new Blob([base64ToBuffer(result.document_base64)], { type: result.document_mime });
    }
    const resp = await fetch("/api/deanonymize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: result.document_name,
        file_base64: result.document_base64,
        mapping: keptMapping,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
    return new Blob([base64ToBuffer(data.document_base64)], { type: data.document_mime });
  }, [result, kept, keptMapping]);

  const downloadDoc = async () => {
    if (!result) return;
    setDocBusy(true);
    try {
      download(await buildEffectiveDoc(), result.document_name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDocBusy(false);
    }
  };
  const downloadMapping = () => {
    if (!result) return;
    download(new Blob([mappingJson], { type: "application/json" }), `${stem}.map.json`);
  };
  const downloadZip = async () => {
    if (!result) return;
    setDocBusy(true);
    try {
      const zip = new JSZip();
      zip.file(result.document_name, await buildEffectiveDoc());
      zip.file(`${stem}.map.json`, mappingJson);
      download(await zip.generateAsync({ type: "blob" }), `${stem}_anonymized.zip`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDocBusy(false);
    }
  };

  // --- Deanonymize action ---
  // forceLast — «восстановить последний обезличенный документ», не глядя на
  // галочку: так работает кнопка обратной подстановки сразу после
  // обезличивания (см. restoreLast).
  const runDeanon = useCallback(async (forceLast = false) => {
    const useLast = forceLast || deUseLast;
    setDeLoading(true);
    setDeError(null);
    setDeResult(null);
    try {
      let resp: Response;
      if (useLast) {
        if (!result) throw new Error("Нет последнего документа. Снимите галочку и загрузите файлы.");
        resp = await fetch("/api/deanonymize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: result.document_name,
            file_base64: result.document_base64,
            mapping: result.mapping,
          }),
        });
      } else {
        if (!deFile) throw new Error("Загрузите маскированный документ.");
        if (!deMapFile) throw new Error("Загрузите файл маппинга (.json).");
        const mappingText = await deMapFile.text();
        try {
          JSON.parse(mappingText);
        } catch {
          throw new Error("Маппинг не является корректным JSON.");
        }
        const fd = new FormData();
        fd.append("file", deFile);
        fd.append("mapping", mappingText);
        resp = await fetch("/api/deanonymize", { method: "POST", body: fd });
      }
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setDeResult(data as DeanonResult);
    } catch (e: unknown) {
      setDeError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeLoading(false);
    }
  }, [deUseLast, result, deFile, deMapFile]);

  const downloadRestored = () => {
    if (!deResult) return;
    download(
      new Blob([base64ToBuffer(deResult.document_base64)], { type: deResult.document_mime }),
      deResult.document_name,
    );
  };

  const entityCount = result ? Object.keys(result.mapping).length : 0;
  // Только явно исчерпанная квота или истёкший срок: пока сводки нет (не
  // вошёл, режим без авторизации, сеть) — не мешаем работать, решение всё
  // равно за бэкендом.
  const quotaExpired = summary?.quota.expired === true;
  const quotaExhausted = summary?.quota.exhausted === true || quotaExpired;

  const createInvite = async (e: React.FormEvent) => {
    e.preventDefault();
    setInviteBusy(true);
    setInviteError(null);
    setInviteUrl(null);
    setInviteCopied(false);
    try {
      const resp = await fetch("/api/account/invitations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setInviteUrl(typeof data?.url === "string" ? data.url : null);
      setInviteEmail("");
      await loadSummary();
    } catch (e: unknown) {
      setInviteError(e instanceof Error ? e.message : String(e));
    } finally {
      setInviteBusy(false);
    }
  };

  // Смена тарифа при действующем сроке открывает новый срок с даты оплаты,
  // а остаток текущего (страницы и дни) сгорает — см. lib/plan-activation.ts.
  // Тот же тариф продлевается, предупреждать не о чем.
  const burnOnSwitch =
    summary && !summary.quota.expired && summary.quota.paidUntil
      ? { pagesTenths: summary.quota.remainingTenths ?? 0, until: summary.quota.paidUntil }
      : null;

  const startPayment = async (plan: string, months: 1 | 12) => {
    if (
      burnOnSwitch &&
      burnOnSwitch.pagesTenths > 0 &&
      plan !== summary?.plan.code &&
      !window.confirm(
        `При смене тарифа сгорят оставшиеся ${pages(burnOnSwitch.pagesTenths)} стр. и остаток ` +
          `текущего срока (до ${dateRu(burnOnSwitch.until)}). Новый тариф начнёт действовать ` +
          "с даты оплаты.\n\nПродолжить?",
      )
    ) {
      return;
    }
    setPayKey(`${plan}:${months}`);
    setPayError(null);
    try {
      const resp = await fetch("/api/billing/checkout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan, months }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || typeof data?.url !== "string") {
        throw new Error(data?.error || `HTTP ${resp.status}`);
      }
      window.location.href = data.url;
    } catch (e: unknown) {
      setPayError(e instanceof Error ? e.message : String(e));
      setPayKey(null);
    }
  };

  const revokeInvite = async (id: number) => {
    setInviteError(null);
    try {
      const resp = await fetch(`/api/account/invitations?id=${id}`, { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      await loadSummary();
    } catch (e: unknown) {
      setInviteError(e instanceof Error ? e.message : String(e));
    }
  };

  const setUserActive = async (id: number, isActive: boolean) => {
    setInviteError(null);
    try {
      const resp = await fetch(`/api/account/users?id=${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ isActive }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      await loadSummary();
    } catch (e: unknown) {
      setInviteError(e instanceof Error ? e.message : String(e));
    }
  };

  const loadGlossary = useCallback(async () => {
    try {
      const resp = await fetch("/api/account/glossary", { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      setGlTerms(Array.isArray(data?.terms) ? (data.terms as GlossaryRow[]) : []);
      if (typeof data?.max === "number") setGlMax(data.max);
    } catch {
      /* словарь — вспомогательная карточка, не мешаем остальному кабинету */
    }
  }, []);

  const hasSummary = summary !== null;
  useEffect(() => {
    if (hasSummary) void loadGlossary();
  }, [hasSummary, loadGlossary]);

  const addGlossary = async (body: { aliases?: string; canonical?: string; bulk?: string }) => {
    setGlBusy(true);
    setGlError(null);
    setGlNote(null);
    try {
      const resp = await fetch("/api/account/glossary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      if (body.bulk !== undefined) {
        const skipped: { line: number; reason: string }[] = Array.isArray(data?.skipped)
          ? data.skipped
          : [];
        setGlNote(
          `Добавлено: ${data.added}. Пропущено: ${skipped.length}` +
            (skipped.length
              ? " — " +
                skipped
                  .slice(0, 5)
                  .map((s) => (s.line ? `строка ${s.line}: ${s.reason}` : s.reason))
                  .join(" ")
              : "."),
        );
        setGlBulk("");
      } else {
        setGlAliases("");
        setGlCanonical("");
      }
      await loadGlossary();
    } catch (e: unknown) {
      setGlError(e instanceof Error ? e.message : String(e));
    } finally {
      setGlBusy(false);
    }
  };

  const deleteGlossary = async (id: number) => {
    setGlError(null);
    setGlNote(null);
    try {
      const resp = await fetch(`/api/account/glossary?id=${id}`, { method: "DELETE" });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      await loadGlossary();
    } catch (e: unknown) {
      setGlError(e instanceof Error ? e.message : String(e));
    }
  };

  // Выбор пункта меню на узком экране закрывает его: меню там лежит поверх
  // содержимого, и оставлять его открытым значит прятать результат.
  const pickTab = (next: Tab) => {
    setTab(next);
    if (narrow) setMenuOpen(false);
  };

  // Обратная подстановка сразу после обезличивания: переключает режим и тут
  // же восстанавливает последний документ, чтобы не заставлять искать эту
  // возможность в меню и жать «Восстановить» вторым шагом.
  const restoreLast = () => {
    pickTab("deanon");
    setDeUseLast(true);
    void runDeanon(true);
  };

  return (
    <div className={`shell${menuOpen ? "" : " menu-closed"}`}>
      <aside className="sidebar">
        <div className="sidebar-head">
          <ShieldCheck size={20} />
          <span className="sidebar-title">Анонимизатор</span>
          <button
            className="icon-btn"
            onClick={() => setMenuOpen(false)}
            aria-label="Скрыть меню"
            title="Скрыть меню"
          >
            <PanelLeftClose size={18} />
          </button>
        </div>

        <nav className="sidebar-section">
          <div className="sidebar-label">Режим работы</div>
          <button
            className={`nav-item${tab === "anon" ? " active" : ""}`}
            onClick={() => pickTab("anon")}
          >
            <Lock size={16} />
            Анонимизация
          </button>
          <button
            className={`nav-item${tab === "deanon" ? " active" : ""}`}
            onClick={() => pickTab("deanon")}
          >
            <KeyRound size={16} />
            Деанонимизация
            {/* Точка появляется, когда есть обезличенный документ: обратная
                подстановка перестаёт быть спрятанной за незнакомым словом. */}
            {result && <span className="nav-dot" title="Есть документ для восстановления" />}
          </button>
          <button
            className={`nav-item${tab === "account" ? " active" : ""}`}
            onClick={() => pickTab("account")}
          >
            <CircleUser size={16} />
            Личный кабинет
          </button>
        </nav>

        <div className="sidebar-section">
          <button
            className="sidebar-toggle"
            onClick={() => setSettingsOpen((v) => !v)}
            aria-expanded={settingsOpen}
          >
            Экспериментальные настройки
            <ChevronDown size={15} className={`chevron${settingsOpen ? " open" : ""}`} />
          </button>
          {settingsOpen && (
            <>
              {(Object.keys(STAGE_LABELS) as StageKey[]).map((k) => (
                <label className="nav-item nav-check" key={k}>
                  <input
                    type="checkbox"
                    checked={stages[k]}
                    disabled={k === "subject" && !stages.llm}
                    onChange={() => toggle(k)}
                  />
                  {STAGE_LABELS[k]}
                </label>
              ))}
              <p className="note" style={{ margin: "10px 2px 0" }}>
                Можно отключить любой слой — например, оставить только GLiNER.
                LLM-проверка идёт последней: пересматривает найденное и снимает
                маскирование с очевидных ошибок (обычные слова, названия
                продуктов); работает, только если бэкенд запущен с --review.
              </p>
              <p className="note" style={{ margin: "8px 2px 0" }}>
                Предмет договора — наименования товаров, работ и услуг (модели,
                марки, номенклатура), чтобы по ним нельзя было восстановить
                отрасль. Идёт в тот же LLM-вызов, времени не добавляет; требует
                включённого слоя LLM.
              </p>
            </>
          )}
        </div>

        <div className="sidebar-section">
          <button
            className="sidebar-toggle"
            onClick={() => setFormatsOpen((v) => !v)}
            aria-expanded={formatsOpen}
          >
            Поддерживаемые форматы
            <ChevronDown size={15} className={`chevron${formatsOpen ? " open" : ""}`} />
          </button>
          {formatsOpen && (
            <>
              {FORMAT_GROUPS.map((group) => (
                <div className="format-group" key={group.title}>
                  <b>{group.title}</b>
                  <span className="note">{group.items}</span>
                </div>
              ))}
              <p className="note" style={{ margin: "2px 8px 0" }}>
                .rtf возвращается как .rtf, если на сервере есть LibreOffice; без неё — как
                .docx. Презентации (.ppt, .pptx) не принимаются.
              </p>
            </>
          )}
        </div>

        <div className="sidebar-foot">
          {summary && (
            // Остаток на виду всегда: узнавать об исчерпанной квоте в момент
            // загрузки документа — поздно.
            <button className="nav-item quota-mini" onClick={() => pickTab("account")}>
              <Gauge size={16} />
              {summary.quota.unlimited ? (
                <span>Квота: безлимит</span>
              ) : summary.quota.expired ? (
                <span>Срок тарифа истёк</span>
              ) : (
                <span>
                  Осталось {pages(summary.quota.remainingTenths ?? 0)} из{" "}
                  {pages(summary.quota.allowanceTenths ?? 0)} стр.
                </span>
              )}
            </button>
          )}
          {sessionStatus === "authenticated" && session?.user?.email ? (
            <>
              <div className="note sidebar-user">{session.user.email}</div>
              <button
                className="nav-item"
                onClick={() => {
                  // Результат предыдущего пользователя не должен всплыть у
                  // следующего в этой же вкладке.
                  clearCachedResult();
                  clearJob();
                  // Без серверного редиректа: за nginx Auth.js строит его из
                  // внутреннего адреса (https://localhost:8010), если не задан
                  // AUTH_URL. Относительный путь браузер разрешит сам.
                  void signOut({ redirect: false }).then(() => window.location.assign("/"));
                }}
              >
                <LogOut size={16} />
                Выйти
              </button>
            </>
          ) : sessionStatus !== "loading" ? (
            <>
              <Link className="nav-item" href="/login">
                <LogIn size={16} />
                Войти
              </Link>
              <Link className="nav-item" href="/register">
                <UserPlus size={16} />
                Регистрация
              </Link>
            </>
          ) : null}
          {/* Подвала на главном экране нет — документы здесь, как в claude.ai. */}
          <div className="sidebar-legal">
            <Link href={LEGAL_DOCS.offer.href}>Соглашение</Link>
            <Link href={LEGAL_DOCS.privacy.href}>Конфиденциальность</Link>
            <Link href="/legal">Правовая информация</Link>
          </div>
        </div>
      </aside>

      {/* Затемнение под меню — только на узком экране, где меню лежит поверх
          содержимого (см. globals.css). */}
      <div className="backdrop" onClick={() => setMenuOpen(false)} />

      {/* Кнопка живёт в углу окна, а не рядом с заголовком: заголовок
          центрируется вместе с колонкой и уезжает от края. */}
      <button
        className="icon-btn menu-open-btn"
        onClick={() => setMenuOpen(true)}
        aria-label="Показать меню"
        title="Показать меню"
      >
        <PanelLeft size={18} />
      </button>

      <main className="main">
        <div className="wrap">
          <header>
            <h1>
              <ShieldCheck size={24} />
              Анонимизатор персональных данных
            </h1>
            <p>Загрузите документ — получите маскированную версию и ключ восстановления (mapping).</p>
          </header>

          {tab === "anon" && (
            <>
              <div className="card">
                <h2>Документ</h2>
                <div
                  className={`drop${drag ? " drag" : ""}`}
                  onClick={() => inputRef.current?.click()}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setDrag(true);
                  }}
                  onDragLeave={() => setDrag(false)}
                  onDrop={(e) => {
                    e.preventDefault();
                    setDrag(false);
                    onPick(e.dataTransfer.files?.[0]);
                  }}
                >
                  <strong>Перетащите файл сюда</strong> или нажмите, чтобы выбрать
                  <div className="note">
                    Word, Excel, OpenDocument, PDF, текст — результат в том же формате.
                    Полный список — в меню слева.
                  </div>
                  {file && (
                    <div className="file-name">
                      <FileText size={16} />
                      {file.name}
                    </div>
                  )}
                </div>
                <input
                  ref={inputRef}
                  type="file"
                  accept=".docx,.doc,.pdf,.xlsx,.xls,.xlsm,.xml,.rtf,.odt,.txt,.csv,.md,.json"
                  style={{ display: "none" }}
                  onChange={(e) => onPick(e.target.files?.[0])}
                />
              </div>

              <div className="run">
                <button
                  className="primary big"
                  disabled={
                    !file || loading || !Object.values(stages).some(Boolean) || quotaExhausted
                  }
                  onClick={run}
                >
                  {loading ? (
                    <>
                      <LoaderCircle className="spin" size={18} />
                      Обрабатываю…
                    </>
                  ) : (
                    <>
                      <Lock size={18} />
                      Замаскировать
                    </>
                  )}
                </button>
                {loading && (
                  <span className="note" style={{ textAlign: "center" }}>
                    {jobName ? `Документ «${jobName}»: ` : ""}запрос идёт на бэкенд (GLiNER + LLM)
                    {elapsed > 0 ? `, ${elapsed} с` : ""}. Это может занять несколько минут —
                    вкладку можно свернуть, а страницу — перезагрузить.
                  </span>
                )}
                {quotaExpired && (
                  <div className="error">
                    {expiredMessage(summary!.plan.code, summary!.quota.paidUntil!)} Новые
                    документы не обрабатываются.
                  </div>
                )}
                {quotaExhausted && !quotaExpired && (
                  <div className="error">
                    Лимит страниц на {quotaPeriodLabel(summary!.quota)} исчерпан:
                    использовано {pages(summary!.quota.usedTenths)} из{" "}
                    {pages(summary!.quota.allowanceTenths ?? 0)}. Новые документы не
                    обрабатываются до следующего периода или увеличения лимита.
                  </div>
                )}
                {error && <div className="error">Ошибка: {error}</div>}
                {cancelled && !error && <div className="note">Задача отменена.</div>}
                {notice && !error && <div className="note">{notice}</div>}
              </div>

              {result && (
                <>
                  <div className="card">
                    <h2>Результат</h2>
                    {!!result.preexisting_placeholders && (
                      <div className="error" style={{ marginBottom: 14 }}>
                        <TriangleAlert size={16} className="inline-icon" />В файле уже было{" "}
                        {result.preexisting_placeholders} плейсхолдеров вида
                        [PERSON_1] — похоже, это уже маскированный документ. Они защищены и не
                        трогались повторно, но проверьте, не загрузили ли вы .anon-файл по ошибке.
                      </div>
                    )}
                    <div className="metrics">
                      <div className="metric">
                        <div className="v">{entityCount}</div>
                        <div className="k">Сущностей найдено</div>
                      </div>
                      <div className="metric">
                        <div className="v">{result.anonymized_text.length.toLocaleString("ru")}</div>
                        <div className="k">Символов</div>
                      </div>
                      <div className="metric">
                        <div className="v">{result.is_docx ? "DOCX" : "TXT"}</div>
                        <div className="k">Формат</div>
                      </div>
                    </div>
                    {(Object.keys(result.summary).length > 0 || result.elapsed_seconds != null) && (
                      <p className="note" style={{ marginTop: 14, marginBottom: 0 }}>
                        {Object.keys(result.summary).length > 0 && (
                          <>
                            По типам:{" "}
                            {Object.entries(result.summary)
                              .map(([k, v]) => `${k}: ${v}`)
                              .join(" · ")}
                          </>
                        )}
                        {result.elapsed_seconds != null && (
                          <>
                            {Object.keys(result.summary).length > 0 ? " · " : ""}
                            Время обработки: {result.elapsed_seconds.toFixed(1)} с
                          </>
                        )}
                      </p>
                    )}
                  </div>

                  {result.warnings && result.warnings.length > 0 && (() => {
                    // result.warnings — объединение двух разных форм (см.
                    // engine.py): найденные-но-не-скрытые значения (verify.py,
                    // есть value) и сбои отдельных слоёв проверки (есть message).
                    // Делим по наличию поля, а не по конкретным kind — так
                    // незнакомый в будущем kind всё равно попадёт в нужную карточку.
                    const residual = result.warnings!.filter((w) => w.value !== undefined);
                    const failed = mergeChunkWarnings(
                      result.warnings!.filter((w) => w.value === undefined),
                    );
                    // Жёсткие — слой не отработал по тексту, который больше никто
                    // так не смотрел; мягкие — не завершилась лишь перепроверка
                    // (см. SOFT_WARNING_KINDS).
                    const failedHard = failed.filter((w) => !SOFT_WARNING_KINDS.has(w.kind));
                    const failedSoft = failed.filter((w) => SOFT_WARNING_KINDS.has(w.kind));
                    return (
                      <>
                        {residual.length > 0 && (
                          <div className="card warn-card">
                            <h2 style={{ marginTop: 0 }}>
                              <TriangleAlert size={18} />
                              Проверьте вручную — возможно, не скрыто
                            </h2>
                            <p className="note" style={{ marginTop: 0 }}>
                              Автопроверка нашла в результате фрагменты, похожие на неанонимизированные
                              данные (длинные числа — счета/ОГРН/ИНН/телефоны, адреса эл. почты). Если это
                              действительно ПДн — фрагмент пропустили детекторы; сообщите, какой это тип, или
                              отредактируйте документ вручную.
                            </p>
                            <div className="scroll-tbl">
                              <table className="map">
                                <thead>
                                  <tr>
                                    <th>Тип</th>
                                    <th>Значение</th>
                                    <th>Контекст</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {residual.map((w, i) => (
                                    <tr key={i}>
                                      <td>
                                        <span className="tag">{warningLabel(w.kind)}</span>
                                      </td>
                                      <td>
                                        <code>{w.value}</code>
                                      </td>
                                      <td style={{ fontSize: 13, opacity: 0.8 }}>{w.context}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </div>
                        )}

                        {failedHard.length > 0 && (
                          <div className="card warn-card">
                            <h2 style={{ marginTop: 0 }}>
                              <TriangleAlert size={18} />
                              Часть документа проверена не полностью
                            </h2>
                            <p className="note" style={{ marginTop: 0 }}>
                              Один из проверочных слоёв не смог завершить работу на этих местах.
                              Остальные слои их разобрали, и маскирование чаще всего выполнено
                              полностью — но шанс пропуска здесь выше обычного, поэтому места стоит
                              просмотреть вручную.
                            </p>
                            <WarningList items={failedHard} />
                          </div>
                        )}

                        {failedSoft.length > 0 && (
                          <div className="card">
                            <h2 style={{ marginTop: 0 }}>
                              <Info size={18} />
                              Дополнительная перепроверка выполнена не полностью
                            </h2>
                            <p className="note" style={{ marginTop: 0 }}>
                              Это не пропуск: основные слои эти места проверили и данные в них
                              замаскированы. Не завершился лишь повторный проход, который ищет то, что
                              могли не заметить основные слои.
                            </p>
                            <WarningList items={failedSoft} />
                          </div>
                        )}
                      </>
                    );
                  })()}

                  <div className="card">
                    <h2>
                      <Package size={18} />
                      Скачать
                    </h2>
                    <div className="row">
                      <button className="ghost" onClick={downloadZip} disabled={docBusy}>
                        <Download size={16} />
                        ZIP (документ + mapping)
                      </button>
                      <button className="ghost" onClick={downloadDoc} disabled={docBusy}>
                        <Download size={16} />
                        {result.document_name}
                      </button>
                      <button className="ghost" onClick={downloadMapping}>
                        <Braces size={16} />
                        {stem}.map.json
                      </button>
                      {docBusy && <span className="note">Собираю документ…</span>}
                    </div>
                    {formatNote(result) && (
                      <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                        <FileType size={16} className="inline-icon" />
                        {formatNote(result)}
                      </p>
                    )}
                    <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                      <TriangleAlert size={16} className="inline-icon" />
                      Mapping — ключ восстановления. Храните его отдельно от маскированного документа.
                    </p>
                    <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                      <Info size={16} className="inline-icon" />
                      {resultCached
                        ? "Результат сохранён в этой вкладке до её закрытия (не дольше 10 минут). Скачайте файл и карту соответствий — после закрытия вкладки получить их повторно нельзя."
                        : "Результат доступен на сервере не дольше 10 минут с момента загрузки документа. Скачайте файл и карту соответствий — потом получить их повторно нельзя."}
                    </p>
                  </div>

                  <div className="run">
                    <button className="ghost big" onClick={restoreLast} disabled={deLoading}>
                      {deLoading ? (
                        <>
                          <LoaderCircle className="spin" size={18} />
                          Восстанавливаю…
                        </>
                      ) : (
                        <>
                          <Undo2 size={18} />
                          Обратная подстановка
                        </>
                      )}
                    </button>
                    <span className="note" style={{ textAlign: "center" }}>
                      Вернёт исходные значения в этот документ и покажет результат в режиме
                      «Деанонимизация». Ключ уже здесь — загружать mapping не нужно.
                    </span>
                  </div>

                  <div className="card">
                    <h2>Маскированный текст</h2>
                    <pre className="preview">{previewText}</pre>
                  </div>

                  <div className="card">
                    <h2>Mapping ({entityCount})</h2>
                    {kept.size > 0 && (
                      <p className="note" style={{ marginTop: 0 }}>
                        Возвращено в текст вручную: {kept.size}. Эти значения НЕ замаскированы — они
                        исключены из ключа и подставлены в документ. Нажмите «Вернуть», чтобы снова
                        скрыть.
                      </p>
                    )}
                    {entityCount === 0 ? (
                      <p className="note">Сущностей не найдено.</p>
                    ) : (
                      <div className="scroll-tbl">
                        <table className="map">
                          <thead>
                            <tr>
                              <th>Плейсхолдер</th>
                              <th>Тип</th>
                              <th>Оригинал</th>
                              <th>Действие</th>
                            </tr>
                          </thead>
                          <tbody>
                            {Object.entries(result.mapping).map(([ph, orig]) => {
                              const isKept = kept.has(ph);
                              return (
                                <tr key={ph} style={isKept ? { opacity: 0.55 } : undefined}>
                                  <td>
                                    <code>{ph}</code>
                                  </td>
                                  <td>
                                    <span className="tag">{labelOf(ph)}</span>
                                  </td>
                                  <td style={isKept ? { textDecoration: "line-through" } : undefined}>
                                    {orig}
                                  </td>
                                  <td>
                                    <button
                                      className="ghost"
                                      style={{ padding: "4px 10px", fontSize: 13 }}
                                      onClick={() => toggleKept(ph)}
                                      title={
                                        isKept
                                          ? "Снова скрыть это значение в документе"
                                          : "Оставить это значение в тексте (не анонимизировать)"
                                      }
                                    >
                                      {isKept ? (
                                        <>
                                          <Undo2 size={14} />
                                          Вернуть маску
                                        </>
                                      ) : (
                                        <>
                                          <Eye size={14} />
                                          Оставить в тексте
                                        </>
                                      )}
                                    </button>
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </>
              )}
            </>
          )}

          {tab === "account" && (
            <>
              {summaryError && <div className="error">Ошибка: {summaryError}</div>}
              {!summary && !summaryError && (
                <div className="card">
                  <p className="note" style={{ margin: 0 }}>
                    Кабинет доступен после входа в систему.
                  </p>
                </div>
              )}
              {summary && (
                <>
                  <div className="card">
                    <h2>
                      <CircleUser size={18} />
                      Аккаунт
                    </h2>
                    <table className="map">
                      <tbody>
                        <tr>
                          <th style={{ width: 220 }}>Пользователь</th>
                          <td>{summary.user.email}</td>
                        </tr>
                        <tr>
                          <th>Роль</th>
                          <td>{ROLE_LABELS[summary.user.role] ?? summary.user.role}</td>
                        </tr>
                        <tr>
                          <th>Аккаунт</th>
                          <td>{summary.account.name}</td>
                        </tr>
                        <tr>
                          <th>Состояние</th>
                          <td>{summary.account.isActive ? "Активен" : "Отключён"}</td>
                        </tr>
                        <tr>
                          <th>Пользователей</th>
                          <td>
                            {summary.users.active} активн.
                            {summary.users.limit != null
                              ? `, занято мест ${summary.users.taken} из ${summary.users.limit}`
                              : " (тариф без ограничения)"}
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>

                  <div className="card">
                    <h2>
                      <Package size={18} />
                      Тарифный план
                    </h2>
                    <div className="metrics">
                      <div className="metric">
                        <div className="v">{summary.plan.title}</div>
                        <div className="k">План</div>
                      </div>
                      <div className="metric">
                        <div className="v">
                          {summary.plan.pagesPerMonth != null
                            ? summary.plan.pagesPerMonth.toLocaleString("ru")
                            : "∞"}
                        </div>
                        <div className="k">Страниц в месяц</div>
                      </div>
                      <div className="metric">
                        <div className="v">
                          {summary.plan.priceKopecks > 0
                            ? `${rubles(summary.plan.priceKopecks)} ₽`
                            : "Бесплатно"}
                        </div>
                        <div className="k">Стоимость</div>
                      </div>
                    </div>
                    {summary.quota.limitTenths != null &&
                      summary.plan.pagesPerMonth != null &&
                      summary.quota.limitTenths !== summary.plan.pagesPerMonth * 10 && (
                        <p className="note" style={{ marginBottom: 0 }}>
                          Для аккаунта задан индивидуальный лимит{" "}
                          {pages(summary.quota.limitTenths)} стр. — он замещает лимит
                          тарифа, а не складывается с ним.
                        </p>
                      )}
                    {summary.offers.length > 0 && (
                      <>
                        <h3 style={{ marginBottom: 8 }}>
                          {summary.plan.priceKopecks > 0 ? "Продлить или сменить тариф" : "Оплатить тариф"}
                        </h3>
                        <p className="note" style={{ marginTop: 0 }}>
                          Оплата сразу за год дешевле, чем 12 месяцев по отдельности.
                        </p>
                        <div className="scroll-tbl">
                          <table className="map">
                            <thead>
                              <tr>
                                <th>Тариф</th>
                                <th>Страниц в месяц</th>
                                <th>Пользователей</th>
                                <th style={{ textAlign: "center" }}>Оплата за месяц</th>
                                <th style={{ textAlign: "center" }}>Оплата за год</th>
                              </tr>
                            </thead>
                            <tbody>
                              {summary.offers.map((o) => {
                                // Выгода годовой оплаты против 12 месяцев по отдельности.
                                const yearSaving =
                                  o.priceYearKopecks != null ? o.priceKopecks * 12 - o.priceYearKopecks : 0;
                                return (
                                  <tr key={o.code}>
                                    <td>
                                      {o.title}
                                      {o.code === summary.plan.code && (
                                        <span className="tag" style={{ marginLeft: 8 }}>текущий</span>
                                      )}
                                    </td>
                                    <td>{o.pagesPerMonth?.toLocaleString("ru") ?? "∞"}</td>
                                    <td>{o.maxUsers ?? "без ограничения"}</td>
                                    <td style={{ textAlign: "center" }}>
                                      <button
                                        className="ghost"
                                        style={{ padding: "4px 10px", fontSize: 13, minWidth: 130, justifyContent: "center" }}
                                        disabled={payKey !== null || summary.payBlockedReason !== null}
                                        onClick={() => startPayment(o.code, 1)}
                                      >
                                        {payKey === `${o.code}:1`
                                          ? "Открываю…"
                                          : `${rubles(o.priceKopecks)} ₽ / мес.`}
                                      </button>
                                    </td>
                                    <td style={{ textAlign: "center" }}>
                                      {o.priceYearKopecks != null ? (
                                        <>
                                          <button
                                            className="ghost"
                                            style={{ padding: "4px 10px", fontSize: 13, minWidth: 130, justifyContent: "center" }}
                                            disabled={payKey !== null || summary.payBlockedReason !== null}
                                            onClick={() => startPayment(o.code, 12)}
                                          >
                                            {payKey === `${o.code}:12`
                                              ? "Открываю…"
                                              : `${rubles(o.priceYearKopecks)} ₽ / год`}
                                          </button>
                                          {yearSaving > 0 && (
                                            <div className="note" style={{ marginTop: 4, fontSize: 12 }}>
                                              {rubles(Math.round(o.priceYearKopecks / 12 / 100) * 100)} ₽ / мес.
                                              (скидка {Math.round((yearSaving / (o.priceKopecks * 12)) * 100)}%)
                                            </div>
                                          )}
                                        </>
                                      ) : (
                                        <span className="note">—</span>
                                      )}
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                        {summary.payBlockedReason === "role" && (
                          <p className="note" style={{ marginBottom: 0 }}>
                            Оплатить или сменить тариф может администратор аккаунта.
                          </p>
                        )}
                        {summary.payBlockedReason === "not_configured" && (
                          <div className="error" style={{ marginTop: 12 }}>
                            Онлайн-оплата пока не подключена. Чтобы оплатить тариф, обратитесь
                            в поддержку сервиса.
                          </div>
                        )}
                        {burnOnSwitch && burnOnSwitch.pagesTenths > 0 && (
                          <div className="card warn-card" style={{ marginTop: 12, marginBottom: 0 }}>
                            <p className="note" style={{ margin: 0 }}>
                              <TriangleAlert size={16} className="inline-icon" />
                              При смене тарифа сгорят оставшиеся{" "}
                              {pages(burnOnSwitch.pagesTenths)} стр. и остаток текущего срока
                              (до {dateRu(burnOnSwitch.until)}): новый тариф начнёт действовать с
                              даты оплаты.
                            </p>
                          </div>
                        )}
                        <p className="note" style={{ marginBottom: 0 }}>
                          Повторная оплата текущего тарифа продлевает срок на оплаченные месяцы:
                          текущий месяц дорабатывается с остатком страниц, лимит обновляется в
                          начале каждого следующего месяца. Оплата картой или через СБП на
                          защищённой странице Т-Банка — данные карты сервису не передаются.
                        </p>
                        {payError && (
                          <div className="error" style={{ marginTop: 12 }}>
                            {payError}
                          </div>
                        )}
                      </>
                    )}
                    {summary.payments.length > 0 && (
                      <>
                        <h3 style={{ marginBottom: 8 }}>Платежи</h3>
                        <div className="scroll-tbl">
                          <table className="map">
                            <thead>
                              <tr>
                                <th>Дата</th>
                                <th>Тариф</th>
                                <th>Сумма</th>
                                <th>Возвращено</th>
                                <th>Состояние</th>
                                <th>Доступ до</th>
                              </tr>
                            </thead>
                            <tbody>
                              {summary.payments.map((p) => (
                                <tr key={p.orderId}>
                                  <td>{new Date(p.createdAt).toLocaleString("ru")}</td>
                                  <td>
                                    {p.planTitle}, {p.months === 12 ? "12 мес." : "1 мес."}
                                  </td>
                                  <td>{rubles(p.amountKopecks)} ₽</td>
                                  <td>{p.refundedKopecks > 0 ? `${rubles(p.refundedKopecks)} ₽` : "—"}</td>
                                  <td>{paymentStatusLabel(p)}</td>
                                  <td>
                                    {p.accessUntil && p.activated ? dateRu(p.accessUntil) : "—"}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                        <p className="note" style={{ marginBottom: 0 }}>
                          Полный возврат сокращает срок на оплаченные этим платежом месяцы (если
                          срок был один месяц — доступ закрывается сразу); при частичном возврате
                          срок не меняется. «Доступ до» — срок на момент платежа; действующий срок
                          аккаунта — в карточке «Квота».
                        </p>
                      </>
                    )}
                  </div>

                  <div
                    className={`card${summary.quota.exhausted || summary.quota.expired ? " warn-card" : ""}`}
                  >
                    <h2>
                      <Gauge size={18} />
                      Квота — {quotaPeriodLabel(summary.quota)}
                    </h2>
                    {summary.quota.paidUntil && (
                      <p className="note" style={{ marginTop: 0 }}>
                        {summary.quota.expired
                          ? expiredMessage(summary.plan.code, summary.quota.paidUntil)
                          : `Тариф действует до ${dateRu(summary.quota.paidUntil)}.`}
                      </p>
                    )}
                    {summary.quota.unlimited ? (
                      <p className="note" style={{ marginTop: 0 }}>
                        Лимит не установлен. Израсходовано за период:{" "}
                        {pages(summary.quota.usedTenths)} стр.
                      </p>
                    ) : summary.quota.expired ? (
                      // Страниц по истёкшему сроку нет — не показываем лимит,
                      // который всё равно нельзя потратить.
                      <p className="note" style={{ marginTop: 0 }}>
                        Доступных страниц нет: срок тарифа закончился. Израсходовано за
                        последний период: {pages(summary.quota.usedTenths)} стр.
                      </p>
                    ) : (
                      <>
                        <div className="meter">
                          <div
                            className={`meter-fill${summary.quota.exhausted ? " over" : ""}`}
                            style={{
                              width: `${Math.min(
                                100,
                                summary.quota.allowanceTenths
                                  ? (summary.quota.usedTenths / summary.quota.allowanceTenths) * 100
                                  : 0,
                              )}%`,
                            }}
                          />
                        </div>
                        <div className="metrics" style={{ marginTop: 14 }}>
                          <div className="metric">
                            <div className="v">{pages(summary.quota.usedTenths)}</div>
                            <div className="k">Израсходовано, стр.</div>
                          </div>
                          <div className="metric">
                            <div className="v">{pages(summary.quota.remainingTenths ?? 0)}</div>
                            <div className="k">Осталось, стр.</div>
                          </div>
                          <div className="metric">
                            <div className="v">{pages(summary.quota.allowanceTenths ?? 0)}</div>
                            <div className="k">Лимит, стр.</div>
                          </div>
                        </div>
                        {summary.quota.grantsTenths > 0 && (
                          <p className="note" style={{ marginBottom: 0, marginTop: 12 }}>
                            В лимит входит добавка {pages(summary.quota.grantsTenths)} стр.,
                            выданная сверх тарифа.
                          </p>
                        )}
                        {summary.quota.exhausted && (
                          <p className="note" style={{ marginBottom: 0, marginTop: 12 }}>
                            Новые документы не принимаются до следующего периода или
                            увеличения лимита. Уже начатая обработка не прерывается —
                            отказ получает только следующий запрос.
                          </p>
                        )}
                      </>
                    )}
                    <p className="note" style={{ marginBottom: 0, marginTop: 12 }}>
                      Единица учёта — страница, 1800 символов. Лимит действует один календарный
                      месяц с даты оплаты (для пробного тарифа — с даты регистрации); пробный
                      период не продлевается. Границы периода — по UTC. За{" "}
                      {quotaPeriodLabel(summary.quota)} обработано документов: {summary.periodTotals.documents}, символов:{" "}
                      {summary.periodTotals.chars.toLocaleString("ru")}.
                    </p>
                  </div>

                  <div className="card">
                    <h2>
                      <Users size={18} />
                      Пользователи аккаунта
                    </h2>
                    <p className="note" style={{ marginTop: 0 }}>
                      {summary.users.limit === 1 ? (
                        <>
                          На тарифе «{summary.plan.title}» работает один пользователь — приглашения
                          недоступны. Чтобы подключить сотрудников, перейдите на тариф «Бизнес»
                          или «Корпоратив».
                        </>
                      ) : summary.users.limit != null ? (
                        <>
                          Занято мест: {summary.users.taken} из {summary.users.limit} по
                          тарифу. Место занимает и активный пользователь, и ещё не
                          активированное приглашение; отключённый пользователь место
                          освобождает.
                        </>
                      ) : (
                        <>
                          Пользователей и приглашений: {summary.users.taken}. Число
                          пользователей не ограничено; все они расходуют общий лимит страниц.
                        </>
                      )}
                    </p>

                    <div className="scroll-tbl">
                      <table className="map">
                        <thead>
                          <tr>
                            <th>Пользователь</th>
                            <th>Роль</th>
                            <th>Состояние</th>
                            {summary.canManage && <th>Действие</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {summary.users.list.map((u) => (
                            <tr key={u.id} style={u.isActive ? undefined : { opacity: 0.55 }}>
                              <td>
                                {u.email}
                                {u.isSelf && <span className="tag" style={{ marginLeft: 8 }}>это вы</span>}
                              </td>
                              <td>{ROLE_LABELS[u.role] ?? u.role}</td>
                              <td>{u.isActive ? "Активен" : "Отключён"}</td>
                              {summary.canManage && (
                                <td>
                                  {u.isSelf || u.role === "root" ? (
                                    <span className="note">—</span>
                                  ) : (
                                    <button
                                      className="ghost"
                                      style={{ padding: "4px 10px", fontSize: 13 }}
                                      onClick={() => setUserActive(u.id, !u.isActive)}
                                    >
                                      {u.isActive ? "Отключить" : "Включить"}
                                    </button>
                                  )}
                                </td>
                              )}
                            </tr>
                          ))}
                          {summary.invitations.map((inv) => (
                            <tr key={`inv-${inv.id}`}>
                              <td>
                                {inv.email}
                                <span className="tag" style={{ marginLeft: 8 }}>приглашение</span>
                              </td>
                              <td>{ROLE_LABELS[inv.role] ?? inv.role}</td>
                              <td>
                                Ждёт активации до{" "}
                                {new Date(inv.expiresAt).toLocaleDateString("ru")}
                              </td>
                              {summary.canManage && (
                                <td>
                                  <button
                                    className="ghost"
                                    style={{ padding: "4px 10px", fontSize: 13 }}
                                    onClick={() => revokeInvite(inv.id)}
                                  >
                                    Отозвать
                                  </button>
                                </td>
                              )}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>

                    {summary.canManage && summary.users.limit !== 1 && (
                      <>
                        <form className="row" style={{ marginTop: 16 }} onSubmit={createInvite}>
                          <input
                            type="email"
                            required
                            placeholder="адрес приглашаемого"
                            value={inviteEmail}
                            onChange={(e) => setInviteEmail(e.target.value)}
                            style={{ flex: 1, minWidth: 220 }}
                          />
                          <select
                            value={inviteRole}
                            onChange={(e) => setInviteRole(e.target.value as "member" | "admin")}
                          >
                            <option value="member">Пользователь</option>
                            <option value="admin">Администратор аккаунта</option>
                          </select>
                          <button
                            className="ghost"
                            type="submit"
                            disabled={inviteBusy || summary.users.free === 0}
                          >
                            <UserPlus size={16} />
                            {inviteBusy ? "Выписываю…" : "Пригласить"}
                          </button>
                        </form>
                        {summary.users.free === 0 && (
                          <p className="note" style={{ marginBottom: 0 }}>
                            Свободных мест нет. Отключите пользователя, отзовите приглашение или
                            перейдите на тариф с большим числом мест.
                          </p>
                        )}
                        {inviteError && (
                          <div className="error" style={{ marginTop: 12 }}>
                            {inviteError}
                          </div>
                        )}
                        {inviteUrl && (
                          <div className="card warn-card" style={{ marginTop: 14, marginBottom: 0 }}>
                            <p className="note" style={{ marginTop: 0 }}>
                              <TriangleAlert size={16} className="inline-icon" />
                              Ссылка активации показывается ОДИН раз — в базе хранится только её
                              хеш. Передайте её приглашённому; если потеряете, приглашение
                              придётся отозвать и выписать заново. Пароль и согласие на обработку
                              данных задаёт сам приглашённый.
                            </p>
                            <div className="row">
                              <code style={{ wordBreak: "break-all", flex: 1 }}>{inviteUrl}</code>
                              <button
                                className="ghost"
                                onClick={() => {
                                  void navigator.clipboard?.writeText(inviteUrl);
                                  setInviteCopied(true);
                                }}
                              >
                                {inviteCopied ? "Скопировано" : "Скопировать"}
                              </button>
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </div>

                  <div className="card">
                    <h2>
                      <ShieldCheck size={18} />
                      Словарь терминов
                    </h2>
                    <p className="note" style={{ marginTop: 0 }}>
                      Эти слова и названия всегда маскируются в документах вашего аккаунта —
                      например, названия месторождений, объектов, шифры проектов. Падежи
                      учитываются автоматически. Термины видны только пользователям вашего
                      аккаунта.
                    </p>
                    <p className="note">
                      Записей: {glTerms.length} из {glMax}
                      {!summary.canManage && " · изменять словарь может администратор аккаунта"}
                    </p>

                    {glTerms.length > 0 && (
                      <div className="scroll-tbl">
                        <table className="map">
                          <thead>
                            <tr>
                              <th>Варианты написания</th>
                              <th>В карте замен</th>
                              {summary.canManage && <th>Действие</th>}
                            </tr>
                          </thead>
                          <tbody>
                            {glTerms.map((t) => (
                              <tr key={t.id}>
                                <td>{t.aliases}</td>
                                <td>{t.canonical}</td>
                                {summary.canManage && (
                                  <td>
                                    <button
                                      className="ghost"
                                      style={{ padding: "4px 10px", fontSize: 13 }}
                                      onClick={() => deleteGlossary(t.id)}
                                    >
                                      Удалить
                                    </button>
                                  </td>
                                )}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}

                    {summary.canManage && (
                      <>
                        <form
                          className="row"
                          style={{ marginTop: 16 }}
                          onSubmit={(e) => {
                            e.preventDefault();
                            void addGlossary({ aliases: glAliases, canonical: glCanonical });
                          }}
                        >
                          <input
                            required
                            placeholder="Варианты написания через запятую"
                            value={glAliases}
                            onChange={(e) => setGlAliases(e.target.value)}
                            style={{ flex: 2, minWidth: 220 }}
                          />
                          <input
                            placeholder="Как называть в карте замен (необязательно)"
                            value={glCanonical}
                            onChange={(e) => setGlCanonical(e.target.value)}
                            style={{ flex: 1, minWidth: 200 }}
                          />
                          <button
                            className="ghost"
                            type="submit"
                            disabled={glBusy || glTerms.length >= glMax}
                          >
                            Добавить
                          </button>
                        </form>

                        <p style={{ marginBottom: 0 }}>
                          <button
                            className="ghost"
                            style={{ padding: "4px 10px", fontSize: 13 }}
                            onClick={() => setGlBulkOpen((v) => !v)}
                          >
                            {glBulkOpen ? "Скрыть массовый ввод" : "Вставить списком"}
                          </button>
                        </p>
                        {glBulkOpen && (
                          <>
                            <p className="note">
                              По одной записи в строке: «вариант1, вариант2 = Каноническое имя».
                              Часть после «=» необязательна; всё после «#» в строке игнорируется.
                            </p>
                            <textarea
                              rows={6}
                              value={glBulk}
                              onChange={(e) => setGlBulk(e.target.value)}
                              placeholder={"Ямбург, Ямбургское = Месторождение А\nШФ-7"}
                              style={{ width: "100%", boxSizing: "border-box" }}
                            />
                            <button
                              className="ghost"
                              style={{ marginTop: 8 }}
                              disabled={glBusy || !glBulk.trim()}
                              onClick={() => void addGlossary({ bulk: glBulk })}
                            >
                              Добавить списком
                            </button>
                          </>
                        )}
                      </>
                    )}
                    {glError && (
                      <div className="error" style={{ marginTop: 12 }}>
                        {glError}
                      </div>
                    )}
                    {glNote && (
                      <p className="note" style={{ marginBottom: 0 }}>
                        {glNote}
                      </p>
                    )}
                  </div>

                  <div className="card">
                    <h2>История обработки</h2>
                    {summary.recent.length === 0 ? (
                      <p className="note" style={{ margin: 0 }}>
                        Документов ещё не было.
                      </p>
                    ) : (
                      <div className="scroll-tbl">
                        <table className="map">
                          <thead>
                            <tr>
                              <th>Дата</th>
                              <th>Пользователь</th>
                              <th>Символов</th>
                              <th>Страниц</th>
                              <th>Время</th>
                              <th>Итог</th>
                            </tr>
                          </thead>
                          <tbody>
                            {summary.recent.map((r) => (
                              <tr key={r.id}>
                                <td>{new Date(r.createdAt).toLocaleString("ru")}</td>
                                <td>{r.userEmail ?? "—"}</td>
                                <td>{r.chars.toLocaleString("ru")}</td>
                                <td>{pages(r.billablePagesTenths)}</td>
                                <td>{r.seconds.toFixed(1)} с</td>
                                <td>
                                  {/* Не-ok запись (отмена/ошибка) в БД не различает причину — отдельной
                                      колонки нет; главное для клиента, что страницы не списаны. */}
                                  <span className="tag">{r.ok ? "успешно" : "отменено или ошибка — не списано"}</span>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                    <p className="note" style={{ marginBottom: 0, marginTop: 12 }}>
                      Показаны последние 20 документов аккаунта. Имена файлов не
                      сохраняются — в журнале только объём и время обработки.
                    </p>
                  </div>
                </>
              )}
            </>
          )}

          {tab === "deanon" && (
            <>
              <div className="card">
                <h2>Восстановление по маппингу (без ИИ)</h2>
                {result ? (
                  <label className="stage" style={{ borderRadius: 10 }}>
                    <input
                      type="checkbox"
                      checked={deUseLast}
                      onChange={() => setDeUseLast((v) => !v)}
                    />
                    Использовать последний документ («{result.document_name}», {entityCount} сущностей)
                  </label>
                ) : (
                  <p className="note">
                    Последнего документа нет — загрузите маскированный файл и маппинг вручную.
                  </p>
                )}
              </div>

              {!deUseLast && (
                <div className="card">
                  <h2>Файлы</h2>
                  <div className="row" style={{ alignItems: "stretch" }}>
                    <div
                      className="drop"
                      style={{ flex: 1, minWidth: 220 }}
                      onClick={() => deFileRef.current?.click()}
                    >
                      <strong>Маскированный документ</strong>
                      <div className="note">тот формат, в котором его выдал сервис</div>
                      {deFile && (
                        <div className="file-name">
                          <FileText size={16} />
                          {deFile.name}
                        </div>
                      )}
                    </div>
                    <div
                      className="drop"
                      style={{ flex: 1, minWidth: 220 }}
                      onClick={() => deMapRef.current?.click()}
                    >
                      <strong>Маппинг</strong>
                      <div className="note">.json</div>
                      {deMapFile && (
                        <div className="file-name">
                          <KeyRound size={16} />
                          {deMapFile.name}
                        </div>
                      )}
                    </div>
                  </div>
                  <input
                    ref={deFileRef}
                    type="file"
                    accept=".docx,.xlsx,.xlsm,.odt,.rtf,.xml,.txt,.csv,.md,.json"
                    style={{ display: "none" }}
                    onChange={(e) => setDeFile(e.target.files?.[0] || null)}
                  />
                  <input
                    ref={deMapRef}
                    type="file"
                    accept=".json"
                    style={{ display: "none" }}
                    onChange={(e) => setDeMapFile(e.target.files?.[0] || null)}
                  />
                </div>
              )}

              <div className="run">
                <button
                  className="primary big"
                  disabled={deLoading || (!deUseLast && (!deFile || !deMapFile)) || (deUseLast && !result)}
                  onClick={() => runDeanon()}
                >
                  {deLoading ? (
                    <>
                      <LoaderCircle className="spin" size={18} />
                      Восстанавливаю…
                    </>
                  ) : (
                    <>
                      <KeyRound size={18} />
                      Восстановить
                    </>
                  )}
                </button>
                {deError && <div className="error">Ошибка: {deError}</div>}
              </div>

              {deResult && (
                <>
                  <div className="card">
                    <h2>
                      <Package size={18} />
                      Скачать
                    </h2>
                    <div className="row">
                      <button className="ghost" onClick={downloadRestored}>
                        <Download size={16} />
                        {deResult.document_name}
                      </button>
                    </div>
                    {deResult.leftover.length > 0 ? (
                      <div className="error" style={{ marginTop: 12 }}>
                        Плейсхолдеры без значения в маппинге: {deResult.leftover.join(", ")}
                      </div>
                    ) : (
                      <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                        <CircleCheckBig size={16} className="inline-icon" />
                        Все плейсхолдеры восстановлены.
                      </p>
                    )}
                  </div>

                  <div className="card">
                    <h2>Восстановленный текст</h2>
                    <pre className="preview">{deResult.restored_text}</pre>
                  </div>
                </>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
