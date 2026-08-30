"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

// Дублирует минимальное правило из lib/password.ts (длина >= 10) — только
// чтобы не заставлять пользователя ждать round-trip на сервер ради самой
// очевидной ошибки. Окончательная проверка (включая правило про два класса
// символов) — всегда на сервере, в /api/register; это поле — подсказка, а
// не источник истины.
const MIN_PASSWORD_LENGTH = 10;

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [consent, setConsent] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!consent) {
      setError("Отметьте согласие на обработку персональных данных — без него регистрация невозможна.");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Пароль должен быть не короче ${MIN_PASSWORD_LENGTH} символов.`);
      return;
    }
    if (password !== confirm) {
      setError("Пароли не совпадают.");
      return;
    }

    setLoading(true);
    try {
      const resp = await fetch("/api/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, consent }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setDone(true);
      // Автовход после регистрации намеренно не делаем — /api/register
      // только создаёт учётную запись, а вход идёт исключительно через
      // Auth.js (authorize() в auth.ts), см. app/(auth)/login. Ведём
      // пользователя на страницу входа с уже подставленным email.
      setTimeout(() => {
        router.push(`/login?email=${encodeURIComponent(email)}`);
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
        <h1>Регистрация</h1>
        <p>Создайте учётную запись, чтобы пользоваться сервисом.</p>
      </header>

      <div className="card">
        {done ? (
          <p className="note">
            Учётная запись создана. Переходим на страницу входа…
          </p>
        ) : (
          <form onSubmit={onSubmit}>
            <div className="field">
              <label htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
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

            <button className="primary" type="submit" disabled={loading || !consent}>
              {loading ? "Регистрируем…" : "Зарегистрироваться"}
            </button>
          </form>
        )}

        <p className="auth-links">
          Уже есть учётная запись? <Link href="/login">Войти</Link>
        </p>
      </div>
    </div>
  );
}
