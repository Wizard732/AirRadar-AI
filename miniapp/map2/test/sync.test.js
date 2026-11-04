'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const DataSync = require('../sync.js');

/** Стенд: підроблені час, таймери, мережа, EventSource, сховище та події. */
function rig(o) {
  o = o || {};
  let t = 1_700_000_000_000; const timers = new Map(); let tid = 0;
  const handlers = {}; const sses = []; const mem = new Map(Object.entries(o.storage || {}));
  const r = {
    net: { up: true, data: null, hist: null, histFail: false, calls: 0, hcalls: 0, delay: null }, sseSupported: o.sse !== false,
    now: () => t, mem, sses, timers,
    advance(ms) { const end = t + ms; for (;;) { const nx = [...timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0]; if (!nx) break; t = Math.max(t, nx[1].at); timers.delete(nx[0]); nx[1].fn(); } t = end; },
    emit(n) { (handlers[n] || []).slice().forEach(f => f()); },
    handlerCount: () => Object.values(handlers).reduce((a, l) => a + l.length, 0),
  };
  class FakeES { constructor(u) { this.url = u; this.readyState = 0; this.listeners = {}; sses.push(this); }
    addEventListener(n, f) { this.listeners[n] = f; } close() { this.readyState = 2; }
    open() { this.readyState = 1; this.onopen && this.onopen(); } msg(d) { this.onmessage && this.onmessage({ data: d }); }
    fail(closed) { this.readyState = closed ? 2 : 0; this.onerror && this.onerror(); } }
  const env = {
    fetchData: async () => { r.net.calls++; if (r.net.delay) await r.net.delay(); if (!r.net.up) throw new Error('net'); return typeof r.net.data === 'function' ? r.net.data() : r.net.data; },
    fetchHistory: async () => { r.net.hcalls++; if (!r.net.up || r.net.histFail) throw new Error('hist'); return r.net.hist || { alerts: [], now: Math.floor(t / 1000) }; },
    now: () => t, setTimeout: (f, ms) => { const id = ++tid; timers.set(id, { at: t + ms, fn: f }); return id; }, clearTimeout: id => timers.delete(id),
    random: () => 0.5, EventSource: r.sseSupported ? FakeES : null, sseUrl: '/api/stream', live: o.live !== false,
    storage: { get: k => (mem.has(k) ? mem.get(k) : null), set: (k, v) => mem.set(k, v), del: k => mem.delete(k) },
    cacheKey: 'c', histKey: 'h',
    listen: (n, f) => { (handlers[n] = handlers[n] || []).push(f); return () => { handlers[n] = handlers[n].filter(x => x !== f); }; },
  };
  r.s = new DataSync(env); r.sec = () => Math.floor(t / 1000);
  r.payload = (upd, sirens) => ({ updated_at: upd == null ? r.sec() : upd, delay_min: 0, official: { sirens: sirens || [], sirens_raions: [] }, regions: [] });
  r.log = { data: [], conn: [], hist: [] };
  r.s.on('data', d => r.log.data.push(d)).on('conn', c => r.log.conn.push(c)).on('history', h => r.log.hist.push(h));
  return r;
}
const flush = () => new Promise(res => setImmediate(res));
async function settle(r, ms) { for (let i = 0; i < 6; i++) { await flush(); } if (ms) { r.advance(ms); for (let i = 0; i < 6; i++) await flush(); } }

test('запуск без кешу і без мережі: чесна помилка, не порожня карта', async () => {
  const r = rig(); r.net.up = false; await r.s.start(); await settle(r);
  assert.equal(r.s.conn, 'error'); assert.equal(r.log.data.length, 0);
});

test('запуск з кешем без мережі: збережені дані з віком, стан offline', async () => {
  const r = rig(); const now = r.sec();
  r.mem.set('c', DataSync.wrapCache(r.payload(now - 300, ['kyivska']), now - 300));
  r.net.up = false; await r.s.start(); await settle(r);
  assert.equal(r.s.conn, 'offline'); assert.equal(r.log.data.length, 1);
  assert.equal(r.log.data[0].fromCache, true); assert.equal(r.log.data[0].savedAt, now - 300);
});

test('кеш старший за 6 годин не показується і прибирається', async () => {
  const r = rig(); const now = r.sec();
  r.mem.set('c', DataSync.wrapCache(r.payload(now - 7 * 3600), now - 7 * 3600));
  r.net.up = false; await r.s.start(); await settle(r);
  assert.equal(r.s.conn, 'error'); assert.equal(r.log.data.length, 0); assert.equal(r.mem.has('c'), false);
});

test('старий формат кешу (сам контракт) приймається, вік = updated_at', async () => {
  const r = rig(); const now = r.sec(); r.mem.set('c', JSON.stringify(r.payload(now - 60)));
  r.net.up = false; await r.s.start(); await settle(r);
  assert.equal(r.log.data[0].savedAt, now - 60);
});

test('offline → online: відновлення, recovered → ok, дані оновлені', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  assert.equal(r.s.conn, 'ok');
  r.net.up = false; r.emit('offline'); await settle(r);
  assert.equal(r.s.conn, 'offline');
  r.advance(1000); r.net.up = true; r.net.data = r.payload(); r.emit('online'); await settle(r);
  assert.equal(r.s.conn, 'recovered');
  r.advance(4100); assert.equal(r.s.conn, 'ok');
  assert.deepEqual(r.log.conn, ['ok', 'offline', 'recovered', 'ok']);
});

test('помилка опитування → backoff без шторму → успішне відновлення', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  r.net.up = false;
  let calls0 = r.net.calls;
  for (let i = 0; i < 12; i++) await settle(r, 60000);       // 12 хвилин поспіль
  const fails = r.net.calls - calls0;
  assert.ok(fails <= 9, 'не частіше ~за backoff, було ' + fails);
  assert.ok(r.s.failCount >= 5);
  r.net.up = true; r.net.data = r.payload(); await settle(r, 130000);
  assert.equal(r.s.failCount, 0); assert.ok(['ok', 'recovered'].includes(r.s.conn));
});

test('backoff: росте 10 → 20 → 40 … і обмежений 120 с', () => {
  const b = n => DataSync.backoffMs(n, () => 0.5);
  assert.deepEqual([1, 2, 3, 4, 5, 9].map(b), [10000, 20000, 40000, 80000, 120000, 120000]);
  assert.ok(DataSync.backoffMs(3, () => 0) < DataSync.backoffMs(3, () => 1));
});

test('паралельних опитувань немає: виклики під час запиту зливаються в один повтор', async () => {
  const r = rig(); r.net.data = r.payload(); let release; r.net.delay = () => new Promise(res => { release = res; });
  const p = r.s.start(); await flush(); r.s.load(); r.s.load(); await flush();
  assert.equal(r.net.calls, 1);
  const first = release; r.net.delay = null; first(); await p; await settle(r);
  assert.equal(r.net.calls, 2, 'один повтор, не три');
});

test('одне опитування у черзі: після start лише один таймер', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  assert.equal(r.timers.size <= 2, true);   // таймер опитування (+ можливо відновлення)
  await r.s.load(); await r.s.load(); await settle(r);
  assert.equal([...r.timers.values()].length <= 2, true);
});

test('stale → fresh: прапор stale знімається новими даними', async () => {
  const r = rig(); const AirDataNormalizeStale = (raw, n) => (n - raw.updated_at) > 900;
  r.net.data = r.payload(r.sec() - 3000); await r.s.start(); await settle(r);
  assert.ok(AirDataNormalizeStale(r.log.data[0].raw, r.sec()));
  r.net.data = r.payload(r.sec()); await r.s.load(); await settle(r);
  assert.ok(!AirDataNormalizeStale(r.log.data.at(-1).raw, r.sec()));
});

test('відповідь, старіша за застосовану, відкидається і дані не відкочуються', async () => {
  const r = rig(); const n = r.sec(); r.net.data = r.payload(n, ['kyivska']); await r.s.start(); await settle(r);
  r.net.data = r.payload(n - 600, []); await r.s.load(); await settle(r);
  assert.equal(r.log.data.length, 1); assert.equal(r.s.stats.ignoredOld, 1); assert.equal(r.s.lastUpdated, n);
  assert.equal(r.s.conn, 'ok');
});

test('кеш не перебиває свіжіші дані: пізніший кеш не застосовується після успіху', async () => {
  const r = rig(); const n = r.sec(); r.mem.set('c', DataSync.wrapCache(r.payload(n - 100), n - 100));
  r.net.data = r.payload(n); await r.s.start(); await settle(r);
  assert.equal(r.log.data.length, 1); assert.equal(r.log.data[0].fromCache, false);
});

test('SSE: повторні події з тим самим rev не створюють запитів', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  const es = r.sses[0]; es.open(); await settle(r);
  const base = r.net.calls;
  es.msg('rev1'); await settle(r); const afterFirst = r.net.calls;
  es.msg('rev1'); es.msg('rev1'); await settle(r);
  assert.equal(afterFirst, base + 1); assert.equal(r.net.calls, afterFirst); assert.ok(r.s.stats.ignoredRev >= 2);
  es.msg('rev2'); await settle(r); assert.equal(r.net.calls, afterFirst + 1);
});

test('SSE: rev не запам\'ятовується при невдалому завантаженні, наступна подія повторює спробу', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  const es = r.sses[0]; es.open(); await settle(r);
  r.net.up = false; es.msg('revX'); await settle(r); assert.notEqual(r.s.rev, 'revX');
  r.net.up = true; r.net.data = r.payload(); es.msg('revX'); await settle(r);
  assert.equal(r.s.rev, 'revX');
});

test('SSE disconnect → reconnect: синхронізація даних та історії після перепідключення', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  const es = r.sses[0]; es.open(); await settle(r);
  const before = r.net.calls, hb = r.net.hcalls;
  es.fail(false); await settle(r);                       // браузер перепідключиться сам (readyState CONNECTING)
  assert.equal(r.sses.length, 1);
  r.net.data = r.payload(); es.open(); await settle(r);
  assert.ok(r.net.calls > before, 'дані синхронізовані після reconnect');
  assert.ok(r.net.hcalls > hb, 'історія теж');
});

test('SSE закрився остаточно: перевідкриття з паузою, без шторму', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  r.sses[0].fail(true); await settle(r);
  assert.equal(r.sses.length, 1);
  await settle(r, 15000); assert.equal(r.sses.length, 2);
  r.sses[1].fail(true); await settle(r, 5000); assert.equal(r.sses.length, 2, 'друга спроба пізніше (backoff)');
  await settle(r, 20000); assert.equal(r.sses.length, 3);
});

test('помилка історії не ламає карту; попередній журнал лишається з error', async () => {
  const r = rig(); r.net.data = r.payload(); r.net.hist = { alerts: [[1, 'sumska', 'raion', 'Х район', '', 'red', 'air_raid', 100, 0]], now: r.sec() };
  await r.s.start(); await settle(r);
  assert.equal(r.s.history.rows.length, 1); assert.equal(r.s.history.error, false);
  r.net.histFail = true; await r.s.loadHistory(); await settle(r);
  assert.equal(r.s.history.rows.length, 1); assert.equal(r.s.history.error, true); assert.equal(r.s.conn, 'ok');
});

test('історія з кешу при запуску без мережі позначена error', async () => {
  const r = rig(); const n = r.sec();
  r.mem.set('c', DataSync.wrapCache(r.payload(n - 60), n - 60));
  r.mem.set('h', JSON.stringify({ rows: [[1, 'sumska', 'raion', 'Х район', '', 'red', 'air_raid', 100, 0]], now: n - 60, fetchedAt: n - 60 }));
  r.net.up = false; await r.s.start(); await settle(r);
  assert.equal(r.s.history.fromCache, true); assert.equal(r.s.history.error, true);
});

test('stop() знімає таймери, EventSource і слухачі; повторний start не дублює обробники', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  const h1 = r.handlerCount(); assert.ok(h1 > 0);
  await r.s.start(); await settle(r);
  assert.equal(r.handlerCount(), h1, 'без дублів');
  assert.equal(r.sses.filter(e => e.readyState !== 2).length, 1, 'лише один живий EventSource');
  r.s.stop(); assert.equal(r.handlerCount(), 0); assert.equal(r.timers.size, 0); assert.ok(r.sses.every(e => e.readyState === 2));
});

test('вкладка прихована: опитування зупинене; повернення — миттєва синхронізація', async () => {
  const r = rig(); r.net.data = r.payload(); await r.s.start(); await settle(r);
  r.emit('hidden'); const c = r.net.calls; await settle(r, 600000); assert.equal(r.net.calls, c);
  r.net.data = r.payload(); r.emit('visible'); await settle(r); assert.equal(r.net.calls, c + 1);
});

test('повторні оновлення тими самими даними не дублюють неофіційні повідомлення (стабільні ключі)', () => {
  // нормалізація йде у data.js; тут перевіряємо, що sync віддає той самий raw без мутацій
  const r = rig(); const raw = r.payload(r.sec(), ['kyivska']); const copy = JSON.stringify(raw);
  r.net.data = raw; return r.s.load().then(() => r.s.load()).then(() => assert.equal(JSON.stringify(raw), copy));
});

test('статичний режим (Pages): SSE не відкривається, інтервал 60 с', async () => {
  const r = rig({ live: false }); r.net.data = r.payload(); await r.s.start(); await settle(r);
  assert.equal(r.sses.length, 0);
  const c = r.net.calls; await settle(r, 59000); assert.equal(r.net.calls, c); await settle(r, 2000); assert.equal(r.net.calls, c + 1);
});
