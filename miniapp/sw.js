// Мінімальний service worker: оболонка радара відкривається без мережі, дані завжди беруться з мережі.
const CACHE = 'airradar-shell-v4';
const SHELL = ['./', 'icon-192.png', 'manifest.webmanifest'];
self.addEventListener('install', e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', e => { e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || u.pathname.endsWith('tracks.json') || u.pathname.endsWith('public-regions.json') || u.pathname.includes('/api/')) return;
  if (u.origin === location.origin) {
    e.respondWith(fetch(e.request, { cache: 'no-cache' }).then(r => { const cp = r.clone(); caches.open(CACHE).then(c => c.put(e.request, cp)); return r; }).catch(() => caches.match(e.request)));
  }
});

// Web Push (iPhone-PWA, iOS 16.4+): показуємо лише те, що прислав сервер.
self.addEventListener('push', e => {
  let d = {}; try { d = e.data ? e.data.json() : {}; } catch (_) { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || 'AirRadar AI', { body: d.body || '', icon: 'icon-192.png', tag: 'airradar', renotify: true, requireInteraction: true }));
});
self.addEventListener('notificationclick', e => { e.notification.close(); e.waitUntil(clients.openWindow('./')); });
