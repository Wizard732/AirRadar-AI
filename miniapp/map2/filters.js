/* map2/filters.js: фільтри і пошук нової карти. Чиста логіка без DOM (браузер і node).
   Один набір рядків (rows) живить і карту, і список, тому вони завжди узгоджені.
   Фільтри офіційних і неофіційних даних незалежні: вимкнення одного не чіпає інше.
   Фільтри не змінюють вибір, зум, підкладку; картка території завжди показує справжній стан, незалежно від фільтрів. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.Filters = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const CATEGORIES = ['БпЛА', 'Розвідка', 'Ракети', 'Балістика', 'КАБ', 'Авіація', 'Невідомі'];
  const defaults = () => ({ official: true, hint: true, cats: [], region: '', hideStale: false });

  /** Приведення довільного (напр. з localStorage) об'єкта до коректного набору фільтрів. */
  function normalize(f) {
    const d = defaults(); if (!f || typeof f !== 'object') return d;
    if (typeof f.official === 'boolean') d.official = f.official;
    if (typeof f.hint === 'boolean') d.hint = f.hint;
    if (Array.isArray(f.cats)) d.cats = [...new Set(f.cats.filter(c => CATEGORIES.includes(c)))];
    if (typeof f.region === 'string' && f.region.length < 40) d.region = f.region;
    if (typeof f.hideStale === 'boolean') d.hideStale = f.hideStale;
    return d;
  }
  const isDefault = f => { const d = defaults(); return f.official === d.official && f.hint === d.hint && !f.cats.length && !f.region && f.hideStale === d.hideStale; };
  /** Кількість активних відхилень від типових значень (для бейджа на вкладці). */
  const activeCount = f => (f.official ? 0 : 1) + (f.hint ? 0 : 1) + (f.cats.length ? 1 : 0) + (f.region ? 1 : 0) + (f.hideStale ? 1 : 0);

  /** Усі рядки з даних: {id, stem, kind:'official'|'hint', level|hint}. Київ/Севастополь і райони разом, без фільтрів. */
  function allRows(data, infoById) {
    const rows = [];
    infoById.forEach((p, id) => {
      const stem = id.split('|')[1];
      const off = data.official.raions.get(id) || (data.official.oblasts.has(stem) ? 'alert' : null);
      if (off) rows.push({ id, p, stem, kind: 'official', level: off });
      const hint = data.hints.get(id);
      if (hint) rows.push({ id, p, stem, kind: 'hint', hint });
    });
    return rows;
  }

  /** Застосування фільтрів до рядків. Порядок і вміст детерміновані; джерело не мутується. */
  function applyRows(rows, f) {
    return rows.filter(r => {
      if (f.region && r.stem !== f.region) return false;
      if (r.kind === 'official') return f.official;
      if (!f.hint) return false;
      if (f.cats.length && !f.cats.includes(r.hint.category)) return false;
      if (f.hideStale && r.hint.stale) return false;
      return true;
    });
  }

  /** Рядки → статуси для карти (Map id → {official, hint}). */
  function toStatuses(rows) {
    const m = new Map();
    rows.forEach(r => {
      const cur = m.get(r.id) || { official: null, hint: false };
      if (r.kind === 'official') cur.official = r.level; else cur.hint = true;
      m.set(r.id, cur);
    });
    return m;
  }

  // ---------- пошук ----------
  const STOP = new Set(['район', 'району', 'області', 'область', 'обл', 'м', 'місто', 'р-н']);
  const tokens = q => String(q || '').toLowerCase().replace(/[’ʼ'`.,]/g, '').split(/\s+/).filter(t => t && !STOP.has(t));
  const hay = s => String(s || '').toLowerCase().replace(/[’ʼ'`.,]/g, '');
  const oblastLabel = o => (/Крим|Київ|Севастополь/.test(o) ? o : o + ' область');

  /** Пошук областей і районів. Порожній запит або самі службові слова дають []; результат стабільно впорядкований. */
  function search(infoById, q, limit) {
    const tk = tokens(q); if (!tk.length) return [];
    const max = limit || 12, obl = new Map(), dist = [];
    infoById.forEach((p, id) => {
      const stem = id.split('|')[1], hr = hay(p.r), ho = hay(p.o);
      if (tk.every(t => ho.includes(t)) && !obl.has(stem)) obl.set(stem, { type: 'oblast', stem, label: oblastLabel(p.o), sub: 'Область', rank: ho.startsWith(tk[0]) ? 0 : 1 });
      if (tk.every(t => (hr + ' ' + ho).includes(t))) dist.push({ type: 'raion', id, label: p.r + ' район', sub: p.o, rank: hr.startsWith(tk[0]) ? 0 : 2 });
    });
    const byRank = (a, b) => a.rank - b.rank || a.label.localeCompare(b.label, 'uk');
    return [...obl.values(), ...dist].sort(byRank).slice(0, max);
  }

  return { CATEGORIES, defaults, normalize, isDefault, activeCount, allRows, applyRows, toStatuses, search, tokens, oblastLabel };
});
