import { NextRequest, NextResponse } from "next/server";
import { CredentialsSignin } from "next-auth";
import { signIn } from "@/auth";

export const runtime = "nodejs";

/**
 * POST /api/login — тонкая JSON-обёртка над серверным `signIn()` Auth.js.
 *
 * Зачем отдельный роут, а не встроенная форма `<form action={signIn}>`:
 * страница логина — обычный клиентский компонент (нужен собственный текст
 * ошибки, включая "слишком много попыток" от лимитера в auth.ts), а
 * `signIn()`, вызванный здесь server-side с `redirect: false`, при ошибке
 * авторизации БРОСАЕТ исходный объект ошибки (см. `raw`-режим в
 * next-auth/lib/actions.js) вместо редиректа на `/api/auth/error` — этим и
 * пользуемся, чтобы вытащить `error.code` из authorize() в auth.ts.
 *
 * Cookie сессии Auth.js сам проставляет через `next/headers` `cookies()`
 * внутри `signIn()` — этот роут её руками не трогает.
 */
export async function POST(req: NextRequest) {
  let body: { email?: unknown; password?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }

  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const password = typeof body.password === "string" ? body.password : "";
  if (!email || !password) {
    return NextResponse.json({ error: "Укажите email и пароль." }, { status: 400 });
  }

  try {
    await signIn("credentials", { email, password, redirect: false });
  } catch (e) {
    if (e instanceof CredentialsSignin) {
      if ((e as CredentialsSignin & { code: string }).code === "too_many_attempts") {
        return NextResponse.json(
          {
            error:
              "Слишком много неудачных попыток входа для этого email. Попробуйте снова через 15 минут.",
          },
          { status: 429 },
        );
      }
      // Намеренно один и тот же текст для «нет такого email», «неверный
      // пароль» и «аккаунт отключён» — см. комментарий в auth.ts.
      return NextResponse.json({ error: "Неверный email или пароль." }, { status: 401 });
    }
    console.error("[/api/login] неожиданная ошибка входа:", e);
    return NextResponse.json({ error: "Не удалось выполнить вход. Попробуйте позже." }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}
