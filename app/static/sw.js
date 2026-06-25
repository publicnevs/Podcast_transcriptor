/* PodScribe Service Worker — app shell cache + offline transcript reading */
const CACHE = 'podscribe-v16';
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

  // Static assets: cache-first
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json') {
    e.respondWith(
      caches.match(e.request).then(hit => hit || fetch(e.request).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return resp;
      }))
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
        `<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PodScribe – Offline</title>
<style>
  body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;
       height:100vh;margin:0;background:#f0eff8}
  .box{text-align:center;padding:2rem 2.5rem;background:#fff;border-radius:16px;
       box-shadow:0 4px 24px rgba(0,0,0,.1);max-width:340px}
  h1{color:#7c6ff7;margin-top:0}p{color:#555;line-height:1.5}
  button{margin-top:1rem;padding:.6rem 1.4rem;background:#7c6ff7;color:#fff;
         border:none;border-radius:8px;font-size:1rem;cursor:pointer}
  button:hover{background:#6a5fd6}
</style></head>
<body><div class="box">
  <h1>Offline</h1>
  <p>Keine Internetverbindung. Bereits geöffnete Episoden und Transkripte sind weiterhin lesbar.</p>
  <button onclick="location.reload()">Erneut versuchen</button>
</div></body></html>`,
        { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }
      )
    ))
  );
});
