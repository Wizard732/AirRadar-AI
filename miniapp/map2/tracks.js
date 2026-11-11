/* map2/tracks.js: шар цілей (треки з tracks.json або /api/tracks). Чиста логіка без DOM: нормалізація, перевірки, підписи, демо-потік.
   Принципи (за узгодженням із власником і ревю архітектора):
   - позиція НЕ екстраполюється між оновленнями: значок стоїть там, де його бачили востаннє (плавний рух створював би хибне враження безперервного спостереження);
   - прогноз лише для свіжих треків; ETA завжди діапазоном; типова швидкість позначається як не виміряна;
   - застарілий трек сірий і без прогнозу; відсутність мітки на карті не означає відсутність загрози. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.Tracks = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const KINDS = {
    shahed: { name: 'Шахед (БпЛА-камікадзе)', short: 'Шахед', glyph: 'delta', guide: 'Шахед/Герань' },
    uav: { name: 'Розвідувальний БпЛА', short: 'БпЛА', glyph: 'delta', guide: 'БпЛА' },
    cruise_missile: { name: 'Крилата ракета', short: 'Крилата', glyph: 'dart', guide: 'Ракети' },
    ballistic: { name: 'Балістична ракета', short: 'Балістика', glyph: 'ring', guide: 'Балістика' },
  };
  const BOUNDS = { latMin: 43.0, latMax: 54.5, lonMin: 21.0, lonMax: 41.5 };     // Україна з запасом на сусідів і море
  const STALE_S = 25 * 60;                                                         // як у track_engine.STALE_AFTER_S
  const FEED_STALE_S = 15 * 60;                                                    // увесь файл давно не оновлювався
  const MAX_TRACKS = 200, MAX_POINTS = 30, MAX_FORECAST = 8, MAX_ETA_MIN = 240, MAX_SPEED = 3500;

  const num = v => (typeof v === 'number' && Number.isFinite(v) ? v : (typeof v === 'string' && v.trim() !== '' && Number.isFinite(+v) ? +v : null));
  const inBounds = (lat, lon) => lat != null && lon != null && lat >= BOUNDS.latMin && lat <= BOUNDS.latMax && lon >= BOUNDS.lonMin && lon <= BOUNDS.lonMax;
  const str = (v, n) => (typeof v === 'string' ? v.replace(/[\u0000-\u001f<>]/g, ' ').trim().slice(0, n || 80) : '');

  /** ETA завжди діапазоном: виміряна швидкість ±20%, типова ±40%. */
  function etaRange(etaMin, speedSource) {
    const f = speedSource === 'measured' ? 0.2 : 0.4;
    return { lo: Math.max(1, Math.floor(etaMin * (1 - f))), hi: Math.max(1, Math.ceil(etaMin * (1 + f))) };
  }
  const fmtEta = r => (r.lo === r.hi ? '≈ ' + r.lo + ' хв' : '≈ ' + r.lo + '–' + r.hi + ' хв');

  function normPoint(p) {
    if (!p || typeof p !== 'object') return null;
    const lat = num(p.lat), lon = num(p.lon), ts = num(p.ts);
    if (!inBounds(lat, lon)) return null;
    return { lat, lon, ts: ts && ts > 0 ? ts : null, place: str(p.place, 60) };
  }

  function normTrack(r, nowS, feedStale) {
    if (!r || typeof r !== 'object') return null;
    const weapon = KINDS[r.weapon] ? r.weapon : null;
    const cur = r.now && typeof r.now === 'object' ? r.now : null;
    const lat = cur ? num(cur.lat) : null, lon = cur ? num(cur.lon) : null;
    if (!weapon || !inBounds(lat, lon)) return null;
    const points = (Array.isArray(r.points) ? r.points : []).map(normPoint).filter(Boolean).slice(-MAX_POINTS);
    const lastTs = points.reduce((m, p) => (p.ts && p.ts > m ? p.ts : m), 0) || null;
    const ageS = lastTs ? Math.max(0, nowS - lastTs) : null;
    const status = r.status === 'loitering' ? 'loitering' : (r.status === 'stale' ? 'stale' : 'active');
    const stale = feedStale || status === 'stale' || ageS == null || ageS > STALE_S;
    let speed = num(r.speed_kmh); if (speed != null && (speed <= 0 || speed > MAX_SPEED)) speed = null;
    const speedSource = r.speed_source === 'measured' && speed != null ? 'measured' : 'typical';
    let heading = num(r.heading); if (heading != null) heading = ((Math.round(heading) % 360) + 360) % 360;
    const forecast = stale ? [] : (Array.isArray(r.forecast) ? r.forecast : []).map(f => {
      if (!f || typeof f !== 'object') return null;
      const flat = num(f.lat), flon = num(f.lon), eta = num(f.eta_min), km = num(f.km);
      if (!inBounds(flat, flon) || eta == null || eta < 0 || eta > MAX_ETA_MIN) return null;
      const name = str(f.name, 50); if (!name) return null;
      return { name, lat: flat, lon: flon, km: km != null && km >= 0 ? Math.round(km) : null, etaMin: Math.round(eta), eta: etaRange(Math.round(eta), speedSource) };
    }).filter(Boolean).slice(0, MAX_FORECAST);
    const d = r.dest && typeof r.dest === 'object' ? { lat: num(r.dest.lat), lon: num(r.dest.lon), name: str(r.dest.name, 50) } : null;
    const dest = !stale && d && inBounds(d.lat, d.lon) ? d : null;
    return {
      id: str(String(r.id || ''), 80) || (weapon + ':' + lat.toFixed(2) + ':' + lon.toFixed(2)), weapon, kind: KINDS[weapon],
      count: Math.max(1, Math.min(999, Math.round(num(r.count) || 1))), status, stale, lat, lon, estimated: !!(cur && cur.estimated),
      speedKmh: speed != null ? Math.round(speed) : null, speedSource, heading, headingText: str(r.heading_text, 12),
      lastTs, ageS, points, forecast, dest, sources: Math.max(1, Math.min(99, Math.round(num(r.sources) || 1))),
      official: r.official === true ? true : (r.official === false ? false : null),
    };
  }

  /** Сирий payload → безпечний список. Будь-яке сміття відкидається, а не ламає карту. */
  function normalize(raw, nowS) {
    const out = { tracks: [], rejected: 0, generalized: false, updatedAt: 0, feedStale: false, ok: false };
    if (!raw || typeof raw !== 'object' || !Array.isArray(raw.tracks)) return out;
    out.ok = true;
    out.updatedAt = num(raw.now) || num(raw.updated_at) || 0;
    out.feedStale = !!nowS && (out.updatedAt <= 0 || nowS - out.updatedAt > FEED_STALE_S);
    const src = raw.tracks.slice(0, MAX_TRACKS);
    src.forEach(r => { const t = normTrack(r, nowS, out.feedStale); if (t) out.tracks.push(t); else out.rejected++; });
    out.generalized = raw.public === true || (src.length > 0 && out.tracks.length === 0);       // узагальнений режим сервера: точних позицій немає
    // дублі id: лишається найсвіжіший
    const seen = new Map(); out.tracks.forEach(t => { const p = seen.get(t.id); if (!p || (t.lastTs || 0) >= (p.lastTs || 0)) seen.set(t.id, t); });
    out.tracks = [...seen.values()];
    return out;
  }

  const fmtAge = s => (s == null ? 'час невідомий' : (s < 90 ? 'щойно' : (s < 3600 ? Math.round(s / 60) + ' хв тому' : Math.floor(s / 3600) + ' год ' + Math.round((s % 3600) / 60) + ' хв тому')));
  const speedText = t => (t.speedKmh == null ? 'швидкість невідома' : '≈ ' + t.speedKmh + ' км/год · ' + (t.speedSource === 'measured' ? 'виміряна за зміщенням' : 'типова, не виміряна'));
  const headingText = t => (t.heading == null ? 'курс невідомий' : t.heading + '°' + (t.headingText ? ' · ' + t.headingText : ''));
  const trustText = t => (t.sources > 1 ? t.sources + ' незалежні повідомлення' : 'одне повідомлення') + (t.estimated ? ' · позиція орієнтовна' : '');
  /** Є що малювати як курс: відома точка призначення/прогноз або курс у градусах. Свіжі цілі без нього йдуть у лічильник «без курсу». */
  const hasCourse = t => !!(t.dest || t.forecast.length || t.heading != null);
  /** Лічильники для панелі зверху: кількість цілей (з урахуванням count) за типом + скільки без курсу. Застарілі не рахуються. */
  function counts(tracks) {
    const c = { shahed: 0, cruise_missile: 0, ballistic: 0, uav: 0, noCourse: 0 };
    tracks.forEach(t => { if (t.stale) return; c[t.weapon] += t.count; if (!hasCourse(t)) c.noCourse += t.count; });
    return c;
  }
  const filterKinds = (tracks, kinds) => (kinds && kinds.length ? tracks.filter(t => kinds.indexOf(t.weapon) >= 0 || (t.weapon === 'uav' && kinds.indexOf('shahed') >= 0)) : tracks);


  // ---------- радар навколо користувача ----------
  /** Цілі в колі радіуса radiusKm навколо pos [lat,lon] або такі, що курсом на цю точку (у межах 2R). Лише свіжі; застарілі не враховуються. */
  function nearby(tracks, pos, radiusKm) {
    const out = [];
    (tracks || []).forEach(t => {
      if (t.stale || t.lat == null) return;
      const km = dist(pos, [t.lat, t.lon]), brg = bearing(pos, [t.lat, t.lon]);
      let toMe = false;
      if (t.heading != null && km <= radiusKm * 2) {
        const back = bearing([t.lat, t.lon], pos); let d = Math.abs(t.heading - back) % 360; if (d > 180) d = 360 - d;
        toMe = d <= 30;
      }
      let eta = null;
      (t.forecast || []).forEach(f => { if (f.etaMin != null && dist(pos, [f.lat, f.lon]) <= Math.max(5, radiusKm / 2) && (eta == null || f.etaMin < eta)) eta = f.etaMin; });
      const inside = km <= radiusKm;
      if (!inside && !toMe && eta == null) return;
      out.push({ t, km, brg, side: compass(brg), inside, toMe: toMe || eta != null, etaMin: eta });
    });
    return out.sort((a, b) => a.km - b.km);
  }

  /** Якісна ймовірність підльоту до точки за результатом nearby(): без «відсотків» (вони були б вигаданою точністю). */
  function probability(n) {
    if (n.km <= 3) return 'дуже висока';
    if (n.toMe && n.inside) return 'висока';
    if (n.toMe) return 'середня';
    return 'низька–середня';
  }

  const DISCLAIMER = 'Карта не показує всі загрози і не замінює офіційні оповіщення. Відсутність мітки не означає відсутність небезпеки. Під час тривоги дійте за вказівками офіційних служб.';
  const FORECAST_NOTE = 'Це орієнтовна оцінка, а не підтверджений маршрут. Дані можуть змінитися.';

  // ---------- демо-потік: ВИГАДАНІ цілі для перевірки інтерфейсу, не реальні дані ----------
  const CITIES = [['Київ', 50.45, 30.52], ['Полтава', 49.59, 34.55], ['Суми', 50.91, 34.80], ['Чернігів', 51.49, 31.29], ['Дніпро', 48.46, 35.05], ['Харків', 49.99, 36.23],
    ['Одеса', 46.48, 30.73], ['Вінниця', 49.23, 28.47], ['Житомир', 50.25, 28.66], ['Черкаси', 49.44, 32.06], ['Кременчук', 49.07, 33.42], ['Миколаїв', 46.97, 32.0], ['Запоріжжя', 47.84, 35.14], ['Хмельницький', 49.42, 26.99]];
  const R = 6371;
  const rad = d => d * Math.PI / 180, deg = r => r * 180 / Math.PI;
  function dist(a, b) { const dLa = rad(b[0] - a[0]), dLo = rad(b[1] - a[1]); const h = Math.sin(dLa / 2) ** 2 + Math.cos(rad(a[0])) * Math.cos(rad(b[0])) * Math.sin(dLo / 2) ** 2; return 2 * R * Math.asin(Math.sqrt(h)); }
  function bearing(a, b) { const y = Math.sin(rad(b[1] - a[1])) * Math.cos(rad(b[0])), x = Math.cos(rad(a[0])) * Math.sin(rad(b[0])) - Math.sin(rad(a[0])) * Math.cos(rad(b[0])) * Math.cos(rad(b[1] - a[1])); return (deg(Math.atan2(y, x)) + 360) % 360; }
  function dest(a, brg, km) { const d = km / R, b = rad(brg), la = rad(a[0]), lo = rad(a[1]); const la2 = Math.asin(Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(b)); const lo2 = lo + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la), Math.cos(d) - Math.sin(la) * Math.sin(la2)); return [deg(la2), deg(lo2)]; }
  const COMPASS = ['Пн', 'ПнСх', 'Сх', 'ПдСх', 'Пд', 'ПдЗх', 'Зх', 'ПнЗх'];
  const compass = b => COMPASS[Math.round(b / 45) % 8];

  // [id, weapon, старт lat, lon, курс°, км/год, хвилин у польоті, кількість, джерел, виміряна?, вік останнього повідомлення хв]
  const DEMO = [
    ['d1', 'shahed', 51.9, 35.4, 215, 180, 70, 6, 3, true, 1], ['d2', 'shahed', 51.6, 34.2, 200, 185, 55, 3, 2, true, 2],
    ['d3', 'shahed', 47.0, 33.5, 340, 175, 45, 4, 1, false, 4], ['d4', 'cruise_missile', 52.4, 31.0, 185, 780, 12, 2, 2, true, 1],
    ['d5', 'ballistic', 49.9, 39.5, 280, 1800, 3, 1, 1, false, 2], ['d6', 'uav', 49.0, 28.0, 20, 140, 40, 1, 1, false, 5],
    ['d7', 'shahed', 48.0, 31.0, 330, 180, 80, 2, 1, false, 40], ['d8', 'shahed', 50.0, 25.5, null, 180, 0, 3, 1, false, 3], ['d9', 'uav', 48.7, 36.3, 90, 90, 10, 1, 1, false, 2],
  ];
  function demoPayload(nowS, rows) {
    const tracks = (rows || DEMO).map(d => {
      const [id, weapon, la, lo, brg, kmh, minIn, count, sources, measured, ageMin] = d;
      const start = [la, lo], cur = brg == null ? start : dest(start, brg, kmh * minIn / 60);
      const pts = []; if (brg != null) for (let m = minIn; m >= 0; m -= Math.max(3, Math.ceil(minIn / 10))) { const p = dest(start, brg, kmh * (minIn - m) / 60); pts.push({ ts: nowS - m * 60 - ageMin * 60, lat: +p[0].toFixed(4), lon: +p[1].toFixed(4), count, place: '', precision: 'approx' }); }
      const fc = brg == null ? [] : CITIES.map(c => { const k = dist(cur, [c[1], c[2]]); const off = Math.abs(((bearing(cur, [c[1], c[2]]) - brg + 540) % 360) - 180); return { name: c[0], lat: c[1], lon: c[2], km: Math.round(k), off, eta_min: Math.round(k / kmh * 60) }; })
        .filter(c => c.off < 20 && c.eta_min <= 90 && c.km > 5).sort((a, b) => a.eta_min - b.eta_min).slice(0, 4).map(c => ({ name: c.name, lat: c.lat, lon: c.lon, eta_min: c.eta_min, km: c.km }));
      return { id: 'demo:' + id, weapon, profile: '', status: ageMin > 25 ? 'stale' : 'active', count, speed_kmh: kmh, speed_source: measured ? 'measured' : 'typical', heading: brg == null ? null : Math.round(brg), heading_text: brg == null ? '' : compass(brg),
        now: { lat: +cur[0].toFixed(4), lon: +cur[1].toFixed(4), estimated: !measured }, points: pts.length ? pts : [{ ts: nowS - ageMin * 60, lat: cur[0], lon: cur[1], count, place: '', precision: 'approx' }], forecast: fc, dest: null, sources, official: null };
    });
    return { now: nowS, window_min: 180, tracks, events: [], impacts: [] };
  }

  // ---------- сценарій «атака» (?demo=attack): ВИГАДАНІ цілі летять, 1 с реального часу = 1 хв сценарію ----------
  const KYIV = [50.45, 30.52];
  // [id, зброя, старт lat, lon, ціль [lat, lon], км/год, старт (хв сценарію), кількість, джерел, виміряна?]
  const ATTACK = [
    ['a1', 'shahed', 51.7, 33.4, KYIV, 180, 0, 6, 3, true], ['a2', 'shahed', 51.9, 33.0, [50.51, 30.79], 180, 4, 3, 2, true],
    ['a3', 'shahed', 47.4, 33.6, [48.46, 35.05], 175, 6, 4, 2, false], ['a4', 'shahed', 46.0, 31.0, [46.97, 32.0], 175, 10, 2, 1, false],
    ['c1', 'cruise_missile', 53.2, 31.3, KYIV, 780, 35, 4, 2, true], ['c2', 'cruise_missile', 52.4, 36.4, [49.99, 36.23], 780, 36, 2, 2, true],
    ['b1', 'ballistic', 53.6, 35.4, KYIV, 1800, 62, 1, 2, false], ['b2', 'ballistic', 49.9, 40.5, [48.46, 35.05], 1800, 64, 1, 1, false],
  ];
  const ATTACK_PHASES = [[0, 'Фаза 1 · шахеди'], [35, 'Фаза 2 · крилаті ракети'], [62, 'Фаза 3 · балістика'], [90, 'Відбій']];
  const ATTACK_LEN = 100;
  /** simMin — хвилини сценарію від початку (циклічно). Ціль зникає, діставшись цілі. */
  function demoAttack(nowS, simMin) {
    const m = simMin % ATTACK_LEN, rows = [];
    ATTACK.forEach(a => {
      const [id, weapon, la, lo, target, kmh, startMin, count, sources, measured] = a;
      const flightMin = dist([la, lo], target) / kmh * 60, minIn = m - startMin;
      if (minIn < 0 || minIn > flightMin) return;
      rows.push([id, weapon, la, lo, bearing([la, lo], target), kmh, minIn, count, sources, measured, 0]);
    });
    const p = demoPayload(nowS, rows);
    p.attack = { simMin: Math.round(m), phase: (ATTACK_PHASES.filter(x => m >= x[0]).pop() || ATTACK_PHASES[0])[1] };
    return p;
  }

  return { demoAttack, probability, nearby, compass, dist, bearing, hasCourse, counts, KINDS, BOUNDS, STALE_S, FEED_STALE_S, MAX_TRACKS, normalize, normTrack, etaRange, fmtEta, fmtAge, speedText, headingText, trustText, filterKinds, DISCLAIMER, FORECAST_NOTE, demoPayload };
});
