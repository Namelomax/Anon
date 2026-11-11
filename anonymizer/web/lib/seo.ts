import type { Metadata } from "next";
import { SITE_NAME, SITE_URL } from "@/lib/site";

/** Метаданные конкретной публичной страницы: canonical не наследуется от главной. */
export function pageMetadata(title: string, description: string, path: string): Metadata {
  const url = new URL(path, `${SITE_URL}/`).toString();
  return {
    title,
    description,
    alternates: { canonical: url },
    openGraph: {
      type: "website",
      locale: "ru_RU",
      siteName: SITE_NAME,
      title,
      description,
      url,
      images: [{ url: "/opengraph-image", width: 1200, height: 630, alt: "AnonAI — маскирование персональных данных" }],
    },
    twitter: { card: "summary_large_image", title, description, images: ["/opengraph-image"] },
  };
}
