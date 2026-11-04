'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('fs'), path = require('path');
const T = require('../tracks.js');
const NOW = 1_800_000_000;
const rec = (o) => Object.assign({ id: 'a', weapon: 'shahed', status: 'active', count: 3, speed_kmh: 180, speed_source: 'measured', heading: 200, heading_text: 'ПдЗх', sources: 2,
  now: { lat: 50.2, lon: 34.1, estimated: false }, points: [{ ts: NOW - 600, lat: 50.6, lon: 34.4, count: 3 }, { ts: NOW - 120, lat: 50.2, lon: 34.1, count: 3 }],
  forecast: [{ name: 'Полтава', lat: 49.59, lon: 34.55, eta_min: 40, km: 120 }], dest: null }, o);

test('валідний трек нормалізується; прогноз і ETA-діапазон на місці', () => {
  const r = T.normalize({ now: NOW, tracks: [rec()] }, NOW), t = r.tracks[0];
  assert.equal(r.ok, true); assert.equal(r.tracks.length, 1);
  assert.equal(t.stale, false); assert.equal(t.speedSource, 'measured'); assert.equal(t.heading, 200);
  assert.deepEqual(t.forecast[0].eta, { lo: 32, hi: 48 });             // ±20% для виміряної
  assert.equal(T.fmtEta(t.forecast[0].eta), '≈ 32–48 хв');
});
test('типова швидкість: ширший діапазон і явна позначка «не виміряна»', () => {
  const t = T.normalize({ now: NOW, tracks: [rec({ speed_source: 'typical' })] }, NOW).tracks[0];
  assert.deepEqual(t.forecast[0].eta, { lo: 24, hi: 56 });
  assert.match(T.speedText(t), /типова, не виміряна/);
});
test('застарілий трек (25+ хв) сірий і без прогнозу', () => {
  const old = rec({ points: [{ ts: NOW - 40 * 60, lat: 50.2, lon: 34.1 }] });
  const t = T.normalize({ now: NOW, tracks: [old] }, NOW).tracks[0];
  assert.equal(t.stale, true); assert.deepEqual(t.forecast, []); assert.equal(t.dest, null);
  assert.equal(T.normalize({ now: NOW, tracks: [rec({ status: 'stale' })] }, NOW).tracks[0].stale, true);
});
test('без часу точок свіжість невідома: трек вважається застарілим', () => {
  const t = T.normalize({ now: NOW, tracks: [rec({ points: [] })] }, NOW).tracks[0];
  assert.equal(t.stale, true); assert.equal(T.fmtAge(null), 'час невідомий');
});
test('увесь файл давно не оновлювався: усі цілі застарілі', () => {
  const r = T.normalize({ now: NOW - 30 * 60, tracks: [rec({ points: [{ ts: NOW - 30 * 60, lat: 50.2, lon: 34.1 }] })] }, NOW);
  assert.equal(r.feedStale, true); assert.equal(r.tracks[0].stale, true);
});
test('сміття відкидається, карта не падає: невідомий тип, координати поза межами, не масив, null', () => {
  assert.equal(T.normalize(null, NOW).ok, false); assert.equal(T.normalize({ tracks: 'x' }, NOW).ok, false); assert.equal(T.normalize('x', NOW).tracks.length, 0);
  const r = T.normalize({ now: NOW, tracks: [null, 5, rec({ weapon: 'ufo' }), rec({ id: 'b', now: { lat: 10, lon: 10 } }), rec({ id: 'c', now: { lat: 'x', lon: 34 } }), rec({ id: 'ok' })] }, NOW);
  assert.equal(r.tracks.length, 1); assert.equal(r.tracks[0].id, 'ok'); assert.equal(r.rejected, 5);
});
test('поля: від’ємна/абсурдна швидкість, поганий курс, eta поза межами, порожнє ім’я, html у назві', () => {
  const t = T.normalize({ now: NOW, tracks: [rec({ speed_kmh: -5, heading: 725, count: 0, sources: 0,
    forecast: [{ name: '', lat: 49.6, lon: 34.5, eta_min: 10 }, { name: 'X', lat: 49.6, lon: 34.5, eta_min: 9999 }, { name: 'Ок<b>', lat: 49.6, lon: 34.5, eta_min: -1 }, { name: 'Суми<script>', lat: 50.9, lon: 34.8, eta_min: 20 }] })] }, NOW).tracks[0];
  assert.equal(t.speedKmh, null); assert.equal(t.speedSource, 'typical'); assert.equal(t.heading, 5); assert.equal(t.count, 1); assert.equal(t.sources, 1);
  assert.equal(t.forecast.length, 1); assert.ok(!/[<>]/.test(t.forecast[0].name));
});
test('дублі id: лишається найсвіжіший; ліміт кількості', () => {
  const a = rec({ id: 'x', points: [{ ts: NOW - 900, lat: 50.2, lon: 34.1 }] }), b = rec({ id: 'x', points: [{ ts: NOW - 60, lat: 50.3, lon: 34.1 }] });
  const r = T.normalize({ now: NOW, tracks: [a, b] }, NOW); assert.equal(r.tracks.length, 1); assert.equal(r.tracks[0].lastTs, NOW - 60);
  const many = Array.from({ length: 500 }, (_, i) => rec({ id: 'm' + i }));
  assert.ok(T.normalize({ now: NOW, tracks: many }, NOW).tracks.length <= T.MAX_TRACKS);
});
test('узагальнений режим сервера: точних позицій немає — позначка generalized, а не помилка', () => {
  const r = T.normalize({ now: NOW, public: true, tracks: [{ id: 'z', region: 'poltavska' }] }, NOW);
  assert.equal(r.generalized, true); assert.equal(r.tracks.length, 0);
});
test('фільтр типів; підписи; довіра', () => {
  const r = T.normalize({ now: NOW, tracks: [rec({ id: '1' }), rec({ id: '2', weapon: 'ballistic' }), rec({ id: '3', weapon: 'uav' })] }, NOW);
  assert.equal(T.filterKinds(r.tracks, ['ballistic']).length, 1); assert.equal(T.filterKinds(r.tracks, []).length, 3); assert.equal(T.filterKinds(r.tracks, ['shahed']).length, 2);
  const t = r.tracks[0]; assert.match(T.trustText(t), /2 незалежні/); assert.match(T.headingText(t), /200°/);
  assert.equal(T.fmtAge(30), 'щойно'); assert.equal(T.fmtAge(600), '10 хв тому');
});
test('демо-потік вигаданий, проходить власну нормалізацію, має свіжі, застарілі і типові цілі', () => {
  const r = T.normalize(T.demoPayload(NOW), NOW);
  assert.ok(r.tracks.length >= 6 && r.rejected === 0);
  assert.ok(r.tracks.some(t => t.stale) && r.tracks.some(t => !t.stale) && r.tracks.some(t => t.speedSource === 'typical'));
  assert.ok(r.tracks.some(t => t.forecast.length));
  assert.ok(r.tracks.every(t => /^demo:/.test(t.id)));
});
test('модуль не містить приватних полів і ендпоінтів; прогноз не екстраполюється між оновленнями', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'tracks.js'), 'utf8') + fs.readFileSync(path.join(__dirname, '..', 'layers.js'), 'utf8');
  assert.ok(!/admin|\/api\/private|chat_id|user_id|token/i.test(src));
  assert.ok(!/requestAnimationFrame|setInterval/.test(fs.readFileSync(path.join(__dirname, '..', 'layers.js'), 'utf8')), 'у шарі цілей немає анімації руху');
});

test('nearby: у колі / курсом на точку / далеко і застарілі ігноруються', () => {
  const me = [50.45, 30.52], mk = (id, lat, lon, heading, extra) => Object.assign({ id, lat, lon, heading, stale: false, forecast: [], count: 1 }, extra);
  const inside = mk('a', 50.5, 30.6, null), toMe = mk('b', 50.8, 30.52, 180), away = mk('c', 50.8, 30.52, 0), far = mk('d', 47, 35, 270), old = mk('e', 50.46, 30.53, 0, { stale: true });
  const r = T.nearby([far, away, toMe, inside, old], me, 25);
  assert.deepEqual(r.map(x => x.t.id), ['a', 'b']);            // відсортовано за відстанню; far/away/stale не потрапили
  assert.equal(r[0].inside, true); assert.equal(r[1].inside, false); assert.equal(r[1].toMe, true);
  assert.ok(r[0].km < 10 && r[1].km > 25 && r[1].km <= 50);
  assert.equal(r[1].side, 'Пн');
});
test('nearby: точка прогнозу поблизу дає ETA навіть без курсу', () => {
  const me = [50.45, 30.52], t = { id: 'f', lat: 51.0, lon: 31.5, heading: null, stale: false, count: 2, forecast: [{ lat: 50.46, lon: 30.55, etaMin: 12 }] };
  const r = T.nearby([t], me, 25); assert.equal(r.length, 1); assert.equal(r[0].etaMin, 12); assert.equal(r[0].toMe, true);
});

test('probability: якісні рівні за відстанню і курсом', () => {
  assert.equal(T.probability({ km: 2, toMe: false, inside: true }), 'дуже висока');
  assert.equal(T.probability({ km: 10, toMe: true, inside: true }), 'висока');
  assert.equal(T.probability({ km: 40, toMe: true, inside: false }), 'середня');
  assert.equal(T.probability({ km: 10, toMe: false, inside: true }), 'низька–середня');
});

test('demoAttack: цілі з\'являються за фазами, летять і зникають; координати в межах України', () => {
  const at = m => T.demoAttack(NOW, m);
  const ids = p => p.tracks.map(t => t.id.replace('demo:', '')).sort();
  assert.deepEqual(ids(at(1)), ['a1']);                         // спершу лише шахеди
  assert.ok(ids(at(20)).includes('a2') && !ids(at(20)).some(i => i[0] === 'c'));
  assert.ok(ids(at(40)).includes('c1'));                         // фаза 2
  assert.ok(ids(at(63)).includes('b1') || ids(at(70)).includes('b1'));   // фаза 3
  assert.deepEqual(ids(at(99)), []);                             // кінець: усе долетіло/зникло
  const p1 = at(10).tracks.find(t => t.id === 'demo:a1'), p2 = at(20).tracks.find(t => t.id === 'demo:a1');
  assert.ok(T.dist([p1.now.lat, p1.now.lon], [p2.now.lat, p2.now.lon]) > 20, 'ціль рухається');
  assert.equal(at(40).attack.phase, 'Фаза 2 · крилаті ракети');
  at(40).tracks.forEach(t => { assert.ok(t.now.lat > 43 && t.now.lat < 54.5 && t.now.lon > 21 && t.now.lon < 41.5); });
});
