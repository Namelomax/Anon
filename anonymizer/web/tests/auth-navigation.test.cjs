const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const source = fs.readFileSync('lib/auth-navigation.ts', 'utf8');
const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const moduleShim = { exports: {} };
vm.runInNewContext(js, { module: moduleShim, exports: moduleShim.exports, URL, encodeURIComponent });
const { safeReturnPath, loginUrl } = moduleShim.exports;

test('preserves local paths with query and fragment', () => {
  for (const path of ['/', '/account/payments?plan=test&months=12#transfer', '/?tab=account', '/invite?token=abc']) {
    assert.equal(safeReturnPath(path), path);
    assert.equal(loginUrl(path), `/login?next=${encodeURIComponent(path)}`);
  }
});
test('rejects external, malformed and authentication return paths', () => {
  for (const path of [null, '', 'https://evil.test', '//evil.test', '/\\evil.test', '/a\\b', '/a\nfoo', '/a\rfoo', '/login', '/login?next=/', '/register#x', '/%6cogin', '/x/../login']) {
    assert.equal(safeReturnPath(path), '/', String(path));
  }
});
