import { PrismaClient } from "@prisma/client";
import { hash } from "@node-rs/argon2";

// @node-rs/argon2 экспортирует Algorithm как `const enum`, а tsconfig проекта
// включает isolatedModules (обязательно для Next.js) — const enum со значением
// нельзя импортировать при isolatedModules (TS2748), поэтому ниже передаём
// числовое значение напрямую с комментарием, что оно означает.
const ARGON2ID = 2; // @node-rs/argon2 Algorithm.Argon2id

const prisma = new PrismaClient();

// Пробный лимит для аккаунтов до оплаты. По измеренной экономике сервиса
// (~0.083 ₽/страница, см. память "Anonymizer unit economics") 50 страниц —
// это доли рубля себестоимости, но по факту 1-2 обычных документа (в этом
// корпусе типичный документ — порядка 30 страниц, см. usage_log.py) —
// достаточно, чтобы оценить качество распознавания, не будучи бесплатным
// безлимитом.
const TRIAL_PAGES_PER_MONTH = 50;

async function main() {
  const unlimitedPlan = await prisma.plan.upsert({
    where: { code: "unlimited" },
    update: {},
    create: {
      code: "unlimited",
      title: "Безлимитный",
      pagesPerMonth: null,
      maxUsers: null,
      // Служебный план для root-аккаунта оператора, не продаётся — цена 0.
      priceKopecks: 0,
    },
  });

  await prisma.plan.upsert({
    where: { code: "trial" },
    update: {},
    create: {
      code: "trial",
      title: "Пробный",
      pagesPerMonth: TRIAL_PAGES_PER_MONTH,
      maxUsers: 1,
      priceKopecks: 0,
    },
  });

  const rootEmail = process.env.ROOT_EMAIL;
  const rootPassword = process.env.ROOT_PASSWORD;
  if (!rootEmail || !rootPassword) {
    // Никакого пароля по умолчанию намеренно — учётная запись root открывает
    // полный доступ, захардкоженный дефолт рано или поздно утечёт вместе с
    // репозиторием. Пусть сид падает явно, а не создаёт root с предсказуемым
    // паролем.
    throw new Error(
      "ROOT_EMAIL и ROOT_PASSWORD не заданы — сид отказывается создавать root-пользователя " +
        "с паролем по умолчанию. Задайте обе переменные окружения (см. .env.local.example) и повторите.",
    );
  }

  // У Account в этой схеме (v1) нет естественного уникального ключа, кроме
  // id, поэтому идемпотентность обеспечивается через findFirst+create по
  // имени, а не через upsert по id (полагаться на то, что автоинкремент
  // всегда даст id=1, было бы хрупко).
  let rootAccount = await prisma.account.findFirst({ where: { name: "root" } });
  if (!rootAccount) {
    rootAccount = await prisma.account.create({
      data: {
        name: "root",
        planId: unlimitedPlan.id,
        isActive: true,
      },
    });
  }

  // Алгоритм явно зафиксирован как Argon2id (хотя это и дефолт пакета) — это
  // прямое требование модели угроз У-11 (см. 10_Авторизация_и_биллинг_
  // правовая_фиксация.md), и лучше зафиксировать намерение явно, чем
  // полагаться на то, что дефолт библиотеки не сменится в будущей версии.
  // Соль генерируется библиотекой случайно на каждый вызов — индивидуальная
  // соль на пользователя обеспечивается автоматически, отдельно передавать
  // её не нужно.
  const passwordHash = await hash(rootPassword, { algorithm: ARGON2ID });

  await prisma.user.upsert({
    where: { email: rootEmail },
    update: {},
    create: {
      accountId: rootAccount.id,
      email: rootEmail,
      passwordHash,
      role: "root",
      isActive: true,
    },
  });
}

main()
  .catch((err) => {
    console.error(err);
    process.exit(1);
  })
  .finally(async () => {
    await prisma.$disconnect();
  });
