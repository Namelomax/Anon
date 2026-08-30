"use client";

import JSZip from "jszip";
import Link from "next/link";
import { signOut, useSession } from "next-auth/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

type StageKey = "regex" | "corporate" | "ner" | "llm" | "review" | "subject";

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
): Promise<any> {
  const startedAt = Date.now();
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
      if (++failures > 10) throw new Error("Связь с сервером потеряна");
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
  gliner_chunk_failed: "Фрагмент не проверен",
  llm_chunk_failed: "Фрагмент не проверен",
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

// «символы 12 400–13 200» — офсет/длина есть только у чанковых сбоев
// (gliner_chunk_failed/llm_chunk_failed), чтобы показать, ГДЕ смотреть.
function warningRange(offset?: number, chars?: number): string | null {
  if (offset == null || chars == null) return null;
  const start = offset.toLocaleString("ru");
  const end = (offset + chars).toLocaleString("ru");
  return `символы ${start}–${end}`;
}

// Replace each placeholder token with its original value. Placeholders are
// distinct "[LABEL_N]" tokens, so plain split/join is safe (no partial-match
// clashes: "[PERSON_1]" is not a substring of "[PERSON_10]").
function deanonClient(text: string, m: Record<string, string>): string {
  let out = text;
  for (const [ph, orig] of Object.entries(m)) out = out.split(ph).join(orig);
  return out;
}

export default function Home() {
  // Только отображение личности/выход — проверка прав и квоты в этой задаче
  // сознательно не делается (см. спеку: "не строить проверки квоты в этой
  // задаче"), поэтому статус сессии здесь ни на что не влияет, кроме шапки.
  const { data: session, status: sessionStatus } = useSession();
  const [tab, setTab] = useState<"anon" | "deanon">("anon");

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
  const [drag, setDrag] = useState(false);
  const [stagesOpen, setStagesOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // Id of the job currently being polled, kept in a ref (not just state) so
  // the `pagehide` handler can read the latest value without a stale
  // closure. Cleared as soon as the job reaches a terminal state (done or
  // cancelled) so a finished job is never cancelled by a later unload/new
  // submit.
  const activeJobIdRef = useRef<string | null>(null);
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

  // Cancel the in-flight job when the tab is closed, reloaded, or navigated
  // away from. `pagehide` (not `beforeunload`) is used because it also fires
  // on mobile/bfcache navigations. `keepalive: true` on the fetch is what
  // lets the request survive the page going away.
  useEffect(() => {
    const onPageHide = () => {
      const jobId = activeJobIdRef.current;
      if (!jobId) return;
      cancelJob(jobId, true);
    };
    window.addEventListener("pagehide", onPageHide);
    return () => window.removeEventListener("pagehide", onPageHide);
  }, []);

  const onPick = (f: File | null | undefined) => {
    if (!f) return;
    if (!/\.(docx?|pdf|xlsx?|xlsm|xml|rtf|odt|txt|csv|md)$/i.test(f.name)) {
      setError("Поддерживаются .docx, .doc, .pdf, .xlsx, .xls, .xml, .rtf, .odt, .txt (кроме презентаций)");
      return;
    }
    setError(null);
    setResult(null);
    setCancelled(false);
    setFile(f);
  };

  const toggle = (k: StageKey) => setStages((s) => ({ ...s, [k]: !s[k] }));

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
    setResult(null);
    setElapsed(0);
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
      let final: any = data;
      if (data?.jobId && !data?.done) {
        activeJobIdRef.current = data.jobId;
        final = await pollJob(data.jobId, setElapsed);
        // Terminal state reached (done or cancelled) — clear the ref so a
        // later unload/new submit never sends a cancel for this job again.
        activeJobIdRef.current = null;
      }

      if (final?.cancelled) {
        setCancelled(true);
      } else {
        setResult(final as AnonResult);
        setKept(new Set());
        setDeUseLast(true);
        setDeResult(null);
      }
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [file, stages]);

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
      const txt = deanonClient(result.anonymized_text, keptMapping);
      return new Blob([txt], { type: "text/plain;charset=utf-8" });
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
  const runDeanon = useCallback(async () => {
    setDeLoading(true);
    setDeError(null);
    setDeResult(null);
    try {
      let resp: Response;
      if (deUseLast) {
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
        if (!deFile) throw new Error("Загрузите обезличенный документ (.docx / .txt).");
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

  return (
    <div className="wrap">
      <header>
        <div className="row" style={{ justifyContent: "flex-end", marginBottom: 12 }}>
          {sessionStatus === "authenticated" && session?.user?.email ? (
            <div className="row" style={{ gap: 10 }}>
              <span className="note">{session.user.email}</span>
              <button className="ghost" onClick={() => signOut({ callbackUrl: "/" })}>
                Выйти
              </button>
            </div>
          ) : sessionStatus !== "loading" ? (
            <div className="row" style={{ gap: 10 }}>
              <Link className="ghost" href="/login">
                Войти
              </Link>
              <Link className="ghost" href="/register">
                Регистрация
              </Link>
            </div>
          ) : null}
        </div>
        <h1>🛡️ Анонимизатор персональных данных</h1>
        <p>Загрузите документ — получите обезличенную версию и ключ восстановления (mapping).</p>
      </header>

      <div className="tabs">
        <button className={`tab${tab === "anon" ? " active" : ""}`} onClick={() => setTab("anon")}>
          🔒 Анонимизация
        </button>
        <button className={`tab${tab === "deanon" ? " active" : ""}`} onClick={() => setTab("deanon")}>
          🔑 Деанонимизация
        </button>
      </div>

      {tab === "anon" && (
        <>
          <div className="card">
            <h2>1. Документ</h2>
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
              <div className="note">.docx, .doc, .pdf, .xlsx, .xls, .xml, .rtf, .odt, .txt (кроме презентаций)</div>
              {file && <div className="file-name">📄 {file.name}</div>}
            </div>
            <input
              ref={inputRef}
              type="file"
              accept=".docx,.doc,.pdf,.xlsx,.xls,.xlsm,.xml,.rtf,.odt,.txt,.csv,.md"
              style={{ display: "none" }}
              onChange={(e) => onPick(e.target.files?.[0])}
            />
          </div>

          <div className="card">
            <div
              className="card-toggle"
              role="button"
              tabIndex={0}
              aria-expanded={stagesOpen}
              onClick={() => setStagesOpen((v) => !v)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  setStagesOpen((v) => !v);
                }
              }}
            >
              <h2 style={{ margin: 0 }}>2. Экспериментальные настройки</h2>
              <span className={`chevron${stagesOpen ? " open" : ""}`}>▾</span>
            </div>
            {stagesOpen && (
              <>
                <div className="stages" style={{ marginTop: 14 }}>
                  {(Object.keys(STAGE_LABELS) as StageKey[]).map((k) => (
                    <label className="stage" key={k}>
                      <input
                        type="checkbox"
                        checked={stages[k]}
                        disabled={k === "subject" && !stages.llm}
                        onChange={() => toggle(k)}
                      />
                      {STAGE_LABELS[k]}
                    </label>
                  ))}
                </div>
                <p className="note" style={{ marginBottom: 0, marginTop: 12 }}>
                  Можно отключить любой слой — например, оставить только GLiNER.
                  LLM-проверка — финальный слой: пересматривает уже найденные
                  сущности и снимает маскирование с очевидных ошибок (обычные
                  слова, названия продуктов и т.п.); работает, только если
                  бэкенд запущен с флагом --review.
                </p>
                <p className="note" style={{ marginBottom: 0, marginTop: 8 }}>
                  Предмет договора — маскирует наименования товаров, работ и
                  услуг (модели, марки, номенклатуру), чтобы по ним нельзя было
                  восстановить отрасль; добавляется в тот же LLM-вызов без
                  доп. времени обработки. Включён по умолчанию, снимите галочку,
                  если предмет скрывать не нужно. Требует включённого слоя LLM —
                  недоступен, если LLM выключен.
                </p>
              </>
            )}
          </div>

          <div className="card">
            <div className="row">
              <button
                className="primary"
                disabled={!file || loading || !Object.values(stages).some(Boolean)}
                onClick={run}
              >
                {loading ? (
                  <>
                    <span className="spin" />
                    Обрабатываю…
                  </>
                ) : (
                  "🔒 Обезличить"
                )}
              </button>
              {loading && (
                <span className="note">
                  Запрос идёт на бэкенд (GLiNER + LLM){elapsed > 0 ? `, ${elapsed} с` : ""}. Это
                  может занять несколько минут — вкладку можно свернуть.
                </span>
              )}
            </div>
            {error && (
              <div className="error" style={{ marginTop: 14 }}>
                Ошибка: {error}
              </div>
            )}
            {cancelled && !error && (
              <div className="note" style={{ marginTop: 14 }}>
                Задача отменена.
              </div>
            )}
          </div>

          {result && (
            <>
              <div className="card">
                <h2>Результат</h2>
                {!!result.preexisting_placeholders && (
                  <div className="error" style={{ marginBottom: 14 }}>
                    ⚠️ В файле уже было {result.preexisting_placeholders} плейсхолдеров вида
                    [PERSON_1] — похоже, это уже обезличенный документ. Они защищены и не
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
                const failed = result.warnings!.filter((w) => w.value === undefined);
                return (
                  <>
                    {residual.length > 0 && (
                      <div className="card warn-card">
                        <h2 style={{ marginTop: 0 }}>⚠️ Проверьте вручную — возможно, не скрыто</h2>
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

                    {failed.length > 0 && (
                      <div className="card warn-card">
                        <h2 style={{ marginTop: 0 }}>⚠️ Часть документа проверена не полностью</h2>
                        <p className="note" style={{ marginTop: 0 }}>
                          Некоторые проверочные слои не смогли завершить работу — в этих местах
                          персональные данные могли остаться незамаскированными. Просмотрите их
                          вручную.
                        </p>
                        <ul style={{ margin: 0, paddingLeft: 20 }}>
                          {failed.map((w, i) => {
                            const range = warningRange(w.offset, w.chars);
                            return (
                              <li key={i} style={{ marginBottom: 10 }}>
                                <strong>{warningLabel(w.kind)}</strong>
                                {range && <span className="note"> ({range})</span>}
                                <div className="note" style={{ marginTop: 2 }}>{w.message}</div>
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    )}
                  </>
                );
              })()}

              <div className="card">
                <h2>📦 Скачать</h2>
                <div className="row">
                  <button className="ghost" onClick={downloadZip} disabled={docBusy}>
                    ⬇️ ZIP (документ + mapping)
                  </button>
                  <button className="ghost" onClick={downloadDoc} disabled={docBusy}>
                    ⬇️ {result.document_name}
                  </button>
                  <button className="ghost" onClick={downloadMapping}>
                    ⬇️ {stem}.map.json
                  </button>
                  {docBusy && <span className="note">Собираю документ…</span>}
                </div>
                <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                  ⚠️ Mapping — ключ восстановления. Храните его отдельно от обезличенного документа.
                </p>
              </div>

              <div className="card">
                <h2>Обезличенный текст</h2>
                <pre className="preview">{previewText}</pre>
              </div>

              <div className="card">
                <h2>Mapping ({entityCount})</h2>
                {kept.size > 0 && (
                  <p className="note" style={{ marginTop: 0 }}>
                    Возвращено в текст вручную: {kept.size}. Эти значения НЕ обезличены — они
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
                                  {isKept ? "↩︎ Вернуть маску" : "Оставить в тексте"}
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
                Последнего документа нет — загрузите обезличенный файл и маппинг вручную.
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
                  <strong>Обезличенный документ</strong>
                  <div className="note">.docx / .txt</div>
                  {deFile && <div className="file-name">📄 {deFile.name}</div>}
                </div>
                <div
                  className="drop"
                  style={{ flex: 1, minWidth: 220 }}
                  onClick={() => deMapRef.current?.click()}
                >
                  <strong>Маппинг</strong>
                  <div className="note">.json</div>
                  {deMapFile && <div className="file-name">🔑 {deMapFile.name}</div>}
                </div>
              </div>
              <input
                ref={deFileRef}
                type="file"
                accept=".docx,.txt"
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

          <div className="card">
            <div className="row">
              <button
                className="primary"
                disabled={deLoading || (!deUseLast && (!deFile || !deMapFile)) || (deUseLast && !result)}
                onClick={runDeanon}
              >
                {deLoading ? (
                  <>
                    <span className="spin" />
                    Восстанавливаю…
                  </>
                ) : (
                  "🔑 Восстановить"
                )}
              </button>
            </div>
            {deError && (
              <div className="error" style={{ marginTop: 14 }}>
                Ошибка: {deError}
              </div>
            )}
          </div>

          {deResult && (
            <>
              <div className="card">
                <h2>📦 Скачать</h2>
                <div className="row">
                  <button className="ghost" onClick={downloadRestored}>
                    ⬇️ {deResult.document_name}
                  </button>
                </div>
                {deResult.leftover.length > 0 ? (
                  <div className="error" style={{ marginTop: 12 }}>
                    Плейсхолдеры без значения в маппинге: {deResult.leftover.join(", ")}
                  </div>
                ) : (
                  <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                    ✅ Все плейсхолдеры восстановлены.
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
  );
}
