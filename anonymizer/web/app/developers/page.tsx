import { pageMetadata } from "@/lib/seo";
import Link from "next/link";
import styles from "./developers.module.css";

export const metadata = pageMetadata(
  "API маскирования персональных данных",
  "Подключение API Anon: ключи доступа, маскирование документов, опрос задач и восстановление с помощью mapping.",
  "/developers",
);

const submit = `curl -X POST "$ANON_BASE_URL/api/v1/anonymize" \\
  -H "Authorization: Bearer $ANON_API_KEY" \\
  -F "file=@sample.txt" \\
  -F 'stages={"regex":true}'`;
const poll = `curl -G "$ANON_BASE_URL/api/v1/anonymize" \\
  -H "Authorization: Bearer $ANON_API_KEY" \\
  --data-urlencode "jobId=$JOB_ID"`;
const cancel = `curl -X DELETE -G "$ANON_BASE_URL/api/v1/anonymize" \\
  -H "Authorization: Bearer $ANON_API_KEY" \\
  --data-urlencode "jobId=$JOB_ID"`;
const restore = `curl -X POST "$ANON_BASE_URL/api/v1/deanonymize" \\
  -H "Authorization: Bearer $ANON_API_KEY" \\
  -F "file=@sample.anon.txt" \\
  -F 'mapping={"[PERSON_1]":"Пример"}'`;

export default function DevelopersPage() {
  return <div className={styles.page}>
    <header className={styles.header}><Link href="/" className={styles.brand}>Anon<span>.</span></Link><Link href="/account/developers" className={styles.headerLink}>Управление ключами →</Link></header>
    <main className={styles.main}>
      <div className={styles.hero}><p className={styles.eyebrow}>Anon API / v1</p><h1>Маскирование документов в вашем приложении</h1><p className={styles.lead}>Ставьте документы в очередь, получайте результат и восстанавливайте исходные данные с помощью mapping.</p><Link className={styles.button} href="/account/developers">Получить API-ключ →</Link></div>
      <div className={styles.layout}><nav className={styles.nav} aria-label="Разделы документации"><a href="#auth">Авторизация</a><a href="#mask">Маскирование</a><a href="#restore">Восстановление</a><a href="#errors">Ошибки и квота</a></nav>
      <div className={styles.content}>
        <section id="auth"><p className={styles.number}>01 / Начало работы</p><h2>Авторизация</h2><p>Администратор или владелец аккаунта создаёт и отзывает ключи в <Link href="/account/developers">разделе для разработчиков</Link>. Секрет показывается один раз и не может быть получен повторно. На аккаунт допускается до 20 активных ключей. Сохраните его в защищённом хранилище и передавайте в каждом запросе заголовком <code>Authorization: Bearer</code>.</p><p>Ключ привязан к аккаунту и пользователю; после отзыва он сразу перестаёт действовать. Аккаунт и пользователь должны быть активны. При переводе пользователя в другой аккаунт прежний ключ недействителен. Сессия браузера не заменяет ключ.</p><div className={styles.note}>Не помещайте ключ в URL, клиентский код или репозиторий. Примеры ниже используют переменные окружения Bash <code>ANON_BASE_URL</code> и <code>ANON_API_KEY</code>; задайте их самостоятельно. Команды приведены для примера и не выполняются этой страницей.</div></section>
        <section id="mask"><p className={styles.number}>02 / Задачи</p><h2>Маскирование</h2><p><code>POST /api/v1/anonymize</code> принимает multipart-поле <code>file</code> и необязательное поле <code>stages</code> с JSON-объектом флагов: <code>regex</code>, <code>corporate</code>, <code>ner</code>, <code>llm</code>, <code>review</code>, <code>subject</code>. Неуказанные этапы используют настройки сервера.</p><pre><code>{submit}</code></pre><p>Ответ <code>202</code>: <code>{'{"jobId":"…","done":false}'}</code>. Сохраните идентификатор как <code>JOB_ID</code> и опрашивайте задачу, например раз в 1–2 секунды:</p><pre><code>{poll}</code></pre><p>Пока обработка идёт, ответ содержит <code>{'{"done":false}'}</code>. После завершения — <code>done: true</code>, а также <code>anonymized_text</code>, <code>mapping</code>, <code>summary</code>, <code>spans</code>, <code>document_base64</code>, <code>document_name</code> и <code>document_mime</code>. Отменённая задача возвращает <code>{'{"done":false,"cancelled":true}'}</code>.</p><p>Опрос и отмена доступны пользователю, создавшему задачу, в том числе с другого его ключа. Другие пользователи того же аккаунта не являются владельцами задачи.</p><h3>Отмена</h3><pre><code>{cancel}</code></pre><p>Отмена кооперативная: выполняющийся этап может остановиться не сразу. Уже завершённая задача сохраняет своё состояние.</p></section>
        <section id="restore"><p className={styles.number}>03 / Обратная замена</p><h2>Восстановление</h2><p><code>POST /api/v1/deanonymize</code> принимает multipart-поля <code>file</code> и <code>mapping</code> (JSON-строка). Также можно отправить JSON с полями <code>filename</code>, <code>file_base64</code> и объектом <code>mapping</code>.</p><pre><code>{restore}</code></pre><p>Ответ содержит <code>restored_text</code>, <code>leftover</code> (плейсхолдеры без значений), <code>document_base64</code>, <code>document_name</code> и <code>document_mime</code>. Восстановление детерминированное, без ИИ. Храните mapping как чувствительные данные: он позволяет восстановить исходный документ.</p></section>
        <section id="errors"><p className={styles.number}>04 / Эксплуатация</p><h2>Ошибки и квота</h2><div className={styles.status}><p><strong>400</strong> — некорректный запрос или отсутствует обязательное поле.</p><p><strong>401</strong> — ключ отсутствует или недействителен.</p><p><strong>402</strong> — квота аккаунта исчерпана.</p><p><strong>403</strong> — доступ запрещён.</p><p><strong>404</strong> — задача не найдена или недоступна пользователю; задача также может исчезнуть после истечения срока хранения или перезапуска.</p><p><strong>5xx</strong> — ошибка обработки или связи с сервисом.</p></div><p>Квота маскирования общая с сайтом. Перед постановкой проверяется, не исчерпана ли она; страницы заранее не резервируются, а расход учитывается отдельным импортом. Это не жёсткое ограничение параллельных запросов. Восстановление квоту не расходует.</p></section>
      </div></div>
    </main><footer className={styles.footer}><Link href="/">Anon</Link><span>Обратимое маскирование документов</span><Link href="/legal">Правовая информация</Link></footer>
  </div>;
}
