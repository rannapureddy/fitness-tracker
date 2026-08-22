// Minimal service worker: just enough to make the app installable and to
// speed up repeat loads of the static shell. Deliberately NOT caching
// anything under /api/ — this is a daily-use data-entry app, and a stale
// cached response there would be actively misleading (e.g. showing last
// week's plan or an old logged entry). Only the shell (HTML/JS/icons) is
// cached; every API call always goes straight to the network.

const CACHE_NAME = 'fitness-tracker-shell-v1';
const SHELL_ASSETS = [
  '/',
  '/chart.umd.js',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(
        names
          .filter((name) => name !== CACHE_NAME)
          .map((name) => caches.delete(name))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept API calls — always hit the network so data is current.
  if (url.pathname.startsWith('/api/')) return;

  // Only handle simple same-origin GETs; let everything else pass through.
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const networkFetch = fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        })
        .catch(() => cached);
      // Stale-while-revalidate: serve cached immediately if present, but
      // still refresh the cache in the background from the network.
      return cached || networkFetch;
    })
  );
});
