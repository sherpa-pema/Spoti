const CACHE_NAME = 'spoti-offline-v10';
const SHELL_ASSETS = [
  '/',
  '/static/manifest.json',
  '/static/icon.png',
  'https://cdn.tailwindcss.com'
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

  // 1. Never cache streaming audio or downloads
  if (
    url.pathname.startsWith('/api/stream') ||
    url.pathname.startsWith('/api/download') ||
    url.pathname.startsWith('/api/library/file') ||
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
          const exact = await caches.match(event.request);
          if (exact) return exact;
          const root = await caches.match('/');
          if (root) return root;
          return new Response('Spoti offline shell not available.', {
            status: 503,
            headers: { 'Content-Type': 'text/plain' }
          });
        })
    );
    return;
  }

  // 3. Static assets & Tailwind CDN
  if (url.origin === self.location.origin || url.hostname === 'cdn.tailwindcss.com') {
    event.respondWith(
      caches.match(event.request).then((cached) => {
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
