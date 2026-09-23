"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useState } from "react";

// Должно совпадать с MIN_LENGTH в lib/password.ts — это только подсказка и
// браузерная проверка, решает всё равно сервер.
const MIN_PASSWORD_LENGTH = 10;

/**
 * Активация приглашения: приглашённый задаёт пароль и САМ отмечает согласие
 * на обработку ПДн. Учётная запись и запись согласия создаются одним
 * действием — см. app/api/invite/accept/route.ts и раздел 2
 * ЮРИДИЧЕСКИЕ_ДОКУМЕНТЫ/10_Авторизация_биллинг_и_учётные_данные.md.
 *
 * Адрес почты здесь НЕ вводится и НЕ показывается: он уже зафиксирован в
 * приглашении, а показывать его по одному лишь токену из ссылки значило бы
 * отдавать чужой адрес всякому, кому ссылка попала в руки.
 */
export default function InvitePage() {
  const params = useParams<{ token: string }>();
  const router = useRouter();
  const token = typeof params?.token === "string" ? params.token : "";

  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [consent, setConsent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError("Пароли не совпадают.");
      return;
    }
    setLoading(true);
    try {
      const resp = await fetch("/api/invite/accept", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, password, consent }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setDone(true);
      const email = typeof data?.email === "string" ? data.email : "";
      setTimeout(() => {
        router.push(email ? `/login?email=${encodeURIComponent(email)}` : "/login");
      }, 1200);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="wrap" style={{ maxWidth: 440 }}>
      <header>
        <h1>Активация приглашения</h1>
        <p>Задайте пароль — и учётная запись в аккаунте будет создана.</p>
      </header>

      <div className="card">
        {done ? (
          <p className="note">Учётная запись создана. Переходим на страницу входа…</p>
        ) : (
          <form onSubmit={onSubmit}>
            <div className="field">
              <label htmlFor="password">Пароль</label>
              <input
                id="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={MIN_PASSWORD_LENGTH}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="confirm">Повторите пароль</label>
              <input
                id="confirm"
                type="password"
                autoComplete="new-password"
                required
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </div>
            <p className="note" style={{ marginTop: -8, marginBottom: 16 }}>
              Не короче {MIN_PASSWORD_LENGTH} символов и минимум двух разных типов символов
              (буквы, цифры, спецсимволы).
            </p>

            <label className="consent">
              <input
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
              />
              <span>
                Я даю согласие на обработку персональных данных (email, хеш пароля, IP-адрес) в
                соответствии с Политикой обработки персональных данных сервиса и принимаю условия
                оказания услуг.
              </span>
            </label>

            {error && (
              <div className="error" style={{ marginBottom: 16 }}>
                {error}
              </div>
            )}

            <button className="primary" type="submit" disabled={loading}>
              {loading ? "Создаём…" : "Создать учётную запись"}
            </button>
          </form>
        )}

        <p className="auth-links">
          Уже есть доступ? <Link href="/login">Войти</Link>
        </p>
      </div>
    </div>
  );
}
