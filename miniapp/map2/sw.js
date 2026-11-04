/* map2/sw.js: Service Worker лише нової карти (scope = map2/, окремий від miniapp/sw.js). Основний SW не змінюється.
   Кешує код і геометрію районів, щоб карта відкривалась без мережі. Дані тривог не кешує (див. sw-rules.js). */
importScripts('sw-rules.js');
const R = self.Map2SwRules;
const NET_TIMEOUT_MS = 3500;

async function precache() {
  const c = await caches.open(R.CACHE);
  await c.addAll(R.LOCAL);                                          // наш код обов'язковий: без нього встановлення не відбувається
  await Promise.all(R.LEAFLET.map(u => fetch(u, { mode: 'cors' }).then(r => { if (R.cacheable(r)) return c.put(u, r); }).catch(() => {})));   // CDN необов'язковий
}
self.addEventListener('install', e => { e.waitUntil(precache().then(() => self.skipWaiting())); });

// Основний miniapp/sw.js при активації видаляє ВСІ чужі кеші (у тому числі наш). Самовідновлення: якщо кешу карти немає,
// а мережа є, один раз за життя SW наповнюємо його знову, щоб наступний offline-запуск спрацював.
let lastHeal = 0;
const HEAL_COOLDOWN_MS = 5 * 60 * 1000;                                // нестабільна мережа не повинна давати цикл повторних precache
function selfHeal() {
  if (Date.now() - lastHeal < HEAL_COOLDOWN_MS) return Promise.resolve();
  lastHeal = Date.now();
  return caches.has(R.CACHE).then(has => has ? undefined : precache()).catch(() => {});
}
self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(R.staleCaches(keys).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

// мережа першою (свіжий код), кеш лише коли мережі немає або вона не відповіла за таймаут
function networkFirst(req) {
  return new Promise(resolve => {
    let done = false;
    const fromCache = () => caches.match(req, { ignoreSearch: true }).then(m => m || caches.match('index.html', { ignoreSearch: true }));
    const t = setTimeout(() => { if (done) return; fromCache().then(m => { if (m && !done) { done = true; resolve(m); } }); }, NET_TIMEOUT_MS);
    fetch(req, { cache: 'no-cache' }).then(r => {
      clearTimeout(t);
      // 5xx від проксі/сервера = фактично немає мережі: віддаємо збережену копію, якщо вона є
      if (r.status >= 500) { fromCache().then(m => { if (!done) { done = true; resolve(m || r); } }); return; }
      if (R.cacheable(r)) { const cp = r.clone(); caches.open(R.CACHE).then(c => c.put(req, cp)); }
      if (!done) { done = true; resolve(r); }
    }).catch(() => { clearTimeout(t); fromCache().then(m => { if (!done) { done = true; resolve(m || Response.error()); } }); });
  });
}
function cacheFirst(req) {
  return caches.match(req).then(m => m || fetch(req).then(r => { if (R.cacheable(r)) { const cp = r.clone(); caches.open(R.CACHE).then(c => c.put(req, cp)); } return r; }));
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const kind = R.classify(req.url, self.registration.scope);
  if (kind === 'never' || kind === 'other') return;                   // мережа без втручання SW
  e.waitUntil(selfHeal());
  e.respondWith(kind === 'leaflet' ? cacheFirst(req) : networkFirst(req));
});
