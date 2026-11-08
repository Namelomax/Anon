const TOKEN = /\[[A-Z_]+_\d+\]/g;
const SERVER_TOKEN = /^\[([A-Z_]+)_(\d+)\]$/;

export function makePreview(text) {
  return text.trim().split(/\s+/u).slice(0, 8).join(' ').slice(0, 160);
}

function checkMapping(mapping) {
  if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) throw new Error('Некорректный ответ сервера');
  for (const [key, value] of Object.entries(mapping)) {
    if (!SERVER_TOKEN.test(key) || typeof value !== 'string') throw new Error('Некорректный ответ сервера');
  }
}

export function createRecord(text, result, counters = {}) {
  if (typeof text !== 'string' || !result || typeof result.anonymized_text !== 'string') throw new Error('Некорректный ответ сервера');
  checkMapping(result.mapping);
  // An identical source token cannot be distinguished reliably from a server replacement.
  if (Object.keys(result.mapping).some(key => text.includes(key))) throw new Error('Плейсхолдер в исходном тексте совпадает с ответом сервера');
  if (!counters || typeof counters !== 'object' || Array.isArray(counters)) throw new Error('Некорректные счётчики плейсхолдеров');
  const tokenCounters = Object.create(null);
  for (const [label, count] of Object.entries(counters)) {
    if (!/^[A-Z_]+$/.test(label) || !Number.isSafeInteger(count) || count < 0) throw new Error('Некорректные счётчики плейсхолдеров');
    tokenCounters[label] = count;
  }
  // Never overwrite literal placeholders already in the source or unknown
  // placeholders returned unchanged by the server. Replacements are single-pass.
  const occupied = new Set(text.match(TOKEN) || []);
  for (const token of result.anonymized_text.match(TOKEN) || []) {
    if (!Object.hasOwn(result.mapping, token)) occupied.add(token);
  }
  const replacements = Object.create(null);
  const mapping = Object.create(null);
  for (const [key, value] of Object.entries(result.mapping)) {
    const [, label] = key.match(SERVER_TOKEN);
    let number = tokenCounters[label] || 0;
    let renamed;
    do {
      number++;
      if (!Number.isSafeInteger(number)) throw new Error('Исчерпан диапазон номеров плейсхолдеров');
      renamed = `[${label}_${number}]`;
    } while (occupied.has(renamed));
    tokenCounters[label] = number;
    occupied.add(renamed);
    replacements[key] = renamed;
    mapping[renamed] = value;
  }
  const maskedText = result.anonymized_text.replace(TOKEN, token => replacements[token] ?? token);
  return {
    id: crypto.randomUUID(), title: makePreview(text), createdAt: new Date().toISOString(),
    mapping, maskedText, edits: maskedEdits(text, maskedText, result.spans, mapping),
    warnings: result.warnings ?? [], summary: result.summary ?? null, tokenCounters,
  };
}

export function restoreText(text, mapping) {
  if (typeof text !== 'string') throw new Error('Некорректный текст');
  checkMapping(mapping);
  const unknown = [];
  const seen = new Set();
  const restored = text.replace(TOKEN, token => {
    if (Object.hasOwn(mapping, token)) return mapping[token];
    if (!seen.has(token)) { seen.add(token); unknown.push(token); }
    return token;
  });
  return { text: restored, unknown };
}

export function validateBaseUrl(value) {
  let url;
  try { url = new URL(value); } catch { throw new Error('Некорректный адрес сервера'); }
  if (url.username || url.password || url.search || url.hash ||
      !(url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))) {
    throw new Error('Некорректный адрес сервера');
  }
  return url.href.replace(/\/+$/, '');
}

export function serverOrigin(value) { return new URL(validateBaseUrl(value)).origin; }

// Server spans use Unicode code-point offsets; DOM/JS strings use UTF-16.
export function maskedEdits(source, masked, spans, mapping) {
  if (!Array.isArray(spans)) return null;
  const positions = [0];
  for (const char of source) positions.push(positions.at(-1) + char.length);
  const edits = [];
  let originalAt = 0, maskedAt = 0;
  for (const span of [...spans].sort((a, b) => a.start - b.start)) {
    const start = positions[span.start], end = positions[span.end];
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < originalAt || end <= start) return null;
    const unchanged = source.slice(originalAt, start);
    if (!masked.startsWith(unchanged, maskedAt)) return null;
    maskedAt += unchanged.length;
    const token = /^\[[A-Z_]+_\d+\]/.exec(masked.slice(maskedAt))?.[0];
    if (!token || !Object.hasOwn(mapping, token)) return null;
    edits.push({ start, end, text: token });
    maskedAt += token.length;
    originalAt = end;
  }
  return masked.slice(maskedAt) === source.slice(originalAt) ? edits : null;
}

export function restorationEdits(text, mapping) {
  checkMapping(mapping);
  return [...text.matchAll(TOKEN)].filter(match => Object.hasOwn(mapping, match[0])).map(match => ({
    start: match.index, end: match.index + match[0].length, text: mapping[match[0]]
  }));
}
