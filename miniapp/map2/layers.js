/* map2/layers.js: єдиний шар між інтерфейсом і рушієм карти (зараз Leaflet).
   UI викликає лише методи AirMap; Leaflet тут ізольований, щоб його можна було замінити (напр. на MapLibre).
   Публічний API:
     const m = new AirMap(el, { onSelect(id|null), onReady() });
     await m.loadGeo(geojson)           — полігони районів (id = «Район|Область»); повторний виклик замінює шар, дублів немає
     m.setStatus(id, {official:'alert'|'warn'|null, hint:bool})  / m.setStatuses(Map)  — ідемпотентно, лише змінені райони
     m.select(id), m.flyTo(id), m.fitCountry(padding), m.setBasemap('dark'|'sat'|'light'), m.zoomBy(±1)
     m.locate() -> Promise<{lat,lon}>   — позиція користувача лишається в браузері, назовні не передається
     m.districtAt(lat, lon) -> id|null  — район, у який потрапила точка (точка не зберігається)
     m.invalidate()                     — після зміни розміру контейнера
     m.setShelters(list, onPick) / m.showShelters(bool) / m.markUser(lat,lon) / m.clearUser() / m.fitPoints(pts, pad) — шар цивільних укриттів */
(function (global) {
  'use strict';

  // Підкладки без API-ключа (CARTO basemaps тепер вимагає ключ). Esri: безкоштовно з атрибуцією, для помірного навантаження;
  // для великого трафіку потрібна власна підкладка або ліцензія (див. docs/airradar-map-design-system.md).
  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/';
  const BASEMAPS = {
    dark:  { name: 'Темна схема', url: ESRI + 'Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', sub: '', max: 12, attr: 'Tiles © Esri' },
    light: { name: 'Світла схема', url: ESRI + 'Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', sub: '', max: 12, attr: 'Tiles © Esri' },
    sat:   { name: 'Супутник', url: ESRI + 'World_Imagery/MapServer/tile/{z}/{y}/{x}', sub: '', max: 12, attr: 'Imagery © Esri', dim: true },
  };
  const UA_BOUNDS = [[44.0, 21.8], [52.6, 40.4]];
  const CLS = { base: 'ar-d', alert: 'ar-d ar-alert', warn: 'ar-d ar-warn', hint: 'ar-d ar-hint' };

  /** Ключ району: «Район|Область» (перше слово області; Крим уніфіковано, як у публічному контракті). */
  function districtId(props) {
    const o = String(props.o || '').split(' ')[0];
    return props.r + '|' + (o === 'Автономна' ? 'Крим' : o);
  }

  function AirMap(el, opts) {
    opts = opts || {};
    this.opts = opts;
    this.layers = new Map();      // id -> L.Polygon (єдине джерело істини, дублів не буває)
    this.state = new Map();       // id -> 'official|hint' для diff
    this.selectedId = null;
    this.basemapKey = null;
    this._tile = null;
    const map = this.map = L.map(el, {
      zoomControl: false, attributionControl: true, preferCanvas: false,
      minZoom: 4, maxZoom: 12, zoomSnap: 0.25, maxBounds: [[40.0, 15.0], [56.0, 47.0]], maxBoundsViscosity: 0.6,
      worldCopyJump: false,
    });
    map.attributionControl.setPrefix(false);
    map.fitBounds(UA_BOUNDS);
    // користувач уже рухав карту: resize/орієнтація не повинні скидати його вигляд
    this.userMoved = false;
    ['pointerdown', 'wheel', 'touchstart'].forEach(ev => el.addEventListener(ev, () => { this.userMoved = true; }, { passive: true }));
    this.setBasemap(opts.basemap || 'dark');
    map.on('click', () => { if (this.selectedId) { this.select(null); } });
  }

  AirMap.prototype.loadGeo = function (geojson) {
    // повне перезавантаження: прибираємо старі шари, щоб не було накладання
    if (this.group) { this.group.remove(); this.layers.clear(); this.state.clear(); }
    const self = this;
    this.group = L.geoJSON(geojson, {
      style: () => ({ className: CLS.base, weight: 1, fillOpacity: 1, opacity: 1 }),
      onEachFeature(f, layer) {
        const id = districtId(f.properties);
        layer._arId = id; layer._arName = f.properties.r; layer._arOblast = f.properties.o;
        self.layers.set(id, layer);
        layer.on('click', e => { L.DomEvent.stopPropagation(e); self.select(id); if (self.opts.onSelect) self.opts.onSelect(id, f.properties); });
        layer.on('add', () => self._decorate(layer, id));
      },
    }).addTo(this.map);
    this.group.eachLayer(l => this._decorate(l, l._arId));
    if (this.opts.onReady) this.opts.onReady();
  };

  AirMap.prototype._decorate = function (layer, id) {
    const p = layer.getElement && layer.getElement();
    if (!p || p.dataset.arDone) return;
    p.dataset.arDone = '1';
    p.setAttribute('tabindex', '0'); p.setAttribute('role', 'button');
    p.setAttribute('aria-label', layer._arName + ' район, ' + layer._arOblast);
    p.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); this.select(id); if (this.opts.onSelect) this.opts.onSelect(id, { r: layer._arName, o: layer._arOblast }); }
    });
  };

  AirMap.prototype.setStatus = function (id, st) {
    const layer = this.layers.get(id); if (!layer) return false;
    const official = (st && st.official) || null, hint = !!(st && st.hint);
    const sig = (official || '') + '|' + (hint ? 'h' : '');
    if ((this.state.has(id) ? this.state.get(id) : '|') === sig) return false;            // без змін: нічого не перемальовуємо
    this.state.set(id, sig);
    const el = layer.getElement(); if (!el) return false;
    const cls = official === 'alert' ? 'ar-alert' : official === 'warn' ? 'ar-warn' : hint ? 'ar-hint' : '';
    el.setAttribute('class', ('leaflet-interactive ' + CLS.base + ' ' + cls + (this.selectedId === id ? ' ar-sel' : '')).trim());
    const label = layer._arName + ' район, ' + layer._arOblast + (official === 'alert' ? ', офіційна тривога' : official === 'warn' ? ', загроза поруч' : hint ? ', неофіційне повідомлення' : '');
    el.setAttribute('aria-label', label);
    return true;
  };

  AirMap.prototype.setStatuses = function (statusById) {
    let n = 0;
    this.layers.forEach((_, id) => { if (this.setStatus(id, statusById.get(id) || null)) n++; });
    return n;                                                  // кількість реально змінених районів
  };

  AirMap.prototype.select = function (id) {
    const prev = this.selectedId && this.layers.get(this.selectedId);
    if (prev && prev.getElement()) prev.getElement().classList.remove('ar-sel');
    this.selectedId = id || null;
    const cur = id && this.layers.get(id);
    if (cur) { cur.bringToFront(); if (cur.getElement()) cur.getElement().classList.add('ar-sel'); }
  };

  AirMap.prototype.flyTo = function (id, opts) {
    const l = this.layers.get(id); if (!l) return;
    // відступи під шторку/картку/інструменти: район опиняється в вільній частині екрана
    const o = opts || {};
    this._fly(l.getBounds(), o);
  };
  AirMap.prototype._fly = function (bounds, o) {
    const opts = { paddingTopLeft: o.tl || [40, 40], paddingBottomRight: o.br || [40, 40], maxZoom: o.maxZoom || 9 };
    if (global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches) this.map.fitBounds(bounds, Object.assign(opts, { animate: false }));
    else this.map.flyToBounds(bounds, Object.assign(opts, { duration: 0.7 }));
  };

  AirMap.prototype.flyToMany = function (ids, opts) {
    const o = opts || {}, b = L.latLngBounds([]);
    ids.forEach(id => { const l = this.layers.get(id); if (l) b.extend(l.getBounds()); });
    if (b.isValid()) this._fly(b, o);
  };

  AirMap.prototype.fitCountry = function (padding) {
    this.userMoved = false;
    this.map.fitBounds(UA_BOUNDS, { paddingTopLeft: (padding && padding.tl) || [0, 0], paddingBottomRight: (padding && padding.br) || [0, 0] });
  };

  AirMap.prototype.setBasemap = function (key) {
    const b = BASEMAPS[key]; if (!b || key === this.basemapKey) return;
    if (this._tile) this._tile.remove();
    this._tile = L.tileLayer(b.url, { subdomains: b.sub || 'abc', maxZoom: b.max, attribution: b.attr, crossOrigin: true, className: b.dim ? 'ar-tiles ar-dim' : 'ar-tiles' }).addTo(this.map);
    this._tile.bringToBack();
    this.basemapKey = key;
    // вигляд, вибір і статуси не скидаються: змінюється лише шар підкладки
    document.documentElement.dataset.basemap = key;
  };
  AirMap.basemaps = BASEMAPS;

  AirMap.prototype.zoomBy = function (d) { this.map.setZoom(this.map.getZoom() + d); };
  AirMap.prototype.invalidate = function () { this.map.invalidateSize({ pan: false }); };

  AirMap.prototype.locate = function () {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) return reject(new Error('unsupported'));
      navigator.geolocation.getCurrentPosition(p => resolve({ lat: p.coords.latitude, lon: p.coords.longitude }), reject, { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 });
    });
  };


  /** Позиція користувача + коло радіуса (км). Лише на екрані, назовні не передається. */
  AirMap.prototype.setMe = function (pos, radiusKm) {
    if (!this._mePane) { this.map.createPane('me'); this.map.getPane('me').style.zIndex = 455; this._mePane = true; }
    this.clearMe();
    this._meCircle = L.circle([pos.lat, pos.lon], { pane: 'me', radius: radiusKm * 1000, color: '#2f9bff', weight: 2, dashArray: '6 6', fillColor: '#2f9bff', fillOpacity: 0.07, interactive: false }).addTo(this.map);
    this._meDot = L.marker([pos.lat, pos.lon], { pane: 'me', interactive: false, keyboard: false, icon: L.divIcon({ className: 'me-dot', html: '<i></i>', iconSize: [18, 18], iconAnchor: [9, 9] }) }).addTo(this.map);
  };
  /** Збережені місця (дім/робота…): коло радіуса й підпис. */
  AirMap.prototype.setPlaces = function (list) {
    if (!this._mePane) { this.map.createPane('me'); this.map.getPane('me').style.zIndex = 455; this._mePane = true; }
    if (this._places) this._places.remove();
    this._places = L.layerGroup().addTo(this.map);
    list.forEach(p => {
      L.circle([p.lat, p.lon], { pane: 'me', radius: p.r * 1000, color: '#ffb020', weight: 2, dashArray: '2 6', fillColor: '#ffb020', fillOpacity: 0.05, interactive: false }).addTo(this._places);
      L.marker([p.lat, p.lon], { pane: 'me', interactive: false, keyboard: false, icon: L.divIcon({ className: 'place-pin', html: '<b></b><span></span>', iconSize: [90, 30], iconAnchor: [45, 15] }) })
        .addTo(this._places).getElement().querySelector('span').textContent = p.name;
    });
  };
  /** Наслідки (влучання) за координатами з impacts.json: маркер 💥, лише записи з позицією. */
  AirMap.prototype.setImpacts = function (items, onPick) {
    if (!this._impPane) { this.map.createPane('impacts'); this.map.getPane('impacts').style.zIndex = 465; this._impPane = true; this._impLayer = L.layerGroup().addTo(this.map); }
    this._impLayer.clearLayers();
    items.forEach((it, i) => {
      if (it.lat == null || it.lon == null) return;
      const m = L.marker([it.lat, it.lon], { pane: 'impacts', keyboard: true, title: (it.place || it.oblast || 'Наслідки'), icon: L.divIcon({ className: 'imp-pin', html: '<i>💥</i>', iconSize: [28, 28], iconAnchor: [14, 14] }) });
      m.on('click', () => onPick && onPick(i)); m.addTo(this._impLayer);
    });
  };
  AirMap.prototype.showImpacts = function (on) { if (!this._impLayer) return; if (on && !this.map.hasLayer(this._impLayer)) this._impLayer.addTo(this.map); else if (!on) this._impLayer.remove(); };
  AirMap.prototype.clearMe = function () { ['_meCircle', '_meDot'].forEach(k => { if (this[k]) { this[k].remove(); this[k] = null; } }); };
  AirMap.prototype.districtCenter = function (id) { const l = this.layers.get(id); return l ? l.getBounds().getCenter() : null; };

  AirMap.prototype.districtAt = function (lat, lon) {
    const pt = [lon, lat];
    for (const [id, layer] of this.layers) {
      const g = layer.feature.geometry, polys = g.type === 'Polygon' ? [g.coordinates] : g.coordinates;
      for (const poly of polys) if (inRing(pt, poly[0]) && !poly.slice(1).some(h => inRing(pt, h))) return id;
    }
    return null;
  };

  function inRing(pt, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if ((yi > pt[1]) !== (yj > pt[1]) && pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  // ---------- шар укриттів (canvas: тисячі точок без DOM-вузлів) ----------
  const SHELTER_MIN_ZOOM = 8;
  AirMap.SHELTER_MIN_ZOOM = SHELTER_MIN_ZOOM;
  AirMap.prototype.setShelters = function (list, onPick) {
    [this._shelV, this._shelU].forEach(g => { if (g) g.remove(); });
    this._shelPick = onPick;
    if (!this._shelRenderer) {
      this.map.createPane('shelters'); this.map.getPane('shelters').style.zIndex = 450;
      this._shelRenderer = L.canvas({ pane: 'shelters', padding: 0.2 });
      this.map.on('zoomend', () => this._syncShelters());
    }
    const self = this;
    // два рівні довіри: з назвою (зелена заливка) і без назви (сірий контур, тип невідомий)
    const mk = s => {
      const base = s.verified ? { radius: 6, weight: 2, color: '#ffffff', fillColor: '#12a37a', fillOpacity: 1 } : { radius: 5, weight: 2, color: '#8794a1', fillColor: '#8794a1', fillOpacity: .25 };
      const m = L.circleMarker([s.lat, s.lon], Object.assign({ renderer: self._shelRenderer, bubblingMouseEvents: false }, base));
      m.on('click', e => { L.DomEvent.stopPropagation(e); if (self._shelPick) self._shelPick(s); });
      return m;
    };
    this._shelV = L.layerGroup(list.filter(s => s.verified).map(mk));
    this._shelU = L.layerGroup(list.filter(s => !s.verified).map(mk));
    this._syncShelters();
  };
  /** on: шар укриттів; unverified: додатково точки без назви. */
  AirMap.prototype.showShelters = function (on, unverified) { this._shelOn = !!on; this._shelUn = !!unverified; this._syncShelters(); };
  AirMap.prototype._syncShelters = function () {
    if (!this._shelV) return;
    const z = this.map.getZoom() >= SHELTER_MIN_ZOOM;
    [[this._shelV, this._shelOn && z], [this._shelU, this._shelOn && this._shelUn && z]].forEach(([g, want]) => {
      if (want && !this.map.hasLayer(g)) g.addTo(this.map); else if (!want && this.map.hasLayer(g)) g.remove();
    });
  };
  AirMap.prototype.sheltersShown = function () { return !!this._shelV && this.map.hasLayer(this._shelV); };
  /** Позначка «ви тут» та виділене укриття: тимчасові, позиція не зберігається. */
  AirMap.prototype.markUser = function (lat, lon) { this.clearUser(); this._user = L.circleMarker([lat, lon], { radius: 8, weight: 3, color: '#ffffff', fillColor: '#2f7de1', fillOpacity: 1, interactive: false }).addTo(this.map); };
  AirMap.prototype.clearUser = function () { if (this._user) { this._user.remove(); this._user = null; } };
  AirMap.prototype.fitPoints = function (pts, o) {
    const b = L.latLngBounds(pts.map(p => [p.lat, p.lon])); if (!b.isValid()) return;
    this._fly(b, o || {});
  };

  // ---------- шар цілей: білий силует за типом (за курсом), коротка пунктирна лінія до найближчої точки призначення і плашка «→ Місто».
  // Рух між оновленнями НЕ анімується (див. tracks.js). Цілі без курсу на карті не малюються, лише в лічильнику. ----------
  const SIL = {
    delta: '<path d="M16 1.5 19.2 11.5 30.5 24.5 30.5 27 21.4 24.2 19 30.5H13L10.6 24.2 1.5 27V24.5L12.8 11.5Z"/>',
    dart: '<path d="M16 1 18.2 5.5V14.5L27 20V22.8L18.2 20.8V25.5L21.2 28.6V30.5L16 29 10.8 30.5V28.6L13.8 25.5V20.8L5 22.8V20L13.8 14.5V5.5Z"/>',
    ring: '<path d="M16 .8C17.9 3.4 18.7 5.6 18.7 8.2V22.6L22.6 28V31.2L18.2 29.3 16 31 13.8 29.3 9.4 31.2V28L13.3 22.6V8.2C13.3 5.6 14.1 3.4 16 .8Z"/>',
    quad: '<path d="M11.2 11.2 20.8 20.8M20.8 11.2 11.2 20.8" fill="none" stroke-width="2.6"/><circle cx="16" cy="16" r="3.4"/><circle cx="7" cy="7" r="4.4"/><circle cx="25" cy="7" r="4.4"/><circle cx="7" cy="25" r="4.4"/><circle cx="25" cy="25" r="4.4"/>',
  };
  // колір за типом цілі: видно з першого погляду, що саме летить (застарілі — сірі)
  const W_COLOR = { shahed: '#ffb02e', uav: '#ffd84d', cruise_missile: '#ff4d3d', ballistic: '#ff2d8a', kab: '#5aa9ff', aviation: '#b9c4d0' };
  function colorOf(t) { return t.stale ? '#b3bcc6' : (t.profile === 'reactive' ? '#ff7a2e' : (W_COLOR[t.weapon] || '#ffffff')); }
  function glyphSvg(glyph, stale, color) {
    const fill = color || (stale ? '#b3bcc6' : '#ffffff');
    return '<svg viewBox="0 0 32 32" width="36" height="36" fill="' + fill + '" stroke="#0b1015" stroke-width="1.9" stroke-linejoin="round" aria-hidden="true">' + (SIL[glyph] || SIL.delta) + '</svg>';
  }
  const R_EARTH = 6371;
  function destPoint(lat, lon, brg, km) {
    const d = km / R_EARTH, b = brg * Math.PI / 180, la = lat * Math.PI / 180, lo = lon * Math.PI / 180;
    const la2 = Math.asin(Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(b));
    const lo2 = lo + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la), Math.cos(d) - Math.sin(la) * Math.sin(la2));
    return [la2 * 180 / Math.PI, lo2 * 180 / Math.PI];
  }
  const escHtml = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const CLUSTER_PX = 56, CLUSTER_MAX_ZOOM = 8, COURSE_MAX_KM = 70;   // кластер ширше, лінії курсу коротші: карта має читатися з першого погляду
  AirMap.prototype.setTargets = function (tracks, onPick) {
    if (!this._tgtPane) {
      this.map.createPane('targets'); this.map.getPane('targets').style.zIndex = 470; this._tgtPane = true; this._tgt = new Map(); this._tgtLayer = L.layerGroup().addTo(this.map);
      const z = () => {
        const c = this.map.getContainer(), zm = this.map.getZoom();
        c.classList.toggle('tgt-zoomlow', zm < 6);      // на загальному плані підписи міст ховаються: їх було б забагато
        c.classList.toggle('tgt-zs', zm < 6); c.classList.toggle('tgt-zm', zm >= 6 && zm < 8);   // розмір значка росте з масштабом
        if (this._tgtLast && (zm < CLUSTER_MAX_ZOOM || this._tgtClustered)) this.setTargets(this._tgtLast.tracks, this._tgtLast.onPick);
      };
      this.map.on('zoomend', z); z();
    }
    this._tgtPick = onPick; this._tgtLast = { tracks, onPick };
    this._tgt.forEach(rec => rec.parts.forEach(p => p.remove())); this._tgt.clear();    // повний перемал: кластери залежать від масштабу
    const g = this._tgtLayer, map = this.map, zoom = map.getZoom();
    // кластеризація свіжих цілей на малих масштабах: щільні скупчення → один значок із числом
    const cl = new Map(), singles = [];
    tracks.forEach(t => {
      if (zoom >= CLUSTER_MAX_ZOOM || t.stale) { singles.push(t); return; }
      const pt = map.latLngToContainerPoint([t.lat, t.lon]), key = Math.floor(pt.x / CLUSTER_PX) + ':' + Math.floor(pt.y / CLUSTER_PX);
      (cl.get(key) || cl.set(key, []).get(key)).push(t);
    });
    this._tgtClustered = false;
    const groups = [];
    cl.forEach(arr => { if (arr.length === 1) singles.push(arr[0]); else groups.push(arr); });
    const kmBetween = (a, b) => { const R = Math.PI / 180, dy = (b[0] - a[0]) * 111.2, dx = (b[1] - a[1]) * 111.2 * Math.cos(a[0] * R); return Math.sqrt(dx * dx + dy * dy); };
    const drawCourse = (t, parts, color) => {
      // курс = ОДНА коротка стрілка (≤ COURSE_MAX_KM) у бік найближчої точки призначення; без довгих слідів і прогнозів через пів країни
      let to = null, label = '';
      if (!t.stale) {
        let far = null;
        if (t.dest) { far = [t.dest.lat, t.dest.lon]; label = t.dest.name; }
        else if (t.forecast.length) { far = [t.forecast[0].lat, t.forecast[0].lon]; label = t.forecast[0].name; }
        if (far) {
          const d = kmBetween([t.lat, t.lon], far);
          if (d <= COURSE_MAX_KM) to = far;
          else { const brg = t.heading != null ? t.heading : Math.atan2((far[1] - t.lon) * Math.cos(t.lat * Math.PI / 180), far[0] - t.lat) * 180 / Math.PI; to = destPoint(t.lat, t.lon, brg, COURSE_MAX_KM); }
        } else if (t.heading != null) to = destPoint(t.lat, t.lon, t.heading, 40);
      }
      if (to) {
        parts.push(L.polyline([[t.lat, t.lon], to], { pane: 'targets', color, weight: 3, opacity: .9, lineCap: 'round', interactive: false }).addTo(g));
        const brg = Math.atan2((to[1] - t.lon) * Math.cos(t.lat * Math.PI / 180), to[0] - t.lat) * 180 / Math.PI;
        parts.push(L.marker(to, { pane: 'targets', interactive: false, keyboard: false, icon: L.divIcon({ className: 'tgt-arrow-wrap', html: '<i class="tgt-arrow" style="border-bottom-color:' + color + ';transform:rotate(' + brg + 'deg)"></i>', iconSize: [14, 14], iconAnchor: [7, 7] }) }).addTo(g));
        if (label && zoom >= 7) parts.push(L.marker(to, { pane: 'targets', interactive: false, keyboard: false, icon: L.divIcon({ className: 'tgt-dest-wrap', html: '<span class="tgt-dest" style="border-left:3px solid ' + color + '">→ ' + escHtml(label) + '</span>', iconSize: [0, 0], iconAnchor: [-10, 11] }) }).addTo(g));
      }
      return label;
    };
    singles.forEach(t => {
      const rec = { parts: [] }, parts = rec.parts, color = colorOf(t), label = drawCourse(t, parts, color);
      if (t.estimated && t.radiusKm && !t.stale) parts.push(L.circle([t.lat, t.lon], { pane: 'targets', radius: t.radiusKm * 1000, color: '#ffffff', weight: 1.5, opacity: .6, dashArray: '4 5', fillColor: '#ffffff', fillOpacity: .06, interactive: false }).addTo(g));   // неточна позиція — коло невизначеності, а не точка
      const rot = t.heading == null ? 0 : t.heading;
      const html = '<div class="tgt' + (t.stale ? ' stale' : '') + (t.estimated ? ' est' : '') + '"><span class="ico" style="transform:rotate(' + (t.kind.glyph === 'quad' ? 0 : rot) + 'deg)">' + glyphSvg(t.kind.glyph, t.stale, color) + '</span>' + (t.count > 1 ? '<b class="cnt">×' + t.count + '</b>' : '') + '</div>';
      const m = L.marker([t.lat, t.lon], { pane: 'targets', icon: L.divIcon({ className: 'tgt-wrap', html, iconSize: [38, 38], iconAnchor: [19, 19] }), keyboard: true, riseOnHover: true,
        title: t.kind.name + (t.count > 1 ? ' × ' + t.count : '') + (t.stale ? ' (застаріло)' : '') + (label ? ', курс: ' + label : '') }).addTo(g);
      m.on('click', e => { L.DomEvent.stopPropagation(e); if (this._tgtPick) this._tgtPick(t); });
      parts.push(m); this._tgt.set(t.id, rec);
    });
    groups.forEach((arr, gi) => {
      this._tgtClustered = true;
      const rec = { parts: [] }, parts = rec.parts, n = arr.reduce((a, t) => a + (t.count || 1), 0);
      arr.forEach(t => drawCourse(t, parts, colorOf(t)));                                   // напрямки лишаються видимими навіть у скупченні
      const lat = arr.reduce((a, t) => a + t.lat, 0) / arr.length, lon = arr.reduce((a, t) => a + t.lon, 0) / arr.length;
      const kinds = {}; arr.forEach(t => { kinds[t.weapon] = (kinds[t.weapon] || 0) + 1; });
      const top = Object.keys(kinds).sort((a, b) => kinds[b] - kinds[a])[0], color = W_COLOR[top] || '#ffffff';
      const m = L.marker([lat, lon], { pane: 'targets', keyboard: true, title: 'Цілей: ' + n + ' — натисніть, щоб наблизити',
        icon: L.divIcon({ className: 'tgt-wrap', html: '<div class="tgt-cl" style="border-color:' + color + '"><b>' + n + '</b></div>', iconSize: [40, 40], iconAnchor: [20, 20] }) }).addTo(g);
      m.on('click', e => { L.DomEvent.stopPropagation(e); this._fly(L.latLngBounds(arr.map(t => [t.lat, t.lon])), { maxZoom: 9 }); });
      parts.push(m); this._tgt.set('cluster:' + gi, rec);
    });
  };
  AirMap.prototype.showTargets = function (on) { if (!this._tgtLayer) return; if (on && !this.map.hasLayer(this._tgtLayer)) this._tgtLayer.addTo(this.map); else if (!on) this._tgtLayer.remove(); };
  AirMap.prototype.targetCount = function () { return this._tgt ? this._tgt.size : 0; };

  AirMap.glyphSvg = glyphSvg; AirMap.W_COLOR = W_COLOR;
  AirMap.districtId = districtId;
  global.AirMap = AirMap;
})(window);
