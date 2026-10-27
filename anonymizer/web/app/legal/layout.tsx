import Link from "next/link";
import { ShieldCheck } from "lucide-react";
import { SiteFooter } from "@/app/_components/SiteFooter";

// Каркас раздела документов: узкая колонка для чтения, ссылка на сервис
// сверху, подвал с реквизитами снизу.
export default function LegalLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="legal-page">
      <div className="legal-top">
        <Link href="/" className="legal-brand">
          <ShieldCheck size={20} />
          Анонимизатор
        </Link>
        <Link href="/legal" className="legal-top-link">
          Правовая информация
        </Link>
      </div>
      <main className="legal-main">{children}</main>
      <SiteFooter />
    </div>
  );
}
