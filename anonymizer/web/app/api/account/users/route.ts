import { NextRequest, NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { auth } from "@/auth";
import { getSeatUsage } from "@/lib/invitations";

export const runtime = "nodejs";

const MANAGER_ROLES = new Set(["root", "admin"]);

async function requireManager() {
  const session = await auth();
  const userId = session?.user?.id ? Number(session.user.id) : null;
  const accountId = typeof session?.accountId === "number" ? session.accountId : null;
  const role = typeof session?.role === "string" ? session.role : "";
  if (userId == null || accountId == null || !Number.isFinite(userId)) {
    return NextResponse.json({ error: "Требуется вход в систему." }, { status: 401 });
  }
  if (!MANAGER_ROLES.has(role)) {
    return NextResponse.json(
      { error: "Управлять пользователями аккаунта может только администратор." },
      { status: 403 },
    );
  }
  return { userId, accountId, role };
}

/**
 * PATCH /api/account/users?id=... — включить/отключить пользователя аккаунта.
 *
 * Отключение — это и есть освобождение места по тарифу (см. шапку
 * lib/invitations.ts): удаления пользователей нет, потому что на них
 * ссылается журнал использования, а он — доказательная запись расхода.
 *
 * Обратное включение проверяет лимит мест: пока пользователь был отключён,
 * место могли занять другим человеком или приглашением.
 */
export async function PATCH(req: NextRequest) {
  const manager = await requireManager();
  if (manager instanceof NextResponse) return manager;

  const id = Number(req.nextUrl.searchParams.get("id"));
  if (!Number.isInteger(id) || id <= 0) {
    return NextResponse.json({ error: "Не указан пользователь." }, { status: 400 });
  }

  let body: { isActive?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Некорректный запрос." }, { status: 400 });
  }
  if (typeof body.isActive !== "boolean") {
    return NextResponse.json({ error: "Не указано новое состояние." }, { status: 400 });
  }
  const isActive = body.isActive;

  if (id === manager.userId) {
    // Отключить себя — запереть аккаунт: следующий же запрос сессии увидит
    // isActive=false и разлогинит (см. callbacks.jwt в auth.ts), а включить
    // обратно будет некому.
    return NextResponse.json(
      { error: "Нельзя изменить состояние собственной учётной записи." },
      { status: 400 },
    );
  }

  const target = await prisma.user.findUnique({ where: { id } });
  if (!target || target.accountId !== manager.accountId) {
    // Одинаковый ответ и для «нет такого», и для «чужой аккаунт» — чтобы
    // перебором id нельзя было узнать, какие пользователи существуют.
    return NextResponse.json({ error: "Пользователь не найден." }, { status: 404 });
  }
  if (target.role === "root") {
    return NextResponse.json(
      { error: "Учётная запись владельца сервиса не изменяется отсюда." },
      { status: 403 },
    );
  }

  if (isActive && !target.isActive) {
    const seats = await getSeatUsage(manager.accountId);
    if (seats?.free != null && seats.free <= 0) {
      return NextResponse.json(
        {
          error:
            `Лимит пользователей по тарифу исчерпан: занято ${seats.taken} из ${seats.limit}. ` +
            "Освободите место, прежде чем включать пользователя обратно.",
        },
        { status: 402 },
      );
    }
  }

  await prisma.user.update({ where: { id }, data: { isActive } });
  return NextResponse.json({ ok: true });
}
