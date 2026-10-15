/**
 * Личный словарь аккаунта («Мои термины»): разбор и валидация ввода.
 *
 * Термины конфиденциальны — значения не логируем и не кладём в тексты ошибок
 * дальше пределов ответа самому пользователю. Пределы зеркалят
 * server.py:_parse_custom_terms (Python отклонит всё, что пройдёт мимо).
 */

export const GLOSSARY_MAX_ENTRIES = 500;
export const GLOSSARY_MAX_ALIASES = 20;
export const GLOSSARY_MIN_ALIAS = 2;
export const GLOSSARY_MAX_ALIAS = 100;
export const GLOSSARY_MAX_CANONICAL = 200;

export type ParsedTerm = { aliases: string[]; canonical: string };
export type ParseResult = { ok: true; term: ParsedTerm } | { ok: false; reason: string };

/** Проверить одну запись: aliases — строка через запятую, canonical — необязателен. */
export function parseTermInput(aliasesRaw: unknown, canonicalRaw?: unknown): ParseResult {
  if (typeof aliasesRaw !== "string") {
    return { ok: false, reason: "Укажите варианты написания." };
  }
  const canonicalIn = typeof canonicalRaw === "string" ? canonicalRaw.trim() : "";
  if (/[=\r\n]/.test(aliasesRaw) || /[=\r\n]/.test(canonicalIn)) {
    return { ok: false, reason: "Символы «=» и переводы строк в терминах недопустимы." };
  }
  const aliases = aliasesRaw
    .split(",")
    .map((a) => a.trim())
    .filter(Boolean);
  if (aliases.length === 0) {
    return { ok: false, reason: "Укажите хотя бы один вариант написания." };
  }
  if (aliases.length > GLOSSARY_MAX_ALIASES) {
    return { ok: false, reason: `Не более ${GLOSSARY_MAX_ALIASES} вариантов написания в одной записи.` };
  }
  if (aliases.some((a) => a.length < GLOSSARY_MIN_ALIAS || a.length > GLOSSARY_MAX_ALIAS)) {
    return {
      ok: false,
      reason: `Каждый вариант — от ${GLOSSARY_MIN_ALIAS} до ${GLOSSARY_MAX_ALIAS} символов.`,
    };
  }
  const canonical = canonicalIn || aliases[0];
  if (canonical.length > GLOSSARY_MAX_CANONICAL) {
    return { ok: false, reason: `Название в карте замен — не длиннее ${GLOSSARY_MAX_CANONICAL} символов.` };
  }
  return { ok: true, term: { aliases, canonical } };
}

/**
 * Разобрать текст формата «алиас1, алиас2 = Каноническое имя» (по строке на
 * запись, «#» — комментарий, пустые строки пропускаются). Невалидные строки
 * не рушат импорт, а попадают в skipped с номером строки и причиной (без
 * самого значения).
 */
export function parseBulk(text: string): {
  terms: ParsedTerm[];
  skipped: { line: number; reason: string }[];
} {
  const terms: ParsedTerm[] = [];
  const skipped: { line: number; reason: string }[] = [];
  text.split(/\r\n|\r|\n/).forEach((raw, i) => {
    const line = raw.split("#", 1)[0].trim();
    if (!line) return;
    const eq = line.indexOf("=");
    if (eq !== line.lastIndexOf("=")) {
      skipped.push({ line: i + 1, reason: "Больше одного символа «=»." });
      return;
    }
    const left = eq >= 0 ? line.slice(0, eq) : line;
    const right = eq >= 0 ? line.slice(eq + 1) : "";
    const r = parseTermInput(left, right);
    if (r.ok) terms.push(r.term);
    else skipped.push({ line: i + 1, reason: r.reason });
  });
  return { terms, skipped };
}

/**
 * Записи аккаунта в виде custom_terms для бэкенда. Хранимые aliases —
 * строка «как введено», разбираем обратно; битая запись пропускается.
 */
export function toBackendTerms(rows: { aliases: string; canonical: string }[]): ParsedTerm[] {
  const out: ParsedTerm[] = [];
  for (const row of rows) {
    const aliases = row.aliases
      .split(",")
      .map((a) => a.trim())
      .filter(Boolean);
    if (aliases.length) out.push({ aliases, canonical: row.canonical || aliases[0] });
  }
  return out;
}
