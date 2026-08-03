import { NextRequest, NextResponse } from "next/server";
import { callBackend, callBackendGet, describeError } from "../_shared";
import {
  MAX_UPLOAD_BYTES,
  PLATFORM_BODY_LIMIT_BYTES,
  explainUploadLimit,
  formatBytes,
} from "../../limits";

export const runtime = "nodejs";
// Ни один запрос сюда не длится дольше нескольких секунд: POST только СТАВИТ
// задачу на бэкенде и сразу возвращает job_id, GET?jobId=... делает ОДИН опрос
// статуса. Ждать больше нечего.
//
// Раньше здесь стояло 900 с и весь конвейер ждался внутри одной инвокации.
// Из-за этого, во-первых, деплой падал («invalid maxDuration for plan» — на
// Hobby потолок 300 с), а во-вторых, лимит платформы становился лимитом самой
// анонимизации. Теперь опрос ведёт браузер, и время работы конвейера не
// ограничено ничем: задача может считаться пять минут или пятнадцать.
// 280 — с запасом под потолок тарифа Hobby (300 с). Фактически столько не
// нужно: и submit, и опрос отвечают за миллисекунды. Это страховка на случай
// медленного одиночного обращения к бэкенду через прокси.
export const maxDuration = 280;

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

type Stages = Partial<
  Record<"regex" | "corporate" | "ner" | "llm" | "review" | "subject", boolean>
>;

// Таймаут одного обращения к бэкенду: постановки задачи и каждого опроса.
// Достаточно, чтобы бэкенд успел ответить через прокси, и достаточно мало,
// чтобы застрявший запрос не съел всю инвокацию.
const _REQUEST_TIMEOUT_MS = 45_000;

/**
 * POST — принять файл и ПОСТАВИТЬ задачу на бэкенде.
 *
 * Браузер шлёт multipart, мы перекодируем файл в base64 и отправляем на
 * POST /jobs/anonymize-file, получая в ответ job_id. Дальше клиент сам
 * опрашивает GET /api/anonymize?jobId=... Такое разделение нужно из-за двух
 * независимых потолков, каждый из которых режет ОДИН запрос, а не общую работу:
 * релей dev tunnel обрывает соединение примерно через 100 с, а Vercel — по
 * maxDuration инвокации. Короткие submit и poll не задевают ни тот, ни другой.
 *
 * Bearer-токен подставляется на сервере и до клиента не доходит. Обращения идут
 * через callBackend/callBackendGet, а не через fetch: прокси JupyterHub шлёт
 * многострочный заголовок CSP, который undici отвергает как невалидный.
 */
export async function POST(req: NextRequest) {
  try {
    const form = await req.formData();
    const file = form.get("file");
    if (!(file instanceof File)) {
      return NextResponse.json({ error: "Файл не получен" }, { status: 400 });
    }

    // Вторая линия обороны после проверки в браузере: сюда можно прийти и в
    // обход формы (curl, старая вкладка). Отдаём ЧЕСТНЫЙ JSON с 413 — платформа
    // на своём пороге отвечает обычным текстом, который клиент не разберёт.
    if (file.size > MAX_UPLOAD_BYTES) {
      return NextResponse.json({ error: explainUploadLimit(file.size) }, { status: 413 });
    }

    let stages: Stages = {};
    const rawStages = form.get("stages");
    if (typeof rawStages === "string" && rawStages) {
      try {
        stages = JSON.parse(rawStages) as Stages;
      } catch {
        /* ignore malformed stages; backend falls back to defaults */
      }
    }

    const buf = Buffer.from(await file.arrayBuffer());
    const payload = { filename: file.name, file_base64: buf.toString("base64"), ...stages };

    const submitResp = await callBackend(
      `${BACKEND_URL}/jobs/anonymize-file`,
      JSON.stringify(payload),
      BACKEND_KEY,
      _REQUEST_TIMEOUT_MS,
    );

    if (submitResp.status !== 202) {
      let data: unknown;
      try {
        data = JSON.parse(submitResp.text);
      } catch {
        data = {
          error: `Некорректный ответ бэкенда (HTTP ${submitResp.status}): ${submitResp.text.slice(0, 300)}`,
        };
      }
      return NextResponse.json(data, { status: submitResp.status });
    }

    try {
      const submitData = JSON.parse(submitResp.text) as { job_id?: string };
      if (!submitData.job_id) throw new Error("no job_id in response");
      return NextResponse.json({ jobId: submitData.job_id, done: false }, { status: 202 });
    } catch {
      return NextResponse.json(
        {
          error: `Некорректный ответ бэкенда при постановке задачи: ${submitResp.text.slice(0, 300)}`,
        },
        { status: 502 },
      );
    }
  } catch (e: unknown) {
    const msg = describeError(e, BACKEND_URL);
    console.error("[/api/anonymize] submit failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}

/**
 * GET ?jobId=... — ОДИН опрос статуса задачи.
 *
 * Пока не готово, отдаём 200 {done:false}: браузер отличает «ещё считается» по
 * полю, и промежуточный статус не путается с ошибкой.
 */
export async function GET(req: NextRequest) {
  const jobId = new URL(req.url).searchParams.get("jobId");
  if (!jobId) {
    return NextResponse.json({ error: "jobId обязателен" }, { status: 400 });
  }

  try {
    const pollResp = await callBackendGet(
      `${BACKEND_URL}/jobs/${jobId}`,
      BACKEND_KEY,
      _REQUEST_TIMEOUT_MS,
    );

    let pollData: { status?: string; result?: unknown; error?: string | null };
    try {
      pollData = JSON.parse(pollResp.text);
    } catch {
      return NextResponse.json(
        {
          error: `Некорректный ответ бэкенда (HTTP ${pollResp.status}): ${pollResp.text.slice(0, 300)}`,
        },
        { status: pollResp.status >= 400 ? pollResp.status : 502 },
      );
    }

    if (pollResp.status === 404) {
      return NextResponse.json(
        { error: pollData.error || `Бэкенд забыл задачу ${jobId} (перезапуск или истёк TTL)` },
        { status: 404 },
      );
    }
    if (pollData.status === "error") {
      return NextResponse.json({ error: pollData.error || "unknown error" }, { status: 500 });
    }
    if (pollData.status !== "done") {
      return NextResponse.json({ done: false }, { status: 200 });
    }

    // Ограничение платформы действует и на ОТВЕТ. В результате лежит
    // document_base64 — готовый .docx, снова раздутый base64 на треть, так что
    // ответ бывает тяжелее запроса. Если он не пролезает, платформа оборвёт его
    // на полуслове и клиент получит обрывок вместо JSON; лучше отдать текст и
    // мапинг, а бинарник честно исключить.
    const body = JSON.stringify(pollData.result);
    if (Buffer.byteLength(body) > PLATFORM_BODY_LIMIT_BYTES) {
      const result = (pollData.result ?? {}) as Record<string, unknown>;
      const { document_base64: _dropped, ...withoutDoc } = result;
      if (Buffer.byteLength(JSON.stringify(withoutDoc)) <= PLATFORM_BODY_LIMIT_BYTES) {
        return NextResponse.json(
          {
            done: true,
            ...withoutDoc,
            document_base64: "",
            document_too_large: true,
            warning:
              `Готовый документ (${formatBytes(Buffer.byteLength(body))}) не помещается в ` +
              `ответ веб-приложения. Текст и таблица замен полные, но скачивание файла ` +
              `недоступно — заберите его с бэкенда напрямую.`,
          },
          { status: 200 },
        );
      }
      return NextResponse.json(
        {
          error:
            `Результат (${formatBytes(Buffer.byteLength(body))}) не помещается в ответ ` +
            `веб-приложения. Разбейте документ на части или работайте с бэкендом напрямую.`,
        },
        { status: 413 },
      );
    }

    return NextResponse.json(
      { done: true, ...(pollData.result as Record<string, unknown>) },
      { status: 200 },
    );
  } catch (e: unknown) {
    const msg = describeError(e, BACKEND_URL);
    console.error("[/api/anonymize] poll failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
