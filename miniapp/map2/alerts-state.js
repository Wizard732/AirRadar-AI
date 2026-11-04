/* map2/alerts-state.js: стан офіційних тривог. Чиста логіка без DOM, працює в браузері та в node (тести).
   Джерело історії: /api/alerts-history або alerts_hist.json; рядок: [id, slug, loc_type, title, raion, level, kind, started_ts, ended_ts],
   ended_ts = 0 доки тривога триває. Час завершення виставляє бот за опитуванням alerts.in.ua, тож точність кінця ≈ інтервал опитування.
   Неофіційні повідомлення сюди НЕ потрапляють: це окрема сутність (ключ hint:..., див. data.js).

   Ключ тривоги:   alert:{slug}:{район|*}:{started_ts}   (* = тривога всієї області / міста)
   Антидребезг:    відбій і новий старт тієї ж території з паузою ≤ 120 с показуються одним епізодом (початок першого, кінець
                   останнього). Пауза НЕ входить у тривалість, реальний кінець не зсувається, поточний статус не продовжується.
   Стани території: active | cleared | no_data | stale. Час початку і тривалість показуються лише коли їх дає джерело. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.AlertState = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DEBOUNCE_S = 120;
  const AIR_KINDS = new Set(['', 'air_raid']);

  const alertKey = (slug, raion, started) => 'alert:' + slug + ':' + (raion || '*') + ':' + started;

  /** Рядок журналу → запис. Повертає null для сміття; не-повітряні типи (обстріли, бої) відфільтровуються окремо. */
  function parseRow(r) {
    if (!Array.isArray(r) || r.length < 9) return null;
    const [srcId, slug, type, title, raion, level, kind, started, ended] = r;
    if (!slug || !(started > 0)) return null;
    const scope = type === 'raion' ? 'raion' : type === 'oblast' ? 'oblast' : type === 'city' ? 'city' : 'other';
    const name = scope === 'raion' ? String(title || raion || '').replace(/\s*район$/i, '') : scope === 'city' ? String(title || '').replace(/^м\.\s*/i, '') : '';
    return { srcId, slug, scope, name, level: level || 'red', kind: kind || '', started: +started, ended: ended > 0 ? +ended : null };
  }

  /** Рядки → Map ключ → запис. Ідемпотентно: однакові рядки не створюють дублів, пізніший рядок з тим самим ключем перемагає. */
  function buildAlerts(rows) {
    const m = new Map();
    (rows || []).forEach(r => {
      const a = parseRow(r); if (!a || !AIR_KINDS.has(a.kind)) return;
      a.key = alertKey(a.slug, a.scope === 'oblast' ? '' : a.name, a.started);
      const prev = m.get(a.key);
      const rank = x => (x.ended === null ? -1 : x.ended);      // завершений запис важливіший за відкритий, пізніший кінець за ранній
      if (!prev || rank(a) >= rank(prev)) m.set(a.key, a);
    });
    return m;
  }

  /** Антидребезг: епізоди по території. Повертає масив {territory, started, ended|null, activeSeconds, parts, keys[], level}. */
  function buildEpisodes(alerts, debounceS) {
    const gap = debounceS == null ? DEBOUNCE_S : debounceS;
    const by = new Map();
    [...alerts.values()].forEach(a => { const t = a.slug + '|' + a.scope + '|' + a.name; (by.get(t) || by.set(t, []).get(t)).push(a); });
    const out = [];
    by.forEach((list, territory) => {
      list.sort((x, y) => x.started - y.started);
      let cur = null;
      list.forEach(a => {
        const dur = a.ended ? a.ended - a.started : 0;
        if (cur && cur.ended !== null && a.started - cur.ended <= gap && a.started >= cur.ended) {
          cur.ended = a.ended; cur.activeSeconds += dur; cur.parts++; cur.keys.push(a.key); cur.level = a.level; cur.last = a;
        } else {
          if (cur) out.push(cur);
          cur = { territory, slug: a.slug, scope: a.scope, name: a.name, started: a.started, ended: a.ended, activeSeconds: dur, parts: 1, keys: [a.key], level: a.level, last: a };
        }
      });
      if (cur) out.push(cur);
    });
    return out.sort((x, y) => y.started - x.started);
  }

  /** Епізоди, що стосуються району. slug/stem району та його назва передаються викликачем; місто/область збігаються за slug. */
  function episodesFor(episodes, district) {
    return episodes.filter(e => e.slug === district.slug && (
      e.scope === 'oblast' ||
      (e.scope === 'raion' && e.name === district.name) ||
      (e.scope === 'city' && district.city && e.name === district.name)));
  }

  /** Стан території. ctx: {hasOfficial, stale, active:'alert'|'warn'|null, episodes, now}. Статус `active` бере ТІЛЬКИ з поточного офіційного контракту. */
  function stateFor(ctx) {
    if (!ctx.hasOfficial) return { state: 'no_data' };
    if (ctx.stale) return { state: 'stale' };
    const eps = ctx.episodes || [];
    if (ctx.active) {
      const open = eps.filter(e => e.ended === null).sort((a, b) => a.started - b.started)[0];
      // час початку лише за достовірним відкритим епізодом; якщо журнал його не містить, час не вигадуємо
      return { state: 'active', level: ctx.active, since: open ? open.started : null, duration: open ? Math.max(0, open.activeSeconds + ctx.now - open.last.started) : null };
    }
    const last = eps.filter(e => e.ended !== null).sort((a, b) => b.ended - a.ended)[0];
    return { state: 'cleared', lastEnded: last ? last.ended : null, lastDuration: last ? last.activeSeconds : null };
  }

  /** Список для вкладки «Історія»: активні і завершені за вікно, новіші першими. */
  function recent(episodes, now, windowS) {
    const w = windowS || 6 * 3600;
    return episodes.filter(e => e.ended === null || now - e.ended <= w);
  }

  /** Сигнатура множини активних ключів: щоб клієнт вирішував, чи треба оновлювати історію. */
  function signature(alerts) { return [...alerts.values()].filter(a => !a.ended).map(a => a.key).sort().join(','); }

  return { DEBOUNCE_S, alertKey, parseRow, buildAlerts, buildEpisodes, episodesFor, stateFor, recent, signature };
});
