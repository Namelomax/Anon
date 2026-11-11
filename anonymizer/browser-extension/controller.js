import { createRecord, restoreText, restorationEdits, validateBaseUrl, serverOrigin } from './core.js';
import { submitJob, pollJob, cancelJob } from './api.js';

export const STATE_KEY = 'anonState';
export const COUNTER_KEY = 'anonTokenCounters';
export const MAX_TEXT = 100_000;
const MAX_RECORDS = 50;
const MAX_STORAGE_BYTES = 8_000_000;

function initialState() {
  return { settings: { baseUrl: '', apiKey: '' }, records: [], selectedId: null, capture: null, activeJob: null, directDelivery: null };
}

function requireText(value) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('Выделите или вставьте текст.');
  if (value.length > MAX_TEXT) throw new Error('Выберите фрагмент до 100 000 символов.');
  return value;
}

// Only the extension panel receives this view. Job source and credentials snapshot stay private.
function publicState(state) {
  const { directDelivery, ...visible } = state;
  return { ...visible, activeJob: state.activeJob && { jobId: state.activeJob.jobId, startedAt: state.activeJob.startedAt } };
}

export function createController({ storage, counterStorage = storage, hasPermission, api = { submitJob, pollJob, cancelJob } }) {
  // Serializes read/modify/write even when two side panels are open.
  let queue = Promise.resolve();
  function run(task) {
    const result = queue.then(task);
    queue = result.catch(() => {});
    return result;
  }
  async function read() {
    return (await storage.get(STATE_KEY))[STATE_KEY] || initialState();
  }
  async function write(state) {
    if (new TextEncoder().encode(JSON.stringify(state)).length > MAX_STORAGE_BYTES) {
      throw new Error('Память сессии заполнена. Удалите ненужные таблицы соответствий.');
    }
    await storage.set({ [STATE_KEY]: state });
  }
  async function checkSettings(settings) {
    if (!settings?.baseUrl || !settings?.apiKey) throw new Error('Сначала откройте AnonAI и сохраните адрес сервиса и API-ключ.');
    const baseUrl = validateBaseUrl(settings?.baseUrl);
    const apiKey = settings?.apiKey?.trim();
    if (typeof apiKey !== 'string' || !/^anon_[A-Za-z0-9_-]+$/.test(apiKey) || apiKey.length > 512) {
      throw new Error('Введите персональный API-ключ AnonAI из раздела «Для разработчиков».');
    }
    if (!await hasPermission(serverOrigin(baseUrl) + '/*')) {
      throw new Error('Разрешите расширению доступ к выбранному серверу в настройках.');
    }
    return { baseUrl, apiKey };
  }
  return {
    lookupTokens(tokens) {
      return run(async () => {
        if (!Array.isArray(tokens) || tokens.length > 200 || tokens.some(token => typeof token !== 'string' || token.length > 160 || !/^\[[A-Z_]+_\d+\]$/.test(token))) {
          throw new Error('Некорректный список плейсхолдеров.');
        }
        const state = await read();
        return { entries: [...new Set(tokens)].map(token => {
          const values = new Set(state.records.filter(record => Object.hasOwn(record.mapping, token)).map(record => record.mapping[token]));
          return values.size > 1 ? { token, original: null, conflict: true } : { token, original: values.size ? [...values][0] : null };
        }) };
      });
    },
    directTask() {
      return run(async () => {
        const state = await read();
        return {
          active: state.activeJob?.directTarget ? { target: state.activeJob.directTarget, startedAt: state.activeJob.startedAt } : null,
          delivery: state.directDelivery || null
        };
      });
    },
    acknowledgeDelivery(id) {
      return run(async () => {
        const state = await read();
        if (state.directDelivery?.id === id) { state.directDelivery = null; await write(state); }
      });
    },
    recordNotice(message, error = false, phase = error ? 'error' : 'success') {
      return run(async () => {
        const state = await read();
        state.notice = { message, error, phase, id: crypto.randomUUID() };
        await write(state);
      });
    },
    capture(text, mode) {
      return run(async () => {
        const state = await read();
        state.capture = { text: requireText(text), mode, nonce: crypto.randomUUID() };
        await write(state);
      });
    },
    dispatch(message) {
      return run(async () => {
        const state = await read();
        switch (message.type) {
          case 'GET_STATE': return { state: publicState(state) };
          case 'SAVE_SETTINGS':
            if (state.activeJob) throw new Error('Сначала завершите или отмените текущую обработку.');
            state.settings = await checkSettings(message.settings);
            break;
          case 'CHECK_CONNECTION':
            return api.checkConnection(await checkSettings(state.settings));
          case 'START_MASK': {
            if (state.activeJob) throw new Error('Обработка уже выполняется.');
            if (state.directDelivery) throw new Error('Дождитесь подстановки предыдущего результата.');
            if (state.records.length >= MAX_RECORDS) throw new Error('Сохранено 50 таблиц. Удалите ненужные перед новой обработкой.');
            const text = requireText(message.text);
            const settings = await checkSettings(state.settings);
            const result = await api.submitJob(settings, text);
            state.activeJob = { jobId: result.jobId, startedAt: Date.now(), source: text, settings,
              ...(message.directTarget ? { directTarget: message.directTarget } : {}) };
            break;
          }
          case 'POLL_JOB': {
            if (!state.activeJob) return { state: publicState(state) };
            const job = state.activeJob;
            let result;
            try {
              result = await api.pollJob(job.settings, job.jobId);
            } catch (error) {
              if ([400, 401, 402, 403, 404, 410].includes(error.status)) {
                if (job.directTarget) state.directDelivery = { id: crypto.randomUUID(), target: job.directTarget, error: error.message };
                state.activeJob = null;
                await write(state);
              }
              throw error;
            }
            if (result.cancelled) state.activeJob = null;
            else if (result.done) {
              let record;
              try {
                const counters = { ...((await counterStorage.get(COUNTER_KEY))[COUNTER_KEY] || {}) };
                // Also respect existing mappings when upgrading/recovering metadata.
                for (const existing of state.records) {
                  for (const token of Object.keys(existing.mapping)) {
                    const match = /^\[([A-Z_]+)_(\d+)\]$/.exec(token);
                    if (match && Number.isSafeInteger(Number(match[2]))) {
                      counters[match[1]] = Math.max(counters[match[1]] || 0, Number(match[2]));
                    }
                  }
                }
                const { tokenCounters, ...created } = createRecord(job.source, result, counters);
                // Reserve numbers first. They contain no original values and
                // must survive clearing mappings/restarting the browser so old
                // text cannot silently restore using a newly reused number.
                await counterStorage.set({ [COUNTER_KEY]: tokenCounters });
                record = created;
              }
              catch (error) {
                if (job.directTarget) state.directDelivery = { id: crypto.randomUUID(), target: job.directTarget, error: error.message };
                state.activeJob = null;
                await write(state);
                throw error;
              }
              state.records.unshift(record);
              state.selectedId = record.id;
              if (job.directTarget) state.directDelivery = {
                id: crypto.randomUUID(), target: job.directTarget, text: record.maskedText,
                edits: record.edits,
                warningCount: Array.isArray(record.warnings) ? record.warnings.length : 0,
                unchanged: record.maskedText === job.source
              };
              state.activeJob = null;
            }
            break;
          }
          case 'CANCEL_JOB':
            if (state.activeJob) {
              await api.cancelJob(state.activeJob.settings, state.activeJob.jobId);
              if (state.activeJob.directTarget) state.directDelivery = {
                id: crypto.randomUUID(), target: state.activeJob.directTarget, error: 'Маскирование отменено.'
              };
              state.activeJob = null;
            }
            break;
          case 'SELECT_RECORD':
            if (!state.records.some(record => record.id === message.id)) throw new Error('Таблица соответствий не найдена.');
            state.selectedId = message.id;
            break;
          case 'DELETE_RECORD':
            state.records = state.records.filter(record => record.id !== message.id);
            if (state.selectedId === message.id) state.selectedId = state.records[0]?.id || null;
            break;
          case 'CLEAR_RECORDS':
            state.records = [];
            state.selectedId = null;
            break;
          case 'CONSUME_CAPTURE':
            if (state.capture?.nonce === message.nonce) state.capture = null;
            break;
          case 'RESTORE': {
            const record = state.records.find(record => record.id === message.id);
            if (!record) throw new Error('Выберите таблицу соответствий для восстановления.');
            return restoreText(requireText(message.text), record.mapping);
          }
          case 'RESTORE_AUTO': {
            const text = requireText(message.text);
            const mapping = Object.create(null);
            for (const record of state.records) {
              for (const [key, value] of Object.entries(record.mapping)) {
                if (Object.hasOwn(mapping, key) && mapping[key] !== value) throw new Error('Конфликт таблиц соответствий. Выберите запись в панели AnonAI.');
                mapping[key] = value;
              }
            }
            const tokens = text.match(/\[[A-Z_]+_\d+\]/g) || [];
            const restoredCount = tokens.filter(token => Object.hasOwn(mapping, token)).length;
            if (!restoredCount) throw new Error('Не найдены плейсхолдеры из текущей сессии AnonAI.');
            return { ...restoreText(text, mapping), restoredCount, edits: restorationEdits(text, mapping) };
          }
          default: throw new Error('Неизвестное действие расширения.');
        }
        await write(state);
        return { state: publicState(state) };
      });
    }
  };
}
