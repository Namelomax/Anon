const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const legal = { LICENSOR: { fullName: 'ООО Пример', inn: '123456789012', ogrnip: '123456789012345', address: 'Test address', email: 'support@example.test' } };
const validEnv = { BANK_TRANSFER_BANK_NAME: 'Тестовый банк', BANK_TRANSFER_BIC: '123456789', BANK_TRANSFER_ACCOUNT: '12345678901234567890', BANK_TRANSFER_CORR_ACCOUNT: '09876543210987654321' };
function load(file, imports, env = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(js, { module, exports: module.exports, require: name => {
    if (!(name in imports)) throw new Error(`Unexpected import: ${name}`);
    return imports[name];
  }, process: { env }, URL, Number }, { filename: file });
  return module.exports;
}
function setup(options = {}) {
  const calls = { users: [], plans: [] };
  const session = options.session === undefined ? { user: { id: '7', role: 'admin' }, accountId: 42 } : options.session;
  const user = options.user === undefined ? { accountId: 42, isActive: true, role: 'admin', account: { isActive: true } } : options.user;
  const plan = options.plan === undefined ? { id: 19, code: 'business', title: 'Бизнес', priceKopecks: 12345, priceYearKopecks: 120000 } : options.plan;
  const bank = load('lib/bank-transfer.ts', { '@/lib/legal': legal }, options.env || {});
  const route = load('app/api/billing/bank-details/route.ts', {
    'next/server': { NextResponse: { json: (body, init) => ({ body, ...init }) } },
    '@/auth': { auth: async () => { if (options.authError) throw Error('SECRET_AUTH'); return session; } },
    '@/lib/db': { prisma: {
      user: { findUnique: async args => { calls.users.push(args); if (options.userError) throw Error('SECRET_DB'); return user; } },
      plan: { findUnique: async args => { calls.plans.push(args); if (options.planError) throw Error('SECRET_PLAN'); return plan; } },
    } },
    '@/lib/legal': legal, '@/lib/bank-transfer': bank,
  });
  return { calls, get: async (query = 'plan=business&months=1') => {
    const result = await route.GET({ url: `https://example.test/api/billing/bank-details?${query}` });
    assert.equal(result.headers['Cache-Control'], 'no-store');
    return { status: result.status, body: JSON.parse(JSON.stringify(result.body)) };
  } };
}

test('authentication is required before database access', async () => {
  for (const session of [null, { user: { id: 'x' }, accountId: 42 }, { user: { id: '7' }, accountId: '42' }]) {
    const ctx = setup({ session });
    assert.equal((await ctx.get()).status, 401);
    assert.equal(ctx.calls.users.length, 0);
  }
});

test('database role and account membership override stale session data', async () => {
  const cases = [null, { accountId: 42, isActive: true, role: 'member', account: { isActive: true } }, { accountId: 99, isActive: true, role: 'admin', account: { isActive: true } }, { accountId: 42, isActive: false, role: 'admin', account: { isActive: true } }, { accountId: 42, isActive: true, role: 'admin', account: { isActive: false } }];
  for (const user of cases) {
    const ctx = setup({ user });
    assert.equal((await ctx.get()).status, 403);
    assert.equal(ctx.calls.plans.length, 0);
  }
});

test('rejects invalid or duplicate parameters before looking up a plan', async () => {
  for (const query of ['plan=business&months=2', 'plan=business&months=01', 'plan=business&months=1&months=12', 'plan=business&plan=other&months=1', 'plan=&months=1', 'months=1', `plan=${'a'.repeat(129)}&months=1`]) {
    const ctx = setup();
    assert.equal((await ctx.get(query)).status, 400, query);
    assert.equal(ctx.calls.plans.length, 0);
  }
});

test('rejects unavailable plans and terms', async () => {
  const base = { id: 19, code: 'business', title: 'Бизнес', priceKopecks: 12345, priceYearKopecks: 120000 };
  for (const plan of [null, { ...base, code: 'trial' }, { ...base, code: 'unlimited' }, { ...base, priceYearKopecks: null }, { ...base, priceYearKopecks: 0 }]) {
    assert.equal((await setup({ plan }).get('plan=business&months=12')).status, 400);
  }
});

test('quote takes price and plan id from database and account from session', async () => {
  const ctx = setup({ env: validEnv });
  const { status, body } = await ctx.get('plan=business&months=12&amount=1&accountId=999&planId=999');
  assert.equal(status, 200);
  assert.deepEqual(body.selection, { plan: 'business', title: 'Бизнес', months: 12, amountKopecks: 120000 });
  assert.equal(body.purpose, 'Оплата лицензии Anon. Аккаунт 42; тариф 19; срок 12 мес.');
  assert.equal(body.configured, true);
  assert.deepEqual(body.recipient, { name: legal.LICENSOR.fullName, inn: legal.LICENSOR.inn, ogrnip: legal.LICENSOR.ogrnip, address: legal.LICENSOR.address, bankName: validEnv.BANK_TRANSFER_BANK_NAME, bic: validEnv.BANK_TRANSFER_BIC, account: validEnv.BANK_TRANSFER_ACCOUNT, correspondentAccount: validEnv.BANK_TRANSFER_CORR_ACCOUNT });
  assert.equal(body.supportEmail, legal.LICENSOR.email);
  assert.deepEqual(JSON.parse(JSON.stringify(ctx.calls.plans[0])), { where: { code: 'business' } });
});

test('incomplete or invalid configuration never exposes partial recipient', async () => {
  for (const env of [{ ...validEnv, BANK_TRANSFER_BIC: '' }, { ...validEnv, BANK_TRANSFER_ACCOUNT: '123' }, { ...validEnv, BANK_TRANSFER_BANK_NAME: 'Банк\nтест' }, { ...validEnv, BANK_TRANSFER_BANK_INN: '123' }, { ...validEnv, BANK_TRANSFER_BANK_ADDRESS: 'Address\nBank' }, { ...validEnv, BANK_TRANSFER_BANK_ADDRESS: 'a'.repeat(501) }]) {
    const { status, body } = await setup({ env }).get();
    assert.equal(status, 200);
    assert.equal(body.configured, false);
    assert.equal(body.selection.amountKopecks, 12345);
    assert.equal(body.supportEmail, legal.LICENSOR.email);
    assert.equal('recipient' in body, false);
    assert.equal('purpose' in body, false);
  }
});

test('internal failures return 503 without error details', async () => {
  for (const options of [{ authError: true }, { userError: true }, { planError: true }]) {
    const { status, body } = await setup(options).get();
    assert.equal(status, 503);
    assert.deepEqual(Object.keys(body), ['error']);
    assert.doesNotMatch(JSON.stringify(body), /SECRET/);
  }
});


test('optional bank details are trimmed and included only when present', async () => {
  const absent = await setup({ env: { ...validEnv, BANK_TRANSFER_BANK_INN: '', BANK_TRANSFER_BANK_ADDRESS: ' ' } }).get();
  assert.equal(absent.body.configured, true);
  assert.equal('bankInn' in absent.body.recipient, false);
  assert.equal('bankAddress' in absent.body.recipient, false);
  const present = await setup({ env: { ...validEnv, BANK_TRANSFER_BANK_INN: ' 1234567890 ', BANK_TRANSFER_BANK_ADDRESS: ' Test bank address ' } }).get();
  assert.equal(present.body.configured, true);
  assert.equal(present.body.recipient.bankInn, '1234567890');
  assert.equal(present.body.recipient.bankAddress, 'Test bank address');
});
