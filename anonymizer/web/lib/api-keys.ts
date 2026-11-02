import { createHash, randomBytes } from "node:crypto";
import { prisma } from "@/lib/db";

const KEY_PATTERN = /^anon_([1-9][0-9]{0,15})_[A-Za-z0-9_-]{43}$/;

export function apiKeyAccountId(key: string): number | null {
  const match = KEY_PATTERN.exec(key);
  const id = match ? Number(match[1]) : NaN;
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

export function bearerApiKey(request: Request): string | null {
  const header = request.headers.get("authorization");
  if (!header || header.length > 128) return null;
  const match = /^Bearer (\S+)$/i.exec(header);
  return match && apiKeyAccountId(match[1]) != null ? match[1] : null;
}

export const hashApiKey = (key: string) => createHash("sha256").update(key, "utf8").digest("hex");

export function issueApiKey(accountId: number) {
  if (!Number.isSafeInteger(accountId) || accountId <= 0) throw new Error("Invalid account identity");
  const random = randomBytes(32).toString("base64url");
  const key = `anon_${accountId}_${random}`;
  return { key, keyHash: hashApiKey(key), prefix: `anon_${accountId}_${random.slice(0, 8)}…` };
}

/** Ключ связан с аккаунтом при выдаче: смена аккаунта пользователя его не переносит. */
export async function identifyApiKey(key: string): Promise<{ accountId: number; userId: number } | null> {
  const accountId = apiKeyAccountId(key);
  if (accountId == null) return null;
  const record = await prisma.apiKey.findUnique({
    where: { keyHash: hashApiKey(key) },
    select: {
      id: true, revokedAt: true,
      user: { select: { id: true, accountId: true, isActive: true, account: { select: { isActive: true } } } },
    },
  });
  if (!record || record.revokedAt || !record.user.isActive || !record.user.account.isActive ||
      record.user.accountId !== accountId) return null;
  // Повторная проверка revokedAt закрывает отзыв между чтением и отметкой использования.
  const used = await prisma.apiKey.updateMany({
    where: { id: record.id, revokedAt: null, user: { accountId, isActive: true, account: { isActive: true } } },
    data: { lastUsedAt: new Date() },
  });
  return used.count === 1 ? { accountId, userId: record.user.id } : null;
}
