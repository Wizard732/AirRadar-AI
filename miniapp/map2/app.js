/* map2/app.js: інтерфейс нової карти AirRadar. Дані: AirData (публічний контракт), карта: AirMap (layers.js). */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const P = new URLSearchParams(location.search);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  // desktop ≥900 px; land: телефон у landscape (висота ≤500, ширина <1000; бічна шторка); mobile: нижня шторка
  const layout = () => (innerHeight <= 500 && innerWidth > innerHeight && innerWidth < 1000) ? 'land' : innerWidth >= 900 ? 'desktop' : 'mobile';
  const mobile = () => layout() === 'mobile';
  const MY_KEY = 'airradar.map2.my', FAV_KEY = 'airradar.map2.fav', NOTIFY_KEY = 'airradar.map2.notify', PREFS_KEY = 'airradar.map2.prefs', SHEL_KEY = 'airradar.map2.shel', BASE_KEY = 'airradar.map2.base';
  const lsGet = k => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } };
  const lsJson = k => { try { return JSON.parse(lsGet(k) || 'null'); } catch (e) { return null; } };

  // ---------- вигляд: тема і розмір тексту ----------
  let prefs = UiState.normalizePrefs(lsJson(PREFS_KEY));
  const resolvedTheme = () => prefs.theme === 'auto' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : prefs.theme;
  function applyPrefs() {
    const r = document.documentElement; r.dataset.theme = prefs.theme; r.style.setProperty('--k', UiState.SCALE[prefs.size]);
    const m = document.querySelector('meta[name="theme-color"]'); if (m) m.content = resolvedTheme() === 'light' ? '#eef2f6' : '#0e1318';
  }
  applyPrefs();
  try { matchMedia('(prefers-color-scheme: light)').addEventListener('change', applyPrefs); } catch (e) { /* стара версія */ }

  let amap, data = null, fav = Watch.normalizeFav(lsJson(FAV_KEY)), notifyCfg = Watch.normalizeNotify(lsJson(NOTIFY_KEY)), replayT = null, cardMode = null, feats = [], infoById = new Map(), selId = null, myId = null;
  try { myId = localStorage.getItem(MY_KEY); } catch (e) { /* ignore */ }

  let episodes = [], history = null, tab = 'feed', lastFocus = null;
  if (P.get('demo') === 'attack') window.AIRRADAR_ATTACK_T0 = Date.now();   // спільний початок сценарію для тривог (data.js) і цілей (tracks.js)
  const air = new AirData({ demo: P.has('demo'), staticOnly: P.has('static') });
  window.__air = air;                                         // налагодження (як і __amap): емуляція оновлень у перевірках

  // ---------- карта ----------
  amap = new AirMap($('map'), { basemap: lsGet(BASE_KEY) || (resolvedTheme() === 'light' ? 'light' : 'dark'), onSelect: id => openCard(id) });
  window.__amap = amap;
  fitForUi();
  fetch('../raions.json').then(r => { if (!r.ok) throw new Error('raions ' + r.status); return r.json(); }).then(g => {
    feats = g.features;
    feats.forEach(f => infoById.set(AirMap.districtId(f.properties), f.properties));
    amap.loadGeo(g);
    if (data) applyData();
    fav = Watch.normalizeFav(fav, infoById);
    if (typeof tgtApply === 'function') setTimeout(tgtApply, 0);
    const dl = UiState.districtFromSearch(location.search, infoById);
    if (dl) { amap.select(dl); amap.flyTo(dl, flyPad()); openCard(dl); }
    else if (UiState.hasDistrictParam(location.search)) { syncUrl(null); flash('Район за посиланням не знайдено'); }
    if (lsGet(SHEL_KEY) === '1') setShelOn(true);
    if (P.get('card')) { const id = [...infoById.keys()].find(k => k.endsWith('|Харківська')) || [...infoById.keys()][0]; amap.select(id); openCard(id); }
  }).catch(() => showError());

  function fitForUi() {
    amap.invalidate();
    const L = layout();
    if (L === 'mobile') amap.fitCountry({ tl: [8, 120], br: [76, 220] });
    else if (L === 'land') amap.fitCountry({ tl: [336, 64], br: [64, 8] });
    else amap.fitCountry({ tl: [392, 70], br: [76, 20] });
  }
  // resize/орієнтація: перерахунок розміру без скидання вигляду, якщо користувач уже рухав карту
  // (адресний рядок мобільного браузера теж викликає resize; повторний fit збивав би зум)
  let resizeT = null;
  function onResize() { amap.invalidate(); if (!amap.userMoved) fitForUi(); sizeTools(); }
  addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(onResize, 120); });
  addEventListener('orientationchange', () => { clearTimeout(resizeT); resizeT = setTimeout(onResize, 250); });
  if (window.visualViewport) { const upd = () => document.documentElement.style.setProperty('--vvh', window.visualViewport.height + 'px'); window.visualViewport.addEventListener('resize', upd); upd(); }

  // ---------- дані ----------
  air.on('data', d => { data = d; applyData(); }).on('conn', onConn).on('history', h => {
    history = h;
    episodes = h ? AlertState.buildEpisodes(AlertState.buildAlerts(h.rows)) : [];
    if (data && feats.length) { renderMy(); if (selId) renderCard(selId); if (tab === 'history') renderHistory(); if (tab === 'stats') renderStats(); if (replayT != null) applyMap(); }
  });
  air.start();
  if (P.get('demo') === 'attack') setInterval(() => air.retry(), 2000);   // у сценарії статуси тривог змінюються швидко

  // ---------- фільтри ----------
  const FKEY = 'airradar.map2.filters';
  let F = (function () { try { return Filters.normalize(JSON.parse(localStorage.getItem(FKEY) || 'null')); } catch (e) { return Filters.defaults(); } })();
  const saveFilters = () => { try { localStorage.setItem(FKEY, JSON.stringify(F)); } catch (e) { /* ignore */ } };
  const visibleRows = () => Filters.applyRows(Filters.allRows(data, infoById), F);

  function applyMap() { const changed = amap.setStatuses(replayT != null ? replayStatuses(replayT) : Filters.toStatuses(visibleRows())); window.__lastChanged = changed; return changed; }

  /** Зміна фільтрів: оновлює карту і панель. Вибір, зум, підкладка, відкрита картка не чіпаються. */
  function setFilters(patch) {
    F = Filters.normalize(Object.assign({}, F, patch)); saveFilters();
    if (data && feats.length) applyMap();
    syncChips(); updateBadge();
    if (tab === 'feed' && data) { $('list').dataset.sig = ''; renderList(); }
    if (tab === 'filters') renderFilters();
  }
  function syncChips() {
    const k = F.official && F.hint ? 'all' : F.official ? 'official' : F.hint ? 'hint' : '';
    $('chips').querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', String(c.dataset.f === k)));
  }
  function updateBadge() {
    const n = Filters.activeCount(F), b = document.querySelector('[data-tab="filters"]');
    b.innerHTML = 'Фільтри' + (n ? ' <span class="badge" aria-label="активних фільтрів: ' + n + '">' + n + '</span>' : '');
  }

  function applyData() {
    $('loading').hidden = true; $('err').hidden = true;
    if (!feats.length) return;
    applyMap();
    document.documentElement.toggleAttribute('data-stale', !!data.stale);   // застарілі дані: заливки тьмяніють
    renderList(); renderMy(); renderFresh();
    if (selId) renderCard(selId);
    if (tab === 'history') renderHistory();
    if (tab === 'stats') renderStats();
    if (tab === 'mine') renderMine();
    watchTick();
  }

  function pluralOblast(n) { const m10 = n % 10, m100 = n % 100; return m10 === 1 && m100 !== 11 ? 'область' : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'області' : 'областей'); }
  function renderList() {
    if (tab !== 'feed') return;
    const rows = visibleRows();
    const nOff = new Set(rows.filter(r => r.kind === 'official' && r.level === 'alert').map(r => r.stem)).size;
    const nHint = rows.filter(r => r.kind === 'hint').length;
    $('sumN').textContent = nOff;
    $('sumT').textContent = pluralOblast(nOff) + ' у тривозі' + (nHint ? ' · неофіційних: ' + nHint : '') + (Filters.isDefault(F) ? '' : ' · фільтри активні');
    const list = $('list');
    if (!rows.length) {
      const msg = !data.hasOfficial ? 'Немає офіційних даних. Орієнтуйтесь на сирену'
        : data.stale ? 'Немає свіжих даних. Орієнтуйтесь на сирену'
        : Filters.isDefault(F) ? 'Активних тривог і повідомлень немає' : 'За вибраними фільтрами нічого немає';
      list.innerHTML = '<div class="empty">' + msg + (!Filters.isDefault(F) ? '<br><button class="btn-reset" id="f-reset-empty">Скинути фільтри</button>' : '') + '</div>'; list.dataset.sig = '';
      const rb = $('f-reset-empty'); if (rb) rb.onclick = () => setFilters(Filters.defaults());
      return;
    }
    const by = {};
    rows.forEach(r => (by[r.stem] = by[r.stem] || []).push(r));
    let h = '';
    Object.keys(by).sort((x, y) => x.localeCompare(y, 'uk')).forEach(o => {
      h += '<div class="grp">' + esc(o) + ' · ' + by[o].length + '</div>';
      by[o].forEach(r => {
        const a = r.kind === 'official';
        const old = !a && r.hint.stale;
        h += '<button class="row' + (old ? ' old' : '') + '" data-id="' + esc(r.id) + '"><span class="mark ' + (a ? 'a' : 'h') + '"><svg class="ic" style="width:18px;height:18px"><use href="#' + (a ? 'i-siren' : 'i-hint') + '"/></svg></span>' +
          '<span class="t"><b>' + esc(r.p.r) + (a ? '' : '<span class="tag" style="color:var(--hint)">неофіційно</span>') + '</b><small>' +
          (a ? (r.level === 'warn' ? 'Загроза поруч (жовтий рівень)' : 'Офіційна повітряна тривога') : esc(r.hint.category) + (r.hint.count ? ' · ' + esc(r.hint.count) : '') + (old ? ' · дані застарівають' : '')) +
          '</small></span><time>' + (a ? '' : hhmm(r.hint.updated)) + '</time></button>';
      });
    });
    const top = list.scrollTop;
    if (list.dataset.sig !== h) { list.innerHTML = h; list.dataset.sig = h; list.scrollTop = top; }   // без скачків, якщо нічого не змінилось
    list.querySelectorAll('.row').forEach(b => b.onclick = () => { lastFocus = b; amap.select(b.dataset.id); amap.flyTo(b.dataset.id, flyPad()); openCard(b.dataset.id); });
  }

  $('chips').addEventListener('click', e => {
    const b = e.target.closest('.chip'); if (!b) return;
    setFilters(b.dataset.f === 'all' ? { official: true, hint: true } : b.dataset.f === 'official' ? { official: true, hint: false } : { official: false, hint: true });
  });
  syncChips(); updateBadge();

  // ---------- вкладка «Фільтри» ----------
  function renderFilters() {
    const list = $('list'), act = document.activeElement, key = act && (act.dataset.sw || act.dataset.cat || act.id) || '';
    const regions = [...new Set([...infoById.keys()].map(k => k.split('|')[1]))].sort((x, y) => x.localeCompare(y, 'uk'));
    const sw = (k, label, sub) => '<button class="swrow" role="switch" data-sw="' + k + '" aria-checked="' + F[k] + '"><span>' + label + (sub ? '<small>' + sub + '</small>' : '') + '</span><i class="knob"></i></button>';
    list.dataset.sig = '';
    list.innerHTML = '<div class="fgrp"><h4>Що показувати</h4>' + sw('official', 'Офіційні тривоги', 'alerts.in.ua') + sw('hint', 'Неофіційні повідомлення', 'не підтверджено') + '</div>' +
      '<div class="fgrp' + (F.hint ? '' : ' off') + '"><h4>Категорії неофіційних</h4><div class="chips" style="padding:0">' +
      Filters.CATEGORIES.map(c => '<button class="chip" data-cat="' + c + '" aria-pressed="' + F.cats.includes(c) + '"' + (F.hint ? '' : ' disabled') + '>' + c + '</button>').join('') +
      '</div><small class="hintline">Нічого не вибрано: показуються всі категорії</small></div>' +
      '<div class="fgrp"><h4>Область</h4><select id="f-region" aria-label="Область"><option value="">Усі області</option>' + regions.map(r => '<option value="' + esc(r) + '"' + (F.region === r ? ' selected' : '') + '>' + esc(r) + '</option>').join('') + '</select></div>' +
      '<div class="fgrp">' + sw('hideStale', 'Приховати застарілі неофіційні', 'без свіжих оновлень') + '</div>' +
      '<div class="fgrp"><button class="btn-reset" id="f-reset"' + (Filters.isDefault(F) ? ' disabled' : '') + '>Скинути фільтри</button><small class="hintline">Фільтри не змінюють карту, зум і відкриту картку. Картка завжди показує справжній стан території.</small></div>';
    const el = key && (list.querySelector('[data-sw="' + key + '"]') || list.querySelector('[data-cat="' + key + '"]') || list.querySelector('#' + key)); if (el && !el.disabled) el.focus();
  }
  $('list').addEventListener('click', e => {
    if (tab !== 'filters') return;
    const sw = e.target.closest('[data-sw]'), cat = e.target.closest('[data-cat]'), rs = e.target.closest('#f-reset');
    if (sw) setFilters({ [sw.dataset.sw]: !F[sw.dataset.sw] });
    else if (cat && !cat.disabled) setFilters({ cats: F.cats.includes(cat.dataset.cat) ? F.cats.filter(c => c !== cat.dataset.cat) : F.cats.concat(cat.dataset.cat) });
    else if (rs && !rs.disabled) setFilters(Filters.defaults());
  });
  $('list').addEventListener('change', e => { if (tab === 'filters' && e.target.id === 'f-region') setFilters({ region: e.target.value }); });

  const hhmm = ts => ts ? new Date(Math.floor(ts / 300) * 300 * 1000).toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit' }) : '';
  const flyPad = () => { const L = layout(); return L === 'mobile' ? { tl: [16, 130], br: [76, 360] } : L === 'land' ? { tl: [340, 70], br: [70, 20] } : { tl: [400, 90], br: [100, 40] }; };


  // ---------- стан території (alerts-state.js) ----------
  const fmtTime = ts => new Date(ts * 1000).toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Kyiv' });
  const fmtDur = sec => { const m = Math.max(1, Math.round(sec / 60)); return m < 60 ? m + ' хв' : Math.floor(m / 60) + ' год' + (m % 60 ? ' ' + (m % 60) + ' хв' : ''); };
  function territory(id) {
    if (!data) return { state: 'no_data' };
    const p = infoById.get(id), meta = AirData.districtMeta(id, p);
    return AlertState.stateFor({ hasOfficial: data.hasOfficial, stale: data.stale, active: statusOf(id), episodes: AlertState.episodesFor(episodes, meta), now: Math.floor(Date.now() / 1000) });
  }

  // ---------- Моя територія ----------
  function statusOf(id) {
    if (!data) return null;
    const stem = id.split('|')[1];
    return data.official.raions.get(id) || (data.official.oblasts.has(stem) ? 'alert' : null);
  }
  function renderMy() {
    const el = $('my'), t = $('myT');
    el.classList.remove('alert', 'nodata');
    if (!myId || !infoById.has(myId)) { t.textContent = 'Оберіть район'; el.classList.add('nodata'); return; }
    const name = infoById.get(myId).r;
    const T = territory(myId);
    if (T.state === 'no_data') { el.classList.add('nodata'); t.textContent = 'Немає офіційних даних · ' + name; }
    else if (T.state === 'stale') { el.classList.add('nodata'); t.textContent = 'Немає свіжих даних · ' + name; }
    else if (statusOf(myId) === 'alert') { el.classList.add('alert'); t.textContent = 'ТРИВОГА · ' + name; }
    else if (statusOf(myId) === 'warn') { el.classList.add('alert'); t.textContent = 'Загроза поруч · ' + name; }
    else t.textContent = 'Тривоги немає · ' + name;
  }
  $('my').onclick = async () => {
    if (myId && infoById.has(myId)) { amap.select(myId); amap.flyTo(myId, flyPad()); openCard(myId); return; }
    await locateMe(true);
  };

  async function locateMe(save) {
    try {
      const pos = await amap.locate();
      const id = amap.districtAt(pos.lat, pos.lon);          // координати далі не зберігаються і не передаються
      if (!id) { flash('Ви поза межами карти України'); return; }
      if (save) { myId = id; try { localStorage.setItem(MY_KEY, id); } catch (e) { /* ignore */ } renderMy(); }
      amap.select(id); amap.flyTo(id, flyPad()); openCard(id);
    } catch (e) { flash('Не вдалося визначити місцезнаходження'); }
  }
  $('t-loc').onclick = () => locateMe(!myId);

  // ---------- свіжість і стани зв'язку ----------
  function renderFresh() {
    const f = $('fresh'), t = $('freshT');
    f.classList.remove('stale', 'off');
    if (!data) { t.textContent = 'завантаження'; return; }
    const age = Math.max(0, Math.floor(Date.now() / 1000) - data.updatedAt);
    t.textContent = 'оновлено ' + (age < 90 ? age + ' с' : Math.round(age / 60) + ' хв') + ' тому';
    if (air.conn === 'offline') f.classList.add('off'); else if (data.stale) f.classList.add('stale');
  }
  setInterval(() => { if (data) { renderFresh(); if (air.conn === 'offline' || data.stale) syncBanner(); } }, 15000);

  /** «5 хв тому» для збережених даних: вік завжди показуємо явно. */
  function ageText(ts) { const a = Math.max(0, Math.floor(Date.now() / 1000) - (ts || 0)); return (a < 90 ? a + ' с' : a < 5400 ? Math.round(a / 60) + ' хв' : Math.round(a / 3600) + ' год') + ' тому'; }
  let flashUntil = 0;
  function banner(html) { const b = $('banner'); if (!html) { b.hidden = true; return; } b.hidden = false; b.innerHTML = '<svg class="ic"><use href="#i-hint"/></svg>' + html; }
  function flash(msg, ms) { ms = ms || 3500; flashUntil = Date.now() + ms; banner(esc(msg)); setTimeout(() => { if ($('banner').textContent.includes(msg)) syncBanner(); }, ms); }
  function syncBanner() {
    if (Date.now() < flashUntil) return;
    if (air.conn === 'offline') banner('Немає зв’язку. Показано збережені дані, оновлені ' + ageText(data && data.updatedAt) + (data && data.stale ? '. Орієнтуйтесь на сирену' : ''));
    else if (air.conn === 'recovered') banner('Зв’язок відновлено');
    else if (data && data.stale) banner('Дані застарілі. Орієнтуйтесь на сирену');
    else banner('');
  }
  function onConn(c) {
    if (c === 'error') showError();
    else { $('err').hidden = true; if (data) { renderFresh(); renderMy(); } syncBanner(); }
  }
  function showError() {
    $('loading').hidden = true; $('err').hidden = false;
    // чесна причина: порожню карту не можна видавати за «тривог немає»
    $('errT').textContent = navigator.onLine === false ? 'Немає з’єднання з інтернетом. Збережених даних ще немає.' : 'Дані не завантажились. Спробуємо ще раз автоматично.';
  }
  $('retry').onclick = () => { $('err').hidden = true; $('loading').hidden = false; air.retry(); };
  // окремий Service Worker карти (scope map2/): лише код і геометрія, дані тривог він не кешує
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || location.hostname === 'localhost') && !P.has('demo') && !window.AIRRADAR_DATA_BASE) {
    addEventListener('load', () => navigator.serviceWorker.register('sw.js', { scope: './' }).catch(() => {}));
  }

  // ---------- картка території ----------
  /** Обмеження джерела: Київ-місто і Київська область (kyivska), Севастополь і Крим (crimea) приходять як один slug. Не подаємо як точний поділ. */
  function sharedNote(id) {
    const slug = AirData.districtMeta(id, infoById.get(id)).slug;
    if (slug === 'kyivska') return '<div class="note">Офіційне джерело не розділяє тривогу міста Києва і Київської області: позначаються обидві території.</div>';
    if (slug === 'crimea') return '<div class="note">Для Криму і Севастополя джерело дає одну тривогу: позначаються обидві території.</div>';
    return '';
  }
  function openCard(id) {
    const a = document.activeElement; lastFocus = a && a !== document.body ? a : lastFocus;
    selId = id; cardMode = 'district'; syncUrl(id); renderCard(id);
    const x = $('card').querySelector('.x'); if (x) x.focus({ preventScroll: true });   // фокус на картку; закриття повертає його назад
  }
  function renderCard(id) {
    const p = infoById.get(id); if (!p) return;
    const c = $('card'); c.hidden = false; document.body.classList.add('has-card'); cardMode = 'district';
    if (replayT != null) return renderReplayCard(id, p);
    const st = statusOf(id), hint = data && data.hints.get(id), stale = data && data.stale;
    const T = territory(id);
    let official;
    if (T.state === 'no_data' || T.state === 'stale') official = '<div class="blk"><h3><span>Офіційна тривога</span></h3><div class="big" style="color:var(--nodata)">Немає офіційних даних</div><div class="kv"><span>' + (T.state === 'stale' ? 'Дані застарілі. ' : '') + 'Орієнтуйтесь на сирену</span></div></div>';
    else if (T.state === 'active') official = '<div class="blk a"><h3><span>Офіційна тривога</span><span>alerts.in.ua</span></h3><div class="big">' + (T.level === 'warn' ? 'Загроза поруч' : 'Триває повітряна тривога') + '</div>' +
      (T.since ? '<div class="kv"><span>Початок</span><b>' + fmtTime(T.since) + '</b></div>' : '') + (T.duration != null ? '<div class="kv"><span>Триває</span><b>' + fmtDur(T.duration) + '</b></div>' : '') +
      '<div class="kv"><span>Рівень</span><b>' + (T.level === 'warn' ? 'жовтий' : 'червоний') + '</b></div>' + sharedNote(id) + '</div>';
    else official = '<div class="blk"><h3><span>Офіційна тривога</span></h3><div class="big" style="color:var(--ok)">Тривоги немає</div>' + (T.lastEnded ? '<div class="kv"><span>Останній відбій</span><b>' + fmtTime(T.lastEnded) + (T.lastDuration ? ' · тривала ' + fmtDur(T.lastDuration) : '') + '</b></div>' : '') + '</div>';
    const unof = hint
      ? '<div class="blk h"><h3><span>Неофіційні повідомлення</span><span class="tag" style="color:var(--hint)">не підтверджено</span></h3><div class="kv"><span>Категорія</span><b>' + esc(hint.category) + ' <button class="lnk" data-guide="' + esc(hint.category) + '">що це?</button></b></div>' + (hint.count ? '<div class="kv"><span>Кількість</span><b>' + esc(hint.count) + '</b></div>' : '') + '<div class="kv"><span>Оновлено</span><b>≈ ' + hhmm(hint.updated) + (hint.stale ? ' · застаріло' : '') + '</b></div></div>'
      : '<div class="blk"><h3><span>Неофіційні повідомлення</span></h3><div class="kv"><span>Немає активних повідомлень</span></div></div>';
    c.innerHTML = '<button class="x" aria-label="Закрити картку"><svg class="ic"><use href="#i-close"/></svg></button><h2>' + esc(p.r) + ' район</h2><div class="sub">' + esc(p.o) + (/Крим|Київ|Севастополь/.test(p.o) ? '' : ' область') + '</div>' + official + unof + tgtWarnHtml(id) + cardActions(id);
    c.querySelector('.x').onclick = closeCard;
    setCardH();
  }
  function cardActions(id) {
    const f = fav.includes(id);
    return '<div class="cacts"><button data-fav="' + esc(id) + '" aria-pressed="' + f + '"><svg class="ic"><use href="#i-star"/></svg>' + (f ? 'В обраному' : 'В обране') + '</button>' +
      '<button data-share="' + esc(id) + '"><svg class="ic"><use href="#i-share"/></svg>Поділитись</button>' +
      '<button data-shel="' + esc(id) + '"><svg class="ic"><use href="#i-shelter"/></svg>Укриття</button></div>';
  }
  /** Картка в режимі історії: показує стан на вибраний момент за журналом, а не поточний. */
  function renderReplayCard(id, p) {
    const c = $('card'), on = Stats.activeAt(episodes, replayT), eps = AlertState.episodesFor(on, AirData.districtMeta(id, p));
    c.innerHTML = '<button class="x" aria-label="Закрити картку"><svg class="ic"><use href="#i-close"/></svg></button><h2>' + esc(p.r) + ' район</h2><div class="sub">' + esc(p.o) + (/Крим|Київ|Севастополь/.test(p.o) ? '' : ' область') + '</div>' +
      '<div class="blk' + (eps.length ? ' a' : '') + '"><h3><span>Історія · ' + fmtDT(replayT) + '</span><span>журнал</span></h3><div class="big" style="color:var(--' + (eps.length ? 'alert' : 'ok') + ')">' + (eps.length ? 'Була повітряна тривога' : 'Тривоги не було') + '</div>' +
      '<div class="note">Це стан на вибраний момент за журналом офіційних тривог, а не поточна обстановка. Неофіційні повідомлення в історії не зберігаються.</div></div>' +
      '<div class="cacts"><button data-replay-exit="1">До поточних даних</button><button data-share="' + esc(id) + '"><svg class="ic"><use href="#i-share"/></svg>Поділитись</button></div>';
    c.querySelector('.x').onclick = closeCard; setCardH();
  }
  const setCardH = () => document.documentElement.style.setProperty('--cardH', $('card').getBoundingClientRect().height + 'px');
  if (window.ResizeObserver) { new ResizeObserver(() => { if (!$('card').hidden) setCardH(); }).observe($('card')); new ResizeObserver(() => sizeTools()).observe($('sheet')); }
  function closeCard() {
    selId = null; cardMode = null; syncUrl(null); $('card').hidden = true; document.body.classList.remove('has-card'); amap.select(null);
    sizeTools();                                                // шторка повертається в той самий стан (peek/half/full), у якому була
    if (lastFocus && document.contains(lastFocus)) { try { lastFocus.focus({ preventScroll: true }); } catch (e) { /* ignore */ } }
    lastFocus = null;
  }
  addEventListener('keydown', e => { if (e.key === 'Escape') { if (!$('basepick').hidden) toggleBase(false); else if (!$('results').hidden) { $('results').hidden = true; } else closeCard(); } });


  // ---------- вкладки: Стрічка / Історія ----------
  document.querySelector('.tabs').addEventListener('click', e => {
    const b = e.target.closest('button[data-tab]'); if (!b || b.disabled) return;
    if (tab === 'history' && b.dataset.tab !== 'history') exitReplay();     // історичний вигляд не лишається на карті після виходу з вкладки
    tab = b.dataset.tab; $('list').dataset.hist = '';
    document.querySelectorAll('.tabs button').forEach(x => x.setAttribute('aria-selected', String(x === b)));
    document.querySelector('.sum').hidden = document.getElementById('chips').hidden = tab !== 'feed';
    if (tab === 'feed') { $('list').dataset.sig = ''; if (data) renderList(); } else if (tab === 'filters') renderFilters(); else if (tab === 'guide') renderGuide();
    else if (tab === 'mine') renderMine(); else if (tab === 'stats') renderStats(); else renderHistory();
  });
  /** Довідник типів загроз: статичний, не залежить від даних; cat підсвічує й розкриває потрібний тип. */
  function renderGuide(cat) {
    const list = $('list'); list.dataset.sig = '';
    list.innerHTML = '<div class="empty" style="padding:10px 14px;text-align:left">Довідка про типи загроз. Схематичні силуети, не масштаб. Не пов’язано з поточними даними карти.</div>' +
      Guide.TYPES.map(t => '<details class="gtype"' + (cat && t.cat === cat ? ' open' : '') + ' data-cat="' + esc(t.cat) + '"><summary><svg class="sil" viewBox="0 0 100 60" aria-hidden="true" fill="currentColor">' + Guide.SIL[t.sil] + '</svg><span class="t"><b>' + esc(t.name) + '</b><small>' + esc(t.cat) + ' · ' + esc(t.models) + '</small></span></summary>' +
        '<div class="gbody"><p>' + esc(t.what) + '</p><p><b>Ознаки.</b> ' + esc(t.signs) + '</p><p><b>Що робити.</b> ' + esc(t.todo) + '</p></div></details>').join('');
    if (cat) { const o = list.querySelector('details[open]'); if (o && o.scrollIntoView) o.scrollIntoView({ block: 'nearest' }); }
  }
  $('card').addEventListener('click', e => {
    const g = e.target.closest('[data-guide]'); if (g) { openGuide(g.dataset.guide); return; }
    const f = e.target.closest('[data-fav]'); if (f) { toggleFavorite(f.dataset.fav); return; }
    const sh = e.target.closest('[data-share]'); if (sh) { shareDistrict(sh.dataset.share); return; }
    const sl = e.target.closest('[data-shel]'); if (sl) { showSheltersAt(sl.dataset.shel); return; }
    if (e.target.closest('[data-replay-exit]')) { exitReplay(); return; }
    const ss = e.target.closest('[data-ss]'); if (ss) shelAction(ss.dataset.ss);
  });
  function openGuide(cat) {
    const b = document.querySelector('[data-tab="guide"]'); if (selId) closeCard();
    b.click(); renderGuide(cat);
  }
  // ---------- історія: список і таймлайн ----------
  const fmtDT = ts => new Date(ts * 1000).toLocaleString('uk-UA', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Kyiv' });
  const nowS = () => Math.floor(Date.now() / 1000);
  let tlRange = null, playTimer = null;
  /** Статуси карти на момент t за журналом: лише офіційні тривоги, без неофіційних. */
  function replayStatuses(t) {
    const act = Stats.activeAt(episodes, t), m = new Map();
    if (!act.length) return m;
    infoById.forEach((p, id) => {
      const hit = AlertState.episodesFor(act, AirData.districtMeta(id, p));
      if (hit.length) m.set(id, { official: hit.some(e => e.level !== 'yellow') ? 'alert' : 'warn', hint: false });
    });
    return m;
  }
  function enterReplay(t) {
    replayT = t; document.documentElement.dataset.replay = '1';
    $('replaybar').hidden = false; $('replayT').textContent = 'Історія: ' + fmtDT(t) + '. Це не поточна обстановка';
    if (data && feats.length) applyMap();
    if (selId) renderCard(selId);
    syncTlLabel();
  }
  function exitReplay() {
    stopPlay();
    if (replayT == null) { syncTlLabel(); return; }
    replayT = null; delete document.documentElement.dataset.replay; $('replaybar').hidden = true;
    if (data && feats.length) applyMap();
    if (selId) renderCard(selId);
    const tl = $('tl'); if (tl) tl.value = tl.max;
    syncTlLabel();
  }
  $('replayNow').onclick = exitReplay;
  function syncTlLabel() {
    const o = $('tlOut'); if (!o) return;
    o.textContent = replayT == null ? 'Зараз (поточні дані)' : fmtDT(replayT) + ' · ' + fmtDur(nowS() - replayT) + ' тому';
  }
  function stopPlay() { if (playTimer) { clearInterval(playTimer); playTimer = null; } const b = $('tlplay'); if (b) { b.setAttribute('aria-label', 'Відтворити історію'); b.innerHTML = '<svg class="ic"><use href="#i-play"/></svg>'; } }
  function startPlay() {
    const tl = $('tl'); if (!tl) return;
    const b = $('tlplay'); b.setAttribute('aria-label', 'Зупинити відтворення'); b.innerHTML = '<svg class="ic"><use href="#i-pause"/></svg>';
    let t = replayT == null ? +tl.min : replayT;
    const step = () => { t += 600; if (t >= +tl.max) { exitReplay(); return; } tl.value = t; enterReplay(t); };
    enterReplay(t); tl.value = t; playTimer = setInterval(step, 700);
  }
  function renderHistory() {
    const list = $('list');
    if (!history) { list.innerHTML = '<div class="empty">Журнал тривог недоступний</div>'; list.dataset.hist = ''; return; }
    const now = nowS(), rows = AlertState.recent(episodes, now, 6 * 3600);
    const histNote = history.error ? '<div class="empty" role="status" style="padding:10px 14px;text-align:left">Журнал не оновився ' + ageText(history.fetchedAt) + '. Час і тривалість можуть бути неактуальними.</div>' : '';
    const items = rows.length ? rows.slice(0, 80).map(e => {
      const active = e.ended === null, dur = active ? e.activeSeconds + now - e.last.started : e.activeSeconds;
      const place = e.scope === 'oblast' ? (AirData.SLUG_STEM[e.slug] || e.slug) + ' (вся область)' : e.name + ' район';
      return '<div class="row' + (active ? '' : ' old') + '"><span class="mark a"><svg class="ic" style="width:18px;height:18px"><use href="#i-siren"/></svg></span><span class="t"><b>' + esc(place) + '</b><small>' +
        fmtTime(e.started) + (active ? ' · триває ' : ' – ' + fmtTime(e.ended) + ' · ') + fmtDur(dur) + (e.parts > 1 ? ' · ' + e.parts + ' спрацювання' : '') + '</small></span></div>';
    }).join('') : '<div class="empty">За останні 6 годин тривог не було</div>';
    tlRange = Stats.timelineRange(episodes, now, 24 * 3600, 300);
    if (list.dataset.hist === '1' && $('hlist') && $('tl')) {            // оновлення даних не збиває повзунок під час перетягування
      $('hlist').innerHTML = histNote + items; const tl = $('tl'); tl.min = tlRange.from; tl.max = tlRange.to; if (replayT == null) tl.value = tlRange.to; syncTlLabel(); return;
    }
    list.dataset.sig = ''; list.dataset.hist = '1';
    const canReplay = tlRange.covered && tlRange.from < tlRange.to;
    const tlHtml = '<div class="tl"><h4>Перегляд за часом</h4>' + (canReplay
      ? '<div class="tlrow"><button class="tlb" id="tlplay" aria-label="Відтворити історію"><svg class="ic"><use href="#i-play"/></svg></button><input type="range" id="tl" aria-label="Момент часу в історії" min="' + tlRange.from + '" max="' + tlRange.to + '" step="' + tlRange.step + '" value="' + (replayT == null ? tlRange.to : replayT) + '"></div><output id="tlOut" aria-live="polite"></output>' +
        '<small>Журнал офіційних тривог від ' + fmtDT(tlRange.from) + '. Раніше цього моменту даних немає. Неофіційні повідомлення не зберігаються.</small>'
      : '<small>Журнал порожній або недоступний: переглядати нема що.</small>') + '</div>';
    list.innerHTML = tlHtml + '<div id="hlist">' + histNote + items + '</div>';
    syncTlLabel();
  }
  $('list').addEventListener('input', e => {
    if (e.target.id !== 'tl') return;
    stopPlay(); const v = +e.target.value;
    if (v >= +e.target.max) exitReplay(); else enterReplay(v);
  });
  $('list').addEventListener('click', e => {
    if (tab !== 'history') return;
    if (e.target.closest('#tlplay')) { if (playTimer) stopPlay(); else startPlay(); }
  });

  // ---------- статистика ----------
  let statWin = 24 * 3600;
  function renderStats() {
    const list = $('list'); list.dataset.sig = '';
    if (!history) { list.innerHTML = '<div class="empty">Журнал тривог недоступний</div>'; return; }
    const now = nowS(), S = Stats.summarize(episodes, now, statWin, sl => AirData.SLUG_STEM[sl] || sl);
    const act = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.win : '';
    const seg = '<div class="seg p8" role="group" aria-label="Період">' + [[24 * 3600, 'Остання доба'], [7 * 24 * 3600, '7 днів']].map(w => '<button data-win="' + w[0] + '" aria-pressed="' + (statWin === w[0]) + '">' + w[1] + '</button>').join('') + '</div>';
    const cov = S.coverageFrom && S.coverageFrom > now - statWin ? '<div class="note" style="margin:6px 8px">Журнал охоплює лише від ' + fmtDT(S.coverageFrom) + '. Раніше: даних немає, а не «тривог не було».</div>' : '';
    const max = Math.max(1, ...S.top.map(o => o.count)), hmax = Math.max(1, ...S.hours);
    let h = seg + '<div class="stat3"><div><b>' + S.count + '</b><span>епізодів</span></div><div><b>' + S.territories + '</b><span>територій</span></div><div><b>' + (S.peakHour == null ? '–' : String(S.peakHour).padStart(2, '0') + ':00') + '</b><span>пікова година</span></div></div>' + cov;
    if (!S.count) h += '<div class="empty">За цей період тривог у журналі немає</div>';
    else {
      h += '<div class="fgrp"><h4>Області за кількістю епізодів</h4><div class="bars">' + S.top.map(o => '<div class="bar"><span>' + esc(o.name) + '</span><b>' + o.count + ' · ' + fmtDur(o.sec) + '</b><i style="width:' + Math.max(4, Math.round(o.count / max * 100)) + '%"></i></div>').join('') + '</div></div>' +
        '<div class="fgrp"><h4>За годинами доби (за київським часом)</h4><div class="hours" role="img" aria-label="Епізодів за годинами доби. Піковий час: ' + String(S.peakHour).padStart(2, '0') + ':00">' + S.hours.map((n, i) => '<i title="' + String(i).padStart(2, '0') + ':00 · ' + n + '" style="height:' + Math.max(2, Math.round(n / hmax * 100)) + '%"></i>').join('') + '</div>' +
        '<div class="hlab"><span>00</span><span>06</span><span>12</span><span>18</span><span>23</span></div></div>';
    }
    h += '<div class="fgrp"><small class="hintline">Епізод: тривога однієї території (область, район або місто); короткі перерви до 2 хв об’єднано. Лише офіційні тривоги за журналом; неофіційні повідомлення сюди не входять. Тривалість у списку області сумується по всіх її територіях.</small></div>';
    list.innerHTML = h;
    if (act) { const el = list.querySelector('[data-win="' + act + '"]'); if (el) el.focus(); }
  }
  $('list').addEventListener('click', e => { const b = tab === 'stats' && e.target.closest('[data-win]'); if (b) { statWin = +b.dataset.win; renderStats(); } });

  // ---------- обрані територї ----------
  const saveFav = () => lsSet(FAV_KEY, JSON.stringify(fav));
  function toggleFavorite(id) {
    const r = Watch.toggleFav(fav, id);
    if (r.full) { flash('Можна додати не більше ' + Watch.MAX_FAV + ' обраних територій'); return; }
    fav = r.list; saveFav(); setTimeout(tgtWatch, 0); flash(r.added ? 'Додано до обраного. Увімкніть сповіщення у вкладці «Моє»' : 'Прибрано з обраного');
    if (selId === id) renderCard(id);
    if (tab === 'mine') renderMine();
  }
  function setMy(id) { myId = id; if (id) lsSet(MY_KEY, id); else { try { localStorage.removeItem(MY_KEY); } catch (e) { /* ignore */ } } renderMy(); if (tab === 'mine') renderMine(); setTimeout(tgtWatch, 0); }
  function stateWord(id) {
    const T = territory(id);
    if (T.state === 'no_data' || T.state === 'stale') return { w: 'Немає свіжих даних', a: false, old: true };
    const st = statusOf(id);
    return st === 'alert' ? { w: 'Триває повітряна тривога', a: true } : st === 'warn' ? { w: 'Загроза поруч', a: true } : { w: 'Тривоги немає', a: false };
  }
  function renderMine() {
    const list = $('list'), act = document.activeElement, key = act && act.dataset ? (act.dataset.nt || act.dataset.pref || '') : '';
    list.dataset.sig = '';
    const terr = (id, primary) => {
      const p = infoById.get(id); if (!p) return '';
      const s = data ? stateWord(id) : { w: 'Завантаження', a: false, old: true };
      return '<div class="mrow"><button class="row' + (s.old ? ' old' : '') + '" data-open="' + esc(id) + '"><span class="mark ' + (s.a ? 'a' : '') + '" style="' + (s.a ? '' : 'border:1.5px solid var(--line)') + '"><svg class="ic" style="width:18px;height:18px"><use href="#' + (s.a ? 'i-siren' : 'i-star') + '"/></svg></span>' +
        '<span class="t"><b>' + esc(p.r) + ' район' + (primary ? '<span class="pri">ГОЛОВНА</span>' : '') + '</b><small>' + esc(s.w) + ' · ' + esc(p.o) + '</small></span></button>' +
        (primary ? '<button class="rm" data-unmy="1" aria-label="Прибрати головну територію"><svg class="ic"><use href="#i-close"/></svg></button>'
          : '<button class="rm" data-mk="' + esc(id) + '" aria-label="Зробити ' + esc(p.r) + ' район головною територією"><svg class="ic"><use href="#i-locate"/></svg></button><button class="rm" data-rm="' + esc(id) + '" aria-label="Прибрати ' + esc(p.r) + ' район з обраного"><svg class="ic"><use href="#i-close"/></svg></button>') + '</div>';
    };
    const favs = fav.filter(id => id !== myId && infoById.has(id));
    const sw = (k, label, sub, on) => '<button class="swrow" role="switch" data-nt="' + k + '" aria-checked="' + on + '"><span>' + label + (sub ? '<small>' + sub + '</small>' : '') + '</span><i class="knob"></i></button>';
    const watched = Watch.watchedIds(myId, fav).filter(id => infoById.has(id)).length;
    const seg = (name, opts, cur) => '<div class="seg" role="group" aria-label="' + name + '">' + opts.map(o => '<button data-pref="' + o[0] + '" aria-pressed="' + (cur === o[0].split(':')[1]) + '">' + o[1] + '</button>').join('') + '</div>';
    list.innerHTML = (tgtWarnList.length ? '<div class="fgrp"><h4>Цілі курсом на ваші території</h4>' + tgtWarnHtml(null) + '</div>' : '') +
      '<div class="fgrp"><h4>Моя територія</h4>' + (myId && infoById.has(myId) ? terr(myId, true) : '<div class="empty" style="padding:8px 4px;text-align:left">Не вибрано. Визначте за геолокацією (позиція не зберігається) або зробіть головною одну з обраних.</div><button class="btn-reset" data-nt="locate">Визначити за геолокацією</button>') + '</div>' +
      '<div class="fgrp"><h4>Обрані території · ' + favs.length + '</h4>' + (favs.length ? favs.map(id => terr(id, false)).join('') : '<small class="hintline" style="margin:0">Відкрийте картку території й натисніть «В обране». До ' + Watch.MAX_FAV + ' територій.</small>') + '</div>' +
      '<div class="fgrp"><h4>Сповіщення</h4>' + sw('on', 'Сповіщати про зміну тривоги', 'Початок, загроза, відбій: для головної й обраних', notifyCfg.on) +
      sw('sound', 'Звук і вібрація', '', notifyCfg.sound) + (('Notification' in window) ? sw('system', 'Системні сповіщення', 'Коли вкладка у фоні', notifyCfg.system) : '') +
      '<button class="btn-reset" data-nt="test" style="margin-top:6px">Перевірити сигнал</button>' +
      (notifyCfg.on && !watched ? '<div class="note">Оберіть головну територію або додайте обрані: інакше сповіщати нема про що.</div>' : '') +
      '<small class="hintline">Працює, лише поки карта відкрита. Це не заміна сирені: орієнтуйтесь на сирену й офіційні повідомлення. Фонові сповіщення дає бот або застосунок. Застарілі чи відсутні дані не дають ні «тривога», ні «відбій».</small></div>' +
      '<div class="fgrp"><h4>Вигляд</h4>' + seg('Тема', [['theme:dark', 'Темна'], ['theme:light', 'Світла'], ['theme:auto', 'Авто']], prefs.theme) + '<div style="height:8px"></div>' + seg('Розмір тексту', [['size:m', 'Звичайний'], ['size:l', 'Більший'], ['size:xl', 'Великий']], prefs.size) + '</div>';
    const el = key && (list.querySelector('[data-nt="' + key + '"]') || list.querySelector('[data-pref="' + key + '"]')); if (el) el.focus();
  }
  $('list').addEventListener('click', async e => {
    if (tab !== 'mine') return;
    const op = e.target.closest('[data-open]'); if (op) { lastFocus = op; amap.select(op.dataset.open); amap.flyTo(op.dataset.open, flyPad()); openCard(op.dataset.open); return; }
    const rm = e.target.closest('[data-rm]'); if (rm) { fav = fav.filter(x => x !== rm.dataset.rm); saveFav(); renderMine(); return; }
    const mk = e.target.closest('[data-mk]'); if (mk) { setMy(mk.dataset.mk); return; }
    if (e.target.closest('[data-unmy]')) { setMy(null); return; }
    const pf = e.target.closest('[data-pref]'); if (pf) { const [k, v] = pf.dataset.pref.split(':'); setPrefs({ [k]: v }); return; }
    const nt = e.target.closest('[data-nt]'); if (!nt) return;
    const k = nt.dataset.nt, saveN = () => lsSet(NOTIFY_KEY, JSON.stringify(notifyCfg));
    if (k === 'locate') { await locateMe(true); renderMine(); return; }
    if (k === 'test') { audioCtx(); beep('alert'); vibrate([200]); flash('Перевірка сигналу. Якщо звуку немає, перевірте гучність і беззвучний режим', 5000); return; }
    if (k === 'on') { notifyCfg.on = !notifyCfg.on; if (notifyCfg.on) audioCtx(); }
    else if (k === 'sound') notifyCfg.sound = !notifyCfg.sound;
    else if (k === 'system') {
      if (notifyCfg.system) notifyCfg.system = false;
      else {
        let perm = Notification.permission;
        if (perm === 'default') { try { perm = await Notification.requestPermission(); } catch (er) { perm = 'denied'; } }
        notifyCfg.system = perm === 'granted';
        if (!notifyCfg.system) flash('Дозвіл на системні сповіщення не надано. Працюватимуть сповіщення на сторінці', 5000);
      }
    }
    saveN(); renderMine();
  });
  function setPrefs(patch) {
    const prevTheme = prefs.theme; prefs = UiState.normalizePrefs(Object.assign({}, prefs, patch)); lsSet(PREFS_KEY, JSON.stringify(prefs)); applyPrefs();
    if (prefs.theme !== prevTheme && amap.basemapKey !== 'sat') {              // підкладка слідує за темою, якщо користувач не обрав супутник
      const b = resolvedTheme() === 'light' ? 'light' : 'dark'; amap.setBasemap(b); lsSet(BASE_KEY, b);
      bp.querySelectorAll('button').forEach(x => x.setAttribute('aria-checked', String(x.dataset.k === amap.basemapKey)));
    }
    renderMine(); setTimeout(() => { amap.invalidate(); sizeTools(); }, 0);
  }

  // ---------- сповіщення на сторінці ----------
  let actx = null, prevSnap = null;
  function audioCtx() { try { actx = actx || new (window.AudioContext || window.webkitAudioContext)(); if (actx.state === 'suspended') actx.resume(); } catch (e) { /* немає аудіо */ } return actx; }
  addEventListener('pointerdown', () => { if (notifyCfg.on) audioCtx(); }, { passive: true });   // автовідтворення дозволене лише після жесту користувача
  function beep(kind) {
    const c = actx; if (!c || c.state !== 'running') return;
    (kind === 'alert' ? [[880, 0], [660, .3], [880, .6]] : [[660, 0], [880, .24]]).forEach(([f, t]) => {
      const o = c.createOscillator(), g = c.createGain(), t0 = c.currentTime + t; o.type = 'sine'; o.frequency.value = f;
      g.gain.setValueAtTime(.0001, t0); g.gain.exponentialRampToValueAtTime(.2, t0 + .03); g.gain.exponentialRampToValueAtTime(.0001, t0 + .24);
      o.connect(g); g.connect(c.destination); o.start(t0); o.stop(t0 + .26);
    });
  }
  const vibrate = p => { try { if (navigator.vibrate) navigator.vibrate(p); } catch (e) { /* ignore */ } };
  function watchTick() {
    if (!data) return;
    const ids = Watch.watchedIds(myId, fav).filter(id => infoById.has(id));
    const snap = Watch.snapshot(ids, statusOf, data.hasOfficial && !data.stale);
    const evs = notifyCfg.on ? Watch.diff(prevSnap, snap) : [];
    prevSnap = snap;
    if (evs.length) fireNotify(evs);
  }
  function fireNotify(evs) {
    const msgs = evs.map(ev => Watch.message(ev, infoById.get(ev.id).r + ' район'));
    const shortText = Watch.summary(evs.map(ev => ({ kind: ev.kind, name: infoById.get(ev.id).r })));
    const urgent = evs.some(ev => ev.kind === 'start' || ev.kind === 'warn');
    flash(shortText, 10000);
    if (notifyCfg.sound) { beep(urgent ? 'alert' : 'end'); vibrate(urgent ? [250, 120, 250] : [120]); }
    if (notifyCfg.system && document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      msgs.forEach((m, i) => { try { new Notification('AirRadar', { body: m, tag: 'ar-' + evs[i].id + '-' + evs[i].kind }); } catch (e) { /* ignore */ } });
    }
  }

  // ---------- посилання на територію ----------
  function syncUrl(id) {
    try { window.history.replaceState(null, '', location.pathname + UiState.withDistrict(location.search, id) + location.hash); } catch (e) { /* file:// тощо */ }
  }
  async function shareDistrict(id) {
    const p = infoById.get(id); if (!p) return;
    const url = UiState.linkFor(location.origin + location.pathname, id), title = 'AirRadar: ' + p.r + ' район';
    try { if (navigator.share) { await navigator.share({ title, url }); return; } } catch (e) { if (e && e.name === 'AbortError') return; }
    try { await navigator.clipboard.writeText(url); flash('Посилання скопійовано'); }
    catch (e) { window.prompt('Скопіюйте посилання', url); }
  }

  // ---------- укриття ----------
  let shelters = null, shelErr = false, shelOn = false, shelNear = null, shelUser = null, shelSel = null, shelBusy = false, shelMsg = '', shelAll = lsGet(SHEL_KEY + '.all') === '1';
  async function loadShelters() {
    if (shelters) return shelters;
    try {
      const r = await fetch('shelters.json'); if (!r.ok) throw new Error('HTTP ' + r.status);
      shelters = Shelters.parse(await r.json()); shelErr = false;
      amap.setShelters(shelters, s => { shelSel = s; openShelters(); });
    } catch (e) { shelErr = true; }
    return shelters;
  }
  function setShelOn(on) {
    shelOn = on; lsSet(SHEL_KEY, on ? '1' : '0'); $('t-shel').setAttribute('aria-pressed', String(on));
    if (on) loadShelters().then(() => { amap.showShelters(shelOn, shelAll); if (cardMode === 'shelters') renderShelters(); }); else amap.showShelters(false, shelAll);
  }
  function openShelters() {
    const a = document.activeElement; lastFocus = a && a !== document.body ? a : lastFocus;
    selId = null; amap.select(null); syncUrl(null); cardMode = 'shelters';
    if (!shelOn) setShelOn(true); else if (!shelters) loadShelters().then(() => { amap.showShelters(shelOn, shelAll); if (cardMode === 'shelters') renderShelters(); });
    renderShelters();
    const x = $('card').querySelector('.x'); if (x) x.focus({ preventScroll: true });
  }
  /** З картки району: показати шар і підлетіти до району. */
  function showSheltersAt(id) {
    amap.select(id); amap.flyTo(id, Object.assign({}, flyPad(), { maxZoom: 11 }));
    selId = null; openShelters();
  }
  function shelRow(s) {
    const d = shelUser ? Shelters.distanceM(shelUser.lat, shelUser.lon, s.lat, s.lon) : null;
    return '<div class="shrow"><span class="shdot" aria-hidden="true"></span><span class="t"><b>' + esc(Shelters.title(s)) + '</b><small>' + esc(s.kind) + (s.street && s.name ? ' · ' + esc(s.street) : '') +
      (d != null ? ' · ' + Shelters.fmtDist(d) + ' · ≈ ' + Math.max(1, Math.ceil(d / Shelters.WALK_M_PER_MIN)) + ' хв пішки' : '') + '</small></span>' +
      '<a href="' + esc(Shelters.routeUrl(s)) + '" target="_blank" rel="noopener noreferrer" aria-label="Маршрут до: ' + esc(Shelters.title(s)) + '">Маршрут</a></div>';
  }
  function renderShelters() {
    const c = $('card'); c.hidden = false; document.body.classList.add('has-card'); cardMode = 'shelters';
    let h = '<button class="x" aria-label="Закрити панель"><svg class="ic"><use href="#i-close"/></svg></button><h2>Укриття</h2><div class="sub">За даними OpenStreetMap' + (shelters ? ': ' + shelters.filter(s => s.verified).length + ' з назвою, ' + shelters.filter(s => !s.verified).length + ' без назви' : '') + '</div>' +
      '<div class="note" style="border-top:0;margin-top:8px">Покриття неповне, це не офіційний реєстр. Чи відкрите укриття, невідомо: перевіряйте на місці. Під час тривоги орієнтуйтесь на сирену й офіційні вказівки.</div>' +
      '<button class="swrow" role="switch" data-ss="toggle" aria-checked="' + shelOn + '"><span>Показувати на карті<small>З’являються від масштабу ' + AirMap.SHELTER_MIN_ZOOM + ': наблизьте карту. Зелені: назва вказує на укриття</small></span><i class="knob"></i></button>' +
      '<button class="swrow" role="switch" data-ss="all" aria-checked="' + shelAll + '"><span>Точки без назви<small>Сірі: тип невідомий, це може бути навіс чи альтанка</small></span><i class="knob"></i></button>';
    if (shelErr) h += '<div class="blk"><div class="kv"><span>Не вдалося завантажити дані укриттів</span></div><button class="btn-reset" data-ss="retry" style="margin-top:8px">Повторити</button></div>';
    else if (!shelters) h += '<div class="blk"><div class="kv"><span>Завантаження даних…</span></div></div>';
    else h += '<button class="btn-reset" data-ss="near"' + (shelBusy ? ' disabled' : '') + '>' + (shelBusy ? 'Визначаємо місцезнаходження…' : 'Знайти найближчі до мене') + '</button><small class="hintline">Позиція лишається у вашому браузері й нікуди не передається. У посилання на маршрут потрапляє лише точка призначення.</small>';
    if (shelMsg) h += '<div class="note" role="status">' + esc(shelMsg) + '</div>';
    if (shelNear && shelNear.length) h += '<div class="blk"><h3><span>Найближчі</span><span>до 5 км</span></h3>' + shelNear.map(shelRow).join('') + '<div class="note">Час пішки орієнтовний (80 м/хв, по прямій), не маршрут.</div></div>';
    if (shelSel) h += '<div class="blk"><h3><span>Вибране на карті</span></h3>' + shelRow(shelSel) + '</div>';
    c.innerHTML = h; c.querySelector('.x').onclick = closeCard; setCardH();
  }
  async function shelAction(a) {
    if (a === 'toggle') { setShelOn(!shelOn); renderShelters(); }
    else if (a === 'all') { shelAll = !shelAll; lsSet(SHEL_KEY + '.all', shelAll ? '1' : '0'); amap.showShelters(shelOn, shelAll); renderShelters(); }
    else if (a === 'retry') { shelErr = false; renderShelters(); await loadShelters(); amap.showShelters(shelOn, shelAll); renderShelters(); }
    else if (a === 'near') {
      shelBusy = true; shelMsg = ''; shelSel = null; renderShelters();
      try {
        await loadShelters(); if (!shelters) throw new Error('data');
        const pos = await amap.locate(); shelUser = pos;
        shelNear = Shelters.nearest(shelters, pos.lat, pos.lon, 3, 5000);
        if (!shelOn) setShelOn(true);
        amap.markUser(pos.lat, pos.lon);
        if (shelNear.length) amap.fitPoints([pos].concat(shelNear), Object.assign({}, flyPad(), { maxZoom: 15 }));
        else { shelMsg = 'У радіусі 5 км у даних укриттів немає. Покриття OpenStreetMap неповне: це не означає, що укриттів поруч нема.'; amap.fitPoints([pos], Object.assign({}, flyPad(), { maxZoom: 12 })); }
      } catch (e) { shelMsg = shelErr ? 'Не вдалося завантажити дані укриттів.' : 'Не вдалося визначити місцезнаходження. Перевірте дозвіл на геолокацію.'; }
      shelBusy = false; renderShelters();
    }
  }
  $('t-shel').onclick = () => { if (cardMode === 'shelters' && !$('card').hidden) closeCard(); else openShelters(); };

  // ---------- цілі: слід, швидкість, курс, орієнтовний прогноз; попередження для моєї території ----------
  const TGT_KEY = 'airradar.map2.tgt', TGT_POLL_MS = 15000;
  let tgtStale = lsGet('airradar.map2.tgtstale') === '1', tgtOn = lsGet(TGT_KEY) !== '0', tgtRaw = null, tgt = { tracks: [], ok: false, generalized: false, feedStale: false, rejected: 0 }, tgtErr = false, tgtSel = null, tgtKinds = [], tgtWarned = new Set(), tgtTimer = null, tgtWarnList = [];
  const ATTACK_T0 = window.AIRRADAR_ATTACK_T0 || Date.now();
  async function tgtFetch() {
    if (P.get('demo') === 'attack') { const d = Tracks.demoAttack(nowS(), (Date.now() - ATTACK_T0) / 1000); const c = $('demoClock'); c.hidden = false; c.textContent = 'ДЕМО · ' + d.attack.phase + ' · t+' + d.attack.simMin + ' хв'; return d; }
    if (P.has('demo')) return Tracks.demoPayload(nowS());
    const base = air.base, stat = new URL('tracks.json', base).href + '?t=' + Math.floor(Date.now() / 15000);
    let r = await fetch(air.staticMode ? stat : '/api/tracks?minutes=180', { cache: 'no-store' });
    if (!r.ok && !air.staticMode) r = await fetch(stat, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }
  async function tgtTick() {
    if (document.hidden) return;
    try { tgtRaw = await tgtFetch(); tgtErr = false; } catch (e) { tgtErr = true; }      // збій цілей не чіпає карту тривог; лишається останній знімок, що старішає
    tgtApply();
  }

  // ---------- радар навколо мене: коло, цілі всередині/курсом на мене, попередження ----------
  const ME_R_KEY = 'airradar.map2.merad';
  let meR = Math.max(5, Math.min(150, Number(lsGet(ME_R_KEY)) || 25)), mePos = null, meNear = [], meWarned = new Set(), meNote = '';
  function meApply() {
    if (!mePos) { amap.clearMe(); meNear = []; return; }
    amap.setMe(mePos, meR);
    meNear = Tracks.nearby(tgt.tracks, [mePos.lat, mePos.lon], meR);
    const keys = new Set(meNear.map(n => n.t.id)), fresh = meNear.filter(n => !meWarned.has(n.t.id));
    meWarned = keys;                                                                    // ціль, що вийшла з кола, при поверненні попередить знову
    if (fresh.length) {
      const txt = fresh.slice(0, 2).map(n => n.t.kind.short + (n.t.count > 1 ? ' ×' + n.t.count : '') + ' — ' + Math.round(n.km) + ' км ' + n.side + (n.inside ? ' (у колі ' + meR + ' км)' : ' · курсом на вас')).join(' · ') + (fresh.length > 2 ? ' · ще ' + (fresh.length - 2) : '');
      flash('Поруч з вами: ' + txt, 15000);
      if (notifyCfg.on) { if (notifyCfg.sound) { beep('alert'); vibrate([300, 120, 300]); } if (notifyCfg.system && document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('AirRadar: ціль поруч', { body: txt, tag: 'ar-me-' + fresh[0].t.id }); } catch (e) { /* ignore */ } } }
    }
    if (cardMode === 'targets') renderTargets();
  }
  async function meLocate() {
    meNote = '';
    try { const p = await amap.locate(); mePos = { lat: p.lat, lon: p.lon, approx: false }; }
    catch (e) {
      const c = myId ? amap.districtCenter(myId) : null;
      if (c) { mePos = { lat: c.lat, lon: c.lng, approx: true }; meNote = 'Геолокація недоступна: радар від центру вашого району (орієнтовно).'; }
      else { mePos = null; meNote = 'Геолокація недоступна. Дозвольте доступ або оберіть свою територію у вкладці «Моє».'; }
    }
    meApply(); if (mePos) amap.fitPoints([mePos], { maxZoom: meR > 60 ? 7 : meR > 25 ? 8 : 9 });
  }
  // Режим «стежити за моїм рухом»: позиція оновлюється, поки вкладка відкрита; нічого не зберігається і не передається.
  let meWatchId = null, meLast = 0;
  function meWatchStop() { if (meWatchId != null && navigator.geolocation) { try { navigator.geolocation.clearWatch(meWatchId); } catch (e) { /* ignore */ } } meWatchId = null; }
  function meWatchStart() {
    if (!navigator.geolocation) { meNote = 'Геолокація недоступна в цьому браузері.'; renderTargets(); return; }
    meWatchStop(); meNote = '';
    meWatchId = navigator.geolocation.watchPosition(p => {
      const np = { lat: p.coords.latitude, lon: p.coords.longitude, approx: false }, now = Date.now();
      const moved = mePos ? Tracks.dist([mePos.lat, mePos.lon], [np.lat, np.lon]) : 99;
      if (moved < 0.15 && now - meLast < 15000) return;     // не частіше ніж раз на 15 с, якщо майже не рухались
      meLast = now; mePos = np; meApply();
    }, () => { meWatchStop(); meNote = 'Не вдалося стежити за позицією: перевірте дозвіл на геолокацію.'; renderTargets(); }, { enableHighAccuracy: false, maximumAge: 10000, timeout: 20000 });
    renderTargets();
  }
  function meHtml() {
    let h = '<div class="blk"><h3><span>Радар навколо мене</span><span>' + (mePos ? meR + ' км' : 'вимкнено') + '</span></h3>' +
      '<div class="rad-chips">' + [10, 25, 50, 100].map(r => '<button class="chip" data-me-r="' + r + '" aria-pressed="' + (meR === r) + '">' + r + ' км</button>').join('') + '</div>' +
      '<div class="cacts"><button data-me-loc="1">' + (mePos ? 'Оновити позицію' : 'Показати радар біля мене') + '</button>' + (mePos ? '<button data-me-follow="1" aria-pressed="' + (meWatchId != null) + '">' + (meWatchId != null ? 'Не стежити за рухом' : 'Стежити за моїм рухом') + '</button><button data-me-off="1">Вимкнути</button>' : '') + '</div>';
    if (meNote) h += '<div class="note">' + esc(meNote) + '</div>';
    if (mePos) h += meNear.length
      ? meNear.map(n => '<div class="tgtrow rad-row' + (n.inside ? ' in' : '') + '" data-tg-row="' + esc(n.t.id) + '" role="button" tabindex="0"><span class="t"><b>' + esc(n.t.kind.short) + (n.t.count > 1 ? ' × ' + n.t.count : '') + '</b><small>' + (n.inside ? 'у колі' : 'курсом на вас') + ' · ймовірність ' + esc(Tracks.probability(n)) + ' · ' + esc(Tracks.fmtAge(n.t.ageS)) + '</small></span><span class="eta"><b>' + Math.round(n.km) + ' км</b> ' + esc(n.side) + (n.etaMin != null ? '<small style="display:block">' + esc(Tracks.fmtEta(Tracks.etaRange(n.etaMin, n.t.speedSource))) + '</small>' : '') + '</span></div>').join('') + '<div class="note">' + esc(Tracks.FORECAST_NOTE) + '</div>'
      : '<div class="note">У радіусі ' + meR + ' км цілей зараз немає. Це не означає, що небезпеки немає. Позиція не зберігається і нікуди не передається.</div>';
    return h + '</div>';
  }
  document.addEventListener('click', e => {
    const loc = e.target.closest('[data-me-loc]'), off = e.target.closest('[data-me-off]'), r = e.target.closest('[data-me-r]'), fol = e.target.closest('[data-me-follow]');
    if (fol) { if (meWatchId != null) { meWatchStop(); renderTargets(); } else meWatchStart(); }
    else if (loc) meLocate(); else if (off) { meWatchStop(); mePos = null; meWarned = new Set(); meApply(); renderTargets(); }
    else if (r) { meR = Number(r.dataset.meR); lsSet(ME_R_KEY, String(meR)); meApply(); if (!mePos) renderTargets(); }
  });


  // ---------- збережені місця (дім, робота…): лише в цьому браузері, з колом і попередженнями ----------
  const PLACES_KEY = 'airradar.map2.places';
  let places = (() => { try { const v = JSON.parse(lsGet(PLACES_KEY) || '[]'); return Array.isArray(v) ? v.filter(p => p && isFinite(p.lat) && isFinite(p.lon)).slice(0, 5).map(p => ({ id: String(p.id || '').replace(/[^\w-]/g, '').slice(0, 24) || 'p' + Math.random().toString(36).slice(2, 8), name: String(p.name || 'Місце').slice(0, 24), lat: +p.lat, lon: +p.lon, r: Math.max(5, Math.min(150, Number(p.r) || 25)) })) : []; } catch (e) { return []; } })();
  let placeNear = new Map(), placeWarned = new Set();
  const savePlaces = () => lsSet(PLACES_KEY, JSON.stringify(places));
  function placesApply() {
    amap.setPlaces(places); placeNear = new Map(); const keys = new Set(), fresh = [];
    places.forEach(p => {
      const l = Tracks.nearby(tgt.tracks, [p.lat, p.lon], p.r); placeNear.set(p.id, l);
      l.forEach(n => { const k = p.id + '>' + n.t.id; keys.add(k); if (!placeWarned.has(k)) fresh.push({ p, n }); });
    });
    placeWarned = keys;
    if (fresh.length) {
      const txt = fresh.slice(0, 2).map(f => f.p.name + ': ' + f.n.t.kind.short + (f.n.t.count > 1 ? ' ×' + f.n.t.count : '') + ' — ' + Math.round(f.n.km) + ' км ' + f.n.side + (f.n.inside ? ' (у колі)' : ' · курсом на це місце') + ' · ймовірність ' + Tracks.probability(f.n)).join(' · ') + (fresh.length > 2 ? ' · ще ' + (fresh.length - 2) : '');
      flash('Увага, ваші місця: ' + txt, 15000);
      if (notifyCfg.on) { if (notifyCfg.sound) { beep('alert'); vibrate([300, 120, 300]); } if (notifyCfg.system && document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('AirRadar: ціль біля місця', { body: txt, tag: 'ar-pl-' + fresh[0].n.t.id }); } catch (e) { /* ignore */ } } }
    }
    if (cardMode === 'targets') renderTargets();
  }
  function placesHtml() {
    let h = '<div class="blk"><h3><span>Мої місця</span><span>' + places.length + '/5</span></h3>';
    if (mePos && places.length < 5) h += '<div class="cacts"><button data-pl-add="🏠 Дім">🏠 Зберегти як «Дім»</button><button data-pl-add="💼 Робота">💼 Як «Робота»</button><button data-pl-add="📍 Місце">📍 Інше</button></div>';
    else if (!mePos) h += '<div class="note">Щоб додати місце, спершу покажіть радар біля себе (кнопка вище), потім збережіть позицію як «Дім» чи «Робота».</div>';
    h += places.map(p => {
      const l = placeNear.get(p.id) || [];
      return '<div class="plc-row"><span><b>' + esc(p.name) + '</b><small style="display:block;color:var(--muted)">радіус ' + p.r + ' км · ' + (l.length ? '<b style="color:#ff4d3d">' + l.length + ' ціл. поруч: ' + esc(Tracks.probability(l[0])) + ' ймовірність</b>' : 'цілей поруч немає') + '</small></span><button class="rm" data-pl-del="' + esc(p.id) + '" aria-label="Видалити місце ' + esc(p.name) + '"><svg class="ic"><use href="#i-close"/></svg></button></div>';
    }).join('');
    return h + '<div class="note">Місця зберігаються лише в цьому браузері (не на сервері). Для сповіщень у Telegram додайте їх у боті: «🛡 Гео-сторож» → «Мої місця».</div></div>';
  }
  document.addEventListener('click', e => {
    const add = e.target.closest('[data-pl-add]'), del = e.target.closest('[data-pl-del]');
    if (add && mePos && places.length < 5) { places.push({ id: 'p' + Date.now().toString(36), name: add.dataset.plAdd, lat: Math.round(mePos.lat * 1e4) / 1e4, lon: Math.round(mePos.lon * 1e4) / 1e4, r: meR }); savePlaces(); placesApply(); }
    else if (del) { places = places.filter(p => p.id !== del.dataset.plDel); savePlaces(); placesApply(); }
  });

  function tgtApply() {
    tgt = Tracks.normalize(tgtRaw, nowS());
    const vis = Tracks.filterKinds(tgt.tracks, tgtKinds);
    amap.setTargets(vis.filter(t => tgtStale ? (t.stale || Tracks.hasCourse(t)) : (!t.stale && Tracks.hasCourse(t))), t => openTarget(t.id)); amap.showTargets(tgtOn);   // без курсу на карті не малюємо (лише лічильник)
    renderTgtBar();
    const fresh = tgt.tracks.filter(t => !t.stale).length;
    $('legTg').hidden = !(tgtOn && tgt.tracks.length); $('disc').hidden = $('discNote').hidden = !(tgtOn && tgt.tracks.length);
    $('t-tgt').setAttribute('aria-pressed', String(tgtOn)); $('t-tgt').title = 'Цілі' + (tgt.tracks.length ? ': ' + fresh + ' актуальних' : '');
    tgtWatch(); if (mePos) meApply(); if (places.length) placesApply(); else amap.setPlaces([]);
    if (cardMode === 'targets') renderTargets(); else if (cardMode === 'target' && tgtSel) renderTarget(tgtSel);
  }
  function renderTgtBar() {
    const bar = $('tgtbar'), c = Tracks.counts(tgt.tracks), kinds = [['shahed', 'delta', 'Шахеди'], ['cruise_missile', 'dart', 'Крилаті ракети'], ['ballistic', 'ring', 'Балістика'], ['uav', 'quad', 'БпЛА розвідувальні']];
    const chips = kinds.filter(k => c[k[0]] > 0).map(k => '<button class="tchip" data-tg-open="1" aria-label="' + k[2] + ': ' + c[k[0]] + '">' + AirMap.glyphSvg(k[1], false).replace('width="36" height="36"', 'width="24" height="24"') + '<b>' + c[k[0]] + '</b></button>');
    if (c.noCourse) chips.push('<button class="tchip nc" data-tg-open="1">ще ' + c.noCourse + ' без курсу</button>');
    bar.hidden = !(tgtOn && chips.length); bar.innerHTML = chips.join('');
    document.body.classList.toggle('has-tgtbar', !bar.hidden);
  }
  $('tgtbar').addEventListener('click', e => { if (e.target.closest('[data-tg-open]')) openTargets(); });
  /** Цілі, чий прогноз проходить через мої/обрані території (за точкою прогнозу, що потрапляє в район). Лише свіжі треки. */
  function tgtWatch() {
    const ids = new Set(Watch.watchedIds(myId, fav).filter(id => infoById.has(id)));
    const list = [], keys = new Set();
    if (ids.size && feats.length) tgt.tracks.forEach(t => {
      if (t.stale) return;
      const hit = new Map();
      [[t.lat, t.lon, 0]].concat(t.forecast.map(f => [f.lat, f.lon, f.etaMin])).forEach(([la, lo, eta]) => { const d = amap.districtAt(la, lo); if (d && ids.has(d) && !hit.has(d)) hit.set(d, eta); });
      hit.forEach((eta, d) => { list.push({ t, id: d, eta }); keys.add(t.id + '>' + d); });
    });
    tgtWarnList = list;
    const fresh = list.filter(w => !tgtWarned.has(w.t.id + '>' + w.id));
    tgtWarned = keys;                                                         // ціль зникла або пройшла: ключ скидається, наступна поява попередить знову
    if (fresh.length) {
      const txt = fresh.slice(0, 2).map(w => w.t.kind.short + (w.t.count > 1 ? ' ×' + w.t.count : '') + ' курсом на ' + infoById.get(w.id).r + ' район' + (w.eta ? ' · ' + Tracks.fmtEta(Tracks.etaRange(w.eta, w.t.speedSource)) : '')).join(' · ') + (fresh.length > 2 ? ' · ще ' + (fresh.length - 2) + ' (див. вкладку «Моє»)' : '');
      flash('Попередження: ' + txt, 12000);
      if (notifyCfg.on) { if (notifyCfg.sound) { beep('alert'); vibrate([250, 120, 250]); } if (notifyCfg.system && document.hidden && 'Notification' in window && Notification.permission === 'granted') { try { new Notification('AirRadar: попередження', { body: txt, tag: 'ar-tgt-' + fresh[0].t.id }); } catch (e) { /* ignore */ } } }
    }
    if (tab === 'mine') renderMine();
  }
  function tgtWarnHtml(idFilter) {
    const l = tgtWarnList.filter(w => !idFilter || w.id === idFilter); if (!l.length) return '';
    return l.map(w => '<div class="tgtwarn" role="alert">' + esc(w.t.kind.short) + (w.t.count > 1 ? ' × ' + w.t.count : '') + ': прогноз веде на ' + esc(infoById.get(w.id).r) + ' район' + (w.eta ? ' · ' + Tracks.fmtEta(Tracks.etaRange(w.eta, w.t.speedSource)) : ' · уже поруч') +
      ' <button class="lnk" data-tg="' + esc(w.t.id) + '">показати</button></div>').join('') + '<div class="note" style="border-top:0;margin-top:0">' + esc(Tracks.FORECAST_NOTE) + '</div>';
  }
  function openTarget(id) {
    const a = document.activeElement; lastFocus = a && a !== document.body ? a : lastFocus;
    tgtSel = id; selId = null; amap.select(null); syncUrl(null); cardMode = 'target'; renderTarget(id);
    const x = $('card').querySelector('.x'); if (x) x.focus({ preventScroll: true });
  }
  function renderTarget(id) {
    const t = tgt.tracks.find(x => x.id === id), c = $('card'); c.hidden = false; document.body.classList.add('has-card');
    if (!t) { c.innerHTML = '<button class="x" aria-label="Закрити картку"><svg class="ic"><use href="#i-close"/></svg></button><h2>Ціль зникла зі стрічки</h2><div class="note">Остання відома мітка прибрана. Це не означає, що загрози немає.</div>'; c.querySelector('.x').onclick = closeCard; setCardH(); return; }
    let h = '<button class="x" aria-label="Закрити картку"><svg class="ic"><use href="#i-close"/></svg></button><h2>' + esc(t.kind.name) + (t.count > 1 ? ' × ' + t.count : '') + '</h2><div class="sub">' + (t.stale ? 'Давно не оновлювалось: ' + esc(Tracks.fmtAge(t.ageS)) : 'Остання мітка: ' + esc(Tracks.fmtAge(t.ageS))) + '</div>' +
      '<div class="blk h"><h3><span>Неофіційні дані</span><span class="tag" style="color:var(--hint)">не підтверджено</span></h3>' +
      '<div class="kv"><span>Швидкість</span><b>' + esc(Tracks.speedText(t)) + '</b></div><div class="kv"><span>Курс</span><b>' + esc(Tracks.headingText(t)) + '</b></div>' +
      '<div class="kv"><span>Достовірність</span><b>' + esc(Tracks.trustText(t)) + '</b></div>' +
      '<div class="kv"><span>Позиція</span><b>' + t.lat.toFixed(2) + ', ' + t.lon.toFixed(2) + (t.estimated ? ' (орієнт.)' : '') + '</b></div></div>';
    if (t.stale) h += '<div class="note">Дані давно не оновлювались: ціль може бути деінде. Прогноз не показується.</div>';
    else if (t.forecast.length) h += '<div class="blk"><h3><span>Імовірний напрямок</span><span>орієнтовно</span></h3>' + t.forecast.map(f => '<div class="tgtrow"><span>' + esc(f.name) + (f.km != null ? '<small style="display:block;color:var(--muted)">≈ ' + f.km + ' км</small>' : '') + '</span><span class="eta">' + esc(Tracks.fmtEta(f.eta)) + '</span></div>').join('') + '<div class="note">' + esc(Tracks.FORECAST_NOTE) + '</div></div>';
    else h += '<div class="note">Прогнозу напрямку немає: недостатньо даних про рух.</div>';
    h += '<div class="note">' + esc(Tracks.DISCLAIMER) + '</div><div class="cacts"><button data-tg-zoom="' + esc(t.id) + '">На карті</button><button data-guide="' + esc(t.kind.guide) + '">Що це?</button></div>';
    c.innerHTML = h; c.querySelector('.x').onclick = closeCard; setCardH();
  }
  function openTargets() {
    const a = document.activeElement; lastFocus = a && a !== document.body ? a : lastFocus;
    selId = null; tgtSel = null; amap.select(null); syncUrl(null); cardMode = 'targets'; renderTargets();
    const x = $('card').querySelector('.x'); if (x) x.focus({ preventScroll: true });
  }
  function renderTargets() {
    const c = $('card'); c.hidden = false; document.body.classList.add('has-card'); cardMode = 'targets';
    const all = tgt.tracks, oldN = all.filter(t => t.stale).length, vis = Tracks.filterKinds(all, tgtKinds).filter(t => tgtStale || !t.stale).slice().sort((a, b) => (a.stale - b.stale) || ((a.forecast[0] ? a.forecast[0].etaMin : 999) - (b.forecast[0] ? b.forecast[0].etaMin : 999)));
    let h = '<button class="x" aria-label="Закрити панель"><svg class="ic"><use href="#i-close"/></svg></button><h2>Цілі</h2><div class="sub">' + (P.has('demo') ? 'ДЕМО: вигадані цілі, не реальні дані · ' : '') + (all.length ? all.filter(t => !t.stale).length + ' актуальних із ' + all.length : 'немає даних про цілі') + '</div>' +
      '<button class="swrow" role="switch" data-tg-on="1" aria-checked="' + tgtOn + '"><span>Показувати на карті<small>Значок за типом, слід польоту і пунктир орієнтовного напрямку</small></span><i class="knob"></i></button>' +
      (oldN ? '<button class="swrow" role="switch" data-tg-stale="1" aria-checked="' + tgtStale + '"><span>Старі цілі (' + oldN + ')<small>Без оновлень понад 25 хв: за замовчуванням приховані</small></span><i class="knob"></i></button>' : '') +
      '<div class="chips" style="padding:8px 0">' + Object.keys(Tracks.KINDS).filter(k => k !== 'uav').map(k => '<button class="chip" data-tg-kind="' + k + '" aria-pressed="' + (!tgtKinds.length || tgtKinds.includes(k)) + '">' + esc(Tracks.KINDS[k].short) + '</button>').join('') + '</div>' + tgtWarnHtml(null) + meHtml() + placesHtml();
    if (tgtErr && !all.length) h += '<div class="note">Не вдалося завантажити дані про цілі. Орієнтуйтесь на сирену й офіційні повідомлення.</div>';
    else if (tgt.generalized) h += '<div class="note">Сервер віддає узагальнені дані без точних позицій цілей.</div>';
    else if (tgt.feedStale) h += '<div class="note">Дані про цілі давно не оновлювались: ' + esc(Tracks.fmtAge(nowS() - tgt.updatedAt)) + '. Ситуація може відрізнятись.</div>';
    else if (!all.length) h += '<div class="note">Зараз цілей у стрічці немає. Це не означає, що небезпеки немає.</div>';
    h += '<div class="blk">' + vis.map(t => '<div class="tgtrow" data-tg-row="' + esc(t.id) + '" role="button" tabindex="0"><span class="shdot" aria-hidden="true" style="background:' + (t.stale ? '#8794a1' : '#ff4d3d') + '"></span><span class="t"><b>' + esc(t.kind.short) + (t.count > 1 ? ' × ' + t.count : '') + '</b><small>' + esc(Tracks.fmtAge(t.ageS)) + (t.heading != null ? ' · ' + esc(Tracks.headingText(t)) : '') + '</small></span><span class="eta">' + (t.stale ? 'застаріло' : (t.forecast[0] ? esc(t.forecast[0].name) + ' ' + esc(Tracks.fmtEta(t.forecast[0].eta)) : '')) + '</span></div>').join('') + '</div>' +
      '<div class="note">' + esc(Tracks.DISCLAIMER) + '</div>';
    c.innerHTML = h; c.querySelector('.x').onclick = closeCard; setCardH();
  }
  function tgtClick(e) {
    const so = e.target.closest('[data-tg-stale]'); if (so) { tgtStale = !tgtStale; lsSet('airradar.map2.tgtstale', tgtStale ? '1' : '0'); tgtApply(); return true; }
    const on = e.target.closest('[data-tg-on]'); if (on) { tgtOn = !tgtOn; lsSet(TGT_KEY, tgtOn ? '1' : '0'); tgtApply(); return true; }
    const k = e.target.closest('[data-tg-kind]'); if (k) { const all = Object.keys(Tracks.KINDS).filter(x => x !== 'uav'); let cur = tgtKinds.length ? tgtKinds.slice() : all.slice(); cur = cur.includes(k.dataset.tgKind) ? cur.filter(x => x !== k.dataset.tgKind) : cur.concat(k.dataset.tgKind); tgtKinds = cur.length === all.length || !cur.length ? [] : cur; tgtApply(); return true; }
    const r = e.target.closest('[data-tg-row],[data-tg]'); if (r) { const id = r.dataset.tgRow || r.dataset.tg; openTarget(id); const t = tgt.tracks.find(x => x.id === id); if (t) amap.fitPoints([{ lat: t.lat, lon: t.lon }].concat(t.forecast.slice(0, 2)), Object.assign({}, flyPad(), { maxZoom: 9 })); return true; }
    const z = e.target.closest('[data-tg-zoom]'); if (z) { const t = tgt.tracks.find(x => x.id === z.dataset.tgZoom); if (t) amap.fitPoints([{ lat: t.lat, lon: t.lon }].concat(t.forecast.slice(0, 3)), Object.assign({}, flyPad(), { maxZoom: 9 })); return true; }
    return false;
  }
  $('card').addEventListener('click', e => { tgtClick(e); });
  $('card').addEventListener('keydown', e => { if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('[data-tg-row]')) { e.preventDefault(); tgtClick(e); } });
  $('t-tgt').onclick = () => { if (cardMode === 'targets' && !$('card').hidden) closeCard(); else openTargets(); };

  // ---------- наслідки (влучання): шар 💥 + список за 24 год; дані з impacts.json ----------
  const IMP_KEY = 'airradar.map2.imp';
  let impOn = lsGet(IMP_KEY) === '1', impList = [], impErr = false;
  async function impFetch() {
    if (P.has('demo')) { const n = nowS(); return { now: n, items: [
      { ts: n - 1800, when: '12:10', oblast: 'Київська', place: 'Бровари', killed: null, injured: 3, damage: ['житлові будинки'], lat: 50.51, lon: 30.79, kind: 'hit', flags: '' },
      { ts: n - 5400, when: '10:40', oblast: 'Харківська', place: 'Харків', killed: 1, injured: 5, damage: ['енергетика', 'пожежа'], lat: 49.99, lon: 36.23, kind: 'hit', flags: '' }], note: 'ДЕМО: вигадані дані.' }; }
    const r = await fetch(new URL('impacts.json', air.base).href + '?t=' + Math.floor(Date.now() / 60000), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status); return r.json();
  }
  async function impTick() {
    if (document.hidden) return;
    try { const d = await impFetch(); impList = (d.items || []).filter(i => i.kind === 'hit' && (i.ts || 0) >= nowS() - 86400); impErr = false; } catch (e) { impErr = true; }
    amap.setImpacts(impList, i => openImpacts(i)); amap.showImpacts(impOn);
    $('t-imp').setAttribute('aria-pressed', String(impOn)); $('t-imp').title = 'Наслідки' + (impList.length ? ': ' + impList.length + ' за 24 год' : '');
    if (cardMode === 'impacts') renderImpacts();
  }
  function openImpacts(focusIdx) { selId = null; tgtSel = null; amap.select(null); syncUrl(null); cardMode = 'impacts'; renderImpacts(); if (focusIdx != null && impList[focusIdx] && impList[focusIdx].lat != null) amap.fitPoints([{ lat: impList[focusIdx].lat, lon: impList[focusIdx].lon }], Object.assign({}, flyPad(), { maxZoom: 11 })); }
  function renderImpacts() {
    const c = $('card'); c.hidden = false; document.body.classList.add('has-card'); cardMode = 'impacts';
    let h = '<button class="x" aria-label="Закрити панель"><svg class="ic"><use href="#i-close"/></svg></button><h2>Наслідки</h2><div class="sub">' + (P.has('demo') ? 'ДЕМО · ' : '') + impList.length + ' за останні 24 год</div>' +
      '<button class="swrow" role="switch" data-imp-on="1" aria-checked="' + impOn + '"><span>Показувати на карті<small>Маркер 💥 на місці влучання</small></span><i class="knob"></i></button>';
    if (impErr && !impList.length) h += '<div class="note">Не вдалося завантажити наслідки. Це не означає, що їх немає.</div>';
    else if (!impList.length) h += '<div class="empty" style="padding:10px 0">Повідомлень про влучання за 24 години немає. Це не означає, що небезпеки немає.</div>';
    h += impList.map((i, k) => '<div class="blk"><h3><span>' + esc(i.place || i.oblast) + (i.place && i.oblast ? ' · ' + esc(i.oblast) : '') + '</span><span>' + esc(i.when) + '</span></h3>' +
      (i.killed ? '<div class="kv"><span>Загинули</span><b>' + i.killed + '</b></div>' : (i.flags && i.flags.includes('k') ? '<div class="kv"><span>Загиблі</span><b>є (кількість уточнюється)</b></div>' : '')) +
      (i.injured ? '<div class="kv"><span>Поранені</span><b>' + i.injured + '</b></div>' : '') +
      ((i.damage || []).length ? '<div class="chips">' + i.damage.map(d => '<span class="chip">' + esc(d) + '</span>').join('') + '</div>' : '') +
      (i.lat != null ? '<div class="cacts"><button data-imp-zoom="' + k + '">На карті</button></div>' : '') + '</div>').join('');
    h += '<div class="note">Дані зі зведень каналів: неофіційні, можуть уточнюватись. Адреси й назви об\'єктів не публікуються.</div>';
    c.innerHTML = h; c.querySelector('.x').onclick = closeCard; setCardH();
  }
  document.addEventListener('click', e => {
    const on = e.target.closest('[data-imp-on]'), z = e.target.closest('[data-imp-zoom]');
    if (on) { impOn = !impOn; lsSet(IMP_KEY, impOn ? '1' : '0'); amap.showImpacts(impOn); $('t-imp').setAttribute('aria-pressed', String(impOn)); renderImpacts(); }
    else if (z) openImpacts(Number(z.dataset.impZoom));
  });
  $('t-imp').onclick = () => { if (cardMode === 'impacts' && !$('card').hidden) closeCard(); else { if (!impOn) { impOn = true; lsSet(IMP_KEY, '1'); } impTick(); openImpacts(); } };
  $('t-rad').onclick = () => { openTargets(); setTimeout(() => { const el = $('card').querySelector('.rad-chips'); if (el) el.scrollIntoView({ block: 'center' }); }, 50); };
  impTick(); setInterval(impTick, 120000);
  tgtTick(); tgtTimer = setInterval(tgtTick, P.get('demo') === 'attack' ? 1500 : TGT_POLL_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tgtTick(); });
  window.__tgtTick = tgtTick; window.__tgt = () => tgt;

  // ---------- пошук ----------
  const closeResults = () => { $('results').hidden = true; };
  function pickResult(b) {
    closeResults(); $('q').value = '';
    if (b.dataset.id) { amap.select(b.dataset.id); amap.flyTo(b.dataset.id, flyPad()); openCard(b.dataset.id); }
    else { const ids = [...infoById.keys()].filter(k => k.split('|')[1] === b.dataset.stem); amap.flyToMany(ids, flyPad()); }
  }
  function renderResults() {
    const q = $('q').value, box = $('results');
    if (!Filters.tokens(q).length) { closeResults(); if (q.trim()) { box.hidden = false; box.innerHTML = '<div class="empty">Введіть назву області або району</div>'; } return; }
    const hits = Filters.search(infoById, q, 12);
    box.hidden = false;
    box.innerHTML = hits.length ? hits.map(h => '<button class="row" ' + (h.id ? 'data-id="' + esc(h.id) + '"' : 'data-stem="' + esc(h.stem) + '"') + '><span class="t"><b>' + esc(h.label) + '</b><small>' + esc(h.sub) + '</small></span></button>').join('') : '<div class="empty">Нічого не знайдено за запитом</div>';
    box.querySelectorAll('.row').forEach(b => b.onclick = () => pickResult(b));
  }
  $('q').addEventListener('input', renderResults);
  $('q').addEventListener('keydown', e => {
    const first = $('results').querySelector('.row');
    if (e.key === 'Enter' && first) { e.preventDefault(); pickResult(first); }
    else if (e.key === 'ArrowDown' && first) { e.preventDefault(); first.focus(); }
  });
  $('results').addEventListener('keydown', e => {
    const rows = [...$('results').querySelectorAll('.row')], i = rows.indexOf(document.activeElement);
    if (e.key === 'ArrowDown' && i < rows.length - 1) { e.preventDefault(); rows[i + 1].focus(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); (i > 0 ? rows[i - 1] : $('q')).focus(); }
  });

  // ---------- підкладка, зум, повний екран ----------
  const bp = $('basepick');
  bp.innerHTML = Object.keys(AirMap.basemaps).map(k => '<button role="radio" data-k="' + k + '" aria-checked="' + (amap.basemapKey === k) + '"><span class="sw ' + k + '"></span>' + AirMap.basemaps[k].name + '</button>').join('');
  bp.addEventListener('click', e => {
    const b = e.target.closest('button'); if (!b) return;
    amap.setBasemap(b.dataset.k); try { localStorage.setItem('airradar.map2.base', b.dataset.k); } catch (_) { /* ignore */ }
    bp.querySelectorAll('button').forEach(x => x.setAttribute('aria-checked', String(x === b)));
    toggleBase(false);
  });
  function toggleBase(on) { bp.hidden = !on; $('t-layers').setAttribute('aria-expanded', String(on)); }
  $('t-layers').onclick = () => toggleBase(bp.hidden);
  $('t-in').onclick = () => amap.zoomBy(1);
  $('t-out').onclick = () => amap.zoomBy(-1);

  const tg = window.Telegram && window.Telegram.WebApp;
  const fsOK = !!(tg && tg.requestFullscreen) || !!document.documentElement.requestFullscreen;
  if (fsOK) {
    $('t-full').hidden = false;
    $('t-full').onclick = () => {
      if (tg && tg.requestFullscreen) { tg.isFullscreen ? tg.exitFullscreen() : tg.requestFullscreen(); return; }
      if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {});
    };
  }
  document.addEventListener('fullscreenchange', () => setTimeout(fitForUi, 200));

  // ---------- шторка (mobile): peek / half / full, перетягування, фокус ----------
  const sh = $('sheet'), grab = $('grab');
  const SNAP = ['peek', 'half', 'full'];
  let sheetState = SNAP.includes(P.get('sheet')) ? P.get('sheet') : (innerWidth < 900 ? 'peek' : 'half');
  const snapPx = k => k === 'peek' ? 132 : k === 'full' ? Math.round(innerHeight * 0.84) : Math.round(innerHeight * 0.46);
  const stateNames = { peek: 'згорнуто', half: 'наполовину', full: 'розгорнуто' };
  function setSheet(k) {
    sheetState = k; sh.classList.remove('peek', 'full'); if (k !== 'half') sh.classList.add(k);
    sh.style.height = '';
    grab.setAttribute('aria-expanded', String(k !== 'peek'));
    grab.setAttribute('aria-label', 'Панель обстановки: ' + stateNames[k] + '. Натисніть, щоб змінити');
    sizeTools();
  }
  function sizeTools() { if (layout() === 'mobile' && !sh.hidden) document.documentElement.style.setProperty('--sheetH', sh.getBoundingClientRect().height + 'px'); }
  let drag = null, rafT = 0;
  function dragStart(e) {
    if (layout() !== 'mobile') return;
    if (e.target.closest('button') && !e.target.classList.contains('grab')) return;   // кнопки вкладок лишаються кліками
    drag = { y0: e.clientY, h0: sh.getBoundingClientRect().height, y: e.clientY, t: performance.now(), v: 0, moved: false, el: e.currentTarget };
    try { drag.el.setPointerCapture(e.pointerId); } catch (_) { /* ignore */ }
  }
  function dragMove(e) {
    if (!drag) return;
    const dy = drag.y0 - e.clientY;
    if (!drag.moved && Math.abs(dy) < 6) return;
    drag.moved = true; sh.classList.add('dragging');
    const now = performance.now(), dt = Math.max(1, now - drag.t);
    drag.v = 0.8 * drag.v + 0.2 * ((drag.y - e.clientY) / dt); drag.y = e.clientY; drag.t = now;   // px/мс, додатне = вгору
    sh.style.height = Math.max(96, Math.min(innerHeight * 0.9, drag.h0 + dy)) + 'px';
    if (!rafT) rafT = requestAnimationFrame(() => { rafT = 0; sizeTools(); });
  }
  function dragEnd(e) {
    if (!drag) return;
    const d = drag; drag = null; sh.classList.remove('dragging');
    if (!d.moved) { if (e.target === grab) cycleSheet(); return; }
    const h = sh.getBoundingClientRect().height;
    let idx = SNAP.reduce((best, k, i) => Math.abs(snapPx(k) - h) < Math.abs(snapPx(SNAP[best]) - h) ? i : best, 0);
    if (Math.abs(d.v) > 0.4) idx = Math.max(0, Math.min(2, SNAP.indexOf(sheetState) + (d.v > 0 ? 1 : -1)));   // швидкий свайп = сусідній стан
    setSheet(SNAP[idx]);
  }
  const cycleSheet = () => setSheet(sheetState === 'peek' ? 'half' : sheetState === 'half' ? 'full' : 'peek');
  [grab, document.querySelector('.tabs')].forEach(el => { el.addEventListener('pointerdown', dragStart); el.addEventListener('pointermove', dragMove); el.addEventListener('pointerup', dragEnd); el.addEventListener('pointercancel', dragEnd); });
  grab.onkeydown = e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); cycleSheet(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setSheet(SNAP[Math.min(2, SNAP.indexOf(sheetState) + 1)]); }
    else if (e.key === 'ArrowDown') { e.preventDefault(); setSheet(SNAP[Math.max(0, SNAP.indexOf(sheetState) - 1)]); }
  };
  setSheet(sheetState);            // шторка не змінює стан сама: ні фільтри, ні пошук, ні оновлення даних її не чіпають
  // Telegram Mini App: безпечні зони (Bot API 8.0+), з fallback на env(safe-area-*)
  function tgInsets() {
    if (!tg) return;
    const s = tg.safeAreaInset || {}, c = tg.contentSafeAreaInset || {}, r = document.documentElement.style;
    r.setProperty('--sat', 'max(env(safe-area-inset-top,0px),' + ((s.top || 0) + (c.top || 0)) + 'px)');
    r.setProperty('--sab', 'max(env(safe-area-inset-bottom,0px),' + ((s.bottom || 0) + (c.bottom || 0)) + 'px)');
    r.setProperty('--sal', 'max(env(safe-area-inset-left,0px),' + (s.left || 0) + 'px)');
    r.setProperty('--sar', 'max(env(safe-area-inset-right,0px),' + (s.right || 0) + 'px)');
  }
  if (tg) { tgInsets(); ['viewportChanged', 'safeAreaChanged', 'contentSafeAreaChanged', 'fullscreenChanged'].forEach(ev => { try { tg.onEvent(ev, () => { tgInsets(); onResize(); }); } catch (e) { /* стара версія API */ } }); }
  if (tg) { try { tg.ready(); tg.expand(); } catch (e) { /* ignore */ } }
  if ('serviceWorker' in navigator && location.protocol === 'https:' && !P.has('demo')) { /* реєстрація sw.js: етап 7 (offline), щоб не зачепити поточний кеш радара */ }
})();
