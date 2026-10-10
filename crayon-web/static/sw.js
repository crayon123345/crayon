// Crayon service worker: cache only the static app shell. Never touch /api, /auth or /setup (streaming + sessions).
const V = 'crayon-v1';
const SHELL = ['/static/style.css', '/static/app.js', '/static/crayon.svg', '/static/favicon.svg', '/static/icon-192.png', '/static/icon-512.png'];
self.addEventListener('install', (e) => { e.waitUntil(caches.open(V).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener('activate', (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== V).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener('fetch', (e) => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  if (u.pathname.startsWith('/api') || u.pathname.startsWith('/auth') || u.pathname.startsWith('/setup') || u.pathname === '/health') return;
  if (r.mode === 'navigate') {
    e.respondWith(fetch(r).catch(() => new Response('<meta name=viewport content="width=device-width"><body style="font:16px system-ui;background:#131112;color:#eee;text-align:center;padding:60px 20px"><h2>You are offline</h2><p>Crayon needs internet to think. Reconnect and reopen.</p>', {headers: {'Content-Type': 'text/html'}})));
    return;
  }
  if (u.pathname.startsWith('/static/')) {
    e.respondWith(fetch(r).then((res) => { if (res.ok) { const c = res.clone(); caches.open(V).then((ca) => ca.put(r, c)); } return res; }).catch(() => caches.match(r)));
  }
});
