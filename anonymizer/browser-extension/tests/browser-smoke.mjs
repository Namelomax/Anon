// Optional real-browser smoke check: node tests/browser-smoke.mjs <msedge.exe|chrome.exe>
// Uses an isolated temporary profile and a synthetic API; no real account or server.
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, join, sep } from 'node:path';
import assert from 'node:assert/strict';

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
    async evaluate(expression) {
      const value = await this.call('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture: true });
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
  // Replace the network only in this isolated test worker. Exercise the real controller/panel.
  await background.evaluate(`
    chrome.permissions.contains = async () => true;
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
  assert.match(masked, /\[PERSON_[A-P]{12}_1\]/);
  assert.equal(await page.evaluate(`document.querySelector('.record strong').textContent`), 'Документ для Тестов Тест');
  await page.evaluate(`document.getElementById('input').value=${JSON.stringify(masked)};document.getElementById('restore').click()`);
  await eventually(() => page.evaluate(`document.getElementById('result').value==='Документ для Тестов Тест'`));
  await page.call('Page.reload');
  await eventually(() => page.evaluate(`document.getElementById('result')?.value.includes('[PERSON_')`));
  assert.equal(await page.evaluate(`document.getElementById('record-count').textContent`), '1');
  await background.evaluate("globalThis.fetch=async()=>{throw new Error('offline')}");
  await page.evaluate(`document.getElementById('input').value='[PERSON_UNKNOWN_99]';document.getElementById('restore').click()`);
  await eventually(() => page.evaluate(`document.getElementById('status').textContent==='Часть плейсхолдеров не восстановлена'`));
  assert.equal(await page.evaluate(`document.getElementById('result').value`), '[PERSON_UNKNOWN_99]');
  await page.evaluate(`document.getElementById('input').value=${JSON.stringify(masked)};document.getElementById('restore').click()`);
  await eventually(() => page.evaluate(`document.getElementById('result').value==='Документ для Тестов Тест'`));
  await page.evaluate("document.getElementById('clear-text').click()");
  assert.equal(await page.evaluate("document.getElementById('input').value + document.getElementById('result').value"), '');
  assert.equal(await page.evaluate("document.getElementById('record-count').textContent"), '1');
  console.log('PASS: installed extension, settings, masking, history preview, offline restore, unknown tokens, panel reload, clear fields. Synthetic API only.');
} finally {
  for (const client of connections) client.ws.close();
  browser.kill();
  await pause(500);
  // Generated profile only; never touch an existing user profile.
  if (resolve(profile).startsWith(tempRoot + sep) && profile.includes('anon-browser-test-')) {
    await rm(profile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {});
  }
}
