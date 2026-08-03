import { NextRequest, NextResponse } from "next/server";
import { callBackend, callBackendGet, describeError } from "../_shared";

export const runtime = "nodejs";
export const maxDuration = 300; // long pipeline (GLiNER + LLM)

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

type Stages = Partial<
  Record<"regex" | "corporate" | "ner" | "llm" | "review" | "subject", boolean>
>;

// Per-request timeout for the submit call and each status poll: short and
// well under the devtunnel relay's ~100s ceiling, so a single stuck request
// never eats the whole budget.
const _REQUEST_TIMEOUT_MS = 30_000;
// Interval between status polls.
const _POLL_INTERVAL_MS = 2_000;
// Total time budget for submit + polling, kept under maxDuration (300s).
const _TOTAL_BUDGET_MS = 280_000;

function _sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Proxy: browser uploads a file here; we base64-encode it and forward to the
 * Python backend's async job API — POST /jobs/anonymize-file, then poll
 * GET /jobs/<job_id> every 2s until it's done. The devtunnel relay in front
 * of the backend 504s any single request after ~100s, and the anonymization
 * pipeline routinely takes longer than that; splitting into a fast submit
 * plus many fast polls keeps every individual request well under the limit.
 * The JSON returned to the browser is identical to the old synchronous
 * /anonymize-file response, so the client needs no changes. Injects the
 * Bearer token server-side so it never reaches the client. Uses callBackend/
 * callBackendGet (not fetch) to tolerate the JupyterHub proxy's malformed
 * multi-line CSP header.
 */
export async function POST(req: NextRequest) {
  try {
    const form = await req.formData();
    const file = form.get("file");
    if (!(file instanceof File)) {
      return NextResponse.json({ error: "Файл не получен" }, { status: 400 });
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

    let jobId: string;
    try {
      const submitData = JSON.parse(submitResp.text) as { job_id?: string };
      if (!submitData.job_id) throw new Error("no job_id in response");
      jobId = submitData.job_id;
    } catch {
      return NextResponse.json(
        { error: `Некорректный ответ бэкенда при постановке задачи: ${submitResp.text.slice(0, 300)}` },
        { status: 502 },
      );
    }

    const deadline = Date.now() + _TOTAL_BUDGET_MS;
    while (Date.now() < deadline) {
      await _sleep(_POLL_INTERVAL_MS);

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
          { status: pollResp.status },
        );
      }

      if (pollResp.status === 404) {
        return NextResponse.json({ error: pollData.error || "unknown job" }, { status: 404 });
      }
      if (pollData.status === "done") {
        return NextResponse.json(pollData.result, { status: 200 });
      }
      if (pollData.status === "error") {
        return NextResponse.json({ error: pollData.error || "unknown error" }, { status: 500 });
      }
      // "pending" / "running" — keep polling.
    }

    return NextResponse.json(
      { error: "Превышено время ожидания ответа бэкенда (обработка так и не завершилась)" },
      { status: 504 },
    );
  } catch (e: unknown) {
    const msg = describeError(e, BACKEND_URL);
    console.error("[/api/anonymize] backend call failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
