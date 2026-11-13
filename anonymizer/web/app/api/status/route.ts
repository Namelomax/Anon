import { NextResponse } from "next/server";
import { probeBackend, describeError } from "../_shared";

export const runtime = "nodejs";

const BACKEND_URL =
  process.env.ANONYMIZER_BACKEND_URL?.replace(/\/$/, "") || "http://127.0.0.1:8000";
const BACKEND_KEY = process.env.ANONYMIZER_BACKEND_KEY || "";

// Короткий: это опрос для баннера «серверы обработки недоступны», он не должен
// сам по себе висеть дольше пары секунд. Ниже роут всё равно отвечает 200.
const _PROBE_TIMEOUT_MS = 5_000;

/**
 * GET — состояние Python-бэкенда для страницы обработки.
 *
 * Всегда отвечает 200: сигнал «бэкенд лежит» передаётся полем ok=false —
 * иначе фронт не отличил бы недоступный бэкенд от недоступного самого роута.
 * ok: true  — бэкенд ответил; ready — завершён ли прогрев моделей;
 *             ner/llm — доступность удалённых слоёв (null = не применимо);
 * ok: false — бэкенд не ответил за таймаут или ответил не 200.
 */
export async function GET() {
  try {
    const resp = await probeBackend(`${BACKEND_URL}/health`, BACKEND_KEY, _PROBE_TIMEOUT_MS);
    if (resp.status !== 200) {
      return NextResponse.json(
        { ok: false, error: `Бэкенд ответил HTTP ${resp.status}` },
        { status: 200 },
      );
    }
    let data: {
      ready?: boolean;
      remote?: { ner?: boolean | null; llm?: boolean | null };
      warmup_error?: string | null;
    } | null = null;
    try {
      data = JSON.parse(resp.text);
    } catch {
      return NextResponse.json(
        { ok: false, error: "Некорректный ответ бэкенда /health (не JSON)" },
        { status: 200 },
      );
    }
    return NextResponse.json(
      {
        ok: true,
        ready: data?.ready !== false,
        ner: data?.remote?.ner ?? null,
        llm: data?.remote?.llm ?? null,
        warmupError: data?.warmup_error ?? null,
      },
      { status: 200 },
    );
  } catch (e: unknown) {
    // Бэкенд недоступен (connect timeout/refused/DNS) — это штатное состояние
    // для баннера, а не ошибка роута: детали только в серверный лог.
    console.error("[/api/status] backend probe failed:", describeError(e, BACKEND_URL));
    return NextResponse.json({ ok: false }, { status: 200 });
  }
}
