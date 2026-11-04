/* map2/sync.js: транспорт даних нової карти. Чиста логіка без DOM: усе зовнішнє (fetch, таймери, EventSource, сховище, події
   мережі) передається через env, тож модуль працює в браузері і в node (тести з підміненим часом).

   Гарантії:
   - один запит даних одночасно (single-flight); запит, що прийшов під час іншого, виконується один раз після нього;
   - один таймер опитування (ланцюжок setTimeout), паралельних опитувань немає;
   - відповідь, старіша за вже застосовану (updated_at менший), відкидається;
   - повторна ревізія rev не викликає запит; rev запам'ятовується лише після успішного завантаження;
   - при помилках інтервал росте (10 с → 120 с, ±20%), не частіше; online/повернення на вкладку скидають лічильник;
   - помилка історії не ламає карту: попередня історія лишається з прапором error;
   - stop() знімає всі таймери, EventSource і слухачі (повторний start не дублює обробники).
   Стани зв'язку: loading → ok | error (нема ні мережі, ні кешу) | offline (є старі дані); offline → recovered → ok. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(); else root.DataSync = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const LIVE_SSE_MS = 60000, LIVE_NOSSE_MS = 30000, STATIC_MS = 60000;
  const BACKOFF_BASE_MS = 10000, BACKOFF_MAX_MS = 120000;
  const HISTORY_EVERY_MS = 60000;
  const MAX_CACHE_AGE_S = 6 * 3600;        // старіший кеш не показуємо взагалі: краще чесна помилка, ніж позавчорашня обстановка
  const CACHE_VERSION = 2;

  const backoffMs = (fails, rnd) => {
    const base = Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * Math.pow(2, Math.max(0, fails - 1)));
    return Math.round(base * (0.8 + 0.4 * (rnd ? rnd() : 0.5)));
  };

  /** Обгортка кешу: {v, savedAt, raw}. Старий формат (сам контракт) приймається, savedAt = updated_at. Повертає null, якщо кеш відсутній чи застарів. */
  function readCache(text, nowS) {
    let j = null; try { j = JSON.parse(text || 'null'); } catch (e) { return null; }
    if (!j || typeof j !== 'object') return null;
    const raw = j.v === CACHE_VERSION ? j.raw : (j.updated_at ? j : null);
    if (!raw || typeof raw !== 'object') return null;
    const savedAt = j.v === CACHE_VERSION ? +j.savedAt || 0 : +raw.updated_at || 0;
    if (!(savedAt > 0) || nowS - savedAt > MAX_CACHE_AGE_S) return null;
    return { raw, savedAt };
  }
  const wrapCache = (raw, nowS) => JSON.stringify({ v: CACHE_VERSION, savedAt: nowS, raw });

  /**
   * env: { fetchData():Promise<raw>, fetchHistory():Promise<{alerts|rows, now}>, now():ms, setTimeout, clearTimeout, random(),
   *        EventSource|null, sseUrl, storage:{get(k),set(k,v),del(k)}, listen(name, fn):off  // name: online|offline|visible|hidden
   *        live:boolean (є SSE і живий API), cacheKey, histKey }
   */
  function DataSync(env) {
    this.env = env;
    this.conn = 'loading';
    this.rev = null; this._wantRev = null;
    this.lastUpdated = 0;                // updated_at останнього застосованого контракту
    this.fetchedAt = 0;
    this.failCount = 0;
    this.history = null;
    this.listeners = { data: [], conn: [], history: [] };
    this._everOk = false; this._timer = null; this._inflight = null; this._again = false; this._seq = 0;
    this._sse = null; this._sseOpen = false; this._sseFails = 0; this._sseTimer = null; this._sseWasDown = false;
    this._histInflight = null; this._histAt = 0; this._histSeq = 0; this._sig = null;
    this._offs = []; this._started = false; this._recTimer = null;
    this.stats = { fetches: 0, applied: 0, ignoredOld: 0, ignoredRev: 0, sseOpens: 0 };
  }
  const P = DataSync.prototype;
  P.on = function (ev, fn) { this.listeners[ev].push(fn); return this; };
  P._emit = function (ev, v) { this.listeners[ev].slice().forEach(f => { try { f(v); } catch (e) { if (typeof console !== 'undefined') console.error(e); } }); };
  P._setConn = function (c) { if (c !== this.conn) { this.conn = c; this._emit('conn', c); } };
  P._nowS = function () { return Math.floor(this.env.now() / 1000); };

  // ---------- кеш ----------
  P._loadCache = function () {
    const st = this.env.storage; if (!st) return null;
    let text = null; try { text = st.get(this.env.cacheKey); } catch (e) { return null; }
    const c = readCache(text, this._nowS());
    if (!c && text) { try { st.del(this.env.cacheKey); } catch (e) { /* ignore */ } }   // прострочений кеш прибираємо
    return c;
  };
  P._saveCache = function (raw) { try { this.env.storage && this.env.storage.set(this.env.cacheKey, wrapCache(raw, this._nowS())); } catch (e) { /* приватний режим, квота */ } };

  // ---------- завантаження даних ----------
  /** Один запит одночасно. Викликаний під час запиту, він не плодить паралельний, а запускає один повтор після нього. */
  P.load = function () {
    if (this._inflight) { this._again = true; return this._inflight; }
    const seq = ++this._seq;
    this.stats.fetches++;
    const p = this._inflight = (async () => {
      let raw = null, err = null;
      try { raw = await this.env.fetchData(); } catch (e) { err = e || new Error('fetch'); }
      if (!this._started && !err) { /* зупинено під час запиту: відповідь відкидаємо */ err = new Error('stopped'); }
      if (!err) this._onSuccess(raw, seq); else this._onFailure();
    })().finally(() => {
      this._inflight = null;
      if (this._again && this._started) { this._again = false; return this.load(); }
      this._arm();
    });
    return p;
  };

  P._onSuccess = function (raw, seq) {
    const upd = +raw.updated_at || 0;
    // застаріла відповідь (CDN/проксі віддав старішу версію): мережа працює, але дані не відкочуємо назад
    if (this.lastUpdated && upd && upd < this.lastUpdated) { this.stats.ignoredOld++; this._markOnline(); return; }
    const nowS = this._nowS();
    if (upd) this.lastUpdated = upd;
    this.fetchedAt = nowS;
    if (this._wantRev) { this.rev = this._wantRev; }
    this._saveCache(raw);
    this.stats.applied++;
    this._emit('data', { raw, fromCache: false, savedAt: nowS });
    this._markOnline();
    // історія: при зміні множини тривог, раз на хвилину і після відновлення зв'язку
    const sig = this._sigOf(raw);
    if (sig !== this._sig || !this._histAt || this.env.now() - this._histAt > HISTORY_EVERY_MS || this._histForce) { this._sig = sig; this._histForce = false; this.loadHistory(); }
  };
  P._sigOf = function (raw) {
    const o = (raw && raw.official) || {};
    return [(o.sirens || []).slice().sort().join(','), (o.sirens_raions || []).slice().sort().join(',')].join('|');
  };
  P._markOnline = function () {
    const recovered = this._failedBefore && this._everOk;
    this._failedBefore = false; this._everOk = true; this.failCount = 0;
    this._setConn(recovered ? 'recovered' : 'ok');
    if (recovered) { clearTimeout(this._recTimer); this._recTimer = this.env.setTimeout(() => { if (this.conn === 'recovered') this._setConn('ok'); }, 4000); }
  };
  P._onFailure = function () {
    this._failedBefore = true; this.failCount++;
    if (this._everOk) { this._setConn('offline'); return; }
    // перша помилка: пробуємо збережені дані (з явним віком), інакше чесний екран помилки
    if (!this._triedCache) {
      this._triedCache = true;
      const c = this._loadCache();
      if (c) {
        this.lastUpdated = +c.raw.updated_at || 0;
        this._emit('data', { raw: c.raw, fromCache: true, savedAt: c.savedAt });
        this._cachedShown = true; this._everOk = true;      // є що показати: далі це «втрата зв'язку», а не «нема даних»
        this._setConn('offline');
        this._loadHistoryCache();
        return;
      }
    }
    if (!this._cachedShown) this._setConn('error');
  };

  // ---------- історія ----------
  P._loadHistoryCache = function () {
    if (this.history || !this.env.storage || !this.env.histKey) return;
    let j = null; try { j = JSON.parse(this.env.storage.get(this.env.histKey) || 'null'); } catch (e) { return; }
    if (j && Array.isArray(j.rows) && j.fetchedAt && this._nowS() - j.fetchedAt <= MAX_CACHE_AGE_S) {
      this.history = { rows: j.rows, now: j.now, fetchedAt: j.fetchedAt, fromCache: true, error: true };
      this._emit('history', this.history);
    }
  };
  /** Помилка історії не чіпає дані і зв'язок: лишається попередній журнал з прапором error (час початку показується як «може бути неактуальним»). */
  P.loadHistory = function () {
    if (this._histInflight) return this._histInflight;
    const seq = ++this._histSeq;
    return this._histInflight = (async () => {
      try {
        const j = await this.env.fetchHistory();
        if (seq !== this._histSeq || !this._started) return;
        const nowS = this._nowS();
        const rows = Array.isArray(j && j.alerts) ? j.alerts : Array.isArray(j && j.rows) ? j.rows : [];
        this.history = { rows, now: (j && j.now) || nowS, fetchedAt: nowS, error: false };
        try { this.env.storage && this.env.histKey && this.env.storage.set(this.env.histKey, JSON.stringify({ rows, now: this.history.now, fetchedAt: nowS })); } catch (e) { /* ignore */ }
      } catch (e) {
        if (!this._started) return;
        if (this.history) this.history = Object.assign({}, this.history, { error: true });
        else this._loadHistoryCache();
        if (!this.history) this.history = null;
      }
      this._histAt = this.env.now();
      this._emit('history', this.history);
    })().finally(() => { this._histInflight = null; });
  };

  // ---------- планувальник: один таймер ----------
  P._interval = function () {
    if (this.failCount) return backoffMs(this.failCount, this.env.random);
    if (!this.env.live) return STATIC_MS;
    return this._sseOpen ? LIVE_SSE_MS : LIVE_NOSSE_MS;
  };
  P._arm = function () {
    this.env.clearTimeout(this._timer); this._timer = null;
    if (!this._started || this._hidden) return;
    this._timer = this.env.setTimeout(() => { this._timer = null; this.load(); }, this._interval());
  };
  /** Миттєва перевірка: скидає лічильник помилок (мережа повернулась, користувач натиснув «Повторити»). */
  P.retryNow = function () { this.failCount = 0; return this.load(); };

  // ---------- SSE ----------
  P._openSse = function () {
    const ES = this.env.EventSource; if (!ES || !this.env.live || !this._started) return;
    this._closeSse();
    let es; try { es = new ES(this.env.sseUrl); } catch (e) { this._scheduleSse(); return; }
    this._sse = es;
    const onRev = r => {
      r = String(r || ''); if (!r) return;
      if (r === this.rev || (r === this._wantRev && this._inflight)) { this.stats.ignoredRev++; return; }
      this._wantRev = r; this.load();
    };
    es.onopen = () => {
      this.stats.sseOpens++; this._sseOpen = true; this._sseFails = 0;
      // стрім міг пропустити зміни, поки був розірваний: після перепідключення синхронізуємось явно
      if (this._sseWasDown) { this._sseWasDown = false; this._histForce = true; this.retryNow(); } else this._arm();
    };
    es.onmessage = ev => onRev(ev && ev.data);
    es.addEventListener && es.addEventListener('rev', ev => onRev(ev && ev.data));
    es.onerror = () => {
      this._sseOpen = false; this._sseWasDown = true;
      this._arm();                                   // поки стрім лежить, опитування частіше (30 с / backoff)
      if (es.readyState === 2) this._scheduleSse();  // браузер не перепідключиться сам: пробуємо з паузою
    };
  };
  P._closeSse = function () { if (this._sse) { try { this._sse.close(); } catch (e) { /* ignore */ } this._sse.onopen = this._sse.onmessage = this._sse.onerror = null; this._sse = null; } this._sseOpen = false; };
  P._scheduleSse = function () {
    this.env.clearTimeout(this._sseTimer); this._sseFails++;
    this._sseTimer = this.env.setTimeout(() => { this._sseTimer = null; this._openSse(); }, backoffMs(this._sseFails, this.env.random));
  };

  // ---------- життєвий цикл ----------
  P.start = function () {
    this.stop(); this._started = true;
    const e = this.env;
    this._offs.push(e.listen('online', () => { this.retryNow(); if (e.live && !this._sse) this._openSse(); }));
    this._offs.push(e.listen('offline', () => { this._failedBefore = true; if (this._everOk) this._setConn('offline'); else if (this.conn !== 'loading') this._setConn('error'); }));
    this._offs.push(e.listen('hidden', () => { this._hidden = true; e.clearTimeout(this._timer); this._timer = null; }));
    this._offs.push(e.listen('visible', () => { this._hidden = false; if (!this.fetchedAt || e.now() - this.fetchedAt * 1000 > 5000) this.retryNow(); else this._arm(); }));
    this._openSse();
    return this.load();
  };
  P.stop = function () {
    this._started = false;
    this.env.clearTimeout(this._timer); this.env.clearTimeout(this._sseTimer); this.env.clearTimeout(this._recTimer);
    this._timer = this._sseTimer = this._recTimer = null;
    this._closeSse();
    this._offs.splice(0).forEach(off => { try { off(); } catch (e) { /* ignore */ } });
  };

  DataSync.backoffMs = backoffMs; DataSync.readCache = readCache; DataSync.wrapCache = wrapCache;
  DataSync.MAX_CACHE_AGE_S = MAX_CACHE_AGE_S; DataSync.CACHE_VERSION = CACHE_VERSION;
  return DataSync;
});
