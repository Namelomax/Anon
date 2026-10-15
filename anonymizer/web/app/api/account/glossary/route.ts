import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";
import { GLOSSARY_MAX_ENTRIES, parseBulk, parseTermInput, type ParsedTerm } from "@/lib/glossary";

export const runtime = "nodejs";

const MANAGER_ROLES = new Set(["root", "admin"]);

type Caller = { userId: number; accountId: number; canManage: boolean };

/**
 * Личность — только из СЕССИИ (accountId из запроса позволил бы читать/править
 * чужой словарь). Читать может любой участник аккаунта, менять — admin/root.
 */
async function requireCaller(): Promise<Caller | NextResponse> {
  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;
  const accountId = typeof session?.accountId === "number" ? session.accountId : null;
  const role = typeof session?.role === "string" ? session.role : "";
  if (userId == null || accountId == null || !Number.isFinite(userId)) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }
  return { userId, accountId, canManage: MANAGER_ROLES.has(role) };
}

function forbidden() {
  return NextResponse.json(
    { error: "Менять словарь терминов может только администратор аккаунта." },
    { status: 403 },
  );
}

const toRow = (t: { id: number; aliases: string; canonical: string; createdAt: Date }) => ({
  id: t.id,
  aliases: t.aliases,
  canonical: t.canonical,
  createdAt: t.createdAt.toISOString(),
});

/** GET /api/account/glossary — записи словаря аккаунта (любой участник). */
export async function GET() {
  const caller = await requireCaller();
  if (caller instanceof NextResponse) return caller;
  const rows = await prisma.glossaryTerm.findMany({
    where: { accountId: caller.accountId },
    orderBy: { id: "asc" },
  });
  return NextResponse.json({
    terms: rows.map(toRow),
    count: rows.length,
    max: GLOSSARY_MAX_ENTRIES,
    canManage: caller.canManage,
  });
}

/**
 * POST — {aliases, canonical?} добавляет одну запись, {bulk} — импорт текста
 * формата «a, b = Каноническое». Лимит записей проверяется внутри транзакции
 * вместе со вставкой.
 */
export async function POST(req: NextRequest) {
  const caller = await requireCaller();
  if (caller instanceof NextResponse) return caller;
  if (!caller.canManage) return forbidden();

  let body: { aliases?: unknown; canonical?: unknown; bulk?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }

  let terms: ParsedTerm[];
  let skipped: { line: number; reason: string }[] = [];
  if (body.bulk !== undefined) {
    if (typeof body.bulk !== "string" || !body.bulk.trim()) {
      return NextResponse.json({ error: "Вставьте список терминов." }, { status: 400 });
    }
    if (body.bulk.length > 200_000) {
      return NextResponse.json({ error: "Список слишком большой." }, { status: 400 });
    }
    ({ terms, skipped } = parseBulk(body.bulk));
    if (terms.length === 0) {
      return NextResponse.json(
        { error: "Не найдено ни одной корректной записи.", added: 0, skipped },
        { status: 400 },
      );
    }
  } else {
    const r = parseTermInput(body.aliases, body.canonical);
    if (!r.ok) return NextResponse.json({ error: r.reason }, { status: 400 });
    terms = [r.term];
  }

  try {
    const result = await prisma.$transaction(async (tx) => {
      const existing = await tx.glossaryTerm.count({ where: { accountId: caller.accountId } });
      const room = Math.max(0, GLOSSARY_MAX_ENTRIES - existing);
      if (room === 0) return { full: true as const };
      const take = terms.slice(0, room);
      for (const t of take) {
        await tx.glossaryTerm.create({
          data: {
            accountId: caller.accountId,
            aliases: t.aliases.join(", "),
            canonical: t.canonical,
            createdById: caller.userId,
          },
        });
      }
      return { added: take.length, overflow: terms.length - take.length };
    });

    if ("full" in result) {
      return NextResponse.json(
        { error: `Достигнут предел словаря: ${GLOSSARY_MAX_ENTRIES} записей.` },
        { status: 409 },
      );
    }
    const extra = result.overflow
      ? [{ line: 0, reason: `Превышен предел ${GLOSSARY_MAX_ENTRIES} записей: не добавлено ${result.overflow}.` }]
      : [];
    return NextResponse.json({ added: result.added, skipped: [...skipped, ...extra] });
  } catch {
    // Значения терминов в лог не пишем.
    console.error("[/api/account/glossary] не удалось сохранить термины");
    return NextResponse.json({ error: "Не удалось сохранить термины." }, { status: 500 });
  }
}

/** DELETE ?id= — удалить запись СВОЕГО аккаунта. */
export async function DELETE(req: NextRequest) {
  const caller = await requireCaller();
  if (caller instanceof NextResponse) return caller;
  if (!caller.canManage) return forbidden();

  const id = Number(req.nextUrl.searchParams.get("id"));
  if (!Number.isInteger(id) || id <= 0) {
    return NextResponse.json({ error: "Не указана запись." }, { status: 400 });
  }
  // deleteMany с accountId в условии: чужую запись не удалить даже по прямому API.
  const res = await prisma.glossaryTerm.deleteMany({ where: { id, accountId: caller.accountId } });
  if (res.count === 0) {
    return NextResponse.json({ error: "Запись не найдена." }, { status: 404 });
  }
  return NextResponse.json({ ok: true });
}
