(function () {
  "use strict";

  var bannerId = "mobile-offline-banner";
  var skeletonId = "catalog-sync-skeleton";
  var syncInFlight = null;

  function isOnline() {
    return navigator.onLine !== false;
  }

  function showToast(msg, kind) {
    if (window.AndesMobileToast) {
      window.AndesMobileToast(msg, kind);
      return;
    }
    var toast = document.getElementById("mobile-toast");
    if (!toast) return;
    toast.textContent = msg;
    toast.hidden = false;
    toast.classList.remove("m-toast--error", "m-toast--success");
    if (kind === "error") toast.classList.add("m-toast--error");
    if (kind === "success") toast.classList.add("m-toast--success");
    toast.classList.add("m-toast--visible");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      toast.classList.remove("m-toast--visible", "m-toast--error", "m-toast--success");
      toast.hidden = true;
    }, 4200);
  }

  function formatCount(n) {
    return String(Number(n || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
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

  function updateCatalogSyncUi(progress) {
    var sk = document.getElementById(skeletonId);
    if (!sk) return;
    var text = sk.querySelector(".catalog-sync-skeleton__text");
    var bar = sk.querySelector(".catalog-sync-skeleton__bar-fill");
    var done = progress && progress.done ? progress.done : 0;
    var total = progress && progress.total ? progress.total : 0;
    if (text) {
      if (total > 0) {
        text.textContent =
          "Sincronizando catálogo… " + formatCount(done) + "/" + formatCount(total);
      } else {
        text.textContent = "Sincronizando catálogo…";
      }
    }
    if (bar) {
      var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 8;
      bar.style.width = pct + "%";
    }
  }

  function showCatalogSkeleton(show, progress) {
    var sk = document.getElementById(skeletonId);
    if (!sk) return;
    sk.hidden = !show;
    sk.classList.toggle("catalog-sync-skeleton--active", !!show);
    if (show) {
      updateCatalogSyncUi(progress || { done: 0, total: 0 });
    }
  }

  function syncCatalogBackground(options) {
    options = options || {};
    if (!window.AndesOfflineDb || !isOnline()) {
      return Promise.resolve({ skipped: true, count: 0 });
    }
    var api = document.body.getAttribute("data-catalog-api");
    if (!api) {
      return Promise.resolve({ skipped: true, count: 0 });
    }
    if (syncInFlight) return syncInFlight;

    var force = !!options.force;
    var silent = !!options.silent;
    showCatalogSkeleton(true, { done: 0, total: 0 });

    syncInFlight = AndesOfflineDb.syncCatalog(api, {
      force: force,
      onProgress: function (progress) {
        updateCatalogSyncUi(progress);
        document.dispatchEvent(
          new CustomEvent("andes:catalog-sync-progress", { detail: progress })
        );
      },
    })
      .then(function (result) {
        if (!silent && result && !result.skipped) {
          showToast(
            "Catálogo sincronizado (" + formatCount(result.count) + " productos)",
            "success"
          );
        }
        document.dispatchEvent(
          new CustomEvent("andes:catalog-sync-done", { detail: result || {} })
        );
        return result;
      })
      .catch(function (err) {
        var msg =
          (err && err.message) ||
          "No se pudo sincronizar el catálogo. La búsqueda usará el servidor.";
        if (!silent) {
          showToast(msg, "error");
        }
        document.dispatchEvent(
          new CustomEvent("andes:catalog-sync-error", { detail: { message: msg } })
        );
        throw err;
      })
      .finally(function () {
        showCatalogSkeleton(false);
        syncInFlight = null;
      });

    return syncInFlight;
  }

  function initOfflineState() {
    setOfflineUi(!isOnline());
    window.addEventListener("online", function () {
      setOfflineUi(false);
      syncCatalogBackground({ silent: true });
    });
    window.addEventListener("offline", function () {
      setOfflineUi(true);
    });
  }

  function initCatalogSync() {
    if (!window.AndesOfflineDb || !isOnline()) return;
    syncCatalogBackground({ silent: true }).catch(function () {});
    setInterval(function () {
      if (isOnline()) {
        syncCatalogBackground({ silent: true }).catch(function () {});
      }
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
      return Promise.reject(new Error("Sin conexión"));
    }
    var api = document.body.getAttribute("data-catalog-api");
    if (!api) return Promise.reject(new Error("API de catálogo no configurada"));
    return syncCatalogBackground({ force: true, silent: false });
  }

  window.AndesCatalogSync = {
    force: forceSyncCatalog,
    background: syncCatalogBackground,
  };

  document.addEventListener("DOMContentLoaded", function () {
    initOfflineState();
    initCatalogSync();
    recordRecentFromPage();
  });
})();
