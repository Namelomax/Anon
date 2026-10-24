"use client";

import { CircleCheckBig, CircleX, LoaderCircle, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

/**
 * Возврат с платёжной формы Т-Банка (SuccessURL/FailURL из
 * /api/billing/checkout): крупно «оплата прошла» или «отклонена», через
 * REDIRECT_SECONDS — в кабинет.
 *
 * Сам переход на SuccessURL ничего не доказывает (его может открыть кто
 * угодно), поэтому «прошла» показывается только когда сервер подтвердил, что
 * уведомление банка пришло и тариф включён. Уведомление может отстать от
 * редиректа на секунды — пока его нет, опрашиваем /api/billing/payment.
 */

const REDIRECT_SECONDS = 5;
const POLL_MS = 1500;
/** Сколько ждать уведомления банка, прежде чем сказать «подтверждение задерживается». */
const WAIT_MS = 30_000;

/** Конечные статусы неудачи (справочник статусов Т-Банка). */
const FAILED = new Set(["REJECTED", "AUTH_FAIL", "DEADLINE_EXPIRED", "CANCELED", "ATTEMPTS_EXPIRED", "AMOUNT_MISMATCH"]);

type PaymentState = {
  status: string;
  activated: boolean;
  planTitle: string;
  months: number;
  amountKopecks: number;
  accessUntil: string | null;
};

type View =
  | { kind: "waiting" }
  | { kind: "success"; p: PaymentState }
  | { kind: "failed" }
  | { kind: "delayed" }
  | { kind: "error"; message: string };

function dateRu(iso: string): string {
  return new Date(iso).toLocaleDateString("ru", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    timeZone: "UTC",
  });
}

function rubles(kopecks: number): string {
  return (kopecks / 100).toLocaleString("ru", { maximumFractionDigits: 2 });
}

export default function PaymentResultPage() {
  const [view, setView] = useState<View>({ kind: "waiting" });
  const [left, setLeft] = useState(REDIRECT_SECONDS);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const order = params.get("order") ?? "";
    const bankSaidFail = params.get("fail") === "1";
    const started = Date.now();
    let timer: number | undefined;
    let stopped = false;

    const poll = async () => {
      try {
        const resp = await fetch(`/api/billing/payment?order=${encodeURIComponent(order)}`, {
          cache: "no-store",
        });
        const data = await resp.json().catch(() => ({}));
        if (stopped) return;
        if (!resp.ok) {
          setView({ kind: "error", message: data?.error || `HTTP ${resp.status}` });
          return;
        }
        const p = data as PaymentState;
        if (p.activated) return setView({ kind: "success", p });
        // FailURL — банк сам сказал «не прошло»; ждать уведомления незачем.
        if (bankSaidFail || FAILED.has(p.status)) return setView({ kind: "failed" });
        if (Date.now() - started > WAIT_MS) return setView({ kind: "delayed" });
      } catch {
        if (stopped) return;
        if (Date.now() - started > WAIT_MS) return setView({ kind: "delayed" });
      }
      timer = window.setTimeout(poll, POLL_MS);
    };
    void poll();
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, []);

  const target = view.kind === "success" ? "/?payment=success" : view.kind === "failed" ? "/?payment=fail" : "/";
  const final = view.kind !== "waiting";

  useEffect(() => {
    if (!final) return;
    if (left <= 0) {
      window.location.assign(target);
      return;
    }
    const t = window.setTimeout(() => setLeft((s) => s - 1), 1000);
    return () => window.clearTimeout(t);
  }, [final, left, target]);

  return (
    <div className="wrap" style={{ maxWidth: 480 }}>
      <header>
        <h1>Оплата тарифа</h1>
      </header>
      <div className="card" style={{ textAlign: "center", padding: "32px 24px" }}>
        {view.kind === "waiting" && (
          <>
            <LoaderCircle size={56} className="spin" />
            <h2 style={{ justifyContent: "center" }}>Ждём подтверждения банка…</h2>
            <p className="note">Обычно это занимает несколько секунд.</p>
          </>
        )}
        {view.kind === "success" && (
          <>
            <CircleCheckBig size={56} color="#16a34a" />
            <h2 style={{ justifyContent: "center" }}>Оплата прошла</h2>
            <p>
              Тариф «{view.p.planTitle}» на {view.p.months === 12 ? "12 месяцев" : "1 месяц"} —{" "}
              {rubles(view.p.amountKopecks)} ₽.
              {view.p.accessUntil && <> Действует до {dateRu(view.p.accessUntil)}.</>}
            </p>
          </>
        )}
        {view.kind === "failed" && (
          <>
            <CircleX size={56} color="#dc2626" />
            <h2 style={{ justifyContent: "center" }}>Платёж отклонён</h2>
            <p>Оплата не прошла или была отменена. Деньги не списаны — можно попробовать ещё раз.</p>
          </>
        )}
        {view.kind === "delayed" && (
          <>
            <TriangleAlert size={56} />
            <h2 style={{ justifyContent: "center" }}>Банк ещё не подтвердил оплату</h2>
            <p>
              Если деньги списаны, тариф включится автоматически, как только придёт подтверждение.
              Состояние видно в кабинете, в разделе «Платежи».
            </p>
          </>
        )}
        {view.kind === "error" && (
          <>
            <TriangleAlert size={56} />
            <h2 style={{ justifyContent: "center" }}>Не удалось узнать состояние платежа</h2>
            <div className="error">{view.message}</div>
          </>
        )}
        {final && (
          <p className="note" style={{ marginBottom: 0 }}>
            Возврат в кабинет через {left} с…{" "}
            <a href={target}>Перейти сейчас</a>
          </p>
        )}
      </div>
    </div>
  );
}
