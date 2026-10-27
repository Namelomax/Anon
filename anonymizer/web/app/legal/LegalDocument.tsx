import { readFileSync } from "node:fs";
import path from "node:path";
import { Download } from "lucide-react";

// Текст документа — HTML, собранный scripts/build_legal.py из оферты (Word) и
// Политики (Markdown). Содержимое наше и доверенное, поэтому вставляется как
// есть. Читается при сборке: страницы раздела статические.
export function LegalDocument({ file, pdf }: { file: string; pdf: string }) {
  const html = readFileSync(path.join(process.cwd(), "content", "legal", file), "utf-8");
  return (
    <article className="legal-doc">
      <p className="legal-download">
        <a href={pdf} download>
          <Download size={15} />
          Скачать PDF
        </a>
      </p>
      <div dangerouslySetInnerHTML={{ __html: html }} />
    </article>
  );
}
