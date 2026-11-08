import { pageMetadata } from "@/lib/seo";
import { LEGAL_DOCS } from "@/lib/legal";
import { LegalDocument } from "../LegalDocument";

export const dynamic = "force-static";

export const metadata = pageMetadata(LEGAL_DOCS.privacy.title, "Политика обработки персональных данных Anon: документы пользователей, учётные записи, хранение и конфиденциальность.", LEGAL_DOCS.privacy.href);

export default function PrivacyPage() {
  return <LegalDocument file="privacy.html" pdf={LEGAL_DOCS.privacy.pdf} />;
}
