(function () {
  "use strict";

  function initMasMenu() {
    var toggle = document.getElementById("mobile-mas-toggle");
    var menu = document.getElementById("mobile-mas-menu");
    if (!toggle || !menu) return;

    function openMenu() {
      menu.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      document.body.classList.add("m-mas-open");
    }

    function closeMenu() {
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      document.body.classList.remove("m-mas-open");
    }

    toggle.addEventListener("click", function (e) {
      e.preventDefault();
      if (menu.hidden) openMenu();
      else closeMenu();
    });

    menu.querySelectorAll("[data-mas-close]").forEach(function (el) {
      el.addEventListener("click", closeMenu);
    });
  }

  function initSearchDebounce() {
    var input = document.getElementById("mobile-search-input");
    var results = document.getElementById("mobile-search-results");
    var skeleton = document.getElementById("mobile-search-skeleton");
    if (!input || !results) return;

    var api = results.getAttribute("data-api");
    if (!api) return;

    var timer = null;
    var lastQ = "";

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    function renderItems(items) {
      if (!items.length) {
        results.innerHTML = '<p class="m-empty">Sin resultados.</p>';
        return;
      }
      var html = '<ul class="m-card-list">';
      items.forEach(function (r) {
        var href = "/m/producto/" + encodeURIComponent(r.codigo || "");
        html +=
          '<li><a href="' +
          href +
          '" class="m-result-card">' +
          '<span class="m-result-card__code">' +
          escapeHtml(r.codigo || "") +
          "</span>" +
          '<span class="m-result-card__desc">' +
          escapeHtml(r.descripcion || "") +
          "</span>" +
          '<div class="m-result-card__footer">' +
          '<span class="m-badge">Stock ' +
          escapeHtml(String(r.stock != null ? r.stock : 0)) +
          "</span>" +
          '<span class="m-result-card__price">' +
          escapeHtml(r.precio_fmt || "—") +
          "</span></div></a></li>";
      });
      html += "</ul>";
      results.innerHTML = html;
    }

    function setLoading(on) {
      if (!skeleton) return;
      skeleton.hidden = !on;
      if (on) results.classList.add("m-results--loading");
      else results.classList.remove("m-results--loading");
    }

    function fetchLocal(q) {
      if (!window.AndesOfflineDb) return Promise.resolve([]);
      return AndesOfflineDb.searchLocal(q, 30);
    }

    function fetchResults(q) {
      if (q.length < 2) {
        results.innerHTML = '<p class="m-hint">Escribe al menos 2 caracteres para buscar.</p>';
        return;
      }
      setLoading(true);
      var offline = !navigator.onLine;
      var request = offline
        ? fetchLocal(q).then(function (items) {
            return { items: items, offline: true };
          })
        : fetch(api + "?q=" + encodeURIComponent(q), {
            headers: { "X-Requested-With": "XMLHttpRequest" },
          })
            .then(function (res) {
              return res.json();
            })
            .catch(function () {
              return fetchLocal(q).then(function (items) {
                return { items: items, offline: true };
              });
            });

      request
        .then(function (data) {
          if (input.value.trim() !== q) return;
          var items = (data && data.items) || [];
          renderItems(items);
          if (data && data.offline && items.length) {
            results.insertAdjacentHTML(
              "afterbegin",
              '<p class="m-hint m-hint--offline">Resultados desde catálogo offline.</p>'
            );
          }
        })
        .catch(function () {
          results.innerHTML = '<p class="m-empty">Error al buscar. Intenta de nuevo.</p>';
        })
        .finally(function () {
          setLoading(false);
        });
    }

    input.addEventListener("input", function () {
      var q = input.value.trim();
      if (q === lastQ) return;
      lastQ = q;
      clearTimeout(timer);
      timer = setTimeout(function () {
        fetchResults(q);
      }, 300);
    });
  }

  function initPullToRefresh() {
    var zones = document.querySelectorAll("[data-pull-refresh]");
    if (!zones.length || !("ontouchstart" in window)) return;

    zones.forEach(function (zone) {
      var startY = 0;
      var pulling = false;

      zone.addEventListener(
        "touchstart",
        function (e) {
          if (window.scrollY > 8) return;
          startY = e.touches[0].clientY;
          pulling = true;
        },
        { passive: true }
      );

      zone.addEventListener(
        "touchmove",
        function (e) {
          if (!pulling) return;
          var dy = e.touches[0].clientY - startY;
          if (dy > 72) zone.classList.add("m-pull-ready");
          else zone.classList.remove("m-pull-ready");
        },
        { passive: true }
      );

      zone.addEventListener("touchend", function () {
        if (zone.classList.contains("m-pull-ready")) {
          zone.classList.remove("m-pull-ready");
          window.location.reload();
        }
        pulling = false;
      });
    });
  }

  function initNavPlaceholders() {
    document.querySelectorAll("[data-nav-placeholder]").forEach(function (el) {
      el.addEventListener("click", function (e) {
        e.preventDefault();
      });
    });
  }

  function initPwaAutoUpdate() {
    if (!("serviceWorker" in navigator)) return;

    var body = document.body;
    var swUrl = (body && body.getAttribute("data-sw-url")) || "/m/service-worker.js";
    var UPDATE_INTERVAL_MS = 30 * 60 * 1000;
    var BANNER_FALLBACK_MS = 4500;
    var reloadPending = false;
    var bannerEl = null;
    var bannerTimer = null;

    function logUpdate(msg) {
      console.log("[Andes PWA Update]", msg);
    }

    function ensureUpdateBanner() {
      if (bannerEl) return bannerEl;
      bannerEl = document.createElement("div");
      bannerEl.id = "mobile-sw-update-banner";
      bannerEl.className = "mobile-sw-update-banner";
      bannerEl.setAttribute("role", "status");
      bannerEl.hidden = true;
      bannerEl.innerHTML =
        '<span class="mobile-sw-update-banner__text">Nueva versión disponible — Actualizar</span>' +
        '<button type="button" class="mobile-sw-update-banner__btn" id="mobile-sw-update-btn">Actualizar</button>';
      document.body.appendChild(bannerEl);
      return bannerEl;
    }

    function showUpdateBanner(registration) {
      var banner = ensureUpdateBanner();
      banner.hidden = false;
      document.body.classList.add("mobile-app--update-available");
      var btn = document.getElementById("mobile-sw-update-btn");
      if (btn && !btn._andesBound) {
        btn._andesBound = true;
        btn.addEventListener("click", function () {
          applyUpdate(registration, false);
        });
      }
    }

    function hideUpdateBanner() {
      if (!bannerEl) return;
      bannerEl.hidden = true;
      document.body.classList.remove("mobile-app--update-available");
    }

    function scheduleBannerFallback(registration) {
      clearTimeout(bannerTimer);
      bannerTimer = setTimeout(function () {
        if (reloadPending) return;
        if (registration.waiting) {
          logUpdate("Mostrando banner de actualización (fallback)");
          showUpdateBanner(registration);
        }
      }, BANNER_FALLBACK_MS);
    }

    function applyUpdate(registration, silent) {
      var waiting = registration.waiting;
      if (!waiting) return;
      logUpdate(silent ? "Actualización silenciosa" : "Actualización manual");
      if (silent) hideUpdateBanner();
      reloadPending = true;
      waiting.postMessage({ type: "SKIP_WAITING" });
      if (!silent) {
        window.setTimeout(function () {
          window.location.reload();
        }, 400);
      }
    }

    function onUpdateReady(registration) {
      if (!navigator.serviceWorker.controller) return;
      if (!registration.waiting) return;
      logUpdate("Nueva versión detectada");
      applyUpdate(registration, true);
      scheduleBannerFallback(registration);
    }

    function watchInstallingWorker(registration, worker) {
      if (!worker) return;
      worker.addEventListener("statechange", function () {
        logUpdate("SW state:", worker.state);
        if (worker.state === "installed") {
          onUpdateReady(registration);
        }
      });
    }

    function watchRegistration(registration) {
      if (registration.waiting && navigator.serviceWorker.controller) {
        onUpdateReady(registration);
      }

      registration.addEventListener("updatefound", function () {
        logUpdate("updatefound");
        watchInstallingWorker(registration, registration.installing);
      });
    }

    navigator.serviceWorker.addEventListener("controllerchange", function () {
      if (!reloadPending) {
        reloadPending = true;
      }
      logUpdate("controllerchange — recargando");
      window.location.reload();
    });

    navigator.serviceWorker
      .register(swUrl, { scope: "/m/", updateViaCache: "none" })
      .then(function (registration) {
        logUpdate("Service worker registrado");
        watchRegistration(registration);
        if (registration.waiting && navigator.serviceWorker.controller) {
          onUpdateReady(registration);
        }
        registration.update().catch(function () {});
        setInterval(function () {
          registration.update().catch(function () {});
        }, UPDATE_INTERVAL_MS);
        document.addEventListener("visibilitychange", function () {
          if (!document.hidden) {
            registration.update().catch(function () {});
          }
        });
      })
      .catch(function (err) {
        console.warn("[Andes PWA Update] registro falló:", err);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    initPwaAutoUpdate();
    initMasMenu();
    initSearchDebounce();
    initPullToRefresh();
    initNavPlaceholders();
  });
})();
