import test from 'node:test';
import assert from 'node:assert/strict';

test('background accepts only panel messages and uses session storage', async () => {
  let listener;
  const event = () => ({ addListener() {} });
  let reads = 0;
  globalThis.chrome = {
    storage: {
      session: { async get() { reads++; return {}; }, async set() {} },
      get local() { throw new Error('local storage accessed'); },
      get sync() { throw new Error('sync storage accessed'); },
    },
    permissions: { async contains() { return true; } },
    runtime: { id: 'extension', getURL: path => `chrome-extension://extension/${path}`, onInstalled: event(), onMessage: { addListener(fn) { listener = fn; } } },
    contextMenus: { onClicked: event() }, action: { onClicked: event() }, commands: { onCommand: event() },
  };
  try {
    await import('../background.js');
    const message = { type: 'GET_STATE' };
    assert.equal(listener(message, { id: 'other', url: 'chrome-extension://extension/panel.html' }, () => {}), false);
    assert.equal(listener(message, { id: 'extension', url: 'https://example.org/' }, () => {}), false);
    assert.equal(reads, 0);
    const response = await new Promise(resolve => assert.equal(listener(message, { id: 'extension', url: 'chrome-extension://extension/panel.html' }, resolve), true));
    assert.equal(response.ok, true);
    assert.equal(reads, 1);
  } finally { delete globalThis.chrome; }
});
