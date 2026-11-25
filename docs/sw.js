// Service worker: permite instalar la app y abrirla sin internet.
// Siempre intenta primero la red (así la lista nunca queda vieja) y guarda una copia;
// si no hay conexión, muestra la última copia guardada.
const CACHE = 'tareas-uni-v1';
const BASE = ['./', './index.html', './manifest.json', './icons/icon-192.png', './icons/icon-512.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    for (const k of await caches.keys()) if (k !== CACHE) await caches.delete(k);
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  // tareas.json se pide con ?t=… para saltar cachés: se guarda una sola copia sin ese parámetro
  const clave = url.pathname.endsWith('/tareas.json') ? url.origin + url.pathname : e.request;
  e.respondWith((async () => {
    try {
      const resp = await fetch(e.request, { cache: 'no-store' });
      if (resp.ok) {
        const c = await caches.open(CACHE);
        await c.put(clave, resp.clone());
      }
      return resp;
    } catch (err) {
      const guardada = await caches.match(clave, { ignoreSearch: true });
      if (guardada) return guardada;
      if (e.request.mode === 'navigate') {
        const inicio = await caches.match('./index.html');
        if (inicio) return inicio;
      }
      throw err;
    }
  })());
});
