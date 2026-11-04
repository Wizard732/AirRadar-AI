/* map2/stats.js: статистика і відтворення історії з епізодів офіційних тривог (alerts-state.js). Чиста логіка без DOM.
   Джерело лише журнал офіційних тривог; неофіційні повідомлення сюди не потрапляють. Журнал охоплює обмежений період:
   межу покриття (coverageFrom) показуємо явно, раніше цієї межі «немає тривог» і «немає даних» не розрізнити. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.Stats = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const KYIV = 'Europe/Kyiv';
  /** Година доби за київським часом (0-23). */
  function kyivHour(ts) {
    const h = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', hour12: false, timeZone: KYIV }).format(new Date(ts * 1000));
    return (+h) % 24;
  }

  /** Початок покриття журналу: найраніший старт епізоду (null, якщо порожньо). */
  const coverageFrom = eps => eps.length ? Math.min(...eps.map(e => e.started)) : null;

  /** Скільки секунд епізод перетинає вікно [from, to]; відкритий епізод триває до `to`. Пауза між частинами не рахується окремо. */
  function overlap(e, from, to) {
    const end = e.ended === null ? to : Math.min(e.ended, to), st = Math.max(e.started, from);
    return Math.max(0, end - st);
  }

  /** Статистика за вікно [now-windowS, now]. */
  function summarize(episodes, now, windowS, slugName) {
    const from = now - windowS, inWin = episodes.filter(e => (e.ended === null ? now : e.ended) >= from && e.started <= now);
    const byTerr = new Map(), byOblast = new Map(), hours = new Array(24).fill(0);
    let totalSec = 0;
    inWin.forEach(e => {
      const sec = overlap(e, from, now); totalSec += sec;
      const key = e.slug; const o = byOblast.get(key) || { slug: key, name: slugName ? slugName(key) : key, count: 0, sec: 0 };
      o.count++; o.sec += sec; byOblast.set(key, o);
      byTerr.set(e.territory, 1);
      hours[kyivHour(Math.max(e.started, from))]++;
    });
    const top = [...byOblast.values()].sort((a, b) => b.count - a.count || b.sec - a.sec || a.name.localeCompare(b.name, 'uk')).slice(0, 6);
    return { count: inWin.length, territories: byTerr.size, totalSec, top, hours, peakHour: hours.some(Boolean) ? hours.indexOf(Math.max(...hours)) : null, coverageFrom: coverageFrom(episodes) };
  }

  /** Епізоди, активні в момент t. Відкритий епізод активний, якщо started <= t. */
  const activeAt = (episodes, t) => episodes.filter(e => e.started <= t && (e.ended === null || e.ended > t));

  /** Діапазон слайдера історії: [from, now], крок step; from не раніше покриття журналу. */
  function timelineRange(episodes, now, maxBackS, stepS) {
    const step = stepS || 300, cov = coverageFrom(episodes);
    const lo = cov == null ? now : Math.max(now - maxBackS, Math.floor(cov / step) * step);
    return { from: Math.min(lo, now), to: now, step, covered: cov != null };
  }

  return { kyivHour, coverageFrom, overlap, summarize, activeAt, timelineRange };
});
