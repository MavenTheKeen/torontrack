"use strict";
// Service worker: lets the site install as an app and open with a weak or no connection.
// The app shell (page, scripts, styles, icons, map library) is served network-first and
// falls back to the last copy. The data behind the text pages (status, lines, stations,
// stops, reliability, events) is network-first too, with the last copy kept for when the
// network fails or takes over DATA_WAIT_MS; that copy carries X-Torontrack-Saved-At, so
// the page says it is not live. Moving vehicles, the map's data and trip plans are never
// served from a copy. Map tiles are left to the browser's own HTTP cache (range requests).
const SHELL_CACHE = "torontrack-shell-v42";
const DATA_CACHE = "torontrack-data-v1";
const DATA_WAIT_MS = 8000;
const CACHED_DATA = /^\/api\/(alerts|lines(\/[^/]+)?|stations\/[^/]+|stops\/[^/]+|reliability|report|report-card(\/[^/]+)?|datasets|events|agenda|regional\/stations\/[^/]+)$/;
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
  "/static/vendor/maplibre-gl/maplibre-gl-worker.mjs",
  "/static/vendor/pmtiles/pmtiles.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL_CACHE && k !== DATA_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin) return;
  if (CACHED_DATA.test(url.pathname)) {
    event.respondWith(dataResponse(request));
    return;
  }
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

// Network first; the stored copy, marked with when it was fetched, if the network fails
// or is slower than DATA_WAIT_MS. A late network answer still refreshes the copy.
function dataResponse(request) {
  const network = fetch(request).then(async (response) => {
    if (response.ok) {
      const headers = new Headers(response.headers);
      headers.set("X-Torontrack-Fetched-At", new Date().toISOString());
      const copy = new Response(await response.clone().blob(), { status: 200, headers });
      await caches.open(DATA_CACHE).then((cache) => cache.put(request, copy));
    }
    return response;
  });
  const stored = () =>
    caches.match(request, { cacheName: DATA_CACHE }).then((hit) => {
      if (!hit) return null;
      const headers = new Headers(hit.headers);
      headers.set("X-Torontrack-Saved-At", hit.headers.get("X-Torontrack-Fetched-At") || "");
      return new Response(hit.body, { status: 200, headers });
    });
  const slow = new Promise((resolve) => setTimeout(resolve, DATA_WAIT_MS)).then(stored);
  return Promise.race([network.catch(() => null), slow]).then(
    (first) => first || network.catch(() => stored().then((hit) => hit || Response.error())),
  );
}
