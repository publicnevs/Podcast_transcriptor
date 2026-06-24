/* PodScribe Service Worker — app shell cache + offline transcript reading */
const CACHE = 'podscribe-v17';
const SHELL = [
  '/static/style.css',
  '/static/icons.js',
  '/static/app.js',
  '/static/icon.svg',
  '/manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET') return;

  // Never cache audio streams, file exports, or the SW itself.
  // Exports must reach the network untouched so downloads work in the installed PWA.
  if (url.pathname.includes('/audio') || url.pathname.includes('/export') || url.pathname === '/sw.js') return;

  // Static assets: stale-while-revalidate — serve cache instantly, but always
  // refetch in the background and update the cache, so CSS/JS changes shipped by a
  // deploy reliably reach clients on the next load (no more pinned-stale stylesheet).
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json') {
    e.respondWith(
      caches.match(e.request).then(hit => {
        const network = fetch(e.request).then(resp => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then(c => c.put(e.request, copy));
          }
          return resp;
        }).catch(() => hit);
        return hit || network;
      })
    );
    return;
  }

  // API + pages: network-first, fall back to cache (offline reading)
  e.respondWith(
    fetch(e.request).then(resp => {
      if (resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => caches.match(e.request).then(hit =>
      hit || new Response(
        JSON.stringify({ offline: true, detail: 'Offline — nur bereits geladene Inhalte verfügbar' }),
        { status: 503, headers: { 'Content-Type': 'application/json' } }
      )
    ))
  );
});
