import { pageMetadata } from "@/lib/seo";
import { LEGAL_DOCS } from "@/lib/legal";
import { LegalDocument } from "../LegalDocument";

export const dynamic = "force-static";

export const metadata = pageMetadata(LEGAL_DOCS.offer.title, "Лицензионное соглашение Anon: условия использования, тарифы, оплата и ограничения сервиса маскирования документов.", LEGAL_DOCS.offer.href);

export default function OfferPage() {
  return <LegalDocument file="offer.html" pdf={LEGAL_DOCS.offer.pdf} />;
}
