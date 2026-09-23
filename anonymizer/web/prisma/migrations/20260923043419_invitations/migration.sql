-- CreateTable
CREATE TABLE "invitations" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    "account_id" INTEGER NOT NULL,
    "email" TEXT NOT NULL,
    "role" TEXT NOT NULL,
    "token_hash" TEXT NOT NULL,
    "created_by_user_id" INTEGER,
    "expires_at" DATETIME NOT NULL,
    "accepted_at" DATETIME,
    "accepted_by_user_id" INTEGER,
    "revoked_at" DATETIME,
    "created_at" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "invitations_account_id_fkey" FOREIGN KEY ("account_id") REFERENCES "accounts" ("id") ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT "invitations_created_by_user_id_fkey" FOREIGN KEY ("created_by_user_id") REFERENCES "users" ("id") ON DELETE SET NULL ON UPDATE CASCADE,
    CONSTRAINT "invitations_accepted_by_user_id_fkey" FOREIGN KEY ("accepted_by_user_id") REFERENCES "users" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

-- CreateIndex
CREATE UNIQUE INDEX "invitations_token_hash_key" ON "invitations"("token_hash");

-- CreateIndex
CREATE UNIQUE INDEX "invitations_accepted_by_user_id_key" ON "invitations"("accepted_by_user_id");

-- CreateIndex
CREATE INDEX "invitations_account_id_idx" ON "invitations"("account_id");
