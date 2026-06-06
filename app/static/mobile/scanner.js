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
  var activeMode = modo === "barcode" ? "barcode" : "qr";
  var apiTpl = root.getAttribute("data-api-producto") || "";
  var urlProductoTpl = root.getAttribute("data-url-producto") || "";
  var urlStockTpl = root.getAttribute("data-url-stock") || "";
  var urlVentaRapida = root.getAttribute("data-url-venta-rapida") || "/m/venta-rapida";
  var urlIngresoRapida = root.getAttribute("data-url-ingreso-rapida") || "/m/ingreso-rapido";
  var INGRESO_CART_KEY = "andes_mobile_ingreso_wizard";

  var hintEl = document.getElementById("scanner-hint");
  var detectingEl = document.getElementById("scanner-detecting");
  var successEl = document.getElementById("scanner-success");
  var toastEl = document.getElementById("mobile-toast");
  var deniedEl = document.getElementById("scanner-permission-denied");
  var deniedDetailEl = document.getElementById("scanner-error-detail");
  var retryBtn = document.getElementById("scanner-retry-btn");
  var torchBtn = document.getElementById("scanner-torch-btn");
  var switchBtn = document.getElementById("scanner-switch-btn");
  var tabButtons = root.querySelectorAll(".m-scanner-segmented__btn, .m-scanner-tab");
  var frameEl = root.querySelector(".m-scanner__viewfinder") || root.querySelector(".m-scanner__frame");

  var html5QrCode = null;
  var cameras = [];
  var cameraIndex = 0;
  var useFacingMode = true;
  var torchOn = false;
  var torchSupported = false;
  var paused = false;
  var lastScanAt = 0;
  var COOLDOWN_MS = 1800;

  function supportedFormatsEnum() {
    return window.Html5QrcodeSupportedFormats || null;
  }

  function allFormatsList() {
    var F = supportedFormatsEnum();
    if (!F) return null;
    return [
      F.QR_CODE,
      F.CODE_128,
      F.EAN_13,
      F.EAN_8,
      F.UPC_A,
      F.UPC_E,
      F.CODE_39,
      F.CODE_93,
      F.ITF,
    ];
  }

  function formatNames(formats) {
    if (!formats || !formats.length) return "(ninguno)";
    var F = supportedFormatsEnum();
    if (!F) return formats.join(",");
    return formats
      .map(function (fmt) {
        for (var key in F) {
          if (F[key] === fmt) return key;
        }
        return String(fmt);
      })
      .join(", ");
  }

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

  function setDetecting(on) {
    if (!detectingEl) return;
    detectingEl.hidden = !on;
    detectingEl.classList.toggle("m-scanner__status--active", !!on);
  }

  function showSuccessThen(fn) {
    if (!successEl) {
      fn();
      return;
    }
    successEl.hidden = false;
    successEl.classList.add("m-scanner__success--visible");
    window.setTimeout(function () {
      fn();
    }, 480);
  }

  function updateHint() {
    if (!hintEl) return;
    if (modo === "venta") {
      hintEl.textContent = "Escanea productos para la venta";
      return;
    }
    if (modo === "ingreso") {
      hintEl.textContent = "Escanea productos para el ingreso";
      return;
    }
    hintEl.textContent =
      activeMode === "qr"
        ? "Centra el QR de la etiqueta en el marco"
        : "Centra el código de barras en el marco";
  }

  function updateScannerUi() {
    updateHint();
    if (modo === "venta" || modo === "ingreso") {
      root.classList.remove("m-scanner--mode-qr", "m-scanner--mode-barcode");
      if (frameEl) {
        frameEl.classList.remove("m-scanner__frame--qr", "m-scanner__frame--barcode");
      }
      return;
    }
    root.classList.toggle("m-scanner--mode-qr", activeMode === "qr");
    root.classList.toggle("m-scanner--mode-barcode", activeMode === "barcode");
    if (frameEl) {
      frameEl.classList.toggle("m-scanner__frame--qr", activeMode === "qr");
      frameEl.classList.toggle("m-scanner__frame--barcode", activeMode === "barcode");
    }
  }

  function setActiveTab() {
    tabButtons.forEach(function (btn) {
      var isActive = btn.getAttribute("data-modo") === activeMode;
      btn.classList.toggle("m-scanner-tab--active", isActive);
      btn.classList.toggle("m-scanner-segmented__btn--active", isActive);
      btn.setAttribute("aria-selected", isActive ? "true" : "false");
    });
    updateScannerUi();
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
      qrbox: { width: 280, height: 180 },
      aspectRatio: 1.777778,
      disableFlip: false,
      videoConstraints: videoConstraints,
    };
  }

  function scanFormatIsQr(decodedResult) {
    var F = supportedFormatsEnum();
    if (!F || !decodedResult || !decodedResult.result) return null;
    if (decodedResult.result.format === undefined) return null;
    return decodedResult.result.format === F.QR_CODE;
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

  function stopScanner(destroy) {
    setDetecting(false);
    if (torchOn) {
      applyTorchState(false).catch(function () {});
      torchOn = false;
      updateTorchUi();
    }
    if (!html5QrCode) return Promise.resolve();
    var instance = html5QrCode;
    if (destroy) {
      html5QrCode = null;
      debugLog("Deteniendo y destruyendo instancia Html5Qrcode…");
    } else {
      debugLog("Deteniendo cámara (instancia conservada)…");
    }
    return instance
      .stop()
      .then(function () {
        if (destroy) return instance.clear();
      })
      .catch(function (err) {
        debugWarn("stop/clear:", err && err.message ? err.message : err);
      });
  }

  function ensureScannerInstance() {
    if (html5QrCode) return html5QrCode;
    var formats = allFormatsList();
    if (!formats || !formats.length) {
      throw new Error("Html5QrcodeSupportedFormats no disponible");
    }
    debugLog(
      "Creando Html5Qrcode unificado, formatsToSupport:",
      formatNames(formats)
    );
    html5QrCode = new Html5Qrcode(readerId, {
      formatsToSupport: formats,
      verbose: false,
    });
    return html5QrCode;
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

  function getVideoTrack() {
    var reader = document.getElementById(readerId);
    if (!reader) return null;
    var video = reader.querySelector("video");
    if (!video || !video.srcObject || !video.srcObject.getVideoTracks) return null;
    var tracks = video.srcObject.getVideoTracks();
    return tracks && tracks.length ? tracks[0] : null;
  }

  function updateTorchUi() {
    if (!torchBtn) return;
    torchBtn.classList.toggle("m-scanner-fab--active", torchOn);
    torchBtn.setAttribute("aria-pressed", torchOn ? "true" : "false");
    torchBtn.title = torchOn ? "Apagar linterna" : "Encender linterna";
  }

  function refreshTorchSupport() {
    if (!torchBtn) return;
    torchSupported = false;
    try {
      var track = getVideoTrack();
      if (track && typeof track.getCapabilities === "function") {
        var caps = track.getCapabilities();
        torchSupported = !!(caps && caps.torch);
        debugLog("Linterna soportada:", torchSupported, caps);
      }
    } catch (err) {
      debugWarn("getCapabilities torch:", err && err.message ? err.message : err);
      torchSupported = false;
    }
    if (!torchSupported) {
      torchBtn.hidden = true;
      torchBtn.disabled = true;
      torchOn = false;
      updateTorchUi();
      return;
    }
    torchBtn.hidden = false;
    torchBtn.disabled = false;
  }

  function applyTorchState(on) {
    var track = getVideoTrack();
    if (!track) return Promise.reject(new Error("Sin track de video"));
    var caps = track.getCapabilities ? track.getCapabilities() : {};
    if (!caps || !caps.torch) {
      return Promise.reject(new Error("Torch no soportado"));
    }
    var settings = track.getSettings ? track.getSettings() : {};
    var next = typeof on === "boolean" ? on : !settings.torch;
    debugLog("applyTorchState:", next);
    return track
      .applyConstraints({ advanced: [{ torch: next }] })
      .catch(function (err) {
        debugWarn("torch advanced falló, intentando constraint directo:", err);
        return track.applyConstraints({ torch: next });
      })
      .then(function () {
        torchOn = next;
        updateTorchUi();
      });
  }

  function toggleTorch() {
    if (!torchSupported) {
      showToast("Tu dispositivo no soporta linterna");
      return;
    }
    applyTorchState()
      .catch(function (err) {
        debugError("toggleTorch:", err);
        showToast("Linterna no disponible");
      });
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
            return stopScanner(false).then(next);
          }
          refreshTorchSupport();
          showDenied(false);
          setDetecting(true);
        })
        .catch(function (err) {
          var errMsg = (err && err.message) || String(err);
          debugError("start falló intento", attempt, ":", errMsg);
          return stopScanner(false).then(next);
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
    var isRestart = !!html5QrCode;
    debugLog("startScanner() modo=", modo, "activeMode=", activeMode, "restart=", isRestart);

    var startChain = (isRestart
      ? stopScanner(false)
      : Promise.resolve()
    ).then(function () {
      if (!isRestart) {
        var reader = document.getElementById(readerId);
        if (reader) {
          reader.innerHTML = "";
          reader.style.minHeight = "280px";
        }
        ensureScannerInstance();
        debugLog("Instancia Html5Qrcode lista, readerId=", readerId);
        return requestCameraAccess()
          .then(function () {
            return loadCamerasOptional();
          })
          .then(function () {
            return startWithFallbacks();
          });
      }
      return startWithFallbacks();
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

  function onScanSuccess(decodedText, decodedResult) {
    if (paused) return;
    var now = Date.now();
    if (now - lastScanAt < COOLDOWN_MS) return;

    var isQr = scanFormatIsQr(decodedResult);
    var codigo;
    if (modo === "venta" || modo === "ingreso") {
      codigo = parseQrPayload(decodedText) || normalizeBarcode(decodedText);
      if (isQr === null && codigo) {
        isQr = !!parseQrPayload(decodedText);
      }
    } else if (isQr === true) {
      codigo = parseQrPayload(decodedText);
    } else if (isQr === false) {
      codigo = normalizeBarcode(decodedText);
    } else {
      codigo = parseQrPayload(decodedText) || normalizeBarcode(decodedText);
      isQr = !!parseQrPayload(decodedText);
    }
    if (!codigo) return;

    debugLog("Código leído:", codigo, "formatoQR=", isQr);
    paused = true;
    lastScanAt = now;

    fetchProducto(codigo)
      .then(function (data) {
        if (!data || !data.exists) {
          paused = false;
          if (modo !== "venta" && modo !== "ingreso" && isQr) showToast("QR no reconocido");
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
        if (modo === "ingreso") {
          try {
            var ingRaw = sessionStorage.getItem(INGRESO_CART_KEY);
            var ing = ingRaw ? JSON.parse(ingRaw) : { items: [], step: 2 };
            if (!ing.items) ing.items = [];
            var foundIng = ing.items.find(function (it) {
              return it.codigo === resolved;
            });
            if (foundIng) {
              foundIng.cantidad = (Number(foundIng.cantidad) || 0) + 1;
            } else {
              ing.items.push({
                codigo: resolved,
                descripcion: "",
                cantidad: 1,
                valor_neto: 0,
                bodega: "Bodega 1",
              });
            }
            ing.step = 2;
            sessionStorage.setItem(INGRESO_CART_KEY, JSON.stringify(ing));
          } catch (_e) {}
          showToast(resolved + " agregado al ingreso");
          paused = false;
          return;
        }
        showSuccessThen(function () {
          if (isQr) {
            window.location.href = tplReplace(urlProductoTpl, resolved);
          } else {
            window.location.href = tplReplace(urlStockTpl, resolved);
          }
        });
      })
      .catch(function () {
        paused = false;
        showToast("Error al validar código");
      });
  }

  function switchMode(newModo) {
    var next = (newModo || "qr").toLowerCase();
    if (next === activeMode) return;
    debugLog("Cambio de tab escáner (solo UI):", activeMode, "→", next);
    activeMode = next;
    setActiveTab();
  }

  if (modo !== "venta" && modo !== "ingreso") {
    tabButtons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        switchMode(btn.getAttribute("data-modo") || "qr");
      });
    });
  }

  var listoBtn = document.getElementById("scanner-listo-btn");
  if (listoBtn && (modo === "venta" || modo === "ingreso")) {
    listoBtn.addEventListener("click", function (e) {
      e.preventDefault();
      var dest = modo === "ingreso" ? urlIngresoRapida : urlVentaRapida;
      window.location.href = dest + "?step=2";
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
      if (torchOn) {
        applyTorchState(false).catch(function () {});
      }
      torchOn = false;
      updateTorchUi();
      startScanner();
    });
  }

  if (torchBtn) {
    torchBtn.addEventListener("click", function () {
      toggleTorch();
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
    stopScanner(true);
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopScanner(true);
    }
  });

  debugLog("scanner.js cargado, modo=", modo, "activeMode=", activeMode, "readyState=", document.readyState);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", waitForLibraryAndBoot);
  } else {
    waitForLibraryAndBoot();
  }
})();
