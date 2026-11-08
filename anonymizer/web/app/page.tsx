import { auth } from "@/auth";
import { redirect } from "next/navigation";
import { loginUrl } from "@/lib/auth-navigation";
import { pageMetadata } from "@/lib/seo";
import { SITE_DESCRIPTION, SITE_TITLE } from "@/lib/site";
import Landing from "./landing";
import Workspace from "./Workspace";

// Ответ зависит от сессии: персональный интерфейс нельзя кешировать как публичный HTML.
export const dynamic = "force-dynamic";
export const metadata = pageMetadata(SITE_TITLE, SITE_DESCRIPTION, "/");

export default async function HomePage({ searchParams }: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const session = await auth();
  if (session?.user?.id) return <Workspace />;

  const params = await searchParams;
  if (params.tab === "account" || (Array.isArray(params.tab) && params.tab[0] === "account")) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      for (const item of Array.isArray(value) ? value : value === undefined ? [] : [value]) {
        query.append(key, item);
      }
    }
    redirect(loginUrl(`/?${query}`));
  }

  // Landing — клиентский компонент с интерактивными элементами, который Next.js
  // также рендерит на сервере. Проверка сессии больше не скрывает его HTML.
  return <Landing />;
}
