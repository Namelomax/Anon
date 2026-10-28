import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";

// Служебные разделы закрыты от индексации: API, приглашения и формы входа
// поисковику показывать незачем.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", allow: "/", disallow: ["/api/", "/invite/", "/login", "/register"] }],
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
