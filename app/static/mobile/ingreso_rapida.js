(function () {
  "use strict";

  var CFG = window.ANDES_INGRESO_RAPIDO || {};
  var STORAGE_KEY = "andes_mobile_ingreso_wizard";
  var root = document.getElementById("ingreso-rapido-root");
  if (!root) return;

  var puedeIngreso = root.getAttribute("data-puede-ingreso") === "1";
  var state = loadState();
  var saving = false;

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }

  function loadState() {
    try {
      var raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) return JSON.parse(raw);
    } catch (_e) {}
    return { step: 1, proveedor: null, items: [], numero_documento: "", fecha: "", metodo_pago: "Efectivo", observacion: "", total_factura: "" };
  }

  function saveState() {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }

  function fmtMoney(n) {
    return "$" + String(Math.round(Number(n) || 0)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  function showToast(msg) {
    var el = document.getElementById("mobile-toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    el.classList.add("m-toast--visible");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      el.classList.remove("m-toast--visible");
      el.hidden = true;
    }, 3200);
  }

  function setStep(step) {
    state.step = step;
    saveState();
    root.querySelectorAll("[data-step]").forEach(function (p) {
      p.hidden = String(p.getAttribute("data-step")) !== String(step);
    });
    root.querySelectorAll("[data-step-indicator]").forEach(function (li) {
      var n = parseInt(li.getAttribute("data-step-indicator"), 10);
      li.classList.toggle("m-wizard__step--active", n === step);
      li.classList.toggle("m-wizard__step--done", n < step);
    });
    if (step === 2) renderCart();
  }

  function totalNeto() {
    return (state.items || []).reduce(function (s, it) {
      return s + (Number(it.valor_neto) || 0) * (Number(it.cantidad) || 0);
    }, 0);
  }

  function renderCart() {
    var cart = document.getElementById("ir-cart");
    var empty = document.getElementById("ir-cart-empty");
    var totalEl = document.getElementById("ir-total-parcial");
    var next2 = document.getElementById("ir-next-2");
    if (!cart) return;
    if (!state.items.length) {
      cart.innerHTML = "";
      if (empty) empty.hidden = false;
      if (next2) next2.disabled = true;
      if (totalEl) totalEl.textContent = "$0";
      return;
    }
    if (empty) empty.hidden = true;
    if (next2) next2.disabled = !puedeIngreso;
    if (totalEl) totalEl.textContent = fmtMoney(totalNeto());
    cart.innerHTML = state.items
      .map(function (it, idx) {
        return (
          '<li class="m-cart__item" data-idx="' +
          idx +
          '"><div class="m-cart__main"><strong>' +
          it.codigo +
          "</strong><span>" +
          (it.descripcion || "") +
          '</span></div><div class="m-cart__qty"><button type="button" data-qty-d="-1">−</button><span>' +
          it.cantidad +
          '</span><button type="button" data-qty-d="1">+</button></div><input type="number" class="m-cart__neto" data-neto value="' +
          (it.valor_neto || 0) +
          '" min="0" step="1"></li>'
        );
      })
      .join("");
    cart.querySelectorAll("[data-qty-d]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var li = btn.closest(".m-cart__item");
        var idx = parseInt(li.getAttribute("data-idx"), 10);
        var d = parseInt(btn.getAttribute("data-qty-d"), 10);
        state.items[idx].cantidad = Math.max(1, (state.items[idx].cantidad || 1) + d);
        saveState();
        renderCart();
      });
    });
    cart.querySelectorAll("[data-neto]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var li = inp.closest(".m-cart__item");
        var idx = parseInt(li.getAttribute("data-idx"), 10);
        state.items[idx].valor_neto = Number(inp.value) || 0;
        saveState();
        renderCart();
      });
    });
  }

  function addProducto(prod) {
    if (!prod || !prod.codigo) return;
    var found = state.items.find(function (it) {
      return it.codigo === prod.codigo;
    });
    if (found) {
      found.cantidad = (found.cantidad || 0) + 1;
    } else {
      state.items.push({
        codigo: prod.codigo,
        descripcion: prod.descripcion || "",
        marca: prod.marca || "",
        cantidad: 1,
        valor_neto: Number(prod.valor_neto) || 0,
        bodega: prod.bodega || "Bodega 1",
      });
    }
    saveState();
    renderCart();
    showToast(prod.codigo + " agregado");
  }

  function fetchProducto(codigo) {
    var url = (CFG.apiProducto || "").replace("__CODE__", encodeURIComponent(codigo));
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } }).then(function (r) {
      return r.json();
    });
  }

  function searchProveedores(q) {
    if (!CFG.apiProveedores || q.length < 2) return Promise.resolve([]);
    return fetch(CFG.apiProveedores + "?q=" + encodeURIComponent(q), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        return (d && d.items) || [];
      })
      .catch(function () {
        return [];
      });
  }

  function updateProveedorUI() {
    var sel = document.getElementById("ir-proveedor-sel");
    var next1 = document.getElementById("ir-next-1");
    if (state.proveedor) {
      sel.textContent = "Proveedor: " + (state.proveedor.display_name || state.proveedor.nombre || state.proveedor.rut);
      if (next1) next1.disabled = !puedeIngreso;
    } else {
      sel.textContent = "Ningún proveedor seleccionado";
      if (next1) next1.disabled = true;
    }
  }

  function mergeScannedFromSession() {
    try {
      var raw = sessionStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      var parsed = JSON.parse(raw);
      if (parsed && parsed.items && parsed.items.length && state.step >= 2) {
        state.items = parsed.items;
        if (parsed.proveedor) state.proveedor = parsed.proveedor;
      }
    } catch (_e) {}
  }

  document.getElementById("ir-proveedor-q").addEventListener("input", function () {
    var q = this.value.trim();
    var list = document.getElementById("ir-proveedores-list");
    if (q.length < 2) {
      list.innerHTML = "";
      return;
    }
    searchProveedores(q).then(function (items) {
      list.innerHTML = items
        .map(function (p) {
          return (
            '<button type="button" class="m-card-select" data-pid="' +
            p.id +
            '"><span class="m-card-select__title">' +
            (p.display_name || p.nombre) +
            "</span><span class="m-card-select__sub'>" +
            (p.rut || "") +
            "</span></button>"
          );
        })
        .join("");
      list.querySelectorAll(".m-card-select").forEach(function (btn) {
        btn.addEventListener("click", function () {
          var pid = parseInt(btn.getAttribute("data-pid"), 10);
          var p = items.find(function (x) {
            return x.id === pid;
          });
          state.proveedor = p || null;
          saveState();
          updateProveedorUI();
        });
      });
    });
  });

  document.getElementById("ir-next-1").addEventListener("click", function () {
    setStep(2);
  });
  document.getElementById("ir-back-2").addEventListener("click", function () {
    setStep(1);
  });
  document.getElementById("ir-next-2").addEventListener("click", function () {
    setStep(3);
    var fecha = document.getElementById("ir-fecha");
    if (fecha && !fecha.value) {
      fecha.value = new Date().toISOString().slice(0, 10);
      state.fecha = fecha.value;
    }
    document.getElementById("ir-confirmar").disabled = !puedeIngreso;
  });
  document.getElementById("ir-back-3").addEventListener("click", function () {
    setStep(2);
  });

  document.getElementById("ir-toggle-buscar").addEventListener("click", function () {
    var w = document.getElementById("ir-buscar-wrap");
    w.hidden = !w.hidden;
  });

  document.getElementById("ir-producto-q").addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;
    var codigo = this.value.trim();
    if (!codigo) return;
    fetchProducto(codigo).then(function (d) {
      if (d && d.success && d.producto) addProducto(d.producto);
      else showToast("Producto no encontrado");
    });
  });

  document.getElementById("ir-confirmar").addEventListener("click", function () {
    if (saving || !puedeIngreso) return;
    saving = true;
    var payload = {
      proveedor_id: state.proveedor ? state.proveedor.id : 0,
      proveedor_rut: state.proveedor ? state.proveedor.rut : "",
      numero_documento: document.getElementById("ir-numero-doc").value.trim(),
      fecha_documento: document.getElementById("ir-fecha").value,
      metodo_pago: document.getElementById("ir-metodo-pago").value,
      observacion: document.getElementById("ir-observacion").value.trim(),
      total_factura: document.getElementById("ir-total-factura").value.trim(),
      items: state.items,
    };
    fetch(CFG.apiGuardar, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        if (d && d.success && d.doc_id) {
          sessionStorage.removeItem(STORAGE_KEY);
          window.location.href = (CFG.urlDetalle || "/m/ingreso/") + d.doc_id;
        } else {
          showToast((d && d.message) || "Error al guardar");
        }
      })
      .catch(function () {
        showToast("Error de red");
      })
      .finally(function () {
        saving = false;
      });
  });

  try {
    var params = new URLSearchParams(window.location.search);
    if (params.get("step") === "2") {
      state.step = 2;
      saveState();
    }
  } catch (_e) {}

  mergeScannedFromSession();
  updateProveedorUI();
  setStep(state.step || 1);
  if ((state.step || 1) >= 2) renderCart();
})();
