"use client";

import { useState } from "react";
import Link from "next/link";
import { ArrowDown, ArrowRight, FileText, LockKeyhole, ShieldCheck, Sparkles } from "lucide-react";
import { LEGAL_DOCS } from "../lib/legal";
import styles from "./landing.module.css";

export default function Landing({ onStart }: { onStart?: () => void }) {
  const [paused, setPaused] = useState(false);

  return (
    <div className={styles.landing}>
      <header className={styles.header}>
        <a className={styles.brand} href="#top" aria-label="Anon - на главную"><span className={styles.brandMark}>a<span>.</span></span> anon</a>
        <nav className={styles.nav} aria-label="Навигация"><a href="#how">Как это работает</a><a href="#security">О данных</a><a href="#faq">Вопросы</a></nav>
        <div className={styles.headerActions}><Link href="/login">Войти</Link><Link className={styles.headerButton} href="/register">Регистрация <ArrowRight size={15} /></Link></div>
      </header>

      <main id="top">
        <section className={styles.hero}>
          <div className={styles.heroCopy}>
            <div className={styles.eyebrow}><span /> СЕРВИС МАСКИРОВАНИЯ ДОКУМЕНТОВ</div>
            <h1>Делитесь текстом.<br /><em>Не личным.</em></h1>
            <p className={styles.lead}>Anon помогает скрыть персональные данные в документах и вернуть их обратно, когда это нужно.</p>
            <div className={styles.actions}><Link className={styles.primary} href="/register">Начать работу <ArrowRight size={18} /></Link>{onStart && <button className={styles.secondary} type="button" onClick={onStart}>Открыть интерфейс</button>}</div>
            <div className={styles.heroNote}><ShieldCheck size={17} /> Обратимое маскирование с ключом восстановления</div>
          </div>
          <div className={styles.demoWrap}>
            <div className={styles.orbit} />
            <div className={styles.demo}>
              <div className={styles.demoTop}><div className={styles.demoFile}><span className={styles.fileIcon}><FileText size={18} /></span><span><strong>Договор.docx</strong><small>Демонстрационный документ</small></span></div><span className={styles.demoDots}>•••</span></div>
              <div className={styles.paper} aria-hidden="true">
                <div className={styles.paperLabel}>ФРАГМЕНТ ДОКУМЕНТА</div><div className={styles.paperTitle}>Контактные данные</div>
                <div className={styles.paperLine}>ФИО: <span className={`${styles.swap} ${paused ? styles.paused : ""}`}><span>Иванов Иван Иванович</span><b>[PERSON_1]</b></span></div>
                <div className={styles.paperLine}>Телефон: <span className={`${styles.swap} ${paused ? styles.paused : ""}`}><span>+7 900 000-00-00</span><b>[PHONE_1]</b></span></div>
                <div className={styles.paperLine}>Email: <span className={`${styles.swap} ${paused ? styles.paused : ""}`}><span>ivan@example.com</span><b>[EMAIL_1]</b></span></div>
                <div className={styles.fakeLine} /><div className={`${styles.fakeLine} ${styles.shortLine}`} />
              </div>
              <p className={styles.srOnly}>Демонстрация: вымышленные имя, телефон и email заменяются плейсхолдерами PERSON_1, PHONE_1 и EMAIL_1.</p>
              <div className={styles.demoBottom}><span><span className={styles.statusDot} /> Пример маскирования</span><button type="button" onClick={() => setPaused(!paused)}>{paused ? "Продолжить" : "Пауза"}</button></div>
            </div>
            <div className={styles.floating}><LockKeyhole size={17} /><span>Ключ восстановления<small>Сохраните отдельно</small></span></div>
          </div>
          <a className={styles.scrollHint} href="#how"><ArrowDown size={15} /> Узнать больше</a>
        </section>

        <section className={styles.intro} id="how"><div><div className={styles.kicker}>ПОНЯТНЫЙ ПРОЦЕСС</div><h2>От документа к результату<br /><em>за три шага</em></h2></div><p>Работайте с содержанием документа, скрывая прямые идентификаторы. При необходимости исходные значения можно восстановить.</p></section>
        <section className={styles.steps} aria-label="Как работает Anon">
          <article><span className={styles.stepNum}>01 / ЗАГРУЗКА</span><div className={styles.stepIcon}><FileText size={23} /></div><h3>Добавьте документ</h3><p>Загрузите файл, с которым хотите работать. Сервис поддерживает документы, таблицы и текстовые форматы.</p></article>
          <article><span className={styles.stepNum}>02 / МАСКИРОВАНИЕ</span><div className={styles.stepIcon}><Sparkles size={23} /></div><h3>Проверьте результат</h3><p>Найденные данные заменяются плейсхолдерами. Просмотрите результат и проверьте его перед использованием.</p></article>
          <article><span className={styles.stepNum}>03 / ВОССТАНОВЛЕНИЕ</span><div className={styles.stepIcon}><LockKeyhole size={23} /></div><h3>Сохраните ключ</h3><p>Скачайте документ и mapping. Ключ позволит восстановить исходные значения без ИИ.</p></article>
        </section>

        <section className={styles.formats}><div><div className={styles.kicker}>ПРИВЫЧНЫЕ ФОРМАТЫ</div><h2>Ваши документы.<br /><em>Ваш рабочий процесс.</em></h2><p>Скачайте маскированный файл и ключ восстановления. Формат результата зависит от исходного файла и доступности преобразования.</p></div><div className={styles.formatList}>{["DOCX", "DOC", "XLSX", "XLS", "ODT", "RTF", "XML", "PDF", "TXT", "CSV", "MD", "JSON"].map(format => <span key={format}>.{format.toLowerCase()}</span>)}</div></section>

        <section className={styles.security} id="security"><div className={styles.securityIcon}><ShieldCheck size={30} /></div><div><div className={styles.kicker}>ОБРАЩЕНИЕ С ДАННЫМИ</div><h2>Контроль начинается<br /><em>с понимания</em></h2><p>Маскирование обратимо: mapping связывает плейсхолдеры с исходными значениями. Храните ключ отдельно от обработанного файла и ограничивайте доступ к нему.</p><p>Фронтенд и бэкенд размещаются в России, на инфраструктуре в Иркутске. Результат с сохранённым ключом остаётся персональными данными.</p></div></section>

        <section className={styles.faq} id="faq"><div><div className={styles.kicker}>ВОПРОСЫ И ОТВЕТЫ</div><h2>Полезно знать<br /><em>перед началом</em></h2></div><div className={styles.questions}><details><summary>Что такое mapping?</summary><p>Это таблица соответствий между плейсхолдерами и исходными значениями. Она нужна для восстановления документа.</p></details><details><summary>Можно ли восстановить исходный документ?</summary><p>Да, с помощью маскированного файла и его mapping. Восстановление выполняется без ИИ.</p></details><details><summary>Нужно ли проверять результат?</summary><p>Да. Перед передачей или использованием документа человек должен проверить, что нужные данные скрыты корректно.</p></details><details><summary>Это необратимое обезличивание?</summary><p>Нет. Пока существует mapping, данные можно восстановить. Это обратимое маскирование, или псевдонимизация.</p></details></div></section>

        <section className={styles.cta}><div><div className={styles.kicker}>НАЧНИТЕ РАБОТУ С ANON</div><h2>Меньше личного.<br /><em>Больше свободы в работе.</em></h2><p>Подготовьте документ к работе с помощью обратимого маскирования.</p></div><Link className={styles.primary} href="/register">Создать аккаунт <ArrowRight size={18} /></Link></section>
      </main>
      <footer className={styles.footer}><a className={styles.brand} href="#top"><span className={styles.brandMark}>a<span>.</span></span> anon</a><span>Маскирование данных с возможностью восстановления</span><div><Link href={LEGAL_DOCS.offer.href}>Оферта</Link><Link href={LEGAL_DOCS.privacy.href}>Конфиденциальность</Link><Link href="/legal">Правовая информация</Link></div></footer>
    </div>
  );
}
