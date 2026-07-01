import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Анонимизатор ПДн",
  description: "Загрузите документ — получите обезличенную версию и ключ восстановления.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ru">
      <body>{children}</body>
    </html>
  );
}
