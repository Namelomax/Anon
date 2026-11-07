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

function randomNamespace() {
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return Array.from(bytes, byte => String.fromCharCode(65 + (byte & 15))).join('');
}

export function createRecord(text, result, namespace) {
  if (typeof text !== 'string' || !result || typeof result.anonymized_text !== 'string') throw new Error('Некорректный ответ сервера');
  checkMapping(result.mapping);
  // An identical source token cannot be distinguished reliably from a server replacement.
  if (Object.keys(result.mapping).some(key => text.includes(key))) throw new Error('Плейсхолдер в исходном тексте совпадает с ответом сервера');
  const occupied = text + result.anonymized_text + Object.keys(result.mapping).join('');
  let ns = namespace;
  if (ns !== undefined && (!/^[A-P]{12}$/.test(ns) || occupied.includes(`_${ns}_`))) throw new Error('Некорректное пространство имён');
  if (ns === undefined) {
    do { ns = randomNamespace(); } while (occupied.includes(`_${ns}_`));
  }
  const replacements = Object.create(null);
  const mapping = Object.create(null);
  for (const [key, value] of Object.entries(result.mapping)) {
    const [, label, number] = key.match(SERVER_TOKEN);
    const renamed = `[${label}_${ns}_${number}]`;
    replacements[key] = renamed;
    mapping[renamed] = value;
  }
  return {
    id: crypto.randomUUID(), title: makePreview(text), createdAt: new Date().toISOString(),
    mapping, maskedText: result.anonymized_text.replace(TOKEN, token => replacements[token] ?? token),
    warnings: result.warnings ?? [], summary: result.summary ?? null,
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
