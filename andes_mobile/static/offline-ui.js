(function () {
  "use strict";

  var bannerId = "mobile-offline-banner";
  var chipId = "catalog-sync-chip";
  var SYNC_START_DELAY_MS = 3000;
  var SYNC_RESUME_DELAY_MS = 1200;
  var PROGRESS_POLL_MS = 2500;

  var syncState = {
    inFlight: null,
    sessionId: null,
    pollTimer: null,
    startTimer: null,
    resumeTimer: null,
    chipExpandedUntil: 0,
    userPaused: false,
  };

  function isOnline() {
    return navigator.onLine !== false;
  }

  function newSessionId() {
    return (
      Date.now().toString(36) +
      "-" +
      Math.random().toString(36).slice(2, 10)
    );
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

  function getSyncChip() {
    return document.getElementById(chipId);
  }

  function updateCatalogSyncUi(progress, options) {
    options = options || {};
    var chip = getSyncChip();
    if (!chip) return;
    var textEl = chip.querySelector(".catalog-sync-chip__text");
    var done = progress && progress.done ? progress.done : 0;
    var total = progress && progress.total ? progress.total : 0;
    var show = !!options.show;
    var expanded = !!options.expanded || Date.now() < syncState.chipExpandedUntil;

    chip.hidden = !show;
    chip.classList.toggle("catalog-sync-chip--active", show);
    chip.classList.toggle("catalog-sync-chip--expanded", expanded && total > 0);

    if (textEl) {
      if (total > 0) {
        textEl.textContent = formatCount(done) + "/" + formatCount(total);
        chip.setAttribute(
          "aria-label",
          "Sincronizando catálogo " + formatCount(done) + " de " + formatCount(total)
        );
      } else {
        textEl.textContent = "";
        chip.setAttribute("aria-label", "Sincronizando catálogo");
      }
    }
  }

  function hideCatalogSyncUi() {
    updateCatalogSyncUi({ done: 0, total: 0 }, { show: false });
    document.body.classList.remove("mobile-app--catalog-sync");
  }

  function showCatalogSyncUi(progress, expanded) {
    document.body.classList.add("mobile-app--catalog-sync");
    if (expanded) {
      syncState.chipExpandedUntil = Date.now() + 2200;
    }
    updateCatalogSyncUi(progress || { done: 0, total: 0 }, { show: true, expanded: expanded });
  }

  function stopProgressPoll() {
    if (syncState.pollTimer) {
      clearInterval(syncState.pollTimer);
      syncState.pollTimer = null;
    }
  }

  function startProgressPoll() {
    if (syncState.pollTimer || !window.AndesOfflineDb) return;
    syncState.pollTimer = setInterval(function () {
      if (!window.AndesOfflineDb) return;
      AndesOfflineDb.getCatalogSyncProgress().then(function (progress) {
        if (progress.inProgress) {
          showCatalogSyncUi(progress, false);
          document.dispatchEvent(
            new CustomEvent("andes:catalog-sync-progress", { detail: progress })
          );
        } else if (!syncState.inFlight) {
          stopProgressPoll();
          hideCatalogSyncUi();
        }
      });
    }, PROGRESS_POLL_MS);
  }

  function dispatchProgress(progress) {
    updateCatalogSyncUi(progress, { show: true, expanded: false });
    document.dispatchEvent(
      new CustomEvent("andes:catalog-sync-progress", { detail: progress })
    );
  }

  function runCatalogSync(options) {
    options = options || {};
    if (!window.AndesOfflineDb || !isOnline()) {
      return Promise.resolve({ skipped: true, count: 0 });
    }
    var api = document.body.getAttribute("data-catalog-api");
    if (!api) {
      return Promise.resolve({ skipped: true, count: 0 });
    }
    if (syncState.inFlight) return syncState.inFlight;

    if (!syncState.sessionId) {
      syncState.sessionId = newSessionId();
    }

    var force = !!options.force;
    var silent = !!options.silent;

    syncState.inFlight = AndesOfflineDb.getCatalogSyncProgress()
      .then(function (existing) {
        showCatalogSyncUi(
          existing.inProgress ? existing : { done: 0, total: 0 },
          !!force || existing.inProgress
        );
        return AndesOfflineDb.syncCatalog(api, {
          force: force,
          sessionId: syncState.sessionId,
          onProgress: function (progress) {
            dispatchProgress(progress);
          },
        });
      })
      .then(function (result) {
        if (result && result.lockHeld) {
          startProgressPoll();
          return AndesOfflineDb.getCatalogSyncProgress().then(function (progress) {
            if (progress.inProgress) {
              showCatalogSyncUi(progress, false);
            }
            return result;
          });
        }
        stopProgressPoll();
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
        if (!syncState.pollTimer) {
          hideCatalogSyncUi();
        }
        syncState.inFlight = null;
      });

    return syncState.inFlight;
  }

  function scheduleAutoSync() {
    clearTimeout(syncState.startTimer);
    syncState.startTimer = setTimeout(function () {
      if (!window.AndesOfflineDb || !isOnline()) return;
      AndesOfflineDb.assessCatalogSyncNeed().then(function (plan) {
        if (!plan.needed) return;
        if (syncState.userPaused) return;
        var kick = function () {
          runCatalogSync({ silent: true }).catch(function () {});
        };
        if (window.requestIdleCallback) {
          window.requestIdleCallback(kick, { timeout: 5000 });
        } else {
          kick();
        }
      });
    }, SYNC_START_DELAY_MS);
  }

  function pauseCatalogSyncForUser() {
    syncState.userPaused = true;
    if (window.AndesOfflineDb) {
      AndesOfflineDb.pauseCatalogSync();
    }
  }

  function resumeCatalogSyncForUser() {
    clearTimeout(syncState.resumeTimer);
    syncState.resumeTimer = setTimeout(function () {
      syncState.userPaused = false;
      if (window.AndesOfflineDb) {
        AndesOfflineDb.resumeCatalogSync();
      }
      if (!syncState.inFlight && isOnline() && window.AndesOfflineDb) {
        AndesOfflineDb.assessCatalogSyncNeed().then(function (plan) {
          if (plan.needed) {
            runCatalogSync({ silent: true }).catch(function () {});
          }
        });
      }
    }, SYNC_RESUME_DELAY_MS);
  }

  function initOfflineState() {
    setOfflineUi(!isOnline());
    window.addEventListener("online", function () {
      setOfflineUi(false);
      scheduleAutoSync();
    });
    window.addEventListener("offline", function () {
      setOfflineUi(true);
    });
    window.addEventListener("pagehide", function () {
      stopProgressPoll();
      clearTimeout(syncState.startTimer);
      clearTimeout(syncState.resumeTimer);
      if (window.AndesOfflineDb && syncState.sessionId && AndesOfflineDb.releaseSyncSession) {
        AndesOfflineDb.releaseSyncSession(syncState.sessionId);
      }
    });
  }

  function initCatalogSync() {
    if (!window.AndesOfflineDb || !isOnline()) return;
    syncState.sessionId = newSessionId();
    AndesOfflineDb.getCatalogSyncProgress().then(function (progress) {
      if (progress.inProgress) {
        showCatalogSyncUi(progress, true);
        startProgressPoll();
      }
    });
    scheduleAutoSync();
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
    syncState.userPaused = false;
    if (AndesOfflineDb.resumeCatalogSync) {
      AndesOfflineDb.resumeCatalogSync();
    }
    showCatalogSyncUi({ done: 0, total: 0 }, true);
    return runCatalogSync({ force: true, silent: false });
  }

  window.AndesCatalogSync = {
    force: forceSyncCatalog,
    background: function (options) {
      return runCatalogSync(options || { silent: true });
    },
    pause: pauseCatalogSyncForUser,
    resume: resumeCatalogSyncForUser,
  };

  document.addEventListener("DOMContentLoaded", function () {
    initOfflineState();
    initCatalogSync();
    recordRecentFromPage();
  });
})();
