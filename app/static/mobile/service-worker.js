/* Andes Mobile PWA — service worker v2 */
const SW_VERSION = "andes-mobile-v6";
const STATIC_CACHE = `${SW_VERSION}-static`;
const HTML_CACHE = `${SW_VERSION}-html`;
const API_DASH_CACHE = `${SW_VERSION}-api-dash`;
const API_SWR_CACHE = `${SW_VERSION}-api-swr`;
const CDN_CACHE = `${SW_VERSION}-cdn`;

const PRECACHE_URLS = [
  "/m/",
  "/m/service-worker.js",
  "/static/mobile/mobile.min.css",
  "/static/mobile/mobile.min.js",
  "/static/mobile/offline-db.min.js",
  "/static/mobile/offline-ui.min.js",
  "/static/mobile/splash.min.js",
  "/static/mobile/manifest.json",
  "/static/mobile/icons/icon-192.png",
  "/static/mobile/icons/icon-512.png",
  "/static/mobile/icons/icon-192-maskable.png",
  "/static/mobile/icons/icon-512-maskable.png",
  "/static/mobile/icons/apple-touch-icon.png",
  "/static/mobile/lib/html5-qrcode.min.js",
  "/static/mobile/scanner.js",
];

const CDN_ASSETS = [];

const CACHE_FIRST_EXTENSIONS = [
  ".css",
  ".js",
  ".png",
  ".jpg",
  ".jpeg",
  ".gif",
  ".webp",
  ".svg",
  ".woff",
  ".woff2",
  ".json",
];

function isHtmlRequest(request) {
  if (request.mode === "navigate") return true;
  const accept = request.headers.get("accept") || "";
  return accept.includes("text/html");
}

function isCacheFirstAsset(url) {
  const path = url.pathname.toLowerCase();
  if (path.startsWith("/static/mobile/")) return true;
  return CACHE_FIRST_EXTENSIONS.some((ext) => path.endsWith(ext));
}

function isMobileScope(url) {
  return url.pathname === "/m" || url.pathname.startsWith("/m/");
}

function isDashboardApi(url) {
  return url.pathname === "/m/api/dashboard";
}

function isStaleWhileRevalidateApi(url) {
  if (url.pathname === "/m/api/buscar") return true;
  if (url.pathname.startsWith("/m/api/producto/")) return true;
  return false;
}

function isCatalogApi(url) {
  return url.pathname === "/m/api/catalogo";
}

function isCdnAsset(url) {
  return url.hostname === "unpkg.com" && url.pathname.includes("html5-qrcode");
}

async function cachePut(cacheName, request, response) {
  if (!response || response.status !== 200) return;
  const cache = await caches.open(cacheName);
  await cache.put(request, response.clone());
}

async function cacheFirst(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) return cached;
  const response = await fetch(request);
  await cachePut(cacheName, request, response);
  return response;
}

async function networkFirstHtml(request) {
  try {
    const response = await fetch(request);
    if (response && response.status === 200) {
      await cachePut(HTML_CACHE, request, response);
    }
    return response;
  } catch (_err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    const home = await caches.match("/m/");
    if (home) return home;
    return new Response("Sin conexión", { status: 503, headers: { "Content-Type": "text/plain; charset=utf-8" } });
  }
}

async function networkFirstApi(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response && response.status === 200) {
      await cachePut(cacheName, request, response);
    }
    return response;
  } catch (_err) {
    const cached = await caches.match(request);
    if (cached) return cached;
    return new Response(JSON.stringify({ success: false, offline: true, message: "Sin conexión" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

async function staleWhileRevalidate(request) {
  const cached = await caches.match(request);
  const networkPromise = fetch(request)
    .then(async (response) => {
      if (response && response.status === 200) {
        await cachePut(API_SWR_CACHE, request, response);
      }
      return response;
    })
    .catch(() => null);

  if (cached) {
    networkPromise.catch(() => {});
    return cached;
  }

  const network = await networkPromise;
  if (network) return network;

  return new Response(JSON.stringify({ success: false, offline: true, items: [], count: 0 }), {
    status: 503,
    headers: { "Content-Type": "application/json" },
  });
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(STATIC_CACHE);
      await cache.addAll(PRECACHE_URLS);
      const cdnCache = await caches.open(CDN_CACHE);
      await Promise.all(
        CDN_ASSETS.map(async (url) => {
          try {
            const res = await fetch(url, { mode: "cors" });
            if (res.ok) await cdnCache.put(url, res);
          } catch (_e) {
            /* CDN opcional en install */
          }
        })
      );
    })()
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key.startsWith("andes-mobile-") && !key.startsWith(SW_VERSION))
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);

  if (isCdnAsset(url)) {
    event.respondWith(cacheFirst(request, CDN_CACHE));
    return;
  }

  if (url.origin !== self.location.origin) return;

  const inScope = isMobileScope(url) || url.pathname.startsWith("/static/mobile/");
  if (!inScope) return;

  if (isCacheFirstAsset(url)) {
    event.respondWith(cacheFirst(request, STATIC_CACHE));
    return;
  }

  if (isDashboardApi(url)) {
    event.respondWith(networkFirstApi(request, API_DASH_CACHE));
    return;
  }

  if (isStaleWhileRevalidateApi(url)) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }

  if (isCatalogApi(url)) {
    event.respondWith(networkFirstApi(request, API_DASH_CACHE));
    return;
  }

  if (isHtmlRequest(request) && isMobileScope(url)) {
    event.respondWith(networkFirstHtml(request));
  }
});
