"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import type { BankTransferQuote } from "@/lib/bank-transfer-types";
import styles from "./bank-transfer.module.css";

type Props = { plan: string; months: 1 | 12; canManage: boolean };
type Result = { key: string; quote?: BankTransferQuote; error?: string };

export default function BankTransferPanel({ plan, months, canManage }: Props) {
  const [result, setResult] = useState<Result | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [copyStatus, setCopyStatus] = useState<"copied" | "error" | null>(null);
  const key = `${plan}:${months}:${attempt}`;

  useEffect(() => {
    setCopyStatus(null);
    if (!canManage) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ plan, months: String(months) });
    async function load() {
      try {
        const response = await fetch(`/api/billing/bank-details?${params}`, { cache: "no-store", signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body?.error === "string" ? body.error : "Не удалось загрузить реквизиты.");
        if (!body || typeof body.configured !== "boolean" || !body.selection || body.selection.plan !== plan || body.selection.months !== months) {
          throw new Error("Не удалось загрузить реквизиты.");
        }
        if (!controller.signal.aborted) setResult({ key, quote: body as BankTransferQuote });
      } catch (error) {
        if (!controller.signal.aborted) setResult({ key, error: error instanceof Error ? error.message : "Не удалось загрузить реквизиты." });
      }
    }
    void load();
    return () => controller.abort();
  }, [plan, months, canManage, attempt, key]);

  async function copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("error");
    }
  }

  if (!canManage) return <p className={styles.notice}>Для оплаты обратитесь к администратору аккаунта.</p>;
  if (!result || result.key !== key) return <p className={styles.notice} role="status">Загружаем банковские реквизиты…</p>;
  if (result.error || !result.quote) return <div className={styles.notice}><p role="alert">{result.error || "Не удалось загрузить реквизиты."}</p><button className={styles.button} type="button" onClick={() => setAttempt((value) => value + 1)}>Повторить</button></div>;

  const quote = result.quote;
  if (!quote.configured) return <div className={styles.notice}><p><strong>Банковские реквизиты пока не настроены.</strong></p><p>Выберите <Link href="/billing">онлайн-оплату</Link> или обратитесь в поддержку: <a href={`mailto:${quote.supportEmail}`}>{quote.supportEmail}</a>.</p></div>;

  const amount = new Intl.NumberFormat("ru-RU", { style: "currency", currency: "RUB" }).format(quote.selection.amountKopecks / 100);
  const fields = [
    ["Получатель", quote.recipient.name], ["ИНН", quote.recipient.inn],
    ["ОГРНИП", quote.recipient.ogrnip], ["Юридический адрес", quote.recipient.address],
    ["Банк", quote.recipient.bankName],
    ...(quote.recipient.bankInn ? [["ИНН банка", quote.recipient.bankInn]] : []),
    ...(quote.recipient.bankAddress ? [["Адрес банка", quote.recipient.bankAddress]] : []),
    ["БИК", quote.recipient.bic], ["Расчётный счёт", quote.recipient.account],
    ["Корреспондентский счёт", quote.recipient.correspondentAccount], ["Назначение платежа", quote.purpose],
  ];
  const all = [["Тариф", quote.selection.title], ["Период", months === 12 ? "12 месяцев" : "1 месяц"], ["Сумма", amount], ...fields].map(([label, value]) => `${label}: ${value}`).join("\n");

  return <section className={styles.panel} aria-label="Банковские реквизиты">
    <div className={styles.summary}><div><span>Тариф</span><strong>{quote.selection.title}</strong></div><div><span>Период</span><strong>{months === 12 ? "12 месяцев" : "1 месяц"}</strong></div><div><span>Сумма</span><strong>{amount}</strong></div></div>
    <dl className={styles.fields}>{fields.map(([label, value]) => <div className={styles.row} key={label}><dt>{label}</dt><dd><span>{value}</span><button className={styles.copy} type="button" onClick={() => void copy(value)} aria-label={`Скопировать: ${label}`}>Копировать</button></dd></div>)}</dl>
    <button className={styles.button} type="button" onClick={() => void copy(all)}>Скопировать всё</button>
    {copyStatus === "copied" && <p className={styles.feedback} role="status" aria-live="polite">Скопировано</p>}
    {copyStatus === "error" && <p className={styles.feedback} role="alert">Не удалось скопировать. Выделите нужный текст и скопируйте его вручную.</p>}
    <p className={styles.explanation}>Подписка активируется после подтверждения поступления перевода. Проверка перевода выполняется вручную.</p>
  </section>;
}
