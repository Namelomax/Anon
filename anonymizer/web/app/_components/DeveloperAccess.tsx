"use client";

import Link from "next/link";
import { useSession } from "next-auth/react";
import { useEffect, useRef, useState } from "react";
import { useLoginRedirect } from "./useLoginRedirect";
import layout from "./account-billing.module.css";
import styles from "./developer-access.module.css";

type ApiKey = { id: number; name: string | null; prefix: string; createdAt: string; lastUsedAt: string | null; revokedAt: string | null };
const date = (value: string) => new Date(value).toLocaleDateString("ru-RU", { timeZone: "UTC" });
const errorFor = (status: number) => status === 401 ? "Сессия завершилась. Войдите в аккаунт снова." : status === 403 ? "Управление ключами доступно администратору аккаунта." : "Не удалось выполнить запрос. Попробуйте ещё раз.";
async function responseError(response: Response) {
  if (response.status === 400 || response.status === 409) {
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "error" in body && typeof body.error === "string") return body.error;
    } catch { /* Fall back to the general error. */ }
  }
  return errorFor(response.status);
}

export default function DeveloperAccess() {
  const { data: session, status } = useSession();
  useLoginRedirect(status);
  const identity = session?.user?.id ?? session?.user?.email ?? null;
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [name, setName] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const [secretId, setSecretId] = useState<number | null>(null);
  const [showSecret, setShowSecret] = useState(false);
  const [copyMessage, setCopyMessage] = useState("");
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const mutex = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    mutex.current = false;
    setKeys([]); setLoadedFor(null); setError(null); setSecret(null); setSecretId(null); setShowSecret(false); setCopyMessage(""); setConfirmId(null); setName(""); setBusy(false);
    if (status !== "authenticated" || !identity) { setLoading(false); return () => { controller.abort(); generation.current++; }; }
    setLoading(true);
    fetch("/api/account/api-keys", { cache: "no-store", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error(errorFor(response.status)); return response.json() as Promise<{ keys: ApiKey[] }>; })
      .then(data => { if (generation.current === current) { setKeys(data.keys); setLoadedFor(identity); } })
      .catch((err: unknown) => { if (!controller.signal.aborted && generation.current === current) setError(err instanceof Error ? err.message : "Не удалось загрузить ключи."); })
      .finally(() => { if (generation.current === current) setLoading(false); });
    return () => { controller.abort(); generation.current++; };
  }, [status, identity, attempt]);

  async function createKey(event: React.FormEvent) {
    event.preventDefault();
    if (mutex.current || secret) return;
    mutex.current = true; setBusy(true); setError(null);
    const current = generation.current;
    try {
      const response = await fetch("/api/account/api-keys", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() || undefined }) });
      if (!response.ok) throw new Error(await responseError(response));
      const data = await response.json() as { key: string; apiKey: ApiKey };
      if (generation.current === current) { setKeys(previous => [data.apiKey, ...previous]); setSecret(data.key); setSecretId(data.apiKey.id); setName(""); setShowSecret(false); setCopyMessage(""); }
    } catch (err) { if (generation.current === current) setError(err instanceof Error ? err.message : "Не удалось создать ключ."); }
    finally { if (generation.current === current) { mutex.current = false; setBusy(false); } }
  }

  async function revokeKey(id: number) {
    if (mutex.current) return;
    mutex.current = true; setBusy(true); setError(null);
    const current = generation.current;
    try {
      const response = await fetch(`/api/account/api-keys?id=${encodeURIComponent(id)}`, { method: "DELETE", cache: "no-store" });
      if (!response.ok) throw new Error(await responseError(response));
      if (generation.current === current) { setKeys(previous => previous.map(key => key.id === id ? { ...key, revokedAt: new Date().toISOString() } : key)); setConfirmId(null); if (secretId === id) { setSecret(null); setSecretId(null); setShowSecret(false); setCopyMessage(""); } }
    } catch (err) { if (generation.current === current) setError(err instanceof Error ? err.message : "Не удалось отозвать ключ."); }
    finally { if (generation.current === current) { mutex.current = false; setBusy(false); } }
  }

  async function copySecret() {
    const current = generation.current;
    try { await navigator.clipboard.writeText(secret ?? ""); if (generation.current === current) setCopyMessage("Ключ скопирован"); }
    catch { if (generation.current === current) setCopyMessage("Не удалось скопировать. Покажите ключ и скопируйте его вручную."); }
  }

  const ready = status === "authenticated" && identity && loadedFor === identity;
  return <div className={layout.shell}>
    <aside className={layout.sidebar}>
      <Link href="/" className={layout.back}>← К документам</Link>
      <p className={layout.navTitle}>Аккаунт</p>
      <nav className={layout.nav} aria-label="Аккаунт">
        <Link href="/account/subscription">Моя подписка</Link><Link href="/account/payments">Платежи и реквизиты</Link><Link href="/account/developers" aria-current="page">API</Link>
      </nav>
      <Link href="/?tab=account" className={layout.settings}>Настройки аккаунта</Link>
    </aside>
    <main className={layout.main}><div className={layout.content}>
      <h1>API</h1>
      <p className={styles.intro}>Создайте ключ для доступа к API Anon. Используйте ключ только в серверной интеграции и храните его в секрете. Обработка через API расходует общую квоту аккаунта. <Link href="/developers">Документация API →</Link></p>
      {(status === "loading" || loading) && <p className={layout.notice} role="status">Загружаем ключи…</p>}
      {status === "unauthenticated" && <p className={layout.notice}>Войдите в аккаунт, чтобы управлять ключами. <Link href="/login">Войти</Link></p>}
      {status === "authenticated" && !identity && <p className={layout.notice} role="alert">Не удалось определить аккаунт. Войдите снова.</p>}
      {error && <p className={layout.notice} role="alert">{error} {!ready && <button className={layout.textButton} onClick={() => setAttempt(n => n + 1)}>Повторить</button>}</p>}
      {ready && <>
        {secret && <section className={`${layout.card} ${styles.secret}`} aria-labelledby="secret-title">
          <h2 id="secret-title">Сохраните ключ</h2>
          <p className={layout.muted}>Ключ показан только сейчас. Позже его нельзя будет посмотреть или восстановить.</p>
          <div className={styles.secretRow}><input aria-label="Новый API-ключ" type={showSecret ? "text" : "password"} readOnly value={secret} /><button type="button" onClick={() => setShowSecret(!showSecret)}>{showSecret ? "Скрыть" : "Показать"}</button><button type="button" onClick={copySecret}>Копировать</button></div>
          {copyMessage && <p className={layout.muted} role="status">{copyMessage}</p>}
          <button className={styles.primary} type="button" onClick={() => { setSecret(null); setSecretId(null); setShowSecret(false); setCopyMessage(""); }}>Я сохранил ключ</button>
        </section>}
        <section className={layout.card} aria-labelledby="create-title"><h2 id="create-title">Новый ключ</h2><p className={layout.muted}>Название поможет отличить ключи друг от друга. На аккаунт можно создать до 20 активных ключей.</p>
          <form className={styles.form} onSubmit={createKey}><label htmlFor="key-name">Название <span>необязательно</span></label><div className={styles.formRow}><input id="key-name" maxLength={80} value={name} onChange={event => setName(event.target.value)} placeholder="Например, рабочая интеграция" /><button className={styles.primary} disabled={busy || !!secret} type="submit">{busy ? "Подождите…" : "Создать ключ"}</button></div></form>
        </section>
        <section className={layout.card} aria-labelledby="keys-title"><h2 id="keys-title">Ключи аккаунта</h2>
          {keys.length === 0 ? <p className={layout.muted}>Ключей пока нет.</p> : <div className={styles.list}>{keys.map(key => <article className={styles.key} key={key.id}><div className={styles.keyTop}><div><strong>{key.name || "Без названия"}</strong><code>{key.prefix}</code></div><span className={styles.status}>{key.revokedAt ? "Отозван" : "Активен"}</span></div><p>Создан {date(key.createdAt)} · {key.lastUsedAt ? `Использован ${date(key.lastUsedAt)}` : "Ещё не использовался"}</p>{!key.revokedAt && (confirmId === key.id ? <div className={styles.confirm}><span>Отозвать этот ключ? Доступ с ним прекратится.</span><button type="button" disabled={busy} onClick={() => revokeKey(key.id)}>Отозвать</button><button type="button" disabled={busy} onClick={() => setConfirmId(null)}>Отмена</button></div> : <button className={styles.textButton} type="button" disabled={busy} onClick={() => setConfirmId(key.id)}>Отозвать ключ</button>)}</article>)}</div>}
        </section>
      </>}
    </div></main>
  </div>;
}
