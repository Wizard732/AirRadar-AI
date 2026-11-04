/* map2/shelters.js: цивільні укриття (OpenStreetMap, ODbL). Чиста логіка без DOM: розбір, відстань, найближчі.
   Дані неповні і не є офіційним реєстром: чи відкрите укриття, невідомо. Позиція користувача нікуди не передається:
   відстань рахується в браузері, у посилання на маршрут потрапляє лише точка призначення. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.Shelters = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const KINDS = ['Укриття', 'Точка без назви'];            // 0: назва вказує на цивільне укриття; 1: без назви, тип невідомий
  const WALK_M_PER_MIN = 80;
  const inUa = (lat, lon) => lat >= 43.5 && lat <= 53 && lon >= 21 && lon <= 41;

  /** Файл shelters.json → [{lat, lon, kind, name, street}]. Сміття і точки поза Україною відкидаються. */
  function parse(json) {
    const rows = json && Array.isArray(json.rows) ? json.rows : [];
    const out = [];
    rows.forEach(r => {
      if (!Array.isArray(r) || r.length < 3) return;
      const lat = +r[0], lon = +r[1];
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || !inUa(lat, lon)) return;
      out.push({ lat, lon, verified: +r[2] === 0, kind: KINDS[+r[2] === 0 ? 0 : 1], name: String(r[3] || ''), street: String(r[4] || '') });
    });
    return out;
  }

  /** Відстань по великому колу, метри. */
  function distanceM(lat1, lon1, lat2, lon2) {
    const R = 6371000, rad = Math.PI / 180, dLat = (lat2 - lat1) * rad, dLon = (lon2 - lon1) * rad;
    const a = Math.sin(dLat / 2) ** 2 + Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  /** n найближчих у межах maxM, від ближчого. Стабільно: за однакової відстані порядок як у даних. */
  function nearest(list, lat, lon, n, maxM) {
    const lim = maxM == null ? 5000 : maxM;
    return list.map((s, i) => ({ s, i, d: distanceM(lat, lon, s.lat, s.lon) }))
      .filter(x => x.d <= lim).sort((a, b) => a.d - b.d || a.i - b.i).slice(0, n || 3)
      .map(x => Object.assign({}, x.s, { distM: Math.round(x.d), walkMin: Math.max(1, Math.ceil(x.d / WALK_M_PER_MIN)) }));
  }

  const fmtDist = m => (m < 950 ? Math.round(m / 10) * 10 + ' м' : (m / 1000).toFixed(1).replace('.', ',') + ' км');
  /** Маршрут пішки: лише точка призначення, позиція користувача в посилання не потрапляє. */
  const routeUrl = s => 'https://www.google.com/maps/dir/?api=1&travelmode=walking&destination=' + s.lat.toFixed(5) + ',' + s.lon.toFixed(5);
  const title = s => s.name || (s.street ? 'Без назви: ' + s.street : 'Точка без назви, тип невідомий');

  return { KINDS, WALK_M_PER_MIN, parse, distanceM, nearest, fmtDist, routeUrl, title };
});
