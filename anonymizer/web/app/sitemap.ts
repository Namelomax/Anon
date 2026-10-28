import type { MetadataRoute } from "next";
import { LEGAL_DOCS } from "@/lib/legal";
import { SITE_URL } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: `${SITE_URL}/`, changeFrequency: "weekly", priority: 1 },
    { url: `${SITE_URL}/legal`, changeFrequency: "monthly", priority: 0.3 },
    { url: `${SITE_URL}${LEGAL_DOCS.offer.href}`, changeFrequency: "monthly", priority: 0.3 },
    { url: `${SITE_URL}${LEGAL_DOCS.privacy.href}`, changeFrequency: "monthly", priority: 0.3 },
  ];
}
