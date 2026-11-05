-- One-time 50% increase of subscription prices. Prisma records this migration
-- in _prisma_migrations, so subsequent deploys do not compound the increase.
-- Keep money in integer kopecks; half a kopeck rounds up. NULL annual prices
-- remain NULL. Free/service plans, quotas and previously created payments
-- (including pending bank orders) retain their existing values.
UPDATE "plans"
SET "price_kopecks" = ("price_kopecks" * 3 + 1) / 2,
    "price_year_kopecks" = CASE
      WHEN "price_year_kopecks" IS NULL THEN NULL
      ELSE ("price_year_kopecks" * 3 + 1) / 2
    END
WHERE "code" NOT IN ('trial', 'unlimited') AND "price_kopecks" > 0;
