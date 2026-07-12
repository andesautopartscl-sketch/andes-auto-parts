(function () {
  "use strict";

  var CFG = window.ANDES_VENTA_RAPIDA || {};

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }
  var STORAGE_KEY = "andes_mobile_venta_wizard";
  var root = document.getElementById("venta-rapida-root");
  if (!root) return;

  var puedeVender = root.getAttribute("data-puede-vender") === "1";
  var state = loadState();
  var currentStep = state.step || 1;
  var saving = false;

  var elClientesList = document.getElementById("vr-clientes-list");
  var elClienteQ = document.getElementById("vr-cliente-q");
  var elClienteSel = document.getElementById("vr-cliente-seleccionado");
  var elNext1 = document.getElementById("vr-next-1");
  var elCart = document.getElementById("vr-cart");
  var elCartEmpty = document.getElementById("vr-cart-empty");
  var elTotalParcial = document.getElementById("vr-total-parcial");
  var elToast = document.getElementById("vr-toast");
  var elModal = document.getElementById("vr-modal");

  function loadState() {
    try {
      var raw = sessionStorage.getItem(STORAGE_KEY);
      if (raw) return JSON.parse(raw);
    } catch (_e) {}
    return {
      step: 1,
      cliente: null,
      consumidor_final: false,
      items: [],
      metodo_pago: "efectivo",
      observacion: "",
    };
  }

  function saveState() {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  }

  function clearState() {
    state = {
      step: 1,
      cliente: null,
      consumidor_final: false,
      items: [],
      metodo_pago: "efectivo",
      observacion: "",
    };
    sessionStorage.removeItem(STORAGE_KEY);
  }

  function fmtMoney(n) {
    var v = Math.round(Number(n) || 0);
    return "$" + String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }

  function showToast(msg) {
    if (!elToast) return;
    elToast.textContent = msg;
    elToast.hidden = false;
    elToast.classList.add("m-toast--visible");
    clearTimeout(showToast._t);
    showToast._t = setTimeout(function () {
      elToast.classList.remove("m-toast--visible");
      elToast.hidden = true;
    }, 3200);
  }

  function vibrateOk() {
    if (navigator.vibrate) navigator.vibrate([80, 40, 80]);
  }

  function setStep(step) {
    currentStep = step;
    state.step = step;
    saveState();
    root.querySelectorAll("[data-step]").forEach(function (panel) {
      var ps = panel.getAttribute("data-step");
      panel.hidden = String(ps) !== String(step);
    });
    root.querySelectorAll("[data-step-indicator]").forEach(function (li) {
      var n = parseInt(li.getAttribute("data-step-indicator"), 10);
      li.classList.toggle("m-wizard__step--active", n === step);
      li.classList.toggle("m-wizard__step--done", n < step);
    });
    if (step === 3) updateResumen();
  }

  function updateClienteUI() {
    if (state.consumidor_final) {
      elClienteSel.textContent = "Cliente: Consumidor final (boleta)";
      elNext1.disabled = !puedeVender;
      return;
    }
    if (state.cliente) {
      elClienteSel.textContent =
        "Cliente: " + (state.cliente.nombre || "") + (state.cliente.rut ? " · " + state.cliente.rut : "");
      elNext1.disabled = !puedeVender;
      return;
    }
    elClienteSel.textContent = "Ningún cliente seleccionado";
    elNext1.disabled = true;
  }

  function calcTotals() {
    var subtotalBruto = 0;
    state.items.forEach(function (it) {
      subtotalBruto += (Number(it.precio) || 0) * (Number(it.cantidad) || 0);
    });
    var pct = state.cliente && state.cliente.margen_descuento_pct ? Number(state.cliente.margen_descuento_pct) : 0;
    if (state.cliente && state.cliente.cliente_mayorista && !pct) pct = 0;
    var descuento = pct ? Math.round(subtotalBruto * (pct / 100) * 100) / 100 : 0;
    var subtotal = Math.round((subtotalBruto - descuento) * 100) / 100;
    var iva = Math.round(subtotal * 0.19 * 100) / 100;
    var total = Math.round((subtotal + iva) * 100) / 100;
    return { subtotalBruto: subtotalBruto, subtotal: subtotal, iva: iva, total: total };
  }

  function renderCart() {
    if (!state.items.length) {
      elCart.innerHTML = "";
      elCartEmpty.hidden = false;
      elTotalParcial.textContent = "$0";
      document.getElementById("vr-next-2").disabled = true;
      return;
    }
    elCartEmpty.hidden = true;
    var totals = calcTotals();
    elTotalParcial.textContent = fmtMoney(totals.total);
    document.getElementById("vr-next-2").disabled = !puedeVender;

    elCart.innerHTML = state.items
      .map(function (it, idx) {
        var sub = (Number(it.precio) || 0) * (Number(it.cantidad) || 0);
        return (
          '<li class="m-cart__item" data-idx="' +
          idx +
          '">' +
          '<div class="m-cart__head">' +
          '<span class="m-cart__code">' +
          escapeHtml(it.codigo) +
          "</span>" +
          '<button type="button" class="m-cart__remove" data-remove="' +
          idx +
          '" aria-label="Quitar">&times;</button>' +
          "</div>" +
          '<p class="m-cart__desc">' +
          escapeHtml(it.descripcion || "") +
          "</p>" +
          '<div class="m-cart__row">' +
          '<div class="m-qty">' +
          '<button type="button" class="m-qty__btn" data-qty-d="' +
          idx +
          '">−</button>' +
          '<input type="number" class="m-qty__input" data-qty="' +
          idx +
          '" value="' +
          it.cantidad +
          '" min="1" inputmode="numeric">' +
          '<button type="button" class="m-qty__btn" data-qty-i="' +
          idx +
          '">+</button>' +
          "</div>" +
          '<span class="m-cart__unit">' +
          fmtMoney(it.precio) +
          " c/u</span>" +
          '<span class="m-cart__sub">' +
          fmtMoney(sub) +
          "</span>" +
          "</div></li>"
        );
      })
      .join("");

    elCart.querySelectorAll("[data-remove]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var i = parseInt(btn.getAttribute("data-remove"), 10);
        state.items.splice(i, 1);
        saveState();
        renderCart();
      });
    });
    elCart.querySelectorAll("[data-qty-d]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var i = parseInt(btn.getAttribute("data-qty-d"), 10);
        if (state.items[i].cantidad > 1) state.items[i].cantidad -= 1;
        saveState();
        renderCart();
      });
    });
    elCart.querySelectorAll("[data-qty-i]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var i = parseInt(btn.getAttribute("data-qty-i"), 10);
        state.items[i].cantidad += 1;
        saveState();
        renderCart();
      });
    });
    elCart.querySelectorAll("[data-qty]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var i = parseInt(inp.getAttribute("data-qty"), 10);
        var v = parseInt(inp.value, 10);
        state.items[i].cantidad = v > 0 ? v : 1;
        saveState();
        renderCart();
      });
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function addProducto(prod) {
    if (!prod || !prod.codigo) return;
    var found = state.items.find(function (it) {
      return it.codigo === prod.codigo;
    });
    if (found) {
      found.cantidad += 1;
    } else {
      state.items.push({
        codigo: prod.codigo,
        descripcion: prod.descripcion || "",
        cantidad: 1,
        precio: Number(prod.precio) || 0,
        marca: prod.marca || "",
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

  function searchClientes(q) {
    if (!q || q.length < 2) {
      elClientesList.innerHTML = "";
      return;
    }
    var url = (CFG.apiClientes || "") + "?q=" + encodeURIComponent(q);
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          elClientesList.innerHTML = '<p class="m-hint">Sin clientes.</p>';
          return;
        }
        elClientesList.innerHTML = items
          .map(function (c) {
            return (
              '<button type="button" class="m-card-select' +
              (state.cliente && state.cliente.id === c.id ? " m-card-select--active" : "") +
              '" data-cliente-id="' +
              c.id +
              '">' +
              '<span class="m-card-select__title">' +
              escapeHtml(c.nombre) +
              "</span>" +
              '<span class="m-card-select__meta">' +
              escapeHtml(c.rut || "Sin RUT") +
              "</span></button>"
            );
          })
          .join("");
        elClientesList.querySelectorAll("[data-cliente-id]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var id = parseInt(btn.getAttribute("data-cliente-id"), 10);
            var cli = items.find(function (x) {
              return x.id === id;
            });
            state.cliente = cli || null;
            state.consumidor_final = false;
            saveState();
            updateClienteUI();
            elClientesList.querySelectorAll(".m-card-select").forEach(function (b) {
              b.classList.toggle("m-card-select--active", b === btn);
            });
          });
        });
      });
  }

  function searchProductos(q) {
    var list = document.getElementById("vr-productos-list");
    if (!list) return;
    if (!q || q.length < 2) {
      list.innerHTML = "";
      return;
    }
    fetch("/m/api/buscar?q=" + encodeURIComponent(q), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          list.innerHTML = '<p class="m-hint">Sin productos.</p>';
          return;
        }
        list.innerHTML = items
          .map(function (p) {
            return (
              '<button type="button" class="m-card-select" data-add-codigo="' +
              escapeHtml(p.codigo) +
              '">' +
              '<span class="m-card-select__title">' +
              escapeHtml(p.codigo) +
              "</span>" +
              '<span class="m-card-select__meta">' +
              escapeHtml(p.descripcion || "") +
              " · " +
              escapeHtml(p.precio_fmt || "") +
              "</span></button>"
            );
          })
          .join("");
        list.querySelectorAll("[data-add-codigo]").forEach(function (btn) {
          btn.addEventListener("click", function () {
            var cod = btn.getAttribute("data-add-codigo");
            fetchProducto(cod).then(function (res) {
              if (res && res.success && res.producto) addProducto(res.producto);
              else showToast((res && res.message) || "Producto no encontrado");
            });
          });
        });
      });
  }

  function updateResumen() {
    var t = calcTotals();
    document.getElementById("vr-res-subtotal").textContent = fmtMoney(t.subtotal);
    document.getElementById("vr-res-iva").textContent = fmtMoney(t.iva);
    document.getElementById("vr-res-total").textContent = fmtMoney(t.total);
    document.getElementById("vr-confirmar").disabled = !puedeVender || saving;
  }

  function openModal(total) {
    document.getElementById("vr-modal-text").textContent =
      "¿Confirmar venta de " + fmtMoney(total) + "?";
    elModal.hidden = false;
  }

  function closeModal() {
    elModal.hidden = true;
  }

  function guardarVenta() {
    if (saving || !puedeVender) return;
    saving = true;
    var btn = document.getElementById("vr-confirmar");
    var spinner = document.getElementById("vr-spinner");
    btn.disabled = true;
    spinner.hidden = false;

    var payload = {
      consumidor_final: !!state.consumidor_final,
      cliente_id: state.cliente ? state.cliente.id : 0,
      items: state.items.map(function (it) {
        return { codigo: it.codigo, cantidad: it.cantidad, precio: it.precio };
      }),
      metodo_pago: document.getElementById("vr-metodo-pago").value,
      observacion: document.getElementById("vr-observacion").value.trim(),
    };

    fetch(CFG.apiGuardar || "/m/api/venta-rapida", {
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
        if (!res.ok || !res.body.success) {
          btn.disabled = false;
          showToast((res.body && res.body.message) || "Error al guardar");
          return;
        }
        vibrateOk();
        showToast("Venta guardada");
        var b = res.body;
        document.getElementById("vr-success-numero").textContent =
          (b.tipo || "Doc").toUpperCase() + " " + (b.numero || "");
        document.getElementById("vr-success-detalle").textContent =
          (b.cliente || "") + " · " + fmtMoney(b.total);
        document.getElementById("vr-ver-detalle").href =
          (CFG.urlDetalle || "/m/venta/") + (b.doc_id || "");
        clearState();
        setStep("ok");
      })
      .catch(function () {
        saving = false;
        spinner.hidden = true;
        btn.disabled = false;
        showToast("Error de conexión");
      });
  }

  document.getElementById("vr-consumidor-final").addEventListener("click", function () {
    state.consumidor_final = true;
    state.cliente = null;
    saveState();
    updateClienteUI();
    elClientesList.innerHTML = "";
    if (elClienteQ) elClienteQ.value = "";
  });

  if (elClienteQ) {
    var ctimer = null;
    elClienteQ.addEventListener("input", function () {
      clearTimeout(ctimer);
      ctimer = setTimeout(function () {
        searchClientes(elClienteQ.value.trim());
      }, 280);
    });
  }

  document.getElementById("vr-next-1").addEventListener("click", function () {
    setStep(2);
    renderCart();
  });
  document.getElementById("vr-back-2").addEventListener("click", function () {
    setStep(1);
  });
  document.getElementById("vr-next-2").addEventListener("click", function () {
    if (!state.items.length) return;
    setStep(3);
  });
  document.getElementById("vr-back-3").addEventListener("click", function () {
    setStep(2);
  });

  document.getElementById("vr-toggle-buscar").addEventListener("click", function () {
    var wrap = document.getElementById("vr-buscar-wrap");
    wrap.hidden = !wrap.hidden;
  });

  var ptimer = null;
  var elProdQ = document.getElementById("vr-producto-q");
  if (elProdQ) {
    elProdQ.addEventListener("input", function () {
      clearTimeout(ptimer);
      ptimer = setTimeout(function () {
        searchProductos(elProdQ.value.trim());
      }, 280);
    });
  }

  document.getElementById("vr-confirmar").addEventListener("click", function () {
    var t = calcTotals();
    openModal(t.total);
  });

  document.getElementById("vr-modal-ok").addEventListener("click", function () {
    closeModal();
    guardarVenta();
  });

  elModal.querySelectorAll("[data-vr-modal-close]").forEach(function (el) {
    el.addEventListener("click", closeModal);
  });

  function enrichCartPrecios() {
    var needs = state.items.filter(function (it) {
      return !it.precio || it.precio <= 0;
    });
    if (!needs.length) return Promise.resolve();
    return Promise.all(
      needs.map(function (it) {
        return fetchProducto(it.codigo).then(function (res) {
          if (res && res.success && res.producto) {
            it.precio = Number(res.producto.precio) || 0;
            it.descripcion = res.producto.descripcion || it.descripcion;
            it.marca = res.producto.marca || it.marca;
            it.bodega = res.producto.bodega || it.bodega;
          }
        });
      })
    ).then(function () {
      saveState();
      renderCart();
    });
  }

  var params = new URLSearchParams(window.location.search);
  if (params.get("step") === "2") currentStep = 2;

  updateClienteUI();
  enrichCartPrecios().then(function () {
    renderCart();
    setStep(currentStep === "ok" ? 1 : currentStep);
  });
})();
