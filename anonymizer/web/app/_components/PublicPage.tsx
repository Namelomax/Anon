import Link from "next/link";
import type { ReactNode } from "react";
import styles from "./PublicPage.module.css";

export default function PublicPage({ title, intro, children }: { title: string; intro: string; children: ReactNode }) {
  return <div className={styles.page}>
    <header className={styles.header}><Link className={styles.brand} href="/">AnonAI</Link><nav aria-label="Основная навигация"><Link href="/pricing">Тарифы</Link><Link href="/developers">API</Link></nav><Link className={styles.button} href="/">Начать работу</Link></header>
    <main className={styles.main}>
      <nav className={styles.breadcrumb} aria-label="Хлебные крошки"><Link href="/">Главная</Link><span aria-hidden="true"> / </span><span aria-current="page">{title}</span></nav>
      <div className={styles.hero}><span className={styles.eyebrow}>ANONAI · МАСКИРОВАНИЕ ДАННЫХ</span><h1>{title}</h1><p>{intro}</p></div>
      <article className={styles.content}>{children}</article>
    </main>
    <footer className={styles.footer}><Link href="/">AnonAI</Link><Link href="/pricing">Тарифы</Link><Link href="/developers">API для разработчиков</Link><Link href="/on-premise">Внедрение</Link><Link href="/legal">Правовая информация</Link><Link href="/legal/privacy">Конфиденциальность</Link></footer>
  </div>;
}
