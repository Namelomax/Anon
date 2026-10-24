"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { signOut } from "next-auth/react";

// Должно совпадать с MIN_LENGTH в lib/password.ts — это только подсказка и
// браузерная проверка, решает всё равно сервер.
const MIN_PASSWORD_LENGTH = 10;

type InviteInfo = {
  accountName: string;
  email: string;
  role: string;
  existingUser: boolean;
  /** Есть только у вошедшего: может ли он вступить и почему нет. */
  joinCheck?: { ok: true } | { ok: false; reason: string };
};

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

  // undefined — ещё грузится; null — ссылка недействительна.
  const [info, setInfo] = useState<InviteInfo | null | undefined>(undefined);
  const [infoError, setInfoError] = useState<string | null>(null);
  const [understand, setUnderstand] = useState(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    fetch(`/api/invite/info?token=${encodeURIComponent(token)}`)
      .then(async (resp) => {
        const data = await resp.json().catch(() => ({}));
        if (cancelled) return;
        if (!resp.ok) {
          setInfoError(data?.error || `HTTP ${resp.status}`);
          setInfo(null);
        } else {
          setInfo(data as InviteInfo);
        }
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setInfoError(e instanceof Error ? e.message : String(e));
        setInfo(null);
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  const onJoin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const resp = await fetch("/api/invite/join", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, confirm: understand }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      setDone(true);
      // Полная перезагрузка: сессия уже указывает на новый аккаунт (auth.ts
      // перечитывает его из БД), но клиентский кеш сессии и кабинета старый.
      window.location.href = "/";
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setLoading(false);
    }
  };

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

  const activation = (
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

  const shell = (title: string, children: React.ReactNode) => (
    <div className="wrap" style={{ maxWidth: 440 }}>
      <header>
        <h1>{title}</h1>
      </header>
      <div className="card">{children}</div>
    </div>
  );

  if (info === undefined) {
    return shell("Приглашение", <p className="note">Загружаем…</p>);
  }
  if (info === null) {
    return shell(
      "Приглашение",
      <div className="error">{infoError || "Ссылка приглашения недействительна или истекла."}</div>,
    );
  }
  if (!info.existingUser) return activation;

  const here = `/invite/${encodeURIComponent(token)}`;
  const jc = info.joinCheck;
  if (!jc) {
    // Не вошёл: возврат на эту же страницу после входа (?next=, см. login).
    return shell(
      "Вступление в аккаунт",
      <p className="note">
        Адрес {info.email} уже зарегистрирован. Войдите, чтобы вступить в аккаунт «
        {info.accountName}».{" "}
        <Link href={`/login?email=${encodeURIComponent(info.email)}&next=${encodeURIComponent(here)}`}>
          Войти
        </Link>
      </p>,
    );
  }
  if (!jc.ok) {
    return shell(
      "Вступление в аккаунт",
      <>
        <div className="error" style={{ marginBottom: 16 }}>
          {jc.reason}
        </div>
        <button className="ghost" type="button" onClick={() =>
            // Без серверного редиректа — см. выход в app/page.tsx.
            void signOut({ redirect: false }).then(() => window.location.assign(here))
          }>
          Выйти
        </button>
      </>,
    );
  }
  return shell(
    "Вступление в аккаунт",
    done ? (
      <p className="note">Вы вступили в аккаунт. Переходим…</p>
    ) : (
      <form onSubmit={onJoin}>
        <p className="note" style={{ marginBottom: 12 }}>
          Вас приглашают в аккаунт «{info.accountName}». После вступления:
        </p>
        <ul className="note" style={{ marginBottom: 16, paddingLeft: 20 }}>
          <li>ваш текущий аккаунт будет закрыт;</li>
          <li>неизрасходованный остаток страниц пробного периода пропадёт;</li>
          <li>история обработки останется в старом аккаунте и не переносится;</li>
          <li>
            вы будете расходовать общий лимит аккаунта «{info.accountName}», тариф оплачивает его
            администратор.
          </li>
        </ul>
        <label className="consent">
          <input
            type="checkbox"
            checked={understand}
            onChange={(e) => setUnderstand(e.target.checked)}
          />
          <span>Понимаю, что мой текущий аккаунт будет закрыт</span>
        </label>
        {error && (
          <div className="error" style={{ marginBottom: 16 }}>
            {error}
          </div>
        )}
        <button className="primary" type="submit" disabled={loading || !understand}>
          {loading ? "Вступаем…" : "Вступить в аккаунт"}
        </button>
      </form>
    ),
  );
}
