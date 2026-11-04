'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('fs'), path = require('path'), vm = require('vm');
const AS = require('../alerts-state.js'), F = require('../filters.js');
// data.js — браузерний скрипт: виконуємо в sandbox із мінімальними заглушками
const sb = { console, URL, location: { hostname: 'localhost' }, document: { baseURI: 'http://x/map2/' }, DataSync: function () {} };
sb.window = sb; vm.createContext(sb); vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'data.js'), 'utf8'), sb);
const AirData = sb.AirData, N = 1_700_000_000;
const norm = (raw, now) => AirData.normalize(raw, now == null ? N : now);
const ok = o => Object.assign({ updated_at: N - 10, delay_min: 0, official: { sirens: [], sirens_raions: [], sirens_raions_y: [] }, regions: [] }, o);

test('«тривог немає» (sirens=[]) і «немає даних» (sirens відсутні/null) розрізняються', () => {
  assert.equal(norm(ok({})).hasOfficial, true);
  assert.equal(norm(ok({ official: { sirens: null } })).hasOfficial, false);
  assert.equal(norm(ok({ official: {} })).hasOfficial, false);
  assert.equal(norm(ok({ official: undefined })).hasOfficial, false);
  assert.equal(norm({}).hasOfficial, false);
});
test('пошкоджені/часткові відповіді не кидають винятків', () => {
  [null, undefined, 0, 'text', [], { regions: 'x' }, { regions: [null, 1, 'a', {}] }, { official: 'x' }, { official: { sirens: [1, null, {}], sirens_raions: 'x' } }, { updated_at: 'abc' }].forEach(raw => {
    assert.doesNotThrow(() => norm(raw), JSON.stringify(raw));
  });
});
test('відсутній updated_at = свіжість невідома = stale, а не «свіжі»', () => {
  assert.equal(norm({ official: { sirens: [] } }).stale, true);
  assert.equal(norm(ok({ updated_at: 0 })).stale, true);
});
test('stale → fresh за updated_at і delay_min', () => {
  assert.equal(norm(ok({ updated_at: N - 14 * 60 })).stale, false);
  assert.equal(norm(ok({ updated_at: N - 16 * 60 })).stale, true);
  assert.equal(norm(ok({ updated_at: N - 20 * 60, delay_min: 10 })).stale, false, 'затримка джерела враховується');
  assert.equal(norm(ok({ updated_at: N - 20 * 60, delay_min: 1 })).stale, true);
});
test('район зник з відповіді: статусу немає, нічого не «залипає»', () => {
  const ids = ['Ізюмський|Харківська', 'Конотопський|Сумська'];
  const a = AirData.toStatuses(norm(ok({ official: { sirens: [], sirens_raions: ['Ізюмський|Харківська', 'Конотопський|Сумська'] } })), ids);
  assert.equal(a.size, 2);
  const b = AirData.toStatuses(norm(ok({ official: { sirens: [], sirens_raions: ['Конотопський|Сумська'] } })), ids);
  assert.equal(b.size, 1); assert.ok(!b.has('Ізюмський|Харківська'));
});
test('повторне застосування однієї відповіді дає ідентичний результат і не дублює повідомлення', () => {
  const raw = ok({ regions: [{ region: 'poltavska', raion: 'Кременчуцький', category: 'shahed', last_update: N - 5 }, { region: 'poltavska', raion: 'Кременчуцький', category: 'shahed', last_update: N - 5 }] });
  const a = norm(raw), b = norm(raw);
  assert.equal(a.hints.size, 1); assert.deepEqual([...a.hints.keys()], [...b.hints.keys()]);
});
test('Київ/Севастополь додаються лише при тривозі kyivska/crimea', () => {
  assert.ok(norm(ok({ official: { sirens: ['kyivska'] } })).official.oblasts.has('Київ'));
  assert.ok(!norm(ok({ official: { sirens: ['lvivska'] } })).official.oblasts.has('Київ'));
});
test('жовтий рівень не перекриває червоний для того ж району', () => {
  const d = norm(ok({ official: { sirens: [], sirens_raions: ['А|Сумська'], sirens_raions_y: ['А|Сумська', 'Б|Сумська'] } }));
  assert.equal(d.official.raions.get('А|Сумська'), 'alert'); assert.equal(d.official.raions.get('Б|Сумська'), 'warn');
});
test('цикл active → cleared → active: два епізоди, активний стан лише з поточного контракту', () => {
  const T = 1_700_000_000, row = (s, e) => [1, 'sumska', 'raion', 'Конотопський район', '', 'red', 'air_raid', s, e];
  const meta = { slug: 'sumska', name: 'Конотопський', city: false };
  // 1) активна
  let eps = AS.buildEpisodes(AS.buildAlerts([row(T, 0)]));
  let st = AS.stateFor({ hasOfficial: true, stale: false, active: 'alert', episodes: AS.episodesFor(eps, meta), now: T + 600 });
  assert.equal(st.state, 'active'); assert.equal(st.since, T); assert.equal(st.duration, 600);
  // 2) відбій
  eps = AS.buildEpisodes(AS.buildAlerts([row(T, T + 900)]));
  st = AS.stateFor({ hasOfficial: true, stale: false, active: null, episodes: AS.episodesFor(eps, meta), now: T + 1000 });
  assert.equal(st.state, 'cleared'); assert.equal(st.lastEnded, T + 900); assert.equal(st.lastDuration, 900);
  // 3) нова тривога через >120 с: окремий епізод, тривалість нового рахується з нового початку
  eps = AS.buildEpisodes(AS.buildAlerts([row(T, T + 900), row(T + 2000, 0)]));
  assert.equal(eps.length, 2);
  st = AS.stateFor({ hasOfficial: true, stale: false, active: 'alert', episodes: AS.episodesFor(eps, meta), now: T + 2300 });
  assert.equal(st.state, 'active'); assert.equal(st.since, T + 2000); assert.equal(st.duration, 300);
});
test('журнал недоступний, але контракт каже «тривога»: активний стан без вигаданого часу', () => {
  const st = AS.stateFor({ hasOfficial: true, stale: false, active: 'alert', episodes: [], now: N });
  assert.equal(st.state, 'active'); assert.equal(st.since, null); assert.equal(st.duration, null);
});
test('контракт недоступний (no_data) важливіший за порожній журнал: «тривоги немає» не показується', () => {
  const st = AS.stateFor({ hasOfficial: false, stale: false, active: null, episodes: [], now: N });
  assert.equal(st.state, 'no_data');
  assert.equal(AS.stateFor({ hasOfficial: true, stale: true, active: null, episodes: [], now: N }).state, 'stale');
});
test('конфліктні записи одного ключа: завершений перемагає відкритий, порядок не важливий', () => {
  const o = [1, 'sumska', 'raion', 'Х район', '', 'red', 'air_raid', 100, 0], c = [1, 'sumska', 'raion', 'Х район', '', 'red', 'air_raid', 100, 400];
  [[o, c], [c, o]].forEach(rows => { const a = [...AS.buildAlerts(rows).values()]; assert.equal(a.length, 1); assert.equal(a[0].ended, 400); });
});
test('фільтри переживають пошкоджений localStorage (нормалізація)', () => {
  [null, 'x', 5, [], { cats: 'БпЛА', region: 99, official: 'yes' }, { cats: ['nope'] }].forEach(v => {
    const f = F.normalize(v); assert.equal(typeof f.official, 'boolean'); assert.ok(Array.isArray(f.cats));
  });
});
