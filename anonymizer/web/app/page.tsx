"use client";

import JSZip from "jszip";
import { useCallback, useMemo, useRef, useState } from "react";

type StageKey = "regex" | "corporate" | "ner" | "llm" | "review";

type AnonResult = {
  filename: string;
  is_docx: boolean;
  anonymized_text: string;
  mapping: Record<string, string>;
  summary: Record<string, number>;
  elapsed_seconds?: number;
  preexisting_placeholders?: number;
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
};

function base64ToBuffer(b64: string): ArrayBuffer {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
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

export default function Home() {
  const [tab, setTab] = useState<"anon" | "deanon">("anon");

  // --- Anonymize state ---
  const [file, setFile] = useState<File | null>(null);
  const [stages, setStages] = useState<Record<StageKey, boolean>>({
    regex: true,
    corporate: true,
    ner: true,
    llm: true,
    review: true,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AnonResult | null>(null);
  const [drag, setDrag] = useState(false);
  const [stagesOpen, setStagesOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

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

  const onPick = (f: File | null | undefined) => {
    if (!f) return;
    if (!/\.(docx|txt)$/i.test(f.name)) {
      setError("Поддерживаются только файлы .docx и .txt");
      return;
    }
    setError(null);
    setResult(null);
    setFile(f);
  };

  const toggle = (k: StageKey) => setStages((s) => ({ ...s, [k]: !s[k] }));

  const run = useCallback(async () => {
    if (!file) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("stages", JSON.stringify(stages));
      const resp = await fetch("/api/anonymize", { method: "POST", body: fd });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setResult(data as AnonResult);
      setDeUseLast(true);
      setDeResult(null);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [file, stages]);

  const mappingJson = useMemo(
    () => (result ? JSON.stringify(result.mapping, null, 2) : ""),
    [result],
  );

  const downloadDoc = () => {
    if (!result) return;
    download(
      new Blob([base64ToBuffer(result.document_base64)], { type: result.document_mime }),
      result.document_name,
    );
  };
  const downloadMapping = () => {
    if (!result) return;
    download(new Blob([mappingJson], { type: "application/json" }), `${stem}.map.json`);
  };
  const downloadZip = async () => {
    if (!result) return;
    const zip = new JSZip();
    zip.file(result.document_name, base64ToBuffer(result.document_base64));
    zip.file(`${stem}.map.json`, mappingJson);
    const blob = await zip.generateAsync({ type: "blob" });
    download(blob, `${stem}_anonymized.zip`);
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
              <div className="note">Поддерживаются .docx и .txt</div>
              {file && <div className="file-name">📄 {file.name}</div>}
            </div>
            <input
              ref={inputRef}
              type="file"
              accept=".docx,.txt"
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
                      <input type="checkbox" checked={stages[k]} onChange={() => toggle(k)} />
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
              {loading && <span className="note">Запрос идёт на бэкенд (GLiNER + LLM). Это может занять время.</span>}
            </div>
            {error && (
              <div className="error" style={{ marginTop: 14 }}>
                Ошибка: {error}
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

              <div className="card">
                <h2>📦 Скачать</h2>
                <div className="row">
                  <button className="ghost" onClick={downloadZip}>
                    ⬇️ ZIP (документ + mapping)
                  </button>
                  <button className="ghost" onClick={downloadDoc}>
                    ⬇️ {result.document_name}
                  </button>
                  <button className="ghost" onClick={downloadMapping}>
                    ⬇️ {stem}.map.json
                  </button>
                </div>
                <p className="note" style={{ marginTop: 12, marginBottom: 0 }}>
                  ⚠️ Mapping — ключ восстановления. Храните его отдельно от обезличенного документа.
                </p>
              </div>

              <div className="card">
                <h2>Обезличенный текст</h2>
                <pre className="preview">{result.anonymized_text}</pre>
              </div>

              <div className="card">
                <h2>Mapping ({entityCount})</h2>
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
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(result.mapping).map(([ph, orig]) => (
                          <tr key={ph}>
                            <td>
                              <code>{ph}</code>
                            </td>
                            <td>
                              <span className="tag">{labelOf(ph)}</span>
                            </td>
                            <td>{orig}</td>
                          </tr>
                        ))}
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
