// Minimal service worker: makes Jarvis installable and caches the app shell.
// API calls always go to the network (personal data is never cached).
const CACHE = "jarvis-shell-v28";
const SHELL = ["/", "/static/app.js", "/static/style.css", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) return;
  // network first, fall back to cache when offline
  event.respondWith(
    fetch(event.request).then((response) => {
      const copy = response.clone();
      if (response.ok) caches.open(CACHE).then((c) => c.put(event.request, copy));
      return response;
    }).catch(() => caches.match(event.request, { ignoreSearch: true }))
  );
});
