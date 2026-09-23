import { prisma } from "@/lib/db";
import { getQuotaState } from "@/lib/quota";
import { getSeatUsage, pendingInvitationWhere } from "@/lib/invitations";

/**
 * Сводка для личного кабинета: кто вошёл, на каком тарифе сидит его аккаунт,
 * сколько квоты израсходовано и что обрабатывалось последним.
 *
 * Считается из ТЕХ ЖЕ данных и по той же формуле, что и проверка лимита на
 * входе (`getQuotaState`) — иначе кабинет показывал бы одно, а отказ на
 * загрузке приходил бы по другому, и доверия к цифрам не было бы.
 *
 * costKopecks из UsageRecord сюда СОЗНАТЕЛЬНО не попадает: это себестоимость
 * оператора (сколько апстрим-шлюз взял с владельца сервиса), а не цена для
 * клиента. Показывать её клиенту — раскрывать свою экономику.
 */

/** Сколько последних документов показывать в истории кабинета. */
const RECENT_LIMIT = 20;

export type AccountSummary = {
  user: { email: string; role: string };
  account: { id: number; name: string; isActive: boolean };
  plan: {
    code: string;
    title: string;
    pagesPerMonth: number | null;
    maxUsers: number | null;
    priceKopecks: number;
  };
  quota: {
    period: string;
    unlimited: boolean;
    usedTenths: number;
    /** Лимит плана/индивидуального override БЕЗ грантов; null — безлимит. */
    limitTenths: number | null;
    grantsTenths: number;
    /** Лимит вместе с грантами; null — безлимит. */
    allowanceTenths: number | null;
    /** Остаток, не опускается ниже нуля; null — безлимит. */
    remainingTenths: number | null;
    exhausted: boolean;
  };
  /** Роль вошедшего позволяет управлять составом аккаунта (см. routes). */
  canManage: boolean;
  users: {
    active: number;
    /** Plan.maxUsers; null — без ограничения. */
    limit: number | null;
    /** Занято мест: активные пользователи + живые приглашения. */
    taken: number;
    free: number | null;
    list: {
      id: number;
      email: string;
      role: string;
      isActive: boolean;
      createdAt: string;
      isSelf: boolean;
    }[];
  };
  /** Только живые приглашения: активированные и отозванные места не занимают. */
  invitations: {
    id: number;
    email: string;
    role: string;
    createdAt: string;
    expiresAt: string;
  }[];
  periodTotals: { documents: number; chars: number; pagesTenths: number };
  recent: {
    id: number;
    createdAt: string;
    userEmail: string | null;
    chars: number;
    pagesTenths: number;
    billablePagesTenths: number;
    seconds: number;
    ok: boolean;
  }[];
};

export async function getAccountSummary(
  accountId: number,
  userId: number,
): Promise<AccountSummary | null> {
  const state = await getQuotaState(accountId);
  if (!state) return null;

  const user = await prisma.user.findUnique({ where: { id: userId } });
  if (!user) return null;

  const [seats, members, invitations, totals, recent] = await Promise.all([
    getSeatUsage(accountId),
    prisma.user.findMany({
      where: { accountId },
      orderBy: [{ isActive: "desc" }, { createdAt: "asc" }],
    }),
    prisma.invitation.findMany({
      where: { accountId, ...pendingInvitationWhere() },
      orderBy: { createdAt: "desc" },
    }),
    prisma.usageRecord.aggregate({
      where: { accountId, period: state.period },
      _count: { _all: true },
      _sum: { chars: true, billablePagesTenths: true },
    }),
    prisma.usageRecord.findMany({
      where: { accountId },
      orderBy: { createdAt: "desc" },
      take: RECENT_LIMIT,
      include: { user: { select: { email: true } } },
    }),
  ]);

  return {
    user: { email: user.email, role: user.role },
    account: {
      id: state.account.id,
      name: state.account.name,
      isActive: state.account.isActive,
    },
    plan: {
      code: state.account.plan.code,
      title: state.account.plan.title,
      pagesPerMonth: state.account.plan.pagesPerMonth,
      maxUsers: state.account.plan.maxUsers,
      priceKopecks: state.account.plan.priceKopecks,
    },
    quota: {
      period: state.period,
      unlimited: state.allowanceTenths == null,
      usedTenths: state.usedTenths,
      limitTenths: state.limitTenths,
      grantsTenths: state.grantsTenths,
      allowanceTenths: state.allowanceTenths,
      remainingTenths: state.remainingTenths,
      exhausted: state.exhausted,
    },
    canManage: user.role === "root" || user.role === "admin",
    users: {
      active: seats?.activeUsers ?? 0,
      limit: state.account.plan.maxUsers,
      taken: seats?.taken ?? 0,
      free: seats?.free ?? null,
      list: members.map((m) => ({
        id: m.id,
        email: m.email,
        role: m.role,
        isActive: m.isActive,
        createdAt: m.createdAt.toISOString(),
        isSelf: m.id === userId,
      })),
    },
    invitations: invitations.map((i) => ({
      id: i.id,
      email: i.email,
      role: i.role,
      createdAt: i.createdAt.toISOString(),
      expiresAt: i.expiresAt.toISOString(),
    })),
    periodTotals: {
      documents: totals._count._all,
      chars: totals._sum.chars ?? 0,
      pagesTenths: totals._sum.billablePagesTenths ?? 0,
    },
    recent: recent.map((r) => ({
      id: r.id,
      createdAt: r.createdAt.toISOString(),
      userEmail: r.user?.email ?? null,
      chars: r.chars,
      pagesTenths: r.pagesTenths,
      billablePagesTenths: r.billablePagesTenths,
      seconds: r.seconds,
      ok: r.ok,
    })),
  };
}
