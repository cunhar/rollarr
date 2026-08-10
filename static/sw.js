// Rolarr Service Worker — Offline Fallback
// Cache version: bump this string to force cache refresh on next visit
const CACHE_NAME = 'rolarr-v1';
const OFFLINE_URL = '/offline';

// Resources to pre-cache on install
const PRECACHE_URLS = [
  '/offline',
  '/static/favicon.png',
];

// ── Install: pre-cache the offline page ─────────────────────────────────────
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(PRECACHE_URLS);
    })
  );
  // Activate immediately, don't wait for old SW to be gone
  self.skipWaiting();
});

// ── Activate: clean up old caches ───────────────────────────────────────────
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((cacheNames) => {
      return Promise.all(
        cacheNames
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      );
    })
  );
  // Take control of all open clients immediately
  self.clients.claim();
});

// ── Fetch: try network first, fall back to offline page ─────────────────────
self.addEventListener('fetch', (event) => {
  // Only intercept same-origin navigation requests (page loads)
  if (event.request.mode !== 'navigate') return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        // Network succeeded — also update the main page cache opportunistically
        if (response.ok && event.request.url === self.location.origin + '/') {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      })
      .catch(() => {
        // Network failed — serve the offline fallback from cache
        return caches.match(OFFLINE_URL);
      })
  );
});
