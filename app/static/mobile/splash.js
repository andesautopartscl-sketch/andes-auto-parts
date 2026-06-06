(function () {
  "use strict";

  var SPLASH_MIN_MS = 1500;
  var SPLASH_MAX_MS = 8000;

  function isStandalone() {
    return (
      window.matchMedia("(display-mode: standalone)").matches ||
      window.navigator.standalone === true
    );
  }

  function hideSplash() {
    var splash = document.getElementById("mobile-splash");
    if (!splash) return;
    splash.classList.add("mobile-splash--hide");
    setTimeout(function () {
      splash.remove();
    }, 420);
  }

  function waitMinDelay(started) {
    var elapsed = Date.now() - started;
    var wait = Math.max(0, SPLASH_MIN_MS - elapsed);
    return new Promise(function (resolve) {
      setTimeout(resolve, wait);
    });
  }

  function waitForCatalog() {
    if (!window.AndesOfflineDb || !navigator.onLine) {
      return Promise.resolve();
    }
    var api = document.body && document.body.getAttribute("data-catalog-api");
    if (!api) return Promise.resolve();
    return AndesOfflineDb.syncCatalog(api).catch(function () {
      return null;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var splash = document.getElementById("mobile-splash");
    if (!splash) return;
    if (!isStandalone()) {
      splash.remove();
      return;
    }
    var started = Date.now();
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      Promise.all([waitMinDelay(started), waitForCatalog()]).then(hideSplash);
    }

    window.addEventListener("load", finish, { once: true });
    setTimeout(finish, SPLASH_MAX_MS);
  });
})();
