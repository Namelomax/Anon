import { createRecord, restoreText, validateBaseUrl, serverOrigin } from './core.js';
import { submitJob, pollJob, cancelJob } from './api.js';

export const STATE_KEY = 'anonState';
export const MAX_TEXT = 100_000;
const MAX_RECORDS = 50;
const MAX_STORAGE_BYTES = 8_000_000;

function initialState() {
  return { settings: { baseUrl: '', apiKey: '' }, records: [], selectedId: null, capture: null, activeJob: null };
}

function requireText(value) {
  if (typeof value !== 'string' || !value.trim()) throw new Error('Выделите или вставьте текст.');
  if (value.length > MAX_TEXT) throw new Error('Выберите фрагмент до 100 000 символов.');
  return value;
}

// Only the extension panel receives this view. Job source and credentials snapshot stay private.
function publicState(state) {
  return { ...state, activeJob: state.activeJob && { jobId: state.activeJob.jobId, startedAt: state.activeJob.startedAt } };
}

export function createController({ storage, hasPermission, api = { submitJob, pollJob, cancelJob } }) {
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
    const baseUrl = validateBaseUrl(settings?.baseUrl);
    const apiKey = settings?.apiKey?.trim();
    if (typeof apiKey !== 'string' || !/^anon_[A-Za-z0-9_-]+$/.test(apiKey) || apiKey.length > 512) {
      throw new Error('Введите персональный API-ключ Anon из раздела «Для разработчиков».');
    }
    if (!await hasPermission(serverOrigin(baseUrl) + '/*')) {
      throw new Error('Разрешите расширению доступ к выбранному серверу в настройках.');
    }
    return { baseUrl, apiKey };
  }
  return {
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
          case 'START_MASK': {
            if (state.activeJob) throw new Error('Обработка уже выполняется.');
            if (state.records.length >= MAX_RECORDS) throw new Error('Сохранено 50 таблиц. Удалите ненужные перед новой обработкой.');
            const text = requireText(message.text);
            const settings = await checkSettings(state.settings);
            const result = await api.submitJob(settings, text);
            state.activeJob = { jobId: result.jobId, startedAt: Date.now(), source: text, settings };
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
                state.activeJob = null;
                await write(state);
              }
              throw error;
            }
            if (result.cancelled) state.activeJob = null;
            else if (result.done) {
              const record = createRecord(job.source, result);
              state.records.unshift(record);
              state.selectedId = record.id;
              state.activeJob = null;
            }
            break;
          }
          case 'CANCEL_JOB':
            if (state.activeJob) {
              await api.cancelJob(state.activeJob.settings, state.activeJob.jobId);
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
          default: throw new Error('Неизвестное действие расширения.');
        }
        await write(state);
        return { state: publicState(state) };
      });
    }
  };
}
