(function () {
  "use strict";

  var LOG_PREFIX = "[Andes Scanner]";

  function debugLog() {
    var args = Array.prototype.slice.call(arguments);
    args.unshift(LOG_PREFIX);
    console.log.apply(console, args);
  }

  function debugWarn() {
    var args = Array.prototype.slice.call(arguments);
    args.unshift(LOG_PREFIX);
    console.warn.apply(console, args);
  }

  function debugError() {
    var args = Array.prototype.slice.call(arguments);
    args.unshift(LOG_PREFIX);
    console.error.apply(console, args);
  }

  var root = document.getElementById("mobile-scanner-root");
  if (!root) {
    debugWarn("mobile-scanner-root no encontrado");
    return;
  }

  var readerId = "qr-reader";
  var modo = (root.getAttribute("data-modo") || "qr").toLowerCase();
  var apiTpl = root.getAttribute("data-api-producto") || "";
  var urlProductoTpl = root.getAttribute("data-url-producto") || "";
  var urlStockTpl = root.getAttribute("data-url-stock") || "";
  var urlVentaRapida = root.getAttribute("data-url-venta-rapida") || "/m/venta-rapida";

  var hintEl = document.getElementById("scanner-hint");
  var toastEl = document.getElementById("mobile-toast");
  var deniedEl = document.getElementById("scanner-permission-denied");
  var deniedDetailEl = document.getElementById("scanner-error-detail");
  var retryBtn = document.getElementById("scanner-retry-btn");
  var torchBtn = document.getElementById("scanner-torch-btn");
  var switchBtn = document.getElementById("scanner-switch-btn");
  var tabButtons = root.querySelectorAll(".m-scanner-tab");

  var html5QrCode = null;
  var cameras = [];
  var cameraIndex = 0;
  var useFacingMode = true;
  var torchOn = false;
  var torchSupported = false;
  var paused = false;
  var lastScanAt = 0;
  var COOLDOWN_MS = 1800;

  var QR_FORMATS = window.Html5QrcodeSupportedFormats
    ? [Html5QrcodeSupportedFormats.QR_CODE]
    : undefined;

  var BARCODE_FORMATS = window.Html5QrcodeSupportedFormats
    ? [
        Html5QrcodeSupportedFormats.CODE_128,
        Html5QrcodeSupportedFormats.EAN_13,
        Html5QrcodeSupportedFormats.EAN_8,
        Html5QrcodeSupportedFormats.UPC_A,
        Html5QrcodeSupportedFormats.UPC_E,
      ]
    : undefined;

  function tplReplace(tpl, code) {
    return tpl.replace("__CODE__", encodeURIComponent(code));
  }

  function showToast(msg) {
    if (!toastEl) return;
    toastEl.textContent = msg;
    toastEl.hidden = false;
    toastEl.classList.add("m-toast--visible");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      toastEl.classList.remove("m-toast--visible");
      toastEl.hidden = true;
    }, 2800);
  }

  function feedbackOk() {
    if (navigator.vibrate) navigator.vibrate(80);
    try {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = 880;
      gain.gain.value = 0.08;
      osc.start();
      osc.stop(ctx.currentTime + 0.08);
    } catch (_e) {}
  }

  function parseQrPayload(text) {
    var t = (text || "").trim();
    if (!t) return null;
    var lower = t.toLowerCase();
    var markers = ["/m/producto/", "/producto/"];
    for (var i = 0; i < markers.length; i++) {
      var idx = lower.indexOf(markers[i]);
      if (idx >= 0) {
        var rest = t.substring(idx + markers[i].length);
        var codigo = rest.split("?")[0].split("#")[0].replace(/\/+$/, "");
        if (codigo) return codigo.toUpperCase();
      }
    }
    if (/^[A-Za-z0-9._\-/]+$/.test(t)) return t.toUpperCase();
    return t.toUpperCase();
  }

  function normalizeBarcode(text) {
    var t = (text || "").trim();
    return t ? t.toUpperCase() : null;
  }

  function updateHint() {
    if (!hintEl) return;
    if (modo === "venta") {
      hintEl.textContent = "Escanea productos para la venta";
      return;
    }
    hintEl.textContent =
      modo === "qr"
        ? "Apunta al QR de la etiqueta"
        : "Apunta al código de barras";
  }

  function setActiveTab() {
    tabButtons.forEach(function (btn) {
      var isActive = btn.getAttribute("data-modo") === modo;
      btn.classList.toggle("m-scanner-tab--active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    updateHint();
  }

  function scannerConfig(facingMode) {
    var videoConstraints = {
      width: { ideal: 1280 },
      height: { ideal: 720 },
    };
    if (facingMode) {
      videoConstraints.facingMode = facingMode;
    }
    return {
      fps: 10,
      qrbox: function (viewfinderWidth, viewfinderHeight) {
        var size = Math.floor(Math.min(viewfinderWidth, viewfinderHeight) * 0.72);
        return { width: size, height: Math.floor(size * 0.85) };
      },
      aspectRatio: 1.777778,
      disableFlip: false,
      videoConstraints: videoConstraints,
    };
  }

  function formatsForMode() {
    if (modo === "venta") {
      if (BARCODE_FORMATS && QR_FORMATS) {
        return BARCODE_FORMATS.concat(QR_FORMATS);
      }
      return BARCODE_FORMATS || QR_FORMATS;
    }
    return modo === "qr" ? QR_FORMATS : BARCODE_FORMATS;
  }

  function cameraConfigForAttempt(attempt) {
    if (attempt === 0) {
      debugLog("Intento cámara: facingMode environment");
      return { facingMode: "environment" };
    }
    if (attempt === 1) {
      debugLog("Intento cámara: facingMode user (frontal)");
      return { facingMode: "user" };
    }
    if (cameras.length) {
      var cam = cameras[cameraIndex % cameras.length];
      debugLog("Intento cámara por deviceId:", cam.id, cam.label || "(sin label)");
      return { deviceId: { exact: cam.id } };
    }
    debugLog("Intento cámara: facingMode environment (último recurso)");
    return { facingMode: "environment" };
  }

  function showDenied(show, message) {
    if (!deniedEl) return;
    deniedEl.hidden = !show;
    if (deniedDetailEl && message) {
      deniedDetailEl.textContent = message;
    }
  }

  function stopScanner() {
    if (!html5QrCode) return Promise.resolve();
    debugLog("Deteniendo escáner…");
    return html5QrCode
      .stop()
      .then(function () {
        return html5QrCode.clear();
      })
      .catch(function (err) {
        debugWarn("stop/clear:", err && err.message ? err.message : err);
      });
  }

  function verifyVideoStream() {
    var reader = document.getElementById(readerId);
    if (!reader) {
      debugWarn("Contenedor #qr-reader no encontrado tras start");
      return false;
    }
    var video = reader.querySelector("video");
    if (!video) {
      debugWarn("Elemento <video> no creado por html5-qrcode");
      return false;
    }
    debugLog("Video encontrado:", {
      readyState: video.readyState,
      videoWidth: video.videoWidth,
      videoHeight: video.videoHeight,
      paused: video.paused,
      srcObject: !!video.srcObject,
    });
    if (!video.srcObject && video.readyState < 2) {
      return false;
    }
    return true;
  }

  function refreshTorchSupport() {
    if (!html5QrCode || !torchBtn) return;
    torchSupported = false;
    try {
      var track = html5QrCode.getRunningTrack && html5QrCode.getRunningTrack();
      if (track && typeof track.getCapabilities === "function") {
        var caps = track.getCapabilities();
        torchSupported = !!(caps && caps.torch);
        debugLog("Linterna soportada:", torchSupported);
      }
    } catch (err) {
      debugWarn("getCapabilities torch:", err && err.message ? err.message : err);
      torchSupported = false;
    }
    torchBtn.disabled = !torchSupported;
  }

  function facingFromConfig(config) {
    if (config && config.facingMode) return config.facingMode;
    return null;
  }

  function tryStartWithConfig(config, attempt) {
    debugLog("Html5Qrcode.start() intento", attempt, "config:", JSON.stringify(config));
    return html5QrCode.start(
      config,
      scannerConfig(facingFromConfig(config)),
      onScanSuccess,
      function () {}
    );
  }

  function startWithFallbacks() {
    var maxAttempts = 4;
    var attempt = 0;

    function next() {
      if (attempt >= maxAttempts) {
        var msg =
          "No se pudo iniciar la cámara tras " +
          maxAttempts +
          " intentos. Revisa permisos de cámara en Ajustes → Apps → Chrome.";
        debugError(msg);
        showDenied(true, msg);
        showToast("Cámara no disponible");
        return Promise.reject(new Error(msg));
      }
      var config = cameraConfigForAttempt(attempt);
      attempt += 1;
      return tryStartWithConfig(config, attempt)
        .then(function () {
          debugLog("Html5Qrcode.start() OK en intento", attempt);
          if (!verifyVideoStream()) {
            debugWarn("Stream de video no verificado, reintentando…");
            return stopScanner().then(next);
          }
          refreshTorchSupport();
          showDenied(false);
        })
        .catch(function (err) {
          var errMsg = (err && err.message) || String(err);
          debugError("start falló intento", attempt, ":", errMsg);
          return stopScanner().then(next);
        });
    }

    return next();
  }

  function releaseMediaStream(stream) {
    if (!stream || !stream.getTracks) return;
    stream.getTracks().forEach(function (track) {
      try {
        track.stop();
      } catch (_e) {}
    });
  }

  function requestCameraAccess() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      debugWarn("navigator.mediaDevices.getUserMedia no disponible");
      return Promise.resolve();
    }
    debugLog("Solicitando permiso getUserMedia (environment)…");
    return navigator.mediaDevices
      .getUserMedia({ video: { facingMode: "environment" } })
      .then(function (stream) {
        debugLog("getUserMedia environment OK, tracks:", stream.getTracks().length);
        releaseMediaStream(stream);
      })
      .catch(function (err) {
        debugWarn(
          "getUserMedia environment falló:",
          err && err.name ? err.name : "Error",
          err && err.message ? err.message : err
        );
        debugLog("Reintentando getUserMedia (user)…");
        return navigator.mediaDevices
          .getUserMedia({ video: { facingMode: "user" } })
          .then(function (stream) {
            debugLog("getUserMedia user OK");
            releaseMediaStream(stream);
          })
          .catch(function (err2) {
            debugError(
              "getUserMedia denegado:",
              err2 && err2.name ? err2.name : "Error",
              err2 && err2.message ? err2.message : err2
            );
            throw err2;
          });
      });
  }

  function loadCamerasOptional() {
    debugLog("Enumerando cámaras (opcional)…");
    return Html5Qrcode.getCameras()
      .then(function (devices) {
        cameras = devices || [];
        debugLog("Cámaras detectadas:", cameras.length);
        cameras.forEach(function (cam, idx) {
          debugLog("  [" + idx + "]", cam.id, cam.label || "(sin label)");
        });
      })
      .catch(function (err) {
        cameras = [];
        debugWarn("getCameras() falló (se usará facingMode):", err && err.message ? err.message : err);
      });
  }

  function startScanner() {
    paused = false;
    showDenied(false);
    debugLog("startScanner() modo=", modo);

    var formats = formatsForMode();
    var startChain = stopScanner().then(function () {
      html5QrCode = null;
      var reader = document.getElementById(readerId);
      if (reader) {
        reader.innerHTML = "";
        reader.style.minHeight = "280px";
      }
      html5QrCode = formats
        ? new Html5Qrcode(readerId, { formatsToSupport: formats, verbose: true })
        : new Html5Qrcode(readerId, { verbose: true });
      debugLog("Instancia Html5Qrcode creada, readerId=", readerId);
      return requestCameraAccess()
        .then(function () {
          return loadCamerasOptional();
        })
        .then(function () {
          return startWithFallbacks();
        });
    });

    return startChain.catch(function (err) {
      var errMsg = (err && err.message) || "Error desconocido al abrir la cámara";
      debugError("startScanner error final:", errMsg);
      showDenied(true, errMsg);
    });
  }

  function fetchProducto(codigo) {
    return fetch(tplReplace(apiTpl, codigo), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    }).then(function (r) {
      return r.json();
    });
  }

  function onScanSuccess(decodedText) {
    if (paused) return;
    var now = Date.now();
    if (now - lastScanAt < COOLDOWN_MS) return;

    var codigo;
    if (modo === "venta") {
      codigo = parseQrPayload(decodedText) || normalizeBarcode(decodedText);
    } else {
      codigo = modo === "qr" ? parseQrPayload(decodedText) : normalizeBarcode(decodedText);
    }
    if (!codigo) return;

    debugLog("Código leído:", codigo);
    paused = true;
    lastScanAt = now;

    fetchProducto(codigo)
      .then(function (data) {
        if (!data || !data.exists) {
          paused = false;
          if (modo === "qr") showToast("QR no reconocido");
          else showToast("Código no encontrado");
          return;
        }
        feedbackOk();
        var resolved = data.codigo || codigo;
        if (modo === "venta") {
          try {
            var cartKey = "andes_mobile_venta_wizard";
            var cartRaw = sessionStorage.getItem(cartKey);
            var cart = cartRaw ? JSON.parse(cartRaw) : { items: [] };
            if (!cart.items) cart.items = [];
            var found = cart.items.find(function (it) {
              return it.codigo === resolved;
            });
            if (found) {
              found.cantidad = (Number(found.cantidad) || 0) + 1;
            } else {
              cart.items.push({
                codigo: resolved,
                descripcion: "",
                cantidad: 1,
                precio: 0,
              });
            }
            cart.step = 2;
            sessionStorage.setItem(cartKey, JSON.stringify(cart));
          } catch (_e) {}
          showToast(resolved + " agregado");
          paused = false;
          return;
        }
        if (modo === "qr") {
          window.location.href = tplReplace(urlProductoTpl, resolved);
        } else {
          window.location.href = tplReplace(urlStockTpl, resolved);
        }
      })
      .catch(function () {
        paused = false;
        showToast("Error al validar código");
      });
  }

  function switchMode(newModo) {
    if (newModo === modo) return;
    modo = newModo;
    setActiveTab();
    startScanner();
  }

  if (modo !== "venta") {
    tabButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchMode(btn.getAttribute("data-modo") || "qr");
      });
    });
  }

  var listoBtn = document.getElementById("scanner-listo-btn");
  if (listoBtn) {
    listoBtn.addEventListener("click", function () {
      window.location.href = urlVentaRapida + "?step=2";
    });
  }

  if (switchBtn) {
    switchBtn.addEventListener("click", function () {
      if (cameras.length >= 2) {
        cameraIndex = (cameraIndex + 1) % cameras.length;
        useFacingMode = false;
        debugLog("Cambiar cámara → índice", cameraIndex);
      } else {
        useFacingMode = !useFacingMode;
        debugLog("Alternar facingMode, useFacingMode=", useFacingMode);
      }
      torchOn = false;
      startScanner();
    });
  }

  if (torchBtn) {
    torchBtn.addEventListener("click", function () {
      if (!html5QrCode || !torchSupported) return;
      torchOn = !torchOn;
      html5QrCode
        .applyVideoConstraints({ advanced: [{ torch: torchOn }] })
        .catch(function () {
          showToast("Linterna no disponible");
        });
      torchBtn.classList.toggle("m-scanner-action--active", torchOn);
    });
  }

  if (retryBtn) {
    retryBtn.addEventListener("click", function () {
      debugLog("Reintentar solicitado por usuario");
      useFacingMode = true;
      cameraIndex = 0;
      startScanner();
    });
  }

  function bootWhenLibraryReady() {
    if (typeof Html5Qrcode === "undefined") {
      debugWarn("Html5Qrcode aún no cargado, esperando…");
      return false;
    }
    debugLog("Html5Qrcode disponible, versión lib OK");
    setActiveTab();
    startScanner();
    return true;
  }

  function waitForLibraryAndBoot() {
    if (bootWhenLibraryReady()) return;
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (bootWhenLibraryReady()) {
        clearInterval(timer);
      } else if (tries >= 60) {
        clearInterval(timer);
        var msg = "No se cargó la librería html5-qrcode. Revisa tu conexión y recarga.";
        debugError(msg);
        showDenied(true, msg);
      }
    }, 100);
  }

  window.addEventListener("pagehide", function () {
    stopScanner();
  });

  debugLog("scanner.js cargado, modo=", modo, "readyState=", document.readyState);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", waitForLibraryAndBoot);
  } else {
    waitForLibraryAndBoot();
  }
})();
