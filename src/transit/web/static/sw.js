"use strict";
// Service worker: lets the site install as an app and open with a weak or no connection.
// The app shell (page, scripts, styles, icons, map library) is served network-first and
// falls back to the last copy; live data under /api/ is never cached, so nothing stale is
// shown as live. Map tiles are left to the browser's own HTTP cache (range requests).
const SHELL_CACHE = "torontrack-shell-v12";
const SHELL = [
  "/",
  "/static/app.css",
  "/static/fonts/atkinson-hyperlegible-next-latin.woff2",
  "/static/fonts/atkinson-hyperlegible-next-latin-ext.woff2",
  "/static/theme.js",
  "/static/icons.js",
  "/static/guidance.js",
  "/static/motion.js",
  "/static/app.js",
  "/static/icon.svg",
  "/static/icon-192.png",
  "/static/manifest.webmanifest",
  "/static/vendor/maplibre-gl/maplibre-gl.css",
  "/static/vendor/maplibre-gl/maplibre-gl.mjs",
  "/static/vendor/maplibre-gl/maplibre-gl-shared.mjs",
  "/static/vendor/maplibre-gl/maplibre-gl-worker.mjs",
  "/static/vendor/pmtiles/pmtiles.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/map-data/") || url.pathname.startsWith("/data/")) return;
  const isShell = request.mode === "navigate" || url.pathname.startsWith("/static/");
  if (!isShell) return;
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then((cache) => cache.put(request.mode === "navigate" ? "/" : request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request.mode === "navigate" ? "/" : request).then((hit) => hit || Response.error())),
  );
});
