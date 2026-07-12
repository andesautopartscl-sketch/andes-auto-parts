(function () {
  "use strict";

  var actions = document.getElementById("m-oc-actions");
  if (!actions) return;

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
  }

  function todayIso() {
    return new Date().toISOString().slice(0, 10);
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

  function openModal(id) {
    var m = document.getElementById(id);
    if (m) m.hidden = false;
  }

  function closeModals() {
    document.querySelectorAll(".m-modal").forEach(function (m) {
      m.hidden = true;
    });
  }

  function postJson(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(payload || {}),
    }).then(function (r) {
      return r.json();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-oc-close]").forEach(function (el) {
      el.addEventListener("click", closeModals);
    });

    var fechaEntrega = document.getElementById("m-oc-fecha-entrega");
    if (fechaEntrega && !fechaEntrega.value) fechaEntrega.value = todayIso();

    var fechaPago = document.getElementById("m-oc-fecha-pago");
    if (fechaPago && !fechaPago.value) fechaPago.value = todayIso();

    var btnEntregar = document.getElementById("m-oc-btn-entregar");
    if (btnEntregar) {
      btnEntregar.addEventListener("click", function () {
        openModal("m-oc-modal-entregar");
      });
    }

    var btnPago = document.getElementById("m-oc-btn-pago");
    if (btnPago) {
      btnPago.addEventListener("click", function () {
        openModal("m-oc-modal-pago");
      });
    }

    var btnAnular = document.getElementById("m-oc-btn-anular");
    if (btnAnular) {
      btnAnular.addEventListener("click", function () {
        openModal("m-oc-modal-anular");
      });
    }

    var confirmEntregar = document.getElementById("m-oc-confirm-entregar");
    if (confirmEntregar) {
      confirmEntregar.addEventListener("click", function () {
        confirmEntregar.disabled = true;
        postJson(actions.getAttribute("data-url-entregar"), {
          fecha_entrega_real: document.getElementById("m-oc-fecha-entrega").value,
          numero_guia_despacho: document.getElementById("m-oc-guia").value,
          descontar_stock: document.getElementById("m-oc-descontar-stock").checked,
        })
          .then(function (res) {
            if (res && res.ok && res.redirect) {
              window.location.href = res.redirect;
              return;
            }
            showToast((res && res.error) || "No se pudo registrar entrega");
            confirmEntregar.disabled = false;
          })
          .catch(function () {
            showToast("Error de conexión");
            confirmEntregar.disabled = false;
          });
      });
    }

    var confirmPago = document.getElementById("m-oc-confirm-pago");
    if (confirmPago) {
      confirmPago.addEventListener("click", function () {
        var factura = (document.getElementById("m-oc-numero-factura").value || "").trim();
        if (!factura) {
          showToast("El número de factura es obligatorio");
          return;
        }
        confirmPago.disabled = true;
        postJson(actions.getAttribute("data-url-pago"), {
          numero_factura: factura,
          fecha_pago: document.getElementById("m-oc-fecha-pago").value,
          metodo_pago: document.getElementById("m-oc-metodo-pago").value,
        })
          .then(function (res) {
            if (res && res.ok && res.redirect) {
              window.location.href = res.redirect;
              return;
            }
            showToast((res && res.error) || "No se pudo registrar pago");
            confirmPago.disabled = false;
          })
          .catch(function () {
            showToast("Error de conexión");
            confirmPago.disabled = false;
          });
      });
    }

    var confirmAnular = document.getElementById("m-oc-confirm-anular");
    if (confirmAnular) {
      confirmAnular.addEventListener("click", function () {
        confirmAnular.disabled = true;
        postJson(actions.getAttribute("data-url-anular"), {
          auth_user: document.getElementById("m-oc-auth-user").value,
          auth_password: document.getElementById("m-oc-auth-pass").value,
        })
          .then(function (res) {
            if (res && res.ok && res.redirect) {
              window.location.href = res.redirect;
              return;
            }
            showToast((res && res.error) || "No se pudo anular");
            confirmAnular.disabled = false;
          })
          .catch(function () {
            showToast("Error de conexión");
            confirmAnular.disabled = false;
          });
      });
    }
  });
})();
