import type { Metadata } from "next";
import Link from "next/link";
import { FileText } from "lucide-react";
import { LEGAL_DOCS, LICENSOR } from "@/lib/legal";

export const dynamic = "force-static";

export const metadata: Metadata = {
  title: "Правовая информация",
  alternates: { canonical: "/legal" },
};

const DOCS = [
  {
    ...LEGAL_DOCS.offer,
    about:
      "Условия использования сервиса: предмет лицензии, тарифы и оплата, возврат, поручение обработки персональных данных, ответственность сторон.",
  },
  {
    ...LEGAL_DOCS.privacy,
    about:
      "Какие персональные данные обрабатываются, на каком основании и сколько хранятся, кто привлекается к обработке и как реализуются права субъектов.",
  },
];

export default function LegalIndexPage() {
  return (
    <>
      <h1 className="legal-title">Правовая информация</h1>

      {DOCS.map((d) => (
        <section className="card legal-card" key={d.href}>
          <h2>
            <FileText size={17} />
            <Link href={d.href}>{d.title}</Link>
          </h2>
          <p className="note">{d.edition}</p>
          <p>{d.about}</p>
          <p className="legal-card-links">
            <Link href={d.href}>Читать</Link>
            <a href={d.pdf} download>
              Скачать PDF
            </a>
          </p>
        </section>
      ))}

      <section className="card legal-card">
        <h2>Сведения о Лицензиаре</h2>
        <dl className="legal-dl">
          <dt>Лицензиар</dt>
          <dd>{LICENSOR.fullName}</dd>
          <dt>ИНН</dt>
          <dd>{LICENSOR.inn}</dd>
          <dt>ОГРНИП</dt>
          <dd>{LICENSOR.ogrnip}</dd>
          <dt>Адрес</dt>
          <dd>{LICENSOR.address}</dd>
          <dt>Электронная почта</dt>
          <dd>
            <a href={`mailto:${LICENSOR.email}`}>{LICENSOR.email}</a>
          </dd>
          <dt>Телефон</dt>
          <dd>
            <a href={`tel:${LICENSOR.phoneHref}`}>{LICENSOR.phone}</a>
          </dd>
        </dl>
        <p className="note">
          Обращения по вопросам обработки персональных данных, в том числе запросы субъектов
          персональных данных, принимаются по электронной почте. Срок ответа — 10 рабочих дней.
        </p>
      </section>
    </>
  );
}
