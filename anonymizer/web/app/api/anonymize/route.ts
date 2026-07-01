import { NextRequest, NextResponse } from "next/server";
import { callBackend, describeError } from "../_shared";

export const runtime = "nodejs";
export const maxDuration = 300; // long pipeline (GLiNER + LLM)

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

type Stages = Partial<Record<"regex" | "corporate" | "ner" | "llm" | "review", boolean>>;

/**
 * Proxy: browser uploads a file here; we base64-encode it and forward to the
 * Python backend's /anonymize-file, injecting the Bearer token server-side so
 * it never reaches the client. Uses callBackend (not fetch) to tolerate the
 * JupyterHub proxy's malformed multi-line CSP header.
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

    const resp = await callBackend(
      `${BACKEND_URL}/anonymize-file`,
      JSON.stringify(payload),
      BACKEND_KEY,
      290_000,
    );

    let data: unknown;
    try {
      data = JSON.parse(resp.text);
    } catch {
      data = { error: `Некорректный ответ бэкенда (HTTP ${resp.status}): ${resp.text.slice(0, 300)}` };
    }
    return NextResponse.json(data, { status: resp.status });
  } catch (e: unknown) {
    const msg = describeError(e, BACKEND_URL);
    console.error("[/api/anonymize] backend call failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
