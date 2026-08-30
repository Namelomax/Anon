import { PrismaClient } from "@prisma/client";

/**
 * Синглтон PrismaClient в стандартной для Next.js форме: в dev-режиме Next
 * перезагружает модули при каждом HMR-обновлении, и без кеша на `globalThis`
 * каждый такой reload плодил бы новое соединение с БД (и новое SQLite-
 * подключение, до исчерпания лимита файловых дескрипторов на долгой сессии
 * разработки). В проде модуль грузится один раз, кеш просто не пригождается.
 */
const globalForPrisma = globalThis as unknown as {
  prisma: PrismaClient | undefined;
};

function createPrismaClient(): PrismaClient {
  const client = new PrismaClient();

  // WAL — обязателен: без него запись (списание квоты/учёт использования)
  // блокирует ЧТЕНИЕ на всё время транзакции, а фронтенд в это время
  // поллит статус job'ы — без WAL этот поллинг будет спотыкаться на каждом
  // списании. busy_timeout заставляет SQLite подождать и повторить попытку
  // вместо немедленного SQLITE_BUSY при параллельных запросах, вместо того
  // чтобы ронять запрос с ошибкой блокировки.
  client.$queryRaw`PRAGMA journal_mode=WAL;`.catch((err) => {
    console.error("[db] не удалось включить WAL:", err);
  });
  client.$queryRaw`PRAGMA busy_timeout=5000;`.catch((err) => {
    console.error("[db] не удалось выставить busy_timeout:", err);
  });

  return client;
}

export const prisma: PrismaClient = globalForPrisma.prisma ?? createPrismaClient();

if (process.env.NODE_ENV !== "production") {
  globalForPrisma.prisma = prisma;
}
