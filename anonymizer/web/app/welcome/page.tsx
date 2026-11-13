import Landing from "../landing";
import { pageMetadata } from "@/lib/seo";
import { SITE_DESCRIPTION, SITE_TITLE } from "@/lib/site";

export const dynamic = "force-dynamic";

// Раньше здесь был permanentRedirect("/"): для ВОШЕДШЕГО пользователя «/»
// отдаёт Workspace (см. app/page.tsx), поэтому кнопка «О сервисе» из меню
// рабочего места уходила на редирект и мгновенно возвращала в приложение -
// страницу welcome было не открыть. Возвращаем прямой рендер лендинга.
// Дубль контента с главной закрываем canonical на «/» (тот же приём, что и
// в lib/seo.ts) - индексироваться welcome как отдельная страница не будет.
export const metadata = {
  ...pageMetadata(SITE_TITLE, SITE_DESCRIPTION, "/welcome"),
  alternates: { canonical: "/" },
};

export default function WelcomePage() {
  return <Landing />;
}
