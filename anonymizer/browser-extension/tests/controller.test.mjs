import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { createController, STATE_KEY, COUNTER_KEY } from '../controller.js';

globalThis.crypto ??= webcrypto;
const settings = { baseUrl: 'https://example.org/', apiKey: 'anon_test_key' };

function setup(counterStorage) {
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
  const controller = createController({ storage, counterStorage: counterStorage || storage, hasPermission: async origin => { calls.push(['permission', origin]); return permission; }, api });
  return { controller, api, calls, setPermission(value) { permission = value; }, state: () => data[STATE_KEY], setState(value) { data[STATE_KEY] = value; } };
}
const send = (c, type, extra = {}) => c.dispatch({ type, ...extra });

test('inspector returns only requested tokens, reports conflicts and never changes state or uses API', async () => {
  const { controller: c, setState, calls, state } = setup();
  setState({ settings, records: [
    { mapping: { '[PERSON_1]': 'Alice', '[ORG_1]': 'Acme', '[PERSON_OLD_2]': 'Bob' } },
    { mapping: { '[PERSON_1]': 'Other' } }
  ] });
  const before = structuredClone(state());
  assert.deepEqual(await c.lookupTokens(['[ORG_1]', '[PERSON_1]', '[MISSING_9]', '[ORG_1]', '[PERSON_OLD_2]']), { entries: [
    { token: '[ORG_1]', original: 'Acme' },
    { token: '[PERSON_1]', original: null, conflict: true },
    { token: '[MISSING_9]', original: null },
    { token: '[PERSON_OLD_2]', original: 'Bob' }
  ] });
  assert.deepEqual(state(), before);
  assert.equal(calls.some(call => call[0] !== 'get'), false);
  await assert.rejects(c.lookupTokens(['not a token']));
  await assert.rejects(c.lookupTokens(Array(201).fill('[ORG_1]')));
});

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
  assert.equal(first.maskedText, '[PERSON_1]');
  assert.deepEqual(await send(c, 'RESTORE', { id: first.id, text: first.maskedText }), { text: 'Иван', unknown: [] });
  api.pollJob = async () => ({ done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Мария' } });
  await send(c, 'START_MASK', { text: 'Мария' });
  result = await send(c, 'POLL_JOB');
  const second = result.state.records[0];
  assert.equal(second.maskedText, '[PERSON_2]');
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
  assert.ok(calls.filter(call => call[0] === 'set').every(call => call[1].length === 1 && [STATE_KEY, COUNTER_KEY].includes(call[1][0])));
});

test('automatic restore combines independent mappings and preserves unknown tokens', async () => {
  const { controller: c, api } = setup();
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'Иван' });
  const first = (await send(c, 'POLL_JOB')).state.records[0];
  api.pollJob = async () => ({ done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Мария' } });
  await send(c, 'START_MASK', { text: 'Мария' });
  const second = (await send(c, 'POLL_JOB')).state.records[0];
  assert.deepEqual(await send(c, 'RESTORE_AUTO', { text: `${first.maskedText} и ${second.maskedText} [UNKNOWN_123]` }),
    { text: 'Иван и Мария [UNKNOWN_123]', unknown: ['[UNKNOWN_123]'], restoredCount: 2,
      edits: [{ start: 0, end: 10, text: 'Иван' }, { start: 13, end: 23, text: 'Мария' }] });
  await assert.rejects(send(c, 'RESTORE_AUTO', { text: '[UNKNOWN_123]' }), /Не найдены/);
});

test('direct jobs retain pending target, hide private delivery and guard acknowledgement', async () => {
  const { controller: c, api } = setup();
  const target = { tabId: 42, documentId: 'doc', id: 'selection' };
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'Иван', directTarget: target });
  api.pollJob = async () => ({ done: false });
  const pending = (await send(c, 'POLL_JOB')).state;
  assert.equal('directTarget' in pending.activeJob, false);
  assert.equal('directDelivery' in pending, false);
  assert.deepEqual((await c.directTask()).active.target, target);
  api.pollJob = async () => ({ done: true, anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Иван' } });
  const completed = (await send(c, 'POLL_JOB')).state;
  const delivery = (await c.directTask()).delivery;
  assert.equal('directDelivery' in completed, false);
  assert.deepEqual(delivery.target, target);
  assert.equal(delivery.text, completed.records[0].maskedText);
  await c.acknowledgeDelivery('wrong');
  assert.equal((await c.directTask()).delivery.id, delivery.id);
  await c.acknowledgeDelivery(delivery.id);
  assert.equal((await c.directTask()).delivery, null);
});

test('terminal failure and cancellation create direct error deliveries', async () => {
  const { controller: c, api } = setup();
  const target = { tabId: 1, id: 'selection' };
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'Иван', directTarget: target });
  api.pollJob = async () => { throw Object.assign(new Error('не найдено'), { status: 404 }); };
  await assert.rejects(send(c, 'POLL_JOB'), /не найдено/);
  let delivery = (await c.directTask()).delivery;
  assert.equal(delivery.error, 'не найдено');
  assert.deepEqual(delivery.target, target);
  await c.acknowledgeDelivery(delivery.id);
  await send(c, 'START_MASK', { text: 'Иван', directTarget: target });
  await send(c, 'CANCEL_JOB');
  delivery = (await c.directTask()).delivery;
  assert.match(delivery.error, /отменено/);
  assert.deepEqual(delivery.target, target);
});

test('deleting records and resetting the session preserve counter allocation', async () => {
  let counters = {};
  const counterStorage = {
    async get() { return structuredClone(counters); },
    async set(value) { counters = structuredClone({ ...counters, ...value }); },
  };
  const one = setup(counterStorage);
  await send(one.controller, 'SAVE_SETTINGS', { settings });
  const mask = async c => {
    await send(c, 'START_MASK', { text: 'Ivan' });
    return (await send(c, 'POLL_JOB')).state.records[0];
  };
  const first = await mask(one.controller);
  assert.equal(first.maskedText, '[PERSON_1]');
  await send(one.controller, 'DELETE_RECORD', { id: first.id });
  assert.equal((await mask(one.controller)).maskedText, '[PERSON_2]');
  await send(one.controller, 'CLEAR_RECORDS');
  assert.equal((await mask(one.controller)).maskedText, '[PERSON_3]');
  const two = setup(counterStorage);
  await send(two.controller, 'SAVE_SETTINGS', { settings });
  assert.equal((await mask(two.controller)).maskedText, '[PERSON_4]');
});

test('automatic restore supports compact and old namespaced tokens together', async () => {
  const { controller: c, state, setState } = setup();
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'Ivan' });
  const compact = (await send(c, 'POLL_JOB')).state.records[0];
  const old = '[PERSON_ABCDEFGHIJKL_1]';
  const saved = state();
  saved.records.push({ id: 'old', mapping: { [old]: 'Maria' } });
  setState(saved);
  assert.deepEqual(await send(c, 'RESTORE_AUTO', { text: `${compact.maskedText} and ${old}` }),
    { text: '\u0418\u0432\u0430\u043d and Maria', unknown: [], restoredCount: 2,
      edits: [{ start: 0, end: 10, text: '\u0418\u0432\u0430\u043d' }, { start: 15, end: 15 + old.length, text: 'Maria' }] });
});

test('recovers a higher counter from retained session records', async () => {
  let counters = {};
  const counterStorage = {
    async get() { return structuredClone(counters); },
    async set(value) { counters = structuredClone({ ...counters, ...value }); },
  };
  const { controller: c, state, setState } = setup(counterStorage);
  await send(c, 'SAVE_SETTINGS', { settings });
  await send(c, 'START_MASK', { text: 'Ivan' });
  await send(c, 'POLL_JOB');
  const saved = state();
  saved.records[0].mapping = { '[PERSON_8]': 'Ivan' };
  saved.records[0].maskedText = '[PERSON_8]';
  saved.records[0].tokenCounters = { PERSON: 8 };
  setState(saved);
  counters = {};
  await send(c, 'START_MASK', { text: 'Ivan' });
  assert.equal((await send(c, 'POLL_JOB')).state.records[0].maskedText, '[PERSON_9]');
});
