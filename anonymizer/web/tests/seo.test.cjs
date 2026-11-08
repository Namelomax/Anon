const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

function load(file, imports) {
  const source = fs.readFileSync(file, 'utf8');
  const js = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
    jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const module = { exports: {} };
  vm.runInNewContext(js, { module, exports: module.exports, URL, URLSearchParams,
    require(name) {
      if (name === 'react/jsx-runtime') return require(name);
      if (!(name in imports)) throw new Error(`Unexpected import: ${name}`);
      return imports[name];
    },
  }, { filename: file });
  return module.exports;
}

const site = { SITE_NAME: 'Anon', SITE_URL: 'https://example.test', SITE_TITLE: 'Documents', SITE_DESCRIPTION: 'Mask documents' };
const seo = load('lib/seo.ts', { '@/lib/site': site });
const clean = value => JSON.parse(JSON.stringify(value));

function home(session) {
  const Landing = () => null;
  const Workspace = () => null;
  const mod = load('app/page.tsx', {
    '@/auth': { auth: async () => session },
    'next/navigation': { redirect: path => { throw Object.assign(new Error('redirect'), { path }); } },
    '@/lib/auth-navigation': { loginUrl: path => `/login?next=${encodeURIComponent(path)}` },
    '@/lib/seo': seo, '@/lib/site': site,
    './landing': { default: Landing }, './Workspace': { default: Workspace },
  });
  return { ...mod, Landing, Workspace };
}

test('anonymous home renders the landing on the server, independent of client session loading', async () => {
  const route = home(null);
  const result = await route.default({ searchParams: Promise.resolve({}) });
  assert.equal(result.type, route.Landing);
  assert.equal(route.dynamic, 'force-dynamic');
});

test('authenticated home preserves the workspace, incomplete sessions remain public', async () => {
  const route = home({ user: { id: 'synthetic-user' } });
  assert.equal((await route.default({ searchParams: Promise.resolve({}) })).type, route.Workspace);
  const anonymous = home({ user: {} });
  assert.equal((await anonymous.default({ searchParams: Promise.resolve({}) })).type, anonymous.Landing);
});

test('anonymous account deep links keep local return parameters and require login', async () => {
  const route = home(null);
  await assert.rejects(route.default({ searchParams: Promise.resolve({ tab: 'account', tag: ['one', 'two'] }) }), error => {
    const url = new URL(error.path, site.SITE_URL);
    assert.equal(url.pathname, '/login');
    assert.equal(url.searchParams.get('next'), '/?tab=account&tag=one&tag=two');
    return true;
  });
});

test('each public page has its own absolute canonical and matching sharing URL', () => {
  const home = seo.pageMetadata('Home', 'Description', '/');
  const docs = seo.pageMetadata('API', 'Documentation', '/developers');
  assert.equal(home.alternates.canonical, 'https://example.test/');
  assert.equal(docs.alternates.canonical, 'https://example.test/developers');
  assert.equal(docs.openGraph.url, docs.alternates.canonical);
});

test('sitemap includes public pages and excludes duplicate and private routes', () => {
  const sitemap = load('app/sitemap.ts', { '@/lib/site': site,
    '@/lib/legal': { LEGAL_DOCS: { offer: { href: '/legal/offer' }, privacy: { href: '/legal/privacy' } } },
  }).default();
  const urls = sitemap.map(item => item.url);
  assert.equal(new Set(urls).size, urls.length);
  for (const path of ['/', '/developers', '/pricing', '/on-premise', '/formats/pdf', '/formats/docx', '/formats/excel', '/guides/pseudonymization-and-ai']) {
    assert.ok(urls.includes(`https://example.test${path}`), path);
  }
  assert.ok(urls.every(url => !/\/(welcome|account|billing|login|register)(\/|$)/.test(url)));
});

test('public catalog queries only public plan fields, never account or payment data', async () => {
  let query;
  const sample = [{ code: 'synthetic-plan', priceKopecks: 12345, pagesPerMonth: 500 }];
  const catalog = load('lib/public-plans.ts', { 'server-only': {}, '@/lib/db': { prisma: {
    plan: { findMany: async args => { query = args; return sample; } },
  } } });
  assert.equal(await catalog.getPublicPlans(), sample);
  assert.deepEqual(clean(query.where), { code: { notIn: ['unlimited', 'trial'] }, priceKopecks: { gt: 0 } });
  assert.deepEqual(Object.keys(query.select).sort(), ['code', 'maxUsers', 'pagesPerMonth', 'priceKopecks', 'priceYearKopecks', 'title']);
  assert.equal(query.include, undefined);
});
