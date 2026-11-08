"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
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
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<Result | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [values, setValues] = useState<Record<number, string>>({});
  const [revision, setRevision] = useState(0);
  const generation = useRef(0);
  const mutex = useRef(false);
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setLoading(true); setError(null); setResult(null);
    const params = new URLSearchParams({ q: query, page: String(page) });
    fetch(`/api/admin/accounts?${params}`, { cache: "no-store", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error(await responseError(response)); return response.json() as Promise<Result>; })
      .then(data => { if (generation.current === current) { setResult(data); setValues(Object.fromEntries(data.accounts.map(a => [a.id, a.pageLimitOverrideTenths === null ? "" : String(a.pageLimitOverrideTenths / 10)]))); } })
      .catch((err: unknown) => { if (!controller.signal.aborted && generation.current === current) setError(err instanceof Error ? err.message : "Не удалось загрузить аккаунты."); })
      .finally(() => { if (generation.current === current) setLoading(false); });
    return () => { controller.abort(); generation.current++; };
  }, [query, page, revision]);

  const save = useCallback(async (accountId: number, value: number | null) => {
    if (mutex.current) return;
    mutex.current = true; setBusy(true); setError(null);
    try {
      const response = await fetch("/api/admin/accounts", { method: "PATCH", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ accountId, pageLimitOverrideTenths: value }) });
      if (!response.ok) throw new Error(await responseError(response));
      setRevision(n => n + 1);
    } catch (err) { setError(err instanceof Error ? err.message : "Не удалось сохранить лимит."); }
    finally { mutex.current = false; setBusy(false); }
  }, []);

  return <div className={layout.shell}>
    <aside className={layout.sidebar}><Link href="/" className={layout.back}>← К документам</Link><p className={layout.navTitle}>Управление</p><nav className={layout.nav} aria-label="Управление"><Link href="/admin" aria-current="page">Администрирование</Link></nav></aside>
    <main className={layout.main}><div className={layout.content}>
      <h1>Администрирование</h1>
      <p className={layout.muted}>Индивидуальный лимит задаёт общее число страниц аккаунта за период, а не остаток. Он действует до возврата к тарифу. Изменение лимита не сбрасывает расход и не продлевает срок подписки.</p>
      <label className={styles.search}>Поиск по названию аккаунта или email<input value={query} onChange={event => { setQuery(event.target.value); setPage(1); }} placeholder="Название или email" /></label>
      {loading && <p className={layout.notice} role="status">Загружаем аккаунты…</p>}
      {error && <p className={layout.notice} role="alert">{error} <button className={layout.textButton} onClick={() => setRevision(n => n + 1)}>Повторить загрузку</button></p>}
      {result && <>
        <p className={layout.muted}>Найдено аккаунтов: {result.total}</p>
        {result.accounts.length === 0 && <p className={layout.notice}>Аккаунты не найдены.</p>}
        {result.accounts.map(account => {
          const value = values[account.id] ?? "";
          const parsed = parsePages(value);
          const quota = account.quota;
          return <section className={layout.card} key={account.id}>
            <div className={layout.cardHeader}><div><h2>{account.name}</h2><p className={layout.muted}>ID {account.id} · {account.isActive ? "Активен" : "Неактивен"} · Тариф: {account.plan.title}</p></div></div>
            <p className={layout.muted}>Пользователи ({account.userCount}): {account.users.map(user => `${user.email} (${user.role}${user.isActive ? "" : ", неактивен"})`).join(", ") || "нет"}</p>
            <div className={styles.stats}><span>Период: {quota.period}</span><span>Лимит: {pages(quota.limitTenths)} {account.pageLimitOverrideTenths === null ? "(по тарифу)" : "(индивидуальный)"}</span><span>Расход: {pages(quota.usedTenths)}</span>{quota.grantsTenths > 0 && <span>Добавки: {pages(quota.grantsTenths)}</span>}<span>Всего с добавками: {pages(quota.allowanceTenths)}</span><span>Остаток: {pages(quota.remainingTenths)}</span><span>{quota.expired ? "Срок подписки истёк" : "Срок подписки действует"}{quota.paidUntil ? ` до ${new Date(quota.paidUntil).toLocaleDateString("ru-RU", { timeZone: "UTC" })}` : " (срок не установлен)"}</span></div>
            <label className={styles.field}>Индивидуальный лимит, страниц<input inputMode="decimal" value={value} onChange={event => setValues(previous => ({ ...previous, [account.id]: event.target.value }))} placeholder="Например, 100,5" disabled={busy} /></label>
            <p className={layout.muted}>От 0 до 214 748 364,7 страницы; не более одного знака после запятой.</p>
            <div className={styles.actions}><button type="button" disabled={busy || parsed === null} onClick={() => { if (parsed !== null) void save(account.id, parsed); }}>Сохранить лимит</button><button type="button" disabled={busy || account.pageLimitOverrideTenths === null} onClick={() => void save(account.id, null)}>Вернуть тариф</button></div>
          </section>;
        })}
        <div className={styles.actions}><button type="button" disabled={busy || page <= 1} onClick={() => setPage(n => n - 1)}>Назад</button><span>Страница {result.page} из {Math.max(1, Math.ceil(result.total / result.pageSize))}</span><button type="button" disabled={busy || result.page * result.pageSize >= result.total} onClick={() => setPage(n => n + 1)}>Далее</button></div>
      </>}
    </div></main>
  </div>;
}
