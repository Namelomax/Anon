import type { Metadata } from "next";
import "./globals.css";
import { Providers } from "./providers";
import ChunkErrorReload from "./_components/ChunkErrorReload";
import { LICENSOR } from "@/lib/legal";
import { SITE_DESCRIPTION, SITE_NAME, SITE_TITLE, SITE_URL } from "@/lib/site";
import { pageMetadata } from "@/lib/seo";

export const metadata: Metadata = {
  ...pageMetadata(SITE_TITLE, SITE_DESCRIPTION, "/"),
  metadataBase: new URL(SITE_URL),
  title: { default: SITE_TITLE, template: `%s - ${SITE_NAME}` },
  description: SITE_DESCRIPTION,
  applicationName: SITE_NAME,
  icons: {
    icon: { url: "/favicon.png", type: "image/png", sizes: "120x120" },
  },
  // Canonical задаётся страницами: дочерние URL не должны наследовать адрес главной.
  alternates: undefined,
  verification: {
    google: process.env.GOOGLE_SITE_VERIFICATION?.trim() || undefined,
    yandex: process.env.YANDEX_SITE_VERIFICATION?.trim() || undefined,
  },
};

// Schema.org для сниппетов Яндекса и Google: что это за сервис и кто оператор.
const JSON_LD = {
  "@context": "https://schema.org",
  "@type": "WebApplication",
  name: SITE_NAME,
  url: SITE_URL,
  description: SITE_DESCRIPTION,
  applicationCategory: "BusinessApplication",
  operatingSystem: "Web",
  inLanguage: "ru",
  provider: {
    "@type": "Organization",
    name: LICENSOR.shortName,
    email: LICENSOR.email,
    telephone: LICENSOR.phoneHref,
  },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ru">
      <body>
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{ __html: JSON.stringify(JSON_LD) }}
        />
        <Providers>{children}</Providers>
        <ChunkErrorReload />
      </body>
    </html>
  );
}
