'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('fs'), path = require('path'), vm = require('vm');
const R = require('../sw-rules.js');
const SCOPE = 'https://air.example/miniapp/map2/';

test('класифікація: дані тривог і закриті API ніколи не кешуються', () => {
  ['https://air.example/api/public/v1/regions', 'https://air.example/api/stream', 'https://air.example/api/alerts-history?days=7',
    'https://air.example/miniapp/public-regions.json', 'https://air.example/miniapp/alerts_hist.json', 'https://air.example/miniapp/tracks.json', 'https://air.example/miniapp/impacts.json',
    'https://air.example/admin/panel', 'https://air.example/api/threats'].forEach(u => assert.equal(R.classify(u, SCOPE), 'never', u));
});
test('класифікація: наш код і геометрія static, Leaflet окремо, чужі домени не чіпаємо', () => {
  assert.equal(R.classify('https://air.example/miniapp/map2/app.js', SCOPE), 'static');
  assert.equal(R.classify('https://air.example/miniapp/map2/', SCOPE), 'static');
  assert.equal(R.classify('https://air.example/miniapp/raions.json', SCOPE), 'static');
  assert.equal(R.classify('https://unpkg.com/leaflet@1.9.4/dist/leaflet.js', SCOPE), 'leaflet');
  assert.equal(R.classify('https://server.arcgisonline.com/tile/1/2/3', SCOPE), 'other');
  assert.equal(R.classify('https://air.example/miniapp/radar.html', SCOPE), 'other');
});
test('кеші: видаляються лише старі версії map2, чужі (основний застосунок) лишаються', () => {
  const keys = ['airradar-shell-v4', 'airradar-map2-v0', R.CACHE, 'airradar-map2-old'];
  assert.deepEqual(R.staleCaches(keys), ['airradar-map2-v0', 'airradar-map2-old']);
});
test('cacheable: лише успішні basic/cors, не помилки і не opaque', () => {
  assert.ok(R.cacheable({ ok: true, status: 200, type: 'basic' })); assert.ok(R.cacheable({ ok: true, status: 200, type: 'cors' }));
  assert.ok(!R.cacheable({ ok: false, status: 500, type: 'basic' })); assert.ok(!R.cacheable({ ok: true, status: 200, type: 'opaque' })); assert.ok(!R.cacheable(null));
});

/** Запуск реального sw.js у sandbox із підробленими caches/fetch/self. */
function loadSw(version, store, net) {
  const handlers = {}; const rules = fs.readFileSync(path.join(__dirname, '..', 'sw-rules.js'), 'utf8').replace(/const VERSION = '[^']+'/, `const VERSION = '${version}'`);
  const mkCaches = () => ({
    async open(name) { const m = store.get(name) || store.set(name, new Map()).get(name); return { async addAll(l) { for (const u of l) { const r = await net(new URL(u, SCOPE).href); if (!r.ok) throw new Error('addAll ' + u); m.set(new URL(u, SCOPE).href, r); } },
      async put(k, v) { m.set(typeof k === 'string' ? k : k.url, v); } }; },
    async keys() { return [...store.keys()]; }, async has(k) { return store.has(k); }, async delete(k) { return store.delete(k); },
    async match(req) { const url = typeof req === 'string' ? new URL(req, SCOPE).href : req.url; for (const m of store.values()) { const hit = m.get(url) || m.get(url.split('?')[0]); if (hit) return hit; } return undefined; },
  });
  const sb = { self: { registration: { scope: SCOPE }, addEventListener: (n, f) => { handlers[n] = f; }, skipWaiting: async () => { sb.skipped = true; }, clients: { claim: async () => { sb.claimed = true; } } },
    caches: mkCaches(), fetch: req => net(typeof req === 'string' ? req : req.url), URL, Response: { error: () => ({ ok: false, error: true }) }, Promise, setTimeout, clearTimeout, console,
    importScripts: () => { vm.runInContext(rules, sb); }, module: undefined };
  sb.self.Map2SwRules = undefined; vm.createContext(sb); sb.self.self = sb.self;
  // UMD в sandbox пишет в root = self
  vm.runInContext(rules.replace(/\}\)\(typeof self !== 'undefined' \? self : this,/, '})(self,'), sb);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'sw.js'), 'utf8').replace(`importScripts('sw-rules.js');`, '').replace('const R = self.Map2SwRules;', 'const R = self.Map2SwRules;'), sb);
  const fire = async (name, extra) => { let p; handlers[name](Object.assign({ waitUntil: x => { p = x; } , respondWith: x => { p = x; sb.responded = true; } }, extra)); return p; };
  return { sb, fire, handlers };
}
const ok = (body, type) => ({ ok: true, status: 200, type: type || 'basic', body, clone() { return this; } });

test('install кладе код і геометрію в кеш поточної версії; недоступність Leaflet не ламає установку', async () => {
  const store = new Map(); const net = async u => (u.includes('unpkg') ? { ok: false, status: 503 } : ok('f:' + u));
  const sw = loadSw('v1', store, net); await sw.fire('install');
  const c = store.get('airradar-map2-v1'); assert.ok(c.has(SCOPE + 'app.js') && c.has(SCOPE + 'sync.js') && c.has('https://air.example/miniapp/raions.json'));
  assert.equal(sw.sb.skipped, true);
});
test('install падає, якщо наш код недоступний (не ставимо половинчату версію)', async () => {
  const store = new Map(); const net = async u => (u.endsWith('app.js') ? { ok: false, status: 404 } : ok('x'));
  const sw = loadSw('v1', store, net); await assert.rejects(sw.fire('install'));
});
test('оновлення версії SW: activate видаляє старі кеші map2, основний кеш не чіпає', async () => {
  const store = new Map([['airradar-map2-v1', new Map()], ['airradar-shell-v4', new Map([['k', 1]])]]);
  const sw = loadSw('v2', store, async () => ok('x')); await sw.fire('activate');
  assert.deepEqual([...store.keys()].sort(), ['airradar-shell-v4']);   // v2 ще не створений (install не викликався); v1 прибрано
  assert.equal(sw.sb.claimed, true);
  const sw2 = loadSw('v2', store, async () => ok('x')); await sw2.fire('install'); await sw2.fire('activate');
  assert.deepEqual([...store.keys()].sort(), ['airradar-map2-v2', 'airradar-shell-v4']);
});
test('fetch: дані тривог і API SW не перехоплює (respondWith не викликається)', async () => {
  const store = new Map(); const sw = loadSw('v1', store, async () => ok('x')); await sw.fire('install');
  for (const u of ['https://air.example/api/public/v1/regions', 'https://air.example/miniapp/public-regions.json', 'https://air.example/miniapp/alerts_hist.json', 'https://air.example/admin/x']) {
    sw.sb.responded = false; sw.handlers.fetch({ request: { method: 'GET', url: u }, waitUntil: () => {}, respondWith: () => { sw.sb.responded = true; } });
    assert.equal(sw.sb.responded, false, u);
  }
  // і нічого з даних не потрапило в кеш
  assert.ok([...store.get('airradar-map2-v1').keys()].every(k => !/regions|alerts_hist|\/api\//.test(k)));
});
test('fetch: без мережі код віддається з кешу; тривоги з кешу не віддаються', async () => {
  const store = new Map(); let up = true;
  const net = async u => { if (!up && !u.endsWith('sw.js')) throw new Error('offline'); return ok('f:' + u); };
  const sw = loadSw('v1', store, net); await sw.fire('install'); up = false;
  let resp; sw.handlers.fetch({ request: { method: 'GET', url: SCOPE + 'app.js' }, waitUntil: () => {}, respondWith: p => { resp = p; } });
  const r = await resp; assert.equal(r.body, 'f:' + SCOPE + 'app.js');
  let called = false; sw.handlers.fetch({ request: { method: 'GET', url: 'https://air.example/api/public/v1/regions' }, waitUntil: () => {}, respondWith: () => { called = true; } });
  assert.equal(called, false);
});

test('самовідновлення: основний SW видалив кеш карти, наступний fetch наповнює його знову (один раз за життя)', async () => {
  const store = new Map(); let calls = 0;
  const net = async u => { calls++; return ok('f:' + u); };
  const sw = loadSw('v1', store, net); await sw.fire('install'); store.delete('airradar-map2-v1');   // як activate основного sw.js
  let heal; sw.handlers.fetch({ request: { method: 'GET', url: SCOPE + 'app.js' }, waitUntil: p => { heal = p; }, respondWith: () => {} });
  await heal; assert.ok(store.has('airradar-map2-v1') && store.get('airradar-map2-v1').has(SCOPE + 'app.js'));
  store.delete('airradar-map2-v1'); let heal2; sw.handlers.fetch({ request: { method: 'GET', url: SCOPE + 'app.js' }, waitUntil: p => { heal2 = p; }, respondWith: () => {} });
  await heal2; await new Promise(r => setImmediate(r)); assert.ok(!store.has('airradar-map2-v1') || !store.get('airradar-map2-v1').has(SCOPE + 'sync.js'), 'повторного повного precache в межах життя SW немає');
});
test('самовідновлення: нестабільна мережа не викликає циклу повторних precache (cooldown)', async () => {
  const store = new Map(); let calls = 0; let up = true;
  const net = async u => { calls++; if (!up) throw new Error('net'); return ok('f:' + u); };
  const sw = loadSw('v1', store, net); await sw.fire('install'); store.delete('airradar-map2-v1'); up = false; const base = calls;
  for (let i = 0; i < 5; i++) { let h; sw.handlers.fetch({ request: { method: 'GET', url: SCOPE + 'app.js' }, waitUntil: p => { h = p; }, respondWith: () => {} }); await h; }
  const attempts = calls - base; assert.ok(attempts <= 7, 'один precache + запити fetch, не 5 повних precache: ' + attempts);
});
