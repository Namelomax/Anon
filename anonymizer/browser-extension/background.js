import { createController } from './controller.js';

const controller = createController({
  storage: chrome.storage.session,
  hasPermission: origin => chrome.permissions.contains({ origins: [origin] })
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({ id: 'mask-selection', title: 'Маскировать выделенное в Anon', contexts: ['selection'] });
    chrome.contextMenus.create({ id: 'restore-selection', title: 'Восстановить выделенное в Anon', contexts: ['selection'] });
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
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (!['mask-selection', 'restore-selection'].includes(info.menuItemId)) return;
  void captureFromTab(tab, info.menuItemId === 'restore-selection' ? 'restore' : 'mask', info.selectionText, info.frameId);
});
chrome.commands.onCommand.addListener((command, tab) => {
  if (!['mask-selection', 'restore-selection'].includes(command)) return;
  void captureFromTab(tab, command === 'restore-selection' ? 'restore' : 'mask');
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // Page/content-script messages must never access keys, mappings or start network requests.
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('panel.html')) return false;
  if (!message || typeof message.type !== 'string') return false;
  controller.dispatch(message).then(
    result => sendResponse({ ok: true, ...result }),
    error => sendResponse({ ok: false, error: error.message || 'Не удалось выполнить действие.' })
  );
  return true;
});
