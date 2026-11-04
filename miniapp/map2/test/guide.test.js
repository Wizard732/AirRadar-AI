'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const G = require('../guide.js'), F = require('../filters.js');
test('довідник покриває всі категорії фільтрів і має силует для кожного типу', () => {
  F.CATEGORIES.forEach(c => assert.ok(G.byCat(c).length, c));
  G.TYPES.forEach(t => assert.ok(G.SIL[t.sil], t.id));
});
test('довідник не містить оперативних даних: ні чисел швидкості/дальності/ETA, ні координат', () => {
  const txt = JSON.stringify(G.TYPES);
  assert.ok(!/км\/год|км\b|м\/с|хв\b|ETA|курс|азимут|координат/i.test(txt));
});
