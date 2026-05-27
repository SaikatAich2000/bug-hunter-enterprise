// Bug Hunter — minimal service worker.
//
// We deliberately do NOT cache /api/* responses (they're tenant-scoped
// and would leak data between sessions on a shared device). The asset
// cache is purely for the static SPA files, keyed on the fingerprint
// the server bakes into the manifest query string (?v=…).
//
// On activation we sweep any old caches so a redeployed app version
// doesn't get served from a stale cache.

const CACHE_NAME = "bug-hunter-static-v1";

self.addEventListener("install", (event) => {
  // Take over immediately on the next reload.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  // Never touch API or auth pages — those must be live.
  if (url.pathname.startsWith("/api/") ||
      url.pathname === "/" ||
      url.pathname.endsWith(".html") ||
      url.pathname === "/login" ||
      url.pathname === "/signup" ||
      url.pathname === "/reset" ||
      url.pathname === "/accept-invite") {
    return; // fall through to network
  }
  // Cache-first for /static/ assets.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE_NAME).then((cache) =>
        cache.match(req).then((cached) => {
          if (cached) return cached;
          return fetch(req).then((res) => {
            if (res.ok) cache.put(req, res.clone());
            return res;
          });
        })
      )
    );
  }
});
