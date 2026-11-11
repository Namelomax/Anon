import Link from "next/link";
import PublicPage from "@/app/_components/PublicPage";
import { pageMetadata } from "@/lib/seo";

export const metadata = pageMetadata("Как работает AnonAI", "Как маскировать данные, проверить результат и восстановить исходные значения по ключу.", "/guides");

export default function GuidesPage() {
  return <PublicPage title="Как работает AnonAI" intro="AnonAI заменяет найденные данные плейсхолдерами и сохраняет соответствия для восстановления.">
    <h2>Порядок работы</h2><ol><li>Загрузите текст или документ и запустите маскирование.</li><li>Проверьте результат вручную: автоматический поиск может пропустить данные, в том числе на изображениях.</li><li>Сохраните таблицу соответствий mapping как ключ восстановления отдельно от маскированного материала и ограничьте доступ к ней.</li><li>При необходимости восстановите исходные значения по ключу без ИИ и проверьте результат.</li></ol>
    <h2>Руководства по форматам</h2><ul><li><Link href="/formats/docx">WORD</Link></li><li><Link href="/formats/pdf">PDF</Link></li><li><Link href="/formats/excel">Excel</Link></li><li><Link href="/formats/other">Другие форматы</Link></li></ul>
    <p>Подробнее о <Link href="/guides/pseudonymization-and-ai">псевдонимизации перед работой с ИИ</Link>.</p>
  </PublicPage>;
}
