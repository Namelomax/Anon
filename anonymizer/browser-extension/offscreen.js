import * as api from './api.js';

const operations = new Set(['submitJob', 'pollJob', 'cancelJob', 'checkConnection']);
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== 'anon-offscreen') return;
  if (sender.id !== chrome.runtime.id || sender.tab || (sender.url && sender.url !== chrome.runtime.getURL('background.js')) || !operations.has(message.operation)) {
    sendResponse({ ok: false, error: 'Недопустимый запрос' });
    return;
  }
  const argument = message.operation === 'submitJob' ? message.text : message.jobId;
  Promise.resolve().then(() => api[message.operation](message.settings, argument)).then(
    data => sendResponse({ ok: true, data }),
    error => {
      const safe = { ok: false, error: 'Не удалось выполнить запрос' };
      if (['HTTP', 'TIMEOUT', 'NETWORK', 'INVALID_RESPONSE'].includes(error?.code)) {
        safe.error = error.message;
        safe.code = error.code;
        if (typeof error.status === 'number') safe.status = error.status;
      }
      sendResponse(safe);
    }
  );
  return true;
});
