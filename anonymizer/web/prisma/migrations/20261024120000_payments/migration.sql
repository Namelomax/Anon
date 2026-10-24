-- CreateTable
CREATE TABLE "payments" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "order_id" TEXT NOT NULL,
    "account_id" INTEGER NOT NULL,
    "user_id" INTEGER,
    "plan_code" TEXT NOT NULL,
    "months" INTEGER NOT NULL,
    "amount_kopecks" INTEGER NOT NULL,
    "status" TEXT NOT NULL,
    "tbank_payment_id" TEXT,
    "payment_url" TEXT,
    "error_code" TEXT,
    "activated_at" DATETIME,
    "created_at" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "updated_at" DATETIME NOT NULL,
    CONSTRAINT "payments_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "accounts" ("id") ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT "payments_user_id_fkey" FOREIGN KEY ("user_id") REFERENCES "users" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

-- CreateIndex
CREATE UNIQUE INDEX "payments_order_id_key" ON "payments"("order_id");

-- CreateIndex
CREATE INDEX "payments_account_id_idx" ON "payments"("account_id");
