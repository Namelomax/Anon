import test from 'node:test';
import assert from 'node:assert/strict';
import { submitJob, pollJob, cancelJob, checkConnection } from '../api.js';

const settings = { baseUrl: 'https://example.org', apiKey: 'secret' };
test('uses bearer, multipart and private request settings', async () => {
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async (url, options) => {
      assert.equal(String(url), 'https://example.org/api/v1/anonymize');
      assert.equal(options.headers.Authorization, 'Bearer secret');
      assert.equal(options.credentials, 'omit');
      assert.equal(options.cache, 'no-store');
      assert.equal(options.redirect, 'error');
      assert.equal(options.body.get('file').name, 'selection.txt');
      assert.equal(await options.body.get('file').text(), 'пример');
      return Response.json({ jobId: 'id', done: false });
    };
    assert.equal((await submitJob(settings, 'пример')).jobId, 'id');
  } finally { globalThis.fetch = old; }
});

test('poll validates result and cancel encodes job id', async () => {
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async (url, options) => {
      assert.equal(url.searchParams.get('jobId'), 'a&b');
      return Response.json(options.method === 'DELETE' ? { cancelled: true } : { done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'x' } });
    };
    assert.equal((await pollJob(settings, 'a&b')).done, true);
    assert.equal((await cancelJob(settings, 'a&b')).cancelled, true);
    globalThis.fetch = async () => Response.json({ done: true, anonymized_text: '', mapping: { '[PERSON_1]': 2 } });
    await assert.rejects(pollJob(settings, 'id'), /Некорректный ответ/);
  } finally { globalThis.fetch = old; }
});

test('HTTP errors expose only status, never server content', async () => {
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response('secret private text', { status: 401 });
    await assert.rejects(pollJob(settings, 'id'), error => error.status === 401 && !error.message.includes('secret'));
  } finally { globalThis.fetch = old; }
});

test('connection check accepts only the authenticated missing-job response', async () => {
  const old = globalThis.fetch;
  try {
    globalThis.fetch = async (url, options) => {
      assert.equal(options.method, 'GET');
      assert.equal(url.search, '');
      assert.equal(options.body, undefined);
      return Response.json({ error: 'jobId обязателен' }, { status: 400 });
    };
    assert.deepEqual(await checkConnection(settings), { authenticated: true });
    globalThis.fetch = async () => Response.json({ error: 'private secret' }, { status: 400 });
    await assert.rejects(checkConnection(settings), e => e.code === 'INVALID_RESPONSE' && !e.message.includes('secret'));
    globalThis.fetch = async () => new Response('<html>secret</html>', { status: 400 });
    await assert.rejects(checkConnection(settings), e => e.code === 'INVALID_RESPONSE' && !e.message.includes('secret'));
    globalThis.fetch = async () => Response.json({ error: 'secret' }, { status: 401 });
    await assert.rejects(checkConnection(settings), e => e.code === 'HTTP' && e.status === 401 && !e.message.includes('secret'));
  } finally { globalThis.fetch = old; }
});

test('submission timeout is bounded and does not retry', async () => {
  const oldFetch = globalThis.fetch, oldSetTimeout = globalThis.setTimeout, oldClearTimeout = globalThis.clearTimeout;
  let calls = 0;
  try {
    globalThis.setTimeout = (callback, delay) => { assert.equal(delay, 120000); queueMicrotask(callback); return 1; };
    globalThis.clearTimeout = () => {};
    globalThis.fetch = async (_url, options) => {
      calls++;
      await new Promise(resolve => options.signal.addEventListener('abort', resolve, { once: true }));
      throw new Error('secret');
    };
    await assert.rejects(submitJob(settings, 'text'), e => e.code === 'TIMEOUT' && /могло быть создано/.test(e.message) && !e.message.includes('secret'));
    assert.equal(calls, 1);
  } finally { globalThis.fetch = oldFetch; globalThis.setTimeout = oldSetTimeout; globalThis.clearTimeout = oldClearTimeout; }
});
