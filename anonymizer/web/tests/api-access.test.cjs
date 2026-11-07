const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
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
  }, process: { env }, URL, Number, Date, File, Buffer, console }, { filename: file });
  return module.exports;
}
function request(auth, extra = {}) {
  return { url: extra.url || 'https://example.test/api/account/api-keys?id=5', headers: new Headers({ ...(auth == null ? {} : { authorization: auth }), ...extra.headers }), json: async () => extra.body };
}
function setup(options = {}) {
  const calls = { find: [], used: [], users: [], list: [], count: [], create: [], revoke: [], lookup: [], auth: 0 };
  const user = options.user === undefined ? { accountId: 42, role: 'admin', isActive: true, account: { isActive: true } } : options.user;
  const record = options.record === undefined ? { id: 5, revokedAt: null, user: { id: 7, accountId: 42, isActive: true, account: { isActive: true } } } : options.record;
  const prisma = { user: { findUnique: async a => { calls.users.push(a); return user; } }, apiKey: {
    findUnique: async a => { calls.find.push(a); if (options.dbError) throw Error('SECRET_DB'); return record; },
    updateMany: async a => { if ('lastUsedAt' in a.data) { calls.used.push(a); return { count: options.usedCount ?? 1 }; } calls.revoke.push(a); return { count: options.revokeCount ?? 1 }; },
    findMany: async a => { calls.list.push(a); return []; },
    count: async a => { calls.count.push(a); return options.count ?? 0; },
    create: async a => { calls.create.push(a); return { id: 5, name: a.data.name, prefix: a.data.prefix }; },
    findFirst: async a => { calls.lookup.push(a); return options.lookup ?? null; },
  } };
  prisma.$transaction = fn => fn(prisma);
  const keys = load('lib/api-keys.ts', { 'node:crypto': crypto, '@/lib/db': { prisma } });
  const auth = async () => { calls.auth++; return options.session === undefined ? { user: { id: '7', role: 'admin' }, accountId: 42 } : options.session; };
  const common = { 'next/server': { NextResponse }, '@/auth': { auth }, '@/lib/api-keys': keys };
  return { calls, keys, guard: load('lib/auth-guard.ts', common, { ANONYMIZER_REQUIRE_AUTH: 'false' }), publicApi: load('lib/public-api.ts', common), route: load('app/api/account/api-keys/route.ts', { ...common, '@/lib/db': { prisma } }, options.env) };
}
const clean = value => JSON.parse(JSON.stringify(value));
const noStore = response => assert.equal(response.headers.get('cache-control'), 'no-store');

test('issued keys are random, account-bound, hashed and reveal only a prefix', () => {
  const { keys } = setup();
  const a = keys.issueApiKey(42), b = keys.issueApiKey(42);
  assert.match(a.key, /^anon_42_[A-Za-z0-9_-]{43}$/);
  assert.notEqual(a.key, b.key);
  assert.equal(a.keyHash, crypto.createHash('sha256').update(a.key).digest('hex'));
  assert.equal(keys.apiKeyAccountId(a.key), 42);
  assert.ok(a.key.startsWith(a.prefix.slice(0, -1)));
  assert.ok(!a.prefix.includes(a.key));
  for (const id of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) assert.throws(() => keys.issueApiKey(id));
});

test('bearer parsing rejects missing and malformed credentials', () => {
  const { keys } = setup(); const key = keys.issueApiKey(42).key;
  for (const h of [null, '', key, `Basic ${key}`, `Bearer ${key} extra`, 'Bearer anon_0_bad', `Bearer ${'x'.repeat(130)}`]) assert.equal(keys.bearerApiKey(request(h)), null);
  assert.equal(keys.bearerApiKey(request(`bearer ${key}`)), key);
});

test('identification rejects revoked, inactive and transferred credentials without updating last use', async () => {
  const base = { id: 5, revokedAt: null, user: { id: 7, accountId: 42, isActive: true, account: { isActive: true } } };
  for (const record of [null, { ...base, revokedAt: new Date() }, { ...base, user: { ...base.user, isActive: false } }, { ...base, user: { ...base.user, account: { isActive: false } } }, { ...base, user: { ...base.user, accountId: 43 } }]) {
    const ctx = setup({ record }); assert.equal(await ctx.keys.identifyApiKey(ctx.keys.issueApiKey(42).key), null); assert.equal(ctx.calls.used.length, 0);
  }
  const ctx = setup(); const key = ctx.keys.issueApiKey(42).key;
  assert.deepEqual(clean(await ctx.keys.identifyApiKey(key)), { accountId: 42, userId: 7 });
  assert.equal(ctx.calls.find[0].where.keyHash, ctx.keys.hashApiKey(key));
  assert.equal(ctx.calls.used[0].where.revokedAt, null);
  assert.equal(ctx.calls.used[0].where.user.accountId, 42);
  const race = setup({ usedCount: 0 }); assert.equal(await race.keys.identifyApiKey(race.keys.issueApiKey(42).key), null);
});

test('explicit invalid authorization never falls back to a session or development mode', async () => {
  for (const header of ['Basic abc', `Bearer ${setup().keys.issueApiKey(42).key}`]) {
    const ctx = setup({ record: null }); const result = await ctx.guard.resolveIdentity(request(header));
    assert.equal(result.error.status, 401); noStore(result.error); assert.equal(ctx.calls.auth, 0);
  }
  const ctx = setup({ dbError: true }); const result = await ctx.guard.resolveIdentity(request(`Bearer ${ctx.keys.issueApiKey(42).key}`));
  assert.equal(result.error.status, 503); noStore(result.error); assert.doesNotMatch(JSON.stringify(result.error.body), /SECRET_DB/); assert.equal(ctx.calls.auth, 0);
});

test('v1 wrapper requires a syntactically valid key and disables caching', async () => {
  const ctx = setup(); let calls = 0;
  const handler = ctx.publicApi.apiKeyOnly(async () => { calls++; return NextResponse.json({ ok: true }); });
  for (const h of [null, 'Bearer invalid']) { const result = await handler(request(h, { headers: { cookie: 'session=example' } })); assert.equal(result.status, 401); noStore(result); }
  assert.equal(calls, 0);
  const result = await handler(request(`Bearer ${ctx.keys.issueApiKey(42).key}`)); assert.equal(calls, 1); assert.equal(result.status, 200); noStore(result);
});

test('management requires active database administrator in the session account', async () => {
  const guest = setup({ session: null }); assert.equal((await guest.route.GET()).status, 401); assert.equal(guest.calls.users.length, 0);
  const base = { accountId: 42, role: 'admin', isActive: true, account: { isActive: true } };
  for (const user of [null, { ...base, role: 'member' }, { ...base, accountId: 43 }, { ...base, isActive: false }, { ...base, account: { isActive: false } }]) {
    const ctx = setup({ user }); const response = await ctx.route.GET(); assert.equal(response.status, 403); noStore(response); assert.equal(ctx.calls.list.length, 0);
  }
});

test('management creates only hashed keys, returns secret once and lists metadata only', async () => {
  const ctx = setup(); const created = await ctx.route.POST(request(null, { body: { name: '  Integration  ' } }));
  assert.equal(created.status, 201); noStore(created); assert.equal(created.body.apiKey.name, 'Integration');
  const data = ctx.calls.create[0].data; assert.equal(data.keyHash, ctx.keys.hashApiKey(created.body.key)); assert.equal(data.prefix, created.body.apiKey.prefix); assert.ok(!JSON.stringify(data).includes(created.body.key));
  const listed = await ctx.route.GET(); noStore(listed); assert.equal(ctx.calls.list[0].select.keyHash, undefined); assert.equal(listed.body.key, undefined);
  const revoked = await ctx.route.DELETE(request()); noStore(revoked); assert.equal(revoked.body.key, undefined);
});

test('root and admin can list, issue and revoke keys using their current database role', async () => {
  for (const role of ['root', 'admin']) {
    const ctx = setup({
      user: { accountId: 42, role, isActive: true, account: { isActive: true } },
      session: { user: { id: '7' }, role: 'member', accountId: 42 },
    });
    assert.equal((await ctx.route.GET()).status, 200);
    assert.equal((await ctx.route.POST(request(null, { body: { name: 'Synthetic extension' } }))).status, 201);
    assert.equal((await ctx.route.DELETE(request())).status, 200);
    assert.equal(ctx.calls.create[0].data.userId, 7);
  }
  const downgraded = setup({ user: { accountId: 42, role: 'member', isActive: true, account: { isActive: true } },
    session: { user: { id: '7' }, role: 'root', accountId: 42 } });
  for (const method of ['GET', 'POST', 'DELETE']) {
    assert.equal((await downgraded.route[method](request(null, { body: {} }))).status, 403);
  }
  assert.equal(downgraded.calls.create.length, 0);
  assert.equal(downgraded.calls.revoke.length, 0);
});

test('management uses configured public origin behind nginx without trusting forwarded hosts', async () => {
  for (const method of ['POST', 'DELETE']) {
    const ctx = setup({ env: { AUTH_URL: 'https://example.test' } });
    const response = await ctx.route[method](request(null, {
      url: 'http://localhost:3000/api/account/api-keys?id=5',
      headers: { origin: 'https://example.test', 'sec-fetch-site': 'same-origin' }, body: {},
    }));
    assert.equal(response.status, method === 'POST' ? 201 : 200);
    for (const headers of [
      { origin: 'https://evil.test', 'x-forwarded-host': 'evil.test', 'x-forwarded-proto': 'https' },
      { origin: 'http://localhost:3000' },
      { origin: 'https://example.test', 'sec-fetch-site': 'cross-site' },
    ]) {
      const rejected = setup({ env: { AUTH_URL: 'https://example.test' } });
      assert.equal((await rejected.route[method](request(null, {
        url: 'http://localhost:3000/api/account/api-keys?id=5', headers, body: {},
      }))).status, 403);
      assert.equal(rejected.calls.auth, 0);
      assert.equal(rejected.calls.create.length, 0);
      assert.equal(rejected.calls.revoke.length, 0);
    }
  }
  const invalid = setup({ env: { AUTH_URL: 'invalid' } });
  assert.equal((await invalid.route.POST(request(null, { headers: { origin: 'https://example.test' }, body: {} }))).status, 403);
  const direct = setup();
  assert.equal((await direct.route.POST(request(null, { headers: { origin: 'https://example.test' }, body: {} }))).status, 201);
});

test('management validates names and enforces active-key limit', async () => {
  for (const body of [null, [], { name: 4 }, { name: 'x'.repeat(81) }, { name: 'bad\nname' }]) {
    const ctx = setup(); assert.equal((await ctx.route.POST(request(null, { body }))).status, 400); assert.equal(ctx.calls.create.length, 0);
  }
  const ctx = setup({ count: 20 }); assert.equal((await ctx.route.POST(request(null, { body: { name: 'key' } }))).status, 409); assert.equal(ctx.calls.create.length, 0); assert.equal(ctx.calls.count[0].where.revokedAt, null);
});

test('revocation is account-scoped and idempotent', async () => {
  const missing = setup({ revokeCount: 0 }); assert.equal((await missing.route.DELETE(request())).status, 404);
  assert.deepEqual(clean(missing.calls.revoke[0].where), { id: 5, user: { accountId: 42 }, revokedAt: null });
  assert.deepEqual(clean(missing.calls.lookup[0].where), { id: 5, user: { accountId: 42 } });
  const already = setup({ revokeCount: 0, lookup: { id: 5 } }); assert.equal((await already.route.DELETE(request())).status, 200);
});

test('cross-origin mutations are rejected before authentication or database access', async () => {
  for (const method of ['POST', 'DELETE']) for (const headers of [{ origin: 'https://evil.test' }, { 'sec-fetch-site': 'cross-site' }]) {
    const ctx = setup(); const response = await ctx.route[method](request(null, { headers, body: { name: 'key' } }));
    assert.equal(response.status, 403); noStore(response); assert.equal(ctx.calls.auth, 0); assert.equal(ctx.calls.users.length, 0);
  }
});

function anonymizeSetup(quota = { ok: true }, poll = { status: 200, text: '{"status":"pending"}' }) {
  const calls = { identity: [], backend: [], quota: [], terms: [] };
  const respond = (method, status, text) => async (...args) => { calls.backend.push({ method, args }); return { status, text }; };
  const route = load('app/api/anonymize/route.ts', {
    'next/server': { NextResponse },
    '../_shared': { callBackend: respond('POST', 202, '{"job_id":"job-1"}'), callBackendGet: respond('GET', poll.status, poll.text), callBackendDelete: respond('DELETE', 200, '{}'), describeError: () => 'error' },
    '@/lib/auth-guard': { resolveIdentity: async req => { calls.identity.push(req); return { identity: { accountId: 42, userId: 7 } }; } },
    '@/lib/quota': { checkQuota: async id => { calls.quota.push(id); return quota; } },
    '@/lib/db': { prisma: { glossaryTerm: { findMany: async args => { calls.terms.push(args); return []; } } } },
    '@/lib/glossary': { toBackendTerms: terms => terms },
  });
  return { calls, route };
}
test('job polling and cancellation use resolved ownership, ignoring client ownership', async () => {
  for (const method of ['GET', 'DELETE']) {
    const ctx = anonymizeSetup(); const url = 'https://example.test/api/anonymize?jobId=job-1&account_id=999&user_id=999';
    const req = { url, nextUrl: new URL(url) }; const response = await ctx.route[method](req);
    assert.equal(response.status, 200); assert.equal(ctx.calls.identity[0], req);
    const backend = new URL(ctx.calls.backend[0].args[0]);
    assert.equal(backend.searchParams.get('account_id'), '42'); assert.equal(backend.searchParams.get('user_id'), '7');
  }
});
test('job submission uses resolved identity despite spoofed stage fields and enforces quota', async () => {
  const form = new FormData(); form.set('file', new File(['synthetic'], 'test.txt')); form.set('stages', JSON.stringify({ regex: true, account_id: 999, user_id: 999 }));
  const req = { formData: async () => form }; const ctx = anonymizeSetup();
  assert.equal((await ctx.route.POST(req)).status, 202); assert.equal(ctx.calls.identity[0], req); assert.deepEqual(ctx.calls.quota, [42]);
  const payload = JSON.parse(ctx.calls.backend[0].args[1]); assert.equal(payload.account_id, 42); assert.equal(payload.user_id, 7); assert.equal(payload.regex, true);
  assert.equal(ctx.calls.terms[0].where.accountId, 42);
  const denied = anonymizeSetup({ ok: false, status: 429, message: 'quota' });
  assert.equal((await denied.route.POST(req)).status, 429); assert.equal(denied.calls.identity[0], req); assert.equal(denied.calls.backend.length, 0); assert.equal(denied.calls.terms.length, 0);
});

test('upstream polling failures never masquerade as a pending job', async () => {
  for (const status of [401, 403, 429, 503]) {
    const ctx = anonymizeSetup({ ok: true }, { status, text: '{"error":"Synthetic upstream failure"}' });
    const response = await ctx.route.GET({ url: 'https://example.test/api/anonymize?jobId=job-1' });
    assert.equal(response.status, status);
    assert.equal(response.body.error, 'Synthetic upstream failure');
    assert.equal('done' in response.body, false);
  }
});
