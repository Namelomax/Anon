import { randomBytes } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";
import { publicOrigin } from "@/lib/invitations";
import { initPayment, tbankConfig } from "@/lib/tbank";

export const runtime = "nodejs";

/** Платить за аккаунт может только его админ (или root) — как и управлять составом. */
const MANAGER_ROLES = new Set(["root", "admin"]);

/** Служебные планы не продаются. */
const NOT_FOR_SALE = new Set(["unlimited", "trial"]);

/**
 * POST /api/billing/checkout { plan, months } → { url } — ссылка на платёжную
 * форму Т-Банка (см. lib/tbank.ts).
 *
 * Аккаунт, пользователь и роль — из СЕССИИ, сумма — из Plan в БД: из тела
 * запроса берутся только код плана и срок, иначе клиент назначил бы себе цену
 * сам (документация банка прямо требует не передавать сумму с фронтенда).
 * Тариф здесь НЕ включается — только по уведомлению банка (../notify).
 */
export async function POST(req: NextRequest) {
  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;
  const accountId = typeof session?.accountId === "number" ? session.accountId : null;
  const role = typeof session?.role === "string" ? session.role : "";
  if (userId == null || accountId == null || !Number.isFinite(userId)) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }
  if (!MANAGER_ROLES.has(role)) {
    return NextResponse.json(
      { error: "Оплатить тариф может только администратор аккаунта." },
      { status: 403 },
    );
  }

  const cfg = tbankConfig();
  if (!cfg) {
    return NextResponse.json({ error: "Онлайн-оплата не настроена." }, { status: 503 });
  }

  let body: { plan?: unknown; months?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }
  const planCode = typeof body.plan === "string" ? body.plan : "";
  const months = body.months === 12 ? 12 : body.months === 1 ? 1 : null;
  if (!months) {
    return NextResponse.json({ error: "Срок оплаты — 1 или 12 месяцев." }, { status: 400 });
  }

  const plan = await prisma.plan.findUnique({ where: { code: planCode } });
  if (!plan || NOT_FOR_SALE.has(plan.code)) {
    return NextResponse.json({ error: "Такой тариф не продаётся." }, { status: 400 });
  }
  const amountKopecks = months === 12 ? plan.priceYearKopecks : plan.priceKopecks;
  if (!amountKopecks || amountKopecks <= 0) {
    return NextResponse.json(
      { error: `Для тарифа «${plan.title}» нет цены за ${months} мес.` },
      { status: 400 },
    );
  }

  const user = await prisma.user.findUnique({ where: { id: userId } });
  if (!user || user.accountId !== accountId || !user.isActive) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }

  // OrderId обязан быть уникален на каждую попытку и не длиннее 50 символов;
  // случайный хвост — чтобы номер заказа не выдавал число продаж.
  const orderId = `a${accountId}-${Date.now().toString(36)}-${randomBytes(4).toString("hex")}`;
  const payment = await prisma.payment.create({
    data: {
      orderId,
      accountId,
      userId,
      planCode: plan.code,
      months,
      amountKopecks,
      status: "NEW",
    },
  });

  const origin = publicOrigin(req);
  const period = months === 12 ? "12 месяцев" : "1 месяц";
  try {
    const res = await initPayment(cfg, {
      orderId,
      amountKopecks,
      description: `Тариф «${plan.title}», ${period}`,
      itemName: `Доступ к сервису, тариф «${plan.title}», ${period}`,
      email: user.email,
      // Возврат на страницу результата (app/payment/result): она ждёт
      // уведомления банка и через 5 секунд уводит в кабинет.
      successUrl: `${origin}/payment/result?order=${orderId}`,
      failUrl: `${origin}/payment/result?order=${orderId}&fail=1`,
      notificationUrl: `${origin}/api/billing/notify`,
    });
    if (!res.Success || !res.PaymentURL) {
      await prisma.payment.update({
        where: { id: payment.id },
        data: { status: "INIT_FAILED", errorCode: String(res.ErrorCode ?? "") },
      });
      console.error(
        `[billing] Init отклонён: заказ ${orderId}, код ${res.ErrorCode}: ${res.Message ?? ""} ${res.Details ?? ""}`,
      );
      return NextResponse.json(
        { error: `Банк не принял платёж (код ${res.ErrorCode}). Попробуйте позже.` },
        { status: 502 },
      );
    }
    await prisma.payment.update({
      where: { id: payment.id },
      data: {
        status: res.Status ?? "NEW",
        tbankPaymentId: res.PaymentId != null ? String(res.PaymentId) : null,
        paymentUrl: res.PaymentURL,
      },
    });
    console.log(
      `[billing] платёж создан: заказ ${orderId}, платёж ${res.PaymentId}, аккаунт ${accountId}, ` +
        `${plan.code} на ${months} мес., ${amountKopecks} коп.`,
    );
    return NextResponse.json({ url: res.PaymentURL });
  } catch (e) {
    await prisma.payment.update({ where: { id: payment.id }, data: { status: "INIT_FAILED" } });
    console.error(`[billing] Init не выполнен: заказ ${orderId}:`, e);
    return NextResponse.json(
      { error: "Не удалось связаться с банком. Попробуйте позже." },
      { status: 502 },
    );
  }
}
