"use client";

import Link from "next/link";
import { useSession } from "next-auth/react";
import { useLoginRedirect } from "@/app/_components/useLoginRedirect";
import { useEffect, useState } from "react";
import type { AccountSummary } from "@/lib/account-summary";
import BankTransferPanel from "./BankTransferPanel";
import styles from "./account-billing.module.css";

const date = (value: string) => new Date(value).toLocaleDateString("ru-RU", { timeZone: "UTC" });
const pages = (value: number) => (value / 10).toLocaleString("ru-RU");
const money = (value: number) => `${(value / 100).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
const period = (value: number) => value === 1 ? "1 месяц" : value === 12 ? "12 месяцев" : `${value} мес.`;
function paymentStatus(status: string) {
  if (status === "CONFIRMED") return "Оплачен";
  if (["NEW", "FORM_SHOWED", "AUTHORIZING", "AUTHORIZED", "3DS_CHECKING", "3DS_CHECKED"].includes(status)) return "Ожидает оплаты";
  if (["REFUNDED", "REVERSED"].includes(status)) return "Возвращён";
  if (["PARTIAL_REFUNDED", "PARTIAL_REVERSED"].includes(status)) return "Частичный возврат";
  if (["REJECTED", "DEADLINE_EXPIRED", "CANCELED"].includes(status)) return "Не оплачен";
  return "Уточняется";
}

export default function AccountBilling({ section }: { section: "subscription" | "payments" }) {
  const { data: session, status } = useSession();
  useLoginRedirect(status);
  const identity = session?.user?.id ?? session?.user?.email ?? null;
  const [account, setAccount] = useState<AccountSummary | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [plan, setPlan] = useState("");
  const [months, setMonths] = useState<1 | 12>(1);
  const summary = status === "authenticated" && identity && loadedFor === identity ? account : null;

  useEffect(() => {
    setAccount(null); setLoadedFor(null); setError(null); setPlan(""); setMonths(1);
    if (status !== "authenticated" || !identity) { setLoading(status === "loading"); return; }
    const controller = new AbortController();
    setLoading(true);
    fetch("/api/account", { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (response.status === 401) throw new Error("Сессия завершилась. Войдите в аккаунт снова.");
        if (!response.ok) throw new Error("Не удалось загрузить данные аккаунта.");
        return response.json() as Promise<AccountSummary>;
      })
      .then((data) => {
        if (controller.signal.aborted) return;
        const query = new URLSearchParams(window.location.search);
        const requested = data.offers.find((offer) => offer.code === query.get("plan"));
        const selected = requested ?? data.offers.find((offer) => offer.code === data.plan.code) ?? data.offers[0];
        setPlan(selected?.code ?? "");
        setMonths(query.get("months") === "12" && selected?.priceYearKopecks != null ? 12 : 1);
        setAccount(data); setLoadedFor(identity);
      })
      .catch((err: unknown) => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : "Не удалось загрузить данные аккаунта."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [status, identity, attempt]);

  useEffect(() => {
    if (summary && section === "payments" && window.location.hash === "#transfer") {
      requestAnimationFrame(() => document.getElementById("transfer")?.scrollIntoView());
    }
  }, [summary, section]);

  const selected = summary?.offers.find((offer) => offer.code === plan);
  const quota = summary?.quota;
  const progress = quota?.allowanceTenths ? Math.min(100, Math.max(0, quota.usedTenths / quota.allowanceTenths * 100)) : 0;
  return <div className={styles.shell}>
    <aside className={styles.sidebar}>
      <Link href="/" className={styles.back}>← К документам</Link>
      <p className={styles.navTitle}>Аккаунт</p>
      <nav className={styles.nav} aria-label="Аккаунт">
        <Link href="/account/subscription" aria-current={section === "subscription" ? "page" : undefined}>Моя подписка</Link>
        <Link href="/account/payments" aria-current={section === "payments" ? "page" : undefined}>Платежи и реквизиты</Link>
        <Link href="/account/developers">API</Link>
      </nav>
      <Link href="/?tab=account" className={styles.settings}>Настройки аккаунта</Link>
    </aside>
    <main className={styles.main}>
      <div className={styles.content}>
        <h1>{section === "subscription" ? "Моя подписка" : "Платежи и реквизиты"}</h1>
        {(status === "loading" || (status === "authenticated" && loading)) && <p className={styles.notice} role="status">Загружаем данные аккаунта…</p>}
        {status === "unauthenticated" && <p className={styles.notice}>Войдите в аккаунт, чтобы посмотреть данные. <Link href="/login">Войти</Link></p>}
        {status === "authenticated" && !identity && <p className={styles.notice}>Не удалось определить аккаунт. Войдите снова.</p>}
        {status === "authenticated" && error && <p className={styles.notice} role="alert">{error} <button className={styles.textButton} onClick={() => setAttempt((n) => n + 1)}>Повторить</button></p>}
        {summary && <>
          <section className={styles.card} aria-labelledby="current-plan">
            <div className={styles.cardHeader}><div><p className={styles.eyebrow}>Текущий тариф</p><h2 id="current-plan">{summary.plan.title}</h2></div><span className={styles.badge}>{summary.quota.expired ? "Срок истёк" : "Действует"}</span></div>
            <p className={styles.muted}>{summary.quota.paidUntil ? `${summary.quota.expired ? "Срок истёк" : "Действует"} до ${date(summary.quota.paidUntil)}` : "Без установленного срока"}</p>
            <Link href="/billing" className={styles.primary}>{summary.quota.expired || summary.plan.priceKopecks === 0 ? "Выбрать тариф" : "Продлить"}</Link>
          </section>
          {section === "subscription" ? <>
            <section className={styles.card} aria-labelledby="quota-title">
              <h2 id="quota-title">Использование страниц</h2>
              <p className={styles.muted}>Период с {date(summary.quota.periodStart)}{summary.quota.periodEnd ? ` по ${date(summary.quota.periodEnd)}` : ""}</p>
              <div className={styles.quotaValue}>{pages(summary.quota.usedTenths)} <span>из {summary.quota.unlimited ? "неограниченного количества" : `${pages(summary.quota.allowanceTenths ?? 0)} стр.`}</span></div>
              {!summary.quota.unlimited && <div className={styles.progress} role="progressbar" aria-label="Использовано страниц" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress)}><div style={{ width: `${progress}%` }} /></div>}
              <p className={styles.muted}>{summary.quota.expired ? "Срок подписки истёк; обработка недоступна до продления." : summary.quota.unlimited ? "Лимит страниц не установлен." : `Осталось ${pages(summary.quota.remainingTenths ?? 0)} стр.`}</p>
              {summary.quota.limitTenths !== (summary.plan.pagesPerMonth == null ? null : summary.plan.pagesPerMonth * 10) && <p className={styles.muted}>Для аккаунта установлен индивидуальный лимит.</p>}
              <p className={styles.small}>Квота обновляется в каждом периоде. Маскирование обратимо с помощью сохранённого ключа восстановления.</p>
            </section>
            <Link className={styles.inlineLink} href="/account/payments">Платежи и реквизиты →</Link>
          </> : summary.canManage ? <>
            <section className={styles.card} id="transfer" aria-labelledby="transfer-title">
              <h2 id="transfer-title">Банковский перевод</h2>
              <p className={styles.muted}>Выберите тариф и срок, чтобы получить реквизиты для оплаты.</p>
              {summary.offers.length === 0 ? <p className={styles.notice}>Сейчас нет доступных тарифов.</p> : <>
                <div className={styles.fields}>
                  <label>Тариф<select value={plan} onChange={(event) => { const next = summary.offers.find((offer) => offer.code === event.target.value); setPlan(event.target.value); if (next?.priceYearKopecks == null) setMonths(1); }}>{summary.offers.map((offer) => <option value={offer.code} key={offer.code}>{offer.title}</option>)}</select></label>
                  <label>Срок<select value={months} onChange={(event) => setMonths(event.target.value === "12" ? 12 : 1)}><option value={1}>1 месяц</option>{selected?.priceYearKopecks != null && <option value={12}>12 месяцев</option>}</select></label>
                </div>
                {selected && <BankTransferPanel key={`${identity}:${plan}:${months}`} plan={plan} months={months} canManage={summary.canManage} />}
              </>}
            </section>
            <section className={styles.card} aria-labelledby="history-title">
              <h2 id="history-title">История платежей</h2>
              {summary.payments.length === 0 ? <p className={styles.muted}>Платежей пока нет.</p> : <div className={styles.history}>{summary.payments.map((payment) => <article className={styles.payment} key={payment.orderId}>
                <div className={styles.paymentTop}><strong>{payment.planTitle} · {period(payment.months)}</strong><strong>{money(payment.amountKopecks)}</strong></div>
                <p>{date(payment.createdAt)} · {paymentStatus(payment.status)}</p>
                {payment.refundedKopecks > 0 && <p>Возвращено: {money(payment.refundedKopecks)}</p>}
                {payment.status === "CONFIRMED" && !payment.activated && <p>Ожидает активации</p>}
                {payment.accessUntil && payment.activated && <p>Доступ до {date(payment.accessUntil)}</p>}
              </article>)}</div>}
            </section>
          </> : <p className={styles.notice}>История платежей и реквизиты доступны администратору аккаунта. Обратитесь к нему для оплаты.</p>}
        </>}
      </div>
    </main>
  </div>;
}
