import Link from "next/link";
import { getPublicPlans } from "@/lib/public-plans";
import { pageMetadata } from "@/lib/seo";
import styles from "./pricing.module.css";

export const dynamic = "force-dynamic";
export const metadata = pageMetadata(
  "Тарифы на маскирование документов",
  "Тарифы Anon для маскирования персональных данных в документах: лимиты страниц, пользователи и стоимость.",
  "/pricing",
);

const money = (kopecks: number) => `${(kopecks / 100).toLocaleString("ru-RU", { maximumFractionDigits: 2 })} ₽`;

export default async function PricingPage() {
  let plans: Awaited<ReturnType<typeof getPublicPlans>> | null = null;
  try {
    plans = await getPublicPlans();
  } catch {
    // Каталог может быть временно недоступен; подробности ошибки не публичны.
  }

  return <div className={styles.page}>
    <header className={styles.header}>
      <Link href="/" className={styles.brand}>Anon<span>.</span></Link>
      <nav aria-label="Основная навигация">
        <Link href="/on-premise">Внедрение</Link>
        <Link href="/billing">Оплата</Link>
        <Link href="/register" className={styles.navButton}>Регистрация</Link>
      </nav>
    </header>
    <main>
      <section className={styles.intro}>
        <p className={styles.eyebrow}>Тарифы Anon</p>
        <h1>Тарифы на маскирование документов</h1>
        <p>Выберите объём обработки и число пользователей. После маскирования сохраните таблицу соответствий, чтобы восстановить исходные значения.</p>
      </section>
      <section className={styles.catalog} aria-labelledby="plans-title">
        <div className={styles.sectionHead}>
          <h2 id="plans-title">Доступные тарифы</h2>
          <p>Стоимость и лимиты из актуального каталога</p>
        </div>
        {plans === null ? <p className={styles.notice} role="status">Не удалось загрузить тарифы. Попробуйте обновить страницу позже.</p> : plans.length === 0 ? <p className={styles.notice}>Сейчас нет доступных тарифов.</p> : <div className={styles.plans}>
          {plans.map((plan) => <article className={styles.plan} key={plan.code}>
            <h3>{plan.title}</h3>
            <p className={styles.price}>{money(plan.priceKopecks)} <span>/ месяц</span></p>
            {plan.priceYearKopecks != null && <p className={styles.year}>{money(plan.priceYearKopecks)} за 12 месяцев</p>}
            <ul>
              <li>{plan.pagesPerMonth == null ? "Без ограничения страниц" : `${plan.pagesPerMonth.toLocaleString("ru-RU")} страниц в месяц`}</li>
              <li>{plan.maxUsers == null ? "Без ограничения пользователей" : `Пользователей: ${plan.maxUsers.toLocaleString("ru-RU")}`}</li>
              <li>Маскирование поддерживаемых документов</li>
              <li>Восстановление исходных значений по таблице соответствий</li>
            </ul>
            <Link className={styles.button} href="/billing">Перейти к оплате</Link>
          </article>)}
        </div>}
        <p className={styles.note}>Для оплаты войдите в аккаунт. Управлять оплатой и тарифом может администратор аккаунта.</p>
      </section>
      <section className={styles.details} aria-labelledby="details-title">
        <h2 id="details-title">Как считается объём</h2>
        <p>Объём текста рассчитывается из количества символов: 1 страница соответствует 1 800 символам. Результат округляется до десятых долей страницы.</p>
        <p>Маскирование обратимо: таблица соответствий содержит исходные значения и остаётся чувствительной. Проверяйте результат перед дальнейшим использованием.</p>
        <p>Для работы в собственной инфраструктуре рассмотрите <Link href="/on-premise">внедрение у заказчика</Link>. Условия использования приведены в <Link href="/legal/offer">публичной оферте</Link>.</p>
      </section>
    </main>
    <footer className={styles.footer}>
      <Link href="/" className={styles.brand}>Anon<span>.</span></Link>
      <nav aria-label="Ссылки в подвале"><Link href="/register">Регистрация</Link><Link href="/billing">Оплата</Link><Link href="/on-premise">Внедрение</Link><Link href="/legal/offer">Оферта</Link></nav>
    </footer>
  </div>;
}
