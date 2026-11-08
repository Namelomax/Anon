// Канонический адрес и описание сайта для SEO: metadataBase, sitemap.xml,
// robots.txt, Open Graph и Schema.org. Не путать с AUTH_URL — тот может
// указывать на тестовый поддомен, а поисковикам нужен один боевой адрес.
export const SITE_URL = (process.env.SITE_URL?.trim() || "https://anonai.ru").replace(/\/$/, "");

export const SITE_NAME = "Anon";

export const SITE_TITLE = "Маскирование персональных данных в документах онлайн";

export const SITE_DESCRIPTION =
  "Маскирование персональных данных в документах: ФИО, паспорта, " +
  "телефоны, адреса и реквизиты заменяются масками, а ключ позволяет вернуть исходный текст.";
