(function () {
  "use strict";

  var CFG = window.ANDES_IMPORT_IMG || {};
  var root = document.getElementById("import-img-root");
  if (!root) return;

  var enabled = root.getAttribute("data-enabled") === "1";
  var items = [];
  var nextId = 1;
  var uploading = false;

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute("content") || "" : "";
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

  function codigoFromFilename(name) {
    var base = (name || "").replace(/\\/g, "/").split("/").pop() || "";
    var dot = base.lastIndexOf(".");
    var stem = (dot > 0 ? base.slice(0, dot) : base).trim();
    if (stem.toLowerCase().endsWith("_despiece")) stem = stem.slice(0, -9);
    return stem.toUpperCase();
  }

  function normalizeTipo(raw) {
    var t = String(raw || "producto").toLowerCase();
    if (t === "360" || t === "productos360") return "360";
    if (t === "despiece" || t === "epc" || t === "oem") return t === "oem" ? "oem" : "despiece";
    return "producto";
  }

  function addFiles(fileList) {
    if (!enabled || !fileList || !fileList.length) return;
    Array.prototype.forEach.call(fileList, function (file) {
      if (!file || !file.type || file.type.indexOf("image/") !== 0) return;
      var id = nextId++;
      var preview = URL.createObjectURL(file);
      items.push({
        id: id,
        file: file,
        nombre: file.name || "imagen.jpg",
        codigo: codigoFromFilename(file.name),
        tipo: "producto",
        preview: preview,
        status: "pending",
        progress: 0,
        message: "",
      });
    });
    render();
  }

  function removeItem(id) {
    var item = items.find(function (x) {
      return x.id === id;
    });
    if (item && item.preview) URL.revokeObjectURL(item.preview);
    items = items.filter(function (x) {
      return x.id !== id;
    });
    render();
  }

  function searchProductos(q) {
    if (!CFG.apiBuscar || q.length < 1) return Promise.resolve([]);
    return fetch(CFG.apiBuscar + "?q=" + encodeURIComponent(q), {
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

  function render() {
    var list = document.getElementById("ii-list");
    var empty = document.getElementById("ii-empty");
    var sticky = document.getElementById("ii-sticky");
    var uploadAll = document.getElementById("ii-upload-all");
    if (!list) return;

    if (!items.length) {
      list.innerHTML = "";
      if (empty) empty.hidden = false;
      if (sticky) sticky.hidden = true;
      if (uploadAll) uploadAll.disabled = true;
      return;
    }
    if (empty) empty.hidden = true;
    if (sticky) sticky.hidden = false;

    var pending = items.filter(function (it) {
      return it.status === "pending" || it.status === "error";
    });
    if (uploadAll) uploadAll.disabled = !enabled || uploading || !pending.length;

    list.innerHTML = items
      .map(function (it) {
        var tipoOpts = [
          { v: "producto", l: "Producto" },
          { v: "360", l: "360°" },
          { v: "despiece", l: "Despiece" },
          { v: "oem", l: "OEM" },
        ]
          .map(function (o) {
            return (
              '<option value="' +
              o.v +
              '"' +
              (normalizeTipo(it.tipo) === o.v ? " selected" : "") +
              ">" +
              o.l +
              "</option>"
            );
          })
          .join("");
        var statusLabel =
          it.status === "done"
            ? "✓ Subida"
            : it.status === "uploading"
              ? "Subiendo…"
              : it.status === "error"
                ? "Error"
                : "Pendiente";
        return (
          '<li class="m-import-img__card" data-id="' +
          it.id +
          '"><div class="m-import-img__card-head">' +
          '<img src="' +
          it.preview +
          '" alt="" class="m-import-img__thumb" width="72" height="72">' +
          '<div class="m-import-img__meta"><span class="m-import-img__name">' +
          (it.nombre || "") +
          '</span><span class="m-import-img__status m-import-img__status--' +
          it.status +
          '">' +
          statusLabel +
          "</span></div>" +
          '<button type="button" class="m-import-img__remove" data-remove="' +
          it.id +
          '" aria-label="Eliminar">×</button></div>' +
          '<label class="m-field"><span class="m-field__label">Código producto</span>' +
          '<input type="search" class="m-field__input ii-codigo" data-id="' +
          it.id +
          '" value="' +
          (it.codigo || "") +
          '" autocomplete="off" enterkeyhint="search"></label>' +
          '<div class="ii-suggest" data-suggest="' +
          it.id +
          '"></div>' +
          '<label class="m-field"><span class="m-field__label">Tipo</span>' +
          '<select class="m-field__input ii-tipo" data-id="' +
          it.id +
          '">' +
          tipoOpts +
          "</select></label>" +
          '<div class="m-import-img__progress"><div class="m-import-img__progress-bar" style="width:' +
          (it.progress || 0) +
          '%"></div></div>' +
          (it.message ? '<p class="m-import-img__msg">' + it.message + "</p>" : "") +
          "</li>"
        );
      })
      .join("");

    list.querySelectorAll("[data-remove]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        removeItem(parseInt(btn.getAttribute("data-remove"), 10));
      });
    });
    list.querySelectorAll(".ii-codigo").forEach(function (inp) {
      inp.addEventListener("input", function () {
        var id = parseInt(inp.getAttribute("data-id"), 10);
        var item = items.find(function (x) {
          return x.id === id;
        });
        if (item) item.codigo = inp.value.trim().toUpperCase();
        var q = inp.value.trim();
        var box = list.querySelector('[data-suggest="' + id + '"]');
        if (!box || q.length < 1) {
          if (box) box.innerHTML = "";
          return;
        }
        searchProductos(q).then(function (hits) {
          box.innerHTML = hits
            .slice(0, 6)
            .map(function (h) {
              return (
                '<button type="button" class="m-card-select ii-hit" data-id="' +
                id +
                '" data-code="' +
                (h.codigo || h.display_codigo || "") +
                '"><span class="m-card-select__title">' +
                (h.display_codigo || h.codigo || "") +
                '</span><span class="m-card-select__sub">' +
                (h.descripcion || "").slice(0, 60) +
                "</span></button>"
              );
            })
            .join("");
          box.querySelectorAll(".ii-hit").forEach(function (hit) {
            hit.addEventListener("click", function () {
              var code = hit.getAttribute("data-code") || "";
              inp.value = code;
              if (item) item.codigo = code.toUpperCase();
              box.innerHTML = "";
            });
          });
        });
      });
    });
    list.querySelectorAll(".ii-tipo").forEach(function (sel) {
      sel.addEventListener("change", function () {
        var id = parseInt(sel.getAttribute("data-id"), 10);
        var item = items.find(function (x) {
          return x.id === id;
        });
        if (item) item.tipo = normalizeTipo(sel.value);
      });
    });
  }

  function uploadOne(item) {
    var fd = new FormData();
    fd.append("imagen", item.file, item.nombre || item.file.name);
    fd.append("codigo", item.codigo || "");
    fd.append("archivo_nombre", item.nombre || item.file.name || "imagen.jpg");
    fd.append("tipo_imagen", normalizeTipo(item.tipo));
    item.status = "uploading";
    item.progress = 12;
    render();
    return fetch(CFG.apiSubir, {
      method: "POST",
      headers: {
        "X-Requested-With": "XMLHttpRequest",
        "X-CSRF-Token": csrfToken(),
      },
      body: fd,
    })
      .then(function (r) {
        item.progress = 70;
        render();
        return r.json();
      })
      .then(function (d) {
        if (d && (d.ok || d.success)) {
          item.status = "done";
          item.progress = 100;
          item.message = d.mensaje || "Vinculado";
        } else {
          item.status = "error";
          item.progress = 0;
          item.message = (d && (d.error || d.mensaje)) || "Error al subir";
        }
        render();
        return d;
      })
      .catch(function () {
        item.status = "error";
        item.progress = 0;
        item.message = "Error de red";
        render();
      });
  }

  function uploadAll() {
    if (uploading || !enabled) return;
    var queue = items.filter(function (it) {
      return it.status === "pending" || it.status === "error";
    });
    if (!queue.length) return;
    uploading = true;
    render();
    var chain = Promise.resolve();
    var ok = 0;
    queue.forEach(function (item) {
      if (!item.codigo) {
        item.status = "error";
        item.message = "Falta código";
        return;
      }
      chain = chain.then(function () {
        return uploadOne(item).then(function (d) {
          if (d && (d.ok || d.success)) ok += 1;
        });
      });
    });
    chain.finally(function () {
      uploading = false;
      render();
      showToast(ok + " de " + queue.length + " imágenes subidas");
    });
  }

  document.getElementById("ii-camera").addEventListener("change", function () {
    addFiles(this.files);
    this.value = "";
  });
  document.getElementById("ii-gallery").addEventListener("change", function () {
    addFiles(this.files);
    this.value = "";
  });
  document.getElementById("ii-upload-all").addEventListener("click", uploadAll);

  render();
})();
