'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const S = require('../shelters.js'), W = require('../watch.js'), T = require('../stats.js'), U = require('../ui-state.js'), A = require('../alerts-state.js');

// ---------- укриття ----------
test('shelters.json: розбір, усі точки в межах України, є джерело', () => {
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'shelters.json'), 'utf8'));
  assert.match(raw.src, /OpenStreetMap/);
  const list = S.parse(raw); assert.ok(list.length > 1000 && list.length === raw.rows.length);
  assert.ok(list.filter(x => x.verified).length > 30, 'є точки з назвою');
  assert.ok(!list.some(x => /водосхов|зерносхов|книг|двт|\bдот\b/i.test(x.name)), 'шум OSM відфільтровано');
  assert.ok(list.every(s => s.lat > 43.5 && s.lat < 53 && s.lon > 21 && s.lon < 41));
});
test('shelters.parse відкидає сміття і точки поза Україною', () => {
  assert.deepEqual(S.parse(null), []);
  const l = S.parse({ rows: [[50, 30, 0, 'a', ''], [0, 0, 0], 'x', [50, 'z', 0], [60, 30, 0], [50, 30, 1, 'b']] });
  assert.equal(l.length, 2); assert.equal(l[1].kind, 'Точка без назви'); assert.equal(l[1].verified, false); assert.equal(l[0].verified, true);
});
test('shelters.nearest: порядок за відстанню, ліміт, пішохідний час', () => {
  const l = [{ lat: 50.001, lon: 30, kind: 'Укриття', name: 'A', street: '' }, { lat: 50.02, lon: 30, kind: 'Укриття', name: 'B', street: '' }, { lat: 50.0005, lon: 30, kind: 'Укриття', name: 'C', street: '' }];
  const r = S.nearest(l, 50, 30, 2, 5000);
  assert.deepEqual(r.map(x => x.name), ['C', 'A']);
  assert.ok(r[0].distM < 80 && r[0].walkMin === 1);
  assert.equal(S.nearest(l, 50, 30, 3, 100).length, 1);
  assert.equal(S.nearest([], 50, 30).length, 0);
});
test('маршрут містить лише точку призначення', () => {
  const u = S.routeUrl({ lat: 50.123456, lon: 30.654321 });
  assert.match(u, /destination=50\.12346,30\.65432$/); assert.ok(!/origin/.test(u));
});
test('форматування відстані', () => { assert.equal(S.fmtDist(240), '240 м'); assert.equal(S.fmtDist(1530), '1,5 км'); });

// ---------- обрані і сповіщення ----------
test('обрані: нормалізація, ліміт, дублі, невідомі', () => {
  const known = new Set(['A|X', 'B|X']);
  assert.deepEqual(W.normalizeFav(['A|X', 'A|X', 'Z|Q', 5, null], known), ['A|X']);
  assert.deepEqual(W.normalizeFav('bad', known), []);
  let l = []; for (let i = 0; i < W.MAX_FAV; i++) l = W.toggleFav(l, 'id' + i).list;
  const r = W.toggleFav(l, 'extra'); assert.equal(r.full, true); assert.equal(r.list.length, W.MAX_FAV);
  assert.deepEqual(W.toggleFav(['a', 'b'], 'a'), { list: ['b'], added: false });
});
test('watchedIds: головна + обрані без дублів', () => { assert.deepEqual(W.watchedIds('a', ['b', 'a']), ['a', 'b']); assert.deepEqual(W.watchedIds(null, []), []); });
test('сповіщення: старт, загроза, відбій, пониження', () => {
  const s = (o, ok = true) => ({ ok, s: o });
  assert.deepEqual(W.diff(s({ a: null }), s({ a: 'alert' })), [{ id: 'a', kind: 'start' }]);
  assert.deepEqual(W.diff(s({ a: null }), s({ a: 'warn' })), [{ id: 'a', kind: 'warn' }]);
  assert.deepEqual(W.diff(s({ a: 'alert' }), s({ a: null })), [{ id: 'a', kind: 'end' }]);
  assert.deepEqual(W.diff(s({ a: 'warn' }), s({ a: 'alert' })), [{ id: 'a', kind: 'start' }]);
  assert.deepEqual(W.diff(s({ a: 'alert' }), s({ a: 'warn' })), [{ id: 'a', kind: 'downgrade' }]);
  assert.deepEqual(W.diff(s({ a: 'alert' }), s({ a: 'alert' })), []);
});
test('сповіщення: перший знімок, недостовірні дані і нова територія подій не дають', () => {
  const s = (o, ok = true) => ({ ok, s: o });
  assert.deepEqual(W.diff(null, s({ a: 'alert' })), []);
  assert.deepEqual(W.diff(s({ a: 'alert' }, false), s({ a: null })), [], 'не було достовірних даних: відбою не оголошуємо');
  assert.deepEqual(W.diff(s({ a: 'alert' }), s({ a: null }, false)), [], 'дані стали недостовірними: це не відбій');
  assert.deepEqual(W.diff(s({ a: null }, false), s({ a: 'alert' })), [{ id: 'a', kind: 'start' }], 'після застарілих даних про початок повідомляємо');
  assert.deepEqual(W.diff(s({ a: null }), s({ a: null, b: 'alert' })), [], 'щойно додана територія');
});
test('snapshot і текст повідомлення; налаштування сповіщень типово вимкнені', () => {
  const sn = W.snapshot(['a', 'b'], id => (id === 'a' ? 'alert' : undefined), true); assert.deepEqual(sn, { ok: true, s: { a: 'alert', b: null } });
  assert.equal(W.message({ kind: 'end' }, 'Ізюмський'), 'Відбій повітряної тривоги: Ізюмський');
  assert.equal(W.summary([{ kind: 'end', name: 'А' }, { kind: 'start', name: 'Б' }, { kind: 'start', name: 'В' }]), 'Повітряна тривога: Б, В · Відбій повітряної тривоги: А');
  assert.equal(W.summary([]), '');
  assert.deepEqual(W.normalizeNotify(undefined), { on: false, sound: true, system: false });
  assert.equal(W.normalizeNotify({ on: 'yes' }).on, false);
});

// ---------- статистика і таймлайн ----------
const mk = (slug, name, started, ended) => ({ territory: slug + '|raion|' + name, slug, scope: 'raion', name, started, ended, activeSeconds: ended ? ended - started : 0, parts: 1, keys: [], level: 'red', last: { started } });
test('статистика: підрахунок за вікном, відкритий епізод до «зараз», межа покриття', () => {
  const now = 1_700_000_000;
  const eps = [mk('sumska', 'A', now - 3600, now - 1800), mk('sumska', 'B', now - 7200, now - 6000), mk('kharkivska', 'C', now - 600, null), mk('odeska', 'D', now - 90000, now - 89000)];
  const r = T.summarize(eps, now, 24 * 3600, s => s);
  assert.equal(r.count, 3); assert.equal(r.territories, 3);
  assert.equal(r.totalSec, 1800 + 1200 + 600);
  assert.equal(r.top[0].slug, 'sumska'); assert.equal(r.top[0].count, 2);
  assert.equal(r.coverageFrom, now - 90000); assert.equal(r.hours.reduce((a, b) => a + b, 0), 3);
  assert.equal(T.summarize([], now, 3600).peakHour, null);
});
test('статистика: епізод, що почався до вікна, рахується лише в межах вікна', () => {
  const now = 100000; const e = mk('x', 'Y', now - 5000, now - 1000);
  assert.equal(T.overlap(e, now - 2000, now), 1000);
});
test('таймлайн: activeAt і діапазон не раніше покриття журналу', () => {
  const now = 1_700_000_000, a = mk('s', 'A', now - 3000, now - 1000), b = mk('s', 'B', now - 500, null);
  assert.deepEqual(T.activeAt([a, b], now - 2000).map(e => e.name), ['A']);
  assert.deepEqual(T.activeAt([a, b], now - 1000).map(e => e.name), [], 'кінець виключний');
  assert.deepEqual(T.activeAt([a, b], now).map(e => e.name), ['B']);
  const r = T.timelineRange([a, b], now, 24 * 3600, 300); assert.ok(r.from >= now - 3300 && r.from <= now - 3000 && r.covered);
  assert.equal(T.timelineRange([], now, 3600, 300).covered, false);
});
test('таймлайн сумісний з alerts-state (journal → епізоди → стан у момент t)', () => {
  const now = 1_700_000_000;
  const eps = A.buildEpisodes(A.buildAlerts([[1, 'sumska', 'raion', 'Конотопський район', '', 'red', 'air_raid', now - 3000, now - 1000]]));
  assert.equal(T.activeAt(eps, now - 2000).length, 1); assert.equal(T.activeAt(eps, now).length, 0);
});

// ---------- налаштування і посилання ----------
test('налаштування вигляду: типові значення і відкидання сміття', () => {
  assert.deepEqual(U.normalizePrefs(null), { theme: 'dark', size: 'm' });
  assert.deepEqual(U.normalizePrefs({ theme: 'x', size: 'xl' }), { theme: 'dark', size: 'xl' });
});
test('посилання на територію: кодування, розбір, невідомий і зіпсований параметр', () => {
  const known = new Set(['Ізюмський|Харківська']);
  const url = U.linkFor('https://air.example/miniapp/map2/', 'Ізюмський|Харківська');
  const search = '?' + url.split('?')[1];
  assert.equal(U.districtFromSearch(search, known), 'Ізюмський|Харківська');
  assert.equal(U.districtFromSearch('?d=Невідомий|Х', known), null);
  assert.equal(U.districtFromSearch('?d=%E0%A4%A', known), null);
  assert.equal(U.districtFromSearch('', known), null);
  assert.equal(U.hasDistrictParam('?d=x'), true);
  assert.equal(U.withDistrict('?demo=1&d=a', null), '?demo=1'); assert.equal(U.withDistrict('', 'A|B'), '?d=A%7CB');
});
test('нові модулі не містять приватних полів і службових адрес', () => {
  ['shelters.js', 'watch.js', 'stats.js', 'ui-state.js'].forEach(f => {
    const src = fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
    assert.ok(!/\/api\/|\/admin|bot_token|initData|speed|heading|\beta\b/i.test(src.replace(/\/\*[\s\S]*?\*\//g, '')), f);
  });
});
