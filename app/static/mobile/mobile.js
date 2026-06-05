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

  document.addEventListener("DOMContentLoaded", function () {
    initMasMenu();
    initSearchDebounce();
    initPullToRefresh();
    initNavPlaceholders();
  });
})();
