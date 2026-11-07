import test from 'node:test';
import assert from 'node:assert/strict';
import { submitJob, pollJob, cancelJob } from '../api.js';

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
