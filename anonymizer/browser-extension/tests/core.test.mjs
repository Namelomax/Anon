import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { makePreview, createRecord, restoreText, validateBaseUrl, serverOrigin } from '../core.js';

globalThis.crypto ??= webcrypto;
const ns = 'ABCDEFGHIJKLMNOP';
const scope = ns.slice(0, 12);

test('preview limits words and length', () => {
  assert.equal(makePreview('  один два три четыре пять шесть семь восемь девять '), 'один два три четыре пять шесть семь восемь');
  assert.equal(makePreview('я'.repeat(200)).length, 160);
});

test('renames repeated tokens in one pass without touching other source tokens', () => {
  const record = createRecord('данные [OTHER_9]', { anonymized_text: '[PERSON_1] [PERSON_1] [OTHER_9]', mapping: { '[PERSON_1]': '[OTHER_9]' } }, scope);
  assert.equal(record.maskedText, `[PERSON_${scope}_1] [PERSON_${scope}_1] [OTHER_9]`);
  assert.deepEqual(restoreText(record.maskedText, record.mapping), { text: '[OTHER_9] [OTHER_9] [OTHER_9]', unknown: ['[OTHER_9]'] });
  assert.throws(() => createRecord('[PERSON_1]', { anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'x' } }, scope));
  assert.throws(() => createRecord(`[PERSON_${scope}_1]`, { anonymized_text: '', mapping: {} }, scope));
});

test('rejects malformed mappings and preserves hostile strings literally', () => {
  assert.throws(() => createRecord('x', { anonymized_text: 'x', mapping: { bad: 'x' } }));
  assert.throws(() => restoreText('[PERSON_1]', { '[PERSON_1]': 1 }));
  assert.deepEqual(restoreText('[PERSON_1] [MISSING_2] [MISSING_2]', { '[PERSON_1]': '<img src=x onerror=alert(1)>[MISSING_2]' }), { text: '<img src=x onerror=alert(1)>[MISSING_2] [MISSING_2] [MISSING_2]', unknown: ['[MISSING_2]'] });
});

test('validates server URLs', () => {
  assert.equal(validateBaseUrl('https://example.org/path/'), 'https://example.org/path');
  assert.equal(serverOrigin('http://[::1]:8000/path'), 'http://[::1]:8000');
  for (const value of ['http://example.org', 'https://user:pass@example.org', 'https://example.org/?x=1', 'https://example.org/#x', 'javascript:alert(1)']) assert.throws(() => validateBaseUrl(value));
});
