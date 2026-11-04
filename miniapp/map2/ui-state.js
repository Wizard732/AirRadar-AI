/* map2/ui-state.js: налаштування вигляду і посилання на територію. Чиста логіка без DOM (браузер і node). */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.UiState = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const THEMES = ['dark', 'light', 'auto'], SIZES = ['m', 'l', 'xl'];
  const SCALE = { m: 1, l: 1.15, xl: 1.3 };

  function normalizePrefs(v) {
    const o = v && typeof v === 'object' ? v : {};
    return { theme: THEMES.includes(o.theme) ? o.theme : 'dark', size: SIZES.includes(o.size) ? o.size : 'm' };
  }

  /** Посилання на територію: ?d=Район|Область. Інші параметри відносно бази не додаються, щоб посилання було коротким і не несло службових прапорців. */
  function linkFor(base, id) { return base + '?d=' + encodeURIComponent(id); }

  /** id з рядка пошуку: лише відомий район. Невідомий або зіпсований параметр дає null, а не виняток. */
  function districtFromSearch(search, known) {
    let v; try { v = new URLSearchParams(search).get('d'); } catch (e) { return null; }
    return v && v.length < 80 && known.has(v) ? v : null;
  }
  const hasDistrictParam = search => { try { return new URLSearchParams(search).has('d'); } catch (e) { return false; } };

  /** Оновлений search без d (або з новим d), інші параметри зберігаються. */
  function withDistrict(search, id) {
    const p = new URLSearchParams(search); if (id) p.set('d', id); else p.delete('d');
    const s = p.toString(); return s ? '?' + s : '';
  }

  return { THEMES, SIZES, SCALE, normalizePrefs, linkFor, districtFromSearch, hasDistrictParam, withDistrict };
});
