// Real Edge/Chrome integration: node tests/commands-browser.mjs <browser executable>
// An isolated profile, loopback server and synthetic data are used throughout.
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import assert from 'node:assert/strict';

const binary = process.argv[2];
if (!binary) throw new Error('Укажите путь к браузеру.');
const root = resolve(import.meta.dirname, '..');
const tempRoot = resolve(tmpdir());
const profile = await mkdtemp(join(tempRoot, 'anon-command-test-'));
const counts = { post: 0, get: 0, denied: 0 };
let delayNextPost = false;
const server = createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/api/v1/anonymize') {
    if (req.headers.authorization !== 'Bearer anon_test_key') { counts.denied++; res.writeHead(401).end(); return; }
    res.setHeader('Content-Type', 'application/json');
    if (req.method === 'POST') {
      counts.post++; req.resume();
      const delay = delayNextPost ? 35_000 : 0;
      delayNextPost = false;
      setTimeout(() => res.writeHead(202).end(JSON.stringify({ jobId: 'synthetic', done: false })), delay);
      return;
    }
    if (req.method === 'GET' && !url.searchParams.has('jobId')) { res.writeHead(400).end(JSON.stringify({ error: 'jobId обязателен' })); return; }
    if (req.method === 'GET' && url.searchParams.get('jobId') === 'synthetic') {
      counts.get++; res.end(JSON.stringify({ done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Тестов Тест' }, warnings: [] })); return;
    }
    res.writeHead(404).end(); return;
  }
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end('<!doctype html><textarea id="edit">Тестов Тест</textarea><div id="rich" contenteditable="true">Тестов Тест</div><iframe id="frame" srcdoc="&lt;textarea id=edit&gt;Тестов Тест&lt;/textarea&gt;"></iframe>');
});
await new Promise(done => server.listen(0, '127.0.0.1', done));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = spawn(binary, [
  ...(process.argv.includes('--headed') ? [] : ['--headless=new']), '--no-sandbox', '--disable-gpu', '--disable-background-networking',
  '--no-first-run', '--no-default-browser-check', '--enable-unsafe-extension-debugging', '--auto-confirm-extension-install',
  '--remote-debugging-port=0', `--user-data-dir=${profile}`,
  `--disable-extensions-except=${root}`, `--load-extension=${root}`, 'about:blank'
], { windowsHide: !process.argv.includes('--headed'), stdio: 'ignore' });
let launchError;
browser.on('error', error => { launchError = error; });
const connections = [];
const pause = ms => new Promise(done => setTimeout(done, ms));
async function eventually(check, label, timeout = 20_000) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    if (launchError) throw launchError;
    try { const result = await check(); if (result) return result; } catch (error) { last = error; }
    await pause(150);
  }
  throw new Error(`${label}: тайм-аут${last ? ` (${last.message})` : ''}`);
}
async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((done, reject) => { ws.onopen = done; ws.onerror = reject; });
  const pending = new Map();
  const listeners = new Map();
  let id = 0;
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.method) for (const listener of listeners.get(message.method) || []) listener(message.params);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error(message.error.message)); else item.done(message.result);
  };
  const client = {
    ws,
    once(method) {
      return new Promise((done, reject) => {
        const timer = setTimeout(() => reject(new Error(`CDP event timeout: ${method}`)), 10_000);
        const handler = params => { clearTimeout(timer); listeners.set(method, (listeners.get(method) || []).filter(fn => fn !== handler)); done(params); };
        listeners.set(method, [...(listeners.get(method) || []), handler]);
      });
    },
    call(method, params = {}) {
      return new Promise((done, reject) => {
        const key = ++id;
        const timer = setTimeout(() => { pending.delete(key); reject(new Error(`CDP timeout: ${method}`)); }, 10_000);
        pending.set(key, { done, reject, timer });
        ws.send(JSON.stringify({ id: key, method, params }));
      });
    },
    async evaluate(expression) {
      const result = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
      return result.result.value;
    }
  };
  connections.push(client);
  return client;
}
try {
  const port = await eventually(async () => Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]), 'Запуск браузера');
  const endpoint = `http://127.0.0.1:${port}`;
  const [version, protocol] = await Promise.all([fetch(`${endpoint}/json/version`).then(r => r.json()), fetch(`${endpoint}/json/protocol`).then(r => r.json())]);
  const extensionDomain = protocol.domains.find(domain => domain.domain === 'Extensions');
  const trigger = extensionDomain?.commands?.find(command => command.name === 'triggerAction');
  console.log('CDP Extensions.triggerAction:', JSON.stringify(trigger?.parameters || null));
  const browserClient = await connect(version.webSocketDebuggerUrl);
  const targets = () => fetch(`${endpoint}/json/list`).then(r => r.json());
  const workerClients = new Map();
  const worker = await eventually(async () => {
    for (const item of (await targets()).filter(target => target.type === 'service_worker')) {
      let client = workerClients.get(item.id);
      if (!client) { client = await connect(item.webSocketDebuggerUrl); workerClients.set(item.id, client); }
      if (await client.evaluate("chrome.runtime.getManifest().name === 'Anon — маскирование текста'")) return client;
    }
  }, 'Запуск расширения');
  const extensionBase = await worker.evaluate("chrome.runtime.getURL('')");
  const extensionId = new URL(extensionBase).host;
  const shortcuts = await worker.evaluate('chrome.commands.getAll()');
  console.log('Зарегистрированные команды:', JSON.stringify(shortcuts));
  assert.ok(shortcuts.some(command => command.name === 'mask-selection' && command.shortcut), 'Горячая клавиша маскирования не зарегистрирована');
  const restoreShortcut = shortcuts.find(command => command.name === 'restore-selection')?.shortcut;
  const inspectCommand = shortcuts.find(command => command.name === 'inspect-selection');
  assert.ok(inspectCommand, 'Inspection command not registered');
  assert.ok(inspectCommand.shortcut, 'Browser did not assign an inspection shortcut');
  const target = (await targets()).find(item => item.type === 'page');
  const page = await connect(target.webSocketDebuggerUrl);
  await page.call('Page.navigate', { url: 'edge://extensions/' });
  await eventually(() => page.evaluate("location.host === 'extensions' && !!chrome.developerPrivate"), 'Загрузка управления расширениями');
  try {
    await page.evaluate(`new Promise((resolve, reject) => chrome.developerPrivate.addHostPermission(${JSON.stringify(extensionId)}, ${JSON.stringify(origin + '/*')}, () => chrome.runtime.lastError ? reject(new Error(chrome.runtime.lastError.message)) : resolve()))`);
  } catch (error) { throw new Error(`Не удалось выдать разрешение через управление расширениями: ${error.message}`); }
  const permission = await worker.evaluate(`chrome.permissions.contains({origins:[${JSON.stringify(origin + '/*')}]})`);
  if (!permission) {
    const info = await page.evaluate(`new Promise(resolve => chrome.developerPrivate.getExtensionsInfo({includeDisabled:true,includeTerminated:true}, items => resolve(items.find(item => item.id === ${JSON.stringify(extensionId)}))))`);
    console.log(`Управление расширениями не выдало необязательное разрешение: ${JSON.stringify({permissions:info?.permissions})}`);
    await page.call('Page.navigate', { url: extensionBase + 'panel.html' });
    await eventually(() => page.evaluate("location.pathname === '/panel.html' && !!document.getElementById('settings')"), 'Загрузка панели');
    let granted;
    try { granted = await page.evaluate(`chrome.permissions.request({origins:[${JSON.stringify(origin + '/*')}]})`); }
    catch (error) { throw new Error(`Не удалось запросить реальное разрешение хоста: ${error.message}`); }
    assert.equal(granted, true, 'Браузер отклонил разрешение хоста');
  }
  assert.equal(await worker.evaluate(`chrome.permissions.contains({origins:[${JSON.stringify(origin + '/*')}]})`), true);
  await worker.evaluate(`chrome.storage.session.set({anonState:{settings:{baseUrl:${JSON.stringify(origin)},apiKey:'anon_test_key'},records:[],selectedId:null,capture:null,activeJob:null,directDelivery:null}})`);
  await page.call('Page.navigate', { url: origin });
  await eventually(() => page.evaluate("!!document.getElementById('edit')"), 'Загрузка тестовой страницы');
  await page.call('Page.bringToFront');
  await page.call('Emulation.setFocusEmulationEnabled', { enabled: true });
  // The action is a real browser gesture and grants activeTab; it may open a panel.
  if (trigger) {
    const browserTargets = await browserClient.call('Target.getTargets');
    const tabTarget = browserTargets.targetInfos.find(item => item.type === 'tab');
    if (tabTarget) {
      const params = {};
      for (const param of trigger.parameters || []) {
        if (param.name === 'id') params.id = extensionId;
        else if (param.name === 'targetId') params.targetId = tabTarget.targetId;
        else if (!param.optional) throw new Error(`Неизвестный обязательный параметр Extensions.triggerAction: ${param.name}`);
      }
      await browserClient.call('Extensions.triggerAction', params);
    } else console.log('CDP не предоставляет tab target для triggerAction; проверка с выданным host permission.');
  }
  await page.call('Page.bringToFront');
  await page.evaluate("document.getElementById('edit').focus();document.getElementById('edit').select()");
  const beforePanelTargets = (await targets()).filter(item => item.url.startsWith(extensionBase + 'panel.html')).map(item => item.id).sort();
  async function shortcut(letter) {
    const code = `Key${letter}`;
    await page.call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Alt', code: 'AltLeft', windowsVirtualKeyCode: 18, modifiers: 1 });
    await page.call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Shift', code: 'ShiftLeft', windowsVirtualKeyCode: 16, modifiers: 9 });
    await page.call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: letter, code, windowsVirtualKeyCode: letter.charCodeAt(0), modifiers: 9 });
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: letter, code, windowsVirtualKeyCode: letter.charCodeAt(0), modifiers: 9 });
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Shift', code: 'ShiftLeft', windowsVirtualKeyCode: 16, modifiers: 1 });
    await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Alt', code: 'AltLeft', windowsVirtualKeyCode: 18 });
  }
  await shortcut('M');
  let nativeShortcut = true;
  try { await eventually(() => page.evaluate("document.getElementById('edit').value.includes('[PERSON_')"), 'Горячая клавиша маскирования', 3000); }
  catch { nativeShortcut = false; console.log('CDP Input не активировал системную команду; тестируем настоящий обработчик через Debugger.'); }
  let panel;
  let lineNumber;
  let tabId;
  async function direct(mode) {
    if (!panel) {
      const newTarget = await browserClient.call('Target.createTarget', { url: extensionBase + 'panel.html', background: true });
      const panelTarget = await eventually(async () => (await targets()).find(item => item.id === newTarget.targetId), 'Создание панели');
      panel = await connect(panelTarget.webSocketDebuggerUrl);
      await eventually(() => panel.evaluate("location.pathname === '/panel.html' && !!document.getElementById('settings')"), 'Загрузка панели');
      tabId = await worker.evaluate(`chrome.tabs.query({}).then(tabs => tabs.find(tab => tab.url === ${JSON.stringify(origin + '/')})?.id)`);
      assert.ok(tabId, 'Тестовая вкладка не найдена');
      await worker.call('Debugger.enable');
      const source = await readFile(join(root, 'background.js'), 'utf8');
      lineNumber = source.split('\n').findIndex(line => line.includes('controller.dispatch(message).then('));
      assert.ok(lineNumber >= 0, 'Точка останова обработчика не найдена');
    }
    await page.call('Page.bringToFront');
    const { breakpointId } = await worker.call('Debugger.setBreakpointByUrl', { url: extensionBase + 'background.js', lineNumber });
    const stopped = worker.once('Debugger.paused');
    await panel.evaluate("void chrome.runtime.sendMessage({type:'GET_STATE'})");
    const paused = await stopped;
    try {
      await worker.call('Debugger.removeBreakpoint', { breakpointId });
      const result = await worker.call('Debugger.evaluateOnCallFrame', { callFrameId: paused.callFrames[0].callFrameId, expression: `void directSelection({id:${tabId}},${JSON.stringify(mode)})` });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    } finally { await worker.call('Debugger.resume'); }
  }
  if (!nativeShortcut) {
    await direct('mask');
    try { await eventually(() => page.evaluate("document.getElementById('edit').value.includes('[PERSON_')"), 'Маскирование прямым вызовом обработчика', 15_000); }
    catch (error) {
      const status = await page.evaluate("document.getElementById('__anon_extension_status')?.shadowRoot?.textContent");
      const state = await worker.evaluate("chrome.storage.session.get('anonState').then(x=>({active:!!x.anonState?.activeJob,records:x.anonState?.records?.length}))");
      throw new Error(`${error.message}; HTTP ${JSON.stringify(counts)}; state ${JSON.stringify(state)}; status ${JSON.stringify(status)}`);
    }
    await panel.evaluate("document.getElementById('check-connection').click()");
    try { await eventually(() => panel.evaluate("document.getElementById('status').textContent === 'Сервер доступен. API-ключ принят.'"), 'Проверка подключения в панели', 15_000); }
    catch (error) { throw new Error(`${error.message}; status=${JSON.stringify(await panel.evaluate("document.getElementById('status').textContent"))}`); }
  }
  assert.ok(counts.post > 0 && counts.get > 0 && counts.denied === 0, `HTTP: ${JSON.stringify(counts)}`);
  assert.ok(restoreShortcut, 'Горячая клавиша восстановления не зарегистрирована браузером');
  // Lose focus and selection after masking: restore must use the updated saved range.
  await page.evaluate("document.getElementById('edit').setSelectionRange(0,0);document.getElementById('edit').blur();window.getSelection().removeAllRanges()");
  if (nativeShortcut) await shortcut(restoreShortcut.endsWith('U') ? 'U' : 'R');
  else await direct('restore');
  await eventually(() => page.evaluate("document.getElementById('edit').value === 'Тестов Тест'"), 'Горячая клавиша восстановления', 10_000);
  // Exercise inspection and hover through the loaded extension and its runtime authorization.
  const inspectionToken = await worker.evaluate("chrome.storage.session.get('anonState').then(x=>x.anonState.records[0].maskedText)");
  await page.evaluate(`{
    document.getElementById('edit').blur();
    const answer=document.createElement('article');answer.id='inspect-answer';
    const known=document.createElement('strong');known.textContent=${JSON.stringify(inspectionToken)};
    answer.append('AI reply: ',known,' and [FOREIGN_999]');document.body.append(answer);
    window.inspectionBefore=answer.outerHTML;
    const range=document.createRange();range.selectNodeContents(answer);window.getSelection().removeAllRanges();window.getSelection().addRange(range);
  }`);
  const httpBeforeInspection = { ...counts };
  await direct('inspect');
  // CDP pierces closed shadow roots without changing the production inspector.
  async function shadowText(hostId) {
    const { root: tree } = await page.call('DOM.getDocument', { depth: -1, pierce: true });
    const find = node => {
      if (node.attributes?.some((value, index) => value === 'id' && node.attributes[index + 1] === hostId)) return node;
      for (const child of [...(node.children || []), ...(node.shadowRoots || [])]) { const found = find(child); if (found) return found; }
    };
    const host = find(tree);
    if (!host?.shadowRoots?.length) return null;
    const collect = node => (node.nodeValue || '') + [...(node.children || []), ...(node.shadowRoots || [])].map(collect).join('');
    return host.shadowRoots.map(collect).join('');
  }
  const modal = await eventually(async () => { const text = await shadowText('__anon_extension_inspector'); return text?.includes('\u0422\u0435\u0441\u0442\u043e\u0432 \u0422\u0435\u0441\u0442') && text; }, 'Inspection modal');
  assert.ok(modal.includes(inspectionToken) && modal.includes('[FOREIGN_999]') && modal.includes('\u0418\u0441\u0445\u043e\u0434\u043d\u043e\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e'));
  assert.equal(await page.evaluate("document.getElementById('inspect-answer').outerHTML === window.inspectionBefore"), true);
  assert.equal(await page.evaluate("document.documentElement.outerHTML.includes('anon_test_key') || document.documentElement.outerHTML.includes('maskedText')"), false);
  await page.call('Input.dispatchKeyEvent', { type: 'rawKeyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await page.call('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
  await eventually(async () => !(await shadowText('__anon_extension_inspector')), 'Escape closes inspection');
  const point = await page.evaluate(`(()=>{const range=document.createRange();range.selectNodeContents(document.querySelector('#inspect-answer strong'));const box=range.getBoundingClientRect();return {x:box.x+box.width/2,y:box.y+box.height/2}})()`);
  await page.call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  const tooltip = await eventually(async () => { const text = await shadowText('__anon_extension_tooltip'); return text?.includes('\u0422\u0435\u0441\u0442\u043e\u0432 \u0422\u0435\u0441\u0442') && text; }, 'Hover tooltip');
  assert.ok(tooltip.includes(inspectionToken));
  assert.equal(await page.evaluate("document.getElementById('inspect-answer').outerHTML === window.inspectionBefore && !document.documentElement.outerHTML.includes('anon_test_key')"), true);
  assert.deepEqual(counts, httpBeforeInspection, 'Inspection triggered an HTTP request');
  await page.call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 1, y: 1 });
  await page.evaluate("document.getElementById('edit').focus();document.getElementById('edit').select()");
  if (!nativeShortcut) {
    // A newly generated AI answer has different prose/markup from the prompt.
    // Restore its tokens through the real controller without another API call.
    const token = await worker.evaluate("chrome.storage.session.get('anonState').then(x=>x.anonState.records[0].maskedText)");
    await page.evaluate(`{
      document.getElementById('edit').setSelectionRange(0,0);document.getElementById('edit').blur();
      const answer=document.createElement('article');answer.id='ai-answer';
      const heading=document.createElement('h2');heading.textContent='Generated reply';
      const paragraph=document.createElement('p');paragraph.style.textAlign='center';paragraph.append('Prepared for ');
      const strong=document.createElement('strong');strong.textContent=${JSON.stringify(token)};paragraph.append(strong,'.');
      const list=document.createElement('ul');const item=document.createElement('li');item.textContent='Hello '+${JSON.stringify(token)}+'! [FOREIGN_999]';list.append(item);
      answer.append(heading,paragraph,list);document.body.append(answer);window.answerRefs=[answer,heading,paragraph,strong,list,item];
      const range=document.createRange();range.selectNodeContents(answer);window.getSelection().removeAllRanges();window.getSelection().addRange(range);
    }`);
    const previousPosts = counts.post, previousGets = counts.get;
    await direct('restore');
    await eventually(() => page.evaluate("document.getElementById('ai-answer').textContent.includes('Prepared for Тестов Тест.') && document.getElementById('ai-answer').textContent.includes('Hello Тестов Тест!')"), 'Восстановление нового ответа ИИ', 10_000);
    assert.equal(await page.evaluate("window.answerRefs.every(node=>node.isConnected) && document.querySelector('#ai-answer p').style.textAlign==='center' && document.getElementById('ai-answer').textContent.includes('[FOREIGN_999]')"), true);
    assert.equal(counts.post, previousPosts); assert.equal(counts.get, previousGets);
    await page.evaluate("document.getElementById('edit').focus();document.getElementById('edit').select()");
  }
  if (nativeShortcut) assert.deepEqual((await targets()).filter(item => item.url.startsWith(extensionBase + 'panel.html')).map(item => item.id).sort(), beforePanelTargets, 'Команда открыла боковую панель');
  delayNextPost = true;
  if (nativeShortcut) await shortcut('M'); else await direct('mask');
  await eventually(() => page.evaluate("document.getElementById('edit').value.includes('[PERSON_')"), 'Маскирование с задержкой сервера 35 секунд', 50_000);
  assert.equal(counts.post, 2, `Повторная отправка: ${JSON.stringify(counts)}`);
  if (!nativeShortcut) {
    await page.evaluate("document.getElementById('edit').setSelectionRange(0,0);const frame=document.getElementById('frame');frame.contentWindow.focus();frame.contentDocument.getElementById('edit').focus();frame.contentDocument.getElementById('edit').select()");
    await direct('mask');
    await eventually(() => page.evaluate("document.getElementById('frame').contentDocument.getElementById('edit').value.includes('[PERSON_')"), 'Маскирование выделения во фрейме', 15_000);
    assert.equal(counts.post, 3, `Отправка выделения во фрейме: ${JSON.stringify(counts)}`);
  }
  console.log(`PASS: ${nativeShortcut ? 'горячие клавиши' : 'прямой вызов production-обработчика через отладчик'}, chrome.scripting и локальный HTTP API.`, JSON.stringify(counts));
} finally {
  for (const client of connections) client.ws.close();
  browser.kill();
  await new Promise(done => server.close(done));
  await pause(500);
  if (resolve(profile).startsWith(tempRoot + sep) && profile.includes('anon-command-test-')) {
    await rm(profile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {});
  }
}
