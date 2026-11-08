"use client";

import Link from "next/link";
import { useSession } from "next-auth/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { addMonthsClamped } from "@/lib/period";
import layout from "./account-billing.module.css";
import styles from "./root-admin.module.css";

type Account = {
  id: number; name: string; isActive: boolean;
  plan: { code: string; title: string; pagesPerMonth: number | null };
  pageLimitOverrideTenths: number | null;
  users: { id: number; email: string; role: string; isActive: boolean }[];
  userCount: number;
  quota: { period: string; usedTenths: number; limitTenths: number | null; grantsTenths: number; allowanceTenths: number | null; remainingTenths: number | null; expired: boolean; paidUntil: string | null };
};
type Result = { accounts: Account[]; page: number; pageSize: number; total: number };
const pages = (value: number | null) => value === null ? "Безлимит" : `${(value / 10).toLocaleString("ru-RU")} стр.`;
const utcInput = (date: Date) => date.toISOString().slice(0, -1);
function parseUtcInput(value: string): string | null {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value);
  if (!match) return null;
  const normalized = `${match[1]}:${match[2] ?? "00"}.${(match[3] ?? "").padEnd(3, "0")}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) || date.toISOString() !== normalized ? null : normalized;
}
function roleLabel(role: string) {
  return ({ root: "Владелец сервиса", admin: "Администратор", member: "Пользователь" } as Record<string, string>)[role] ?? role;
}
function periodLabel(value: string) {
  if (/^\d{4}-\d{2}$/.test(value)) {
    const [year, month] = value.split("-").map(Number);
    if (month >= 1 && month <= 12) return new Date(Date.UTC(year, month - 1, 1)).toLocaleDateString("ru-RU", { month: "long", year: "numeric", timeZone: "UTC" });
  }
  if (/^\d{4}-\d{2}-\d{2}(?:T.*)?$/.test(value)) {
    const date = new Date(value);
    if (!Number.isNaN(date.getTime())) return date.toLocaleDateString("ru-RU", { timeZone: "UTC" });
  }
  return value;
}
function parsePages(value: string): number | null {
  const match = /^(\d+)(?:[.,](\d))?$/.exec(value.trim());
  if (!match) return null;
  const whole = Number(match[1]);
  const result = whole * 10 + Number(match[2] ?? 0);
  return Number.isSafeInteger(result) && result <= 2147483647 ? result : null;
}
async function responseError(response: Response) {
  const data = await response.json().catch(() => null);
  return typeof data?.error === "string" ? data.error : `Ошибка запроса (HTTP ${response.status}).`;
}

export default function RootAdmin() {
  const { data: session, status } = useSession();
  const identity = status === "authenticated" && session?.role === "root" && session.user?.id && session.accountId != null ? `${session.user.id}:${session.accountId}` : null;
  const identityRef = useRef(identity);
  identityRef.current = identity;
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<Result | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [success, setSuccess] = useState<{ identity: string; message: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [values, setValues] = useState<Record<number, string>>({});
  const [dates, setDates] = useState<Record<number, string>>({});
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const mutex = useRef(false);
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setResult(null); setLoadedFor(null); setError(null); setBusy(false); mutex.current = false;
    if (!identity) { setLoading(false); return () => { controller.abort(); generation.current++; }; }
    setLoading(true);
    const params = new URLSearchParams({ q: query, page: String(page) });
    fetch(`/api/admin/accounts?${params}`, { cache: "no-store", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error(await responseError(response)); return response.json() as Promise<Result>; })
      .then(data => { if (generation.current === current && identityRef.current === identity) { setResult(data); setLoadedFor(identity); setSuccess(previous => previous?.identity === identity ? { identity, message: "Изменения сохранены, данные обновлены." } : null); setValues(Object.fromEntries(data.accounts.map(a => [a.id, a.pageLimitOverrideTenths === null ? "" : String(a.pageLimitOverrideTenths / 10)]))); setDates(Object.fromEntries(data.accounts.map(a => [a.id, a.quota.paidUntil ? utcInput(new Date(a.quota.paidUntil)) : ""]))); } })
      .catch((err: unknown) => { if (!controller.signal.aborted && generation.current === current && identityRef.current === identity) { setError(err instanceof Error ? err.message : "Не удалось загрузить аккаунты."); setSuccess(previous => previous?.identity === identity ? { identity, message: "Изменения сохранены, но обновить данные не удалось." } : null); } })
      .finally(() => { if (generation.current === current) setLoading(false); });
    return () => { controller.abort(); generation.current++; };
  }, [query, page, revision, identity]);

  const save = useCallback(async (accountId: number, patch: { pageLimitOverrideTenths?: number | null; paidUntil?: string | null }) => {
    if (mutex.current || !identity) return;
    const current = generation.current;
    const saveIdentity = identity;
    mutex.current = true; setBusy(true); setError(null); setSuccess(null);
    try {
      const response = await fetch("/api/admin/accounts", { method: "PATCH", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ accountId, ...patch }) });
      if (!response.ok) throw new Error(await responseError(response));
      if (generation.current !== current || identityRef.current !== saveIdentity) return;
      setSuccess({ identity: saveIdentity, message: "Изменения сохранены. Обновляем данные аккаунтов…" });
      setRevision(n => n + 1);
    } catch (err) { if (generation.current === current && identityRef.current === saveIdentity) setError(err instanceof Error ? err.message : "Не удалось сохранить изменения."); }
    finally { if (generation.current === current && identityRef.current === saveIdentity) { mutex.current = false; setBusy(false); } }
  }, [identity]);

  return <div className={layout.shell}>
    <aside className={layout.sidebar}><Link href="/" className={layout.back}>← К документам</Link><p className={layout.navTitle}>Управление</p><nav className={layout.nav} aria-label="Управление"><Link href="/admin" aria-current="page">Администрирование</Link></nav></aside>
    <main className={layout.main}><div className={layout.content}>
      <h1>Администрирование</h1>
      <p className={layout.muted}>Индивидуальный лимит задаёт общее число страниц аккаунта за период, а не остаток. Он действует до возврата к тарифу. Изменение лимита не сбрасывает расход и не продлевает срок подписки.</p>
      {identity && <form onSubmit={event => { event.preventDefault(); setSuccess(null); setPage(1); setQuery(draft.trim()); }}><label className={styles.search}>Поиск по названию аккаунта или email<input value={draft} maxLength={200} disabled={busy || loading} onChange={event => setDraft(event.target.value)} placeholder="Название или email" /></label><div className={styles.actions}><button type="submit" disabled={busy || loading}>Найти</button></div></form>}
      {!identity && <p className={layout.notice}>{status === "loading" ? "Проверяем доступ…" : "Раздел доступен только владельцу сервиса."}</p>}
      {identity && success?.identity === identity && <p className={layout.notice} role="status">{success.message}</p>}
      {identity && loading && <p className={layout.notice} role="status">Загружаем аккаунты…</p>}
      {identity && error && <p className={layout.notice} role="alert">{error} <button className={layout.textButton} onClick={() => setRevision(n => n + 1)}>Повторить загрузку</button></p>}
      {identity && loadedFor === identity && result && <>
        <p className={layout.muted}>Найдено аккаунтов: {result.total}</p>
        {result.accounts.length === 0 && <p className={layout.notice}>Аккаунты не найдены.</p>}
        {result.accounts.map(account => {
          const value = values[account.id] ?? "";
          const parsed = parsePages(value);
          const dateValue = dates[account.id] ?? "";
          const parsedDate = parseUtcInput(dateValue);
          const quota = account.quota;
          return <section className={layout.card} key={account.id}>
            <div className={layout.cardHeader}><div><h2>{account.name}</h2><p className={layout.muted}>ID {account.id} · {account.isActive ? "Активен" : "Неактивен"} · Тариф: {account.plan.title}</p></div></div>
            <p className={layout.muted}>Пользователи ({account.userCount}): {account.users.map(user => `${user.email} (${roleLabel(user.role)}${user.isActive ? "" : ", неактивен"})`).join(", ") || "нет"}</p>
            {account.users.length < account.userCount && <p className={layout.muted}>Показаны первые 100 пользователей.</p>}
            <div className={styles.stats}><span>Период: {periodLabel(quota.period)}</span><span>Лимит: {pages(quota.limitTenths)} {account.pageLimitOverrideTenths === null ? "(по тарифу)" : "(индивидуальный)"}</span><span>Расход: {pages(quota.usedTenths)}</span>{quota.grantsTenths > 0 && <span>Добавки: {pages(quota.grantsTenths)}</span>}<span>Всего с добавками: {pages(quota.allowanceTenths)}</span><span>Остаток: {pages(quota.remainingTenths)}</span><span>{quota.expired ? "Срок подписки истёк" : "Срок подписки действует"}{quota.paidUntil ? ` до ${new Date(quota.paidUntil).toLocaleDateString("ru-RU", { timeZone: "UTC" })}` : " (срок не установлен)"}</span></div>
            <label className={styles.field}>Индивидуальный лимит, страниц<input inputMode="decimal" value={value} onChange={event => setValues(previous => ({ ...previous, [account.id]: event.target.value }))} placeholder="Например, 100,5" disabled={busy} /></label>
            <p className={layout.muted}>От 0 до 214 748 364,7 страницы; не более одного знака после запятой.</p>
            <div className={styles.actions}><button type="button" disabled={busy || loading || parsed === null} onClick={() => { if (parsed !== null) void save(account.id, { pageLimitOverrideTenths: parsed }); }}>Сохранить лимит</button><button type="button" disabled={busy || loading || account.pageLimitOverrideTenths === null} onClick={() => void save(account.id, { pageLimitOverrideTenths: null })}>Вернуть тариф</button></div>
            <div className={styles.access}>
              <h3>Срок доступа</h3>
              <p className={layout.muted}>Ручная бесплатная выдача текущего тарифа без платежа. Тариф, лимит, расход и даты обновления квоты сохраняются; для аккаунта с истёкшим сроком продолжается прежний расчётный график.</p>
              <label className={styles.field}>Дата и время окончания (UTC)<input type="datetime-local" step="0.001" value={dateValue} disabled={busy || loading} onChange={event => setDates(previous => ({ ...previous, [account.id]: event.target.value }))} /></label>
              <p className={layout.muted}>Выбранный момент окончания не включается в срок доступа. Прошлая дата останавливает доступ. Отсутствие срока не означает безлимит страниц.</p>
              <p className={layout.muted}>Быстрый выбор — от текущего момента:</p>
              <div className={styles.actions}>{[[1, "1 месяц"], [2, "2 месяца"], [12, "1 год"]].map(([months, label]) => <button key={months} type="button" disabled={busy || loading} onClick={() => setDates(previous => ({ ...previous, [account.id]: utcInput(addMonthsClamped(new Date(), Number(months))) }))}>{label}</button>)}</div>
              <div className={styles.actions}><button type="button" disabled={busy || loading || parsedDate === null} onClick={() => { if (parsedDate !== null) void save(account.id, { paidUntil: parsedDate }); }}>Сохранить срок</button><button type="button" disabled={busy || loading || quota.paidUntil === null} onClick={() => void save(account.id, { paidUntil: null })}>Без срока</button></div>
            </div>
          </section>;
        })}
        <div className={styles.actions}><button type="button" disabled={busy || page <= 1} onClick={() => setPage(n => n - 1)}>Назад</button><span>Страница {result.page} из {Math.max(1, Math.ceil(result.total / result.pageSize))}</span><button type="button" disabled={busy || result.page * result.pageSize >= result.total} onClick={() => setPage(n => n + 1)}>Далее</button></div>
      </>}
    </div></main>
  </div>;
}
