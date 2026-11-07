import { validateBaseUrl, serverOrigin } from './core.js';

const $ = id => document.getElementById(id);
const input = $('input');
const result = $('result');
let state = { settings: {}, records: [], selectedId: null, activeJob: null };
let pollTimer = null;
let polling = false;
let busy = false;
let observedJob = null;
let knownIds = new Set();
let settingsLoaded = false;
let consuming = null;
let displayedSelection = null;
let initialState = true;

async function send(type, payload = {}) {
  const response = await chrome.runtime.sendMessage({ type, ...payload });
  if (!response?.ok) throw new Error(response?.error || 'Не удалось выполнить действие');
  return response;
}
function status(message, error = false) {
  $('status').textContent = message;
  $('status').classList.toggle('error', error);
}
function showError(error) { status(error?.message || 'Не удалось выполнить действие', true); }
function controls() {
  const working = busy || Boolean(state.activeJob);
  $('mask').disabled = working;
  $('restore').disabled = working || !state.selectedId;
  $('cancel').hidden = !state.activeJob;
  $('cancel').disabled = busy;
  $('delete').disabled = busy || !state.selectedId;
  $('clear').disabled = busy || !state.records.length;
  $('copy').disabled = !result.value;
  $('settings').querySelector('button').disabled = working;
}
function showWarnings(warnings) {
  $('warnings').textContent = Array.isArray(warnings) ? warnings.map(item => typeof item === 'string' ? item : JSON.stringify(item)).join('\n') : '';
}
function renderRecords() {
  const container = $('records');
  container.replaceChildren();
  $('record-count').textContent = String(state.records.length);
  if (!state.records.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = 'Записей пока нет';
    container.append(empty);
    return;
  }
  for (const record of state.records) {
    const label = document.createElement('label');
    label.className = 'record';
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = 'record'; radio.value = record.id;
    radio.checked = record.id === state.selectedId;
    radio.disabled = busy || Boolean(state.activeJob);
    radio.addEventListener('change', async () => {
      try { const response = await send('SELECT_RECORD', { id: record.id }); await update(response.state); }
      catch (error) { showError(error); renderRecords(); }
    });
    const details = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = record.title || 'Без названия';
    const meta = document.createElement('small');
    const date = new Date(record.createdAt);
    const formatted = Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU');
    const count = Object.keys(record.mapping || {}).length;
    meta.textContent = `${formatted} · Замен: ${count}`;
    details.append(title, meta); label.append(radio, details); container.append(label);
  }
}
async function update(next) {
  if (!next) return;
  const previousJob = observedJob;
  state = { ...state, ...next, records: next.records || [] };
  if (!settingsLoaded) {
    $('base-url').value = state.settings?.baseUrl || '';
    $('api-key').value = state.settings?.apiKey || '';
    $('settings').closest('details').open = !state.settings?.baseUrl || !state.settings?.apiKey;
    settingsLoaded = true;
  }
  if (state.activeJob) {
    if (!previousJob) knownIds = new Set(state.records.map(record => record.id));
    observedJob = state.activeJob.jobId;
  } else if (previousJob) {
    const created = state.records.find(record => !knownIds.has(record.id));
    if (created) {
      result.value = created.maskedText || '';
      showWarnings(created.warnings);
      status('Маскирование завершено');
    }
    observedJob = null;
  }
  if (initialState || state.selectedId !== displayedSelection) {
    const selected = state.records.find(record => record.id === state.selectedId);
    result.value = selected?.maskedText || '';
    showWarnings(selected?.warnings);
    displayedSelection = state.selectedId;
    initialState = false;
  }
  renderRecords(); controls();
  if (state.activeJob && !pollTimer) pollTimer = setInterval(poll, 1500);
  if (!state.activeJob && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  const capture = state.capture;
  if (capture?.nonce && consuming !== capture.nonce) {
    consuming = capture.nonce;
    input.value = capture.text || '';
    $('mode-label').textContent = capture.mode === 'restore' ? 'Восстановление' : 'Маскирование';
    try { await send('CONSUME_CAPTURE', { nonce: capture.nonce }); }
    catch (error) { showError(error); }
  }
}
async function poll() {
  if (polling) return;
  polling = true;
  try { const response = await send('POLL_JOB'); await update(response.state); }
  catch (error) {
    showError(error);
    try { const response = await send('GET_STATE'); await update(response.state); } catch { /* Keep the polling error visible. */ }
  }
  finally { polling = false; }
}
async function action(callback) {
  if (busy) return;
  busy = true; controls();
  try { await callback(); } catch (error) { showError(error); }
  finally { busy = false; controls(); renderRecords(); }
}
$('mask').addEventListener('click', () => action(async () => {
  if (!input.value.trim()) throw new Error('Введите текст для маскирования');
  $('mode-label').textContent = 'Маскирование';
  knownIds = new Set(state.records.map(record => record.id));
  const response = await send('START_MASK', { text: input.value });
  result.value = '';
  showWarnings([]);
  await update(response.state);
  status('Маскирование выполняется…');
}));
$('restore').addEventListener('click', () => action(async () => {
  if (!input.value) throw new Error('Введите текст для восстановления');
  const response = await send('RESTORE', { text: input.value, id: state.selectedId });
  result.value = response.text;
  $('mode-label').textContent = 'Восстановление';
  showWarnings(response.unknown?.length ? [`Не найдены соответствия: ${response.unknown.join(', ')}`] : []);
  status(response.unknown?.length ? 'Часть плейсхолдеров не восстановлена' : 'Текст восстановлен');
}));
$('cancel').addEventListener('click', () => action(async () => {
  const response = await send('CANCEL_JOB'); await update(response.state); status('Маскирование отменено');
}));
$('clear-text').addEventListener('click', () => {
  input.value = ''; result.value = ''; showWarnings([]); status('Текст очищен'); controls();
});
$('copy').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(result.value); status('Результат скопирован'); }
  catch (error) { showError(error); }
});
$('delete').addEventListener('click', () => action(async () => {
  if (!confirm('Удалить запись? Восстановить текст с её помощью больше не получится.')) return;
  const response = await send('DELETE_RECORD', { id: state.selectedId }); await update(response.state); status('Запись удалена');
}));
$('clear').addEventListener('click', () => action(async () => {
  if (!confirm('Удалить все записи? Восстановить текст с их помощью больше не получится.')) return;
  const response = await send('CLEAR_RECORDS'); await update(response.state); status('История очищена');
}));
$('settings').addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || state.activeJob) return;
  try {
    const baseUrl = validateBaseUrl($('base-url').value.trim());
    const apiKey = $('api-key').value.trim();
    if (!apiKey) throw new Error('Введите API-ключ');
    const granted = await chrome.permissions.request({ origins: [`${serverOrigin(baseUrl)}/*`] });
    if (!granted) throw new Error('Доступ к адресу сервиса не предоставлен');
    await action(async () => {
      const response = await send('SAVE_SETTINGS', { settings: { baseUrl, apiKey } });
      await update(response.state); $('base-url').value = baseUrl; status('Настройки сохранены');
    });
  } catch (error) { showError(error); }
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'session' && changes.anonState) send('GET_STATE').then(response => update(response.state)).catch(showError);
});
controls();
send('GET_STATE').then(response => update(response.state)).catch(showError);
