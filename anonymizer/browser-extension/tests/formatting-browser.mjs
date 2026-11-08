// Optional real-browser regression: node tests/formatting-browser.mjs <msedge.exe|chrome.exe>
import { spawn } from 'node:child_process';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import assert from 'node:assert/strict';
import { pageSelection } from '../selection.js';

const binary = process.argv[2];
if (!binary) throw new Error('Укажите путь к браузеру вторым аргументом.');
const tempRoot = resolve(tmpdir());
const profile = await mkdtemp(join(tempRoot, 'anon-formatting-test-'));
const browser = spawn(binary, ['--headless=new', '--no-sandbox', '--disable-gpu', '--disable-background-networking', '--no-first-run', '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
let launchError;
browser.on('error', error => { launchError = error; });
const pause = ms => new Promise(done => setTimeout(done, ms));
let ws;
try {
  let port;
  for (let end = Date.now() + 20000; Date.now() < end && !port;) {
    if (launchError) throw launchError;
    try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); } catch { await pause(150); }
  }
  assert.ok(port, 'Браузер не запустился за 20 секунд');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(targets.find(target => target.type === 'page').webSocketDebuggerUrl);
  await new Promise((done, reject) => { ws.onopen = done; ws.onerror = reject; });
  let id = 0;
  const pending = new Map();
  ws.onmessage = event => {
    const message = JSON.parse(event.data), item = pending.get(message.id);
    if (!item) return;
    clearTimeout(item.timer); pending.delete(message.id);
    if (message.error) item.reject(new Error(message.error.message)); else item.done(message.result);
  };
  const call = (method, params = {}) => new Promise((done, reject) => {
    const key = ++id, timer = setTimeout(() => { pending.delete(key); reject(new Error(`CDP timeout: ${method}`)); }, 10000);
    pending.set(key, { done, reject, timer });
    ws.send(JSON.stringify({ id: key, method, params }));
  });
  const evaluate = async (expression, contextId) => {
    const result = await call('Runtime.evaluate', { expression, contextId, returnByValue: true, awaitPromise: true, userGesture: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  const html = `<div id="before">Outside before</div><div contenteditable="true" id="rich"><h2 title="heading">Report</h2><p style="text-align:center">Hello <strong class="name">Alice</strong>. <span style="color:red">Keep</span><img alt="diagram"></p><ul><li>Email <em>a@example.test</em></li></ul><table><tbody><tr><td>Cell</td></tr></tbody></table></div><div id="after">Outside after</div><textarea id="plain">Before Alice after</textarea>`;
  await call('Page.navigate', { url: 'data:text/html;charset=utf-8,' + encodeURIComponent(html) });
  for (let end = Date.now() + 10000; !(await evaluate("Boolean(document.getElementById('rich'))"));) {
    if (Date.now() > end) throw new Error('Страница не загрузилась');
    await pause(100);
  }
  await call('Page.bringToFront');
  await call('Emulation.setFocusEmulationEnabled', { enabled: true });
  const tree = await call('Page.getFrameTree');
  const { executionContextId } = await call('Page.createIsolatedWorld', { frameId: tree.frameTree.frame.id, worldName: 'anon-formatting-test' });
  const selection = (action, payload = {}) => evaluate(`(${pageSelection.toString()})(${JSON.stringify(action)},${JSON.stringify(payload)})`, executionContextId);
  const select = () => evaluate(`{const root=document.getElementById('rich');root.focus();const range=document.createRange();range.selectNodeContents(root);window.getSelection().removeAllRanges();window.getSelection().addRange(range)}`);
  const editsFor = (source, replacements) => replacements.map(([from, text]) => {
    const start = source.indexOf(from);
    assert.notEqual(start, -1, `Missing ${from} in captured text`);
    return { start, end: start + from.length, text };
  });
  const edited = (source, edits) => [...edits].sort((a, b) => b.start - a.start).reduce((value, edit) => value.slice(0, edit.start) + edit.text + value.slice(edit.end), source);

  await evaluate(`window.refs=[...document.querySelectorAll('#rich, #rich *')];window.before=document.getElementById('before');window.after=document.getElementById('after')`);
  await select();
  let captured = await selection('capture');
  assert.ok(captured.id, JSON.stringify(captured));
  assert.match(captured.text, /Report\n/);
  assert.ok(!captured.text.includes('ReportHello'));
  let edits = editsFor(captured.text, [['Alice', '[PERSON_1]'], ['a@example.test', '[EMAIL_1]']]);
  let result = edited(captured.text, edits);
  assert.equal((await selection('apply', { id: captured.id, text: result, edits })).ok, true);
  assert.equal(await evaluate(`window.refs.every(node=>node.isConnected) && window.refs.every((node,i)=>node===[...document.querySelectorAll('#rich, #rich *')][i]) && document.getElementById('before')===window.before && document.getElementById('after')===window.after`), true);
  assert.equal(await evaluate(`document.querySelector('#rich p').getAttribute('style')`), 'text-align:center');
  assert.equal(await evaluate(`document.querySelector('#rich strong').textContent`), '[PERSON_1]');
  assert.equal(await evaluate(`document.querySelector('#rich em').textContent`), '[EMAIL_1]');
  await select();
  captured = await selection('capture');
  assert.equal(captured.text, result);

  // A new AI answer contains a placeholder split across formatting nodes.
  await evaluate(`document.getElementById('rich').innerHTML='<h2 title="new">New answer</h2><p style="text-align:right">Dear <strong>[PER</strong><em>SON_1]</em>, unknown [FOREIGN_9].</p><ul><li>Another item</li></ul><table><tbody><tr><td>Value</td></tr></tbody></table>';window.refs=[...document.querySelectorAll('#rich, #rich *')];`);
  await select();
  captured = await selection('capture');
  edits = editsFor(captured.text, [['[PERSON_1]', 'Alice']]);
  result = edited(captured.text, edits);
  assert.equal((await selection('apply', { id: captured.id, text: result, edits })).ok, true);
  assert.equal(await evaluate(`window.refs.every(node=>node.isConnected) && window.refs.every((node,i)=>node===[...document.querySelectorAll('#rich, #rich *')][i])`), true);
  assert.equal(await evaluate(`document.querySelector('#rich p').getAttribute('style')`), 'text-align:right');
  await select();
  captured = await selection('capture');
  assert.equal(captured.text, result);
  assert.match(captured.text, /Alice, unknown \[FOREIGN_9\]/);
  assert.match(captured.text, /New answer/);

  await evaluate(`document.querySelector('#rich h2').textContent='User changed heading'`);
  const stale = await selection('apply', { id: captured.id, text: 'stale', edits: [{ start: 0, end: captured.text.length, text: 'stale' }] });
  assert.ok(stale.error, JSON.stringify(stale));
  assert.equal(await evaluate(`document.querySelector('#rich h2').textContent`), 'User changed heading');
  assert.match(await evaluate(`document.getElementById('rich').textContent`), /Alice/);

  // Older callers omit edits; they must preserve structure or leave it untouched.
  await select();
  captured = await selection('capture');
  const fallbackText = captured.text.replace('Alice', '[PERSON_2]');
  const beforeHtml = await evaluate(`document.getElementById('rich').innerHTML`);
  const fallback = await selection('apply', { id: captured.id, text: fallbackText });
  if (fallback.ok) {
    assert.equal(await evaluate(`window.refs.every(node=>node.isConnected) && window.refs.every((node,i)=>node===[...document.querySelectorAll('#rich, #rich *')][i])`), true);
    await select();
    assert.equal((await selection('capture')).text, fallbackText);
  } else {
    assert.ok(fallback.error);
    assert.equal(await evaluate(`document.getElementById('rich').innerHTML`), beforeHtml);
  }

  // Nested editable prose remains available, while controls form barriers.
  await evaluate(`document.getElementById('rich').innerHTML='<p>Before Alice<button type="button">Secret button label</button><input value="private value"><span contenteditable="true">Bob</span><span contenteditable="false"> prose</span><span role="toolbar">Toolbar label</span> after</p>';window.controlRefs=[...document.querySelectorAll('#rich, #rich *')];`);
  await select();
  captured = await selection('capture');
  assert.ok(captured.id, JSON.stringify(captured));
  assert.match(captured.text, /Alice\n+Bob prose\n after/);
  assert.ok(!captured.text.includes('Secret button label'));
  assert.ok(!captured.text.includes('private value'));
  assert.ok(!captured.text.includes('Toolbar label'));
  const crossing = editsFor(captured.text, [[captured.text.slice(captured.text.indexOf('Alice'), captured.text.indexOf('Bob') + 3), 'Combined']]);
  const controlHtml = await evaluate(`document.getElementById('rich').innerHTML`);
  assert.ok((await selection('apply', { id: captured.id, text: edited(captured.text, crossing), edits: crossing })).error);
  assert.equal(await evaluate(`document.getElementById('rich').innerHTML`), controlHtml);
  edits = editsFor(captured.text, [['Alice', '[PERSON_1]'], ['Bob', '[PERSON_2]']]);
  assert.equal((await selection('apply', { id: captured.id, text: edited(captured.text, edits), edits })).ok, true);
  assert.equal(await evaluate(`document.querySelector('#rich input').value`), 'private value');
  assert.equal(await evaluate(`document.querySelector('#rich button').textContent`), 'Secret button label');
  assert.equal(await evaluate(`window.controlRefs.every(node=>node.isConnected) && window.controlRefs.every((node,i)=>node===[...document.querySelectorAll('#rich, #rich *')][i])`), true);

  await evaluate(`document.getElementById('rich').innerHTML='<div contenteditable="true" role="textbox"><span>Editable Carol</span></div><button><span>Hidden label</span></button><div role="toolbar"><span>Hidden toolbar</span></div>';`);
  await select();
  captured = await selection('capture');
  assert.ok(captured.id, JSON.stringify(captured));
  assert.match(captured.text, /Editable Carol/);
  assert.ok(!captured.text.includes('Hidden'));
  edits = editsFor(captured.text, [['Carol', '[PERSON_3]']]);
  assert.equal((await selection('apply', { id: captured.id, text: edited(captured.text, edits), edits })).ok, true);
  assert.equal(await evaluate(`document.querySelector('[role="textbox"] span').textContent`), 'Editable [PERSON_3]');
  for (const selector of ['#rich button span', '#rich [role="toolbar"] span']) {
    await evaluate(`{const range=document.createRange();range.selectNodeContents(document.querySelector(${JSON.stringify(selector)}));window.getSelection().removeAllRanges();window.getSelection().addRange(range)}`);
    assert.ok((await selection('capture')).error, `Control text captured from ${selector}`);
  }

  await evaluate(`document.getElementById('plain').focus();document.getElementById('plain').setSelectionRange(7,12)`);
  captured = await selection('capture');
  assert.equal(captured.text, 'Alice');
  assert.equal((await selection('apply', { id: captured.id, text: '[PERSON_1]', edits: [{ start: 0, end: 5, text: '[PERSON_1]' }] })).ok, true);
  assert.equal(await evaluate(`document.getElementById('plain').value`), 'Before [PERSON_1] after');
  await selection('install');
  await selection('install');
  await evaluate(`document.getElementById('plain').setSelectionRange(0,0)`);
  captured = await selection('capture', { usePrevious: true });
  assert.equal(captured.text, '[PERSON_1]');
  assert.equal((await selection('apply', { id: captured.id, text: 'Alice' })).ok, true);
  assert.equal(await evaluate(`document.getElementById('plain').value`), 'Before Alice after');
  await evaluate(`document.getElementById('plain').setSelectionRange(0,0)`);
  assert.ok((await selection('capture')).error);
  await evaluate(`document.getElementById('rich').innerHTML='<p>Dear <strong>[PERSON_1]</strong>!</p>';`);
  await select();
  await evaluate(`document.dispatchEvent(new Event('selectionchange'));window.getSelection().removeAllRanges()`);
  captured = await selection('capture', { usePrevious: true });
  assert.match(captured.text, /Dear \[PERSON_1\]/);
  edits = editsFor(captured.text, [['[PERSON_1]', 'Alice']]);
  assert.equal((await selection('apply', { id: captured.id, text: edited(captured.text, edits), edits })).ok, true);
  assert.equal(await evaluate(`document.querySelector('#rich strong').textContent`), 'Alice');
  await select();
  captured = await selection('capture');
  edits = editsFor(captured.text, [['Alice', '[PERSON_1]']]);
  assert.equal((await selection('apply', { id: captured.id, text: edited(captured.text, edits), edits })).ok, true);
  await evaluate(`window.getSelection().removeAllRanges()`);
  captured = await selection('capture', { usePrevious: true });
  edits = editsFor(captured.text, [['[PERSON_1]', 'Alice']]);
  assert.equal((await selection('apply', { id: captured.id, text: edited(captured.text, edits), edits })).ok, true);
  assert.equal(await evaluate(`document.querySelector('#rich strong').textContent`), 'Alice');
  await evaluate(`window.getSelection().removeAllRanges()`);
  captured = await selection('capture', { usePrevious: true });
  assert.match(captured.text, /Alice/);
  await selection('release', { id: captured.id });
  assert.match((await selection('capture', { usePrevious: true })).text, /Alice/);
  await evaluate(`document.querySelector('#rich strong').textContent='Changed';window.getSelection().removeAllRanges()`);
  assert.ok((await selection('capture', { usePrevious: true })).error);
  await evaluate(`document.getElementById('plain').focus();document.getElementById('plain').setSelectionRange(7,12);document.dispatchEvent(new Event('select'));document.getElementById('plain').setSelectionRange(0,6)`);
  assert.equal((await selection('capture', { usePrevious: true })).text, 'Before');
  await evaluate(`document.getElementById('plain').setSelectionRange(6,7)`);
  assert.ok((await selection('capture', { usePrevious: true })).error);
  await evaluate(`document.getElementById('plain').readOnly=true;document.getElementById('plain').setSelectionRange(0,0)`);
  assert.ok((await selection('capture', { usePrevious: true })).error);
  await evaluate(`document.getElementById('plain').readOnly=false;const input=document.createElement('input');input.type='password';input.value='secret';document.body.append(input);input.focus();input.setSelectionRange(0,0)`);
  assert.ok((await selection('capture', { usePrevious: true })).error);
  console.log('PASS: formatted selection edits, previous selection, stale guard and textarea.');
} finally {
  ws?.close();
  browser.kill();
  await pause(500);
  if (resolve(profile).startsWith(tempRoot + sep) && profile.includes('anon-formatting-test-')) await rm(profile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {});
}
