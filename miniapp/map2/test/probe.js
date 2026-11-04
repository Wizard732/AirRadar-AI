// Ручна перевірка розкладки в браузері: eval(await (await fetch('/map2/test/probe.js')).text()) → результат останнього виразу.
(async () => {
  const $ = id => document.getElementById(id), sleep = ms => new Promise(r => setTimeout(r, ms));
  const R = e => { const r = e.getBoundingClientRect(); return { l: Math.round(r.left), t: Math.round(r.top), r: Math.round(r.right), b: Math.round(r.bottom), w: Math.round(r.width), h: Math.round(r.height) }; };
  const vis = e => { const r = e.getBoundingClientRect(), cs = getComputedStyle(e); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  await sleep(1500);
  const out = { vp: [innerWidth, innerHeight], issues: [] };
  const W = innerWidth, H = innerHeight;
  const inside = (name, e) => { if (!e || !vis(e)) return; const r = R(e); if (r.l < -1 || r.t < -1 || r.r > W + 1 || r.b > H + 1) out.issues.push(name + ' виходить за екран ' + JSON.stringify(r)); };
  if (document.documentElement.scrollWidth > W + 1) out.issues.push('горизонтальний скрол ' + document.documentElement.scrollWidth);
  const overlap = (a, b) => { const x = R(a), y = R(b); return x.l < y.r && x.r > y.l && x.t < y.b && x.b > y.t; };
  const parts = { brand: document.querySelector('.brand'), search: document.querySelector('.search'), my: $('my'), fresh: $('fresh'), tools: document.querySelector('.tools'), sheet: $('sheet') };
  Object.entries(parts).forEach(([k, e]) => inside(k, e));
  const top = ['brand', 'search', 'my', 'fresh']; for (let i = 0; i < top.length; i++) for (let j = i + 1; j < top.length; j++) if (vis(parts[top[i]]) && vis(parts[top[j]]) && overlap(parts[top[i]], parts[top[j]])) out.issues.push('перекриття ' + top[i] + '/' + top[j]);
  if (vis(parts.tools) && vis(parts.sheet) && overlap(parts.tools, parts.sheet)) out.issues.push('інструменти перекривають шторку');
  if (vis(parts.tools) && vis(parts.my) && overlap(parts.tools, parts.my)) out.issues.push('інструменти перекривають «Моя територія»');
  const m = R($('map')); if (m.w !== W || m.h !== H) out.issues.push('карта не на весь екран ' + JSON.stringify(m));
  // цілі дотику
  const small = [...document.querySelectorAll('button,input,select,[role=button]')].filter(e => vis(e) && !e.closest('[hidden]') && !(e.tagName === 'path') ).map(e => [e, R(e)]).filter(([, r]) => Math.min(r.w, r.h) < 44).map(([e, r]) => (e.id || e.className || e.tagName) + ' ' + r.w + 'x' + r.h);
  out.smallTargets = small;
  out.sheet = vis(parts.sheet) ? R(parts.sheet) : null; out.tools = R(parts.tools);
  // сценарій: рядок → картка → закриття → шторка повернулась у той самий стан
  const hBefore = vis(parts.sheet) ? R(parts.sheet).h : 0;
  const row = document.querySelector('#list .row'); if (row) { row.click(); await sleep(600); document.getAnimations().forEach(a => { try { a.finish(); } catch (_) {} });   // у фоновій вкладці анімації можуть стояти
    const card = $('card'); out.card = R(card); inside('card', card);
    if (vis(parts.tools) && overlap(parts.tools, card) && innerWidth < 900 && innerHeight > 500) out.issues.push('інструменти перекривають картку');
    out.focusOnClose = document.activeElement === card.querySelector('.x');
    card.querySelector('.x').click(); await sleep(400);
    out.sheetRestored = vis(parts.sheet) ? R(parts.sheet).h === hBefore : null;
    out.focusRestored = document.activeElement === row || !document.contains(row);
  }
  out.ok = out.issues.length === 0;
  return out;
})()
