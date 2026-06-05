(function () {
  "use strict";

  var CFG = window.ANDES_AJUSTE_STOCK || {};

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }
  var form = document.getElementById("ajuste-stock-form");
  if (!form) return;

  var lineas = CFG.lineas || [];
  var elBodega = document.getElementById("as-bodega");
  var elMarca = document.getElementById("as-marca");
  var elStockActual = document.getElementById("as-stock-actual");
  var elTipoHint = document.getElementById("as-tipo-hint");
  var elCantidadLabel = document.getElementById("as-cantidad-label");
  var elToast = document.getElementById("as-toast");
  var saving = false;

  function showToast(msg) {
    if (!elToast) return;
    elToast.textContent = msg;
    elToast.hidden = false;
    elToast.classList.add("m-toast--visible");
    if (navigator.vibrate) navigator.vibrate(80);
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      elToast.classList.remove("m-toast--visible");
      elToast.hidden = true;
    }, 2800);
  }

  function marcasEnBodega(bodega) {
    var seen = {};
    var out = [];
    lineas.forEach(function (l) {
      if (l.bodega !== bodega) return;
      var m = l.marca || "";
      if (!seen[m]) {
        seen[m] = true;
        out.push({ marca: m, stock: l.stock });
      }
    });
    return out;
  }

  function stockSeleccionado() {
    var bodega = elBodega.value;
    var marca = elMarca ? elMarca.value : "";
    var row = lineas.find(function (l) {
      return l.bodega === bodega && (l.marca || "") === (marca || "");
    });
    if (row) return row.stock;
    var sum = 0;
    lineas.forEach(function (l) {
      if (l.bodega === bodega) sum += Number(l.stock) || 0;
    });
    return sum || null;
  }

  function refreshMarcas() {
    if (!elMarca || elMarca.tagName !== "SELECT") return;
    var bodega = elBodega.value;
    var opts = marcasEnBodega(bodega);
    elMarca.innerHTML =
      '<option value="">Selecciona marca</option>' +
      opts
        .map(function (o) {
          return (
            '<option value="' +
            (o.marca || "").replace(/"/g, "&quot;") +
            '">' +
            (o.marca || "(sin marca)") +
            " (" +
            o.stock +
            ")</option>"
          );
        })
        .join("");
    if (opts.length === 1) elMarca.value = opts[0].marca || "";
    updateStockHint();
  }

  function tipoSeleccionado() {
    var r = form.querySelector('input[name="tipo"]:checked');
    return r ? r.value : "ingreso";
  }

  function updateStockHint() {
    var st = stockSeleccionado();
    elStockActual.textContent =
      st !== null ? "Stock en bodega seleccionada: " + st : "Stock en bodega seleccionada: —";
    var tipo = tipoSeleccionado();
    if (tipo === "ingreso") {
      elTipoHint.textContent = "Ingreso: suma unidades al stock actual.";
      elCantidadLabel.textContent = "Cantidad a ingresar";
    } else if (tipo === "salida") {
      elTipoHint.textContent = "Salida: resta unidades del stock actual.";
      elCantidadLabel.textContent = "Cantidad a retirar";
    } else {
      elTipoHint.textContent = "Ajuste: define el stock final (cantidad objetivo).";
      elCantidadLabel.textContent = "Stock final deseado";
    }
  }

  elBodega.addEventListener("change", refreshMarcas);
  if (elMarca) elMarca.addEventListener("change", updateStockHint);
  form.querySelectorAll('input[name="tipo"]').forEach(function (r) {
    r.addEventListener("change", updateStockHint);
  });

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (saving) return;
    var motivo = document.getElementById("as-motivo").value.trim();
    if (!motivo) {
      showToast("Indica el motivo del movimiento");
      return;
    }
    saving = true;
    var btn = document.getElementById("as-submit");
    var spinner = document.getElementById("as-spinner");
    btn.disabled = true;
    spinner.hidden = false;

    var payload = {
      codigo: CFG.codigo,
      tipo: tipoSeleccionado(),
      bodega: elBodega.value,
      marca: elMarca ? elMarca.value : "",
      cantidad: document.getElementById("as-cantidad").value,
      motivo: motivo,
    };

    fetch(CFG.apiGuardar || "/m/api/ajustar-stock", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().then(function (body) {
          return { ok: r.ok, body: body };
        });
      })
      .then(function (res) {
        saving = false;
        spinner.hidden = true;
        btn.disabled = false;
        if (!res.ok || !res.body.success) {
          showToast((res.body && res.body.message) || "No se pudo guardar");
          return;
        }
        showToast(res.body.message || "Guardado");
        setTimeout(function () {
          window.location.href = CFG.urlStock || "/m/stock/" + encodeURIComponent(CFG.codigo);
        }, 900);
      })
      .catch(function () {
        saving = false;
        spinner.hidden = true;
        btn.disabled = false;
        showToast("Error de conexión");
      });
  });

  refreshMarcas();
})();
