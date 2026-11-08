import test from 'node:test';
import assert from 'node:assert/strict';

test('offscreen accepts background transport only, never page or panel requests', async () => {
  const oldChrome = globalThis.chrome, oldFetch = globalThis.fetch;
  let listener, requests = 0;
  globalThis.chrome = { runtime: {
    id: 'extension', getURL: path => `chrome-extension://extension/${path}`,
    onMessage: { addListener(fn) { listener = fn; } }
  } };
  globalThis.fetch = async () => { requests++; return Response.json({ error: 'jobId обязателен' }, { status: 400 }); };
  try {
    await import('../offscreen.js');
    const message = { target: 'anon-offscreen', operation: 'checkConnection', settings: { baseUrl: 'https://example.org', apiKey: 'anon_synthetic' } };
    for (const sender of [
      { id: 'other' },
      { id: 'extension', tab: { id: 1 } },
      { id: 'extension', url: 'https://example.org' },
      { id: 'extension', url: 'chrome-extension://extension/panel.html' }
    ]) {
      let reply;
      assert.notEqual(listener(message, sender, value => { reply = value; }), true);
      assert.equal(reply.ok, false);
    }
    assert.equal(requests, 0);
    const result = await new Promise(resolve => {
      assert.equal(listener(message, { id: 'extension', url: 'chrome-extension://extension/background.js' }, resolve), true);
    });
    assert.deepEqual(result, { ok: true, data: { authenticated: true } });
    assert.equal(requests, 1);
  } finally { globalThis.chrome = oldChrome; globalThis.fetch = oldFetch; }
});

test('transport reuses a single offscreen document for concurrent calls', async () => {
  const old = globalThis.chrome;
  let creates = 0, exists = false;
  globalThis.chrome = {
    runtime: {
      getURL: path => `chrome-extension://extension/${path}`,
      async getContexts() { return exists ? [{}] : []; },
      async sendMessage(message) { return { ok: true, data: { operation: message.operation } }; }
    },
    offscreen: { async createDocument(options) { creates++; assert.deepEqual(options.reasons, ['BLOBS']); await Promise.resolve(); exists = true; } }
  };
  try {
    const { remoteApi } = await import('../transport.js');
    const settings = { baseUrl: 'https://example.org', apiKey: 'anon_synthetic' };
    const replies = await Promise.all([remoteApi.checkConnection(settings), remoteApi.pollJob(settings, 'synthetic')]);
    assert.equal(creates, 1);
    assert.deepEqual(replies, [{ operation: 'checkConnection' }, { operation: 'pollJob' }]);
    await remoteApi.checkConnection(settings);
    assert.equal(creates, 1);
  } finally { globalThis.chrome = old; }
});
