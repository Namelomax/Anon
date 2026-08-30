import { promises as fs } from "node:fs";
import path from "node:path";
import { Prisma } from "@prisma/client";
import { prisma } from "@/lib/db";
import { periodFromIso } from "@/lib/period";

/**
 * Импорт биллингового JSONL-лога `anonymizer/usage_log.py` в БД.
 *
 * Этот файл — «зеркало» одной строки `kind="request_total"` на документ (см.
 * докстринг `usage_log.py`): она пишется БЕЗУСЛОВНО по завершении обработки,
 * даже если клиент так и не забрал результат — это и закрывает дыру
 * «пользователь закрыл вкладку» БЕЗ HTTP-колбэка от Python: раз оба процесса
 * живут на одном хосте, Next.js просто читает тот же файл.
 *
 * Импорт ИНКРЕМЕНТАЛЬНЫЙ: лог растёт всю жизнь сервиса, перечитывать его
 * целиком на каждый вызов (а вызывается он на входе КАЖДОГО anonymize-
 * запроса, см. lib/quota.ts) было бы работой, растущей без предела. Прогресс
 * — байтовое смещение — хранится в единственной строке ImportState.
 */

// Дефолт зеркалит `anonymizer/usage_log.py:_default_log_path()` —
// "<корень репо>/logs/usage.jsonl". Здесь корень репо ищем от каталога
// исходника (anonymizer/web/lib -> anonymizer/web -> anonymizer -> корень),
// а не от process.cwd(): __dirname стабилен независимо от того, откуда
// запущен процесс Next.js. В проде, тем не менее, ЛУЧШЕ задавать
// ANONYMIZER_USAGE_LOG явным абсолютным путём (та же рекомендация, что и
// для DATABASE_URL в .env.local.example) — сборка Next.js может переносить
// скомпилированные файлы в другое дерево каталогов.
function _defaultLogPath(): string {
  return path.resolve(__dirname, "..", "..", "..", "logs", "usage.jsonl");
}

// path.resolve (уже применён и в ANONYMIZER_USAGE_LOG, и в _defaultLogPath)
// нормализует "./"-сегменты и относительные пути, но НЕ обязан давать
// одинаковую строку для двух путей, указывающих на один и тот же файл, если
// они изначально записаны по-разному (например, лишний "./" в env) — отсюда
// отдельная нормализация ниже перед сравнением с сохранённым в БД путём.
const LOG_PATH = path.resolve(process.env.ANONYMIZER_USAGE_LOG || _defaultLogPath());

export interface ImportResult {
  imported: number;
  duplicates: number;
  skippedNoAccount: number;
}

const _EMPTY_RESULT: ImportResult = { imported: 0, duplicates: 0, skippedNoAccount: 0 };

/**
 * Разобрать всё, что появилось в логе с прошлого вызова, и внести в
 * UsageRecord/QuotaCounter. Безопасно вызывать конкурентно (см. `_importOne`
 * — идемпотентность обеспечена уникальностью `requestId`, а не блокировкой).
 */
export async function importUsageLog(): Promise<ImportResult> {
  let stat;
  try {
    stat = await fs.stat(LOG_PATH);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") {
      // Лога ещё нет — например, ни один документ ещё не обработан. Это не
      // ошибка, просто нечего импортировать.
      return _EMPTY_RESULT;
    }
    throw err;
  }

  const state = await prisma.importState.upsert({
    where: { id: 1 },
    update: {},
    create: { id: 1, offsetBytes: 0, logPath: LOG_PATH },
  });

  let startOffset = state.offsetBytes;

  // Сохранённое смещение имеет смысл, только если оно относится к ТОМУ ЖЕ
  // файлу: ANONYMIZER_USAGE_LOG может смениться (переезд, смена окружения),
  // и тогда байтовое смещение прошлого файла указывает в середину СОВЕРШЕННО
  // ДРУГОГО файла — попадание в середину строки ломает парсинг, импорт
  // останавливается ПЕРЕД первой же неразобранной строкой (см. ниже) и
  // счётчик расхода молча замирает, пока кто-то не заметит и не почистит
  // строку руками. `state.logPath === null` (в т.ч. на строке, заведённой ДО
  // появления этого поля) тоже трактуется как "путь сменился" — путь
  // попросту неизвестен, полагаться на старое смещение нельзя.
  //
  // Сравнение — ДО проверки на усечение (см. ниже): если файл сменился,
  // сравнивать старое смещение с размером НОВОГО файла бессмысленно,
  // "усечение" или "всё уже прочитано" были бы случайным совпадением
  // размеров, а не содержательным сигналом об этом файле.
  const logPathChanged = state.logPath !== LOG_PATH;
  if (logPathChanged) {
    // Уровень info: это НЕ ошибка (лог просто указывает на новый файл), но
    // оператор, читающий журнал, должен сразу видеть ПОЧЕМУ произошло
    // полное перечитывание файла, а не тратить время на догадки.
    console.info(
      `[usage-import] путь к логу изменился (${state.logPath ?? "не задан"} -> ${LOG_PATH}) — ` +
        "сбрасываю смещение и читаю файл с начала. Это безопасно: импорт " +
        "идемпотентен по requestId (см. _importOne — повторная вставка уже " +
        "внесённой строки просто ловится как дубликат P2002 и не задваивает " +
        "счётчик), поэтому НЕ убирайте этот сброс ради «оптимизации».",
    );
    startOffset = 0;
  }

  if (stat.size < startOffset) {
    // Файл короче сохранённого смещения — похоже на ротацию/усечение лога.
    // Падать или молча ничего не читать нельзя: начинаем заново, а не
    // теряем оставшуюся историю навсегда.
    console.warn(
      `[usage-import] лог короче сохранённого смещения (${stat.size} < ${startOffset} байт) — ` +
        "похоже на ротацию/усечение файла, читаю с начала.",
    );
    startOffset = 0;
  }

  const length = stat.size - startOffset;
  if (length <= 0) {
    if (startOffset !== state.offsetBytes || logPathChanged) {
      await prisma.importState.update({
        where: { id: 1 },
        data: { offsetBytes: startOffset, logPath: LOG_PATH },
      });
    }
    return _EMPTY_RESULT;
  }

  const buf = Buffer.alloc(length);
  const fh = await fs.open(LOG_PATH, "r");
  try {
    await fh.read(buf, 0, length, startOffset);
  } finally {
    await fh.close();
  }

  const text = buf.toString("utf8");
  const rawLines = text.split("\n");
  // Последний элемент после split("\n") — либо "" (файл целиком дописан до
  // конца, ничего не потеряно), либо ХВОСТ ещё не завершённой строки
  // (Python-процесс пишет её прямо сейчас). В обоих случаях его нельзя
  // разбирать как JSON — просто отбрасываем и не сдвигаем смещение за него:
  // при следующем импорте он будет прочитан заново уже целиком.
  const completeLines = rawLines.slice(0, -1);

  let cursor = startOffset;
  let imported = 0;
  let duplicates = 0;
  let skippedNoAccount = 0;

  for (const line of completeLines) {
    const lineBytes = Buffer.byteLength(line, "utf8") + 1; // +1 — вырезанный split'ом "\n"

    if (line.length === 0) {
      cursor += lineBytes;
      continue;
    }

    let rec: Record<string, unknown>;
    try {
      rec = JSON.parse(line);
    } catch (err) {
      // "Полная" строка (мы уже отрезали хвост выше) не должна ломать JSON —
      // но если сломала, безопаснее остановиться ПЕРЕД ней (не сдвигать
      // cursor), чем закоммитить смещение мимо повреждённой записи и
      // потерять её навсегда.
      console.error("[usage-import] строка лога не распарсилась, останавливаюсь перед ней:", err);
      break;
    }

    if (rec.kind === "request_total") {
      if (rec.account_id == null) {
        // Запись без account_id — клиент, предшествующий системе
        // биллинга (см. докстринг usage_log.request_context). Не билящаяся
        // запись, но это НЕ ошибка импорта.
        skippedNoAccount += 1;
      } else {
        const outcome = await _importOne(rec);
        if (outcome === "imported") imported += 1;
        else duplicates += 1;
      }
    }

    cursor += lineBytes;
  }

  await prisma.importState.update({
    where: { id: 1 },
    data: { offsetBytes: cursor, logPath: LOG_PATH },
  });

  return { imported, duplicates, skippedNoAccount };
}

/**
 * Внести одну строку `request_total` в БД: UsageRecord (по requestId,
 * идемпотентно) + инкремент QuotaCounter — ОДНОЙ транзакцией, чтобы счётчик
 * никогда не разошёлся с суммой UsageRecord.
 *
 * Конкурентные вызовы `importUsageLog` могут прочитать одну и ту же строку
 * дважды (окно между чтением/записью ImportState не блокируется намеренно
 * — см. докстринг модуля) — тогда второй `create` упадёт на уникальности
 * `requestId` (P2002), и это ожидаемый исход "уже импортировано", а не сбой.
 */
async function _importOne(rec: Record<string, unknown>): Promise<"imported" | "duplicate"> {
  const requestId = String(rec.request_id);
  const accountId = Number(rec.account_id);
  const userId = rec.user_id == null ? null : Number(rec.user_id);
  const chars = Math.round(Number(rec.chars ?? 0));
  const pages = Number(rec.pages ?? 0);
  const pagesTenths = Math.round(pages * 10);
  const costRub = Number(rec.cost_rub ?? 0);
  const costKopecks = Math.round(costRub * 100);
  const promptTokens = Math.round(Number(rec.prompt_tokens_total ?? 0));
  const completionTokens = Math.round(Number(rec.completion_tokens_total ?? 0));
  const glinerTokensEst = Math.round(Number(rec.gliner_tokens_est ?? 0));
  const seconds = Number(rec.seconds ?? 0);
  const period = periodFromIso(rec.ts);

  try {
    await prisma.$transaction(async (tx) => {
      await tx.usageRecord.create({
        data: {
          accountId,
          userId,
          period,
          requestId,
          chars,
          pagesTenths,
          // "Мягкая" политика биллинга: сейчас списываем страницы целиком,
          // без деления на "билящиеся"/"нет". Более строгая (например,
          // токен-based) политика сможет в будущем посчитать
          // billablePagesTenths иначе — не трогая уже сохранённые
          // pagesTenths/requestId, то есть без пересчёта истории.
          billablePagesTenths: pagesTenths,
          promptTokens,
          completionTokens,
          glinerTokensEst,
          costKopecks,
          seconds,
          // `request_total` пишется БЕЗУСЛОВНО по завершении обработки (см.
          // докстринг usage_log.py) — сам лог не несёт отдельного флага
          // успеха/неуспеха документа. Раз строка попала в лог — работа
          // была сделана и подлежит оплате, поэтому здесь всегда true.
          ok: true,
        },
      });
      await tx.quotaCounter.upsert({
        where: { accountId_period: { accountId, period } },
        update: { pagesUsedTenths: { increment: pagesTenths } },
        create: { accountId, period, pagesUsedTenths: pagesTenths },
      });
    });
    return "imported";
  } catch (err) {
    if (err instanceof Prisma.PrismaClientKnownRequestError && err.code === "P2002") {
      return "duplicate";
    }
    throw err;
  }
}
