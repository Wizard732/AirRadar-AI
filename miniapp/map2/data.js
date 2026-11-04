/* map2/data.js: єдина точка отримання даних для нової карти.
   Читає ТІЛЬКИ публічний контракт (/api/public/v1/regions або public-regions.json): райони/області, категорії, статуси,
   діапазони. Координат, курсу, швидкості, прогнозів і ETA тут немає і бути не може: адаптер їх не запитує.
   Стани зв'язку: loading → ok | error (перша помилка) ; ok → offline (втрата) → recovered (повернення). */
(function (global) {
  'use strict';

  const SLUG_STEM = {
    vinnytska: 'Вінницька', volynska: 'Волинська', luhanska: 'Луганська', dnipropetrovska: 'Дніпропетровська', donetska: 'Донецька',
    zhytomyrska: 'Житомирська', zakarpatska: 'Закарпатська', zaporizka: 'Запорізька', ivano_frankivska: 'Івано-Франківська',
    kyivska: 'Київська', kirovohradska: 'Кіровоградська', crimea: 'Крим', lvivska: 'Львівська', mykolaivska: 'Миколаївська',
    odeska: 'Одеська', poltavska: 'Полтавська', rivnenska: 'Рівненська', sumska: 'Сумська', ternopilska: 'Тернопільська',
    kharkivska: 'Харківська', khersonska: 'Херсонська', khmelnytska: 'Хмельницька', cherkaska: 'Черкаська',
    chernigivska: 'Чернігівська', chernivetska: 'Чернівецька', kyiv: 'Київ', sevastopol: 'Севастополь',
  };
  const CITY_IN_SLUG = { 'Київська': ['Київ'], 'Крим': ['Севастополь'] };   // sirens.py, ALERTS_IN_UA_REGION_IDS 11 → kyivska, 20 → crimea
  const STEM_SLUG = {}; Object.keys(SLUG_STEM).forEach(k => { STEM_SLUG[SLUG_STEM[k]] = k; });
  STEM_SLUG['Київ'] = 'kyivska'; STEM_SLUG['Севастополь'] = 'crimea';
  const CATEGORY = { drone: 'БпЛА', shahed: 'БпЛА', uav: 'БпЛА', fpv: 'БпЛА', recon_drone: 'Розвідка', recon: 'Розвідка',
    cruise_missile: 'Ракети', missile: 'Ракети', ballistic: 'Балістика', kab: 'КАБ', aviation: 'Авіація', unknown: 'Невідомі' };
  const STALE_AFTER_S = 15 * 60;
  const CACHE_KEY = 'airradar.map2.last', HIST_KEY = 'airradar.map2.hist';

  function AirData(opts) {
    opts = opts || {};
    this.demo = !!opts.demo;
    // дані лежать у корені клієнта (поруч із radar.html), а map2/ — підпапка; APK/Pages можуть задати AIRRADAR_DATA_BASE
    const base = opts.base || global.AIRRADAR_DATA_BASE || new URL('../', document.baseURI).href;
    const pages = /github\.io$/.test(location.hostname) || !!opts.staticOnly || !!global.AIRRADAR_DATA_BASE;
    this.base = base;
    this.staticMode = pages;
    this.url = pages ? new URL('public-regions.json', base).href : '/api/public/v1/regions';
    this.fallbackUrl = new URL('public-regions.json', base).href;
    this.listeners = { data: [], conn: [], history: [] };
    this.conn = 'loading';
    this.last = null; this.history = null;
    const self = this;
    const store = { get: k => localStorage.getItem(k), set: (k, v) => localStorage.setItem(k, v), del: k => localStorage.removeItem(k) };
    this.sync = new DataSync({
      fetchData: () => self._fetch(), fetchHistory: () => self._fetchHistory(),
      now: () => Date.now(), setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: t => clearTimeout(t), random: Math.random,
      EventSource: !pages && !this.demo ? global.EventSource : null, sseUrl: '/api/stream', live: !pages && !this.demo,
      storage: this.demo ? null : store, cacheKey: CACHE_KEY, histKey: HIST_KEY,
      listen: (name, fn) => {
        const map = { online: [global, 'online'], offline: [global, 'offline'], visible: [document, 'visibilitychange'], hidden: [document, 'visibilitychange'] };
        const [tgt, ev] = map[name];
        const h = () => { if (name === 'visible' && document.hidden) return; if (name === 'hidden' && !document.hidden) return; fn(); };
        tgt.addEventListener(ev, h); return () => tgt.removeEventListener(ev, h);
      },
    });
    this.sync.on('conn', c => { this.conn = c; this._emit('conn', c); });
    this.sync.on('data', d => {
      this.last = AirData.normalize(d.raw, Math.floor(Date.now() / 1000));
      this.last.fromCache = !!d.fromCache; this.last.fetchedAt = d.savedAt; this.last.savedAt = d.savedAt;
      this._emit('data', this.last);
    });
    this.sync.on('history', h => { this.history = h; this._emit('history', h); });
  }

  AirData.prototype.on = function (ev, fn) { this.listeners[ev].push(fn); return this; };
  AirData.prototype._emit = function (ev, v) { this.listeners[ev].forEach(f => { try { f(v); } catch (e) { console.error(e); } }); };

  /** Нормалізація публічного контракту в структури, зручні для карти. Ідемпотентно, без побічних ефектів. */
  AirData.normalize = function (raw, nowS) {
    raw = raw && typeof raw === 'object' ? raw : {};
    const arr = v => (Array.isArray(v) ? v.filter(x => typeof x === 'string') : []);       // частковий/пошкоджений ответ не повинен ламати карту
    const num = v => (Number.isFinite(+v) ? +v : 0);
    const out = { updatedAt: num(raw.updated_at), delayMin: num(raw.delay_min), official: { oblasts: new Set(), raions: new Map() }, hints: new Map(), oblastHints: [], stale: false };
    const off = raw.official && typeof raw.official === 'object' ? raw.official : {};
    out.hasOfficial = Array.isArray(off.sirens);               // null/відсутнє = джерело офіційних тривог недоступне (no_data), а НЕ «тривог немає»
    arr(off.sirens).forEach(s => { if (SLUG_STEM[s]) out.official.oblasts.add(SLUG_STEM[s]); });
    // за таблицею sirens.py: Київ-місто живе в slug kyivska, Севастополь у crimea, тож їхня тривога приходить як ці slug
    Object.keys(CITY_IN_SLUG).forEach(stem => { if (out.official.oblasts.has(stem)) CITY_IN_SLUG[stem].forEach(c => out.official.oblasts.add(c)); });
    arr(off.sirens_raions).forEach(k => out.official.raions.set(k, 'alert'));
    arr(off.sirens_raions_y).forEach(k => { if (!out.official.raions.has(k)) out.official.raions.set(k, 'warn'); });
    (Array.isArray(raw.regions) ? raw.regions : []).forEach(r => {
      if (!r || typeof r !== 'object') return;
      const stem = SLUG_STEM[r.region]; if (!stem) return;
      const item = { category: CATEGORY[r.category] || 'Невідомі', status: r.status, count: r.count_bucket || '', updated: num(r.last_update) || num(r.first_seen), stale: !!r.stale, oblast: stem, raion: typeof r.raion === 'string' && r.raion ? r.raion : null };
      // стабільний ключ неофіційного повідомлення; повторне повідомлення оновлює запис, а не дублює
      item.key = 'hint:' + stem + ':' + (item.raion || '*') + ':' + (r.category || '');
      if (item.raion) out.hints.set(item.raion + '|' + stem, item); else out.oblastHints.push(item);
    });
    // немає часу оновлення = свіжість невідома: вважаємо дані застарілими, а не свіжими
    out.stale = !!nowS && (out.updatedAt <= 0 || (nowS - out.updatedAt - out.delayMin * 60) > STALE_AFTER_S);
    return out;
  };

  // Сценарій «атака» (?demo=attack): офіційні тривоги по областях за розкладом (хв сценарію), узгодженим з польотом цілей у tracks.js.
  const ATTACK_ALERTS = [['sumska', 0, 50, 'shahed'], ['chernigivska', 5, 85, 'shahed'], ['poltavska', 12, 70, 'shahed'], ['kyivska', 22, 88, 'shahed'],
    ['dnipropetrovska', 30, 75, 'shahed'], ['kharkivska', 38, 72, 'missile'], ['zhytomyrska', 50, 80, 'missile'], ['cherkaska', 55, 82, 'ballistic']];
  const ATTACK_LEN_MIN = 100;
  AirData.prototype._attackPayload = function () {
    const now = Math.floor(Date.now() / 1000), m = ((Date.now() - global.AIRRADAR_ATTACK_T0) / 1000) % ATTACK_LEN_MIN;
    const on = ATTACK_ALERTS.filter(a => m >= a[1] && m < a[2]);
    return { updated_at: now - 5, delay_min: 0,
      official: { sirens: on.map(a => a[0]), sirens_raions: [], sirens_raions_y: [], siren_threats: {} },
      regions: on.map(a => ({ region: a[0], raion: null, category: a[3], status: 'tracking', count_bucket: '2-5', last_update: now - 20, stale: false })) };
  };

  AirData.prototype._demoPayload = function () {
    if (global.AIRRADAR_ATTACK_T0) return this._attackPayload();
    const now = Math.floor(Date.now() / 1000);
    return { updated_at: now - 40, delay_min: 0,
      official: { sirens: ['khersonska'], sirens_raions: ['Ізюмський|Харківська', 'Конотопський|Сумська', 'Охтирський|Сумська', 'Ізмаїльський|Одеська'], sirens_raions_y: ['Лубенський|Полтавська'], siren_threats: {} },
      regions: [{ region: 'poltavska', raion: 'Кременчуцький', category: 'shahed', status: 'tracking', count_bucket: '2-5', last_update: now - 300, stale: false },
        { region: 'chernigivska', raion: 'Ніжинський', category: 'recon_drone', status: 'tracking', count_bucket: '1', last_update: now - 1500, stale: true },
        { region: 'dnipropetrovska', raion: 'Нікопольський', category: 'shahed', status: 'tracking', count_bucket: '6+', last_update: now - 120, stale: false }] };
  };

  AirData.prototype._fetch = async function () {
    if (this.demo) return this._demoPayload();
    let r = await fetch(this.url + (this.staticMode ? '?t=' + Math.floor(Date.now() / 30000) : ''), { cache: 'no-store' });
    if (!r.ok && !this.staticMode) r = await fetch(this.fallbackUrl + '?t=' + Math.floor(Date.now() / 30000), { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  };

  AirData.prototype._fetchHistory = async function () {
    if (this.demo) return { alerts: this._demoHistory().rows, now: Math.floor(Date.now() / 1000) };
    const stat = new URL('alerts_hist.json', this.base).href + '?t=' + Math.floor(Date.now() / 30000);
    let r = await fetch(this.staticMode ? stat : '/api/alerts-history?days=7', { cache: 'no-store' });
    if (!r.ok && !this.staticMode) r = await fetch(stat, { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  };
  AirData.prototype._demoHistory = function () {
    const n = Math.floor(Date.now() / 1000);
    return { now: n, fetchedAt: n, rows: [
      [1, 'kharkivska', 'raion', 'Ізюмський район', '', 'red', 'air_raid', n - 2580, 0],
      [2, 'sumska', 'raion', 'Конотопський район', '', 'red', 'air_raid', n - 5400, n - 5000],
      [3, 'sumska', 'raion', 'Конотопський район', '', 'red', 'air_raid', n - 4960, 0],          // дребезг: пауза 40 с
      [4, 'poltavska', 'raion', 'Лубенський район', '', 'yellow', 'air_raid', n - 9000, n - 7200],
      [5, 'khersonska', 'oblast', 'Херсонська область', '', 'red', 'air_raid', n - 14000, 0]] };
  };

  AirData.prototype.load = function () { return this.sync.load(); };
  AirData.prototype.retry = function () { return this.sync.retryNow(); };
  AirData.prototype.start = function () { return this.sync.start(); };
  AirData.prototype.stop = function () { this.sync.stop(); };

  /** id району → стан для карти. Область у тривозі підсвічує всі її райони, окремі райони мають пріоритет. */
  AirData.toStatuses = function (data, districtIds) {
    const m = new Map();
    districtIds.forEach(id => {
      const [, stem] = id.split('|');
      let official = data.official.raions.get(id) || (data.official.oblasts.has(stem) ? 'alert' : null);
      const hint = data.hints.get(id);
      if (official || hint) m.set(id, { official, hint: !!hint });
    });
    return m;
  };

  /** Метадані району для alerts-state: slug області, назва району, ознака міста (Київ, Севастополь). */
  AirData.districtMeta = function (id, props) {
    const stem = id.split('|')[1];
    return { slug: STEM_SLUG[stem] || '', name: props.r, city: props.o === 'Київ' || props.o === 'Севастополь' };
  };
  AirData.CATEGORY = CATEGORY; AirData.SLUG_STEM = SLUG_STEM; AirData.STEM_SLUG = STEM_SLUG;
  global.AirData = AirData;
})(window);
