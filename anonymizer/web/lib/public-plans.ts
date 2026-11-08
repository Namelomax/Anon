import "server-only";
import { prisma } from "@/lib/db";

/** Публичный каталог: только продаваемые планы, без аккаунтов, квот и платежей. */
export async function getPublicPlans() {
  return prisma.plan.findMany({
    where: { code: { notIn: ["unlimited", "trial"] }, priceKopecks: { gt: 0 } },
    select: {
      code: true,
      title: true,
      pagesPerMonth: true,
      maxUsers: true,
      priceKopecks: true,
      priceYearKopecks: true,
    },
    orderBy: { priceKopecks: "asc" },
  });
}
