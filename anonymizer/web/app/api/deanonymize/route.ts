import { NextRequest, NextResponse } from "next/server";
import { callBackend, describeError } from "../_shared";

export const runtime = "nodejs";
export const maxDuration = 120;

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

/**
 * Proxy for deanonymization. Accepts either:
 *  - multipart form: file (anonymized doc) + mapping (JSON string), or
 *  - JSON: { filename, file_base64, mapping }  (used by "восстановить последний")
 * Uses callBackend (not fetch) to tolerate the JupyterHub proxy's malformed CSP.
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

    const resp = await callBackend(
      `${BACKEND_URL}/deanonymize-file`,
      JSON.stringify(payload),
      BACKEND_KEY,
      110_000,
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
    console.error("[/api/deanonymize] backend call failed:", msg, e);
    return NextResponse.json({ error: msg }, { status: 502 });
  }
}
