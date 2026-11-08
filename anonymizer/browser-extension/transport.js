import * as api from './api.js';

let creating;
export async function ensureOffscreen() {
  if (!globalThis.chrome?.offscreen) return;
  if (!creating) {
    creating = (async () => {
      const url = chrome.runtime.getURL('offscreen.html');
      const contexts = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'], documentUrls: [url] });
      if (!contexts.length) await chrome.offscreen.createDocument({ url: 'offscreen.html', reasons: ['BLOBS'], justification: 'Создание текстового Blob для отправки файла и ожидание ответа сервера при закрытой панели.' });
    })().finally(() => { creating = undefined; });
  }
  await creating;
}

async function call(operation, settings, extra = {}) {
  if (!globalThis.chrome?.offscreen) return api[operation](settings, extra.text ?? extra.jobId);
  await ensureOffscreen();
  const result = await chrome.runtime.sendMessage({ target: 'anon-offscreen', operation, settings, ...extra });
  if (result?.ok === true) return result.data;
  const error = new Error(typeof result?.error === 'string' ? result.error : 'Не удалось связаться с обработчиком');
  if (typeof result?.status === 'number') error.status = result.status;
  if (typeof result?.code === 'string') error.code = result.code;
  throw error;
}
export const remoteApi = {
  submitJob: (settings, text) => call('submitJob', settings, { text }),
  pollJob: (settings, jobId) => call('pollJob', settings, { jobId }),
  cancelJob: (settings, jobId) => call('cancelJob', settings, { jobId }),
  checkConnection: settings => call('checkConnection', settings)
};
