import { validateBaseUrl } from './core.js';

async function request(settings, method, jobId, body) {
  const base = validateBaseUrl(settings.baseUrl);
  if (typeof settings.apiKey !== 'string' || !settings.apiKey.trim()) throw new Error('Укажите API-ключ');
  const url = new URL(`${base}/api/v1/anonymize`);
  if (jobId !== undefined) {
    if (typeof jobId !== 'string' || !jobId) throw new Error('Некорректный идентификатор задания');
    url.searchParams.set('jobId', jobId);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(url, { method, body, headers: { Authorization: `Bearer ${settings.apiKey}` }, credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal });
    if (!response.ok) {
      const hints = {
        400: 'Проверьте текст и настройки обработки.',
        401: 'Проверьте API-ключ в настройках.',
        402: 'Квота аккаунта исчерпана.',
        403: 'Нет доступа к обработке.',
        404: 'Проверьте адрес сайта; задание также могло истечь или исчезнуть после перезапуска.',
        413: 'Текст превышает ограничение сервера.',
        429: 'Слишком много запросов. Попробуйте позже.'
      };
      const error = new Error(`Ошибка сервера (${response.status}). ${hints[response.status] || 'Попробуйте позже или отмените задание.'}`);
      error.status = response.status;
      throw error;
    }
    let data;
    try { data = await response.json(); } catch { throw new Error('Некорректный ответ сервера'); }
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Некорректный ответ сервера');
    return data;
  } catch (error) {
    if (error instanceof Error && (error.message.startsWith('Ошибка сервера (') || error.message === 'Некорректный ответ сервера')) throw error;
    throw new Error(controller.signal.aborted ? 'Превышено время ожидания сервера' : 'Не удалось связаться с сервером');
  } finally { clearTimeout(timer); }
}

export async function submitJob(settings, text) {
  if (typeof text !== 'string') throw new Error('Некорректный текст');
  const body = new FormData();
  body.append('file', new Blob([text], { type: 'text/plain;charset=utf-8' }), 'selection.txt');
  const data = await request(settings, 'POST', undefined, body);
  if (typeof data.jobId !== 'string' || !data.jobId || data.done !== false) throw new Error('Некорректный ответ сервера');
  return data;
}

export async function pollJob(settings, jobId) {
  const data = await request(settings, 'GET', jobId);
  if (typeof data.done !== 'boolean' || (data.cancelled !== undefined && typeof data.cancelled !== 'boolean')) throw new Error('Некорректный ответ сервера');
  if (data.done && (typeof data.anonymized_text !== 'string' || !data.mapping || typeof data.mapping !== 'object' || Array.isArray(data.mapping) || Object.entries(data.mapping).some(([key, value]) => !/^\[[A-Z_]+_\d+\]$/.test(key) || typeof value !== 'string'))) throw new Error('Некорректный ответ сервера');
  return data;
}

export async function cancelJob(settings, jobId) { return request(settings, 'DELETE', jobId); }
