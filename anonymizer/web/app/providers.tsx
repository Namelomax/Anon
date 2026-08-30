"use client";

import { SessionProvider } from "next-auth/react";

/**
 * Обёртка-клиентский компонент под `<SessionProvider>` из next-auth/react —
 * `useSession()`/`signOut()` в клиентских компонентах (см. app/page.tsx)
 * работают только внутри этого контекста. Вынесено в отдельный файл, а не
 * добавлено прямо в app/layout.tsx, потому что layout.tsx — серверный
 * компонент (в нём нет "use client"), а провайдеру контекста обязательно
 * нужна клиентская граница.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  return <SessionProvider>{children}</SessionProvider>;
}
