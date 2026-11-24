const CACHE_NAME = 'spoti-offline-v34';
const SHELL_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icon.png',
  '/static/css/tailwind.css',
  '/static/js/db.js',
  '/static/js/app.js'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      for (const asset of SHELL_ASSETS) {
        try {
          await cache.add(asset);
        } catch (err) {
          console.warn('[SW] Pre-caching asset failed:', asset, err);
        }
      }
    })
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      );
    })
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 1. Bypass Service Worker entirely for non-GET methods, all API endpoints, and streaming media
  if (
    event.request.method !== 'GET' ||
    url.pathname.startsWith('/api/') ||
    url.hostname.includes('googlevideo.com')
  ) {
    return;
  }

  // 2. Navigation requests (Root page, PWA launch)
  if (
    event.request.mode === 'navigate' ||
    event.request.destination === 'document' ||
    (event.request.method === 'GET' && event.request.headers.get('accept')?.includes('text/html'))
  ) {
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(async () => {
          // Offline fallback: Return cached root shell
          const exact = await caches.match(event.request, { ignoreSearch: true });
          if (exact) return exact;
          const root = await caches.match('/', { ignoreSearch: true });
          if (root) return root;

          // Responsive HTML offline fallback if cache is completely empty
          return new Response(`<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>Spoti - Offline</title>
  <style>
    body {
      margin: 0; padding: 2rem; background: #090A10; color: #F8FAFC;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      height: 100vh; box-sizing: border-box; text-align: center;
    }
    h2 { margin: 0 0 0.5rem 0; font-size: 1.25rem; font-weight: 700; color: #22C55E; }
    p { margin: 0 0 1.5rem 0; color: #94A3B8; font-size: 0.875rem; max-width: 280px; }
    button {
      height: 44px; padding: 0 1.5rem; background: #22C55E; color: #090A10;
      border: none; border-radius: 12px; font-weight: 600; font-size: 0.875rem; cursor: pointer;
    }
  </style>
</head>
<body>
  <h2>Spoti is Offline</h2>
  <p>Connect to Wi-Fi to synchronize or reload the app.</p>
  <button onclick="location.reload()">Reload Spoti</button>
</body>
</html>`, {
            status: 200,
            headers: { 'Content-Type': 'text/html; charset=utf-8' }
          });
        })
    );
    return;
  }

  // 3. Static assets (CSS, JS modules, images, manifest)
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(event.request, { ignoreSearch: true }).then((cached) => {
        if (cached) {
          // Background revalidation
          fetch(event.request)
            .then((fresh) => {
              if (fresh && fresh.status === 200) {
                caches.open(CACHE_NAME).then((cache) => cache.put(event.request, fresh));
              }
            })
            .catch(() => {});
          return cached;
        }

        return fetch(event.request)
          .then((response) => {
            if (response && response.status === 200) {
              const copy = response.clone();
              caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
            }
            return response;
          })
          .catch(() => cached);
      })
    );
    return;
  }
});
