(function () {
  var el = document.getElementById("session-idle-config");
  if (!el) return;
  var cfg;
  try {
    cfg = JSON.parse(el.textContent || "{}");
  } catch (e) {
    return;
  }
  if (!cfg || !cfg.statusUrl) return;

  var warned = false;
  var pollMs = Math.max(15000, parseInt(cfg.pollMs, 10) || 30000);
  var warnSec = Math.max(10, parseInt(cfg.warningBeforeSec, 10) || 60);
  var banner = null;

  function hideBanner() {
    if (banner && banner.parentNode) banner.parentNode.removeChild(banner);
    banner = null;
  }

  function showBanner() {
    hideBanner();
    banner = document.createElement("div");
    banner.setAttribute("role", "status");
    banner.style.cssText =
      "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);max-width:min(92vw,480px);z-index:9990;padding:12px 16px;border-radius:10px;background:#fef3c7;border:1px solid #fcd34d;color:#78350f;font:13px/1.45 system-ui,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.12);";
    var text = document.createElement("span");
    text.innerHTML =
      "Tu sesión se cerrará en menos de un minuto por inactividad. <strong>Usá el sistema</strong> (click, guardar o navegar) para mantenerla abierta.";
    var btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Entendido";
    btn.style.cssText =
      "margin-left:10px;padding:4px 10px;border-radius:6px;border:1px solid #d97706;background:#fff;cursor:pointer;font-size:12px;color:#78350f;";
    btn.addEventListener("click", hideBanner);
    banner.appendChild(text);
    banner.appendChild(btn);
    document.body.appendChild(banner);
  }

  function tick() {
    fetch(cfg.statusUrl, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (r) {
        if (r.status === 401) {
          var q = "expirado=1";
          try {
            var loc = window.location.pathname + (window.location.search || "");
            if (loc && loc !== "/login") {
              q += "&next=" + encodeURIComponent(loc);
            }
          } catch (e) {}
          window.location.href = "/login?" + q;
          return null;
        }
        return r.json();
      })
      .then(function (data) {
        if (!data || data.enabled === false) return;
        if (data.superadmin) return;
        if (data.logged_in === false) return;
        var rem = data.remaining_sec;
        if (typeof rem !== "number") return;
        if (rem > warnSec + 20) warned = false;
        if (rem <= warnSec && !warned) {
          warned = true;
          showBanner();
        }
      })
      .catch(function () {});
  }

  setInterval(tick, pollMs);
  setTimeout(tick, 4000);
})();
