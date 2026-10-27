import { NextRequest, NextResponse } from "next/server";
import { hash } from "@node-rs/argon2";
import { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { addMonthsClamped } from "@/lib/period";
import { LEGAL_DOCS_VERSION } from "@/lib/legal";
import { isValidEmail, validatePasswordStrength } from "@/lib/password";

export const runtime = "nodejs";

// Та же численная константа, что и в prisma/seed.ts — самостоятельная
// регистрация тоже сажает аккаунт на пробный тариф, а не на безлимитный
// (безлимит — только для служебного root-аккаунта, заводимого сидом).
const TRIAL_PLAN_CODE = "trial";

// @node-rs/argon2 экспортирует Algorithm как `const enum` — при
// isolatedModules (обязателен для Next.js) его нельзя импортировать типобезопасно
// (см. тот же комментарий в prisma/seed.ts), поэтому передаём числовое
// значение напрямую.
const ARGON2ID = 2; // @node-rs/argon2 Algorithm.Argon2id

/** x-forwarded-for может содержать цепочку "клиент, прокси1, прокси2" — нужен первый адрес. */
function getClientIp(req: NextRequest): string | null {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) return xff.split(",")[0]!.trim();
  return req.headers.get("x-real-ip");
}

/**
 * POST /api/register — самостоятельная регистрация: email + пароль + отметка
 * о принятии оферты и ознакомлении с Политикой (app/_components/LegalAccept).
 * Данные самого Пользователя обрабатываются на основании договора (п. 5 ч. 1
 * ст. 6 152-ФЗ), поэтому фиксируется акцепт оферты, а не согласие на ПДн.
 *
 * Создаёт ОДНОЙ транзакцией Account (тариф trial) + User (role=admin — сам
 * себя зарегистрировавший пользователь владеет своим аккаунтом и в будущем
 * сможет заводить под-пользователей) + Consent kind="offer" (доказательная
 * запись акцепта: дата, IP, редакции оферты и Политики — LEGAL_DOCS_VERSION).
 * Транзакция нужна, чтобы не оставить в БД "аккаунт без акцепта" при сбое на
 * любом из шагов.
 */
export async function POST(req: NextRequest) {
  let body: { email?: unknown; password?: unknown; consent?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }

  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const password = typeof body.password === "string" ? body.password : "";
  const consent = body.consent === true;

  if (!isValidEmail(email)) {
    return NextResponse.json({ error: "Некорректный адрес электронной почты." }, { status: 400 });
  }
  const passwordError = validatePasswordStrength(password);
  if (passwordError) {
    return NextResponse.json({ error: passwordError }, { status: 400 });
  }
  if (!consent) {
    return NextResponse.json(
      { error: "Регистрация невозможна без принятия условий оферты." },
      { status: 400 },
    );
  }

  // Имя аккаунта — домен email (например, "example.com"), а не email
  // целиком: Account в этой схеме концептуально "организация", и домен
  // читабельнее в будущем списке аккаунтов, чем полный адрес конкретного
  // человека. Уникальность имени не требуется (см. prisma/seed.ts — Account
  // вообще не имеет уникального ключа, кроме id).
  const accountName = email.split("@")[1] || email;

  const plan = await prisma.plan.findUnique({ where: { code: TRIAL_PLAN_CODE } });
  if (!plan) {
    // Означает, что сид (prisma/seed.ts) ещё не запускался на этой БД —
    // конфигурационная ошибка окружения, а не ошибка пользователя.
    console.error(`[/api/register] план "${TRIAL_PLAN_CODE}" не найден — сид не запускался?`);
    return NextResponse.json({ error: "Регистрация временно недоступна." }, { status: 500 });
  }

  // Пароль хешируется ДО транзакции: hash() — это CPU-bound работа за
  // пределами БД, незачем держать транзакцию открытой на это время.
  const passwordHash = await hash(password, { algorithm: ARGON2ID });
  const ip = getClientIp(req);

  try {
    // Пробный период — разовый календарный месяц с момента регистрации, не
    // продлевается (см. lib/period.ts, lib/quota.ts: после paidUntil — 402).
    const now = new Date();
    const paidUntil = addMonthsClamped(now, 1);
    await prisma.$transaction(async (tx) => {
      const account = await tx.account.create({
        data: {
          name: accountName,
          planId: plan.id,
          isActive: true,
          periodAnchorAt: now,
          paidUntil,
        },
      });
      const user = await tx.user.create({
        data: {
          accountId: account.id,
          email,
          passwordHash,
          role: "admin",
          isActive: true,
        },
      });
      await tx.consent.create({
        data: {
          userId: user.id,
          kind: "offer",
          documentVersion: LEGAL_DOCS_VERSION,
          ip,
          granted: true,
        },
      });
    });
  } catch (e) {
    // P2002 — нарушение уникальности users.email: адрес уже зарегистрирован.
    // Сообщение НЕ говорит "email уже занят" — это раскрывало бы факт
    // регистрации конкретного адреса (защита от перебора адресов, та же
    // логика, что и в auth.ts про одинаковый ответ на все причины отказа
    // входа).
    if (e instanceof Prisma.PrismaClientKnownRequestError && e.code === "P2002") {
      return NextResponse.json({ error: "Регистрация невозможна." }, { status: 409 });
    }
    console.error("[/api/register] transaction failed:", e);
    return NextResponse.json({ error: "Не удалось выполнить регистрацию." }, { status: 500 });
  }

  return NextResponse.json({ ok: true });
}
