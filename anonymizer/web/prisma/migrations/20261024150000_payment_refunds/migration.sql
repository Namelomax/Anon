-- AlterTable
ALTER TABLE "payments" ADD COLUMN "access_until" DATETIME;
ALTER TABLE "payments" ADD COLUMN "refunded_kopecks" INTEGER NOT NULL DEFAULT 0;
