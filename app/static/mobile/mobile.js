(function () {
  "use strict";

  var masMenuApi = null;

  function initMasMenu() {
    var toggle = document.getElementById("mobile-mas-toggle");
    var menu = document.getElementById("mobile-mas-menu");
    if (!toggle || !menu) return;

    var masHistoryPushed = false;

    function isMenuOpen() {
      return menu.classList.contains("m-mas-menu--visible");
    }

    function openMenu() {
      menu.hidden = false;
      toggle.setAttribute("aria-expanded", "true");
      document.body.classList.add("m-mas-open");
      requestAnimationFrame(function () {
        menu.classList.add("m-mas-menu--visible");
      });
      if (!masHistoryPushed) {
        history.pushState({ andesMasMenu: true }, "");
        masHistoryPushed = true;
      }
    }

    function closeMenu(fromPopstate) {
      if (!isMenuOpen() && menu.hidden) return;
      menu.classList.remove("m-mas-menu--visible");
      toggle.setAttribute("aria-expanded", "false");
      document.body.classList.remove("m-mas-open");
      window.setTimeout(function () {
        if (!menu.classList.contains("m-mas-menu--visible")) {
          menu.hidden = true;
        }
      }, 280);
      if (!fromPopstate && masHistoryPushed) {
        masHistoryPushed = false;
        window.andesSkipNextBackGuardFlag = true;
        history.back();
      } else if (fromPopstate) {
        masHistoryPushed = false;
      }
    }

    masMenuApi = {
      isOpen: isMenuOpen,
      close: function (fromPopstate) {
        closeMenu(!!fromPopstate);
      },
    };

    toggle.addEventListener("click", function (e) {
      e.preventDefault();
      if (menu.hidden || !isMenuOpen()) openMenu();
      else closeMenu(false);
    });

    menu.querySelectorAll("[data-mas-close]").forEach(function (el) {
      el.addEventListener("click", function () {
        closeMenu(false);
      });
    });

    var syncBtn = document.getElementById("mas-sync-catalog");
    if (syncBtn) {
      syncBtn.addEventListener("click", function () {
        closeMenu();
        if (!window.AndesCatalogSync) {
          showMasToast("Catálogo offline no disponible");
          return;
        }
        syncBtn.disabled = true;
        AndesCatalogSync.force()
          .then(function (res) {
            var n = res && res.count ? res.count : 0;
            if (res && res.skipped) {
              showMasToast("Catálogo ya estaba actualizado", "success");
            } else {
              showMasToast("Catálogo sincronizado (" + n + " productos)", "success");
            }
          })
          .catch(function (err) {
            showMasToast(
              (err && err.message) || "No se pudo sincronizar el catálogo",
              "error"
            );
          })
          .finally(function () {
            syncBtn.disabled = false;
          });
      });
    }
  }

  function showMasToast(msg, kind) {
    var toast = document.getElementById("mobile-toast");
    if (!toast) return;
    toast.textContent = msg;
    toast.hidden = false;
    toast.classList.remove("m-toast--error", "m-toast--success");
    if (kind === "error") toast.classList.add("m-toast--error");
    if (kind === "success") toast.classList.add("m-toast--success");
    toast.classList.add("m-toast--visible");
    clearTimeout(showMasToast._t);
    showMasToast._t = setTimeout(function () {
      toast.classList.remove("m-toast--visible", "m-toast--error", "m-toast--success");
      toast.hidden = true;
    }, kind === "error" ? 4200 : 2800);
  }

  window.AndesMobileToast = showMasToast;

  function initVersionBadge() {
    var version = document.body.getAttribute("data-pwa-version") || "";
    var badge = document.getElementById("mobile-version-badge");
    if (badge && version) badge.textContent = version;
  }

  function initSearchDebounce() {
    var input = document.getElementById("mobile-search-input");
    var results = document.getElementById("mobile-search-results");
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

    function renderNoResults(q) {
      results.innerHTML =
        '<div class="m-empty-state">' +
        '<div class="m-empty-state__icon" aria-hidden="true">' +
        '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="28" cy="28" r="16"/><path d="m42 42 14 14"/></svg>' +
        "</div>" +
        '<p class="m-empty-state__title">Sin resultados</p>' +
        '<p class="m-empty-state__text">No encontramos productos para «' +
        escapeHtml(q) +
        "».</p></div>";
    }

    function renderItems(items) {
      if (!items.length) {
        renderNoResults(lastQ || input.value.trim());
        return;
      }
      var html = '<ul class="m-card-list">';
      items.forEach(function (r) {
        var href = "/m/producto/" + encodeURIComponent(r.codigo || "");
        var thumb = r.imagen
          ? '<img src="' + escapeHtml(r.imagen) + '" alt="" class="m-result-card__thumb" width="56" height="56" loading="lazy" decoding="async">'
          : '<span class="m-result-card__thumb m-result-card__thumb--empty" aria-hidden="true">📦</span>';
        var matchBadge = r.match_en
          ? '<span class="m-result-card__match">' + escapeHtml(r.match_en) + "</span>"
          : "";
        var meta = r.meta_linea
          ? '<span class="m-result-card__meta">' + escapeHtml(r.meta_linea) + "</span>"
          : "";
        html +=
          '<li><a href="' +
          href +
          '" class="m-result-card m-result-card--rich">' +
          '<div class="m-result-card__media">' +
          thumb +
          "</div>" +
          '<div class="m-result-card__body">' +
          '<div class="m-result-card__head">' +
          '<span class="m-result-card__code">' +
          escapeHtml(r.codigo || "") +
          "</span>" +
          matchBadge +
          "</div>" +
          '<span class="m-result-card__desc">' +
          escapeHtml(r.descripcion || "") +
          "</span>" +
          meta +
          '<div class="m-result-card__footer">' +
          '<span class="m-badge">Stock ' +
          escapeHtml(String(r.stock != null ? r.stock : 0)) +
          "</span>" +
          '<span class="m-result-card__price">' +
          escapeHtml(r.precio_fmt || "—") +
          "</span></div></div></a></li>";
      });
      html += "</ul>";
      results.innerHTML = html;
    }

    var EMPTY_HTML =
      '<div class="m-empty-state m-empty-state--hint">' +
      '<div class="m-empty-state__icon" aria-hidden="true">' +
      '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="28" cy="28" r="16"/><path d="m42 42 14 14"/></svg>' +
      "</div>" +
      '<p class="m-empty-state__title">Buscar productos</p>' +
      '<p class="m-empty-state__text">Escribe al menos 2 caracteres (código, OEM, descripción, medidas…)</p>' +
      "</div>";

    var SKELETON_HTML =
      '<div class="m-skeleton-list m-skeleton-list--search" aria-hidden="true">' +
      '<div class="m-skeleton m-skeleton--search-card"><div class="m-skeleton m-skeleton--thumb"></div><div class="m-skeleton m-skeleton--search-body"><div class="m-skeleton m-skeleton--line m-skeleton--line-short"></div><div class="m-skeleton m-skeleton--line"></div><div class="m-skeleton m-skeleton--line m-skeleton--line-meta"></div></div></div>'
        .repeat(4) +
      "</div>";

    function showSearchError(err) {
      var msg =
        (err && err.message) || "Error al buscar. Intenta de nuevo.";
      if (err && err.status === 401) {
        msg = "Sesión expirada. Vuelve a iniciar sesión.";
      }
      results.innerHTML =
        '<div class="m-empty-state m-empty-state--error">' +
        '<div class="m-empty-state__icon" aria-hidden="true">⚠️</div>' +
        '<p class="m-empty-state__title">No se pudo buscar</p>' +
        '<p class="m-empty-state__text">' +
        escapeHtml(msg) +
        "</p></div>";
      showMasToast(msg, "error");
    }

    function fetchSearchApi(url, q) {
      return fetch(url + "?q=" + encodeURIComponent(q), {
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          Accept: "application/json",
        },
        credentials: "same-origin",
        cache: "no-store",
      }).then(function (res) {
        var ct = (res.headers.get("content-type") || "").toLowerCase();
        if (!res.ok || ct.indexOf("application/json") === -1) {
          var err = new Error("Servidor respondió HTTP " + res.status);
          err.status = res.status;
          throw err;
        }
        return res.json();
      }).then(function (data) {
        if (!data || data.success === false) {
          throw new Error((data && data.message) || "Respuesta inválida del servidor");
        }
        return data.items || [];
      });
    }

    function showEmptyState() {
      results.innerHTML = EMPTY_HTML;
    }

    function setLoading(on) {
      if (on) {
        results.innerHTML = SKELETON_HTML;
      }
    }

    function fetchLocal(q) {
      if (!window.AndesOfflineDb) return Promise.resolve([]);
      return AndesOfflineDb.searchLocal(q, 50);
    }

    function normalizeLocalItem(row) {
      return {
        codigo: row.codigo,
        descripcion: row.descripcion,
        stock: row.stock,
        precio_fmt: row.precio_fmt || "—",
        imagen: row.imagen || "",
        meta_linea: row.meta_linea || "",
        match_en: row.match_en || "",
      };
    }

    function fetchResults(q) {
      if (q.length < 2) {
        showEmptyState();
        return;
      }
      setLoading(true);

      function renderLocalFallback(hintClass, hintText) {
        return fetchLocal(q).then(function (localItems) {
          if (input.value.trim() !== q) return;
          if (!localItems.length) return false;
          renderItems(localItems.map(normalizeLocalItem));
          if (hintText) {
            results.insertAdjacentHTML(
              "afterbegin",
              '<p class="m-hint ' + hintClass + '">' + escapeHtml(hintText) + "</p>"
            );
          }
          return true;
        });
      }

      if (navigator.onLine) {
        fetchSearchApi(api, q)
          .then(function (items) {
            if (input.value.trim() !== q) return;
            renderItems(items);
          })
          .catch(function (err) {
            return renderLocalFallback("m-hint--offline", "Sin servidor — resultados locales").then(
              function (handled) {
                if (input.value.trim() !== q) return;
                if (!handled) showSearchError(err);
              }
            );
          });
        return;
      }

      renderLocalFallback("m-hint--offline", "Sin conexión — catálogo local").then(function (handled) {
        if (input.value.trim() !== q) return;
        if (!handled) {
          results.innerHTML =
            '<div class="m-empty-state">' +
            '<div class="m-empty-state__icon" aria-hidden="true">📡</div>' +
            '<p class="m-empty-state__title">Sin conexión</p>' +
            '<p class="m-empty-state__text">No hay resultados locales. Conéctate para buscar en el servidor.</p></div>';
        }
      });
    }

    if (!input.value.trim()) {
      showEmptyState();
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
      if (!zone.classList.contains("m-pull-zone")) {
        zone.classList.add("m-pull-zone");
      }
      var startY = 0;
      var pulling = false;
      var indicator = document.createElement("div");
      indicator.className = "m-pull-indicator";
      indicator.setAttribute("aria-hidden", "true");
      indicator.innerHTML =
        '<span class="m-pull-indicator__icon">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14"/><path d="m19 12-7 7-7-7"/></svg>' +
        "</span>" +
        '<span class="m-pull-indicator__text">Suelta para actualizar</span>';
      zone.insertBefore(indicator, zone.firstChild);

      zone.addEventListener(
        "touchstart",
        function (e) {
          if (window.scrollY > 8) return;
          startY = e.touches[0].clientY;
          pulling = true;
          zone.classList.remove("m-pull-ready");
          zone.classList.remove("m-pull-active");
        },
        { passive: true }
      );

      zone.addEventListener(
        "touchmove",
        function (e) {
          if (!pulling) return;
          var dy = e.touches[0].clientY - startY;
          if (dy <= 0) {
            zone.classList.remove("m-pull-active");
            zone.classList.remove("m-pull-ready");
            return;
          }
          zone.classList.add("m-pull-active");
          var progress = Math.min(dy / 80, 1);
          indicator.style.setProperty("--pull-progress", String(progress));
          if (dy > 72) zone.classList.add("m-pull-ready");
          else zone.classList.remove("m-pull-ready");
        },
        { passive: true }
      );

      zone.addEventListener("touchend", function () {
        if (zone.classList.contains("m-pull-ready")) {
          zone.classList.add("m-pull-loading");
          zone.classList.remove("m-pull-ready");
          window.location.reload();
        }
        zone.classList.remove("m-pull-active");
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

  function initBackNavigation() {
    if (!window.history || !window.history.pushState) return;

    var backToastAt = 0;

    if (!history.state || !history.state.andesMobile) {
      history.replaceState({ andesMobile: true, screen: "app" }, "", location.href);
    }

    window.addEventListener("popstate", function () {
      if (masMenuApi && masMenuApi.isOpen()) {
        masMenuApi.close(true);
        history.pushState({ andesMobile: true, screen: "app" }, "", location.href);
        return;
      }

      if (window.andesSkipNextBackGuardFlag) {
        window.andesSkipNextBackGuardFlag = false;
        return;
      }

      history.pushState({ andesMobile: true, screen: "app" }, "", location.href);
      var now = Date.now();
      if (now - backToastAt > 2200) {
        backToastAt = now;
        showMasToast("Toca atrás de nuevo para cerrar la app");
      }
    });
  }

  function initPageTransitions() {
    var overlay = document.getElementById("mobile-page-transition");
    if (!overlay) return;

    function flashTransition() {
      overlay.hidden = false;
      overlay.classList.add("mobile-page-transition--visible");
      window.setTimeout(function () {
        overlay.classList.remove("mobile-page-transition--visible");
        overlay.hidden = true;
      }, 260);
    }

    document.addEventListener("click", function (e) {
      var link = e.target.closest("a[href]");
      if (!link || link.target === "_blank" || link.hasAttribute("download")) return;
      var href = link.getAttribute("href") || "";
      if (href.indexOf("/m/") !== 0 && href.indexOf("/m") !== 0) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      flashTransition();
    });
  }

  function initQueryToast() {
    try {
      var params = new URLSearchParams(window.location.search);
      var toast = params.get("toast");
      var map = {
        creado: "Cliente creado",
        actualizado: "Cliente actualizado",
        eliminado: "Cliente desactivado",
      };
      var path = window.location.pathname;
      if (path.indexOf("/proveedores") >= 0 || path.indexOf("/proveedor/") >= 0) {
        map = {
          creado: "Proveedor creado",
          actualizado: "Proveedor actualizado",
          eliminado: "Proveedor desactivado",
        };
      }
      if (toast && map[toast]) {
        showMasToast(map[toast]);
        params.delete("toast");
        var qs = params.toString();
        var next = window.location.pathname + (qs ? "?" + qs : "");
        history.replaceState(history.state, "", next);
      }
    } catch (_e) {}
  }

  function initMobileTheme() {
    try {
      var theme = localStorage.getItem("andes_mobile_theme");
      if (theme === "1") {
        document.body.classList.add("mobile-app--dark");
        document.documentElement.classList.add("mobile-theme-dark");
      } else if (theme === "0") {
        document.documentElement.classList.add("mobile-theme-light");
      }
    } catch (_e) {}
  }

  document.addEventListener("DOMContentLoaded", function () {
    initMobileTheme();
    initPwaAutoUpdate();
    initVersionBadge();
    initMasMenu();
    initBackNavigation();
    initPageTransitions();
    initQueryToast();
    initSearchDebounce();
    initPullToRefresh();
    initNavPlaceholders();
  });
})();
