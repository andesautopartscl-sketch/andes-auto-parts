(function () {
  "use strict";

  var bannerId = "mobile-offline-banner";
  var skeletonId = "catalog-sync-skeleton";

  function isOnline() {
    return navigator.onLine !== false;
  }

  function ensureBanner() {
    var el = document.getElementById(bannerId);
    if (el) return el;
    el = document.createElement("div");
    el.id = bannerId;
    el.className = "mobile-offline-banner";
    el.setAttribute("role", "status");
    el.setAttribute("aria-live", "polite");
    el.hidden = true;
    el.textContent = "Sin conexión — mostrando datos cacheados";
    var header = document.querySelector(".mobile-header");
    if (header && header.parentNode) {
      header.parentNode.insertBefore(el, header.nextSibling);
    } else {
      document.body.insertBefore(el, document.body.firstChild);
    }
    return el;
  }

  function setOfflineUi(offline) {
    var banner = ensureBanner();
    banner.hidden = !offline;
    document.body.classList.toggle("mobile-app--offline", offline);

    document.querySelectorAll("[data-requires-online]").forEach(function (el) {
      if (offline) {
        el.setAttribute("aria-disabled", "true");
        if (el.tagName === "BUTTON" || el.tagName === "INPUT") {
          el.disabled = true;
        } else if (el.tagName === "A") {
          el.dataset.offlineHref = el.getAttribute("href") || "";
          el.removeAttribute("href");
          el.classList.add("m-disabled-link");
        }
        el.setAttribute("title", "Requiere internet");
      } else {
        el.removeAttribute("aria-disabled");
        if (el.tagName === "BUTTON" || el.tagName === "INPUT") {
          el.disabled = false;
        } else if (el.tagName === "A") {
          if (el.dataset.offlineHref) {
            el.setAttribute("href", el.dataset.offlineHref);
            delete el.dataset.offlineHref;
          }
          el.classList.remove("m-disabled-link");
        }
        el.removeAttribute("title");
      }
    });
  }

  function showCatalogSkeleton(show) {
    var sk = document.getElementById(skeletonId);
    if (!sk) return;
    sk.hidden = !show;
  }

  function syncCatalogBackground() {
    if (!window.AndesOfflineDb || !isOnline()) return Promise.resolve();
    var api = document.body.getAttribute("data-catalog-api");
    if (!api) return Promise.resolve();
    showCatalogSkeleton(true);
    return AndesOfflineDb.syncCatalog(api)
      .catch(function () {
        return null;
      })
      .finally(function () {
        showCatalogSkeleton(false);
      });
  }

  function initOfflineState() {
    setOfflineUi(!isOnline());
    window.addEventListener("online", function () {
      setOfflineUi(false);
      syncCatalogBackground();
    });
    window.addEventListener("offline", function () {
      setOfflineUi(true);
    });
  }

  function initCatalogSync() {
    if (!window.AndesOfflineDb || !isOnline()) return;
    syncCatalogBackground();
    setInterval(function () {
      if (isOnline()) syncCatalogBackground();
    }, AndesOfflineDb.CATALOG_TTL_MS);
  }

  function recordRecentFromPage() {
    if (!window.AndesOfflineDb) return;
    var producto = document.querySelector("[data-producto-codigo]");
    if (producto) {
      AndesOfflineDb.recordProduct(
        producto.getAttribute("data-producto-codigo"),
        producto.getAttribute("data-producto-desc")
      );
    }
    var venta = document.getElementById("mobile-venta-json");
    if (venta && venta.textContent) {
      try {
        AndesOfflineDb.recordVenta(JSON.parse(venta.textContent));
      } catch (_e) {}
    }
    var ingreso = document.getElementById("mobile-ingreso-json");
    if (ingreso && ingreso.textContent) {
      try {
        AndesOfflineDb.recordIngreso(JSON.parse(ingreso.textContent));
      } catch (_e) {}
    }
  }

  function forceSyncCatalog() {
    if (!window.AndesOfflineDb || !isOnline()) {
      return Promise.reject(new Error("offline"));
    }
    var api = document.body.getAttribute("data-catalog-api");
    if (!api) return Promise.reject(new Error("no api"));
    showCatalogSkeleton(true);
    return AndesOfflineDb.syncCatalog(api, { force: true })
      .then(function (result) {
        return result;
      })
      .finally(function () {
        showCatalogSkeleton(false);
      });
  }

  window.AndesCatalogSync = {
    force: forceSyncCatalog,
  };

  document.addEventListener("DOMContentLoaded", function () {
    initOfflineState();
    initCatalogSync();
    recordRecentFromPage();
  });
})();
