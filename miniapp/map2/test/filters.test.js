'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const F = require('../filters.js');
global.window = global; global.document = { baseURI: 'http://localhost/map2/index.html', addEventListener() {} };
global.location = { hostname: 'localhost' }; global.addEventListener = () => {};
require('../data.js');

const info = new Map([
  ['Ізюмський|Харківська', { r: 'Ізюмський', o: 'Харківська' }],
  ['Богодухівський|Харківська', { r: 'Богодухівський', o: 'Харківська' }],
  ['Охтирський|Сумська', { r: 'Охтирський', o: 'Сумська' }],
  ['Кременчуцький|Полтавська', { r: 'Кременчуцький', o: 'Полтавська' }],
  ['Київ|Київ', { r: 'Київ', o: 'Київ' }],
  ['Ялтинський|Крим', { r: 'Ялтинський', o: 'Автономна Республіка Крим' }],
]);
const raw = { updated_at: 1000, official: { sirens: ['sumska'], sirens_raions: ['Ізюмський|Харківська'], sirens_raions_y: [] },
  regions: [
    { region: 'poltavska', raion: 'Кременчуцький', category: 'shahed', count_bucket: '2-5', last_update: 900, stale: false },
    { region: 'kharkivska', raion: 'Богодухівський', category: 'recon_drone', count_bucket: '1', last_update: 100, stale: true }] };
const data = AirData.normalize(raw, 1000);
const rows = F.allRows(data, info);
const ids = (r, k) => r.filter(x => !k || x.kind === k).map(x => x.id).sort((a, b) => a.localeCompare(b, 'uk'));

test('типові фільтри: усе видно, isDefault, activeCount=0', () => {
  const f = F.defaults(); assert.ok(F.isDefault(f)); assert.equal(F.activeCount(f), 0);
  assert.equal(F.applyRows(rows, f).length, rows.length);
});

test('офіційні і неофіційні фільтри незалежні', () => {
  const noHint = F.applyRows(rows, { ...F.defaults(), hint: false });
  assert.ok(noHint.every(r => r.kind === 'official')); assert.equal(ids(noHint).length, 2);
  const noOff = F.applyRows(rows, { ...F.defaults(), official: false });
  assert.ok(noOff.every(r => r.kind === 'hint')); assert.equal(noOff.length, 2);
  assert.equal(F.applyRows(rows, { ...F.defaults(), official: false, hint: false }).length, 0);
});

test('вимкнення неофіційних не змінює офіційну частину і навпаки', () => {
  const base = ids(F.applyRows(rows, F.defaults()), 'official');
  assert.deepEqual(ids(F.applyRows(rows, { ...F.defaults(), hint: false }), 'official'), base);
  const baseH = ids(F.applyRows(rows, F.defaults()), 'hint');
  assert.deepEqual(ids(F.applyRows(rows, { ...F.defaults(), official: false }), 'hint'), baseH);
});

test('категорії діють тільки на неофіційні; офіційні не зникають', () => {
  const r = F.applyRows(rows, { ...F.defaults(), cats: ['Розвідка'] });
  assert.deepEqual(ids(r, 'hint'), ['Богодухівський|Харківська']);
  assert.equal(ids(r, 'official').length, 2);
});

test('приховати застарілі: лише неофіційні, застарілі зникають', () => {
  const r = F.applyRows(rows, { ...F.defaults(), hideStale: true });
  assert.deepEqual(ids(r, 'hint'), ['Кременчуцький|Полтавська']); assert.equal(ids(r, 'official').length, 2);
});

test('область: обмежує обидва типи; поєднання з офіційними й категорією', () => {
  const r = F.applyRows(rows, { ...F.defaults(), region: 'Харківська' });
  assert.deepEqual(ids(r), ['Богодухівський|Харківська', 'Ізюмський|Харківська']);
  const r2 = F.applyRows(rows, { ...F.defaults(), region: 'Харківська', official: false });
  assert.deepEqual(ids(r2), ['Богодухівський|Харківська']);
  const r3 = F.applyRows(rows, { ...F.defaults(), region: 'Харківська', hint: false, cats: ['БпЛА'] });
  assert.deepEqual(ids(r3), ['Ізюмський|Харківська']);
  assert.equal(F.applyRows(rows, { ...F.defaults(), region: 'Полтавська', official: true, hint: false }).length, 0);   // порожній результат
});

test('фільтри не мутують джерело і детерміновані', () => {
  const before = JSON.stringify(rows.map(r => [r.id, r.kind]));
  const f = { ...F.defaults(), region: 'Харківська' };
  assert.deepEqual(F.applyRows(rows, f).map(r => r.id), F.applyRows(rows, f).map(r => r.id));
  assert.equal(JSON.stringify(rows.map(r => [r.id, r.kind])), before);
});

test('toStatuses: один район може мати і офіційний, і неофіційний статус одночасно', () => {
  const both = [{ id: 'A|X', kind: 'official', level: 'alert' }, { id: 'A|X', kind: 'hint', hint: {} }];
  assert.deepEqual(F.toStatuses(both).get('A|X'), { official: 'alert', hint: true });
  const st = F.toStatuses(F.applyRows(rows, { ...F.defaults(), official: false }));
  assert.ok([...st.values()].every(v => v.official === null && v.hint === true));
});

test('normalize: сміття з localStorage приводиться до типових значень, невідомі категорії відкидаються', () => {
  assert.deepEqual(F.normalize(null), F.defaults());
  assert.deepEqual(F.normalize('x'), F.defaults());
  const f = F.normalize({ official: 'yes', hint: false, cats: ['БпЛА', 'Хакер', 'БпЛА'], region: 'x'.repeat(100), evil: 1 });
  assert.equal(f.official, true); assert.equal(f.hint, false); assert.deepEqual(f.cats, ['БпЛА']); assert.equal(f.region, ''); assert.equal(f.evil, undefined);
});

test('пошук: порожній запит і службові слова = []', () => {
  assert.deepEqual(F.search(info, ''), []); assert.deepEqual(F.search(info, '   '), []); assert.deepEqual(F.search(info, 'район'), []);
});

test('пошук: області і райони, регістр, апострофи, службові слова', () => {
  const r = F.search(info, 'ХАРКІВ');
  assert.equal(r[0].type, 'oblast'); assert.equal(r[0].stem, 'Харківська'); assert.equal(r.filter(x => x.type === 'raion').length, 2);
  assert.equal(F.search(info, 'ізюмський район')[0].id, 'Ізюмський|Харківська');
  assert.equal(F.search(info, 'сумська область')[0].stem, 'Сумська');
  assert.equal(F.search(info, 'крим')[0].stem, 'Крим');
  assert.ok(F.search(info, 'київ').some(x => x.id === 'Київ|Київ'));
});

test('пошук: нічого не знайдено = [], ліміт працює', () => {
  assert.deepEqual(F.search(info, 'яяяя'), []);
  assert.equal(F.search(info, 'і', 2).length, 2);
});
