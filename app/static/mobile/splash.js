(function () {
  "use strict";

  var SPLASH_MIN_MS = 680;
  var SPLASH_MAX_READY_MS = 1500;
  var SPLASH_HARD_MAX_MS = 4200;

  function setProgress(splash, value) {
    var bar = document.getElementById("mobile-splash-progress");
    var progress = splash && splash.querySelector(".mobile-splash__progress");
    var pct = Math.max(0, Math.min(100, Math.round(value)));
    if (bar) bar.style.width = pct + "%";
    if (progress) progress.setAttribute("aria-valuenow", String(pct));
  }

  function hideSplash(splash) {
    if (!splash || splash.classList.contains("mobile-splash--hide")) return;
    setProgress(splash, 100);
    splash.classList.add("mobile-splash--ready");
    window.setTimeout(function () {
      splash.classList.add("mobile-splash--hide");
      document.body.classList.remove("mobile-app--splash");
      window.setTimeout(function () {
        splash.remove();
      }, 380);
    }, 120);
  }

  document.addEventListener("DOMContentLoaded", function () {
    var splash = document.getElementById("mobile-splash");
    if (!splash) return;

    document.body.classList.add("mobile-app--splash");
    var started = Date.now();
    var finished = false;
    var current = 8;
    setProgress(splash, current);

    function bumpProgress(target) {
      current = Math.max(current, target);
      setProgress(splash, current);
    }

    bumpProgress(18);

    document.addEventListener(
      "andes:catalog-sync-progress",
      function (ev) {
        var detail = (ev && ev.detail) || {};
        var total = Number(detail.total || 0);
        var done = Number(detail.done || 0);
        if (total > 0) {
          bumpProgress(28 + Math.round((done / total) * 52));
        } else {
          bumpProgress(42);
        }
      },
      { passive: true }
    );

    document.addEventListener(
      "andes:catalog-sync-done",
      function () {
        bumpProgress(88);
      },
      { once: true, passive: true }
    );

    function finish() {
      if (finished) return;
      finished = true;
      bumpProgress(100);
      var elapsed = Date.now() - started;
      var wait = Math.max(0, SPLASH_MIN_MS - elapsed);
      if (elapsed < SPLASH_MAX_READY_MS) {
        wait = Math.min(wait, SPLASH_MAX_READY_MS - elapsed);
      }
      window.setTimeout(function () {
        hideSplash(splash);
      }, wait);
    }

    if (document.readyState === "complete") {
      bumpProgress(72);
      finish();
    } else {
      window.addEventListener(
        "load",
        function () {
          bumpProgress(72);
          finish();
        },
        { once: true }
      );
    }

    window.setTimeout(function () {
      if (!finished) finish();
    }, SPLASH_HARD_MAX_MS);
  });
})();
