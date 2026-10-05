// Minimal service worker: makes Jarvis installable and caches the app shell.
// API calls always go to the network (personal data is never cached).
const CACHE = "jarvis-shell-v45";
const SHELL = ["/", "/static/app.js", "/static/lights.js", "/static/style.css", "/static/icon.svg", "/manifest.webmanifest"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== "jarvis-share").map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
// Android's share menu (manifest share_target): keep a shared image for the page, pass text in the address.
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method === "POST" && url.pathname === "/share-target") {
    event.respondWith((async () => {
      const form = await event.request.formData();
      const query = new URLSearchParams();
      for (const key of ["title", "text", "url"]) { const v = form.get(key); if (v) query.set(`share_${key}`, String(v).slice(0, 3000)); }
      const image = form.getAll("image").find((f) => f && f.size);
      if (image) {
        const cache = await caches.open("jarvis-share");
        await cache.put("/shared-image", new Response(image, { headers: { "Content-Type": image.type || "image/png" } }));
        query.set("share_image", "1");
      }
      return Response.redirect("/?" + query.toString(), 303);
    })());
    return;
  }
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
