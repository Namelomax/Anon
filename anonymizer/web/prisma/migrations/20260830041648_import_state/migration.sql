-- CreateTable
CREATE TABLE "import_state" (
    "id" INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT DEFAULT 1,
    "offset_bytes" INTEGER NOT NULL DEFAULT 0,
    "updated_at" DATETIME NOT NULL
);
