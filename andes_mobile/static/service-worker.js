/* Andes Mobile PWA — service worker v39 */
const SW_VERSION = "andes-mobile-v39";
const CACHE_PREFIX = `${SW_VERSION}-`;
const STATIC_CACHE = `${SW_VERSION}-static`;
const HTML_CACHE = `${SW_VERSION}-html`;
const API_DASH_CACHE = `${SW_VERSION}-api-dash`;
const API_SWR_CACHE = `${SW_VERSION}-api-swr`;
const CDN_CACHE = `${SW_VERSION}-cdn`;
// Mismo sufijo ?v= que inyectan las plantillas: si difiere, el service worker
// precachea una URL distinta de la que pide la página y el precache no sirve.
const ASSET_V = SW_VERSION.split("-v").pop();

const PRECACHE_URLS = [
  // Solo assets estáticos en el install. Las rutas HTML (/m/...) exigen sesión
  // y, si una falla, Chrome no deja instalar la PWA. El HTML se cachea al navegar.
  "/m/service-worker.js",
  `/static/mobile/mobile.css?v=${ASSET_V}`,
  `/static/mobile/fonts/inter.css?v=${ASSET_V}`,
  "/static/mobile/fonts/inter-400.woff2",
  "/static/mobile/fonts/inter-500.woff2",
  "/static/mobile/fonts/inter-600.woff2",
  "/static/mobile/fonts/inter-700.woff2",
  `/static/mobile/mobile.js?v=${ASSET_V}`,
  `/static/mobile/offline-db.js?v=${ASSET_V}`,
  `/static/mobile/offline-ui.js?v=${ASSET_V}`,
  `/static/mobile/splash.js?v=${ASSET_V}`,
  // Scripts de módulo. Deben llevar el mismo ?v= que inyectan las plantillas:
  // la caché indexa por URL completa, así que un sufijo distinto guarda una
  // entrada que la página nunca pide y el precache queda inservible.
  // scripts/smoke_rutas_mobile.py verifica que esta lista siga completa.
  `/static/mobile/producto_gallery.js?v=${ASSET_V}`,
  `/static/mobile/producto_accordion.js?v=${ASSET_V}`,
  `/static/mobile/home.js?v=${ASSET_V}`,
  `/static/mobile/oc_clientes.js?v=${ASSET_V}`,
  `/static/mobile/oc_clientes_detalle.js?v=${ASSET_V}`,
  `/static/mobile/scanner.js?v=${ASSET_V}`,
  `/static/mobile/ingreso_rapida.js?v=${ASSET_V}`,
  `/static/mobile/importar_imagenes.js?v=${ASSET_V}`,
  `/static/mobile/ajustes.js?v=${ASSET_V}`,
  `/static/mobile/ajuste_stock.js?v=${ASSET_V}`,
  `/static/mobile/venta_rapida.js?v=${ASSET_V}`,
  `/static/mobile/lib/html5-qrcode.min.js?v=${ASSET_V}`,
  "/static/mobile/manifest.json",
  "/static/mobile/icons/icon-192.png",
  "/static/mobile/icons/icon-512.png",
  "/static/mobile/icons/icon-192-maskable.png",
  "/static/mobile/icons/icon-512-maskable.png",
  "/static/mobile/icons/apple-touch-icon.png",
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

function isProductSearchApi(url) {
  return url.pathname === "/m/api/productos/buscar";
}

function isCatalogApi(url) {
  return url.pathname === "/m/api/catalogo";
}

function isLabelsPrint(url) {
  return url.pathname === "/m/etiquetas/imprimir";
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
      // No usar cache.addAll: si UNA URL falla (ruta con login, 302 raro,
      // cold start de Render), falla TODO el install del SW y Chrome deja
      // de ofrecer "Instalar aplicación" / rompe el acceso desde el ícono.
      await Promise.all(
        PRECACHE_URLS.map(async (url) => {
          try {
            const res = await fetch(url, { credentials: "same-origin" });
            if (res && res.ok) await cache.put(url, res.clone());
          } catch (_e) {
            /* asset opcional en install */
          }
        })
      );
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
      // No llamar skipWaiting aquí: al instalar la PWA Chrome activa el SW y,
      // si forzamos el cambio de controlador, la página se recarga y tira al login.
    })()
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys
          .filter((key) => key.startsWith("andes-mobile-") && !key.startsWith(CACHE_PREFIX))
          .map((key) => caches.delete(key))
      );
      await self.clients.claim();
    })()
  );
});

self.addEventListener("message", (event) => {
  if (!event.data || event.data.type !== "SKIP_WAITING") return;
  self.skipWaiting();
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

  if (isProductSearchApi(url)) {
    event.respondWith(networkFirstApi(request, API_SWR_CACHE));
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

  if (isLabelsPrint(url)) {
    event.respondWith(fetch(request));
    return;
  }

  if (isHtmlRequest(request) && isMobileScope(url)) {
    // Ficha de producto / stock cambian seguido: no cachear HTML o la PWA
    // muestra cantidades viejas (p. ej. stock 2+2 cuando ya hay 9).
    if (
      url.pathname.startsWith("/m/producto/") ||
      url.pathname.startsWith("/m/stock/") ||
      url.pathname.startsWith("/m/ajustar-stock/")
    ) {
      event.respondWith(fetch(request));
      return;
    }
    event.respondWith(networkFirstHtml(request));
  }
});
