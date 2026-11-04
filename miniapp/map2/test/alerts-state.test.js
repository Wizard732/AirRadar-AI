'use strict';
// node --test miniapp/map2/test  (запускається з test_map2_alerts_state.py)
const test = require('node:test');
const assert = require('node:assert/strict');
const AS = require('../alerts-state.js');

// мінімальне оточення для data.js (браузерний IIFE)
global.window = global; global.document = { baseURI: 'http://localhost/map2/index.html', addEventListener() {} };
global.location = { hostname: 'localhost' }; global.addEventListener = () => {};
require('../data.js');

const row = (id, slug, type, title, started, ended, kind = 'air_raid', level = 'red') => [id, slug, type, title, '', level, kind, started, ended];
const T0 = 1_800_000_000;

test('ключ тривоги: alert:{slug}:{район|*}:{початок}', () => {
  assert.equal(AS.alertKey('kharkivska', 'Ізюмський', T0), 'alert:kharkivska:Ізюмський:' + T0);
  const m = AS.buildAlerts([row(1, 'kharkivska', 'raion', 'Ізюмський район', T0, 0), row(2, 'sumska', 'oblast', 'Сумська область', T0, 0)]);
  assert.deepEqual([...m.keys()].sort(), ['alert:kharkivska:Ізюмський:' + T0, 'alert:sumska:*:' + T0]);
});

test('повторне застосування тих самих рядків не створює дублів (ідемпотентність)', () => {
  const rows = [row(1, 'sumska', 'raion', 'Охтирський район', T0, 0)];
  const a = AS.buildAlerts(rows.concat(rows, rows));
  assert.equal(a.size, 1);
  assert.equal(AS.buildEpisodes(a).length, 1);
  assert.equal(AS.signature(a), AS.signature(AS.buildAlerts(rows)));
});

test('той самий ключ: пізніший рядок із кінцем перемагає відкритий', () => {
  const a = AS.buildAlerts([row(1, 'sumska', 'raion', 'Охтирський район', T0, 0), row(1, 'sumska', 'raion', 'Охтирський район', T0, T0 + 600)]);
  assert.equal([...a.values()][0].ended, T0 + 600);
});

test('антидребезг: пауза ≤120 с склеює епізод, кінець не зсувається, пауза не входить у тривалість', () => {
  const a = AS.buildAlerts([row(1, 'sumska', 'raion', 'Конотопський район', T0, T0 + 400), row(2, 'sumska', 'raion', 'Конотопський район', T0 + 440, T0 + 1000)]);
  const eps = AS.buildEpisodes(a);
  assert.equal(eps.length, 1);
  assert.equal(eps[0].started, T0); assert.equal(eps[0].ended, T0 + 1000);
  assert.equal(eps[0].activeSeconds, 400 + 560);          // 960, а не 1000: пауза 40 с не рахується
  assert.equal(eps[0].parts, 2); assert.equal(eps[0].keys.length, 2);
});

test('пауза >120 с: окремі епізоди', () => {
  const a = AS.buildAlerts([row(1, 'sumska', 'raion', 'Конотопський район', T0, T0 + 400), row(2, 'sumska', 'raion', 'Конотопський район', T0 + 400 + 121, T0 + 1000)]);
  assert.equal(AS.buildEpisodes(a).length, 2);
});

test('різні території не склеюються', () => {
  const a = AS.buildAlerts([row(1, 'sumska', 'raion', 'Конотопський район', T0, T0 + 400), row(2, 'sumska', 'raion', 'Охтирський район', T0 + 410, T0 + 900)]);
  assert.equal(AS.buildEpisodes(a).length, 2);
});

test('обстріли та бої (не air_raid) не потрапляють у стан тривог', () => {
  const a = AS.buildAlerts([row(1, 'sumska', 'raion', 'Охтирський район', T0, 0, 'artillery_shelling'), row(2, 'sumska', 'raion', 'Конотопський район', T0, 0, 'urban_fights')]);
  assert.equal(a.size, 0);
});

test('сміттєві рядки ігноруються', () => {
  assert.equal(AS.buildAlerts([null, [], [1, '', 'raion', 'x', '', 'red', 'air_raid', T0, 0], [1, 's', 'raion', 'x', '', 'red', 'air_raid', 0, 0]]).size, 0);
});

const dist = { slug: 'sumska', name: 'Охтирський', city: false };
test('стан no_data / stale мають пріоритет над історією', () => {
  assert.equal(AS.stateFor({ hasOfficial: false, stale: false, active: 'alert', episodes: [], now: T0 }).state, 'no_data');
  assert.equal(AS.stateFor({ hasOfficial: true, stale: true, active: 'alert', episodes: [], now: T0 }).state, 'stale');
});

test('active: час початку і тривалість лише за відкритим епізодом; без нього час не вигадується', () => {
  const eps = AS.buildEpisodes(AS.buildAlerts([row(1, 'sumska', 'raion', 'Охтирський район', T0, 0)]));
  const s = AS.stateFor({ hasOfficial: true, stale: false, active: 'alert', episodes: AS.episodesFor(eps, dist), now: T0 + 900 });
  assert.equal(s.state, 'active'); assert.equal(s.since, T0); assert.equal(s.duration, 900);
  const none = AS.stateFor({ hasOfficial: true, stale: false, active: 'alert', episodes: [], now: T0 + 900 });
  assert.equal(none.state, 'active'); assert.equal(none.since, null); assert.equal(none.duration, null);
});

test('active береться тільки з поточного контракту: відкритий рядок історії без активності в контракті = cleared', () => {
  const eps = AS.buildEpisodes(AS.buildAlerts([row(1, 'sumska', 'raion', 'Охтирський район', T0, 0)]));
  const s = AS.stateFor({ hasOfficial: true, stale: false, active: null, episodes: AS.episodesFor(eps, dist), now: T0 + 900 });
  assert.equal(s.state, 'cleared'); assert.equal(s.lastEnded, null);
});

test('cleared: останній відбій і тривалість з історії', () => {
  const eps = AS.buildEpisodes(AS.buildAlerts([row(1, 'sumska', 'raion', 'Охтирський район', T0, T0 + 600)]));
  const s = AS.stateFor({ hasOfficial: true, stale: false, active: null, episodes: AS.episodesFor(eps, dist), now: T0 + 900 });
  assert.equal(s.state, 'cleared'); assert.equal(s.lastEnded, T0 + 600); assert.equal(s.lastDuration, 600);
});

test('відкритий склеєний епізод: тривалість = закриті частини + поточна', () => {
  const eps = AS.buildEpisodes(AS.buildAlerts([row(1, 'sumska', 'raion', 'Охтирський район', T0, T0 + 300), row(2, 'sumska', 'raion', 'Охтирський район', T0 + 330, 0)]));
  const s = AS.stateFor({ hasOfficial: true, stale: false, active: 'alert', episodes: AS.episodesFor(eps, dist), now: T0 + 630 });
  assert.equal(s.since, T0); assert.equal(s.duration, 300 + 300);
});

test('тривога всієї області покриває район, чужий slug ні', () => {
  const eps = AS.buildEpisodes(AS.buildAlerts([row(1, 'sumska', 'oblast', 'Сумська область', T0, 0), row(2, 'poltavska', 'oblast', 'Полтавська область', T0, 0)]));
  assert.equal(AS.episodesFor(eps, dist).length, 1);
});

test('Київ і Севастополь: тривога приходить як slug kyivska/crimea (sirens.py) і підсвічує міські території', () => {
  const d = AirData.normalize({ updated_at: 100, official: { sirens: ['kyivska', 'crimea'], sirens_raions: [], sirens_raions_y: [] }, regions: [] }, 100);
  assert.ok(d.official.oblasts.has('Київ')); assert.ok(d.official.oblasts.has('Севастополь'));
  const st = AirData.toStatuses(d, ['Київ|Київ', 'Севастополь|Севастополь', 'Сумський|Сумська']);
  assert.equal(st.get('Київ|Київ').official, 'alert'); assert.equal(st.get('Севастополь|Севастополь').official, 'alert'); assert.equal(st.has('Сумський|Сумська'), false);
  assert.equal(AirData.districtMeta('Київ|Київ', { r: 'Київ', o: 'Київ' }).slug, 'kyivska');
  assert.equal(AirData.districtMeta('Ялтинський|Крим', { r: 'Ялтинський', o: 'Автономна Республіка Крим' }).slug, 'crimea');
});

test('контракт без sirens = no_data; координати та вектор у нормалізований вигляд не потрапляють', () => {
  const raw = { updated_at: 100, official: { sirens: null }, regions: [{ region: 'sumska', raion: 'Охтирський', category: 'shahed', status: 'tracking', count_bucket: '2-5', lat: 50.1, lon: 30.1, heading: 90, speed_kmh: 180 }] };
  const d = AirData.normalize(raw, 100);
  assert.equal(d.hasOfficial, false);
  const dump = JSON.stringify([...d.hints.values()]);
  for (const k of ['lat', 'lon', 'heading', 'speed', 'eta', 'forecast']) assert.ok(!dump.includes(k), k + ' не повинно бути в клієнтській моделі');
});

test('неофіційне повідомлення має окремий ключ hint:, не alert:', () => {
  const d = AirData.normalize({ updated_at: 100, official: { sirens: [] }, regions: [{ region: 'sumska', raion: 'Охтирський', category: 'shahed', count_bucket: '1' }] }, 100);
  const h = [...d.hints.values()][0];
  assert.ok(h.key.startsWith('hint:')); assert.ok(!h.key.startsWith('alert:'));
});
