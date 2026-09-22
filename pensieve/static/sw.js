// Pensieve service worker: shell cache, stale-while-revalidate for reader GETs, offline queue for state POSTs.
// Registered as /sw.js?v=<build stamp>; the stamp names the cache so a new build drops the old shell.
const BUILD = new URL(self.location.href).searchParams.get('v') || 'dev';
const VERSION = 'pensieve-' + BUILD;
const V = '?v=' + BUILD;
const SHELL = ['/static/app.css' + V, '/static/web.css' + V, '/static/fonts.css' + V, '/static/app.js' + V, '/static/reader.js' + V, '/static/vendor/htmx.min.js' + V,
  '/static/icon.svg' + V, '/static/icon-192.png' + V, '/static/manifest.webmanifest' + V,
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
  if (/^\/items\/[^/]+\/capture$/.test(url.pathname)) return false; // a capture poll must always ask the server
  return url.pathname === '/' || url.pathname.startsWith('/reader/') || url.pathname.startsWith('/items/') || url.pathname.startsWith('/clusters/');
}
// Archived copies and their assets never change for a given URL (page URLs carry ?g=<capture generation>).
function isArchiveGet(url) { return url.pathname.startsWith('/archive/'); }

async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  try {
    const resp = await fetch(request);
    if (resp && resp.ok && resp.type === 'basic') cache.put(request, resp.clone()).catch(() => {});
    return resp;
  } catch (_) {
    return new Response('', { status: 504 });
  }
}

// The Saved list asks for its articles to be cached so they open with no connection at all.
self.addEventListener('message', (event) => {
  const data = event.data || {};
  if (data.type !== 'warm' || !Array.isArray(data.urls)) return;
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    for (const path of data.urls.slice(0, 40)) {
      try {
        const url = new URL(path, self.location.origin);
        if (url.origin !== self.location.origin) continue;
        const req = new Request(url.href, { headers: { 'HX-Request': 'true' }, credentials: 'same-origin' });
        const key = cacheKey(req);
        if (await cache.match(key)) continue;
        const resp = await fetch(req);
        if (resp.ok) await cache.put(key, resp);
      } catch (_) { /* offline or gone: try again next visit */ }
    }
  })());
});
function isStateChange(url) {
  return /^\/items\/[^/]+\/(read|unread|star|unstar|tag|note)$/.test(url.pathname) || url.pathname === '/items/undo-read' || /\/mark-read$/.test(url.pathname);
}

const OFFLINE_HTML = '<div class="empty-state"><div class="empty-icon"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 3l18 18"/><path d="M5 10a12 12 0 0 1 4-2.5M12 6a12 12 0 0 1 9 4M8.5 13.5a7 7 0 0 1 2-1.2M12 10a7 7 0 0 1 5 2.5M12 17h.01"/></svg></div><div class="empty-title">You are offline</div><p>This page is not cached yet. Items you opened before are still available.</p></div>';

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
  return new Response(OFFLINE_HTML, { status: 503, headers: { 'Content-Type': 'text/html' } });
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.method === 'GET') {
    if (url.pathname.startsWith('/static/')) {
      // Cache-first, but never let a cache or network error reject respondWith: that would fail the
      // stylesheet and script for the whole page instead of just missing the cache.
      event.respondWith((async () => {
        try {
          const c = await caches.open(VERSION);
          const hit = await c.match(req);
          if (hit) return hit;
          const r = await fetch(req);
          if (r.ok) c.put(req, r.clone()).catch(() => {});
          return r;
        } catch (_) {
          try { return await fetch(req); } catch (__) { return new Response('', { status: 504 }); }
        }
      })());
    } else if (isArchiveGet(url)) {
      event.respondWith(cacheFirst(req));
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
