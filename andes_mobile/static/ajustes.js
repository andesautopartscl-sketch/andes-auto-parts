(function () {
  "use strict";

  var STORAGE = {
    theme: "andes_mobile_theme",
    notifStock: "andes_mobile_notif_stock",
    notifVentas: "andes_mobile_notif_ventas",
    notifIngresos: "andes_mobile_notif_ingresos",
    syncInterval: "andes_mobile_sync_interval_min",
  };

  function showToast(msg) {
    var el = document.getElementById("mobile-toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    el.classList.add("m-toast--visible");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      el.classList.remove("m-toast--visible");
      el.hidden = true;
    }, 3200);
  }

  function getBool(key, defaultVal) {
    try {
      var v = localStorage.getItem(key);
      if (v === null) return defaultVal;
      return v === "1" || v === "true";
    } catch (_e) {
      return defaultVal;
    }
  }

  function setBool(key, val) {
    try {
      localStorage.setItem(key, val ? "1" : "0");
    } catch (_e) {}
  }

  function applyTheme(dark) {
    document.documentElement.classList.toggle("mobile-theme-dark", !!dark);
    document.documentElement.classList.toggle("mobile-theme-light", !dark);
    document.body.classList.toggle("mobile-app--dark", !!dark);
  }

  function formatSyncDate(ts) {
    if (!ts) return "—";
    try {
      return new Date(Number(ts)).toLocaleString("es-CL", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch (_e) {
      return "—";
    }
  }

  function refreshLastSyncLabel() {
    var el = document.getElementById("set-last-sync");
    if (!el || !window.AndesOfflineDb) return;
    AndesOfflineDb.open()
      .then(function (db) {
        return AndesOfflineDb.getMeta(db, "catalog_synced_at");
      })
      .then(function (ts) {
        el.textContent = "Última sync: " + formatSyncDate(ts);
      })
      .catch(function () {
        el.textContent = "Última sync: —";
      });
  }

  function syncNow() {
    if (!navigator.onLine) {
      showToast("Sin conexión");
      return;
    }
    var api = document.body.getAttribute("data-catalog-api");
    if (!api || !window.AndesOfflineDb) {
      showToast("Sync no disponible");
      return;
    }
    showToast("Sincronizando…");
    var syncFn =
      window.AndesCatalogSync && AndesCatalogSync.force
        ? AndesCatalogSync.force.bind(AndesCatalogSync)
        : function () {
            return AndesOfflineDb.syncCatalog(api, { force: true });
          };
    syncFn()
      .then(function (r) {
        refreshLastSyncLabel();
        if (r && r.skipped) showToast("Catálogo ya estaba al día");
        else showToast("Catálogo sincronizado (" + (r && r.count ? r.count : 0) + ")");
      })
      .catch(function (err) {
        showToast((err && err.message) || "Error al sincronizar");
      });
  }

  function clearCache() {
    var chain = Promise.resolve();
    if (window.AndesOfflineDb && AndesOfflineDb.clearAll) {
      chain = AndesOfflineDb.clearAll();
    }
    chain
      .then(function () {
        if ("serviceWorker" in navigator) {
          return navigator.serviceWorker.getRegistrations().then(function (regs) {
            return Promise.all(
              regs.map(function (reg) {
                return reg.unregister();
              })
            );
          });
        }
      })
      .then(function () {
        showToast("Caché limpiada — recargando…");
        setTimeout(function () {
          window.location.reload();
        }, 800);
      })
      .catch(function () {
        showToast("No se pudo limpiar la caché");
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var themeEl = document.getElementById("set-theme-dark");
    var stockEl = document.getElementById("set-notif-stock");
    var ventasEl = document.getElementById("set-notif-ventas");
    var ingresosEl = document.getElementById("set-notif-ingresos");
    var intervalEl = document.getElementById("set-sync-interval");

    var dark = getBool(STORAGE.theme, false);
    if (themeEl) {
      themeEl.checked = dark;
      applyTheme(dark);
      themeEl.addEventListener("change", function () {
        dark = themeEl.checked;
        try {
          localStorage.setItem(STORAGE.theme, dark ? "1" : "0");
        } catch (_e) {}
        applyTheme(dark);
      });
    }

    if (stockEl) {
      stockEl.checked = getBool(STORAGE.notifStock, true);
      stockEl.addEventListener("change", function () {
        setBool(STORAGE.notifStock, stockEl.checked);
      });
    }
    if (ventasEl) {
      ventasEl.checked = getBool(STORAGE.notifVentas, true);
      ventasEl.addEventListener("change", function () {
        setBool(STORAGE.notifVentas, ventasEl.checked);
      });
    }
    if (ingresosEl) {
      ingresosEl.checked = getBool(STORAGE.notifIngresos, true);
      ingresosEl.addEventListener("change", function () {
        setBool(STORAGE.notifIngresos, ingresosEl.checked);
      });
    }

    if (intervalEl) {
      try {
        var saved = localStorage.getItem(STORAGE.syncInterval);
        if (saved) intervalEl.value = saved;
      } catch (_e) {}
      intervalEl.addEventListener("change", function () {
        try {
          localStorage.setItem(STORAGE.syncInterval, intervalEl.value);
          if (window.AndesOfflineDb) {
            var mins = parseInt(intervalEl.value, 10) || 30;
            AndesOfflineDb.CATALOG_TTL_MS = mins * 60 * 1000;
          }
        } catch (_e) {}
      });
    }

    var syncBtn = document.getElementById("set-sync-now");
    if (syncBtn) syncBtn.addEventListener("click", syncNow);

    var clearBtn = document.getElementById("set-clear-cache");
    if (clearBtn) clearBtn.addEventListener("click", clearCache);

    function refreshInstallUi() {
      var btn = document.getElementById("set-install-app");
      var hint = document.getElementById("set-install-hint");
      var group = document.getElementById("set-install-group");
      var api = window.AndesPwaInstall;
      if (!group || !api) return;
      if (api.isStandalone()) {
        group.hidden = true;
        return;
      }
      group.hidden = false;
      if (btn) {
        btn.hidden = !api.canPrompt();
        if (!btn._andesBound) {
          btn._andesBound = true;
          btn.addEventListener("click", function () {
            if (!api.canPrompt()) {
              showToast("Abre el menú ⋮ de Chrome → Instalar aplicación");
              return;
            }
            btn.disabled = true;
            api.prompt().then(function (choice) {
              btn.disabled = false;
              if (choice && choice.outcome === "accepted") {
                showToast("App instalada");
              } else if (choice && choice.outcome === "dismissed") {
                showToast("Instalación cancelada");
              } else {
                showToast("Abre el menú ⋮ de Chrome → Instalar aplicación");
              }
              refreshInstallUi();
            });
          });
        }
      }
      if (hint) {
        hint.innerHTML = api.canPrompt()
          ? "Opcional: toca el botón para crear el ícono. <strong>No es necesario</strong> para usar la app."
          : "<strong>No hace falta instalarla.</strong> Úsala desde Chrome; si quieres, guárdala en favoritos.";
      }
    }
    document.addEventListener("andes:pwa-install-change", refreshInstallUi);
    refreshInstallUi();
    setTimeout(refreshInstallUi, 800);

    refreshLastSyncLabel();
  });
})();
