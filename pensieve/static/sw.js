// Pensieve service worker: shell cache, stale-while-revalidate for reader GETs, offline queue for state POSTs.
const VERSION = 'pensieve-v2';
const SHELL = ['/static/app.css', '/static/web.css', '/static/fonts.css', '/static/app.js', '/static/reader.js', '/static/vendor/htmx.min.js', '/static/icon.svg', '/static/manifest.webmanifest',
  '/static/fonts/fraunces-latin.woff2', '/static/fonts/fraunces-latin-ext.woff2', '/static/fonts/fraunces-vietnamese.woff2', '/static/fonts/instrument-sans-latin.woff2', '/static/fonts/instrument-sans-latin-ext.woff2'];
const DB_NAME = 'pensieve-offline';
const STORE = 'queue';

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

function cacheKey(request) {
  // Partials (HX-Request) and full pages share a URL; keep them apart.
  const hx = request.headers.get('HX-Request') === 'true' ? '#hx' : '';
  return new Request(request.url + hx, { method: 'GET' });
}

function isReaderGet(url) {
  return url.pathname === '/' || url.pathname.startsWith('/reader/') || url.pathname.startsWith('/items/') || url.pathname.startsWith('/clusters/');
}
function isStateChange(url) {
  return /^\/items\/[^/]+\/(read|unread|star|unstar|tag|note)$/.test(url.pathname) || url.pathname === '/items/undo-read' || /\/mark-read$/.test(url.pathname);
}

async function staleWhileRevalidate(request) {
  const cache = await caches.open(VERSION);
  const key = cacheKey(request);
  const cached = await cache.match(key);
  const network = fetch(request).then((resp) => {
    if (resp && resp.ok && resp.type === 'basic') cache.put(key, resp.clone());
    return resp;
  }).catch(() => null);
  if (cached) { network.catch(() => {}); return cached; }
  const resp = await network;
  if (resp) return resp;
  return new Response('<div class="list-empty"><div class="eyebrow">Offline</div><p class="muted">This page isn\'t cached yet.</p></div>', { status: 503, headers: { 'Content-Type': 'text/html' } });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method === 'GET') {
    if (url.pathname.startsWith('/static/')) {
      event.respondWith(caches.open(VERSION).then((c) => c.match(req).then((hit) => hit || fetch(req).then((r) => { if (r.ok) c.put(req, r.clone()); return r; }))));
    } else if (isReaderGet(url) && !url.pathname.startsWith('/reader/api/')) {
      event.respondWith(staleWhileRevalidate(req));
    }
    return;
  }
  if (req.method === 'POST' && isStateChange(url)) {
    event.respondWith(fetch(req.clone()).catch(async () => {
      await enqueue(req);
      return new Response('', { status: 202, headers: { 'HX-Reswap': 'none', 'X-Pensieve-Queued': '1' } });
    }));
  }
});

function openDb() {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB_NAME, 1);
    r.onupgradeneeded = () => r.result.createObjectStore(STORE, { autoIncrement: true });
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}
async function enqueue(request) {
  const body = await request.clone().text();
  const headers = {};
  request.headers.forEach((v, k) => { headers[k] = v; });
  const db = await openDb();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite');
    tx.objectStore(STORE).add({ url: request.url, body, headers, at: Date.now() });
    tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
  });
}
async function replay() {
  const db = await openDb();
  const entries = await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const out = [];
    const cur = tx.objectStore(STORE).openCursor();
    cur.onsuccess = () => { const c = cur.result; if (c) { out.push({ key: c.key, value: c.value }); c.continue(); } else resolve(out); };
    cur.onerror = () => reject(cur.error);
  });
  for (const { key, value } of entries) {
    try {
      const resp = await fetch(value.url, { method: 'POST', body: value.body, headers: value.headers, credentials: 'include' });
      if (!resp.ok && resp.status !== 404) throw new Error('replay failed');
      await new Promise((resolve, reject) => { const tx = db.transaction(STORE, 'readwrite'); tx.objectStore(STORE).delete(key); tx.oncomplete = resolve; tx.onerror = () => reject(tx.error); });
    } catch (_) { break; }
  }
}
async function clearAll() {
  const keys = await caches.keys();
  await Promise.all(keys.map((k) => caches.delete(k)));
  await new Promise((resolve) => { const r = indexedDB.deleteDatabase(DB_NAME); r.onsuccess = r.onerror = r.onblocked = () => resolve(); });
}
self.addEventListener('message', (event) => {
  if (!event.data) return;
  if (event.data.type === 'replay') event.waitUntil(replay());
  if (event.data.type === 'clear') event.waitUntil(clearAll());
});
self.addEventListener('sync', (event) => { if (event.tag === 'pensieve-replay') event.waitUntil(replay()); });
self.addEventListener('online', () => replay());
