import type { DefaultSession } from "next-auth";

// Расширяем стандартные типы Auth.js своими полями идентичности
// (userId/accountId/role) — они кладутся в JWT/Session в auth.ts
// (callbacks.jwt/session) и читаются везде, где нужна личность пользователя.
declare module "next-auth" {
  interface Session {
    user: {
      id: string;
    } & DefaultSession["user"];
    accountId: number;
    role: string;
  }

  interface User {
    accountId: number;
    role: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    userId?: number;
    accountId?: number;
    role?: string;
  }
}
