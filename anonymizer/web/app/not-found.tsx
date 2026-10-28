import Link from "next/link";
import { ShieldCheck } from "lucide-react";
import { SiteFooter } from "@/app/_components/SiteFooter";

export default function NotFound() {
  return (
    <div className="legal-page">
      <div className="legal-top">
        <Link href="/" className="legal-brand">
          <ShieldCheck size={20} />
          Анонимизатор
        </Link>
      </div>
      <main className="legal-main">
        <h1 className="legal-title">Страница не найдена</h1>
        <p>
          Такой страницы нет или она была удалена. <Link href="/">Перейти на главную</Link> и
          обезличить документ.
        </p>
      </main>
      <SiteFooter />
    </div>
  );
}
