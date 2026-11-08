import type { MetadataRoute } from "next";
import { SITE_URL } from "@/lib/site";

// Страницы с noindex доступны роботам, чтобы они могли прочитать директиву.
export default function robots(): MetadataRoute.Robots {
  return {
    rules: [{ userAgent: "*", allow: "/", disallow: ["/api/", "/invite/"] }],
    sitemap: `${SITE_URL}/sitemap.xml`,
    host: SITE_URL,
  };
}
