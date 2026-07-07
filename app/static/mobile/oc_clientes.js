(function () {
  "use strict";

  var CFG = window.ANDES_OC_CLIENTES || {};
  var root = document.getElementById("m-oc-form-root");
  if (!root || root.getAttribute("data-puede-modificar") !== "1") return;

  var state = {
    cliente: null,
    items: [],
  };
  var saving = false;

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }

  function todayIso() {
    return new Date().toISOString().slice(0, 10);
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

  function calcTotals() {
    var neto = 0;
    state.items.forEach(function (it) {
      neto += Number(it.subtotal) || 0;
    });
    var iva = Math.round(neto * 0.19);
    var total = neto + iva;
    return { neto: neto, iva: iva, total: total };
  }

  function refreshTotals() {
    var t = calcTotals();
    var netoEl = document.getElementById("m-oc-neto");
    var ivaEl = document.getElementById("m-oc-iva");
    var totalEl = document.getElementById("m-oc-total");
    if (netoEl) netoEl.textContent = fmtMoney(t.neto);
    if (ivaEl) ivaEl.textContent = fmtMoney(t.iva);
    if (totalEl) totalEl.textContent = fmtMoney(t.total);
    var btn = document.getElementById("m-oc-guardar");
    if (btn) {
      btn.disabled = !state.cliente || !state.items.length || !(document.getElementById("m-oc-numero").value || "").trim();
    }
  }

  function renderItems() {
    var list = document.getElementById("m-oc-items");
    var empty = document.getElementById("m-oc-items-empty");
    if (!list) return;
    if (!state.items.length) {
      list.innerHTML = "";
      if (empty) empty.hidden = false;
      refreshTotals();
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = state.items
      .map(function (it, idx) {
        return (
          '<li class="m-cart__item" data-idx="' +
          idx +
          '"><div class="m-cart__head"><span class="m-cart__code">' +
          (it.codigo_producto || "—") +
          '</span><button type="button" class="m-cart__remove" data-rm aria-label="Quitar">×</button></div>' +
          '<p class="m-cart__desc">' +
          (it.descripcion || "") +
          '</p><div class="m-cart__row"><div class="m-qty"><button type="button" data-qty="-1">−</button>' +
          '<input type="number" class="m-qty__input" data-qty-input min="1" value="' +
          it.cantidad +
          '"><button type="button" data-qty="1">+</button></div>' +
          '<label class="m-oc-item-price"><span>P.unit</span><input type="number" data-precio min="0" step="1" value="' +
          (it.precio_unitario || 0) +
          '"></label>' +
          '<span class="m-cart__sub">' +
          fmtMoney(it.subtotal) +
          "</span></div></li>"
        );
      })
      .join("");

    list.querySelectorAll("[data-rm]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var idx = parseInt(btn.closest("[data-idx]").getAttribute("data-idx"), 10);
        state.items.splice(idx, 1);
        renderItems();
      });
    });
    list.querySelectorAll("[data-qty]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var li = btn.closest("[data-idx]");
        var idx = parseInt(li.getAttribute("data-idx"), 10);
        var delta = parseInt(btn.getAttribute("data-qty"), 10);
        var it = state.items[idx];
        it.cantidad = Math.max(1, (Number(it.cantidad) || 1) + delta);
        it.subtotal = Math.round(it.cantidad * (Number(it.precio_unitario) || 0));
        renderItems();
      });
    });
    list.querySelectorAll("[data-qty-input]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var li = inp.closest("[data-idx]");
        var idx = parseInt(li.getAttribute("data-idx"), 10);
        var it = state.items[idx];
        it.cantidad = Math.max(1, parseInt(inp.value, 10) || 1);
        it.subtotal = Math.round(it.cantidad * (Number(it.precio_unitario) || 0));
        renderItems();
      });
    });
    list.querySelectorAll("[data-precio]").forEach(function (inp) {
      inp.addEventListener("change", function () {
        var li = inp.closest("[data-idx]");
        var idx = parseInt(li.getAttribute("data-idx"), 10);
        var it = state.items[idx];
        it.precio_unitario = Math.max(0, parseFloat(inp.value) || 0);
        it.subtotal = Math.round(it.cantidad * it.precio_unitario);
        renderItems();
      });
    });
    refreshTotals();
  }

  function addItem(item) {
    state.items.push(item);
    renderItems();
  }

  function selectCliente(c) {
    state.cliente = c;
    document.getElementById("m-oc-cliente-id").value = c.id;
    document.getElementById("m-oc-cliente-seleccionado").textContent =
      (c.nombre || c.name || "Cliente") + (c.rut ? " · " + c.rut : "");
    var dir = document.getElementById("m-oc-direccion");
    if (dir && !dir.value && (c.direccion || c.address)) dir.value = c.direccion || c.address || "";
    document.getElementById("m-oc-clientes-list").innerHTML = "";
    refreshTotals();
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments;
      var self = this;
      clearTimeout(t);
      t = setTimeout(function () {
        fn.apply(self, args);
      }, ms);
    };
  }

  function searchClientes(q) {
    var list = document.getElementById("m-oc-clientes-list");
    if (!list || q.length < 2) {
      if (list) list.innerHTML = "";
      return;
    }
    list.innerHTML = '<div class="m-skeleton m-skeleton--row"></div>';
    fetch((CFG.apiClientes || "/m/api/clientes") + "?q=" + encodeURIComponent(q), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          list.innerHTML = '<p class="m-hint">Sin clientes.</p>';
          return;
        }
        list.innerHTML = items
          .map(function (c) {
            return (
              '<button type="button" class="m-card-select" data-cid="' +
              c.id +
              '"><span class="m-card-select__title">' +
              (c.nombre || "") +
              '</span><span class="m-card-select__meta">' +
              (c.rut || "") +
              "</span></button>"
            );
          })
          .join("");
        list.querySelectorAll(".m-card-select").forEach(function (btn, idx) {
          btn.addEventListener("click", function () {
            selectCliente(items[idx]);
          });
        });
      })
      .catch(function () {
        list.innerHTML = '<p class="m-hint">Error al buscar clientes.</p>';
      });
  }

  function searchProductos(q) {
    var list = document.getElementById("m-oc-productos-list");
    if (!list || q.length < 2) {
      if (list) list.innerHTML = "";
      return;
    }
    list.innerHTML = '<div class="m-skeleton m-skeleton--row"></div>';
    fetch((CFG.apiBuscar || "/m/api/buscar") + "?q=" + encodeURIComponent(q), {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (data) {
        var items = (data && data.items) || [];
        if (!items.length) {
          list.innerHTML =
            '<button type="button" class="m-card-select" data-libre="1"><span class="m-card-select__title">Usar "' +
            q +
            '" como código libre</span></button>';
        } else {
          list.innerHTML =
            items
              .map(function (p) {
                return (
                  '<button type="button" class="m-card-select" data-code="' +
                  (p.codigo || "") +
                  '"><span class="m-card-select__title">' +
                  (p.codigo || "") +
                  '</span><span class="m-card-select__meta">' +
                  (p.descripcion || "") +
                  " · " +
                  (p.precio_fmt || "—") +
                  "</span></button>"
                );
              })
              .join("") +
            '<button type="button" class="m-card-select" data-libre="1"><span class="m-card-select__title">Texto libre: ' +
            q +
            "</span></button>";
        }
        list.querySelectorAll(".m-card-select").forEach(function (btn) {
          btn.addEventListener("click", function () {
            if (btn.getAttribute("data-libre") === "1") {
              addItem({
                codigo_producto: q.toUpperCase(),
                descripcion: q,
                cantidad: 1,
                precio_unitario: 0,
                descuento_item: 0,
                subtotal: 0,
                marca: "",
                bodega: "Bodega 1",
              });
            } else {
              var code = btn.getAttribute("data-code");
              var meta = btn.querySelector(".m-card-select__meta");
              var desc = meta ? meta.textContent.split(" · ")[0] : "";
              addItem({
                codigo_producto: code,
                descripcion: desc,
                cantidad: 1,
                precio_unitario: 0,
                descuento_item: 0,
                subtotal: 0,
                marca: "",
                bodega: "Bodega 1",
              });
            }
            document.getElementById("m-oc-producto-q").value = "";
            list.innerHTML = "";
          });
        });
      });
  }

  function applyScanData(data) {
    if (!data) return;
    if (data.numero_oc) document.getElementById("m-oc-numero").value = data.numero_oc;
    if (data.fecha_oc) document.getElementById("m-oc-fecha").value = data.fecha_oc;
    if (data.fecha_entrega) document.getElementById("m-oc-fecha-entrega").value = data.fecha_entrega;
    if (data.forma_pago) document.getElementById("m-oc-forma-pago").value = data.forma_pago;
    if (data.vendedor) document.getElementById("m-oc-vendedor").value = data.vendedor;
    if (data.direccion_despacho) document.getElementById("m-oc-direccion").value = data.direccion_despacho;
    if (data.cliente_id) {
      selectCliente({
        id: data.cliente_id,
        nombre: data.cliente_nombre || data.cliente_razon_social || "",
        rut: data.cliente_rut || "",
      });
    }
    if (data.items && data.items.length) {
      state.items = data.items.map(function (it) {
        var cant = Number(it.cantidad) || 1;
        var precio = Number(it.precio_unitario) || 0;
        return {
          codigo_producto: (it.codigo_producto || "").toUpperCase(),
          descripcion: it.descripcion || "",
          marca: it.marca || "",
          bodega: "Bodega 1",
          cantidad: cant,
          precio_unitario: precio,
          descuento_item: 0,
          subtotal: Number(it.subtotal) || Math.round(cant * precio),
        };
      });
      renderItems();
    }
    refreshTotals();
    showToast("OC escaneada — revise los datos");
  }

  function scanFile(file) {
    var status = document.getElementById("m-oc-scan-status");
    var sk = document.getElementById("m-oc-scan-skeleton");
    if (status) {
      status.hidden = false;
      status.textContent = "Procesando imagen…";
    }
    if (sk) sk.hidden = false;
    var fd = new FormData();
    fd.append("archivo", file);
    fd.append("csrf_token", csrfToken());
    fetch(CFG.urlEscanear || "/oc-clientes/api/escanear", {
      method: "POST",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrfToken(),
      },
      body: fd,
    })
      .then(function (r) {
        return r.json();
      })
      .then(function (res) {
        if (sk) sk.hidden = true;
        if (!res || !res.ok) {
          if (status) status.textContent = (res && res.error) || "No se pudo escanear.";
          showToast((res && res.error) || "Error al escanear");
          return;
        }
        if (status) status.hidden = true;
        applyScanData(res.data);
      })
      .catch(function () {
        if (sk) sk.hidden = true;
        if (status) status.textContent = "Error de conexión al escanear.";
        showToast("Error al escanear");
      });
  }

  function guardar() {
    if (saving) return;
    var numero = (document.getElementById("m-oc-numero").value || "").trim();
    if (!state.cliente || !state.items.length || !numero) {
      showToast("Complete cliente, N° OC e ítems");
      return;
    }
    saving = true;
    var btn = document.getElementById("m-oc-guardar");
    if (btn) btn.disabled = true;
    var payload = {
      numero_oc: numero,
      cliente_id: state.cliente.id,
      fecha_oc: document.getElementById("m-oc-fecha").value || todayIso(),
      fecha_entrega_comprometida: document.getElementById("m-oc-fecha-entrega").value || "",
      forma_pago: document.getElementById("m-oc-forma-pago").value || "",
      vendedor: document.getElementById("m-oc-vendedor").value || "",
      direccion_despacho: document.getElementById("m-oc-direccion").value || "",
      observaciones: document.getElementById("m-oc-observaciones").value || "",
      items: state.items,
    };
    fetch(CFG.apiGuardar || "/m/api/oc-clientes", {
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
      .then(function (res) {
        saving = false;
        if (!res || !res.ok) {
          var err = (res && res.errors && res.errors.join(" ")) || (res && res.error) || "No se pudo guardar";
          showToast(err);
          if (btn) btn.disabled = false;
          return;
        }
        window.location.href = res.redirect || CFG.urlLista || "/m/oc-clientes";
      })
      .catch(function () {
        saving = false;
        showToast("Error al guardar");
        if (btn) btn.disabled = false;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var fecha = document.getElementById("m-oc-fecha");
    if (fecha && !fecha.value) fecha.value = todayIso();

    var clienteQ = document.getElementById("m-oc-cliente-q");
    if (clienteQ) clienteQ.addEventListener("input", debounce(function () { searchClientes(clienteQ.value.trim()); }, 300));

    var prodQ = document.getElementById("m-oc-producto-q");
    if (prodQ) prodQ.addEventListener("input", debounce(function () { searchProductos(prodQ.value.trim()); }, 300));

    var numero = document.getElementById("m-oc-numero");
    if (numero) numero.addEventListener("input", refreshTotals);

    document.getElementById("m-oc-add-libre").addEventListener("click", function () {
      addItem({
        codigo_producto: "",
        descripcion: "",
        cantidad: 1,
        precio_unitario: 0,
        descuento_item: 0,
        subtotal: 0,
        marca: "",
        bodega: "Bodega 1",
      });
    });

    var scanBtn = document.getElementById("m-oc-scan-btn");
    var scanInput = document.getElementById("m-oc-scan-input");
    if (scanBtn && scanInput) {
      scanBtn.addEventListener("click", function () {
        scanInput.click();
      });
      scanInput.addEventListener("change", function () {
        if (scanInput.files && scanInput.files[0]) scanFile(scanInput.files[0]);
        scanInput.value = "";
      });
    }

    document.getElementById("m-oc-guardar").addEventListener("click", guardar);
  });
})();
