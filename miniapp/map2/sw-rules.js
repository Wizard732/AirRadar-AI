/* map2/sw-rules.js: правила Service Worker нової карти. Чиста логіка (SW через importScripts і node-тести).
   Головне правило безпеки: ДИНАМІЧНІ ДАНІ ТРИВОГ SW НЕ КЕШУЄ ВЗАГАЛІ. Вчорашній статус не повинен виглядати як поточний;
   збережені дані карта показує сама (localStorage) і завжди з явним віком і плашкою. SW зберігає лише код і геометрію. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.Map2SwRules = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const VERSION = 'v5';                                   // змінити при зміні списку/логіки: старі кеші map2 буде видалено
  const CACHE_PREFIX = 'airradar-map2-';
  const CACHE = CACHE_PREFIX + VERSION;
  const LEAFLET = ['https://unpkg.com/leaflet@1.9.4/dist/leaflet.css', 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js'];
  const LOCAL = ['./', 'index.html', 'map2.css', 'layers.js', 'alerts-state.js', 'filters.js', 'guide.js', 'shelters.js', 'watch.js', 'stats.js', 'ui-state.js', 'tracks.js', 'shelters.json', 'sync.js', 'data.js', 'app.js', '../raions.json'];

  // ніколи не кешуємо: API, потік, дані тривог і треків, усе адміністративне/приватне
  const NEVER = [/\/api\//i, /public-regions\.json$/i, /alerts_hist\.json$/i, /tracks\.json$/i, /impacts\.json$/i, /\/admin/i, /\/health/i, /\/private/i, /\/push|subscribe/i];

  /** Клас запиту: 'never' (мережа без втручання SW), 'leaflet' (незмінна версія, cache-first), 'static' (наш код: network-first), 'other'. */
  function classify(urlStr, scopeUrl) {
    let u, s; try { u = new URL(urlStr); s = new URL(scopeUrl); } catch (e) { return 'never'; }
    if (NEVER.some(re => re.test(u.pathname))) return 'never';
    if (LEAFLET.includes(u.origin + u.pathname)) return 'leaflet';
    if (u.origin !== s.origin) return 'other';             // тайли карти, інші домени: SW не втручається
    if (u.pathname.endsWith('/raions.json') || u.pathname.startsWith(s.pathname)) return 'static';
    return 'other';
  }
  /** Чи варто класти відповідь у кеш. */
  const cacheable = r => !!r && r.ok && r.status === 200 && (r.type === 'basic' || r.type === 'cors');
  /** Кеші map2 до видалення при активації: усі свої старих версій; чужі (основного застосунку) не чіпаємо. */
  const staleCaches = keys => keys.filter(k => k.startsWith(CACHE_PREFIX) && k !== CACHE);

  return { VERSION, CACHE_PREFIX, CACHE, LEAFLET, LOCAL, NEVER, classify, cacheable, staleCaches };
});
