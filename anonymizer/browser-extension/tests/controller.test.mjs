import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { createController, STATE_KEY } from '../controller.js';

globalThis.crypto ??= webcrypto;
const settings = { baseUrl: 'https://example.org/', apiKey: 'anon_test_key' };

function setup() {
  let data = {};
  let permission = true;
  const calls = [];
  const storage = {
    async get(key) { calls.push(['get', key]); return structuredClone(data); },
    async set(value) { calls.push(['set', Object.keys(value)]); data = structuredClone({ ...data, ...value }); },
  };
  const api = {
    async submitJob(config, text) { calls.push(['submit', config, text]); return { jobId: 'job' }; },
    async pollJob() { return { done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Иван' } }; },
    async cancelJob(config, jobId) { calls.push(['cancel', config, jobId]); },
  };
  const controller = createController({ storage, hasPermission: async origin => { calls.push(['permission', origin]); return permission; }, api });
  return { controller, api, calls, setPermission(value) { permission = value; }, state: () => data[STATE_KEY] };
}
const send = (c, type, extra = {}) => c.dispatch({ type, ...extra });

test('settings require valid key and granted origin permission', async () => {
  const { controller: c, calls, setPermission, state } = setup();
  await assert.rejects(send(c, 'SAVE_SETTINGS', { settings: { ...settings, apiKey: 'bad' } }), /ключ/);
  setPermission(false);
  await assert.rejects(send(c, 'SAVE_SETTINGS', { settings }), /Разрешите/);
  setPermission(true);
  await send(c, 'SAVE_SETTINGS', { settings });
  assert.deepEqual(state().settings, { ...settings, baseUrl: 'https://example.org' });
  assert.ok(calls.some(call => call[0] === 'permission' && call[1] === 'https://example.org/*'));
});

test('mask, preview and restore isolate independent mappings', async () => {
  const { controller: c, api } = setup();
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'Иван пошёл домой' });
  let result = await send(c, 'POLL_JOB');
  const first = result.state.records[0];
  assert.equal(first.title, 'Иван пошёл домой');
  assert.match(first.maskedText, /^\[PERSON_[A-P]{12}_1\]$/);
  assert.deepEqual(await send(c, 'RESTORE', { id: first.id, text: first.maskedText }), { text: 'Иван', unknown: [] });
  api.pollJob = async () => ({ done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Мария' } });
  await send(c, 'START_MASK', { text: 'Мария' });
  result = await send(c, 'POLL_JOB');
  const second = result.state.records[0];
  assert.notEqual(first.maskedText, second.maskedText);
  assert.deepEqual(await send(c, 'RESTORE', { id: first.id, text: second.maskedText }), { text: second.maskedText, unknown: [second.maskedText] });
  assert.deepEqual(await send(c, 'RESTORE', { id: second.id, text: second.maskedText }), { text: 'Мария', unknown: [] });
});

test('concurrent starts submit once and active job blocks settings changes', async () => {
  const { controller: c, calls } = setup();
  await send(c, 'SAVE_SETTINGS', { settings });
  const results = await Promise.allSettled([send(c, 'START_MASK', { text: 'первый' }), send(c, 'START_MASK', { text: 'второй' })]);
  assert.deepEqual(results.map(item => item.status), ['fulfilled', 'rejected']);
  assert.equal(calls.filter(call => call[0] === 'submit').length, 1);
  await assert.rejects(send(c, 'SAVE_SETTINGS', { settings }), /завершите/);
  const visible = (await send(c, 'GET_STATE')).state.activeJob;
  assert.equal(visible.jobId, 'job');
  assert.equal('source' in visible, false);
  assert.equal('settings' in visible, false);
});

test('capture can only be consumed by its current nonce', async () => {
  const { controller: c } = setup();
  await c.capture('первый', 'mask');
  const old = (await send(c, 'GET_STATE')).state.capture.nonce;
  await c.capture('второй', 'restore');
  const current = (await send(c, 'GET_STATE')).state.capture;
  assert.notEqual(old, current.nonce);
  await send(c, 'CONSUME_CAPTURE', { nonce: old });
  assert.deepEqual((await send(c, 'GET_STATE')).state.capture, current);
  await send(c, 'CONSUME_CAPTURE', { nonce: current.nonce });
  assert.equal((await send(c, 'GET_STATE')).state.capture, null);
});

test('terminal errors clear job, transient errors retain it; cancel clears it', async () => {
  const { controller: c, api, calls } = setup();
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'текст' });
  api.pollJob = async () => { throw Object.assign(new Error('сервер'), { status: 500 }); };
  await assert.rejects(send(c, 'POLL_JOB'));
  assert.equal((await send(c, 'GET_STATE')).state.activeJob.jobId, 'job');
  api.pollJob = async () => { throw Object.assign(new Error('не найдено'), { status: 404 }); };
  await assert.rejects(send(c, 'POLL_JOB'));
  assert.equal((await send(c, 'GET_STATE')).state.activeJob, null);
  await send(c, 'START_MASK', { text: 'текст' });
  await send(c, 'CANCEL_JOB');
  assert.equal((await send(c, 'GET_STATE')).state.activeJob, null);
  assert.equal(calls.filter(call => call[0] === 'cancel').length, 1);
  assert.ok(calls.filter(call => call[0] === 'set').every(call => assert.deepEqual(call[1], [STATE_KEY]) === undefined));
});
