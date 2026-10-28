import type { Metadata } from "next";
import { LEGAL_DOCS } from "@/lib/legal";
import { LegalDocument } from "../LegalDocument";

export const dynamic = "force-static";

export const metadata: Metadata = {
  title: LEGAL_DOCS.offer.title,
  alternates: { canonical: LEGAL_DOCS.offer.href },
};

export default function OfferPage() {
  return <LegalDocument file="offer.html" pdf={LEGAL_DOCS.offer.pdf} />;
}
