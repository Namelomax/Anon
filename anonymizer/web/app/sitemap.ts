import type { MetadataRoute } from "next";
import { LEGAL_DOCS } from "@/lib/legal";
import { SITE_URL } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    "/", "/pricing", "/on-premise", "/formats/docx", "/formats/pdf",
    "/formats/excel", "/guides/pseudonymization-and-ai", "/developers",
    "/legal", LEGAL_DOCS.offer.href, LEGAL_DOCS.privacy.href,
  ].map((path) => ({ url: `${SITE_URL}${path}` }));
}
