-- AlterTable
ALTER TABLE "accounts" ADD COLUMN "period_anchor_at" DATETIME;
ALTER TABLE "accounts" ADD COLUMN "paid_until" DATETIME;

-- Backfill. Prisma пишет DateTime в SQLite целым числом миллисекунд с эпохи
-- (а не текстом, как CURRENT_TIMESTAMP), поэтому значения кладём в том же
-- представлении: миллисекунды считаем из julianday('now').
-- Аккаунты на 'unlimited' остаются с NULL/NULL (срока нет). Все остальные
-- (trial и любой иной план) получают свежий календарный месяц с момента деплоя, чтобы
-- существующие тестеры не оказались заблокированы мгновенно. '+1 month' в
-- SQLite при отсутствии дня переносит в следующий месяц (31.01 -> 03.03), а не
-- клампит, как addMonthsClamped в lib/period.ts, — для разового бэкфилла
-- расхождение в пару дней допустимо.
UPDATE "accounts"
SET "period_anchor_at" = CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER),
    "paid_until"       = CAST((julianday('now', '+1 month') - 2440587.5) * 86400000 AS INTEGER)
WHERE "plan_id" NOT IN (SELECT "id" FROM "plans" WHERE "code" = 'unlimited');

-- Лимит пользователей на аккаунт (решение владельца сервиса): пробный и «Тест» —
-- один пользователь (приглашения недоступны), «Бизнес»/«Корпоратив» и служебный
-- unlimited — без ограничения (NULL в max_users). Сид не меняет уже существующие
-- строки (upsert с update: {}), поэтому на живой базе выставляем здесь.
UPDATE "plans" SET "max_users" = NULL WHERE "code" NOT IN ('trial', 'test');
UPDATE "plans" SET "max_users" = 1 WHERE "code" IN ('trial', 'test');

-- Цена за год (NULL = годовой оплаты нет). Деньги — целые копейки.
ALTER TABLE "plans" ADD COLUMN "price_year_kopecks" INTEGER;

-- Платные тарифы. Вставляем, только если кода ещё нет (уникальный индекс
-- plans_code_key); created_at — миллисекунды с эпохи, как пишет Prisma.
INSERT OR IGNORE INTO "plans" ("code", "title", "pages_per_month", "max_users", "price_kopecks", "price_year_kopecks", "created_at")
VALUES
  ('test',      'Тест',       500,   1,    50000,  550000,  CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
  ('business',  'Бизнес',     5000,  NULL, 450000, 5200000, CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)),
  ('corporate', 'Корпоратив', 10000, NULL, 800000, 9200000, CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER));
