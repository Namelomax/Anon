import test from 'node:test';
import assert from 'node:assert/strict';
import { webcrypto } from 'node:crypto';
import { makePreview, createRecord, restoreText, maskedEdits, restorationEdits, validateBaseUrl, serverOrigin } from '../core.js';

globalThis.crypto ??= webcrypto;

test('preview limits words and length', () => {
  assert.equal(makePreview('  один два три четыре пять шесть семь восемь девять '), 'один два три четыре пять шесть семь восемь');
  assert.equal(makePreview('я'.repeat(200)).length, 160);
});

test('allocates compact tokens without changing literal placeholders', () => {
  const first = createRecord('data [PERSON_1] [OTHER_9]', { anonymized_text: '[PERSON_2] [PERSON_2] [PERSON_1] [OTHER_9]', mapping: { '[PERSON_2]': 'data' } });
  assert.equal(first.maskedText, '[PERSON_2] [PERSON_2] [PERSON_1] [OTHER_9]');
  assert.deepEqual(restoreText(first.maskedText, first.mapping), { text: 'data data [PERSON_1] [OTHER_9]', unknown: ['[PERSON_1]', '[OTHER_9]'] });
  assert.throws(() => createRecord('[PERSON_1]', { anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'x' } }));
});

test('continues per-label counters across records', () => {
  const first = createRecord('Ivan', { anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Ivan' } }, {});
  const second = createRecord('Maria', { anonymized_text: '[PERSON_1]', mapping: { '[PERSON_1]': 'Maria' } }, first.tokenCounters);
  assert.equal(first.maskedText, '[PERSON_1]');
  assert.equal(second.maskedText, '[PERSON_2]');
});

test('preserves whitespace and blank lines while replacing tokens', () => {
  const text = '  TITLE\n\n    Alice\n  end  \n';
  const result = { anonymized_text: '  TITLE\n\n    [PERSON_1]\n  end  \n', mapping: { '[PERSON_1]': 'Alice' } };
  const record = createRecord(text, result, { PERSON: 1 });
  assert.equal(record.maskedText, '  TITLE\n\n    [PERSON_2]\n  end  \n');
  assert.equal(restoreText(record.maskedText, record.mapping).text, text);
});

test('maps code-point spans to UTF-16 edits without using canonical values', () => {
  const source = '😀 Alice\n  Acme\nend';
  const masked = '😀 [PERSON_1]\n  [ORG_1]\nend';
  const mapping = { '[PERSON_1]': 'ALICE CANONICAL', '[ORG_1]': 'ACME CANONICAL' };
  const spans = [{ start: 2, end: 7 }, { start: 10, end: 14 }];
  const edits = [{ start: 3, end: 8, text: '[PERSON_1]' }, { start: 11, end: 15, text: '[ORG_1]' }];
  assert.deepEqual(maskedEdits(source, masked, spans, mapping), edits);
  const record = createRecord(source, { anonymized_text: masked, spans, mapping });
  assert.deepEqual(record.edits, edits);
  assert.equal(maskedEdits(source, masked, [{ start: 2, end: 100 }], mapping), null);
  assert.equal(maskedEdits(source, masked + '!', spans, mapping), null);
});

test('restoration edits locate repeated known tokens in new prose and ignore unknown tokens', () => {
  const text = '😀 Reply: [PERSON_1], [UNKNOWN_7]; again [PERSON_1].';
  const first = text.indexOf('[PERSON_1]');
  const second = text.lastIndexOf('[PERSON_1]');
  assert.deepEqual(restorationEdits(text, { '[PERSON_1]': 'Alice' }), [
    { start: first, end: first + '[PERSON_1]'.length, text: 'Alice' },
    { start: second, end: second + '[PERSON_1]'.length, text: 'Alice' },
  ]);
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
