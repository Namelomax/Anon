// Live integration: pipe one JSON line {"apiKey":"anon_..."} into node tests/live-browser.mjs <browser> [--headed].
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import { createInterface } from 'node:readline';
import assert from 'node:assert/strict';

const binary = process.argv[2];
if (!binary || binary.startsWith('--')) throw new Error('Укажите путь к браузеру.');
if (process.stdin.isTTY) process.stdin.setRawMode(true);
console.log('Ready for private test input (stdin only, no echo).');
const input = createInterface({ input: process.stdin, terminal: false });
let apiKey;
try {
  for await (const line of input) { apiKey = JSON.parse(line).apiKey; break; }
} catch { throw new Error('Ожидалась строка JSON с API-ключом.'); }
finally { input.close(); process.stdin.pause(); }
if (typeof apiKey !== 'string' || !/^anon_[A-Za-z0-9_-]+$/.test(apiKey) || apiKey.length > 512) throw new Error('Неверный формат API-ключа.');
const root = resolve(import.meta.dirname, '..');
const tempRoot = resolve(tmpdir());
const profile = await mkdtemp(join(tempRoot, 'anon-live-test-'));
const original = 'Служебная заметка. Напишите на test@example.org.';
const server = createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.end(`<!doctype html><textarea id="edit">${original}</textarea>`);
});
const clients = [];
let browser, stage = 'запуск', launchError;
const start = Date.now();
const pause = ms => new Promise(done => setTimeout(done, ms));
function report(phase, detail = {}) { console.log(JSON.stringify({ phase, ms: Date.now() - start, ...detail })); }
async function eventually(check, timeout = 20000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (launchError) throw new Error('Браузер не запущен');
    try { const result = await check(); if (result) return result; } catch { /* transient target or navigation */ }
    await pause(200);
  }
  throw new Error('Истекло время ожидания');
}
async function connect(url) {
  const ws = new WebSocket(url);
  await new Promise((done, reject) => { ws.onopen = done; ws.onerror = reject; });
  const pending = new Map(), listeners = new Map();
  let id = 0;
  ws.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.method) for (const listener of listeners.get(message.method) || []) listener(message.params);
    const item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error('Ошибка CDP')); else item.done(message.result);
  };
  const client = {
    ws,
    once(method) { return new Promise((done, reject) => {
      const timer = setTimeout(() => reject(new Error('Тайм-аут события CDP')), 10000);
      const handler = params => { clearTimeout(timer); listeners.set(method, (listeners.get(method) || []).filter(fn => fn !== handler)); done(params); };
      listeners.set(method, [...(listeners.get(method) || []), handler]);
    }); },
    call(method, params = {}, timeout = 15000) { return new Promise((done, reject) => {
      const key = ++id;
      const timer = setTimeout(() => { pending.delete(key); reject(new Error('Тайм-аут CDP')); }, timeout);
      pending.set(key, { done, reject, timer }); ws.send(JSON.stringify({ id: key, method, params }));
    }); },
    async evaluate(expression, timeout) {
      const result = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true }, timeout);
      if (result.exceptionDetails) throw new Error('Ошибка выполнения в браузере');
      return result.result.value;
    }
  };
  clients.push(client); return client;
}
try {
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const origin = `http://127.0.0.1:${server.address().port}`;
  browser = spawn(binary, [
    ...(process.argv.includes('--headed') ? [] : ['--headless=new']), '--no-sandbox', '--disable-gpu',
    '--no-first-run', '--no-default-browser-check', '--enable-unsafe-extension-debugging', '--auto-confirm-extension-install',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, `--disable-extensions-except=${root}`, `--load-extension=${root}`, 'about:blank'
  ], { windowsHide: !process.argv.includes('--headed'), stdio: 'ignore' });
  browser.on('error', error => { launchError = error; });
  const port = await eventually(async () => Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]));
  const endpoint = `http://127.0.0.1:${port}`;
  const version = await fetch(`${endpoint}/json/version`).then(r => r.json());
  const browserClient = await connect(version.webSocketDebuggerUrl);
  const targets = () => fetch(`${endpoint}/json/list`).then(r => r.json());
  const workers = new Map();
  const worker = await eventually(async () => {
    for (const target of (await targets()).filter(t => t.type === 'service_worker')) {
      let client = workers.get(target.id);
      if (!client) { client = await connect(target.webSocketDebuggerUrl); workers.set(target.id, client); }
      if (await client.evaluate("chrome.runtime.getManifest().name === 'Anon — маскирование текста'")) return client;
    }
  });
  const extensionBase = await worker.evaluate("chrome.runtime.getURL('')");
  const newTarget = await browserClient.call('Target.createTarget', { url: extensionBase + 'panel.html' });
  const panel = await connect((await eventually(async () => (await targets()).find(t => t.id === newTarget.targetId))).webSocketDebuggerUrl);
  await eventually(() => panel.evaluate("location.pathname === '/panel.html' && !!document.getElementById('settings')"));
  stage = 'разрешения';
  const origins = [origin + '/*', 'https://anonai.ru/*'];
  // Preauthorize the disposable test profile through the browser's actual
  // extension settings, then exercise the normal optional-permission API.
  await panel.call('Page.navigate', { url: 'chrome://extensions' });
  await eventually(() => panel.evaluate("Boolean(chrome.developerPrivate)"));
  for (const host of origins) {
    await panel.evaluate(`new Promise((resolve,reject)=>chrome.developerPrivate.addHostPermission(${JSON.stringify(new URL(extensionBase).host)},${JSON.stringify(host)},()=>chrome.runtime.lastError?reject(new Error('Host permission rejected')):resolve()))`);
  }
  await panel.call('Page.navigate', { url: extensionBase + 'panel.html' });
  await eventually(() => panel.evaluate("!!document.getElementById('settings')"));
  for (const host of origins) {
    assert.equal(await panel.evaluate(`chrome.permissions.request({origins:[${JSON.stringify(host)}]})`, 45000), true);
  }
  assert.equal(await worker.evaluate(`chrome.permissions.contains({origins:${JSON.stringify(origins)}})`), true);
  await worker.evaluate(`chrome.storage.session.set({anonState:{settings:{baseUrl:'https://anonai.ru',apiKey:${JSON.stringify(apiKey)}},records:[],selectedId:null,capture:null,activeJob:null,directDelivery:null}})`);
  apiKey = undefined;
  report('разрешения получены');
  stage = 'проверка подключения';
  const check = await panel.evaluate(`chrome.runtime.sendMessage({type:'CHECK_CONNECTION'}).then(r=>({ok:!!r?.authenticated || !!r?.result?.authenticated, status:Number(r?.status || r?.error?.status)||null})).catch(()=>({ok:false,status:null}))`, 130000);
  report('подключение', check);
  assert.equal(check.ok, true);
  stage = 'страница';
  const target = (await targets()).find(t => t.type === 'page' && t.id !== newTarget.targetId);
  const page = await connect(target.webSocketDebuggerUrl);
  await page.call('Page.navigate', { url: origin });
  await eventually(() => page.evaluate("!!document.getElementById('edit')"));
  const tabId = await worker.evaluate(`chrome.tabs.query({}).then(tabs=>tabs.find(tab=>tab.url===${JSON.stringify(origin + '/')})?.id)`);
  assert.ok(tabId);
  await worker.call('Debugger.enable');
  const source = await readFile(join(root, 'background.js'), 'utf8');
  const lineNumber = source.split('\n').findIndex(line => line.includes('controller.dispatch(message).then('));
  assert.ok(lineNumber >= 0);
  async function direct(mode) {
    await page.call('Page.bringToFront');
    await page.call('Emulation.setFocusEmulationEnabled', { enabled: true });
    await page.evaluate("document.getElementById('edit').focus();document.getElementById('edit').select()");
    const { breakpointId } = await worker.call('Debugger.setBreakpointByUrl', { url: extensionBase + 'background.js', lineNumber });
    const stopped = worker.once('Debugger.paused');
    await panel.evaluate("void chrome.runtime.sendMessage({type:'GET_STATE'})");
    const paused = await stopped;
    try {
      await worker.call('Debugger.removeBreakpoint', { breakpointId });
      const result = await worker.call('Debugger.evaluateOnCallFrame', { callFrameId: paused.callFrames[0].callFrameId, expression: `void directSelection({id:${tabId}},${JSON.stringify(mode)})` });
      if (result.exceptionDetails) throw new Error('Ошибка обработчика');
    } finally { await worker.call('Debugger.resume'); }
  }
  stage = 'маскирование';
  await direct('mask');
  let lastSnapshot = 0;
  await eventually(async () => {
    const masked = await page.evaluate("document.getElementById('edit').value.includes('[EMAIL_')");
    if (Date.now() - lastSnapshot >= 15000) {
      const state = await worker.evaluate("chrome.storage.session.get('anonState').then(x=>({active:!!x.anonState?.activeJob,records:x.anonState?.records?.length || 0,noticeError:!!x.anonState?.notice?.error}))");
      report('ожидание', state); lastSnapshot = Date.now();
    }
    return masked;
  }, 180000);
  const count = await worker.evaluate("chrome.storage.session.get('anonState').then(x=>x.anonState?.records?.length || 0)");
  assert.equal(count, 1);
  report('маскирование завершено', { records: count });
  stage = 'восстановление';
  await direct('restore');
  await eventually(() => page.evaluate(`document.getElementById('edit').value === ${JSON.stringify(original)}`), 15000);
  report('успех', { roundtrip: true });
} catch (error) {
  console.error(JSON.stringify({ phase: stage, result: 'ошибка', reason: String(error.message).replace(/anon_[A-Za-z0-9_-]+/g, '[KEY]'), ms: Date.now() - start }));
  process.exitCode = 1;
} finally {
  for (const client of clients) client.ws.close();
  if (browser) browser.kill();
  if (server.listening) await new Promise(done => server.close(done));
  await pause(500);
  if (resolve(profile).startsWith(tempRoot + sep) && profile.includes('anon-live-test-')) {
    await rm(profile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {});
  }
}
