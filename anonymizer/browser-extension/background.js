import { createController } from './controller.js';
import { pageSelection } from './selection.js';
import { pageInspector } from './inspector.js';
import { remoteApi } from './transport.js';

const controller = createController({
  storage: chrome.storage.session,
  counterStorage: chrome.storage.local,
  hasPermission: origin => chrome.permissions.contains({ origins: [origin] }),
  api: remoteApi
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: 'mask-selection', title: 'Маскировать выделенное', contexts: ['selection'] });
    chrome.contextMenus.create({ id: 'restore-selection', title: 'Восстановить выделенное', contexts: ['selection'] });
    chrome.contextMenus.create({ id: 'inspect-selection', title: 'Показать замены Anon', contexts: ['selection'] });
  });
});

// Do not await before open(): Chrome requires the original user gesture.
function openPanel(tab) {
  return chrome.sidePanel.open({ windowId: tab.windowId });
}

function readSelection() {
  let element = document.activeElement;
  while (element?.shadowRoot?.activeElement) element = element.shadowRoot.activeElement;
  if (element instanceof HTMLInputElement && element.type === 'password') return '';
  if (element instanceof HTMLTextAreaElement || element instanceof HTMLInputElement) {
    if (typeof element.selectionStart === 'number' && typeof element.selectionEnd === 'number') {
      return element.value.slice(element.selectionStart, element.selectionEnd);
    }
  }
  return window.getSelection()?.toString() || '';
}

async function captureFromTab(tab, mode, selectionText, frameId) {
  if (!tab?.id) return;
  // Open synchronously before selection extraction or storage operations.
  const opening = openPanel(tab);
  try {
    await installInspector({ tabId: tab.id, frameId: frameId || 0 }).catch(() => {});
    let text = selectionText;
    if (!text) {
      const target = { tabId: tab.id };
      if (Number.isInteger(frameId)) target.frameIds = [frameId];
      const results = await chrome.scripting.executeScript({ target, func: readSelection });
      text = results.find(result => result.result)?.result;
    }
    if (text?.trim()) await controller.capture(text, mode);
    await opening;
  } catch {
    // Restricted pages can still use the panel's paste field. Never log selected text.
    await opening.catch(() => {});
  }
}

chrome.action.onClicked.addListener(tab => { void captureFromTab(tab, 'mask'); });
const POLL_ALARM = 'anon-direct-poll';
let processing = null;
// Only documents explicitly activated by the user may request one-token hover lookups.
const inspectorDocuments = new Map();
function inspectorKey(target) { return `${target.tabId}:${target.documentId || `frame-${target.frameId || 0}`}`; }
async function installInspector(target) {
  await chrome.scripting.executeScript({ target: targetOf(target), func: pageSelection, args: ['install'] });
  const results = await chrome.scripting.executeScript({ target: targetOf(target), func: pageInspector, args: ['install'] });
  for (const item of results) {
    if (!item.result?.ok) continue;
    const key = inspectorKey({ tabId: target.tabId, documentId: item.documentId, frameId: item.frameId });
    inspectorDocuments.delete(key);
    inspectorDocuments.set(key, Date.now());
  }
  while (inspectorDocuments.size > 100) inspectorDocuments.delete(inspectorDocuments.keys().next().value);
}

function targetOf(target) {
  return target.documentId
    ? { tabId: target.tabId, documentIds: [target.documentId] }
    : { tabId: target.tabId, frameIds: [target.frameId || 0] };
}

async function execute(target, action, payload = {}) {
  const results = await chrome.scripting.executeScript({ target: targetOf(target), func: pageSelection, args: [action, payload] });
  return results[0]?.result || { error: 'Страница недоступна.' };
}

async function notify(target, message, error = false, phase = error ? 'error' : 'success') {
  await controller.recordNotice(message, error, phase).catch(() => {});
  try { await execute(target, 'notify', { message, error, phase }); } catch { /* Tab may be closed or navigated away. */ }
}

async function deliver(delivery) {
  try {
    if (delivery.error) {
      await notify(delivery.target, delivery.error, true);
    } else if (delivery.unchanged) {
      await notify(delivery.target, delivery.warningCount
        ? 'Текст не изменён. Проверьте предупреждения в истории Anon.'
        : 'Совпадения для маскирования не найдены. Текст не изменён.', Boolean(delivery.warningCount), delivery.warningCount ? 'warning' : 'success');
    } else {
      const result = await execute(delivery.target, 'apply', { id: delivery.target.id, text: delivery.text, edits: delivery.edits });
      await notify(delivery.target, result.error
        ? `${result.error} Результат сохранён в истории Anon.`
        : delivery.warningCount ? 'Текст заменён. В истории Anon есть предупреждения обработки.' : 'Текст замаскирован.',
      Boolean(result.error || delivery.warningCount), result.error ? 'error' : delivery.warningCount ? 'warning' : 'success');
    }
  } catch {
    // Keep the result in history even when the original document no longer exists.
  } finally {
    await execute(delivery.target, 'release', { id: delivery.target.id }).catch(() => {});
    await controller.acknowledgeDelivery(delivery.id);
  }
}

// Chrome alarms resume polling after service-worker suspension. Short polls keep
// normal interactions responsive; no open panel is needed to complete a job.
function processDirect() {
  if (processing) return processing;
  processing = (async () => {
    let failures = 0;
    while (true) {
      const task = await controller.directTask();
      if (task.delivery) { await deliver(task.delivery); continue; }
      if (!task.active) { await chrome.alarms.clear(POLL_ALARM); return; }
      try { await controller.dispatch({ type: 'POLL_JOB' }); failures = 0; }
      catch (error) {
        failures++;
        if (failures === 1) await notify(task.active.target, `${error.message}. Повторяем проверку состояния…`, false, 'retrying');
      }
      const next = await controller.directTask();
      if (!next.active) continue;
      await new Promise(resolve => setTimeout(resolve, Math.min(30_000, 1500 * 2 ** Math.min(failures, 5))));
    }
  })().catch(() => {}).finally(() => { processing = null; });
  return processing;
}

async function directSelection(tab, mode, frameId) {
  // The command API documents tab as optional (some browser invocations omit it).
  if (!tab?.id) {
    try { [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true }); }
    catch { /* Report below without exposing browser URLs. */ }
  }
  if (!tab?.id) {
    await controller.recordNotice('Не найдена активная страница. Откройте страницу с выделенным текстом.', true);
    return;
  }
  let target = { tabId: tab.id, frameId: frameId || 0 };
  let capture;
  try {
    // A cross-origin ad/frame must not prevent capture in the main page.
    // Inspect other frames only when focus is actually inside an iframe.
    const scriptTarget = { tabId: tab.id, frameIds: [Number.isInteger(frameId) ? frameId : 0] };
    const captureOptions = { usePrevious: mode === 'restore' };
    let results = await chrome.scripting.executeScript({ target: scriptTarget, func: pageSelection, args: ['capture', captureOptions] });
    if (!Number.isInteger(frameId) && results.some(item => item.result?.error === 'Выделение находится во вложенном окне.')) {
      try {
        results = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: pageSelection, args: ['capture', captureOptions] });
      } catch {
        await notify(target, 'Нет доступа к выделению во вложенном окне. Используйте панель Anon для этого текста.', true);
        return;
      }
    }
    const found = results.find(item => item.result?.id && item.result?.text);
    if (!found) {
      await notify(target, results.find(item => item.result?.error)?.result.error || 'Выделите текст на странице.', true);
      return;
    }
    capture = found.result;
    target = { tabId: tab.id, frameId: found.frameId, documentId: found.documentId, id: capture.id };
    await installInspector(target).catch(() => {});
    if (mode === 'inspect') {
      const tokens = [...new Set(capture.text.match(/\[[A-Z_]+_\d+\]/g) || [])];
      if (!tokens.length) throw new Error('В выделении нет плейсхолдеров Anon.');
      if (tokens.length > 200) throw new Error('Выберите фрагмент не более чем с 200 разными плейсхолдерами.');
      const result = await controller.lookupTokens(tokens);
      await execute(target, 'release', { id: capture.id });
      await chrome.scripting.executeScript({ target: targetOf(target), func: pageInspector, args: ['show', result] });
      return;
    }
    if (mode === 'restore') {
      await notify(target, 'Anon: восстанавливаем выделенный текст…', false, 'restoring');
      const restored = await controller.dispatch({ type: 'RESTORE_AUTO', text: capture.text });
      const applied = await execute(target, 'apply', { id: capture.id, text: restored.text, edits: restored.edits });
      if (applied.error) throw new Error(applied.error);
      await notify(target, restored.unknown.length ? 'Известные значения восстановлены. Часть плейсхолдеров не найдена.' : 'Текст восстановлен.', Boolean(restored.unknown.length), restored.unknown.length ? 'warning' : 'success');
      return;
    }
    // Create the watchdog before submitting: the worker can be suspended at any time.
    await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 1 });
    await notify(target, 'Отправляем выделенный текст на сервер Anon…', false, 'sending');
    await controller.dispatch({ type: 'START_MASK', text: capture.text, directTarget: target });
    await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 1 });
    await notify(target, 'Запрос принят. Маскируем текст…', false, 'processing');
    void processDirect();
  } catch (error) {
    if (capture) await execute(target, 'release', { id: capture.id }).catch(() => {});
    await notify(target, error.message || 'Не удалось обработать выделение. Откройте Anon для проверки настроек.', true);
    void processDirect();
  }
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!['mask-selection', 'restore-selection', 'inspect-selection'].includes(info.menuItemId)) return;
  void directSelection(tab, info.menuItemId === 'inspect-selection' ? 'inspect' : info.menuItemId === 'restore-selection' ? 'restore' : 'mask', info.frameId);
});
chrome.commands.onCommand.addListener((command, tab) => {
  if (!['mask-selection', 'restore-selection', 'inspect-selection'].includes(command)) return;
  void directSelection(tab, command === 'inspect-selection' ? 'inspect' : command === 'restore-selection' ? 'restore' : 'mask');
});

chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === POLL_ALARM) void processDirect(); });
chrome.runtime.onStartup.addListener(() => { void processDirect(); });

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === 'LOOKUP_PLACEHOLDER') {
    if (sender.id !== chrome.runtime.id || !Number.isInteger(sender.tab?.id) || !sender.documentId) return false;
    const activated = inspectorDocuments.get(inspectorKey({ tabId: sender.tab.id, documentId: sender.documentId }));
    if (!activated || Date.now() - activated > 12 * 60 * 60 * 1000) return false;
    controller.lookupTokens([message.token]).then(
      result => sendResponse({ ok: true, ...result }),
      () => sendResponse({ ok: false, error: 'Не удалось найти замену.' })
    );
    return true;
  }
  // Other content-script messages must never access state, keys or start network requests.
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('panel.html')) return false;
  if (!message || typeof message.type !== 'string') return false;
  controller.dispatch(message).then(
    result => { sendResponse({ ok: true, ...result }); void processDirect(); },
    error => sendResponse({ ok: false, error: error.message || 'Не удалось выполнить действие.' })
  );
  return true;
});
