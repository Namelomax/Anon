import NextAuth, { CredentialsSignin } from "next-auth";
import Credentials from "next-auth/providers/credentials";
import { verify } from "@node-rs/argon2";
import { prisma } from "@/lib/db";
import { checkLoginLock, recordLoginFailure, recordLoginSuccess } from "@/lib/login-limiter";

// Отдельный подкласс CredentialsSignin (не просто `return null` из authorize)
// — так вызывающий код (см. app/api/login/route.ts) может отличить «слишком
// много попыток» от «неверный пароль» через `error.code`, оставаясь в рамках
// штатного механизма ошибок Auth.js (throw внутри authorize перехватывается
// фреймворком и не роняет запрос).
class TooManyAttemptsError extends CredentialsSignin {
  code = "too_many_attempts";
}

// Файл лежит В КОРНЕ проекта (рядом с package.json/next.config.mjs), а не в
// lib/ — это стандартное расположение конфигурации Auth.js v5 для App Router
// (import { auth } from "@/auth" из серверных компонентов/route-хендлеров),
// зафиксированное в официальных примерах next-auth beta.

// 7 дней — компромисс между удобством (не логиниться каждый день) и тем, что
// это одновременно и потолок задержки перед принудительным relogin: см.
// комментарий про разрыв отзыва доступа в callbacks.jwt ниже — реальная
// проверка isActive идёт на каждый запрос, а не раз в 7 дней, так maxAge
// влияет только на то, когда токен протухнет САМ по себе (истёк срок, а не
// был отозван).
const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

export const { handlers, signIn, signOut, auth } = NextAuth({
  // JWT — единственная поддерживаемая Auth.js v5 стратегия сессий при
  // Credentials-провайдере (database-сессии требуют adapter, а adapter нам
  // не нужен — см. комментарий ниже про его отсутствие).
  session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_SECONDS },

  // Адаптер Prisma НЕ подключается намеренно. Модель Account в @auth/prisma-
  // adapter означает «OAuth-аккаунт пользователя у внешнего провайдера» — это
  // лобовая коллизия с нашей моделью Account (биллинговая организация с
  // квотой, см. prisma/schema.prisma). Credentials+JWT не требует ни одной
  // adapter-таблицы: пользователя ищем напрямую в своей таблице User.
  providers: [
    Credentials({
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Пароль", type: "password" },
      },
      async authorize(credentials) {
        const email =
          typeof credentials?.email === "string" ? credentials.email.trim().toLowerCase() : "";
        const password = typeof credentials?.password === "string" ? credentials.password : "";
        if (!email || !password) return null;

        // Блокировку проверяем ДО обращения к БД и до сравнения пароля — см.
        // lib/login-limiter.ts. Считается по email, а не по IP: цель угрозы
        // У-12 — подбор пароля К КОНКРЕТНОМУ аккаунту, атакующий с одним
        // email и множеством IP (ботнет) не должен обходить лимит сменой IP.
        if (checkLoginLock(email) != null) {
          throw new TooManyAttemptsError();
        }

        const user = await prisma.user.findUnique({
          where: { email },
          include: { account: true },
        });
        // Пользователь не найден, деактивирован сам или деактивирован его
        // аккаунт — во всех трёх случаях просто отказываем во входе. НЕ
        // различаем эти причины в ответе (см. /api/register про ту же логику
        // защиты от перебора email) — иначе это раскрывает, зарегистрирован
        // ли адрес. Неудача засчитывается лимитеру ОДИНАКОВО для всех трёх
        // случаев по той же причине: иначе поведение лимитера (блокирует/не
        // блокирует) само по себе стало бы каналом, раскрывающим,
        // существует ли email.
        if (!user || !user.isActive || !user.account.isActive) {
          recordLoginFailure(email);
          return null;
        }

        const passwordOk = await verify(user.passwordHash, password);
        if (!passwordOk) {
          recordLoginFailure(email);
          return null;
        }

        recordLoginSuccess(email);
        return {
          id: String(user.id),
          email: user.email,
          accountId: user.accountId,
          role: user.role,
        };
      },
    }),
  ],

  callbacks: {
    async jwt({ token, user }) {
      if (user) {
        // Свежий логин (authorize() только что вернул пользователя) — кладём
        // идентификаторы в токен один раз.
        token.userId = Number(user.id);
        token.accountId = user.accountId;
        token.role = user.role;
      }

      if (token.userId == null) return token;

      // РАЗРЫВ ОТЗЫВА ДОСТУПА ПРИ JWT-СЕССИЯХ: сам JWT остаётся валидным
      // (подпись верна) до истечения maxAge, ЧТО БЫ НИ СЛУЧИЛОСЬ С
      // ПОЛЬЗОВАТЕЛЕМ В БД. Без этой перепроверки деактивированный
      // (isActive=false) пользователь или заблокированный аккаунт продолжали
      // бы работать вплоть до 7 дней. Плата за это — один SELECT на КАЖДОЕ
      // обращение к сессии; дёшево на SQLite (см. обоснование нагрузки в
      // шапке prisma/schema.prisma — БД здесь никогда не узкое место).
      const dbUser = await prisma.user.findUnique({
        where: { id: token.userId as number },
        include: { account: true },
      });
      if (!dbUser || !dbUser.isActive || !dbUser.account.isActive) {
        // Возврат null — сигнал Auth.js считать токен недействительным:
        // session() ниже получит token без userId и отдаст анонимную сессию,
        // то есть пользователь фактически разлогинен на следующем же запросе.
        return null;
      }
      // Роль/аккаунт могли измениться уже ПОСЛЕ выдачи токена (перевод
      // пользователя, смена роли админом) — синхронизируем токен с текущим
      // состоянием БД, а не доверяем значению, зафиксированному в момент
      // входа.
      token.accountId = dbUser.accountId;
      token.role = dbUser.role;
      return token;
    },

    async session({ session, token }) {
      if (token.userId == null) {
        // jwt() выше вернул null (деактивация) — не отдаём никакой личности.
        return session;
      }
      session.user.id = String(token.userId);
      session.accountId = token.accountId as number;
      session.role = token.role as string;
      return session;
    },
  },

  secret: process.env.AUTH_SECRET,

  // Фронтенд работает за прокси (JupyterHub/reverse-proxy, см. app/api/
  // _shared.ts) — Auth.js по умолчанию не доверяет заголовкам хоста от
  // прокси вне localhost, доверяем явно.
  trustHost: true,
});
