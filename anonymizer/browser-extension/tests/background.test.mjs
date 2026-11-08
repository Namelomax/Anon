import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';

globalThis.crypto ??= webcrypto;

test('background restricts state to panel and one-token lookups to activated documents', async () => {
  let listener, command;
  const event = () => ({ addListener() {} });
  let reads = 0;
  globalThis.chrome = {
    storage: {
      session: { async get() { reads++; return { anonState: { settings: { apiKey: 'anon_private_key' }, records: [{ mapping: { '[ORG_1]': 'Acme', '[PERSON_1]': 'Alice' } }] } }; }, async set() {} },
      local: { async get() { return {}; }, async set(value) { assert.deepEqual(Object.keys(value), ['anonTokenCounters']); } },
      get sync() { throw new Error('sync storage accessed'); },
    },
    permissions: { async contains() { return true; } },
    runtime: { id: 'extension', getURL: path => `chrome-extension://extension/${path}`, onInstalled: event(), onStartup: event(), onMessage: { addListener(fn) { listener = fn; } } },
    contextMenus: { onClicked: event() }, action: { onClicked: event() }, commands: { onCommand: { addListener(fn) { command = fn; } } },
    scripting: { async executeScript({ args }) {
      if (args[0] === 'capture') return [{ frameId: 0, documentId: 'activated-document', result: { id: 'snapshot', text: '[ORG_1]' } }];
      return [{ frameId: 0, documentId: 'activated-document', result: { ok: true } }];
    } },
    alarms: { onAlarm: event(), async clear() {}, async create() {} },
  };
  try {
    await import('../background.js');
    const message = { type: 'GET_STATE' };
    assert.equal(listener(message, { id: 'other', url: 'chrome-extension://extension/panel.html' }, () => {}), false);
    assert.equal(listener(message, { id: 'extension', url: 'https://example.org/' }, () => {}), false);
    assert.equal(reads, 0);
    const lookup = { type: 'LOOKUP_PLACEHOLDER', token: '[ORG_1]' };
    const content = { id: 'extension', url: 'https://example.org/', tab: { id: 7 }, documentId: 'activated-document' };
    assert.equal(listener(lookup, content, () => {}), false);
    command('inspect-selection', { id: 7 });
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal(listener(lookup, { ...content, documentId: 'different-document' }, () => {}), false);
    assert.equal(listener(lookup, { ...content, id: 'other' }, () => {}), false);
    assert.equal(listener(message, content, () => {}), false);
    const found = await new Promise(resolve => assert.equal(listener(lookup, content, resolve), true));
    assert.deepEqual(found, { ok: true, entries: [{ token: '[ORG_1]', original: 'Acme' }] });
    const invalid = await new Promise(resolve => listener({ ...lookup, token: '*' }, content, resolve));
    assert.equal(invalid.ok, false);
    const response = await new Promise(resolve => assert.equal(listener(message, { id: 'extension', url: 'chrome-extension://extension/panel.html' }, resolve), true));
    assert.equal(response.ok, true);
    assert.ok(reads >= 1);
  } finally { delete globalThis.chrome; }
});

test('shortcuts mask and restore in place without opening the panel', async () => {
  let command;
  let data = { anonState: { settings: { baseUrl: 'https://example.org', apiKey: 'anon_test_key' }, records: [], selectedId: null, capture: null, activeJob: null, directDelivery: null } };
  const calls = [];
  let localData = {};
  let selected = 'Иван';
  const event = () => ({ addListener() {} });
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_url, options) => {
    calls.push(['fetch', options.method]);
    return { ok: true, async json() { return options.method === 'POST'
      ? { jobId: 'job', done: false }
      : { done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Иван' } }; } };
  };
  globalThis.chrome = {
    storage: { session: { async get() { return structuredClone(data); }, async set(value) { data = structuredClone({ ...data, ...value }); } },
      local: { async get() { return structuredClone(localData); }, async set(value) { assert.deepEqual(Object.keys(value), ['anonTokenCounters']); localData = structuredClone({ ...localData, ...value }); } } },
    permissions: { async contains() { return true; } },
    runtime: { id: 'extension', getURL: path => `chrome-extension://extension/${path}`, onInstalled: event(), onStartup: event(), onMessage: event() },
    contextMenus: { onClicked: event() }, action: { onClicked: event() },
    commands: { onCommand: { addListener(fn) { command = fn; } } },
    alarms: { onAlarm: event(), async clear(name) { calls.push(['clear', name]); }, async create(name) { calls.push(['alarm', name]); } },
    sidePanel: { open() { calls.push(['panel']); } },
    scripting: { async executeScript({ target, args }) {
      const [action, payload] = args;
      calls.push(['script', action, target, payload]);
      if (action === 'capture') return [{ frameId: 3, documentId: 'document', result: { id: 'selection', text: selected } }];
      if (action === 'apply') selected = payload.text;
      return [{ result: { ok: true } }];
    } },
  };
  const waitFor = async predicate => {
    for (let i = 0; i < 200; i++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 5));
    }
    throw new Error('Timed out waiting for direct operation');
  };
  try {
    await import('../background.js?shortcuts');
    command('mask-selection', { id: 12, windowId: 2 });
    await waitFor(() => calls.some(call => call[0] === 'script' && call[1] === 'release'));
    await waitFor(() => data.anonState.directDelivery === null);
    assert.equal(selected, '[PERSON_1]');
    assert.deepEqual(Object.keys(localData), ['anonTokenCounters']);
    assert.doesNotMatch(JSON.stringify(localData), /\u0418\u0432\u0430\u043d|anon_test_key|mapping/);
    assert.deepEqual(calls.filter(call => call[0] === 'fetch').map(call => call[1]), ['POST', 'GET']);
    assert.deepEqual(calls.find(call => call[0] === 'script' && call[1] === 'capture')[2], { tabId: 12, frameIds: [0] });
    assert.deepEqual(calls.find(call => call[0] === 'script' && call[1] === 'capture')[3], { usePrevious: false });
    assert.deepEqual(calls.find(call => call[0] === 'script' && call[1] === 'apply')[2], { tabId: 12, documentIds: ['document'] });
    const networkCount = calls.filter(call => call[0] === 'fetch').length;
    command('restore-selection', { id: 12, windowId: 2 });
    await waitFor(() => selected === 'Иван');
    assert.deepEqual(calls.filter(call => call[0] === 'script' && call[1] === 'capture').at(-1)[3], { usePrevious: true });
    assert.equal(calls.filter(call => call[0] === 'fetch').length, networkCount);
    assert.equal(calls.some(call => call[0] === 'panel'), false);
    selected = data.anonState.records[0].maskedText;
    globalThis.chrome.tabs = { async query(query) { assert.deepEqual(query, { active: true, lastFocusedWindow: true }); return [{ id: 12 }]; } };
    command('restore-selection');
    await waitFor(() => selected === 'Иван');
    assert.equal(calls.filter(call => call[0] === 'fetch').length, networkCount);
  } finally {
    globalThis.fetch = originalFetch;
    delete globalThis.chrome;
  }
});
