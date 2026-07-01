import { NextRequest, NextResponse } from "next/server";
import { describeError } from "../_shared";

export const runtime = "nodejs";
export const maxDuration = 300; // long pipeline (GLiNER + LLM)

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

type Stages = Partial<Record<"regex" | "corporate" | "ner" | "llm", boolean>>;

/**
 * Proxy: browser uploads a file here; we base64-encode it and forward to the
 * Python backend's /anonymize-file, injecting the Bearer token server-side so
 * it never reaches the client. This also avoids CORS and mixed-content issues
 * when the Vercel UI talks to a remote (JupyterHub) backend.
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
    const payload = {
      filename: file.name,
      file_base64: buf.toString("base64"),
      ...stages,
    };

    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (BACKEND_KEY) headers.Authorization = `Bearer ${BACKEND_KEY}`;

    const resp = await fetch(`${BACKEND_URL}/anonymize-file`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });

    const data = await resp.json().catch(() => ({ error: "Некорректный ответ бэкенда" }));
    return NextResponse.json(data, { status: resp.status });
  } catch (e: unknown) {
    const msg = describeError(e, BACKEND_URL);
    console.error("[/api/anonymize] backend fetch failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
