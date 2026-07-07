(function () {
  "use strict";

  var SPLASH_MIN_MS = 1200;
  var SPLASH_MAX_MS = 5000;

  function hideSplash() {
    var splash = document.getElementById("mobile-splash");
    if (!splash) return;
    splash.classList.add("mobile-splash--hide");
    setTimeout(function () {
      splash.remove();
    }, 480);
  }

  function waitMinDelay(started) {
    var elapsed = Date.now() - started;
    var wait = Math.max(0, SPLASH_MIN_MS - elapsed);
    return new Promise(function (resolve) {
      setTimeout(resolve, wait);
    });
  }

  function markSplashReady() {
    var splash = document.getElementById("mobile-splash");
    if (!splash) return;
    splash.classList.add("mobile-splash--ready");
    var status = splash.querySelector(".mobile-splash__status");
    if (status) status.textContent = "Listo";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var splash = document.getElementById("mobile-splash");
    if (!splash) return;

    var started = Date.now();
    var finished = false;

    function finish() {
      if (finished) return;
      finished = true;
      markSplashReady();
      waitMinDelay(started).then(hideSplash);
    }

    window.addEventListener("load", finish, { once: true });
    setTimeout(finish, SPLASH_MAX_MS);
  });
})();
