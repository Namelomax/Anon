const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

class NextResponse {
  constructor(body, init = {}) { this.body = body; this.status = init.status || 200; this.headers = new Headers(init.headers); }
  static json(body, init) { return new NextResponse(body, init); }
}
function load(file, imports, env = {}) {
  const source = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(js, { module, exports: module.exports, require: name => {
    if (!(name in imports)) throw Error(`Unexpected import: ${name}`);
    return imports[name];
  }, process: { env }, URL, Number, Date, console }, { filename: file });
  return module.exports;
}
const clean = x => JSON.parse(JSON.stringify(x));
const noStore = response => assert.equal(response.headers.get('cache-control'), 'no-store');
function req({ url = 'https://example.test/api/admin/accounts', headers = {}, body, badJson = false } = {}) {
  return { url, nextUrl: new URL(url), headers: new Headers(headers), json: async () => { if (badJson) throw Error('bad JSON'); return body; } };
}
function setup(options = {}) {
  const calls = { users: [], count: [], list: [], update: [], quota: [] };
  const user = options.user === undefined ? { accountId: 42, role: 'root', isActive: true, account: { isActive: true } } : options.user;
  const session = options.session === undefined ? { user: { id: '7', role: 'root' }, accountId: 42 } : options.session;
  const account = { id: 19, name: 'Synthetic', isActive: true, plan: { code: 'test', title: 'Test', pagesPerMonth: 100 }, pageLimitOverrideTenths: null, users: [{ id: 3, email: 'test@example.test', role: 'admin', isActive: true }], _count: { users: 101 } };
  const quota = { period: '2026-01', usedTenths: 12, limitTenths: 1000, grantsTenths: 20, allowanceTenths: 1020, remainingTenths: 1008, expired: false, paidUntil: null, account: { passwordHash: 'SECRET' } };
  const prisma = {
    user: { findUnique: async args => { calls.users.push(args); if (options.userError) throw Error('SECRET_DB'); return user; } },
    account: {
      count: async args => { calls.count.push(args); if (options.dbError) throw Error('SECRET_DB'); return 26; },
      findMany: async args => { calls.list.push(args); return [account]; },
      updateMany: async args => { calls.update.push(args); if (options.dbError) throw Error('SECRET_DB'); return { count: options.updateCount ?? 1 }; },
    },
  };
  const auth = async () => session;
  const guard = load('lib/root-admin.ts', { 'next/server': { NextResponse }, '@/auth': { auth }, '@/lib/db': { prisma } });
  const imports = { 'next/server': { NextResponse }, '@/lib/root-admin': guard, '@/lib/db': { prisma }, '@/lib/quota': { getQuotaState: async id => { calls.quota.push(id); return quota; } } };
  return { calls, guard, route: load('app/api/admin/accounts/route.ts', imports, options.env), account };
}

test('root guard verifies the current database role and account', async () => {
  const guest = setup({ session: null }); const denied = await guest.guard.requireRoot();
  assert.equal(denied.status, 401); noStore(denied); assert.equal(guest.calls.users.length, 0);
  const base = { accountId: 42, role: 'root', isActive: true, account: { isActive: true } };
  for (const user of [null, { ...base, role: 'member' }, { ...base, role: 'admin' }, { ...base, isActive: false }, { ...base, account: { isActive: false } }, { ...base, accountId: 43 }]) {
    const response = await setup({ user }).guard.requireRoot(); assert.equal(response.status, 403); noStore(response);
  }
  const ctx = setup({ session: { user: { id: '7', role: 'member' }, accountId: 42 } });
  assert.deepEqual(clean(await ctx.guard.requireRoot()), { userId: 7, accountId: 42 });
  assert.deepEqual(clean(ctx.calls.users[0].select), { accountId: true, role: true, isActive: true, account: { select: { isActive: true } } });
});

test('GET and PATCH require root authorization', async () => {
  for (const session of [null, { user: { id: '7', role: 'root' }, accountId: 42 }]) {
    const ctx = setup({ session, user: { accountId: 42, role: 'admin', isActive: true, account: { isActive: true } } });
    for (const method of ['GET', 'PATCH']) {
      const response = await ctx.route[method](req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, pageLimitOverrideTenths: 0 } }));
      assert.equal(response.status, session ? 403 : 401); noStore(response);
    }
    assert.equal(ctx.calls.list.length, 0); assert.equal(ctx.calls.update.length, 0);
  }
});

test('PATCH requires exact same origin, including configured public origin', async () => {
  for (const headers of [{}, { origin: 'https://evil.test' }, { origin: 'https://example.test', 'sec-fetch-site': 'cross-site' }]) {
    const ctx = setup(); const response = await ctx.route.PATCH(req({ headers, body: { accountId: 19, pageLimitOverrideTenths: 0 } }));
    assert.equal(response.status, 403); noStore(response); assert.equal(ctx.calls.update.length, 0);
  }
  const ctx = setup({ env: { AUTH_URL: 'https://example.test' } });
  const response = await ctx.route.PATCH(req({ url: 'http://localhost:3000/api/admin/accounts', headers: { origin: 'https://example.test' }, body: { accountId: 19, pageLimitOverrideTenths: 0 } }));
  assert.equal(response.status, 200); noStore(response);
  const invalid = setup({ env: { AUTH_URL: 'invalid' } });
  assert.equal((await invalid.route.PATCH(req({ headers: { origin: 'https://example.test' }, body: {} }))).status, 403);
});

test('PATCH rejects malformed input without changing any account', async () => {
  const bodies = [null, [], {}, { accountId: 1 }, { accountId: 1, pageLimitOverrideTenths: 0, extra: true },
    ...[0, -1, 1.5, '1', 2147483648].map(accountId => ({ accountId, pageLimitOverrideTenths: 0 })),
    ...[-1, 1.5, '1', 2147483648].map(pageLimitOverrideTenths => ({ accountId: 1, pageLimitOverrideTenths }))];
  for (const body of bodies) {
    const ctx = setup(); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, body }));
    assert.equal(response.status, 400, JSON.stringify(body)); noStore(response); assert.equal(ctx.calls.update.length, 0);
  }
  const ctx = setup(); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, badJson: true }));
  assert.equal(response.status, 400); noStore(response); assert.equal(ctx.calls.update.length, 0);
});

test('PATCH accepts zero and null, updates only the limit and reports missing accounts', async () => {
  for (const value of [0, null, 2147483647]) {
    const ctx = setup(); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, pageLimitOverrideTenths: value } }));
    assert.equal(response.status, 200); noStore(response); assert.deepEqual(clean(response.body), { ok: true });
    assert.deepEqual(clean(ctx.calls.update[0]), { where: { id: 19 }, data: { pageLimitOverrideTenths: value } });
  }
  const ctx = setup({ updateCount: 0 }); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, pageLimitOverrideTenths: null } }));
  assert.equal(response.status, 404); noStore(response);
});

test('GET searches all accounts with bounded pagination and returns live quota without secrets', async () => {
  const ctx = setup(); const response = await ctx.route.GET(req({ url: 'https://example.test/api/admin/accounts?q=synthetic&page=2' }));
  assert.equal(response.status, 200); noStore(response);
  const where = { OR: [{ name: { contains: 'synthetic' } }, { users: { some: { email: { contains: 'synthetic' } } } }] };
  assert.deepEqual(clean(ctx.calls.count[0].where), where);
  assert.deepEqual(clean(ctx.calls.list[0]), { where, skip: 25, take: 25, orderBy: { id: 'desc' }, select: { id: true, name: true, isActive: true, plan: { select: { code: true, title: true, pagesPerMonth: true } }, pageLimitOverrideTenths: true, users: { select: { id: true, email: true, role: true, isActive: true }, orderBy: { id: 'asc' }, take: 100 }, _count: { select: { users: true } } } });
  assert.deepEqual(ctx.calls.quota, [19]); assert.equal(response.body.page, 2); assert.equal(response.body.pageSize, 25); assert.equal(response.body.total, 26);
  assert.equal(response.body.accounts[0].userCount, 101); assert.equal(response.body.accounts[0]._count, undefined);
  assert.deepEqual(clean(response.body.accounts[0].quota), { period: '2026-01', usedTenths: 12, limitTenths: 1000, grantsTenths: 20, allowanceTenths: 1020, remainingTenths: 1008, expired: false, paidUntil: null });
  assert.doesNotMatch(JSON.stringify(response.body), /SECRET|passwordHash/);
});

test('GET rejects invalid and repeated query parameters', async () => {
  for (const query of ['page=0', 'page=-1', 'page=1.5', 'page=abc', 'page=1&page=2', 'q=a&q=b', `q=${'x'.repeat(201)}`]) {
    const ctx = setup(); const response = await ctx.route.GET(req({ url: `https://example.test/api/admin/accounts?${query}` }));
    assert.equal(response.status, 400, query); noStore(response); assert.equal(ctx.calls.list.length, 0); assert.equal(ctx.calls.count.length, 0);
  }
});

test('database errors produce safe non-cacheable responses', async () => {
  for (const method of ['GET', 'PATCH']) {
    const ctx = setup({ dbError: true }); const response = await ctx.route[method](req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, pageLimitOverrideTenths: 0 } }));
    assert.equal(response.status, 503); noStore(response); assert.doesNotMatch(JSON.stringify(response.body), /SECRET_DB/);
  }
});

test('manual limit changes immediately affect real quota checks without resetting usage or grants', async () => {
  const account = { id: 19, isActive: true, paidUntil: null, periodAnchorAt: null,
    pageLimitOverrideTenths: null, plan: { code: 'basic', pagesPerMonth: 100 } };
  const calls = { counters: [], grants: [], imports: 0 };
  const prisma = {
    user: { findUnique: async () => ({ accountId: 42, role: 'root', isActive: true, account: { isActive: true } }) },
    account: {
      findUnique: async ({ where }) => where.id === 19 ? account : null,
      updateMany: async ({ where, data }) => {
        if (where.id !== account.id) return { count: 0 };
        Object.assign(account, data); return { count: 1 };
      },
    },
    quotaCounter: { upsert: async args => { calls.counters.push(args); return { pagesUsedTenths: 50 }; } },
    quotaGrant: { aggregate: async args => { calls.grants.push(args); return { _sum: { pagesTenths: 20 } }; } },
  };
  const quota = load('lib/quota.ts', {
    '@/lib/db': { prisma }, '@/lib/usage-import': { importUsageLog: async () => { calls.imports++; } },
    '@/lib/period': { windowFor: () => ({ key: '2026-01', start: new Date('2026-01-01'), end: null }), formatDateRu: () => '01.01.2026' },
  });
  const auth = async () => ({ user: { id: '7' }, accountId: 42 });
  const guard = load('lib/root-admin.ts', { 'next/server': { NextResponse }, '@/auth': { auth }, '@/lib/db': { prisma } });
  const route = load('app/api/admin/accounts/route.ts', { 'next/server': { NextResponse }, '@/lib/root-admin': guard, '@/lib/db': { prisma }, '@/lib/quota': quota });
  const change = async value => {
    const response = await route.PATCH(req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, pageLimitOverrideTenths: value } }));
    assert.equal(response.status, 200); noStore(response);
  };
  const state = async (limit, allowance, remaining) => {
    const actual = await quota.getQuotaState(19);
    assert.equal(actual.limitTenths, limit); assert.equal(actual.grantsTenths, 20);
    assert.equal(actual.allowanceTenths, allowance); assert.equal(actual.remainingTenths, remaining);
    assert.equal(actual.usedTenths, 50);
  };
  await state(1000, 1020, 970);
  await change(0); await state(0, 20, 0);
  assert.equal((await quota.checkQuota(19)).status, 402);
  await change(40); await state(40, 60, 10);
  assert.equal((await quota.checkQuota(19)).ok, true);
  await change(null); await state(1000, 1020, 970);
  await change(2147483647); account.paidUntil = new Date('2000-01-01T00:00:00Z');
  const expired = await quota.getQuotaState(19);
  assert.equal(expired.expired, true); assert.equal(expired.remainingTenths, 0); assert.equal(expired.usedTenths, 50);
  assert.equal((await quota.checkQuota(19)).status, 402);
  assert.ok(calls.imports > 0);
  for (const call of calls.counters) assert.deepEqual(clean(call.update), {});
  for (const call of calls.grants) assert.deepEqual(clean(call.where), { accountId: 19, OR: [{ period: '2026-01' }, { period: null }] });
});

test('PATCH validates canonical UTC expiry and updates only requested fields', async () => {
  for (const [body, expected] of [
    [{ accountId: 19, paidUntil: '2030-02-28T12:34:56.789Z' }, { paidUntil: '2030-02-28T12:34:56.789Z' }],
    [{ accountId: 19, paidUntil: '2000-01-01T00:00:00.000Z' }, { paidUntil: '2000-01-01T00:00:00.000Z' }],
    [{ accountId: 19, paidUntil: null }, { paidUntil: null }],
    [{ accountId: 19, paidUntil: '2030-01-01T00:00:00.000Z', pageLimitOverrideTenths: 0 }, { paidUntil: '2030-01-01T00:00:00.000Z', pageLimitOverrideTenths: 0 }],
  ]) {
    const ctx = setup(); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, body }));
    assert.equal(response.status, 200); noStore(response);
    assert.deepEqual(clean(ctx.calls.update[0]), { where: { id: 19 }, data: expected });
    if (typeof body.paidUntil === 'string') assert.equal(Object.prototype.toString.call(ctx.calls.update[0].data.paidUntil), '[object Date]');
  }
  for (const paidUntil of [0, true, {}, [], '2030-02-30T00:00:00.000Z', '0000-01-01T00:00:00.000Z', '2030-01-01T00:00:00Z', '2030-01-01T00:00:00.000+00:00', '2030-01-01T00:00:00.000', 'not-a-date']) {
    const ctx = setup(); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, paidUntil } }));
    assert.equal(response.status, 400, JSON.stringify(paidUntil)); noStore(response); assert.equal(ctx.calls.update.length, 0);
  }
  const ctx = setup(); const response = await ctx.route.PATCH(req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, paidUntil: null, periodAnchorAt: null } }));
  assert.equal(response.status, 400); assert.equal(ctx.calls.update.length, 0);
});

test('expiry updates immediately change real quota decisions without changing anchor, limit or usage', async () => {
  const anchor = new Date('2024-01-31T12:00:00.000Z');
  const account = { id: 19, isActive: true, paidUntil: new Date('2000-01-01T00:00:00.000Z'), periodAnchorAt: anchor,
    pageLimitOverrideTenths: 100, planId: 4, plan: { code: 'basic', pagesPerMonth: 10 } };
  const updates = [], counters = [], grants = [];
  const prisma = {
    user: { findUnique: async () => ({ accountId: 42, role: 'root', isActive: true, account: { isActive: true } }) },
    account: { findUnique: async () => account, updateMany: async args => { updates.push(args); Object.assign(account, args.data); return { count: 1 }; } },
    quotaCounter: { upsert: async args => { counters.push(args); return { pagesUsedTenths: 50 }; } },
    quotaGrant: { aggregate: async args => { grants.push(args); return { _sum: { pagesTenths: 20 } }; } },
  };
  const period = load('lib/period.ts', {});
  const quota = load('lib/quota.ts', { '@/lib/db': { prisma }, '@/lib/usage-import': { importUsageLog: async () => {} }, '@/lib/period': period });
  const guard = load('lib/root-admin.ts', { 'next/server': { NextResponse }, '@/auth': { auth: async () => ({ user: { id: '7' }, accountId: 42 }) }, '@/lib/db': { prisma } });
  const route = load('app/api/admin/accounts/route.ts', { 'next/server': { NextResponse }, '@/lib/root-admin': guard, '@/lib/db': { prisma }, '@/lib/quota': quota });
  const change = async paidUntil => {
    const response = await route.PATCH(req({ headers: { origin: 'https://example.test' }, body: { accountId: 19, paidUntil } }));
    assert.equal(response.status, 200); assert.deepEqual(Object.keys(updates.at(-1).data), ['paidUntil']);
    assert.equal(account.periodAnchorAt, anchor); assert.equal(account.pageLimitOverrideTenths, 100); assert.equal(account.planId, 4);
  };
  assert.equal((await quota.checkQuota(19)).status, 402);
  await change('9999-12-31T23:59:59.999Z');
  let state = await quota.getQuotaState(19); assert.equal(state.usedTenths, 50); assert.equal(state.remainingTenths, 70); assert.equal((await quota.checkQuota(19)).ok, true);
  await change('2000-01-01T00:00:00.000Z'); assert.equal((await quota.checkQuota(19)).status, 402);
  await change(null); state = await quota.getQuotaState(19); assert.equal(state.usedTenths, 50); assert.equal(state.expired, false); assert.equal((await quota.checkQuota(19)).ok, true);
  for (const call of counters) assert.deepEqual(clean(call.update), {});
  assert.ok(grants.length > 0);
  assert.equal(period.addMonthsClamped(anchor, 1).toISOString(), '2024-02-29T12:00:00.000Z');
  assert.equal(period.addMonthsClamped(anchor, 2).toISOString(), '2024-03-31T12:00:00.000Z');
});
