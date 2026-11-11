"use client";

import { useId, useState } from "react";
import Link from "next/link";
import { ArrowRight, FileText, LockKeyhole, ShieldCheck, Sparkles } from "lucide-react";
import { LEGAL_DOCS } from "../lib/legal";
import OnPremiseLead from "./_components/OnPremiseLead";
import styles from "./landing.module.css";

const formats = [
  { name: "DOCX", description: "Документ Microsoft Word. Также открывается в LibreOffice Writer и Р7-Офис.", href: "/formats/docx" },
  { name: "DOC", description: "Прежний формат Microsoft Word. Для сохранения оформления может потребоваться преобразование.", href: "/formats/docx" },
  { name: "PDF", description: "Документ с фиксированной вёрсткой. Открывается в браузере и Adobe Acrobat Reader.", href: "/formats/pdf" },
  { name: "XLSX", description: "Книга Microsoft Excel. Также открывается в LibreOffice Calc и Р7-Офис.", href: "/formats/excel" },
  { name: "XLS", description: "Прежний формат таблиц Microsoft Excel. Может потребоваться преобразование в XLSX.", href: "/formats/excel" },
  { name: "ODT", description: "Текстовый документ OpenDocument. Используется в LibreOffice Writer и Р7-Офис.", href: "/formats/other#odt" },
  { name: "RTF", description: "Текст с оформлением для обмена между редакторами, например Microsoft Word и LibreOffice Writer.", href: "/formats/other#rtf" },
  { name: "TXT", description: "Обычный текст без оформления. Открывается в Блокноте и любом текстовом редакторе.", href: "/formats/other#text" },
  { name: "CSV", description: "Табличные данные с разделителями. Используется в Excel и LibreOffice Calc, обрабатывается как текст.", href: "/formats/other#text" },
  { name: "MD", description: "Текст с разметкой Markdown. Используется в Obsidian и Visual Studio Code.", href: "/formats/other#text" },
  { name: "JSON", description: "Структурированные данные для приложений и API. Открывается в Visual Studio Code, обрабатывается как текст.", href: "/formats/other#text" },
  { name: "XML", description: "Данные с тегами для обмена между программами. Открывается в браузере и Visual Studio Code.", href: "/formats/other#xml" },
];

function FormatButton({ format }: { format: typeof formats[number] }) {
  const [open, setOpen] = useState(false);
  const tooltipId = useId();
  return <div className={styles.formatItem} onMouseEnter={() => setOpen(true)}
    onMouseLeave={event => { if (!event.currentTarget.contains(document.activeElement)) setOpen(false); }}
    onFocus={() => setOpen(true)} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false); }}
    onKeyDown={event => { if (event.key === "Escape") setOpen(false); }}>
    <button type="button" aria-expanded={open} aria-controls={tooltipId} aria-describedby={tooltipId}
      onClick={() => setOpen(true)}>
      .{format.name.toLowerCase()}
    </button>
    <div className={styles.formatTooltip} id={tooltipId} data-open={open}>
      <strong>{format.name}</strong><p>{format.description}</p><Link href={format.href}>О формате и обработке →</Link>
    </div>
  </div>;
}

function FaqItem({ question, children }: { question: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const answerId = useId();

  return (
    <div className={styles.question}>
      <button
        className={styles.questionButton}
        type="button"
        aria-expanded={open}
        aria-controls={answerId}
        onClick={() => setOpen((value) => !value)}
      >
        {question}
        <span className={styles.questionIndicator} aria-hidden="true">+</span>
      </button>
      <div className={styles.answer} data-open={open} id={answerId} inert={!open}>
        <div className={styles.answerInner}>{children}</div>
      </div>
    </div>
  );
}

export default function Landing() {
  const [paused, setPaused] = useState(false);

  return (
    <div className={styles.landing}>
      <header className={styles.header}>
        <a className={styles.brand} href="#top" aria-label="AnonAI - на главную"><span className={styles.brandMark}>a<span>.</span></span> AnonAI</a>
        <nav className={styles.nav} aria-label="Навигация"><Link href="/guides">Как это работает</Link><a href="#security">О данных</a><Link href="/developers">API</Link><Link href="/pricing">Тарифы</Link><a href="#faq">FAQ</a><Link href="/on-premise">Внедрение</Link></nav>
        <div className={styles.headerActions}><Link href="/login">Войти</Link><Link className={styles.headerButton} href="/register">Регистрация <ArrowRight size={15} /></Link></div>
      </header>

      <main id="top">
        <section className={styles.hero}>
          <div className={styles.heroCopy}>
            <div className={styles.eyebrow}><span /> СЕРВИС МАСКИРОВАНИЯ ДОКУМЕНТОВ</div>
            <h1>Маскирование<br />персональных данных<br /><em>в документах.</em></h1>
            <p className={styles.lead}>Делитесь текстом, сохраняя контроль над личными данными. AnonAI заменяет найденные значения масками и позволяет вернуть их обратно по ключу восстановления.</p>
            <div className={styles.actions}><Link className={styles.primary} href="/register">Начать работу <ArrowRight size={18} /></Link><a className={styles.secondary} href="#on-premise">Внедрить в компании <ArrowRight size={18} /></a></div>
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
              <div className={styles.demoBottom}><span><span className={styles.statusDot} /> Пример маскирования</span><button type="button" aria-pressed={paused} aria-label="Приостановить демонстрацию" onClick={() => setPaused((value) => !value)}>{paused ? "Продолжить" : "Пауза"}</button></div>
            </div>
            <div className={styles.floating}><LockKeyhole size={17} /><span>Ключ восстановления<small>Сохраните отдельно</small></span></div>
          </div>
        </section>

        <section className={styles.intro} id="how"><div><div className={styles.kicker}>ПОНЯТНЫЙ ПРОЦЕСС</div><h2>От документа к результату<br /><em>за три шага</em></h2></div><p>Работайте с содержанием документа, скрывая прямые идентификаторы. При необходимости исходные значения можно восстановить.</p></section>
        <section className={styles.steps} aria-label="Как работает AnonAI">
          <article><div className={styles.stepIcon}><FileText size={23} /></div><h3>Добавьте документ</h3><p>Загрузите файл, с которым хотите работать. Сервис поддерживает документы, таблицы и текстовые форматы.</p></article>
          <article><div className={styles.stepIcon}><Sparkles size={23} /></div><h3>Проверьте результат</h3><p>Найденные данные заменяются плейсхолдерами. Просмотрите результат и проверьте его перед использованием.</p></article>
          <article><div className={styles.stepIcon}><LockKeyhole size={23} /></div><h3>Сохраните ключ</h3><p>Скачайте документ и ключ восстановления. Ключ позволит восстановить исходные значения без ИИ.</p></article>
        </section>

        <section className={styles.formats}><div><div className={styles.kicker}>ПРИВЫЧНЫЕ ФОРМАТЫ</div><h2>Ваши документы.<br /><em>Ваш рабочий процесс.</em></h2><p>Скачайте маскированный файл и ключ восстановления. Формат результата зависит от исходного файла и доступности преобразования.</p></div><div className={styles.formatList}>{formats.map(format => <FormatButton key={format.name} format={format} />)}</div></section>

        <section className={styles.resources} aria-labelledby="formats-guides-title">
          <div className={styles.kicker}>ФОРМАТЫ И ПРАКТИКА</div>
          <h2 id="formats-guides-title">Выберите свой<br /><em>рабочий сценарий</em></h2>
          <div className={styles.resourceGrid}>
            <Link href="/formats/docx"><h3>WORD</h3><p>Маскирование текста, таблиц и колонтитулов. Что проверить в готовом документе.</p><span>Подробнее о Word →</span></Link>
            <Link href="/formats/pdf"><h3>PDF</h3><p>Работа с текстовым слоем, ограничения сканов и возможный формат результата.</p><span>Подробнее о PDF →</span></Link>
            <Link href="/formats/excel"><h3>Excel</h3><p>Обработка текстовых ячеек, особенности XLS и XLSX, проверка результата.</p><span>Подробнее об Excel →</span></Link>
            <Link href="/formats/other"><h3>Другие форматы</h3><p>ODT, RTF, текст и данные: где используются и что учитывать при обработке.</p><span>Прочитать руководство →</span></Link>
          </div>
        </section>

        <section className={styles.security} id="security"><div className={styles.securityIcon}><ShieldCheck size={30} /></div><div><div className={styles.kicker}>ОБРАЩЕНИЕ С ДАННЫМИ</div><h2>Контроль начинается<br /><em>с понимания</em></h2><ul><li>Маскирование обратимо: ключ восстановления связывает плейсхолдеры с исходными значениями. Храните ключ отдельно от обработанного файла и ограничивайте доступ к нему.</li><li>Фронтенд и бэкенд размещаются в России, на инфраструктуре в Иркутске. Результат с сохранённым ключом остаётся персональными данными.</li></ul></div></section>

        <section className={styles.faq} id="faq"><div><div className={styles.kicker}>ВОПРОСЫ И ОТВЕТЫ</div><h2>Полезно знать<br /><em>перед началом</em></h2></div><div className={styles.questions}><FaqItem question="Что такое ключ восстановления?"><p>Это таблица соответствий между плейсхолдерами и исходными значениями. Она нужна для восстановления документа.</p></FaqItem><FaqItem question="Можно ли восстановить исходный документ?"><p>Да, с помощью маскированного файла и его ключа восстановления. Восстановление выполняется без ИИ.</p></FaqItem><FaqItem question="Нужно ли проверять результат?"><p>Да. Перед передачей или использованием документа человек должен проверить, что нужные данные скрыты корректно.</p></FaqItem><FaqItem question="Это необратимое обезличивание?"><p>Нет. Пока существует ключ восстановления, данные можно восстановить. Это обратимое маскирование, или псевдонимизация.</p></FaqItem></div></section>

        <OnPremiseLead />

        <section className={styles.cta}><div><div className={styles.kicker}>НАЧНИТЕ РАБОТУ С AnonAI</div><h2>Меньше личного.<br /><em>Больше свободы в работе.</em></h2><p>Подготовьте документ к работе с помощью обратимого маскирования.</p></div><Link className={`${styles.primary} ${styles.ctaButton}`} href="/register">Создать аккаунт <ArrowRight size={20} /></Link></section>
      </main>
      <footer className={styles.footer}><a className={styles.brand} href="#top"><span className={styles.brandMark}>a<span>.</span></span> AnonAI</a><span>Маскирование данных с возможностью восстановления</span><div><Link href="/guides">Руководства</Link><Link href="/pricing">Тарифы</Link><Link href="/on-premise">Внедрение</Link><Link href="/developers">API</Link><Link href={LEGAL_DOCS.offer.href}>Оферта</Link><Link href={LEGAL_DOCS.privacy.href}>Конфиденциальность</Link><Link href="/legal">Правовая информация</Link></div></footer>
    </div>
  );
}
