import { validateBaseUrl } from './core.js';

function failure(message, code, status) {
  const error = new Error(message);
  error.code = code;
  if (status !== undefined) error.status = status;
  return error;
}
const invalidResponse = () => failure('Некорректный ответ сервера', 'INVALID_RESPONSE');

async function request(settings, method, jobId, body, expectedMissingJob = false) {
  const base = validateBaseUrl(settings.baseUrl);
  if (typeof settings.apiKey !== 'string' || !settings.apiKey.trim()) throw new Error('Укажите API-ключ');
  const url = new URL(`${base}/api/v1/anonymize`);
  if (jobId !== undefined) {
    if (typeof jobId !== 'string' || !jobId) throw new Error('Некорректный идентификатор задания');
    url.searchParams.set('jobId', jobId);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 120000);
  try {
    const response = await fetch(url, { method, body, headers: { Authorization: `Bearer ${settings.apiKey}` }, credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal });
    if (!response.ok && !(expectedMissingJob && response.status === 400)) {
      const hints = {
        400: 'Проверьте текст и настройки обработки.',
        401: 'Проверьте API-ключ в настройках.',
        402: 'Квота аккаунта исчерпана.',
        403: 'Нет доступа к обработке.',
        404: 'Проверьте адрес сайта; задание также могло истечь или исчезнуть после перезапуска.',
        413: 'Текст превышает ограничение сервера.',
        429: 'Слишком много запросов. Попробуйте позже.',
        502: 'Сайт не получил корректный ответ от сервиса обработки.',
        503: 'Сервис временно недоступен. Попробуйте позже.',
        504: 'Сервер не дождался ответа сервиса обработки.'
      };
      throw failure(`Ошибка сервера (${response.status}). ${hints[response.status] || 'Попробуйте позже или отмените задание.'}`, 'HTTP', response.status);
    }
    let data;
    try { data = await response.json(); } catch { throw invalidResponse(); }
    if (!data || typeof data !== 'object' || Array.isArray(data)) throw invalidResponse();
    if (expectedMissingJob && (response.status !== 400 || data.error !== 'jobId обязателен')) throw invalidResponse();
    return data;
  } catch (error) {
    if (error?.code === 'HTTP' || error?.code === 'INVALID_RESPONSE') throw error;
    if (controller.signal.aborted) throw failure(method === 'POST' ? 'Превышено время ожидания. Задание могло быть создано; повторная отправка может создать ещё одно задание.' : 'Превышено время ожидания сервера', 'TIMEOUT');
    throw failure('Не удалось связаться с сервером', 'NETWORK');
  } finally { clearTimeout(timer); }
}

export async function submitJob(settings, text) {
  if (typeof text !== 'string') throw new Error('Некорректный текст');
  const body = new FormData();
  body.append('file', new Blob([text], { type: 'text/plain;charset=utf-8' }), 'selection.txt');
  const data = await request(settings, 'POST', undefined, body);
  if (typeof data.jobId !== 'string' || !data.jobId || data.done !== false) throw invalidResponse();
  return data;
}

export async function pollJob(settings, jobId) {
  const data = await request(settings, 'GET', jobId);
  if (typeof data.done !== 'boolean' || (data.cancelled !== undefined && typeof data.cancelled !== 'boolean')) throw invalidResponse();
  if (data.done && (typeof data.anonymized_text !== 'string' || !data.mapping || typeof data.mapping !== 'object' || Array.isArray(data.mapping) || Object.entries(data.mapping).some(([key, value]) => !/^\[[A-Z_]+_\d+\]$/.test(key) || typeof value !== 'string'))) throw invalidResponse();
  return data;
}

export async function cancelJob(settings, jobId) { return request(settings, 'DELETE', jobId); }
export async function checkConnection(settings) { await request(settings, 'GET', undefined, undefined, true); return { authenticated: true }; }
