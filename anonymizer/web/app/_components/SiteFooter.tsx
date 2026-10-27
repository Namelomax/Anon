import Link from "next/link";
import { LEGAL_DOCS, LICENSOR } from "@/lib/legal";

// Подвал служебных страниц (вход, регистрация, приглашение, оплата, документы).
// На главном экране его нет — там ссылки на документы стоят внизу бокового
// меню (app/page.tsx), как в claude.ai.
export function SiteFooter() {
  return (
    <footer className="site-footer">
      <nav className="site-footer-links">
        <Link href={LEGAL_DOCS.offer.href}>Пользовательское соглашение</Link>
        <Link href={LEGAL_DOCS.privacy.href}>Политика обработки персональных данных</Link>
        <Link href="/legal">Правовая информация</Link>
      </nav>
      <p>
        © {new Date().getFullYear()} {LICENSOR.shortName} · ИНН {LICENSOR.inn} · ОГРНИП{" "}
        {LICENSOR.ogrnip}
      </p>
      <p>
        <a href={`mailto:${LICENSOR.email}`}>{LICENSOR.email}</a> ·{" "}
        <a href={`tel:${LICENSOR.phoneHref}`}>{LICENSOR.phone}</a>
      </p>
    </footer>
  );
}
