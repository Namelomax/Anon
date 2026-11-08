import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { getQuotaState } from "@/lib/quota";
import { requireRoot } from "@/lib/root-admin";

export const runtime = "nodejs";
const PAGE_SIZE = 25;
const MAX_INT = 2_147_483_647;
const json = (body: unknown, status = 200) => NextResponse.json(
  body, { status, headers: { "Cache-Control": "no-store" } },
);

function sameOrigin(request: NextRequest) {
  if (request.headers.get("sec-fetch-site") === "cross-site") return false;
  try {
    const target = new URL(process.env.AUTH_URL?.trim() || request.url);
    return ["http:", "https:"].includes(target.protocol) &&
      request.headers.get("origin") === target.origin;
  } catch { return false; }
}

export async function GET(request: NextRequest) {
  try {
    const caller = await requireRoot();
    if (caller instanceof NextResponse) return caller;
    const query = new URL(request.url).searchParams;
    const rawQuery = query.get("q") || "";
    const q = rawQuery.trim();
    const pageText = query.get("page") ?? "1";
    const page = Number(pageText);
    if (query.getAll("q").length > 1 || query.getAll("page").length > 1 ||
        rawQuery.length > 200 || !/^[1-9]\d*$/.test(pageText) ||
        !Number.isSafeInteger(page) || page > Math.floor(MAX_INT / PAGE_SIZE)) {
      return json({ error: "Некорректные параметры поиска." }, 400);
    }
    const where = q ? { OR: [
      { name: { contains: q } },
      { users: { some: { email: { contains: q } } } },
    ] } : {};
    const [total, rows] = await Promise.all([
      prisma.account.count({ where }),
      prisma.account.findMany({
        where, skip: (page - 1) * PAGE_SIZE, take: PAGE_SIZE, orderBy: { id: "desc" },
        select: {
          id: true, name: true, isActive: true, pageLimitOverrideTenths: true,
          plan: { select: { code: true, title: true, pagesPerMonth: true } },
          users: { select: { id: true, email: true, role: true, isActive: true }, orderBy: { id: "asc" }, take: 100 },
          _count: { select: { users: true } },
        },
      }),
    ]);
    const accounts = [];
    for (const row of rows) {
      // Тот же расчёт, который разрешает обработку документов и показывает
      // остаток в кабинете: учитывает текущий период, гранты и истечение срока.
      const state = await getQuotaState(row.id);
      if (!state) continue; // Аккаунт мог быть удалён между чтениями.
      const { _count, ...account } = row;
      accounts.push({
        ...account, userCount: _count.users,
        quota: {
          period: state.period, usedTenths: state.usedTenths,
          limitTenths: state.limitTenths, grantsTenths: state.grantsTenths,
          allowanceTenths: state.allowanceTenths, remainingTenths: state.remainingTenths,
          expired: state.expired, paidUntil: state.paidUntil,
        },
      });
    }
    return json({ accounts, page, pageSize: PAGE_SIZE, total });
  } catch { return json({ error: "Не удалось загрузить аккаунты." }, 503); }
}

export async function PATCH(request: NextRequest) {
  if (!sameOrigin(request)) return json({ error: "Недопустимый источник запроса." }, 403);
  try {
    const caller = await requireRoot();
    if (caller instanceof NextResponse) return caller;
    let body: unknown;
    try { body = await request.json(); } catch { return json({ error: "Некорректный JSON." }, 400); }
    if (!body || typeof body !== "object" || Array.isArray(body) ||
        Object.keys(body).some(key => !["accountId", "pageLimitOverrideTenths", "paidUntil"].includes(key))) {
      return json({ error: "Укажите аккаунт и изменения лимита или срока доступа." }, 400);
    }
    const fields = body as Record<string, unknown>;
    const { accountId, pageLimitOverrideTenths, paidUntil } = fields;
    const hasLimit = Object.prototype.hasOwnProperty.call(fields, "pageLimitOverrideTenths");
    const hasExpiry = Object.prototype.hasOwnProperty.call(fields, "paidUntil");
    if (typeof accountId !== "number" || !Number.isInteger(accountId) || accountId <= 0 || accountId > MAX_INT ||
        (!hasLimit && !hasExpiry)) {
      return json({ error: "Укажите аккаунт и изменения лимита или срока доступа." }, 400);
    }
    if (hasLimit && pageLimitOverrideTenths !== null &&
          (typeof pageLimitOverrideTenths !== "number" || !Number.isInteger(pageLimitOverrideTenths) ||
           pageLimitOverrideTenths < 0 || pageLimitOverrideTenths > MAX_INT)) {
      return json({ error: "Лимит должен быть целым числом десятых долей страницы от 0 до 2147483647; null возвращает лимит тарифа." }, 400);
    }
    const data: { pageLimitOverrideTenths?: number | null; paidUntil?: Date | null } = {};
    if (hasLimit) data.pageLimitOverrideTenths = pageLimitOverrideTenths as number | null;
    if (hasExpiry) {
      if (paidUntil === null) {
        data.paidUntil = null;
      } else {
        const date = typeof paidUntil === "string" &&
          /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(paidUntil)
          ? new Date(paidUntil) : null;
        if (!date || Number.isNaN(date.getTime()) || date.getUTCFullYear() < 1 || date.toISOString() !== paidUntil) {
          return json({ error: "Укажите корректные дату и время окончания в UTC; null снимает ограничение срока." }, 400);
        }
        data.paidUntil = date;
      }
    }
    // Частичное изменение без платежа. Не меняем тариф, якорь расчётных
    // периодов, расход и гранты. Продление истёкшего срока продолжает прежний
    // график квот; у аккаунта без якоря остаются календарные месяцы по UTC.
    const updated = await prisma.account.updateMany({
      where: { id: accountId }, data,
    });
    if (!updated.count) return json({ error: "Аккаунт не найден." }, 404);
    return json({ ok: true });
  } catch { return json({ error: "Не удалось сохранить изменения аккаунта." }, 503); }
}
