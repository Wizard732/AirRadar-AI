/* map2/watch.js: обрані території і сповіщення про зміну офіційної тривоги. Чиста логіка без DOM (браузер і node).
   Сповіщення працюють лише поки сторінка відкрита: фонових пушів тут немає (їх дає бот/застосунок).
   Подія лише за офіційними даними і лише коли обидва знімки достовірні: застарілі чи відсутні дані не дають ні «тривога», ні «відбій». */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.Watch = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MAX_FAV = 8;

  /** Список обраних із довільного (localStorage) значення: рядки-ключі «Район|Область», унікальні, до MAX_FAV, лише відомі. */
  function normalizeFav(v, known) {
    const arr = Array.isArray(v) ? v : [];
    return [...new Set(arr.filter(x => typeof x === 'string' && x.length < 80 && (!known || known.has(x))))].slice(0, MAX_FAV);
  }
  function toggleFav(list, id) {
    if (list.includes(id)) return { list: list.filter(x => x !== id), added: false };
    if (list.length >= MAX_FAV) return { list, added: false, full: true };
    return { list: list.concat(id), added: true };
  }
  /** Ключі, за якими стежимо: головна територія + обрані (без дублів). */
  const watchedIds = (myId, fav) => [...new Set([myId, ...fav].filter(Boolean))];

  /** Знімок статусів за списком id: {id: 'alert'|'warn'|null}; ok=false, якщо дані недостовірні. */
  function snapshot(ids, statusOf, reliable) {
    const s = {}; ids.forEach(id => { s[id] = statusOf(id) || null; });
    return { ok: !!reliable, s };
  }

  /** Події між двома знімками: [{id, kind:'start'|'warn'|'downgrade'|'end'}]. Перший знімок (prev=null) подій не дає.
      Якщо попередній знімок був недостовірним (застарілі дані), про початок повідомляємо, а про відбій ні: що було «до», невідомо. */
  function diff(prev, next) {
    if (!prev || !next || !next.ok) return [];
    const unsure = !prev.ok, out = [];
    Object.keys(next.s).forEach(id => {
      if (!(id in prev.s)) return;                       // територію щойно додали до стеження: це не подія
      const a = prev.s[id], b = next.s[id];
      if (a === b) return;
      if (b === 'alert') out.push({ id, kind: 'start' });
      else if (b === 'warn' && !a) out.push({ id, kind: 'warn' });
      else if (!b && !unsure) out.push({ id, kind: 'end' });
      else if (b === 'warn' && a === 'alert' && !unsure) out.push({ id, kind: 'downgrade' });
    });
    return out;
  }

  const TEXT = { start: 'Повітряна тривога', warn: 'Загроза поруч', end: 'Відбій повітряної тривоги', downgrade: 'Рівень знижено до «загроза поруч»' };
  const message = (ev, name) => TEXT[ev.kind] + ': ' + name;
  /** Одне повідомлення на тип події: «Повітряна тривога: A, B, C». items = [{kind, name}]. */
  function summary(items) {
    const order = ['start', 'warn', 'downgrade', 'end'], by = {};
    items.forEach(it => { (by[it.kind] = by[it.kind] || []).push(it.name); });
    return order.filter(k => by[k]).map(k => TEXT[k] + ': ' + by[k].join(', ')).join(' · ');
  }

  /** Нормалізація налаштувань сповіщень. */
  function normalizeNotify(v) {
    const o = v && typeof v === 'object' ? v : {};
    return { on: o.on === true, sound: o.sound !== false, system: o.system === true };
  }

  return { MAX_FAV, normalizeFav, toggleFav, watchedIds, snapshot, diff, message, summary, normalizeNotify };
});
