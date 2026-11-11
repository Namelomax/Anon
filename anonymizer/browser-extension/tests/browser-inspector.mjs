// Optional real-browser regression: node tests/browser-inspector.mjs <msedge.exe|chrome.exe>
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import assert from 'node:assert/strict';
import { pageInspector } from '../inspector.js';

const binary = process.argv[2];
if (!binary) throw new Error('Укажите путь к браузеру вторым аргументом.');
const tempRoot = resolve(tmpdir());
const profile = await mkdtemp(join(tempRoot, 'anon-inspector-test-'));
const browser = spawn(binary, ['--headless=new', '--no-sandbox', '--disable-gpu', '--no-first-run', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
const pause = ms => new Promise(done => setTimeout(done, ms));
let ws;
try {
  let port;
  for (let end = Date.now() + 20000; Date.now() < end && !port;) {
    try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); } catch { await pause(150); }
  }
  assert.ok(port, 'Браузер не запустился');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((done, reject) => { ws.onopen = done; ws.onerror = reject; });
  let id = 0; const pending = new Map();
  ws.onmessage = event => { const message = JSON.parse(event.data), item = pending.get(message.id); if (!item) return; pending.delete(message.id); clearTimeout(item.timer); message.error ? item.reject(new Error(message.error.message)) : item.done(message.result); };
  const call = (method, params = {}) => new Promise((done, reject) => { const key = ++id, timer = setTimeout(() => reject(new Error(`CDP timeout: ${method}`)), 10000); pending.set(key, { done, reject, timer }); ws.send(JSON.stringify({ id: key, method, params })); });
  const evaluate = async (expression, contextId) => { const result = await call('Runtime.evaluate', { expression, contextId, returnByValue: true, awaitPromise: true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text); return result.result.value; };
  await call('Page.navigate', { url: 'data:text/html,' + encodeURIComponent('<button id="focus">focus</button><p id="source"><b>[PER</b><i>SON_1]</i> and [ORG_2]</p>') });
  while (!(await evaluate('Boolean(document.getElementById("source"))'))) await pause(50);
  const tree = await call('Page.getFrameTree');
  const { executionContextId: context } = await call('Page.createIsolatedWorld', { frameId: tree.frameTree.frame.id, worldName: 'anon-inspector-test' });
  await evaluate(`window.roots={}; const originalAttach=Element.prototype.attachShadow; Element.prototype.attachShadow=function(options){const root=originalAttach.call(this,options); window.roots[this.id]=root;return root}; window.calls=[]; window.respond=null; window.chrome={runtime:{sendMessage(message){window.calls.push(message);return new Promise(resolve=>{window.respond=resolve})}}}`, context);
  const inspector = (action, payload = {}) => evaluate(`(${pageInspector.toString()})(${JSON.stringify(action)},${JSON.stringify(payload)})`, context);
  await evaluate(`document.getElementById('focus').focus();window.before=document.getElementById('source').outerHTML`, context);
  await inspector('show', { entries: [{ token: '[PERSON_1]', original: '<img src=x onerror=alert(1)>' }, { token: '[ORG_2]', original: null }] });
  assert.equal(await evaluate(`roots.__anon_extension_inspector.textContent.includes('<img src=x onerror=alert(1)>') && !roots.__anon_extension_inspector.querySelector('img')`, context), true);
  await evaluate(`roots.__anon_extension_inspector.querySelector('button').click()`, context);
  assert.equal(await evaluate(`document.activeElement.id`, context), 'focus');
  await inspector('show');
  await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true,cancelable:true}))`, context);
  assert.equal(await evaluate(`roots.__anon_extension_inspector.activeElement.tagName`, context), 'BUTTON');
  await evaluate(`document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))`, context);
  assert.equal(await evaluate(`!document.getElementById('__anon_extension_inspector') && document.activeElement.id==='focus'`, context), true);
  await inspector('install');
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 700, y: 500 });
  await pause(350);
  assert.equal(await evaluate('calls.length', context), 0);
  const point = await evaluate(`(()=>{const r=document.createRange();r.setStart(document.querySelector('#source b').firstChild,1);r.setEnd(document.querySelector('#source b').firstChild,3);const box=r.getBoundingClientRect();return {x:box.x+box.width/2,y:box.y+box.height/2}})()`, context);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  await pause(450);
  assert.deepEqual(await evaluate('calls', context), [{ type: 'LOOKUP_PLACEHOLDER', token: '[PERSON_1]' }]);
  await evaluate(`respond({ok:true,entries:[{token:'[PERSON_1]',original:'Иван'}]})`, context);
  await pause(50);
  assert.match(await evaluate(`roots.__anon_extension_tooltip.textContent`, context), /Иван/);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 1, y: 1 });
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  await pause(400);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: 1, y: 1 });
  await evaluate(`respond({ok:true,entries:[{token:'[PERSON_1]',original:'stale'}]})`, context);
  await pause(50);
  assert.equal(await evaluate(`!document.getElementById('__anon_extension_tooltip') && document.getElementById('source').outerHTML===window.before`, context), true);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  await pause(400);
  await evaluate(`respond({ok:false})`, context);
  await pause(50);
  assert.match(await evaluate(`roots.__anon_extension_tooltip.textContent`, context), /AnonAI/);
  await inspector('show');
  const count = await evaluate('calls.length', context);
  await call('Input.dispatchMouseEvent', { type: 'mouseMoved', x: point.x, y: point.y });
  await pause(400);
  assert.equal(await evaluate('calls.length', context), count);
  console.log('Inspector browser checks passed');
} finally {
  ws?.close(); browser.kill();
  if (resolve(profile).startsWith(tempRoot + sep) && resolve(profile) !== tempRoot) await rm(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
}
