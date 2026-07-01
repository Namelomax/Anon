import { NextRequest, NextResponse } from "next/server";
import { describeError } from "../_shared";

export const runtime = "nodejs";
export const maxDuration = 120;

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

/**
 * Proxy for deanonymization. Accepts either:
 *  - multipart form: file (anonymized doc) + mapping (JSON string), or
 *  - JSON: { filename, file_base64, mapping }  (used by "восстановить последний")
 * Forwards to the Python backend's /deanonymize-file with the Bearer token
 * injected server-side.
 */
export async function POST(req: NextRequest) {
  try {
    let payload: { filename: string; file_base64: string; mapping: unknown };

    const ctype = req.headers.get("content-type") || "";
    if (ctype.includes("application/json")) {
      payload = await req.json();
    } else {
      const form = await req.formData();
      const file = form.get("file");
      if (!(file instanceof File)) {
        return NextResponse.json({ error: "Файл не получен" }, { status: 400 });
      }
      let mapping: unknown = {};
      const rawMap = form.get("mapping");
      if (typeof rawMap === "string") {
        try {
          mapping = JSON.parse(rawMap);
        } catch {
          return NextResponse.json({ error: "Некорректный JSON маппинга" }, { status: 400 });
        }
      }
      const buf = Buffer.from(await file.arrayBuffer());
      payload = { filename: file.name, file_base64: buf.toString("base64"), mapping };
    }

    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (BACKEND_KEY) headers.Authorization = `Bearer ${BACKEND_KEY}`;

    const resp = await fetch(`${BACKEND_URL}/deanonymize-file`, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });

    const data = await resp.json().catch(() => ({ error: "Некорректный ответ бэкенда" }));
    return NextResponse.json(data, { status: resp.status });
  } catch (e: unknown) {
    const msg = describeError(e, BACKEND_URL);
    console.error("[/api/deanonymize] backend fetch failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
