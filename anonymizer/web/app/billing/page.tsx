"use client";
import Link from "next/link";
import { Check, X } from "lucide-react";
import { useSession } from "next-auth/react";
import { useLoginRedirect } from "@/app/_components/useLoginRedirect";
import { useEffect, useRef, useState } from "react";
import type { AccountSummary } from "@/lib/account-summary";
import styles from "./billing.module.css";
type Offer = AccountSummary["offers"][number];
type Selection = {
  offer: Offer;
  months: 1 | 12;
};
const money = (kopecks: number) => `${(kopecks / 100).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;
const date = (value: string) => new Date(value).toLocaleDateString("ru-RU", { timeZone: "UTC" });
const pages = (tenths: number) => (tenths / 10).toLocaleString("ru-RU");
const period = (months: number) => months === 12 ? "12 месяцев" : months === 1 ? "1 месяц" : `${months} мес.`;
const amount = (offer: Offer, months: 1 | 12) => months === 12 ? offer.priceYearKopecks : offer.priceKopecks;
export default function BillingPage() {
  const { data: session, status } = useSession();
  useLoginRedirect(status);
  const identity = session?.user?.id ?? session?.user?.email ?? null;
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const [accountSummary, setSummary] = useState<AccountSummary | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const summary = status === "authenticated" && loadedFor === identity ? accountSummary : null;
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [mode, setMode] = useState<"online" | "invoice" | "api">("online");
  const [months, setMonths] = useState<1 | 12>(1);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [busy, setBusy] = useState(false);
  const [payError, setPayError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLElement | null>(null);
  const mutex = useRef(false);
  useEffect(() => {
    setSummary(null);
    setLoadedFor(null);
    setError(null);
    setSelection(null);
    setPayError(null);
    mutex.current = false;
    setBusy(false);
    if (status !== "authenticated") {
      setSelection(null);
      setLoading(status === "loading");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    fetch("/api/account", { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (response.status === 401)
          throw new Error("Сессия завершилась. Войдите в аккаунт снова.");
        if (!response.ok)
          throw new Error("Не удалось загрузить данные аккаунта.");
        return response.json() as Promise<AccountSummary>;
      })
      .then((data) => {
        if (controller.signal.aborted)
          return;
        setSummary(data);
        setLoadedFor(identity);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted)
          setError(err instanceof Error ? err.message : "Не удалось загрузить данные аккаунта.");
      })
      .finally(() => {
        if (!controller.signal.aborted)
          setLoading(false);
      });
    return () => controller.abort();
  }, [status, retry, identity]);
  useEffect(() => {
    const node = dialog.current;
    if (selection && node && !node.open)
      node.showModal();
    if (!selection && node?.open) {
      node.close();
      trigger.current?.focus();
    }
  }, [selection]);
  const closeDialog = () => {
    if (!mutex.current) {
      setSelection(null);
      setPayError(null);
    }
  };
  async function checkout() {
    if (!selection || !summary || summary.payBlockedReason !== null || mutex.current)
      return;
    mutex.current = true;
    setBusy(true);
    setPayError(null);
    try {
      const response = await fetch("/api/billing/checkout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan: selection.offer.code, months: selection.months }) });
      const data = await response.json().catch(() => ({}));
      if (identityRef.current !== identity)
        return;
      if (!response.ok || typeof data.url !== "string")
        throw new Error(data.error || "Не удалось открыть оплату. Попробуйте снова.");
      window.location.assign(data.url);
    }
    catch (err) {
      setPayError(err instanceof Error ? err.message : "Не удалось открыть оплату.");
      mutex.current = false;
      setBusy(false);
    }
  }
  const warning = summary && selection && !summary.quota.expired && summary.quota.paidUntil && (summary.quota.remainingTenths ?? 0) > 0 && selection.offer.code !== summary.plan.code;
  return <div className={styles.page}>
    <header className={styles.header}>
      <Link className={styles.brand} href="/">Anon</Link>
      <nav aria-label="Навигация">
        <Link href="/account/subscription">Моя подписка</Link>
        <Link href="/account/payments">Платежи и реквизиты</Link>
        <Link className={styles.close} href="/" aria-label="Вернуться к документам"><X size={20} /></Link>
      </nav>
    </header>
    <main className={styles.main}>
      <div className={styles.intro}>
        <h1>Выберите тариф для работы с документами</h1>
        <p>Маскируйте данные, сохраняйте ключ и восстанавливайте исходный текст</p>
        <div className={styles.toggle} role="group" aria-label="Подписки и API">
          <button type="button" aria-pressed={mode === "online"} onClick={() => setMode("online")}>Онлайн-оплата</button>
          <button type="button" aria-pressed={mode === "invoice"} onClick={() => setMode("invoice")}>Для организаций</button>
          <button type="button" aria-pressed={mode === "api"} onClick={() => setMode("api")}>API</button>
        </div>
      </div>
      {(loading || status === "loading") && <p
        role="status"
        className={styles.notice}>Загружаем данные аккаунта…</p>}
      {status === "unauthenticated" && <div className={styles.notice}>Войдите в аккаунт, чтобы посмотреть тарифы и оплату. <Link href="/login">Войти</Link></div>}
      {status === "authenticated" && error && <div
        className={styles.notice}
        role="alert">{error} <button
          className={styles.secondary}
          onClick={() => setRetry((value) => value + 1)}>Повторить</button></div>}
      {status === "authenticated" && summary && <>
        <p className={styles.current}>Текущий тариф: {summary.plan.title} · <Link href="/account/subscription">Управление подпиской</Link></p>
        {mode === "api" ? <section className={`${styles.section} ${styles.apiPanel}`} aria-labelledby="api-title">
          <h2 id="api-title">API для разработчиков</h2>
          <p className={styles.apiPrice}>2 ₽ <span>за страницу</span></p>
          <p>Одна страница - 1 800 символов. API оплачивается отдельно и не входит в подписку.</p>
          <div className={styles.apiLinks}>
            <Link className={styles.primary} href="/account/developers">Управление API-ключами</Link>
            <Link className={styles.secondary} href="/developers">Документация API</Link>
          </div>
        </section> : <section
          className={styles.section}
          aria-labelledby="plans-title"><div className={styles.sectionHead}>
            <div>
              <h2 id="plans-title">Выберите тариф</h2>
              <p>Лимит страниц обновляется в каждом периоде</p>
            </div>
            <div
              className={styles.toggle}
              role="group"
              aria-label="Период оплаты">
              <button
                aria-pressed={months === 1}
                onClick={() => setMonths(1)}>Месяц</button>
              <button
                aria-pressed={months === 12}
                onClick={() => setMonths(12)}>Год</button>
            </div>
          </div>
          {mode === "online" && summary.payBlockedReason && <p className={styles.notice}>
            {summary.payBlockedReason === "role" ? "Оплатить или сменить тариф может администратор аккаунта." : "Онлайн-оплата пока не подключена. Вы можете выбрать банковский перевод."}
          </p>}
          <div className={styles.plans}>
            {summary.offers.map((offer) => {
              const price = amount(offer, months);
              const fullYearPrice = offer.priceKopecks * 12;
              const savings = months === 12 && price != null ? fullYearPrice - price : 0;
              const savingPercent = savings > 0 && fullYearPrice > 0 ? Math.round(savings / fullYearPrice * 100) : 0;
              return <article

                key={offer.code}
                className={styles.plan}>
                <div className={styles.planTop}>
                  <h3>
                    {offer.title}
                  </h3>
                  {offer.code === summary.plan.code && <span className={styles.badge}>Текущий</span>}
                </div>
                <div className={styles.priceComparison}>
                  {savings > 0 && <div className={styles.oldPrice}><span>Без скидки</span><del>{money(fullYearPrice)}</del></div>}
                  <div>
                    {savings > 0 && <span className={styles.priceLabel}>Со скидкой</span>}
                    <div className={styles.price}>{price == null ? "Недоступно" : money(price)}</div>
                  </div>
                </div>
                <p className={styles.priceNote}>
                  {price == null ? "На этот период" : months === 12 ? "за 12 месяцев" : "за месяц"}
                </p>
                {savings > 0 && <p className={styles.saving}>Экономия {money(savings)} · {savingPercent}%</p>}
                {mode === "invoice" ? (price != null && summary.canManage ? <Link
                  className={styles.primary}
                  href={`/account/payments?plan=${encodeURIComponent(offer.code)}&months=${months}#transfer`}
                  aria-label={`Реквизиты для оплаты: ${offer.title}, ${period(months)}`}>
                  Реквизиты для оплаты
                </Link> : <span className={`${styles.primary} ${styles.disabled}`} aria-disabled="true">Реквизиты для оплаты</span>) : <button
                  className={styles.primary}
                  aria-label={`${offer.code === summary.plan.code ? "Продлить" : "Подписаться"}: ${offer.title}, ${period(months)}`}
                  disabled={price == null || summary.payBlockedReason !== null}
                  onClick={(event) => { trigger.current = event.currentTarget; setPayError(null); setSelection({ offer, months }); }}>
                  {offer.code === summary.plan.code ? "Продлить" : "Подписаться"}
                </button>}
                <ul>
                  <li><Check size={16} aria-hidden="true" /> <span>{offer.pagesPerMonth?.toLocaleString("ru-RU") ?? "Без ограничения"} страниц в месяц</span></li>
                  <li><Check size={16} aria-hidden="true" /> <span>{offer.maxUsers == null ? "Без ограничения пользователей" : `Пользователей: ${offer.maxUsers}`}</span></li>
                  <li><Check size={16} aria-hidden="true" /> <span>Маскирование поддерживаемых документов</span></li>
                  <li><Check size={16} aria-hidden="true" /> <span>Ключ восстановления</span></li>
                  <li><Check size={16} aria-hidden="true" /> <span>Восстановление без ИИ</span></li>
                </ul>
              </article>;
            })}
          </div>
          {summary.offers.length === 0 && <p className={styles.notice}>Сейчас нет доступных тарифов.</p>}
        </section>}
      </>}
    </main>
    <dialog
      ref={dialog}
      className={styles.dialog}
      onCancel={(event) => { event.preventDefault(); closeDialog(); }}
      aria-labelledby="checkout-title">
      {selection && summary && <>
        <h2 id="checkout-title">Подтвердите выбор</h2>
        <p className={styles.dialogIntro}>Проверьте условия перед переходом к оплате</p>
        <dl>
          <div>
            <dt>Тариф</dt>
            <dd>
              {selection.offer.title}
            </dd>
          </div>
          <div>
            <dt>Срок</dt>
            <dd>
              {period(selection.months)}
            </dd>
          </div>
          <div>
            <dt>К оплате</dt>
            <dd>
              {money(amount(selection.offer, selection.months)!)}
            </dd>
          </div>
        </dl>
        {warning && <p className={styles.warning}>При смене тарифа остаток {pages(summary.quota.remainingTenths!)} стр. и дни до {date(summary.quota.paidUntil!)} не переносятся. Новый срок начнётся с даты оплаты.</p>}
        {selection.offer.code === summary.plan.code && !summary.quota.expired && summary.quota.paidUntil && <p className={styles.notice}>При продлении текущие страницы сохраняются. Лимит обновится в начале следующего периода.</p>}
        {payError && <p
          role="alert"
          className={styles.error}>
          {payError}
        </p>}
        <div className={styles.dialogActions}>
          <button
            className={styles.secondary}
            onClick={closeDialog}
            disabled={busy}>Отмена</button>
          <button
            className={styles.primary}
            onClick={checkout}
            disabled={busy}>
            {busy ? "Переходим…" : "Перейти к оплате"}
          </button>
        </div>
      </>}
    </dialog>
  </div>;
}
