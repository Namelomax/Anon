// Optional real-browser smoke check: node tests/browser-smoke.mjs <msedge.exe|chrome.exe>
// Uses an isolated temporary profile and a synthetic API; no real account or server.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join, sep } from 'node:path';
import assert from 'node:assert/strict';
import { pageSelection } from '../selection.js';

const binary = process.argv[2];
if (!binary) throw new Error('Укажите путь к браузеру вторым аргументом.');
const root = resolve(import.meta.dirname, '..');
const tempRoot = resolve(tmpdir());
const profile = await mkdtemp(join(tempRoot, 'anon-browser-test-'));
const browser = spawn(binary, [
  '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-background-networking',
  '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, `--disable-extensions-except=${root}`, `--load-extension=${root}`,
  'about:blank'
], { windowsHide: true, stdio: 'ignore' });
let launchError;
browser.on('error', error => { launchError = error; });
const connections = [];
const pause = ms => new Promise(done => setTimeout(done, ms));

async function eventually(check) {
  const end = Date.now() + 20_000;
  while (Date.now() < end) {
    if (launchError) throw launchError;
    try { const value = await check(); if (value) return value; } catch { /* startup */ }
    await pause(150);
  }
  throw new Error('Браузер или расширение не запустилось за 20 секунд.');
}
async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((done, reject) => { ws.onopen = done; ws.onerror = reject; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    const item = pending.get(message.id);
    if (item) {
      clearTimeout(item.timer); pending.delete(message.id);
      if (message.error) item.reject(new Error(message.error.message)); else item.done(message.result);
    }
  };
  const client = {
    ws,
    call(method, params = {}) {
      return new Promise((done, reject) => {
        const key = ++id;
        const timer = setTimeout(() => { pending.delete(key); reject(new Error(`CDP timeout: ${method}`)); }, 10_000);
        pending.set(key, { done, reject, timer });
        ws.send(JSON.stringify({ id: key, method, params }));
      });
    },
    async evaluate(expression, contextId) {
      const value = await this.call('Runtime.evaluate', { expression, contextId, awaitPromise: true, returnByValue: true, userGesture: true });
      if (value.exceptionDetails) throw new Error(value.exceptionDetails.exception?.description || value.exceptionDetails.text);
      return value.result.value;
    }
  };
  connections.push(client);
  return client;
}

try {
  const port = await eventually(async () => Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]));
  const targets = async () => (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const workerClients = new Map();
  const background = await eventually(async () => {
    for (const worker of (await targets()).filter(item => item.type === 'service_worker')) {
      let client = workerClients.get(worker.id);
      if (!client) {
        client = await connect(worker.webSocketDebuggerUrl);
        await client.call('Runtime.enable');
        workerClients.set(worker.id, client);
      }
      try {
        if (await client.evaluate("typeof chrome !== 'undefined' && chrome.runtime?.getManifest().name === 'Anon — маскирование текста'")) return client;
      } catch { /* another worker is still starting */ }
    }
  });
  const extensionBase = await background.evaluate("chrome.runtime.getURL('')");
  assert.equal(await background.evaluate('chrome.runtime.getManifest().name'), 'Anon — маскирование текста');
  // UI smoke uses a synthetic API inside the real offscreen transport.
  // commands-browser.mjs separately checks real permissions, scripting and HTTP.
  await background.evaluate(`chrome.permissions.contains = async () => true;
    chrome.offscreen.createDocument({url:'offscreen.html',reasons:['BLOBS'],justification:'Synthetic browser test'});`);
  const offscreenTarget = await eventually(async () => (await targets()).find(item => item.url === extensionBase + 'offscreen.html'));
  const network = await connect(offscreenTarget.webSocketDebuggerUrl);
  await network.evaluate(`
    globalThis.fetch = async (url, options) => {
      if (options.method === 'POST') return Response.json({jobId:'synthetic', done:false});
      return Response.json({done:true, anonymized_text:'Документ для [PERSON_1]', mapping:{'[PERSON_1]':'Тестов Тест'}, warnings:[]});
    };
  `);
  const page = await connect((await targets()).find(item => item.type === 'page').webSocketDebuggerUrl);
  await page.call('Page.addScriptToEvaluateOnNewDocument', { source: `window.smokeErrors=[];window.addEventListener('error',e=>window.smokeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>window.smokeErrors.push(String(e.reason)));` });
  await page.call('Page.navigate', { url: extensionBase + 'panel.html' });
  try {
    await eventually(() => page.evaluate(`Boolean(document.getElementById('settings') && document.getElementById('settings').closest('details').open)`));
  } catch (error) {
    console.log(await page.evaluate(`({url:location.href,errors:window.smokeErrors,status:document.getElementById('status')?.textContent,body:document.body?.innerText.slice(0,800)})`));
    throw error;
  }
  await page.evaluate(`
    document.getElementById('base-url').value='https://example.org';
    document.getElementById('api-key').value='anon_synthetic_key';
    chrome.permissions.request=async()=>true;
    document.getElementById('settings').requestSubmit();
  `);
  await eventually(() => page.evaluate(`document.getElementById('status').textContent==='Настройки сохранены'`));
  await page.evaluate(`document.getElementById('input').value='Документ для Тестов Тест';document.getElementById('mask').click()`);
  await eventually(() => page.evaluate(`document.getElementById('record-count').textContent==='1' && !document.getElementById('restore').disabled`));
  const masked = await page.evaluate(`document.getElementById('result').value`);
  assert.match(masked, /\[PERSON_\d+\]/);
  assert.equal(await page.evaluate(`document.querySelector('.record strong').textContent`), 'Документ для Тестов Тест');
  await page.evaluate(`document.getElementById('input').value=${JSON.stringify(masked)};document.getElementById('restore').click()`);
  await eventually(() => page.evaluate(`document.getElementById('result').value==='Документ для Тестов Тест'`));
  await page.call('Page.reload');
  await eventually(() => page.evaluate(`document.getElementById('result')?.value.includes('[PERSON_')`));
  assert.equal(await page.evaluate(`document.getElementById('record-count').textContent`), '1');
  await network.evaluate("globalThis.fetch=async()=>{throw new Error('offline')}");
  await page.evaluate(`document.getElementById('input').value='[PERSON_UNKNOWN_99]';document.getElementById('restore').click()`);
  await eventually(() => page.evaluate(`document.getElementById('status').textContent==='Часть плейсхолдеров не восстановлена'`));
  assert.equal(await page.evaluate(`document.getElementById('result').value`), '[PERSON_UNKNOWN_99]');
  await page.evaluate(`document.getElementById('input').value=${JSON.stringify(masked)};document.getElementById('restore').click()`);
  await eventually(() => page.evaluate(`document.getElementById('result').value==='Документ для Тестов Тест'`));
  await page.evaluate("document.getElementById('clear-text').click()");
  assert.equal(await page.evaluate("document.getElementById('input').value + document.getElementById('result').value"), '');
  assert.equal(await page.evaluate("document.getElementById('record-count').textContent"), '1');
  // Exercise the same serialized selection function in an actual isolated world.
  // Command wiring is checked separately in background.test.mjs.
  await page.call('Page.navigate', { url: 'data:text/html;charset=utf-8,' + encodeURIComponent('<textarea id="edit">До Тестов Тест после</textarea><div id="rich" contenteditable="true">До <b>Тестов Тест</b> после</div><p id="plain">До <b>Тестов Тест</b> после</p><input id="password" type="password" value="synthetic"><input id="readonly" readonly value="synthetic">') });
  await eventually(() => page.evaluate("Boolean(document.getElementById('edit'))"));
  await page.call('Page.bringToFront');
  await page.call('Emulation.setFocusEmulationEnabled', { enabled: true });
  const tree = await page.call('Page.getFrameTree');
  const { executionContextId } = await page.call('Page.createIsolatedWorld', { frameId: tree.frameTree.frame.id, worldName: 'anon-selection-test' });
  const selection = (action, payload = {}) => page.evaluate(`(${pageSelection.toString()})(${JSON.stringify(action)},${JSON.stringify(payload)})`, executionContextId);
  await selection('notify', { phase: 'sending', message: 'Отправляем тестовый запрос…' });
  await pause(4300);
  assert.equal(await page.evaluate("document.getElementById('__anon_extension_status')?.dataset.anonPhase"), 'sending', 'Busy indicator must outlive the old four-second toast');
  await page.call('DOM.enable');
  await page.call('CSS.enable');
  const documentTree = await page.call('DOM.getDocument', { depth: -1, pierce: true });
  function findSpinner(node) {
    const attributes = node.attributes || [];
    const classIndex = attributes.indexOf('class');
    if (classIndex >= 0 && attributes[classIndex + 1] === 'icon spinner') return node.nodeId;
    for (const child of [...(node.children || []), ...(node.shadowRoots || [])]) {
      const found = findSpinner(child); if (found) return found;
    }
    return null;
  }
  const spinnerNode = findSpinner(documentTree.root);
  assert.ok(spinnerNode, 'Busy notification has an actual spinner');
  const animationName = async () => (await page.call('CSS.getComputedStyleForNode', { nodeId: spinnerNode })).computedStyle.find(item => item.name === 'animation-name')?.value;
  assert.equal(await animationName(), 'spin');
  await page.call('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  assert.equal(await animationName(), 'none');
  await page.call('Emulation.setEmulatedMedia', { features: [] });
  await selection('notify', { phase: 'processing', message: 'Запрос принят. Обработка…' });
  assert.equal(await page.evaluate("document.getElementById('__anon_extension_status')?.dataset.anonPhase"), 'processing');
  await selection('notify', { phase: 'error', error: true, message: 'Синтетическая ошибка' });
  assert.equal(await page.evaluate("document.getElementById('__anon_extension_status')?.dataset.anonPhase"), 'error');
  await selection('notify', { phase: 'success', message: 'Готово' });
  assert.equal(await page.evaluate("document.getElementById('__anon_extension_status')?.dataset.anonPhase"), 'success');
  await page.evaluate("document.getElementById('edit').focus();document.getElementById('edit').setSelectionRange(3,14)");
  let captured = await selection('capture');
  assert.equal(captured.text, 'Тестов Тест', JSON.stringify(captured));
  assert.equal((await selection('apply', { id: captured.id, text: '[PERSON_TEST_1]' })).ok, true);
  assert.equal(await page.evaluate("document.getElementById('edit').value"), 'До [PERSON_TEST_1] после');
  captured = await selection('capture');
  assert.equal(captured.text, '[PERSON_TEST_1]');
  assert.equal((await selection('apply', { id: captured.id, text: 'Тестов Тест' })).ok, true);
  captured = await selection('capture');
  await page.evaluate("document.getElementById('edit').value='Новая редакция'");
  assert.match((await selection('apply', { id: captured.id, text: 'stale' })).error, /изменился/);
  assert.equal(await page.evaluate("document.getElementById('edit').value"), 'Новая редакция');
  for (const id of ['rich', 'plain']) {
    await page.evaluate(`{document.getElementById('edit').blur();document.getElementById(${JSON.stringify(id)}).focus();const range=document.createRange();range.selectNodeContents(document.querySelector('#${id} b'));window.getSelection().removeAllRanges();window.getSelection().addRange(range);}`);
    captured = await selection('capture');
    assert.equal(captured.text, 'Тестов Тест');
    assert.equal((await selection('notify', { message: 'Обработка…' })).ok, true);
    assert.equal((await selection('apply', { id: captured.id, text: '[PERSON_TEST_1]' })).ok, true);
    assert.equal(await page.evaluate(`document.getElementById(${JSON.stringify(id)}).textContent`), 'До [PERSON_TEST_1] после');
    captured = await selection('capture');
    assert.equal(captured.text, '[PERSON_TEST_1]');
    assert.equal((await selection('apply', { id: captured.id, text: '<img src=x onerror=alert(1)>' })).ok, true);
    assert.equal(await page.evaluate(`document.querySelectorAll('#${id} img').length`), 0);
  }
  await page.evaluate("document.getElementById('password').focus();document.getElementById('password').select()");
  assert.ok((await selection('capture')).error);
  await page.evaluate("document.getElementById('readonly').focus();document.getElementById('readonly').select()");
  assert.ok((await selection('capture')).error);
  console.log('PASS: extension UI + offline restore; isolated-world selection replacement in textarea/contenteditable/static text, repeat selection, stale edit guard, literal HTML, password/readonly guard. Synthetic API only.');
} finally {
  for (const client of connections) client.ws.close();
  browser.kill();
  await pause(500);
  // Generated profile only; never touch an existing user profile.
  if (resolve(profile).startsWith(tempRoot + sep) && profile.includes('anon-browser-test-')) {
    await rm(profile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {});
  }
}
