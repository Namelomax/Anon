"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

function LoginForm() {
  const searchParams = useSearchParams();
  const [email, setEmail] = useState(searchParams.get("email") || "");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const resp = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data?.error || `HTTP ${resp.status}`);
      // Полная перезагрузка, а не router.push: /api/login уже выставил
      // cookie сессии Auth.js (см. app/api/login/route.ts), и
      // SessionProvider (app/providers.tsx) подхватит её при монтировании
      // на новой странице — проще и надёжнее, чем вручную дёргать update().
      window.location.href = "/";
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
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
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>

      {error && (
        <div className="error" style={{ marginBottom: 16 }}>
          {error}
        </div>
      )}

      <button className="primary" type="submit" disabled={loading}>
        {loading ? "Входим…" : "Войти"}
      </button>
    </form>
  );
}

export default function LoginPage() {
  return (
    <div className="wrap" style={{ maxWidth: 440 }}>
      <header>
        <h1>Вход</h1>
        <p>Войдите в учётную запись сервиса.</p>
      </header>

      <div className="card">
        {/* useSearchParams требует Suspense-границу при статической части
            страницы — подставляем email из query (?email=... после
            регистрации) без падения билда на "missing Suspense boundary". */}
        <Suspense fallback={null}>
          <LoginForm />
        </Suspense>

        <p className="auth-links">
          Нет учётной записи? <Link href="/register">Зарегистрироваться</Link>
        </p>
      </div>
    </div>
  );
}
