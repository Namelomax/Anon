import { handlers } from "@/auth";

// Стандартный catch-all route-хендлер Auth.js v5 для App Router — вся логика
// (провайдер, callbacks, JWT) живёт в auth.ts, здесь только реэкспорт.
export const { GET, POST } = handlers;
