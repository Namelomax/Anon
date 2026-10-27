import type { Metadata } from "next";
import { LEGAL_DOCS } from "@/lib/legal";
import { LegalDocument } from "../LegalDocument";

export const dynamic = "force-static";

export const metadata: Metadata = { title: `${LEGAL_DOCS.privacy.title} — Анонимизатор` };

export default function PrivacyPage() {
  return <LegalDocument file="privacy.html" pdf={LEGAL_DOCS.privacy.pdf} />;
}
