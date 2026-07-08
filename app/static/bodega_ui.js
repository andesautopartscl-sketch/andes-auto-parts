(function () {
    "use strict";

    window.toggleMenu = function toggleMenu(id) {
        var el = document.getElementById(id);
        var hdr = document.getElementById("hdr-" + id);
        if (!el || !hdr) return;
        var isOpen = el.classList.contains("open");
        document.querySelectorAll(".submenu").forEach(function (menu) { menu.classList.remove("open"); });
        document.querySelectorAll(".module-header").forEach(function (header) { header.classList.remove("active"); });
        if (!isOpen) {
            el.classList.add("open");
            hdr.classList.add("active");
        }
    };

    function initDateInputs(root) {
        root.querySelectorAll("input[type='date']").forEach(function (el) {
            el.classList.add("active");
        });
    }

    function initRutBindings(root) {
        if (window.RutUtils && typeof window.RutUtils.autoBindRutInputs === "function") {
            window.RutUtils.autoBindRutInputs(root);
        }
    }

    /** Consulta en vivo (ajuste/salida/recepción): SPA para no recargar ni colapsar el acordeón del sidebar. */
    function navigateBodegaConsult(url) {
        try {
            var cur = new URL(window.location.href);
            var next = new URL(url, window.location.origin);
            if (cur.pathname === next.pathname && cur.search === next.search) {
                return;
            }
        } catch (e) {}
        if (typeof window._loadModule === "function") {
            window._loadModule(url, { softNav: true });
        } else {
            window.location.assign(url);
        }
    }

    /** POST de formularios Bodega vía SPA (ajuste, ingreso, salida, recepción). */
    function submitBodegaFormPost(form, submitter, onDone) {
        if (typeof window._submitModuleForm === "function") {
            window._submitModuleForm(form, {
                submitter: submitter || null,
                softNav: true,
                onDone: onDone
            });
            return;
        }
        if (submitter && typeof form.requestSubmit === "function") {
            form.requestSubmit(submitter);
        } else {
            form.submit();
        }
    }

    function bindBodegaFormSpaSubmit(form) {
        if (!form || form.dataset.bodegaSpaSubmitBound === "1") {
            return;
        }
        form.dataset.bodegaSpaSubmitBound = "1";
        form.addEventListener("submit", function (ev) {
            var sub = ev.submitter;
            var method = ((sub && sub.getAttribute("formmethod")) || form.getAttribute("method") || "get").toLowerCase();
            if (method === "get") {
                ev.preventDefault();
                var action = (sub && sub.getAttribute("formaction")) || form.getAttribute("action") || window.location.pathname;
                try {
                    var base = new URL(action, window.location.origin);
                    var fd = new FormData(form);
                    fd.forEach(function (val, key) {
                        if (key !== "csrf_token") {
                            base.searchParams.set(key, val);
                        }
                    });
                    navigateBodegaConsult(base.pathname + "?" + base.searchParams.toString());
                } catch (eGet) {
                    form.submit();
                }
                return;
            }
            ev.preventDefault();
            submitBodegaFormPost(form, sub);
        });
    }

    /** Stock por variante en modal buscar producto; 0 es válido (no usar || con stock total). */
    function productSearchStockQty(it) {
        if (!it) return 0;
        if (it.variant_stock != null && it.variant_stock !== "") {
            return parseInt(it.variant_stock, 10) || 0;
        }
        return parseInt(it.stock || 0, 10) || 0;
    }

    function escProductSearch(v) {
        return String(v == null ? "" : v)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function productSearchResultsHeaderHtml() {
        return (
            '<div class="ingreso-product-results-table">' +
            '<div class="ingreso-product-results-head" role="row">' +
            '<div class="ingreso-product-col ingreso-product-col-codigo">Código</div>' +
            '<div class="ingreso-product-col ingreso-product-col-desc">Descripción</div>' +
            '<div class="ingreso-product-col ingreso-product-col-oem">Código OEM</div>' +
            '<div class="ingreso-product-col ingreso-product-col-variante">Variantes</div>' +
            '<div class="ingreso-product-col ingreso-product-col-stock">Stock</div>' +
            '<div class="ingreso-product-col ingreso-product-col-action" aria-hidden="true"></div>' +
            "</div>" +
            '<div class="ingreso-product-results-body" role="rowgroup">'
        );
    }

    function buildProductSearchRowHtml(it) {
        var oem = ((it.oem || it.codigo_oem || "").trim() || "—");
        var marca = ((it.marca || "").trim() || "—");
        var modelo = (it.modelo || "").trim();
        var desc = (it.descripcion || "").trim() || "—";
        return (
            '<div class="ingreso-product-item" role="row">' +
            '<div class="ingreso-product-col ingreso-product-col-codigo">' +
            escProductSearch(it.codigo || "") +
            "</div>" +
            '<div class="ingreso-product-col ingreso-product-col-desc">' +
            '<span class="ingreso-product-desc-text">' +
            escProductSearch(desc) +
            "</span>" +
            (modelo ? '<span class="ingreso-product-modelo-text">' + escProductSearch(modelo) + "</span>" : "") +
            "</div>" +
            '<div class="ingreso-product-col ingreso-product-col-oem">' +
            escProductSearch(oem) +
            "</div>" +
            '<div class="ingreso-product-col ingreso-product-col-variante">' +
            escProductSearch(marca) +
            "</div>" +
            '<div class="ingreso-product-col ingreso-product-col-stock">' +
            productSearchStockQty(it) +
            "</div>" +
            '<div class="ingreso-product-col ingreso-product-col-action">' +
            '<button type="button" class="btn btn-primary btn-sm">Seleccionar</button>' +
            "</div>" +
            "</div>"
        );
    }

    function mountProductSearchResults(container, items, onSelect) {
        if (!container) return;
        container.innerHTML = "";
        if (!Array.isArray(items) || items.length === 0) {
            container.innerHTML =
                '<div class="history-empty" style="border:none;background:#fff;padding:14px;">Sin resultados.</div>';
            return;
        }
        container.innerHTML = productSearchResultsHeaderHtml() + "</div></div>";
        var body = container.querySelector(".ingreso-product-results-body");
        if (!body) return;
        items.forEach(function (it) {
            var tmp = document.createElement("div");
            tmp.innerHTML = buildProductSearchRowHtml(it);
            var row = tmp.firstElementChild;
            if (!row) return;
            var btn = row.querySelector("button");
            if (btn && typeof onSelect === "function") {
                btn.addEventListener("click", function () {
                    onSelect(it);
                });
            }
            body.appendChild(row);
        });
    }

    function initIngresoView(root) {
        var form = root.querySelector("#ingresoForm");
        if (!form || form.dataset.bodegaUiBound === "20260603") return;
        form.dataset.bodegaUiBound = "20260603";

        var itemsBody = form.querySelector("#itemsBody");
        var addRowBtn = form.querySelector("#btnAddRow");
        var rutInput = form.querySelector("#supplier_rut");
        var rutRow = form.querySelector(".ingreso-rut-row");
        var btnBuscar = form.querySelector("#btnBuscarProveedor");
        var rutStatus = form.querySelector("#rutStatus");
        var numeroDocInput = form.querySelector("#numero_documento");
        var numeroDocStatus = form.querySelector("#numeroDocStatus");
        var numeroDuplicadoUrl = form.dataset.numeroDuplicadoUrl || "";
        var numeroDocCheckTimer = null;
        var supplierFormWrap = form.querySelector("#supplierFormWrap");
        var supplierCard = form.querySelector(".supplier-card");
        var supplierSummary = form.querySelector("#supplierSummary");
        var supplierSummaryName = form.querySelector("#supplierSummaryName");
        var supplierSummaryMeta = form.querySelector("#supplierSummaryMeta");
        var btnEditarProveedor = form.querySelector("#btnEditarProveedor");
        var supplierNameInput = form.querySelector("#supplier_name");

        if (!itemsBody || !addRowBtn || !rutInput || !btnBuscar || !rutStatus || !supplierFormWrap || !supplierSummary || !supplierSummaryName || !supplierSummaryMeta || !btnEditarProveedor || !supplierNameInput) {
            return;
        }

        var supplierSearchUrl = btnBuscar.getAttribute("data-search-url") || "";
        var lookupTimer = null;
        var lastLookupRut = "";
        var isLookingUp = false;
        var ingresoComunasPopulateFn = null;
        rutInput.readOnly = false;
        rutInput.disabled = false;

        function showSupplierRutField() {
            if (rutRow) rutRow.style.display = "";
            if (rutInput) {
                rutInput.setAttribute("required", "required");
                rutInput.dataset.rutRequired = "1";
            }
        }

        function hideSupplierRutFieldForSummary() {
            if (rutRow) rutRow.style.display = "none";
            if (rutInput) {
                rutInput.removeAttribute("required");
                rutInput.dataset.rutRequired = "0";
                rutInput.setCustomValidity("");
            }
        }

        function dispatchRutInputEvents() {
            if (!rutInput) return;
            try {
                rutInput.dispatchEvent(new InputEvent("input", { bubbles: true }));
            } catch (e0) {
                rutInput.dispatchEvent(new Event("input", { bubbles: true }));
            }
            rutInput.dispatchEvent(new Event("change", { bubbles: true }));
        }

        function syncSupplierRutValidity() {
            if (!rutInput) return;
            var raw = (rutInput.value || "").trim();
            if (!raw) {
                if (supplierSummary.classList.contains("is-visible")) {
                    rutInput.setCustomValidity("");
                }
                return;
            }
            var normalized =
                window.RutUtils && window.RutUtils.clean
                    ? window.RutUtils.clean(raw)
                    : raw;
            if (
                window.RutUtils &&
                window.RutUtils.isValid &&
                !window.RutUtils.isValid(normalized)
            ) {
                return;
            }
            rutInput.setCustomValidity("");
        }

        function setIngresoSupplierRut(rutRaw, opts) {
            opts = opts || {};
            if (!rutInput || rutRaw == null || String(rutRaw).trim() === "") {
                return false;
            }
            var formatted =
                window.RutUtils && window.RutUtils.format
                    ? window.RutUtils.format(String(rutRaw).trim())
                    : String(rutRaw).trim();
            if (!formatted) return false;
            rutInput.readOnly = false;
            rutInput.disabled = false;
            rutInput.value = formatted;
            dispatchRutInputEvents();
            syncSupplierRutValidity();
            updateSupplierSummary();
            if (opts.search) {
                searchSupplier(true);
            }
            return true;
        }

        function collapseSupplierSection() {
            supplierFormWrap.classList.add("is-collapsed");
            supplierSummary.classList.add("is-visible");
            hideSupplierRutFieldForSummary();
            if (supplierCard) {
                supplierCard.classList.add("ingreso-supplier-card--hidden");
                supplierCard.style.display = "none";
            }
        }

        function expandSupplierSection() {
            supplierFormWrap.classList.remove("is-collapsed");
            supplierSummary.classList.remove("is-visible");
            showSupplierRutField();
            if (supplierCard) {
                supplierCard.classList.remove("ingreso-supplier-card--hidden");
                supplierCard.style.display = "";
            }
        }

        function hideSupplierRegistrationCard() {
            supplierSummary.classList.remove("is-visible");
            showSupplierRutField();
            if (supplierCard) {
                supplierCard.classList.add("ingreso-supplier-card--hidden");
                supplierCard.style.display = "none";
            }
            supplierFormWrap.classList.add("is-collapsed");
        }

        function updateSupplierSummary() {
            var rutFormatted = (rutInput.value || "").trim() || "Sin RUT";
            var name = (supplierNameInput.value || "").trim() || "Proveedor sin nombre";
            supplierSummaryName.textContent = name;
            supplierSummaryMeta.textContent = "RUT: " + rutFormatted;
        }

        function initIngresoSupplierChileGeo() {
            var geoEl = document.getElementById("ingresoChileGeoData");
            var regionSel = form.querySelector("#ingresoSupplierRegion");
            var comunaSel = form.querySelector("#ingresoSupplierComuna");
            var ciudadInp = form.querySelector("#supplier_ciudad");
            var countryEl = form.querySelector("#supplier_country");
            if (!geoEl || !regionSel || !comunaSel) {
                return;
            }
            var chileGeo = [];
            try {
                chileGeo = JSON.parse(geoEl.textContent || "[]");
            } catch (e0) {
                chileGeo = [];
            }
            if (!Array.isArray(chileGeo)) {
                chileGeo = [];
            }
            var initialComuna = (comunaSel.getAttribute("data-initial-comuna") || comunaSel.value || "").trim();
            function normalize(v) {
                return String(v || "").trim().toLowerCase();
            }
            function isChileSupplier() {
                var v = normalize(countryEl && countryEl.value);
                return v === "chile" || v === "cl" || !v;
            }
            function isRegionMetropolitana(regionName) {
                return normalize(regionName || "").indexOf("metropolitana") !== -1;
            }
            function defaultCiudadForChile(regionName, comuna) {
                if (!comuna) {
                    return "";
                }
                return isRegionMetropolitana(regionName) ? "Santiago" : comuna;
            }
            function syncCityFromComuna() {
                if (!ciudadInp || !isChileSupplier()) {
                    return;
                }
                var comuna = comunaSel.value || "";
                if (!comuna) {
                    return;
                }
                var regionName = regionSel.value || "";
                var desired = defaultCiudadForChile(regionName, comuna);
                var cur = (ciudadInp.value || "").trim();
                if (!cur || normalize(cur) === normalize(comuna)) {
                    ciudadInp.value = desired;
                }
            }
            function ensureSantiagoIfMetroNoComuna(prevComunaOpt) {
                if (!ciudadInp || !isChileSupplier()) {
                    return;
                }
                var regionName = regionSel.value || "";
                if (!isRegionMetropolitana(regionName) || comunaSel.value) {
                    return;
                }
                var cur = (ciudadInp.value || "").trim();
                var prev = (prevComunaOpt || "").trim();
                if (!cur) {
                    ciudadInp.value = "Santiago";
                    return;
                }
                if (prev && normalize(cur) === normalize(prev)) {
                    ciudadInp.value = "Santiago";
                }
            }
            function findRegionData(name) {
                var want = normalize(name);
                for (var i = 0; i < chileGeo.length; i += 1) {
                    if (normalize(chileGeo[i].nombre) === want) {
                        return chileGeo[i];
                    }
                }
                return null;
            }
            function populateComunas(regionName, selectedComuna) {
                var region = findRegionData(regionName);
                var comunas = region && Array.isArray(region.comunas) ? region.comunas : [];
                comunaSel.innerHTML = "";
                var ph = document.createElement("option");
                ph.value = "";
                ph.textContent = "Seleccionar comuna";
                comunaSel.appendChild(ph);
                for (var j = 0; j < comunas.length; j += 1) {
                    var c = comunas[j];
                    var o = document.createElement("option");
                    o.value = c;
                    o.textContent = c;
                    comunaSel.appendChild(o);
                }
                if (selectedComuna) {
                    var exists = false;
                    for (var k = 0; k < comunas.length; k += 1) {
                        if (normalize(comunas[k]) === normalize(selectedComuna)) {
                            exists = true;
                            break;
                        }
                    }
                    comunaSel.value = exists ? selectedComuna : "";
                } else {
                    comunaSel.value = "";
                }
            }
            ingresoComunasPopulateFn = populateComunas;
            regionSel.addEventListener("change", function () {
                var prevComuna = comunaSel.value || "";
                populateComunas(regionSel.value, "");
                var rn = regionSel.value || "";
                if (isChileSupplier() && !isRegionMetropolitana(rn) && ciudadInp && normalize(ciudadInp.value) === "santiago") {
                    ciudadInp.value = "";
                }
                syncCityFromComuna();
                ensureSantiagoIfMetroNoComuna(prevComuna);
            });
            comunaSel.addEventListener("change", function () {
                syncCityFromComuna();
            });
            if (countryEl) {
                countryEl.addEventListener("change", function () {
                    populateComunas(regionSel.value, comunaSel.value || "");
                    syncCityFromComuna();
                    ensureSantiagoIfMetroNoComuna("");
                });
            }
            populateComunas(regionSel.value, initialComuna);
            comunaSel.removeAttribute("data-initial-comuna");
            syncCityFromComuna();
            ensureSantiagoIfMetroNoComuna("");
            var telBtn = document.getElementById("ingresoSupplierTelHelpBtn");
            var telHelpModal = document.getElementById("ingresoTelHelpModal");
            var telHelpModalClose = document.getElementById("ingresoTelHelpModalClose");
            function closeIngresoTelHelpModal() {
                if (!telHelpModal) {
                    return;
                }
                telHelpModal.classList.remove("open");
                telHelpModal.setAttribute("aria-hidden", "true");
                if (telBtn) {
                    telBtn.focus();
                }
            }
            function openIngresoTelHelpModal() {
                if (!telHelpModal) {
                    return;
                }
                telHelpModal.classList.add("open");
                telHelpModal.setAttribute("aria-hidden", "false");
                if (telHelpModalClose) {
                    setTimeout(function () {
                        telHelpModalClose.focus();
                    }, 10);
                }
            }
            if (telBtn && telHelpModal) {
                telBtn.addEventListener("click", function () {
                    openIngresoTelHelpModal();
                });
            }
            if (telHelpModalClose) {
                telHelpModalClose.addEventListener("click", closeIngresoTelHelpModal);
            }
            if (telHelpModal) {
                telHelpModal.addEventListener("click", function (ev) {
                    if (ev.target === telHelpModal) {
                        closeIngresoTelHelpModal();
                    }
                });
            }
            document.addEventListener("keydown", function (ev) {
                if (ev.key !== "Escape" || !telHelpModal || !telHelpModal.classList.contains("open")) {
                    return;
                }
                closeIngresoTelHelpModal();
            });
        }

        var defaultBodega = (form.getAttribute("data-default-bodega") || "Bodega 1").trim();
        var bodegasList = [];
        try {
            bodegasList = JSON.parse(form.getAttribute("data-bodegas") || "[]");
        } catch (e) {
            bodegasList = ["Bodega 1", "Bodega 2", "Bodega 3", "Bodega 4", "Bodega 5"];
        }
        if (!bodegasList || !bodegasList.length) {
            bodegasList = [defaultBodega];
        }
        var marcasUrl = (form.getAttribute("data-marcas-url") || "").trim();
        var productSearchUrl = (form.getAttribute("data-product-search-url") || "").trim();
        var codigoProvUrl = (form.getAttribute("data-codigo-prov-url") || "").trim();
        var marcaTimers = new WeakMap();
        var marcaOptionsByRow = new WeakMap();
        var outIvaResumen = document.getElementById("ingresoIvaResumen") || form.querySelector("#ingresoIvaResumen");
        var outTotalResumen = document.getElementById("ingresoTotalResumen") || form.querySelector("#ingresoTotalResumen");
        var inpTotalFactura = document.getElementById("ingresoTotalFactura") || form.querySelector("#ingresoTotalFactura");
        var IVA_RESUMEN_RATE = 0.19;
        var currentProductSearchRow = null;
        var supplierCountryInput = form.querySelector("#supplier_country");

        function resolveOrigenFromCountry(countryRaw) {
            var country = String(countryRaw || "").trim().toLowerCase();
            if (!country || country === "chile" || country === "cl") {
                return "nacional";
            }
            return "importacion";
        }

        function currentOrigenFromSupplier() {
            return resolveOrigenFromCountry(supplierCountryInput && supplierCountryInput.value);
        }

        function syncOrigenSelectsFromSupplier() {
            var targetOrigen = currentOrigenFromSupplier();
            itemsBody.querySelectorAll("select.ingreso-origen-select").forEach(function (sel) {
                sel.value = targetOrigen;
            });
        }

        /**
         * Monto chileno en línea (miles con punto, decimal con coma). Si no hay coma,
         * trata patrones tipo 12.500 o 3.500.000 como miles (sin decimal).
         */
        function parseValorNetoLine(raw) {
            var s = (raw || "").trim().replace(/\s/g, "");
            if (!s) {
                return 0;
            }
            if (s.indexOf(",") !== -1) {
                s = s.replace(/\./g, "").replace(",", ".");
            } else if (/^\d{1,3}(\.\d{3})+$/.test(s)) {
                s = s.replace(/\./g, "");
            }
            var v = parseFloat(s, 10);
            if (isNaN(v) || v < 0) {
                return 0;
            }
            return v;
        }

        /** Redondeo unitario: entero o 1 decimal (sin 8067.333333…). */
        function roundValorNetoUnitario(n) {
            var v = Number(n);
            if (!isFinite(v)) {
                return null;
            }
            if (Math.abs(v - Math.round(v)) < 1e-9) {
                return Math.round(v);
            }
            return Math.round(v * 10) / 10;
        }

        function formatValorNetoUnitario(n) {
            var v = roundValorNetoUnitario(n);
            if (v === null) {
                return String(n == null ? "" : n);
            }
            try {
                var frac = Math.abs(v - Math.round(v)) < 1e-9 ? 0 : 1;
                return v.toLocaleString("es-CL", {
                    minimumFractionDigits: frac,
                    maximumFractionDigits: 1,
                });
            } catch (fmtErr) {
                return String(v);
            }
        }

        /** Valor neto unitario para POST: sin separador de miles (evita 8.500 → 8.5 en Python). */
        function valorNetoToRawString(n) {
            if (n === null || n === undefined || n === "") {
                return "";
            }
            var v = Number(n);
            if (isNaN(v)) {
                return String(n).trim();
            }
            var rounded = roundValorNetoUnitario(v);
            if (rounded === null) {
                return "";
            }
            if (Math.abs(rounded - Math.round(rounded)) < 1e-9) {
                return String(Math.round(rounded));
            }
            return rounded.toFixed(1);
        }

        function readValorNetoIngresoInput(inp) {
            if (!inp) {
                return 0;
            }
            var raw = inp.getAttribute("data-raw");
            if (raw != null && raw !== "") {
                return parseValorNetoLine(raw);
            }
            return parseValorNetoLine(inp.value || "");
        }

        function bindValorNetoIngresoInput(inp) {
            if (!inp || inp.dataset.valorNetoBound === "1") {
                return;
            }
            inp.dataset.valorNetoBound = "1";
            inp.addEventListener("focus", function () {
                var raw = inp.getAttribute("data-raw");
                if (raw != null && raw !== "") {
                    inp.value = raw;
                }
            });
            inp.addEventListener("blur", function () {
                var parsed = parseValorNetoLine(inp.value);
                if (parsed > 0) {
                    var rawStr = valorNetoToRawString(parsed);
                    inp.setAttribute("data-raw", rawStr);
                    try {
                        inp.value = formatValorNetoUnitario(rawStr);
                    } catch (fmtErr) {
                        inp.value = rawStr;
                    }
                } else {
                    inp.removeAttribute("data-raw");
                }
            });
        }

        function setValorNetoIngresoRaw(inp, n) {
            if (!inp) {
                return;
            }
            bindValorNetoIngresoInput(inp);
            var raw = valorNetoToRawString(n);
            if (!raw) {
                inp.value = "";
                inp.removeAttribute("data-raw");
                return;
            }
            inp.setAttribute("data-raw", raw);
            try {
                inp.value = formatValorNetoUnitario(raw);
            } catch (fmtSetErr) {
                inp.value = raw;
            }
        }

        function syncValorNetoInputsForSubmit() {
            itemsBody.querySelectorAll("input[name='valor_neto_producto[]']").forEach(function (inp) {
                var raw = inp.getAttribute("data-raw");
                if (raw != null && raw !== "") {
                    inp.value = raw;
                }
            });
        }

        /** Margen % en línea: vacío/ inválido -> null; debe ser 0 <= m < 100. */
        function parseMargenPctIngreso(raw) {
            var s = (raw || "").trim().replace(/%/g, "").replace(/\s/g, "");
            if (!s) {
                return null;
            }
            if (s.indexOf(",") !== -1) {
                s = s.replace(/\./g, "").replace(",", ".");
            } else if (/^\d{1,3}(\.\d{3})+$/.test(s)) {
                s = s.replace(/\./g, "");
            }
            var v = parseFloat(s, 10);
            if (isNaN(v) || v < 0 || v >= 100) {
                return null;
            }
            return v;
        }

        function validateMargenYPrecioVentaIngresoRows() {
            itemsBody
                .querySelectorAll(
                    ".ingreso-margen-input.ingreso-requerido-invalido, .ingreso-precio-venta-input.ingreso-requerido-invalido"
                )
                .forEach(function (el) {
                    el.classList.remove("ingreso-requerido-invalido");
                });
            var firstFocus = null;
            var firstMsg = "";
            itemsBody.querySelectorAll(".item-row").forEach(function (row) {
                var ci = row.querySelector("input[name='codigo_producto[]']");
                var codigo = (ci && ci.value || "").trim();
                if (!codigo) {
                    return;
                }
                var inpMg = row.querySelector("input.ingreso-margen-input");
                var inpPV = row.querySelector("input.ingreso-precio-venta-input");
                var rawMg = (inpMg && inpMg.value || "").trim();
                var rawPV = (inpPV && inpPV.value || "").trim();
                var badMg = false;
                var badPv = false;
                if (!rawMg) {
                    badMg = true;
                } else if (parseMargenPctIngreso(inpMg.value) === null) {
                    badMg = true;
                }
                if (!rawPV) {
                    badPv = true;
                } else if (parseValorNetoLine(inpPV.value) <= 0) {
                    badPv = true;
                }
                if (badMg && inpMg) {
                    inpMg.classList.add("ingreso-requerido-invalido");
                    if (!firstFocus) {
                        firstFocus = inpMg;
                    }
                }
                if (badPv && inpPV) {
                    inpPV.classList.add("ingreso-requerido-invalido");
                    if (!firstFocus) {
                        firstFocus = inpPV;
                    }
                }
                if (!firstMsg) {
                    if (badMg && !rawMg) {
                        firstMsg =
                            "En cada línea con código interno debés completar el margen % y el P. neto (precio de venta neto).";
                    } else if (badMg) {
                        firstMsg =
                            "El margen % no es válido: debe ser un número entre 0 y 100 (sin incluir 100). Revisá las filas marcadas.";
                    } else if (badPv && !rawPV) {
                        firstMsg =
                            "En cada línea con código interno debés completar el margen % y el P. neto (precio de venta neto).";
                    } else if (badPv) {
                        firstMsg =
                            "El P. neto (precio de venta neto) debe ser mayor a 0. Revisá las filas marcadas.";
                    }
                }
            });
            if (firstMsg) {
                return { ok: false, message: firstMsg, focusEl: firstFocus };
            }
            return { ok: true };
        }

        function parseCantidadLinea(raw) {
            var n = parseInt(String(raw || "").trim(), 10);
            if (isNaN(n) || n <= 0) {
                return 0;
            }
            return n;
        }

        function formatMontoResumen(n) {
            try {
                return n.toLocaleString("es-CL", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
            } catch (e1) {
                return String(Math.round(n * 100) / 100);
            }
        }

        function parseTotalFacturaLine() {
            if (!inpTotalFactura) {
                return null;
            }
            var raw = (inpTotalFactura.value || "").trim();
            if (!raw) {
                return null;
            }
            var v = parseValorNetoLine(raw);
            return v > 0 ? v : null;
        }

        function updateIngresoTotals() {
            if (!outIvaResumen || !outTotalResumen) {
                return;
            }
            var sum = 0;
            var usedPrecioVentaFallback = false;
            itemsBody.querySelectorAll(".item-row").forEach(function (row) {
                var inpValor = row.querySelector("input[name='valor_neto_producto[]']");
                var inpPv = row.querySelector("input.ingreso-precio-venta-input");
                var inpCantidad = row.querySelector("input[name='cantidad_producto[]']");
                var valorUnitario = readValorNetoIngresoInput(inpValor);
                if (valorUnitario <= 0 && inpPv) {
                    var pv = parseValorNetoLine(inpPv.value || "");
                    if (pv > 0) {
                        valorUnitario = pv;
                        usedPrecioVentaFallback = true;
                    }
                }
                var cantidad = parseCantidadLinea(inpCantidad ? inpCantidad.value : "");
                sum += valorUnitario * cantidad;
            });
            if (sum <= 0) {
                outIvaResumen.textContent = "—";
                outTotalResumen.textContent = "—";
                outIvaResumen.title = "";
                outTotalResumen.title = "";
                return;
            }
            var tf = parseTotalFacturaLine();
            if (tf != null) {
                if (tf + 1e-9 < sum) {
                    outIvaResumen.textContent = "—";
                    outTotalResumen.textContent = "—";
                    outIvaResumen.title = "El total con IVA no puede ser menor que la suma de netos.";
                    outTotalResumen.title = outIvaResumen.title;
                    return;
                }
                var ivaAdj = Math.round((tf - sum) * 100) / 100;
                var totalAdj = Math.round(tf * 100) / 100;
                outIvaResumen.textContent = formatMontoResumen(ivaAdj);
                outTotalResumen.textContent = formatMontoResumen(totalAdj);
                outIvaResumen.title = "IVA = total factura menos suma de netos (cuadre con documento físico).";
                outTotalResumen.title = "Total según campo Total factura (con IVA).";
                return;
            }
            var iva = Math.round(sum * IVA_RESUMEN_RATE * 100) / 100;
            var total = Math.round((sum + iva) * 100) / 100;
            outIvaResumen.textContent = formatMontoResumen(iva);
            outTotalResumen.textContent = formatMontoResumen(total);
            var hintSum =
                "Suma de líneas: Cant. × V. neto unit. (costo). " +
                (usedPrecioVentaFallback
                    ? "En líneas sin V. neto se usó P. neto solo para el resumen."
                    : "");
            outIvaResumen.title = hintSum;
            outTotalResumen.title = hintSum;
        }

        function buildBodegaSelect(selected) {
            var sel = document.createElement("select");
            sel.name = "bodega_producto[]";
            sel.className = "ingreso-bodega-select";
            sel.setAttribute("required", "required");
            sel.setAttribute("aria-label", "Bodega");
            var want = (selected || defaultBodega).trim();
            bodegasList.forEach(function (b) {
                var o = document.createElement("option");
                o.value = b;
                o.textContent = b;
                if (b === want) {
                    o.selected = true;
                }
                sel.appendChild(o);
            });
            if (!sel.value && bodegasList.length) {
                sel.selectedIndex = 0;
            }
            return sel;
        }

        function buildOrigenSelect(selected) {
            var sel = document.createElement("select");
            sel.name = "origen_compra_producto[]";
            sel.className = "ingreso-origen-select";
            sel.setAttribute("required", "required");
            sel.setAttribute("aria-label", "Origen de compra");
            var want = String(selected || currentOrigenFromSupplier()).trim().toLowerCase();
            ["nacional", "importacion"].forEach(function (o) {
                var opt = document.createElement("option");
                opt.value = o;
                opt.textContent = o.charAt(0).toUpperCase() + o.slice(1);
                if (o === want) opt.selected = true;
                sel.appendChild(opt);
            });
            return sel;
        }

        function closeMarcaMenu(row) {
            if (!row) {
                return;
            }
            var menu = row.querySelector(".ingreso-marca-menu");
            var btn = row.querySelector(".ingreso-marca-dropdown-btn");
            if (menu) {
                menu.hidden = true;
                menu.classList.remove("is-dropup");
                menu.style.maxHeight = "";
            }
            if (btn) {
                btn.setAttribute("aria-expanded", "false");
            }
        }

        function closeAllMarcaMenus() {
            itemsBody.querySelectorAll("tr.item-row").forEach(closeMarcaMenu);
        }

        function marcaMenuFilterText(row, menuOpts) {
            menuOpts = menuOpts || {};
            if (menuOpts.showAll) {
                return "";
            }
            if (menuOpts.filter != null) {
                return String(menuOpts.filter).trim().toUpperCase();
            }
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            return marcaInput ? (marcaInput.value || "").trim().toUpperCase() : "";
        }

        function marcaOptionMatchesFilter(mu, filterText) {
            if (!filterText) {
                return true;
            }
            return mu.indexOf(filterText) >= 0;
        }

        function renderMarcaMenu(row, menuOpts) {
            var menu = row.querySelector(".ingreso-marca-menu");
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            if (!menu || !marcaInput) {
                return;
            }
            var opts = marcaOptionsByRow.get(row) || [];
            var current = (marcaInput.value || "").trim().toUpperCase();
            var filterText = marcaMenuFilterText(row, menuOpts);
            menu.innerHTML = "";
            if (!opts.length) {
                var emptyNoOpts = document.createElement("div");
                emptyNoOpts.className = "ingreso-marca-menu-empty";
                emptyNoOpts.textContent = "Sin variantes para este código";
                menu.appendChild(emptyNoOpts);
                return;
            }
            var shown = 0;
            opts.forEach(function (m) {
                var mu = (m || "").trim().toUpperCase();
                if (!mu || !marcaOptionMatchesFilter(mu, filterText)) {
                    return;
                }
                shown += 1;
                var item = document.createElement("button");
                item.type = "button";
                item.className = "ingreso-marca-menu-item";
                item.setAttribute("role", "option");
                item.textContent = mu;
                if (current && mu === current) {
                    item.classList.add("is-selected");
                    item.setAttribute("aria-selected", "true");
                } else {
                    item.setAttribute("aria-selected", "false");
                }
                item.addEventListener("mousedown", function (ev) {
                    ev.preventDefault();
                });
                item.addEventListener("click", function (ev) {
                    ev.preventDefault();
                    ev.stopPropagation();
                    marcaInput.value = mu;
                    marcaInput.dispatchEvent(new Event("input", { bubbles: true }));
                    marcaInput.dispatchEvent(new Event("change", { bubbles: true }));
                    closeMarcaMenu(row);
                    marcaInput.focus();
                });
                menu.appendChild(item);
            });
            if (!shown) {
                var emptyMatch = document.createElement("div");
                emptyMatch.className = "ingreso-marca-menu-empty";
                emptyMatch.textContent = filterText
                    ? "Sin coincidencias"
                    : "Sin variantes para este código";
                menu.appendChild(emptyMatch);
            }
        }

        function positionMarcaMenu(row) {
            var wrap = row.querySelector(".ingreso-marca-wrap");
            var menu = row.querySelector(".ingreso-marca-menu");
            if (!wrap || !menu || menu.hidden) {
                return;
            }
            menu.classList.remove("is-dropup");
            menu.style.maxHeight = "";
            var rect = wrap.getBoundingClientRect();
            var maxDefault = 200;
            var gap = 8;
            var spaceBelow = window.innerHeight - rect.bottom - gap;
            var spaceAbove = rect.top - gap;
            menu.hidden = false;
            var natural = menu.scrollHeight;
            var menuHeight = Math.min(maxDefault, natural || maxDefault);
            var preferDropup = spaceBelow < menuHeight && spaceAbove > spaceBelow;
            if (preferDropup) {
                menu.classList.add("is-dropup");
                menu.style.maxHeight = Math.min(maxDefault, Math.max(72, spaceAbove - 4)) + "px";
            } else {
                menu.style.maxHeight = Math.min(maxDefault, Math.max(72, spaceBelow - 4)) + "px";
            }
        }

        function openMarcaMenu(row, menuOpts) {
            if (!row || !row.querySelector(".ingreso-marca-wrap")) {
                return;
            }
            closeAllMarcaMenus();
            renderMarcaMenu(row, menuOpts);
            var menu = row.querySelector(".ingreso-marca-menu");
            var btn = row.querySelector(".ingreso-marca-dropdown-btn");
            if (menu) {
                menu.hidden = false;
            }
            if (btn) {
                btn.setAttribute("aria-expanded", "true");
            }
            requestAnimationFrame(function () {
                positionMarcaMenu(row);
            });
        }

        function updateMarcaMenuFromInput(row) {
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            var menu = row.querySelector(".ingreso-marca-menu");
            var btn = row.querySelector(".ingreso-marca-dropdown-btn");
            if (!marcaInput || !menu) {
                return;
            }
            closeAllMarcaMenus();
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            var code = codeInput ? (codeInput.value || "").trim() : "";
            if (!code) {
                closeMarcaMenu(row);
                return;
            }
            var opts = marcaOptionsByRow.get(row);
            if (!opts || !opts.length) {
                loadMarcasForRow(row, function () {
                    updateMarcaMenuFromInput(row);
                });
                return;
            }
            renderMarcaMenu(row, { filter: marcaInput.value });
            menu.hidden = false;
            if (btn) {
                btn.setAttribute("aria-expanded", "true");
            }
            requestAnimationFrame(function () {
                positionMarcaMenu(row);
            });
        }

        function ensureMarcaCombobox(row) {
            if (!row) {
                return;
            }
            if (row.querySelector(".ingreso-marca-wrap")) {
                row.dataset.marcaCombobox = "1";
                return;
            }
            if (row.dataset.marcaCombobox === "1") {
                return;
            }
            var stack = row.querySelector(".cell-marca-stack");
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            if (!stack || !marcaInput) {
                return;
            }
            var dl = row.querySelector("datalist.ingreso-marca-datalist");
            if (dl) {
                dl.remove();
            }
            marcaInput.removeAttribute("list");

            var wrap = document.createElement("div");
            wrap.className = "ingreso-marca-wrap";
            stack.insertBefore(wrap, marcaInput);
            wrap.appendChild(marcaInput);

            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "ingreso-marca-dropdown-btn";
            btn.title = "Ver todas las variantes";
            btn.setAttribute("aria-label", "Ver todas las variantes");
            btn.setAttribute("aria-haspopup", "listbox");
            btn.setAttribute("aria-expanded", "false");
            btn.innerHTML = "&#9662;";
            wrap.appendChild(btn);

            var menu = document.createElement("div");
            menu.className = "ingreso-marca-menu";
            menu.setAttribute("role", "listbox");
            menu.setAttribute("aria-label", "Variantes de marca");
            menu.hidden = true;
            wrap.appendChild(menu);

            row.dataset.marcaCombobox = "1";
        }

        function bindMarcaCombobox(row) {
            ensureMarcaCombobox(row);
            var wrap = row.querySelector(".ingreso-marca-wrap");
            var btn = row.querySelector(".ingreso-marca-dropdown-btn");
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            if (!wrap) {
                return;
            }
            if (marcaInput && marcaInput.dataset.marcaInputBound !== "1") {
                marcaInput.dataset.marcaInputBound = "1";
                marcaInput.addEventListener("input", function () {
                    var text = (marcaInput.value || "").trim();
                    if (!text) {
                        closeMarcaMenu(row);
                        return;
                    }
                    updateMarcaMenuFromInput(row);
                });
                marcaInput.addEventListener("keydown", function (ev) {
                    if (ev.key === "Escape") {
                        closeMarcaMenu(row);
                    }
                });
            }
            if (!btn || btn.dataset.bound === "1") {
                return;
            }
            btn.dataset.bound = "1";
            btn.addEventListener("click", function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                var menu = row.querySelector(".ingreso-marca-menu");
                if (menu && !menu.hidden) {
                    closeMarcaMenu(row);
                    return;
                }
                var codeInput = row.querySelector("input[name='codigo_producto[]']");
                var code = codeInput ? (codeInput.value || "").trim() : "";
                if (!code) {
                    return;
                }
                var opts = marcaOptionsByRow.get(row);
                if (!opts || !opts.length) {
                    loadMarcasForRow(row, function () {
                        openMarcaMenu(row, { showAll: true });
                    });
                    return;
                }
                openMarcaMenu(row, { showAll: true });
            });
        }

        function clearMarcaSuggestions(row) {
            marcaOptionsByRow.set(row, []);
            var menu = row.querySelector(".ingreso-marca-menu");
            if (menu) {
                menu.innerHTML = "";
                menu.hidden = true;
            }
            var hint = row.querySelector(".ingreso-marca-hint");
            if (hint) {
                hint.style.display = "none";
            }
        }

        function setCodigoInternoValidState(row, ok, msg) {
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            var wrap = row.querySelector(".ingreso-code-cell");
            if (!codeInput) {
                return;
            }
            if (ok) {
                codeInput.classList.remove("ingreso-codigo-interno-invalido");
                codeInput.removeAttribute("aria-invalid");
                if (wrap) {
                    var m = wrap.querySelector(".ingreso-codigo-interno-msg");
                    if (m) {
                        m.textContent = "";
                        m.style.display = "none";
                    }
                }
            } else {
                codeInput.classList.add("ingreso-codigo-interno-invalido");
                codeInput.setAttribute("aria-invalid", "true");
                if (wrap) {
                    var m2 = wrap.querySelector(".ingreso-codigo-interno-msg");
                    if (!m2) {
                        m2 = document.createElement("small");
                        m2.className = "ingreso-codigo-interno-msg";
                        wrap.appendChild(m2);
                    }
                    m2.textContent = msg || "Código no válido.";
                    m2.style.display = "block";
                }
            }
        }

        function validateCodigoInterno(row) {
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            if (!codeInput || !marcasUrl) {
                return Promise.resolve(true);
            }
            var code = (codeInput.value || "").trim().toUpperCase();
            if (!code) {
                setCodigoInternoValidState(row, true, "");
                return Promise.resolve(true);
            }
            return fetch(marcasUrl + "?codigo=" + encodeURIComponent(code), {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) {
                    return res.json();
                })
                .then(function (data) {
                    if (!data || !data.ok || typeof data.existe !== "boolean") {
                        setCodigoInternoValidState(row, true, "");
                        return true;
                    }
                    if (data.existe === false) {
                        setCodigoInternoValidState(row, false, "Código inexistente o inactivo en catálogo.");
                        return false;
                    }
                    setCodigoInternoValidState(row, true, "");
                    return true;
                })
                .catch(function () {
                    setCodigoInternoValidState(row, true, "");
                    return true;
                });
        }

        function loadMarcasForRow(row, onDone) {
            if (!marcasUrl) {
                if (onDone) {
                    onDone();
                }
                return;
            }
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            var hint = row.querySelector(".ingreso-marca-hint");
            if (!codeInput || !marcaInput) {
                if (onDone) {
                    onDone();
                }
                return;
            }
            var code = (codeInput.value || "").trim().toUpperCase();
            if (!code) {
                clearMarcaSuggestions(row);
                if (onDone) {
                    onDone();
                }
                return;
            }
            fetch(marcasUrl + "?codigo=" + encodeURIComponent(code), {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) {
                    return res.json();
                })
                .then(function (data) {
                    if (!data || !data.ok || !data.marcas || data.marcas.length === 0) {
                        clearMarcaSuggestions(row);
                        return;
                    }
                    var marcas = data.marcas.map(function (m) {
                        return (m || "").trim().toUpperCase();
                    });
                    marcaOptionsByRow.set(row, marcas);
                    var menu = row.querySelector(".ingreso-marca-menu");
                    if (menu && !menu.hidden) {
                        renderMarcaMenu(row, { filter: marcaInput.value });
                        requestAnimationFrame(function () {
                            positionMarcaMenu(row);
                        });
                    }
                    if (hint) {
                        var regs = Array.isArray(data.marcas_registradas) ? data.marcas_registradas : [];
                        if (regs.length > 0) {
                            hint.textContent =
                                "Variantes ya registradas para este código: " +
                                regs.length +
                                " (usá la flecha para elegir otra). También podés escribir una nueva.";
                        } else {
                            hint.textContent =
                                "Sin variantes previas para este código: elegí una sugerencia o escribí una nueva.";
                        }
                        hint.style.display = "block";
                    }
                })
                .catch(function () {
                    clearMarcaSuggestions(row);
                })
                .finally(function () {
                    if (onDone) {
                        onDone();
                    }
                });
        }

        function scheduleMarcasFetch(row) {
            if (!marcasUrl) {
                return;
            }
            var prev = marcaTimers.get(row);
            if (prev) {
                clearTimeout(prev);
            }
            marcaTimers.set(
                row,
                setTimeout(function () {
                    loadMarcasForRow(row);
                }, 280)
            );
        }

        function bindCodigoProveedorLookup(row) {
            if (!codigoProvUrl) {
                return;
            }
            var inpProv = row.querySelector("input.ingreso-codigo-proveedor-input");
            var inpCode = row.querySelector("input[name='codigo_producto[]']");
            if (!inpProv || !inpCode) {
                return;
            }
            var provTimer = null;
            function tryMap() {
                var rut = (rutInput.value || "").trim();
                var cp = (inpProv.value || "").trim();
                if (!rut || !cp) {
                    return;
                }
                if ((inpCode.value || "").trim()) {
                    return;
                }
                fetch(
                    codigoProvUrl +
                        "?rut=" +
                        encodeURIComponent(rut) +
                        "&codigo_proveedor=" +
                        encodeURIComponent(cp),
                    { headers: { "X-Requested-With": "XMLHttpRequest" } }
                )
                    .then(function (res) {
                        return res.json();
                    })
                    .then(function (data) {
                        if (!data || !data.ok || !data.codigo_interno) {
                            return;
                        }
                        if ((inpCode.value || "").trim()) {
                            return;
                        }
                        inpCode.value = data.codigo_interno;
                        setCodigoInternoValidState(row, true, "");
                        scheduleMarcasFetch(row);
                    })
                    .catch(function () {});
            }
            function debounceProv() {
                if (provTimer) {
                    clearTimeout(provTimer);
                }
                provTimer = setTimeout(tryMap, 400);
            }
            inpProv.addEventListener("input", debounceProv);
            inpProv.addEventListener("blur", tryMap);
        }

        function parseChileFloat(s) {
            if (s == null || typeof s !== "string") return null;
            var t = s.trim().replace(/\s/g, "");
            if (!t) return null;
            if (t.indexOf(",") >= 0) {
                t = t.replace(/\./g, "").replace(",", ".");
            }
            var v = parseFloat(t);
            return isNaN(v) ? null : v;
        }

        /**
         * Margen % = margen sobre el precio de venta neto (rentabilidad / % del PVP).
         * P. venta neto unit. = costo_unitario / (1 − margen/100). Ej.: 4000 y 44% → 4000/0,56 ≈ 7142,86.
         * Sin margen informado no recalcula. Margen ≥ 100% no aplica (evita división por cero).
         */
        function recalcPrecioVentaDesdeMargen(row) {
            if (row && row.dataset.ingresoSilentMargen === "1") {
                return;
            }
            var cantInp = row.querySelector("input[name='cantidad_producto[]']");
            var vnInp = row.querySelector("input[name='valor_neto_producto[]']");
            var mgInp = row.querySelector("input.ingreso-margen-input");
            var pvInp = row.querySelector("input.ingreso-precio-venta-input");
            if (!pvInp || !mgInp) return;
            var mgRaw = (mgInp.value || "").trim();
            if (!mgRaw) return;
            var vn = readValorNetoIngresoInput(vnInp);
            var mg = parseChileFloat(mgRaw);
            if (vn == null || vn <= 0 || mg == null) return;
            if (mg >= 100) return;
            var denom = 1 - mg / 100;
            if (denom <= 0) return;
            var unitCost = vn;
            var precio = unitCost / denom;
            pvInp.value = String(Math.round(precio * 100) / 100);
        }

        /**
         * Inverso: si el usuario ingresa P. venta neto unitario, deriva margen %.
         * costo_unitario = V.neto unitario; margen = 100 * (1 - costo_unitario / P).
         */
        /**
         * Si cambia la cantidad, escalar el V. neto de la línea en la misma proporción
         * (mismo costo unitario de compra → el resumen IVA/Total y el P. venta con margen fijo cuadran).
         */
        function escalarValorNetoSiCambiaCantidad(row, inpCant) {
            if (!inpCant) return;
            var prevS = inpCant.getAttribute("data-ingreso-prev-cant");
            var prev =
                prevS != null && prevS !== "" ? parseInt(prevS, 10) : NaN;
            var newC = parseInt(String(inpCant.value || "").trim(), 10);
            if (isNaN(newC) || newC < 1) {
                newC = 1;
            }
            var vnInp = row.querySelector("input[name='valor_neto_producto[]']");
            if (!vnInp) {
                inpCant.setAttribute("data-ingreso-prev-cant", String(newC));
                return;
            }
            var vn = readValorNetoIngresoInput(vnInp);
            if (
                vn > 0 &&
                !isNaN(prev) &&
                prev > 0 &&
                newC > 0 &&
                prev !== newC
            ) {
                var scaled = vn * (newC / prev);
                setValorNetoIngresoRaw(vnInp, Math.round(scaled * 100) / 100);
            }
            inpCant.setAttribute("data-ingreso-prev-cant", String(newC));
        }

        function recalcMargenDesdePrecioVenta(row) {
            var cantInp = row.querySelector("input[name='cantidad_producto[]']");
            var vnInp = row.querySelector("input[name='valor_neto_producto[]']");
            var mgInp = row.querySelector("input.ingreso-margen-input");
            var pvInp = row.querySelector("input.ingreso-precio-venta-input");
            if (!pvInp || !mgInp) return;
            var pvRaw = (pvInp.value || "").trim();
            if (!pvRaw) {
                mgInp.value = "";
                return;
            }
            var vn = readValorNetoIngresoInput(vnInp);
            var pv = parseChileFloat(pvRaw);
            if (vn == null || vn <= 0 || pv == null || pv <= 0) {
                return;
            }
            var unitCost = vn;
            if (pv <= 0 || unitCost <= 0) return;
            var m = 100 * (1 - unitCost / pv);
            if (!isFinite(m)) return;
            if (m > 99.99) m = 99.99;
            if (m < -99.99) m = -99.99;
            var rounded = Math.round(m * 100) / 100;
            var out =
                Math.abs(rounded - Math.round(rounded)) < 1e-6
                    ? String(Math.round(rounded))
                    : String(rounded);
            row.dataset.ingresoSilentMargen = "1";
            mgInp.value = out;
            setTimeout(function () {
                if (row.dataset) row.dataset.ingresoSilentMargen = "0";
            }, 0);
        }

        function bindIngresoProductRow(row) {
            if (!row || row.dataset.ingresoRowBound === "1") {
                return;
            }
            row.dataset.ingresoRowBound = "1";
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            if (codeInput) {
                codeInput.addEventListener("input", function () {
                    setCodigoInternoValidState(row, true, "");
                    scheduleMarcasFetch(row);
                });
                codeInput.addEventListener("blur", function () {
                    scheduleMarcasFetch(row);
                    validateCodigoInterno(row);
                });
            }
            var searchBtn = row.querySelector(".ingreso-code-search-btn");
            if (searchBtn && searchBtn.dataset.bound !== "1") {
                searchBtn.dataset.bound = "1";
                searchBtn.addEventListener("click", function () {
                    openProductSearchModal(row);
                });
            }
            var inpVN = row.querySelector("input[name='valor_neto_producto[]']");
            var inpCant = row.querySelector("input[name='cantidad_producto[]']");
            var inpMg = row.querySelector("input.ingreso-margen-input");
            var inpPV = row.querySelector("input.ingreso-precio-venta-input");
            if (inpMg) {
                inpMg.addEventListener("input", function () {
                    inpMg.classList.remove("ingreso-requerido-invalido");
                    recalcPrecioVentaDesdeMargen(row);
                    updateIngresoTotals();
                });
                inpMg.addEventListener("change", function () {
                    inpMg.classList.remove("ingreso-requerido-invalido");
                    recalcPrecioVentaDesdeMargen(row);
                    updateIngresoTotals();
                });
            }
            if (inpPV) {
                inpPV.addEventListener("input", function () {
                    inpPV.classList.remove("ingreso-requerido-invalido");
                    recalcMargenDesdePrecioVenta(row);
                    updateIngresoTotals();
                });
                inpPV.addEventListener("change", function () {
                    inpPV.classList.remove("ingreso-requerido-invalido");
                    recalcMargenDesdePrecioVenta(row);
                    updateIngresoTotals();
                });
            }
            function sincronizarPrecioMargenTrasCosto(row) {
                var mgRaw = (inpMg && inpMg.value || "").trim();
                if (mgRaw) {
                    recalcPrecioVentaDesdeMargen(row);
                } else if (inpPV && (inpPV.value || "").trim()) {
                    recalcMargenDesdePrecioVenta(row);
                }
            }

            if (inpVN) {
                inpVN.addEventListener("input", function () {
                    sincronizarPrecioMargenTrasCosto(row);
                    updateIngresoTotals();
                });
            }
            if (inpCant) {
                inpCant.setAttribute(
                    "data-ingreso-prev-cant",
                    String(parseInt(inpCant.value, 10) || 1)
                );
                inpCant.addEventListener("input", function () {
                    sincronizarPrecioMargenTrasCosto(row);
                    updateIngresoTotals();
                });
            }
            bindMarcaCombobox(row);
            scheduleMarcasFetch(row);
            bindCodigoProveedorLookup(row);
        }

        function refreshIngresoItemNumbers() {
            if (!itemsBody) {
                return;
            }
            itemsBody.querySelectorAll("tr.item-row").forEach(function (tr, i) {
                var span = tr.querySelector("td.ingreso-item-num-cell .ingreso-item-num");
                if (span) {
                    span.textContent = String(i + 1);
                }
            });
        }

        function addRow() {
            var tr = document.createElement("tr");
            tr.className = "item-row";

            var tdNum = document.createElement("td");
            tdNum.className = "ingreso-item-num-cell";
            var spanNum = document.createElement("span");
            spanNum.className = "ingreso-item-num";
            spanNum.textContent = "0";
            tdNum.appendChild(spanNum);

            var tdProv = document.createElement("td");
            var inpProv = document.createElement("input");
            inpProv.name = "codigo_proveedor_producto[]";
            inpProv.className = "ingreso-codigo-proveedor-input";
            inpProv.placeholder = "Cód. prov.";
            inpProv.setAttribute("autocomplete", "off");
            inpProv.setAttribute("aria-label", "Código del proveedor");
            tdProv.appendChild(inpProv);

            var tdCode = document.createElement("td");
            var codeCell = document.createElement("div");
            codeCell.className = "ingreso-code-cell";
            var inputWrap = document.createElement("div");
            inputWrap.className = "ingreso-code-input-wrap";
            var inpCode = document.createElement("input");
            inpCode.name = "codigo_producto[]";
            inpCode.placeholder = "Interno";
            inpCode.required = true;
            inpCode.setAttribute("autocomplete", "off");
            inputWrap.appendChild(inpCode);
            var btnSearchCode = document.createElement("button");
            btnSearchCode.type = "button";
            btnSearchCode.className = "ingreso-code-search-btn";
            btnSearchCode.title = "Buscar producto";
            btnSearchCode.setAttribute("aria-label", "Buscar producto");
            btnSearchCode.textContent = "🔎";
            inputWrap.appendChild(btnSearchCode);
            codeCell.appendChild(inputWrap);
            tdCode.appendChild(codeCell);

            var tdMarca = document.createElement("td");
            tdMarca.className = "cell-marca";
            var marcaStack = document.createElement("div");
            marcaStack.className = "cell-marca-stack";
            var marcaWrap = document.createElement("div");
            marcaWrap.className = "ingreso-marca-wrap";
            var inpMarca = document.createElement("input");
            inpMarca.name = "marca_producto[]";
            inpMarca.className = "ingreso-marca-input";
            inpMarca.placeholder = "Marca / variante";
            inpMarca.setAttribute("autocomplete", "off");
            inpMarca.setAttribute("aria-label", "Marca o variante");
            marcaWrap.appendChild(inpMarca);
            var btnMarcaDrop = document.createElement("button");
            btnMarcaDrop.type = "button";
            btnMarcaDrop.className = "ingreso-marca-dropdown-btn";
            btnMarcaDrop.title = "Ver todas las variantes";
            btnMarcaDrop.setAttribute("aria-label", "Ver todas las variantes");
            btnMarcaDrop.setAttribute("aria-haspopup", "listbox");
            btnMarcaDrop.setAttribute("aria-expanded", "false");
            btnMarcaDrop.innerHTML = "&#9662;";
            marcaWrap.appendChild(btnMarcaDrop);
            var menuMarca = document.createElement("div");
            menuMarca.className = "ingreso-marca-menu";
            menuMarca.setAttribute("role", "listbox");
            menuMarca.setAttribute("aria-label", "Variantes de marca");
            menuMarca.hidden = true;
            marcaWrap.appendChild(menuMarca);
            marcaStack.appendChild(marcaWrap);
            tr.dataset.marcaCombobox = "1";
            var hint = document.createElement("small");
            hint.className = "ingreso-marca-hint";
            hint.style.display = "none";
            hint.textContent = "Marcas ya registradas para este código: desplegá el campo o escribí otra.";
            marcaStack.appendChild(hint);
            tdMarca.appendChild(marcaStack);

            var tdBodega = document.createElement("td");
            tdBodega.appendChild(buildBodegaSelect(defaultBodega));

            var tdOrigen = document.createElement("td");
            tdOrigen.appendChild(buildOrigenSelect(currentOrigenFromSupplier()));

            var tdCant = document.createElement("td");
            var inpCant = document.createElement("input");
            inpCant.name = "cantidad_producto[]";
            inpCant.type = "number";
            inpCant.min = "1";
            inpCant.step = "1";
            inpCant.required = true;
            tdCant.appendChild(inpCant);

            var tdValorNeto = document.createElement("td");
            var inpVN = document.createElement("input");
            inpVN.name = "valor_neto_producto[]";
            inpVN.type = "text";
            inpVN.setAttribute("inputmode", "decimal");
            inpVN.placeholder = "Neto unit.";
            inpVN.title = "Costo neto unitario según documento del proveedor, opcional";
            inpVN.setAttribute("autocomplete", "off");
            inpVN.setAttribute("aria-label", "Valor neto costo");
            bindValorNetoIngresoInput(inpVN);
            tdValorNeto.appendChild(inpVN);

            var tdMargen = document.createElement("td");
            var inpMg = document.createElement("input");
            inpMg.name = "margen_pct_producto[]";
            inpMg.className = "ingreso-margen-input";
            inpMg.type = "text";
            inpMg.setAttribute("inputmode", "decimal");
            inpMg.placeholder = "%";
            inpMg.title = "Margen % sobre el precio de venta neto (opcional). P. neto = costo unit. ÷ (1 − margen/100)";
            inpMg.setAttribute("autocomplete", "off");
            inpMg.setAttribute("aria-label", "Margen porcentaje");
            tdMargen.appendChild(inpMg);

            var tdPrecioVenta = document.createElement("td");
            var inpPV = document.createElement("input");
            inpPV.name = "precio_venta_neto_producto[]";
            inpPV.className = "ingreso-precio-venta-input";
            inpPV.type = "text";
            inpPV.setAttribute("inputmode", "decimal");
            inpPV.placeholder = "P. venta";
            inpPV.title =
                "Precio de venta neto unitario sin IVA (opcional). Si completás V. neto y cantidad, al escribir aquí se calcula el margen %.";
            inpPV.setAttribute("autocomplete", "off");
            inpPV.setAttribute("aria-label", "Precio venta neto unitario");
            tdPrecioVenta.appendChild(inpPV);

            var tdNota = document.createElement("td");
            var inpNota = document.createElement("input");
            inpNota.name = "nota_producto[]";
            inpNota.placeholder = "Nota";
            tdNota.appendChild(inpNota);

            var tdAct = document.createElement("td");
            var btnRm = document.createElement("button");
            btnRm.type = "button";
            btnRm.className = "btn btn-warn btnRemove btn-compact";
            btnRm.setAttribute("title", "Quitar fila");
            btnRm.setAttribute("aria-label", "Quitar fila");
            btnRm.innerHTML = "&#10005;";
            tdAct.appendChild(btnRm);

            tr.appendChild(tdNum);
            tr.appendChild(tdProv);
            tr.appendChild(tdCode);
            tr.appendChild(tdMarca);
            tr.appendChild(tdBodega);
            tr.appendChild(tdOrigen);
            tr.appendChild(tdCant);
            tr.appendChild(tdValorNeto);
            tr.appendChild(tdMargen);
            tr.appendChild(tdPrecioVenta);
            tr.appendChild(tdNota);
            tr.appendChild(tdAct);

            itemsBody.appendChild(tr);
            bindRemove(btnRm);
            bindIngresoProductRow(tr);
            refreshIngresoItemNumbers();
            updateIngresoTotals();
        }

        var productModal = document.getElementById("ingresoProductModal");
        var productModalClose = document.getElementById("ingresoProductModalClose");
        var productSearchInput = document.getElementById("ingresoProductSearchInput");
        var productSearchBtn = document.getElementById("ingresoProductSearchBtn");
        var productSearchStatus = document.getElementById("ingresoProductSearchStatus");
        var productResults = document.getElementById("ingresoProductResults");

        function closeProductSearchModal() {
            if (!productModal) return;
            productModal.classList.remove("open");
            productModal.setAttribute("aria-hidden", "true");
        }

        function applyProductToRow(row, item) {
            if (!row || !item) return;
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            var bodegaSel = row.querySelector("select.ingreso-bodega-select");
            if (codeInput) {
                codeInput.value = (item.codigo || "").toString().trim().toUpperCase();
                setCodigoInternoValidState(row, true, "");
            }
            if (marcaInput && (item.marca || "").trim()) {
                marcaInput.value = (item.marca || "").trim().toUpperCase();
            }
            if (bodegaSel && (item.bodega || "").trim()) {
                var wanted = (item.bodega || "").trim();
                for (var i = 0; i < bodegaSel.options.length; i += 1) {
                    if (bodegaSel.options[i].value === wanted) {
                        bodegaSel.selectedIndex = i;
                        break;
                    }
                }
            }
            scheduleMarcasFetch(row);
        }

        function renderProductSearchResults(items) {
            mountProductSearchResults(productResults, items, function (it) {
                applyProductToRow(currentProductSearchRow, it);
                closeProductSearchModal();
            });
        }

        function doProductSearch() {
            if (!productSearchUrl || !productSearchInput) return;
            var q = (productSearchInput.value || "").trim();
            if (q.length < 2) {
                if (productSearchStatus) productSearchStatus.textContent = "Escribe al menos 2 caracteres.";
                renderProductSearchResults([]);
                return;
            }
            if (productSearchStatus) productSearchStatus.textContent = "Buscando...";
            fetch(productSearchUrl + "?q=" + encodeURIComponent(q) + "&limit=80", {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    var items = (data && data.success && Array.isArray(data.items)) ? data.items : [];
                    if (productSearchStatus) productSearchStatus.textContent = items.length + " resultado(s).";
                    renderProductSearchResults(items);
                })
                .catch(function () {
                    if (productSearchStatus) productSearchStatus.textContent = "No se pudo buscar productos.";
                    renderProductSearchResults([]);
                });
        }

        function openProductSearchModal(row) {
            if (!productModal) return;
            currentProductSearchRow = row || null;
            productModal.classList.add("open");
            productModal.setAttribute("aria-hidden", "false");
            if (productSearchStatus) productSearchStatus.textContent = "Escribe para buscar productos.";
            if (productResults) productResults.innerHTML = "";
            if (productSearchInput) {
                productSearchInput.value = "";
                setTimeout(function () { productSearchInput.focus(); }, 20);
            }
        }

        if (productModalClose) {
            productModalClose.addEventListener("click", closeProductSearchModal);
        }
        if (productModal) {
            productModal.addEventListener("click", function (ev) {
                if (ev.target === productModal) closeProductSearchModal();
            });
        }
        if (productSearchBtn) {
            productSearchBtn.addEventListener("click", doProductSearch);
        }
        if (productSearchInput) {
            productSearchInput.addEventListener("keydown", function (ev) {
                if (ev.key === "Enter") {
                    ev.preventDefault();
                    doProductSearch();
                }
                if (ev.key === "Escape") {
                    closeProductSearchModal();
                }
            });
        }

        function bindRemove(button) {
            if (!button || button.dataset.bound === "1") {
                return;
            }
            button.dataset.bound = "1";
            button.addEventListener("click", function () {
                var rows = itemsBody.querySelectorAll(".item-row");
                if (rows.length <= 1) {
                    var only = rows[0];
                    only.querySelectorAll("input.ingreso-codigo-proveedor-input").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input[name='codigo_producto[]']").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input.ingreso-marca-input").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input[name='cantidad_producto[]']").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input[name='valor_neto_producto[]']").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input[name='margen_pct_producto[]']").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input[name='precio_venta_neto_producto[]']").forEach(function (inp) {
                        inp.value = "";
                    });
                    only.querySelectorAll("input[name='nota_producto[]']").forEach(function (inp) {
                        inp.value = "";
                    });
                    var bsel = only.querySelector("select.ingreso-bodega-select");
                    if (bsel) {
                        for (var bi = 0; bi < bsel.options.length; bi += 1) {
                            if (bsel.options[bi].value === defaultBodega) {
                                bsel.selectedIndex = bi;
                                break;
                            }
                        }
                    }
                    clearMarcaSuggestions(only);
                    updateIngresoTotals();
                    refreshIngresoItemNumbers();
                    return;
                }
                var row = button.closest("tr");
                if (row) {
                    row.remove();
                }
                refreshIngresoItemNumbers();
                updateIngresoTotals();
            });
        }

        function fillSupplier(p) {
            p = p || {};
            var sn = form.querySelector("#supplier_name");
            if (sn) sn.value = p.name || "";
            var sct = form.querySelector("#supplier_contact");
            if (sct) sct.value = p.contact || "";
            var sg = form.querySelector("#supplier_giro");
            if (sg) sg.value = p.giro || "";
            var se = form.querySelector("#supplier_email");
            if (se) se.value = p.email || "";
            var st = form.querySelector("#supplier_telefono");
            if (st) st.value = p.telefono || "";
            var sa = form.querySelector("#supplier_address");
            if (sa) sa.value = p.address || "";
            var sci = form.querySelector("#supplier_ciudad");
            if (sci) sci.value = p.ciudad || "";
            var sc = form.querySelector("#supplier_country");
            if (sc) sc.value = p.country || "Chile";
            var regSel = form.querySelector("#ingresoSupplierRegion");
            var comSel = form.querySelector("#ingresoSupplierComuna");
            if (regSel) regSel.value = p.region || "";
            if (ingresoComunasPopulateFn && regSel) {
                ingresoComunasPopulateFn(regSel.value || "", p.comuna || "");
            } else if (comSel) {
                comSel.value = p.comuna || "";
            }
            var comEl = form.querySelector("#ingresoSupplierComuna");
            var ciEl = form.querySelector("#supplier_ciudad");
            if (ciEl && (!p.ciudad || !String(p.ciudad).trim()) && comEl && comEl.value && regSel && regSel.value) {
                var cv = (sc && String(sc.value).trim().toLowerCase()) || "";
                var ch = cv === "chile" || cv === "cl" || !cv;
                if (ch) {
                    var metro = String(regSel.value).toLowerCase().indexOf("metropolitana") !== -1;
                    ciEl.value = metro ? "Santiago" : comEl.value;
                }
            }
            syncOrigenSelectsFromSupplier();
        }

        initIngresoSupplierChileGeo();

        function searchSupplier(force) {
            var rut = (window.RutUtils && window.RutUtils.clean)
                ? window.RutUtils.clean(rutInput.value)
                : rutInput.value;
            rutInput.value = (window.RutUtils && window.RutUtils.format)
                ? window.RutUtils.format(rut)
                : rut;
            if (!rut) {
                rutStatus.textContent = "Ingresa un RUT valido para buscar proveedor.";
                rutStatus.style.color = "#64748b";
                return;
            }
            if (window.RutUtils && window.RutUtils.isValid && !window.RutUtils.isValid(rut)) {
                rutStatus.textContent = "RUT invalido. Verifica digito verificador.";
                rutStatus.style.color = "#b91c1c";
                return;
            }
            if (!supplierSearchUrl) return;
            if (!force && (isLookingUp || rut === lastLookupRut)) return;
            isLookingUp = true;
            lastLookupRut = rut;

            fetch(supplierSearchUrl + "?rut=" + encodeURIComponent(rut), {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) {
                    return res.json().then(function (data) {
                        if (!res.ok) throw new Error(data.message || "No se pudo buscar proveedor");
                        return data;
                    });
                })
                .then(function (data) {
                    if (data.found && data.proveedor) {
                        fillSupplier(data.proveedor);
                        updateSupplierSummary();
                        syncSupplierRutValidity();
                        collapseSupplierSection();
                        rutInput.readOnly = false;
                        rutInput.disabled = false;
                        rutStatus.textContent = "Proveedor encontrado y autocompletado.";
                        rutStatus.style.color = "#166534";
                        setTimeout(function () {
                            var firstCodeInput = form.querySelector("input[name='codigo_producto[]']");
                            if (firstCodeInput) firstCodeInput.focus();
                        }, 20);
                    } else {
                        fillSupplier({
                            name: "",
                            contact: "",
                            giro: "",
                            email: "",
                            telefono: "",
                            address: "",
                            comuna: "",
                            region: "",
                            ciudad: "",
                            country: "Chile"
                        });
                        expandSupplierSection();
                        rutInput.readOnly = false;
                        rutInput.disabled = false;
                        rutStatus.textContent = "RUT no encontrado. Completa los datos para crear proveedor en linea al guardar.";
                        rutStatus.style.color = "#92400e";
                    }
                })
                .catch(function (err) {
                    expandSupplierSection();
                    rutInput.readOnly = false;
                    rutInput.disabled = false;
                    rutStatus.textContent = err.message || "Error en busqueda de proveedor.";
                    rutStatus.style.color = "#b91c1c";
                })
                .finally(function () {
                    isLookingUp = false;
                });
        }

        function scheduleAutoLookup() {
            if (lookupTimer) clearTimeout(lookupTimer);
            lookupTimer = setTimeout(function () {
                var rut = (window.RutUtils && window.RutUtils.clean)
                    ? window.RutUtils.clean(rutInput.value)
                    : rutInput.value;
                if (!rut || rut.length < 8) return;
                if (window.RutUtils && window.RutUtils.isValid && !window.RutUtils.isValid(rut)) return;
                searchSupplier(false);
            }, 320);
        }

        btnEditarProveedor.addEventListener("click", function () {
            expandSupplierSection();
            rutInput.focus();
        });

        var btnGuardarProveedor = form.querySelector("#btnGuardarProveedorIngreso");
        if (btnGuardarProveedor) {
            var guardarProveedorUrl = btnGuardarProveedor.getAttribute("data-guardar-url") || "";
            btnGuardarProveedor.addEventListener("click", function () {
                if (!guardarProveedorUrl) {
                    return;
                }
                var rut = (window.RutUtils && window.RutUtils.clean)
                    ? window.RutUtils.clean(rutInput.value)
                    : (rutInput.value || "").trim();
                if (window.RutUtils && window.RutUtils.format) {
                    rutInput.value = window.RutUtils.format(rut);
                }
                if (!rut || (window.RutUtils && window.RutUtils.isValid && !window.RutUtils.isValid(rut))) {
                    rutStatus.textContent = "Ingresa un RUT valido antes de guardar el proveedor.";
                    rutStatus.style.color = "#b91c1c";
                    return;
                }
                var regEl = form.querySelector("#ingresoSupplierRegion");
                var comEl = form.querySelector("#ingresoSupplierComuna");
                var payload = {
                    rut: rut,
                    name: (form.querySelector("#supplier_name") && form.querySelector("#supplier_name").value) || "",
                    contact: (form.querySelector("#supplier_contact") && form.querySelector("#supplier_contact").value) || "",
                    giro: (form.querySelector("#supplier_giro") && form.querySelector("#supplier_giro").value) || "",
                    email: (form.querySelector("#supplier_email") && form.querySelector("#supplier_email").value) || "",
                    telefono: (form.querySelector("#supplier_telefono") && form.querySelector("#supplier_telefono").value) || "",
                    address: (form.querySelector("#supplier_address") && form.querySelector("#supplier_address").value) || "",
                    comuna: (comEl && comEl.value) || "",
                    region: (regEl && regEl.value) || "",
                    ciudad: (form.querySelector("#supplier_ciudad") && form.querySelector("#supplier_ciudad").value) || "",
                    country: (form.querySelector("#supplier_country") && form.querySelector("#supplier_country").value) || "Chile"
                };
                btnGuardarProveedor.disabled = true;
                fetch(guardarProveedorUrl, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-Requested-With": "XMLHttpRequest"
                    },
                    credentials: "same-origin",
                    body: JSON.stringify(payload)
                })
                    .then(function (res) {
                        return res.json().then(function (data) {
                            if (!res.ok) {
                                throw new Error((data && data.message) || "No se pudo guardar el proveedor.");
                            }
                            return data;
                        });
                    })
                    .then(function (data) {
                        if (!data || !data.ok || !data.proveedor) {
                            throw new Error("Respuesta invalida del servidor.");
                        }
                        var p = data.proveedor;
                        fillSupplier({
                            name: p.name,
                            contact: p.contact || "",
                            giro: p.giro,
                            email: p.email,
                            telefono: p.telefono,
                            address: p.address,
                            comuna: p.comuna,
                            region: p.region,
                            ciudad: p.ciudad || "",
                            country: p.country || "Chile"
                        });
                        lastLookupRut = rut;
                        updateSupplierSummary();
                        collapseSupplierSection();
                        rutStatus.textContent = "Proveedor guardado. Podés continuar con el ingreso.";
                        rutStatus.style.color = "#166534";
                        setTimeout(function () {
                            var firstCodeInput = form.querySelector("input[name='codigo_producto[]']");
                            if (firstCodeInput) {
                                firstCodeInput.focus();
                            }
                        }, 20);
                    })
                    .catch(function (err) {
                        rutStatus.textContent = err.message || "No se pudo guardar el proveedor.";
                        rutStatus.style.color = "#b91c1c";
                    })
                    .finally(function () {
                        btnGuardarProveedor.disabled = false;
                    });
            });
        }

        rutInput.addEventListener("input", function () {
            rutInput.readOnly = false;
            rutInput.disabled = false;
            lastLookupRut = "";
            hideSupplierRegistrationCard();
            updateSupplierSummary();
            rutStatus.style.color = "#64748b";
            rutStatus.textContent = "Ingresa el RUT para autocompletar proveedor.";
            scheduleAutoLookup();
        });
        rutInput.addEventListener("blur", function () {
            scheduleAutoLookup();
        });

        function setNumeroDocStatus(msg, isError) {
            if (!numeroDocStatus) return;
            if (!msg) {
                numeroDocStatus.hidden = true;
                numeroDocStatus.textContent = "";
                numeroDocStatus.classList.remove("is-error");
                if (numeroDocInput) numeroDocInput.classList.remove("ingreso-numero-doc-duplicado");
                return;
            }
            numeroDocStatus.hidden = false;
            numeroDocStatus.textContent = msg;
            numeroDocStatus.classList.toggle("is-error", !!isError);
            if (numeroDocInput) numeroDocInput.classList.toggle("ingreso-numero-doc-duplicado", !!isError);
        }

        function checkNumeroDocumentoDuplicado() {
            if (!numeroDuplicadoUrl || !numeroDocInput || !rutInput) {
                return Promise.resolve(false);
            }
            var rut = (rutInput.value || "").trim();
            var numero = (numeroDocInput.value || "").trim();
            if (!rut || !numero) {
                setNumeroDocStatus("", false);
                return Promise.resolve(false);
            }
            var url = numeroDuplicadoUrl
                + "?rut=" + encodeURIComponent(rut)
                + "&numero=" + encodeURIComponent(numero);
            return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
                .then(function (response) { return response.json(); })
                .then(function (data) {
                    if (data && data.duplicado) {
                        setNumeroDocStatus(data.message || "Este N° de documento ya está ingresado.", true);
                        return true;
                    }
                    setNumeroDocStatus("", false);
                    return false;
                })
                .catch(function () {
                    setNumeroDocStatus("", false);
                    return false;
                });
        }

        function scheduleNumeroDocCheck() {
            if (numeroDocCheckTimer) window.clearTimeout(numeroDocCheckTimer);
            numeroDocCheckTimer = window.setTimeout(checkNumeroDocumentoDuplicado, 450);
        }

        if (numeroDocInput) {
            numeroDocInput.addEventListener("input", scheduleNumeroDocCheck);
            numeroDocInput.addEventListener("blur", checkNumeroDocumentoDuplicado);
        }
        if (rutInput) {
            rutInput.addEventListener("change", scheduleNumeroDocCheck);
            rutInput.addEventListener("blur", scheduleNumeroDocCheck);
        }

        var btnGuardarIngreso = form.querySelector("#btnGuardarIngreso")
            || form.querySelector('button[type="submit"]');
        var btnGuardarIngresoLabel = btnGuardarIngreso
            ? (btnGuardarIngreso.textContent || "Guardar ingreso").trim()
            : "Guardar ingreso";
        var ingresoSubmitInFlight = false;

        function setIngresoSubmitBusy(busy) {
            if (!btnGuardarIngreso) {
                return;
            }
            btnGuardarIngreso.disabled = !!busy;
            btnGuardarIngreso.textContent = busy ? "Guardando…" : btnGuardarIngresoLabel;
        }

        function releaseIngresoSubmit() {
            ingresoSubmitInFlight = false;
            setIngresoSubmitBusy(false);
        }

        function proceedIngresoSubmit() {
            var rows = itemsBody.querySelectorAll(".item-row");
            var promises = [];
            rows.forEach(function (row) {
                var ci = row.querySelector("input[name='codigo_producto[]']");
                var codigo = (ci && ci.value || "").trim();
                if (!codigo) {
                    return;
                }
                promises.push(validateCodigoInterno(row));
            });
            if (promises.length === 0) {
                syncValorNetoInputsForSubmit();
                submitBodegaFormPost(form, btnGuardarIngreso, function () {
                    releaseIngresoSubmit();
                });
                return;
            }
            Promise.all(promises).then(function (results) {
                var ok = results.every(function (r) {
                    return r !== false;
                });
                if (!ok) {
                    releaseIngresoSubmit();
                    window.alert(
                        "Hay códigos internos que no existen en el catálogo o están inactivos. Revisá los campos en rojo."
                    );
                    var firstBad = itemsBody.querySelector("input.ingreso-codigo-interno-invalido");
                    if (firstBad) {
                        firstBad.focus();
                    }
                    return;
                }
                syncValorNetoInputsForSubmit();
                submitBodegaFormPost(form, btnGuardarIngreso, function () {
                    releaseIngresoSubmit();
                });
            });
        }

        form.addEventListener("submit", function (ev) {
            if (!marcasUrl) {
                return;
            }
            ev.preventDefault();
            if (ingresoSubmitInFlight) {
                return;
            }
            ingresoSubmitInFlight = true;
            setIngresoSubmitBusy(true);
            var vpCheck = validateMargenYPrecioVentaIngresoRows();
            if (!vpCheck.ok) {
                releaseIngresoSubmit();
                window.alert(vpCheck.message);
                if (vpCheck.focusEl) {
                    vpCheck.focusEl.focus();
                }
                return;
            }
            checkNumeroDocumentoDuplicado().then(function (isDup) {
                if (isDup) {
                    releaseIngresoSubmit();
                    window.alert(
                        (numeroDocStatus && numeroDocStatus.textContent)
                            || "Este N° de documento ya está ingresado para este proveedor."
                    );
                    if (numeroDocInput) numeroDocInput.focus();
                    return;
                }
                proceedIngresoSubmit();
            });
        });

        itemsBody.querySelectorAll("input[name='valor_neto_producto[]']").forEach(bindValorNetoIngresoInput);

        addRowBtn.addEventListener("click", addRow);
        btnBuscar.addEventListener("click", function () {
            searchSupplier(true);
        });
        rutInput.addEventListener("keydown", function (event) {
            if (event.key === "Enter") {
                event.preventDefault();
                searchSupplier(true);
            }
        });
        itemsBody.querySelectorAll(".btnRemove").forEach(bindRemove);
        itemsBody.querySelectorAll(".item-row").forEach(bindIngresoProductRow);
        refreshIngresoItemNumbers();
        syncOrigenSelectsFromSupplier();

        if (form.dataset.marcaMenuCloseBound !== "1") {
            form.dataset.marcaMenuCloseBound = "1";
            document.addEventListener("click", function (ev) {
                if (ev.target.closest(".ingreso-marca-dropdown-btn")) {
                    return;
                }
                if (ev.target.closest(".ingreso-marca-menu")) {
                    return;
                }
                if (ev.target.closest(".ingreso-marca-input")) {
                    return;
                }
                closeAllMarcaMenus();
            });
            document.addEventListener("keydown", function (ev) {
                if (ev.key === "Escape") {
                    closeAllMarcaMenus();
                }
            });
            window.addEventListener(
                "resize",
                function () {
                    itemsBody.querySelectorAll("tr.item-row").forEach(function (row) {
                        var menu = row.querySelector(".ingreso-marca-menu");
                        if (menu && !menu.hidden) {
                            positionMarcaMenu(row);
                        }
                    });
                },
                { passive: true }
            );
            var itemsTableWrap = form.querySelector(".ingreso-items-table-wrap");
            if (itemsTableWrap) {
                itemsTableWrap.addEventListener(
                    "scroll",
                    function () {
                        itemsBody.querySelectorAll("tr.item-row").forEach(function (row) {
                            var menu = row.querySelector(".ingreso-marca-menu");
                            if (menu && !menu.hidden) {
                                positionMarcaMenu(row);
                            }
                        });
                    },
                    { passive: true }
                );
            }
        }

        if (supplierCountryInput) {
            supplierCountryInput.addEventListener("change", syncOrigenSelectsFromSupplier);
            supplierCountryInput.addEventListener("input", syncOrigenSelectsFromSupplier);
        }

        function ingresoTotalsFromEvent(ev) {
            var t = ev.target;
            if (
                t &&
                (t.name === "valor_neto_producto[]" ||
                    t.name === "cantidad_producto[]")
            ) {
                updateIngresoTotals();
            }
        }
        itemsBody.addEventListener("input", ingresoTotalsFromEvent);
        itemsBody.addEventListener("change", ingresoTotalsFromEvent);
        if (inpTotalFactura) {
            inpTotalFactura.addEventListener("input", updateIngresoTotals);
            inpTotalFactura.addEventListener("change", updateIngresoTotals);
        }

        var observer = new MutationObserver(function () {
            itemsBody.querySelectorAll(".btnRemove").forEach(bindRemove);
            itemsBody.querySelectorAll(".item-row").forEach(bindIngresoProductRow);
            refreshIngresoItemNumbers();
            updateIngresoTotals();
        });
        observer.observe(itemsBody, { childList: true, subtree: false });

        updateIngresoTotals();
        refreshIngresoItemNumbers();

        var helpBtn = form.querySelector("#ingresoHelpBtn");
        var helpPanel = form.querySelector("#ingresoHelpPanel");
        var helpClose = form.querySelector("#ingresoHelpClose");
        function closeIngresoHelp() {
            if (!helpPanel) return;
            helpPanel.hidden = true;
            if (helpBtn) helpBtn.setAttribute("aria-expanded", "false");
        }
        function openIngresoHelp() {
            if (!helpPanel) return;
            helpPanel.hidden = false;
            if (helpBtn) helpBtn.setAttribute("aria-expanded", "true");
        }
        function toggleIngresoHelp() {
            if (!helpPanel) return;
            if (helpPanel.hidden) openIngresoHelp(); else closeIngresoHelp();
        }
        if (helpBtn && helpPanel) {
            helpBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                toggleIngresoHelp();
            });
            if (helpClose) helpClose.addEventListener("click", closeIngresoHelp);
            document.addEventListener("click", function (ev) {
                if (!helpPanel || helpPanel.hidden) return;
                var t = ev.target;
                if (helpBtn.contains(t) || helpPanel.contains(t)) return;
                closeIngresoHelp();
            });
            document.addEventListener("keydown", function (ev) {
                if (ev.key === "Escape") closeIngresoHelp();
            });
        }

        var facturaAutoBadge = document.getElementById("ingresoFacturaAutoBadge");

        function hideFacturaAutoBadge() {
            if (!facturaAutoBadge) return;
            facturaAutoBadge.hidden = true;
            facturaAutoBadge.style.display = "none";
            facturaAutoBadge.textContent = "";
        }

        function showFacturaAutoBadge(fieldCount) {
            if (!facturaAutoBadge || !fieldCount || fieldCount <= 0) {
                hideFacturaAutoBadge();
                return;
            }
            facturaAutoBadge.hidden = false;
            facturaAutoBadge.style.display = "";
            facturaAutoBadge.textContent =
                "✅ " + fieldCount + " campo(s) completado(s) automáticamente";
        }

        form.addEventListener("reset", hideFacturaAutoBadge);
        hideFacturaAutoBadge();

        (function initIngresoFacturaScan() {
            var btnScan = document.getElementById("btnFacturaScan");
            var fileInput = document.getElementById("ingresoFacturaFileInput");
            var modal = document.getElementById("ingresoFacturaScanModal");
            if (!btnScan || !fileInput || !modal) return;

            var analizarUrl =
                (btnScan.getAttribute("data-analizar-url") || form.getAttribute("data-analizar-factura-url") || "").trim();
            var modalClose = document.getElementById("ingresoFacturaScanClose");
            var btnCancel = document.getElementById("ingresoFacturaCancelBtn");
            var btnAnalyze = document.getElementById("ingresoFacturaAnalyzeBtn");
            var btnApply = document.getElementById("ingresoFacturaApplyBtn");
            var previewImg = document.getElementById("ingresoFacturaPreviewImg");
            var previewPdfRendered = document.getElementById(
                "ingresoFacturaPreviewPdfRendered"
            );
            var previewPdfCanvas = document.getElementById("ingresoFacturaPreviewPdfCanvas");
            var previewPdfRenderedName = document.getElementById(
                "ingresoFacturaPreviewPdfRenderedName"
            );
            var previewPdf = document.getElementById("ingresoFacturaPreviewPdf");
            var previewPdfName = document.getElementById("ingresoFacturaPreviewPdfName");
            var localPreviewObjectUrl = null;
            var pdfJsModulePromise = null;
            var PDFJS_MODULE_URL =
                "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.min.mjs";
            var PDFJS_WORKER_URL =
                "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.0.379/pdf.worker.min.mjs";
            var statusEl = document.getElementById("ingresoFacturaScanStatus");
            var previewWrap = modal.querySelector(".ingreso-factura-preview-wrap");
            var extractedBox = document.getElementById("ingresoFacturaExtracted");
            var extractedList = document.getElementById("ingresoFacturaExtractedList");
            var extractedProductsBox = document.getElementById("ingresoFacturaExtractedProducts");
            var extractedProductsTitle = document.getElementById("ingresoFacturaExtractedProductsTitle");
            var extractedProductsBody = document.getElementById("ingresoFacturaExtractedProductsBody");
            var numeroDocInput = form.querySelector("#numero_documento");
            var fechaInput = form.querySelector("#fecha_documento");
            var metodoPagoSel = form.querySelector("#ingresoMetodoPago");

            var pendingFile = null;
            var pendingBase64 = "";
            var pendingMediaType = "image/jpeg";
            var extractedData = null;

            function getCsrfToken() {
                var meta = document.querySelector('meta[name="csrf-token"]');
                return meta && meta.content ? String(meta.content).trim() : "";
            }

            function openModal() {
                hideFacturaAutoBadge();
                modal.classList.add("open");
                modal.setAttribute("aria-hidden", "false");
            }

            function closeModal() {
                modal.classList.remove("open");
                modal.setAttribute("aria-hidden", "true");
            }

            function setPreviewWrapVisible(visible) {
                if (!previewWrap) return;
                previewWrap.hidden = !visible;
            }

            function setStatus(msg, kind) {
                if (!statusEl) return;
                if (!msg) {
                    statusEl.hidden = true;
                    statusEl.textContent = "";
                    statusEl.classList.remove("is-loading", "is-error", "muted");
                    return;
                }
                statusEl.hidden = false;
                statusEl.textContent = msg;
                statusEl.classList.remove("is-loading", "is-error", "muted");
                if (kind === "loading") statusEl.classList.add("is-loading");
                else if (kind === "error") statusEl.classList.add("is-error");
                else statusEl.classList.add("muted");
            }

            function revokeLocalPreviewUrl() {
                if (!localPreviewObjectUrl) return;
                try {
                    URL.revokeObjectURL(localPreviewObjectUrl);
                } catch (e) {}
                localPreviewObjectUrl = null;
            }

            var facturaPreviewImgAlt = "Vista previa de la factura";

            function hidePreviewImg() {
                if (!previewImg) return;
                previewImg.hidden = true;
                previewImg.removeAttribute("src");
                previewImg.alt = "";
                previewImg.style.display = "none";
            }

            function showPreviewImg(src) {
                if (!previewImg || !src) return;
                previewImg.src = src;
                previewImg.alt = facturaPreviewImgAlt;
                previewImg.hidden = false;
                previewImg.style.display = "block";
            }

            function hidePreviewPdf() {
                if (!previewPdf) return;
                previewPdf.hidden = true;
                previewPdf.style.display = "none";
            }

            function showPreviewPdf(file) {
                if (!previewPdf) return;
                previewPdf.hidden = false;
                previewPdf.style.display = "flex";
                if (previewPdfName) {
                    previewPdfName.textContent =
                        (file && file.name) || "documento.pdf";
                }
            }

            function setPdfRenderedFileName(file) {
                var name = (file && file.name) || "documento.pdf";
                if (previewPdfRenderedName) {
                    previewPdfRenderedName.textContent = name;
                }
            }

            function hidePreviewPdfCanvas() {
                if (previewPdfRendered) {
                    previewPdfRendered.hidden = true;
                    previewPdfRendered.style.display = "none";
                }
                if (previewPdfCanvas) {
                    try {
                        var ctx = previewPdfCanvas.getContext("2d");
                        if (ctx) {
                            ctx.clearRect(
                                0,
                                0,
                                previewPdfCanvas.width,
                                previewPdfCanvas.height
                            );
                        }
                    } catch (e) {}
                }
            }

            function showPreviewPdfCanvas(file) {
                if (!previewPdfCanvas || !previewPdfRendered) return;
                hidePreviewPdf();
                hidePreviewImg();
                setPdfRenderedFileName(file || pendingFile);
                previewPdfRendered.hidden = false;
                previewPdfRendered.style.display = "";
            }

            function loadPdfJs() {
                if (pdfJsModulePromise) return pdfJsModulePromise;
                pdfJsModulePromise = import(PDFJS_MODULE_URL)
                    .then(function (pdfjsLib) {
                        pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER_URL;
                        return pdfjsLib;
                    })
                    .catch(function (err) {
                        pdfJsModulePromise = null;
                        throw err;
                    });
                return pdfJsModulePromise;
            }

            function preparePdfPreviewForMeasure() {
                setPreviewWrapVisible(true);
                hidePreviewImg();
                hidePreviewPdf();
                if (previewPdfRendered) {
                    previewPdfRendered.hidden = false;
                    previewPdfRendered.style.display = "";
                }
            }

            function measurePdfPreviewContainerWidth() {
                var canvas = previewPdfCanvas;
                var parent = canvas && canvas.parentElement;
                var containerWidth = parent ? parent.clientWidth : 0;
                if (containerWidth > 0) return containerWidth;
                if (previewWrap && previewWrap.clientWidth > 0) {
                    return Math.max(120, previewWrap.clientWidth - 24);
                }
                return 440;
            }

            function renderPdfFirstPageLocal(file) {
                if (!previewPdfCanvas || !file) return;
                loadPdfJs()
                    .then(function (pdfjsLib) {
                        return file.arrayBuffer().then(function (buf) {
                            return pdfjsLib.getDocument({ data: buf }).promise;
                        });
                    })
                    .then(function (pdf) {
                        return pdf.getPage(1);
                    })
                    .then(function (page) {
                        if (pendingFile !== file) return;
                        preparePdfPreviewForMeasure();
                        var canvas = previewPdfCanvas;
                        var parent = canvas.parentElement;
                        var containerWidth = parent
                            ? parent.clientWidth
                            : measurePdfPreviewContainerWidth();
                        if (containerWidth <= 0) {
                            containerWidth = measurePdfPreviewContainerWidth();
                        }
                        var baseViewport = page.getViewport({ scale: 1 });
                        var baseScale = containerWidth / baseViewport.width;
                        if (!isFinite(baseScale) || baseScale <= 0) {
                            baseScale = 1.5;
                        }
                        baseScale = Math.min(baseScale, 3);
                        var renderScale = baseScale * 2;
                        var viewport = page.getViewport({ scale: renderScale });
                        var ctx = canvas.getContext("2d");
                        canvas.width = Math.floor(viewport.width);
                        canvas.height = Math.floor(viewport.height);
                        return page
                            .render({ canvasContext: ctx, viewport: viewport })
                            .promise.then(function () {
                                if (pendingFile !== file) return;
                                showPreviewPdfCanvas(file);
                                setPreviewWrapVisible(true);
                            });
                    })
                    .catch(function () {
                        if (pendingFile !== file) return;
                        hidePreviewPdfCanvas();
                        showPreviewPdf(file);
                    });
            }

            function hideAllPreviewLayers() {
                hidePreviewImg();
                hidePreviewPdf();
                hidePreviewPdfCanvas();
            }

            function isFacturaPdfFile(file) {
                var name = String((file && file.name) || "").toLowerCase();
                var t = String((file && file.type) || "").toLowerCase();
                return t === "application/pdf" || /\.pdf$/i.test(name);
            }

            function isFacturaImageFile(file) {
                if (isFacturaPdfFile(file)) return false;
                var t = String((file && file.type) || "").toLowerCase();
                var name = String((file && file.name) || "").toLowerCase();
                if (/^image\//i.test(t)) return true;
                return /\.(jpe?g|png|webp)$/i.test(name);
            }

            function guessFacturaMediaType(file) {
                if (isFacturaPdfFile(file)) return "application/pdf";
                var name = String((file && file.name) || "").toLowerCase();
                var t = String((file && file.type) || "").toLowerCase();
                if (t === "image/jpg") t = "image/jpeg";
                if (/\.png$/i.test(name) || t === "image/png") return "image/png";
                if (/\.webp$/i.test(name) || t === "image/webp") return "image/webp";
                if (/\.(jpe?g)$/i.test(name) || /^image\/jpe?g$/i.test(t)) {
                    return "image/jpeg";
                }
                if (/^image\//i.test(t)) return t;
                return "image/jpeg";
            }

            function showPdfPlaceholder(file) {
                revokeLocalPreviewUrl();
                if (previewImg) {
                    previewImg.src = "";
                    previewImg.hidden = true;
                    previewImg.style.display = "none";
                    previewImg.alt = "";
                }
                hidePreviewPdfCanvas();
                showPreviewPdf(file);
                renderPdfFirstPageLocal(file);
            }

            function showLocalImagePreview(file) {
                revokeLocalPreviewUrl();
                hidePreviewPdfCanvas();
                if (previewPdf) {
                    previewPdf.hidden = true;
                    previewPdf.style.display = "none";
                }
                if (!previewImg) return;
                try {
                    localPreviewObjectUrl = URL.createObjectURL(file);
                    previewImg.src = localPreviewObjectUrl;
                    previewImg.alt = facturaPreviewImgAlt;
                    previewImg.hidden = false;
                    previewImg.style.display = "";
                } catch (e) {
                    var reader = new FileReader();
                    reader.onload = function () {
                        if (pendingFile !== file || !previewImg) return;
                        if (previewPdf) {
                            previewPdf.hidden = true;
                            previewPdf.style.display = "none";
                        }
                        previewImg.src = String(reader.result || "");
                        previewImg.alt = facturaPreviewImgAlt;
                        previewImg.hidden = false;
                        previewImg.style.display = "";
                        setPreviewWrapVisible(true);
                    };
                    reader.onerror = function () {
                        hidePreviewImg();
                    };
                    reader.readAsDataURL(file);
                }
            }

            function restoreLocalFilePreview() {
                if (!pendingFile) return;
                setPreviewWrapVisible(true);
                if (isFacturaPdfFile(pendingFile)) {
                    showPdfPlaceholder(pendingFile);
                    return;
                }
                if (isFacturaImageFile(pendingFile)) {
                    showLocalImagePreview(pendingFile);
                }
            }

            function applyBackendPreviewImage(src) {
                if (!src || !previewImg) return;
                setPreviewWrapVisible(true);
                previewImg.onload = function () {
                    previewImg.onload = null;
                    previewImg.onerror = null;
                    revokeLocalPreviewUrl();
                    hidePreviewPdf();
                    hidePreviewPdfCanvas();
                    previewImg.hidden = false;
                    previewImg.style.display = "block";
                    setPreviewWrapVisible(true);
                };
                previewImg.onerror = function () {
                    previewImg.onerror = null;
                    previewImg.onload = null;
                    restoreLocalFilePreview();
                };
                previewImg.src = src;
                previewImg.alt = facturaPreviewImgAlt;
            }

            function ensurePreviewVisibleAfterAnalyze() {
                setPreviewWrapVisible(true);
                if (previewImg && previewImg.src && !previewImg.hidden) {
                    return;
                }
                if (
                    previewPdfRendered &&
                    !previewPdfRendered.hidden &&
                    previewPdfCanvas &&
                    previewPdfCanvas.width > 0
                ) {
                    return;
                }
                if (previewPdf && !previewPdf.hidden) {
                    return;
                }
                restoreLocalFilePreview();
            }

            function clearExtractedResults() {
                if (extractedBox) extractedBox.hidden = true;
                if (extractedList) extractedList.innerHTML = "";
                if (extractedProductsBox) extractedProductsBox.hidden = true;
                if (extractedProductsBody) extractedProductsBody.innerHTML = "";
                if (extractedProductsTitle) {
                    extractedProductsTitle.textContent = "Productos detectados";
                }
                if (btnApply) btnApply.disabled = true;
                extractedData = null;
            }

            function resetPreview() {
                revokeLocalPreviewUrl();
                hideAllPreviewLayers();
                setPreviewWrapVisible(false);
                clearExtractedResults();
            }

            function showFilePreview(file) {
                clearExtractedResults();
                pendingFile = file;
                pendingBase64 = "";

                if (isFacturaPdfFile(file)) {
                    pendingMediaType = "application/pdf";
                    showPdfPlaceholder(file);
                } else if (isFacturaImageFile(file)) {
                    pendingMediaType = guessFacturaMediaType(file);
                    showLocalImagePreview(file);
                } else {
                    pendingMediaType = guessFacturaMediaType(file);
                    showPdfPlaceholder(file);
                }

                setPreviewWrapVisible(true);
                openModal();
                if (btnAnalyze) btnAnalyze.disabled = false;
                setStatus("Archivo listo. Presioná «Analizar factura».", null);
            }

            function readFileAsBase64(file) {
                return new Promise(function (resolve, reject) {
                    var reader = new FileReader();
                    reader.onload = function () {
                        var result = String(reader.result || "");
                        var idx = result.indexOf(",");
                        resolve(idx >= 0 ? result.slice(idx + 1) : result);
                    };
                    reader.onerror = function () {
                        reject(new Error("No se pudo leer el archivo"));
                    };
                    reader.readAsDataURL(file);
                });
            }

            function dlRow(label, value) {
                if (value === null || value === undefined || value === "") return "";
                var dt = document.createElement("dt");
                dt.textContent = label;
                var dd = document.createElement("dd");
                dd.appendChild(document.createTextNode(String(value)));
                var tag = document.createElement("span");
                tag.className = "ingreso-factura-auto-tag";
                tag.textContent = "Auto";
                dd.appendChild(document.createTextNode(" "));
                dd.appendChild(tag);
                extractedList.appendChild(dt);
                extractedList.appendChild(dd);
            }

            function dlRowFactura(label, value, missingText) {
                var dt = document.createElement("dt");
                dt.textContent = label;
                var dd = document.createElement("dd");
                if (value === null || value === undefined || value === "") {
                    dd.className = "ingreso-factura-missing";
                    dd.textContent =
                        missingText || "No detectada — revisá en el documento";
                } else {
                    dd.appendChild(document.createTextNode(String(value)));
                    var tag = document.createElement("span");
                    tag.className = "ingreso-factura-auto-tag";
                    tag.textContent = "Auto";
                    dd.appendChild(document.createTextNode(" "));
                    dd.appendChild(tag);
                }
                extractedList.appendChild(dt);
                extractedList.appendChild(dd);
            }

            function coerceFacturaProductosArray(raw) {
                if (Array.isArray(raw)) return raw;
                if (raw && typeof raw === "object") {
                    if (raw.codigo_proveedor || raw.codigo || raw.code) return [raw];
                    if (typeof raw.length === "number") {
                        try {
                            return Array.prototype.slice.call(raw);
                        } catch (sliceErr) {
                            /* ignore */
                        }
                    }
                }
                if (typeof raw === "string" && raw.trim()) {
                    try {
                        return coerceFacturaProductosArray(JSON.parse(raw));
                    } catch (parseErr) {
                        /* ignore */
                    }
                }
                return [];
            }

            function normalizeFacturaProductos(raw) {
                var list = coerceFacturaProductosArray(raw);
                var out = [];
                list.forEach(function (p) {
                    if (!p || typeof p !== "object") {
                        return;
                    }
                    var codigoRaw =
                        p.codigo_proveedor != null
                            ? p.codigo_proveedor
                            : p.codigo != null
                              ? p.codigo
                              : p.code;
                    var codigo = String(codigoRaw == null ? "" : codigoRaw).trim();
                    var item = {
                        codigo_proveedor: codigo,
                        cantidad: p.cantidad != null ? p.cantidad : p.qty,
                        valor_neto: p.valor_neto != null ? p.valor_neto : p.precio,
                        descripcion: String(p.descripcion || ""),
                    };
                    var hasData =
                        codigo ||
                        item.descripcion ||
                        (item.cantidad != null && item.cantidad !== "") ||
                        (item.valor_neto != null && item.valor_neto !== "");
                    if (hasData) out.push(item);
                });
                return out;
            }

            function parseMontoFacturaOcr(raw) {
                if (raw == null) return null;
                var t = String(raw).trim().replace(/\s/g, "");
                if (!t) return null;
                if (t.indexOf(",") >= 0) {
                    t = t.replace(/\./g, "").replace(",", ".");
                } else if (/^\d{1,3}(\.\d{3})+$/.test(t)) {
                    t = t.replace(/\./g, "");
                }
                var v = parseFloat(t);
                return isNaN(v) ? null : v;
            }

            /** Si el servidor devolvió productos vacíos, extrae del OCR crudo (Fitalia: 2,00UN x 8.500). */
            function parseProductosFromOcrCrudo(crudo) {
                var t = String(crudo || "")
                    .replace(/\u2013/g, "-")
                    .replace(/\u2014/g, "-")
                    .replace(/\u2212/g, "-");
                if (!t.trim()) return [];

                var codes = [];
                var reCode = /\b([A-Z]\d{4,6}-[A-Z0-9]{2,12})\b/gi;
                var m;
                while ((m = reCode.exec(t)) !== null) {
                    if (m.index > 0 && t.charAt(m.index - 1) === "-") continue;
                    var head = m[1].split("-")[0] || "";
                    if (!/\d/.test(head)) continue;
                    var c = m[1].toUpperCase();
                    if (codes.indexOf(c) < 0) codes.push(c);
                }
                if (!codes.length) {
                    var mSp = t.match(/\b([A-Z]\d{4,6})\s+([A-Z]{2,12})\b/i);
                    if (mSp && /\d/.test(mSp[1])) {
                        codes.push(mSp[1].toUpperCase() + "-" + mSp[2].toUpperCase());
                    }
                }
                if (!codes.length) return [];

                var codigo = codes[codes.length - 1];
                var qty = null;
                var mTot = t.match(/TOT\.?\s*UNIDADES\s*:?\s*(\d+)/i);
                var mUn = t.match(/(\d+),\d+\s*UN/i);
                if (mTot) qty = parseInt(mTot[1], 10);
                else if (mUn) qty = parseInt(mUn[1], 10);
                if (!qty || qty < 1 || qty > 99999) return [];

                var precio = null;
                var mPx = t.match(/(\d+),\d+\s*UN\s*(?:X|x)\s*([\d.,]+)/i);
                if (mPx) precio = parseMontoFacturaOcr(mPx[2]);
                if (precio == null) {
                    var mNeto = t.match(/TOTAL\s+NETO\s*:?\s*([\d.,]+)/i);
                    if (mNeto) {
                        var neto = parseMontoFacturaOcr(mNeto[1]);
                        if (neto != null && qty) precio = Math.round(neto / qty);
                    }
                }
                if (precio == null || precio <= 0) return [];

                return [
                    {
                        codigo_proveedor: codigo,
                        cantidad: qty,
                        valor_neto: Math.round(precio),
                    },
                ];
            }

            function hydrateProductosFromFlatFields(data) {
                if (!data) return;
                if (Array.isArray(data.productos) && data.productos.length) return;
                if (!data.producto_codigo) return;
                data.productos = [
                    {
                        codigo_proveedor: String(data.producto_codigo).trim(),
                        cantidad: data.producto_cantidad,
                        valor_neto: data.producto_valor_neto,
                    },
                ];
            }

            /** DOM del preview de factura (consulta en vivo; evita refs obsoletos tras SPA). */
            function getFacturaPreviewDom() {
                return {
                    extractedBox: document.getElementById("ingresoFacturaExtracted"),
                    extractedList: document.getElementById("ingresoFacturaExtractedList"),
                    productsBox: document.getElementById("ingresoFacturaExtractedProducts"),
                    productsTitle: document.getElementById("ingresoFacturaExtractedProductsTitle"),
                    productsBody: document.getElementById("ingresoFacturaExtractedProductsBody"),
                };
            }

            /**
             * Unifica productos desde productos[], campos planos u OCR crudo.
             * Mundo Repuestos suele traer productos[] con varios ítems (PDF/columnas).
             * Fitalia suele traer 1 ítem térmico + producto_codigo/producto_cantidad planos.
             */
            function collectFacturaProductosList(data) {
                if (!data) return [];

                var prods = normalizeFacturaProductos(data.productos);
                if (prods.length) {
                    data.productos = prods;
                    return prods;
                }

                if (data.producto_codigo) {
                    var flat = {
                        codigo_proveedor: String(data.producto_codigo).trim(),
                        cantidad: data.producto_cantidad,
                        valor_neto: data.producto_valor_neto,
                    };
                    if (flat.codigo_proveedor) {
                        data.productos = [flat];
                        return [flat];
                    }
                }

                if (
                    data.producto_cantidad != null ||
                    data.producto_valor_neto != null
                ) {
                    var flatSinCodigo = {
                        codigo_proveedor: "",
                        descripcion: data.producto_descripcion || "",
                        cantidad: data.producto_cantidad,
                        valor_neto: data.producto_valor_neto,
                    };
                    if (
                        flatSinCodigo.cantidad != null ||
                        flatSinCodigo.valor_neto != null
                    ) {
                        data.productos = [flatSinCodigo];
                        return [flatSinCodigo];
                    }
                }

                var fromCrudo = parseProductosFromOcrCrudo(data.ocr_texto_crudo);
                if (fromCrudo.length) {
                    prods = normalizeFacturaProductos(fromCrudo);
                    if (prods.length) {
                        data.productos = prods;
                        return prods;
                    }
                }

                data.productos = [];
                return [];
            }

            function ensureFacturaProductos(data) {
                return collectFacturaProductosList(data);
            }

            /** Dibuja filas en #ingresoFacturaExtractedProductsBody */
            function renderFacturaProductosRows(prods) {
                var dom = getFacturaPreviewDom();
                var productsBody = dom.productsBody;
                var productsBox = dom.productsBox;
                var productsTitle = dom.productsTitle;
                if (!productsBody || !productsBox) {
                    return;
                }
                productsBody.innerHTML = "";
                if (!prods || !prods.length) {
                    productsBox.hidden = true;
                    return;
                }
                if (productsTitle) {
                    productsTitle.textContent =
                        "Productos detectados (" + prods.length + ")";
                }
                prods.forEach(function (p) {
                    var tr = document.createElement("tr");
                    var tdCode = document.createElement("td");
                    var codigo = String(p.codigo_proveedor || "").trim();
                    tdCode.textContent = codigo || "—";
                    var tdCant = document.createElement("td");
                    tdCant.className = "num";
                    tdCant.textContent =
                        p.cantidad != null && p.cantidad !== "" ? String(p.cantidad) : "—";
                    var tdNeto = document.createElement("td");
                    tdNeto.className = "num";
                    tdNeto.textContent =
                        p.valor_neto != null && p.valor_neto !== ""
                            ? formatValorNetoUnitario(p.valor_neto)
                            : "—";
                    tr.appendChild(tdCode);
                    tr.appendChild(tdCant);
                    tr.appendChild(tdNeto);
                    productsBody.appendChild(tr);
                });
                productsBox.hidden = false;
            }

            function renderExtractedPreview(data) {
                var dom = getFacturaPreviewDom();
                if (!dom.extractedList || !dom.extractedBox) return;
                var prods = collectFacturaProductosList(data);
                extractedList = dom.extractedList;
                extractedBox = dom.extractedBox;
                extractedProductsBox = dom.productsBox;
                extractedProductsTitle = dom.productsTitle;
                extractedProductsBody = dom.productsBody;

                extractedList.innerHTML = "";
                if (dom.productsBody) dom.productsBody.innerHTML = "";
                if (dom.productsBox) dom.productsBox.hidden = true;

                dlRow("RUT proveedor", data.rut_proveedor);
                dlRow("N° documento", data.numero_documento);
                dlRow("Fecha", data.fecha);
                dlRow("Método de pago", data.metodo_pago);
                if (data.total_neto != null) dlRow("Neto", data.total_neto);
                if (data.total != null) dlRow("Total", data.total);
                if (data.iva != null) dlRow("IVA", data.iva);
                if (data.ocr_parser_rev) {
                    dlRow("Parser OCR", data.ocr_parser_rev);
                }
                if (data.productos_n != null) {
                    dlRow("Ítems API", data.productos_n);
                }

                if (data.productos_fuente) {
                    dlRow("Fuente ítems", data.productos_fuente);
                }

                var p0 = prods[0] || null;
                if (p0 && p0.codigo_proveedor) {
                    dlRow("Código producto", p0.codigo_proveedor);
                }
                if (
                    p0 &&
                    p0.descripcion &&
                    !p0.codigo_proveedor &&
                    !/^(xin\s*wang|xing\s*wang)$/i.test(
                        String(p0.descripcion).trim().replace(/\s+/g, "")
                    )
                ) {
                    dlRow("Descripción", p0.descripcion);
                }
                if (p0 && p0.cantidad != null && p0.cantidad !== "") {
                    dlRow("Cantidad", p0.cantidad);
                }
                if (p0 && p0.valor_neto != null && p0.valor_neto !== "") {
                    dlRow("V. neto unit.", formatValorNetoUnitario(p0.valor_neto));
                }
                extractedBox.hidden = false;
                window.setTimeout(function () {
                    renderFacturaProductosRows(prods);
                    try {
                        document.dispatchEvent(
                            new CustomEvent("ingreso:factura-analizada", { detail: data })
                        );
                    } catch (evErr) {
                        /* ignore */
                    }
                }, 0);
            }

            function mapMetodoPago(raw) {
                var m = String(raw || "").trim().toLowerCase();
                if (!m) return "";
                if (m.indexOf("credito") !== -1 || m.indexOf("crédito") !== -1) return "Crédito proveedor";
                if (m.indexOf("cheque") !== -1) return "Cheque al día";
                if (m.indexOf("transfer") !== -1) return "Transferencia bancaria";
                if (m.indexOf("contado") !== -1 || m.indexOf("efectivo") !== -1) return "Efectivo";
                return "";
            }

            function fechaToInputValue(fechaRaw) {
                var s = String(fechaRaw || "").trim();
                if (!s) return "";
                var mIso = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
                if (mIso) {
                    var yIso = parseInt(mIso[1], 10);
                    var moIso = parseInt(mIso[2], 10);
                    var dIso = parseInt(mIso[3], 10);
                    if (dIso >= 1 && dIso <= 31 && moIso >= 1 && moIso <= 12) {
                        var padIso = function (n) {
                            return n < 10 ? "0" + n : String(n);
                        };
                        return yIso + "-" + padIso(moIso) + "-" + padIso(dIso);
                    }
                }
                var m = s.match(/^(\d{1,2})[\/\-.](\d{1,2})[\/\-.](\d{2,4})$/);
                if (!m) return "";
                var d = parseInt(m[1], 10);
                var mo = parseInt(m[2], 10);
                var y = parseInt(m[3], 10);
                if (y < 100) y += 2000;
                if (d < 1 || d > 31 || mo < 1 || mo > 12) return "";
                var pad = function (n) {
                    return n < 10 ? "0" + n : String(n);
                };
                return y + "-" + pad(mo) + "-" + pad(d);
            }

            function formatMontoInput(n) {
                if (n === null || n === undefined || n === "") return "";
                var v = Number(n);
                if (isNaN(v)) return String(n);
                try {
                    return v.toLocaleString("es-CL", { maximumFractionDigits: 2 });
                } catch (e) {
                    return String(v);
                }
            }

            function totalConIvaDesdeProductos(prods) {
                if (!prods || !prods.length) return null;
                var sum = 0;
                prods.forEach(function (p) {
                    var c = Number(p.cantidad);
                    if (!isFinite(c) || c <= 0) c = 1;
                    var v = Number(p.valor_neto);
                    if (!isFinite(v) || v <= 0) return;
                    sum += c * v;
                });
                if (sum <= 0) return null;
                return Math.round(sum * 1.19);
            }

            function reconcileExtractedTotal(data) {
                var prods = ensureFacturaProductos(data);
                var esperado = totalConIvaDesdeProductos(prods);
                if (esperado == null) return data.total;
                var total = Number(data.total);
                if (!isFinite(total) || total <= 0) return esperado;
                var tol = Math.max(50, Math.round(esperado * 0.02));
                if (Math.abs(total - esperado) > tol) return esperado;
                return total;
            }

            function isIngresoRowEmpty(row) {
                if (!row) return true;
                var code = row.querySelector("input[name='codigo_producto[]']");
                var prov = row.querySelector("input[name='codigo_proveedor_producto[]']");
                var cant = row.querySelector("input[name='cantidad_producto[]']");
                var hasCode = code && String(code.value || "").trim();
                var hasProv = prov && String(prov.value || "").trim();
                var hasCant = cant && String(cant.value || "").trim();
                return !hasCode && !hasProv && !hasCant;
            }

            function dispatchIngresoProvLookupEvents(inputEl) {
                if (!inputEl) return;
                ["input", "change", "blur"].forEach(function (type) {
                    inputEl.dispatchEvent(new Event(type, { bubbles: true }));
                });
            }

            function fillIngresoRowFromProduct(row, prod) {
                if (!row || !prod) return 0;
                var filled = {};
                var inpProv =
                    row.querySelector("input.ingreso-codigo-proveedor-input") ||
                    row.querySelector("input[name='codigo_proveedor_producto[]']");
                var inpCant = row.querySelector("input[name='cantidad_producto[]']");
                var inpVN = row.querySelector("input[name='valor_neto_producto[]']");
                if (inpProv && prod.codigo_proveedor) {
                    inpProv.value = String(prod.codigo_proveedor).trim();
                    filled.codigo_proveedor = inpProv.value;
                    dispatchIngresoProvLookupEvents(inpProv);
                }
                if (inpCant && prod.cantidad != null && prod.cantidad !== "") {
                    inpCant.value = String(Math.max(1, parseInt(prod.cantidad, 10) || 1));
                    filled.cantidad = inpCant.value;
                }
                if (inpVN && prod.valor_neto != null && prod.valor_neto !== "") {
                    setValorNetoIngresoRaw(inpVN, prod.valor_neto);
                    filled.valor_neto = inpVN.getAttribute("data-raw") || inpVN.value;
                }
                return Object.keys(filled).length;
            }

            function markAutoFilled(el) {
                if (!el) return;
                el.classList.add("ingreso-auto-filled");
                window.setTimeout(function () {
                    el.classList.remove("ingreso-auto-filled");
                }, 4000);
            }

            function applyProductsFromFactura(productos) {
                if (!itemsBody || !productos.length) return 0;
                var filledCount = 0;
                productos.forEach(function (prod, idx) {
                    var row;
                    if (idx === 0) {
                        row = itemsBody.querySelector("tr.item-row");
                        if (!row) {
                            addRow();
                            row = itemsBody.querySelector("tr.item-row");
                        }
                    } else {
                        addRow();
                        var all = itemsBody.querySelectorAll("tr.item-row");
                        row = all[all.length - 1];
                    }
                    if (!row) return;
                    filledCount += fillIngresoRowFromProduct(row, prod);
                    var inpProv =
                        row.querySelector("input.ingreso-codigo-proveedor-input") ||
                        row.querySelector("input[name='codigo_proveedor_producto[]']");
                    var inpCant = row.querySelector("input[name='cantidad_producto[]']");
                    var inpVN = row.querySelector("input[name='valor_neto_producto[]']");
                    if (inpProv && prod.codigo_proveedor) markAutoFilled(inpProv);
                    if (inpCant && prod.cantidad != null && prod.cantidad !== "") {
                        markAutoFilled(inpCant);
                    }
                    if (inpVN && prod.valor_neto != null && prod.valor_neto !== "") {
                        markAutoFilled(inpVN);
                    }
                });
                refreshIngresoItemNumbers();
                updateIngresoTotals();
                return filledCount;
            }

            function applyExtractedData(data) {
                if (!data) return 0;
                var count = 0;

                if (data.numero_documento && numeroDocInput) {
                    numeroDocInput.value = String(data.numero_documento).trim();
                    markAutoFilled(numeroDocInput);
                    count++;
                }
                if (fechaInput) {
                    if (data.fecha) {
                        var fv = fechaToInputValue(data.fecha);
                        if (fv) {
                            fechaInput.value = fv;
                            markAutoFilled(fechaInput);
                            count++;
                        }
                    } else {
                        fechaInput.classList.remove("ingreso-auto-filled");
                    }
                }
                if (metodoPagoSel && data.metodo_pago) {
                    var mp = mapMetodoPago(data.metodo_pago);
                    if (mp) {
                        metodoPagoSel.value = mp;
                        markAutoFilled(metodoPagoSel);
                        count++;
                    }
                }
                if (data.rut_proveedor && setIngresoSupplierRut(data.rut_proveedor, { search: true })) {
                    markAutoFilled(rutInput);
                    count++;
                }

                count += applyProductsFromFactura(ensureFacturaProductos(data));

                var totalAplicar = reconcileExtractedTotal(data);
                if (inpTotalFactura && totalAplicar != null && totalAplicar !== "") {
                    inpTotalFactura.value = formatMontoInput(totalAplicar);
                    markAutoFilled(inpTotalFactura);
                    count++;
                }

                updateIngresoTotals();
                showFacturaAutoBadge(count);
                return count;
            }

            function runAnalyze() {
                if (!pendingFile) {
                    setStatus("Seleccioná una imagen o PDF primero.", "error");
                    return;
                }
                if (!analizarUrl) {
                    setStatus("URL de análisis no configurada.", "error");
                    return;
                }
                if (btnAnalyze) btnAnalyze.disabled = true;
                if (btnApply) btnApply.disabled = true;
                setStatus("Analizando factura (Google Vision OCR)…", "loading");

                readFileAsBase64(pendingFile)
                    .then(function (b64) {
                        pendingBase64 = b64;
                        return fetch(analizarUrl, {
                            method: "POST",
                            credentials: "same-origin",
                            headers: {
                                "Content-Type": "application/json",
                                "X-Requested-With": "XMLHttpRequest",
                                "X-CSRF-Token": getCsrfToken(),
                                Accept: "application/json",
                            },
                            body: JSON.stringify({
                                image_base64: b64,
                                media_type: pendingMediaType,
                            }),
                        });
                    })
                    .then(function (res) {
                        return res.text().then(function (t) {
                            var body = null;
                            try {
                                body = JSON.parse(t);
                            } catch (e) {}
                            return { res: res, body: body, text: t };
                        });
                    })
                    .then(function (pack) {
                        if (!pack.body || !pack.res.ok || !pack.body.success) {
                            var msg =
                                (pack.body && pack.body.message) ||
                                "No se pudo analizar la factura.";
                            throw new Error(msg);
                        }
                        var apiBody = pack.body || {};
                        extractedData = JSON.parse(JSON.stringify(apiBody.data || {}));
                        hydrateProductosFromFlatFields(extractedData);
                        if (
                            (!extractedData.productos || !extractedData.productos.length) &&
                            Array.isArray(apiBody.productos) &&
                            apiBody.productos.length
                        ) {
                            extractedData.productos = JSON.parse(
                                JSON.stringify(apiBody.productos)
                            );
                        }
                        if (!Array.isArray(extractedData.productos)) {
                            var coerced = coerceFacturaProductosArray(
                                extractedData.productos
                            );
                            extractedData.productos = coerced.length ? coerced : [];
                        }
                        collectFacturaProductosList(extractedData);
                        if (extractedData.preview_base64) {
                            applyBackendPreviewImage(extractedData.preview_base64);
                        }
                        renderExtractedPreview(extractedData);
                        ensurePreviewVisibleAfterAnalyze();
                        if (btnApply) btnApply.disabled = false;
                        setStatus(
                            !extractedData.fecha
                                ? "Análisis listo. No se pudo leer la fecha — revisá el documento o subí el PDF original."
                                : "Análisis listo. Revisá los datos y presioná «Aplicar datos».",
                            !extractedData.fecha ? "error" : null
                        );
                    })
                    .catch(function (err) {
                        setStatus(err.message || "Error al analizar la factura.", "error");
                    })
                    .finally(function () {
                        if (btnAnalyze) btnAnalyze.disabled = false;
                    });
            }

            function acceptFacturaFile(f) {
                if (!f) {
                    return false;
                }
                var okType =
                    /^image\/(jpeg|png|webp)$/i.test(f.type) ||
                    f.type === "application/pdf" ||
                    /\.(jpe?g|png|webp|pdf)$/i.test(f.name || "");
                if (!okType) {
                    setStatus("Formato no válido. Use JPG, PNG, WEBP o PDF.", "error");
                    openModal();
                    return false;
                }
                if (f.size > 12 * 1024 * 1024) {
                    setStatus("El archivo supera 12 MB.", "error");
                    openModal();
                    return false;
                }
                showFilePreview(f);
                return true;
            }

            btnScan.addEventListener("click", function () {
                fileInput.value = "";
                fileInput.click();
            });

            fileInput.addEventListener("change", function () {
                acceptFacturaFile(fileInput.files && fileInput.files[0]);
            });

            var facturaScanDragDepth = 0;
            btnScan.addEventListener("dragenter", function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                facturaScanDragDepth += 1;
                btnScan.classList.add("is-dragover");
            });
            btnScan.addEventListener("dragover", function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                if (ev.dataTransfer) {
                    ev.dataTransfer.dropEffect = "copy";
                }
            });
            btnScan.addEventListener("dragleave", function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                facturaScanDragDepth -= 1;
                if (facturaScanDragDepth <= 0) {
                    facturaScanDragDepth = 0;
                    btnScan.classList.remove("is-dragover");
                }
            });
            btnScan.addEventListener("drop", function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                facturaScanDragDepth = 0;
                btnScan.classList.remove("is-dragover");
                var f =
                    ev.dataTransfer && ev.dataTransfer.files && ev.dataTransfer.files[0];
                if (f) {
                    acceptFacturaFile(f);
                }
            });

            if (btnAnalyze) btnAnalyze.addEventListener("click", runAnalyze);
            if (btnApply) {
                btnApply.addEventListener("click", function () {
                    if (!extractedData) return;
                    applyExtractedData(extractedData);
                    if (!extractedData.fecha) {
                        setStatus(
                            "Datos aplicados. Fecha no detectada: corregila manualmente en el formulario.",
                            "error"
                        );
                    } else {
                        setStatus("Datos aplicados al formulario.", null);
                    }
                    closeModal();
                });
            }
            function cancelScan() {
                closeModal();
                pendingFile = null;
                pendingBase64 = "";
                resetPreview();
                setStatus("", null);
                hideFacturaAutoBadge();
            }

            setPreviewWrapVisible(false);
            setStatus("", null);
            hideFacturaAutoBadge();
            if (modalClose) modalClose.addEventListener("click", cancelScan);
            if (btnCancel) btnCancel.addEventListener("click", cancelScan);
            modal.addEventListener("click", function (ev) {
                if (ev.target === modal) cancelScan();
            });
            document.addEventListener("keydown", function (ev) {
                if (ev.key === "Escape" && modal.classList.contains("open")) cancelScan();
            });
        })();

        if (form.dataset.supplierFound === "1") {
            updateSupplierSummary();
            collapseSupplierSection();
        } else if (form.dataset.supplierRegistration === "1") {
            expandSupplierSection();
        } else {
            hideSupplierRegistrationCard();
        }
    }

    function initEtiquetasView(root) {
        var labelsForm = root.querySelector("#labelsForm");
        if (!labelsForm || labelsForm.dataset.bodegaUiBound === "1") return;
        labelsForm.dataset.bodegaUiBound = "1";

        var codigosInput = root.querySelector("#codigos");
        var fpInput = root.querySelector("#fp");
        var fpPorCodigoJsonInput = root.querySelector("#fp_por_codigo_json");
        var labelSearchInput = root.querySelector("#label-search");
        var labelSearchBtn = root.querySelector("#label-search-btn");
        var labelSearchResults = root.querySelector("#label-search-results");
        var etiquetasProductModal = root.querySelector("#etiquetasProductModal");
        var etiquetasProductModalClose = root.querySelector("#etiquetasProductModalClose");
        var etiquetasProductSearchInput = root.querySelector("#etiquetasProductSearchInput");
        var etiquetasProductSearchBtn = root.querySelector("#etiquetasProductSearchBtn");
        var etiquetasProductSearchStatus = root.querySelector("#etiquetasProductSearchStatus");
        var etiquetasProductSearchResults = root.querySelector("#etiquetasProductSearchResults");
        var sheet = root.querySelector("#sheet");
        var printModeSelect = root.querySelector("#printMode");
        var btnPrintLabels = root.querySelector("#btnPrintLabels");
        var searchUrl = (labelSearchInput && labelSearchInput.getAttribute("data-search-url")) || "";
        var previewUrl = (sheet && sheet.getAttribute("data-preview-url")) || "";
        var logoSrc = (sheet && sheet.getAttribute("data-logo-src")) || "";
        var registerUrl = (btnPrintLabels && btnPrintLabels.getAttribute("data-register-url")) || "";
        var thermalStyleId = "thermal-page-size-style";
        var lastPrintRegistrationAt = 0;
        var searchTimer = null;
        var timer = null;

        function normalizePrintMode(mode) {
            if (mode === "thermal" || mode === "thermal_100x150") return mode;
            return "a4";
        }

        function thermalPageStyleRule(mode) {
            if (mode === "thermal") {
                return "@media print { @page { size: 60mm 40mm; margin: 0; } }";
            }
            if (mode === "thermal_100x150") {
                return "@media print { @page { size: 100mm 150mm; margin: 3mm 2mm 3mm 7mm; } }";
            }
            return "";
        }

        function ensureThermalPageStyle(mode) {
            var existing = document.getElementById(thermalStyleId);
            if (existing) existing.remove();
            var rule = thermalPageStyleRule(mode);
            if (!rule) return;
            var style = document.createElement("style");
            style.id = thermalStyleId;
            style.textContent = rule;
            // Al final del <body> para que la regla @page sea la última y gane sobre @page letter.
            document.body.appendChild(style);
        }

        function optimizePrintGrid() {
            var mode = document.documentElement.getAttribute("data-print-mode") || "a4";
            if (mode === "thermal") {
                document.documentElement.style.setProperty("--print-label-width", "60mm");
                document.documentElement.style.removeProperty("--print-label-max-height");
                document.documentElement.style.setProperty("--print-col-gap", "0");
                document.documentElement.style.setProperty("--print-row-gap", "0");
                document.documentElement.style.setProperty("--print-justify", "start");
                return;
            }
            if (mode === "thermal_100x150") {
                document.documentElement.style.removeProperty("--print-label-width");
                document.documentElement.style.removeProperty("--print-label-max-height");
                document.documentElement.style.removeProperty("--print-col-gap");
                document.documentElement.style.removeProperty("--print-row-gap");
                document.documentElement.style.removeProperty("--print-justify");
                return;
            }
            var labelWidth = 280;
            var pageMarginPx = 38;
            var minGapPx = 10;
            var available = Math.max(320, window.innerWidth - pageMarginPx);
            var cols = Math.max(1, Math.floor((available + minGapPx) / (labelWidth + minGapPx)));
            var used = cols * labelWidth;
            var remaining = Math.max(0, available - used);
            var gap = cols > 1 ? Math.max(minGapPx, Math.floor(remaining / (cols - 1))) : 0;

            document.documentElement.style.setProperty("--print-label-width", labelWidth + "px");
            document.documentElement.style.setProperty("--print-col-gap", gap + "px");
            document.documentElement.style.setProperty("--print-row-gap", "12px");
            document.documentElement.style.setProperty("--print-justify", cols > 1 ? "start" : "center");
        }

        function applyPrintMode(mode) {
            var normalized = normalizePrintMode(mode);
            document.documentElement.setAttribute("data-print-mode", normalized);
            if (printModeSelect) printModeSelect.value = normalized;
            ensureThermalPageStyle(normalized === "a4" ? null : normalized);
            try {
                localStorage.setItem("etiquetas_print_mode", normalized);
            } catch (e) {}
            optimizePrintGrid();
        }

        function escapeHtml(str) {
            return String(str || "")
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        function highlightText(value, search) {
            var raw = String(value || "");
            var q = String(search || "").trim();
            if (!q) return escapeHtml(raw);
            var tokens = q.split(/\s+/).filter(Boolean).map(function (x) {
                return x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
            });
            if (!tokens.length) return escapeHtml(raw);
            var regex = new RegExp("(" + tokens.join("|") + ")", "ig");
            return escapeHtml(raw).replace(regex, "<mark>$1</mark>");
        }

        function appendCodeToInput(code) {
            var clean = String(code || "").trim().toUpperCase();
            if (!clean || !codigosInput) return;
            var current = (codigosInput.value || "").trim();
            if (!current) {
                codigosInput.value = clean;
                return;
            }
            var normalized = current.toUpperCase();
            if (normalized.split(/[\n,;]+/).map(function (x) { return x.trim(); }).includes(clean)) {
                return;
            }
            codigosInput.value = current + ", " + clean;
        }

        function renderSearchResults(items, searchTerm) {
            if (!labelSearchResults) return;
            if (!items || !items.length) {
                labelSearchResults.innerHTML = "<div class='search-help'>Sin resultados para esta busqueda.</div>";
                return;
            }
            labelSearchResults.innerHTML = items.map(function (item) {
                return "" +
                    "<div class='search-assist-item'>" +
                        "<div class='search-chip'><strong>" + highlightText(item.codigo || "", searchTerm) + "</strong></div>" +
                        "<div class='search-chip muted'>OEM: " + highlightText(item.codigo_oem || "-", searchTerm) + "</div>" +
                        "<div class='search-chip' title='" + escapeHtml(item.descripcion || "") + "'>" + highlightText(item.descripcion || "", searchTerm) + "</div>" +
                        "<button type='button' class='search-add-btn' data-code='" + escapeHtml(item.codigo || "") + "'>Agregar</button>" +
                    "</div>";
            }).join("");
            labelSearchResults.querySelectorAll(".search-add-btn").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    appendCodeToInput(btn.getAttribute("data-code"));
                    if (codigosInput) codigosInput.focus();
                });
            });
        }

        function searchProductsForLabels() {
            var q = (labelSearchInput && labelSearchInput.value || "").trim();
            if (!labelSearchResults) return;
            if (q.length < 2) {
                labelSearchResults.innerHTML = "";
                return;
            }
            if (!searchUrl) return;

            fetch(searchUrl + "?q=" + encodeURIComponent(q), {
                method: "GET",
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (!data || !data.success) {
                        labelSearchResults.innerHTML = "<div class='search-help'>No se pudo obtener resultados.</div>";
                        return;
                    }
                    renderSearchResults(data.items || [], q);
                })
                .catch(function () {
                    labelSearchResults.innerHTML = "<div class='search-help'>Error al buscar productos.</div>";
                });
        }

        function debounceProductSearch() {
            if (searchTimer) clearTimeout(searchTimer);
            searchTimer = setTimeout(searchProductsForLabels, 250);
        }

        function closeEtiquetasProductModal() {
            if (!etiquetasProductModal) return;
            etiquetasProductModal.classList.remove("open");
            etiquetasProductModal.setAttribute("aria-hidden", "true");
        }

        function searchProductsInEtiquetasModal() {
            if (!etiquetasProductSearchInput || !etiquetasProductSearchResults) return;
            var q = (etiquetasProductSearchInput.value || "").trim();
            if (q.length < 2) {
                if (etiquetasProductSearchStatus) {
                    etiquetasProductSearchStatus.textContent = "Escribe al menos 2 caracteres.";
                }
                etiquetasProductSearchResults.innerHTML = "";
                return;
            }
            if (!searchUrl) return;
            if (etiquetasProductSearchStatus) etiquetasProductSearchStatus.textContent = "Buscando...";
            fetch(searchUrl + "?q=" + encodeURIComponent(q), {
                method: "GET",
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (!data || !data.success) {
                        if (etiquetasProductSearchStatus) {
                            etiquetasProductSearchStatus.textContent = "No se pudo obtener resultados.";
                        }
                        etiquetasProductSearchResults.innerHTML = "";
                        return;
                    }
                    var items = data.items || [];
                    if (etiquetasProductSearchStatus) {
                        etiquetasProductSearchStatus.textContent = items.length
                            ? items.length + " resultado(s)."
                            : "Sin resultados para esta busqueda.";
                    }
                    if (!items.length) {
                        etiquetasProductSearchResults.innerHTML =
                            "<div class='search-help'>Sin resultados para esta busqueda.</div>";
                        return;
                    }
                    etiquetasProductSearchResults.innerHTML = items.map(function (item) {
                        return "" +
                            "<div class='search-assist-item'>" +
                                "<div class='search-chip'><strong>" + highlightText(item.codigo || "", q) + "</strong></div>" +
                                "<div class='search-chip muted'>OEM: " + highlightText(item.codigo_oem || "-", q) + "</div>" +
                                "<div class='search-chip' title='" + escapeHtml(item.descripcion || "") + "'>" +
                                    highlightText(item.descripcion || "", q) + "</div>" +
                                "<button type='button' class='search-add-btn' data-code='" + escapeHtml(item.codigo || "") + "'>Agregar</button>" +
                            "</div>";
                    }).join("");
                    etiquetasProductSearchResults.querySelectorAll(".search-add-btn").forEach(function (btn) {
                        btn.addEventListener("click", function () {
                            appendCodeToInput(btn.getAttribute("data-code"));
                            if (codigosInput) codigosInput.focus();
                        });
                    });
                })
                .catch(function () {
                    if (etiquetasProductSearchStatus) {
                        etiquetasProductSearchStatus.textContent = "Error al buscar productos.";
                    }
                    etiquetasProductSearchResults.innerHTML = "";
                });
        }

        function openEtiquetasProductModal() {
            if (!etiquetasProductModal) return;
            var preset = (labelSearchInput && labelSearchInput.value || "").trim();
            etiquetasProductModal.classList.add("open");
            etiquetasProductModal.setAttribute("aria-hidden", "false");
            if (etiquetasProductSearchInput) {
                etiquetasProductSearchInput.value = preset;
                setTimeout(function () {
                    etiquetasProductSearchInput.focus();
                    if (preset.length >= 2) searchProductsInEtiquetasModal();
                }, 20);
            } else if (preset.length >= 2) {
                searchProductsInEtiquetasModal();
            } else if (etiquetasProductSearchStatus) {
                etiquetasProductSearchStatus.textContent = "Escribe al menos 2 caracteres y pulsa Buscar.";
            }
            if (etiquetasProductSearchResults) etiquetasProductSearchResults.innerHTML = "";
        }

        function applyNameClass(el, text) {
            el.classList.remove("name-sm", "name-xs");
            var len = (text || "").trim().length;
            if (len > 50) el.classList.add("name-xs");
            else if (len > 34) el.classList.add("name-sm");
        }

        function bindEditableFields() {
            if (!sheet) return;
            sheet.querySelectorAll(".label").forEach(function (card) {
                if (card.dataset.bodegaEditableBound === "1") return;
                card.dataset.bodegaEditableBound = "1";

                var codeField = card.querySelector(".codigo-field");
                var descriptionField = card.querySelector(".descripcion-field");
                var modelField = card.querySelector(".modelo-field");
                var fpField = card.querySelector(".fp-card.inline-edit");
                var codeInput = card.querySelector(".edit-codigo");
                var descriptionInput = card.querySelector(".edit-descripcion");
                var modelInput = card.querySelector(".edit-modelo");
                var fpInputField = card.querySelector(".edit-fp");
                var codeOutput = card.querySelector(".codigo");
                var descriptionOutput = card.querySelector(".descripcion");
                var modelOutput = card.querySelector(".modelo");
                var fpOutput = card.querySelector(".fp-value");

                function openField(fieldWrapper, input) {
                    if (!fieldWrapper || !input) return;
                    card.querySelectorAll(".inline-edit.is-active").forEach(function (field) {
                        if (field !== fieldWrapper) field.classList.remove("is-active");
                    });
                    fieldWrapper.classList.add("is-active");
                    input.focus();
                    input.select();
                }

                function closeField(fieldWrapper) {
                    if (fieldWrapper) fieldWrapper.classList.remove("is-active");
                }

                function sync() {
                    var code = (codeInput && codeInput.value || "").trim() || "-";
                    var description = (descriptionInput && descriptionInput.value || "").trim() || "SIN DESCRIPCION";
                    var model = (modelInput && modelInput.value || "").trim() || " ";
                    var fpValue = (fpInputField && fpInputField.value || "").trim() || "-";
                    if (codeOutput) codeOutput.textContent = code;
                    if (descriptionOutput) {
                        descriptionOutput.textContent = description;
                        applyNameClass(descriptionOutput, description);
                    }
                    if (modelOutput) modelOutput.textContent = model;
                    if (fpOutput) fpOutput.textContent = fpValue;
                }

                [codeInput, descriptionInput, modelInput, fpInputField].forEach(function (input) {
                    if (input) input.addEventListener("input", sync);
                    if (input) {
                        input.addEventListener("keydown", function (event) {
                            if (event.key === "Enter") {
                                event.preventDefault();
                                closeField(input.closest(".inline-edit"));
                            }
                            if (event.key === "Escape") {
                                event.preventDefault();
                                sync();
                                closeField(input.closest(".inline-edit"));
                            }
                        });
                        input.addEventListener("blur", function () {
                            closeField(input.closest(".inline-edit"));
                        });
                    }
                });

                [[codeField, codeInput], [descriptionField, descriptionInput], [modelField, modelInput], [fpField, fpInputField]].forEach(function (entry) {
                    var fieldWrapper = entry[0];
                    var input = entry[1];
                    if (!fieldWrapper || !input) return;
                    var trigger = fieldWrapper.querySelector(".label-display-only, .fp-value");
                    if (trigger) {
                        trigger.addEventListener("click", function () {
                            openField(fieldWrapper, input);
                        });
                    }
                });
                sync();
            });
        }

        function captureLabelSheetEdits() {
            var queues = Object.create(null);
            var inOrder = [];
            if (!sheet) return { queues: queues, inOrder: inOrder };
            sheet.querySelectorAll(".label").forEach(function (card) {
                var anchor = (card.getAttribute("data-anchor-code") || "").trim().toUpperCase();
                var codeInput = card.querySelector(".edit-codigo");
                var c = (codeInput && codeInput.value || "").trim().toUpperCase();
                if (!anchor) {
                    anchor = c;
                }
                var row = {
                    anchor: anchor,
                    code: c,
                    descripcion: (card.querySelector(".edit-descripcion") && card.querySelector(".edit-descripcion").value || "").trim(),
                    modelo: (card.querySelector(".edit-modelo") && card.querySelector(".edit-modelo").value || "").trim(),
                    fp: (card.querySelector(".edit-fp") && card.querySelector(".edit-fp").value || "").trim()
                };
                inOrder.push(row);
                if (!queues[anchor]) {
                    queues[anchor] = [];
                }
                queues[anchor].push(row);
            });
            return { queues: queues, inOrder: inOrder };
        }

        function cloneAnchorQueues(qs) {
            var out = Object.create(null);
            if (!qs) return out;
            Object.keys(qs).forEach(function (k) {
                out[k] = qs[k].slice();
            });
            return out;
        }

        function applyLabelSheetEdits(preserved) {
            if (!sheet || !preserved) return;
            var queues = cloneAnchorQueues(preserved.queues);
            var inOrder = preserved.inOrder || [];
            var cards = sheet.querySelectorAll(".label");
            var lenMatch = inOrder.length === cards.length;
            var lastByAnchor = Object.create(null);

            for (var i = 0; i < cards.length; i++) {
                var card = cards[i];
                var anchor = (card.getAttribute("data-anchor-code") || "").trim().toUpperCase();
                var codeInput = card.querySelector(".edit-codigo");
                var d = card.querySelector(".edit-descripcion");
                var m = card.querySelector(".edit-modelo");
                var f = card.querySelector(".edit-fp");
                var codigoOut = card.querySelector(".codigo");

                var p = null;
                if (anchor && queues[anchor] && queues[anchor].length) {
                    p = queues[anchor].shift();
                }
                if (!p && anchor && lastByAnchor[anchor]) {
                    p = lastByAnchor[anchor];
                }
                if (!p && lenMatch && inOrder[i]) {
                    p = inOrder[i];
                }
                if (!p) continue;

                if (codeInput && (p.code || "").trim()) {
                    var pv = String(p.code).trim();
                    codeInput.value = pv;
                    if (codigoOut) codigoOut.textContent = pv;
                }
                if (d) d.value = p.descripcion;
                if (m) m.value = p.modelo;
                if (f) f.value = p.fp;
                var syncEl = codeInput || d;
                if (syncEl) syncEl.dispatchEvent(new Event("input", { bubbles: true }));

                if (anchor) {
                    lastByAnchor[anchor] = {
                        anchor: p.anchor || anchor,
                        code: p.code,
                        descripcion: p.descripcion,
                        modelo: p.modelo,
                        fp: p.fp
                    };
                }
            }
        }

        function renderLabels(labels) {
            if (!sheet) return;
            if (!labels || !labels.length) {
                sheet.innerHTML = "";
                return;
            }
            sheet.innerHTML = labels.map(function (label) {
                return "<article class='label' data-anchor-code='" + escapeHtml(label.codigo) + "'>" +
                    "<div class='label-header'>" +
                    "<div class='brand-row'><div class='logo-wrap'><img class='logo-img' src='" + logoSrc + "' alt='Logo Andes'></div><div class='empresa'>ANDES AUTO PARTS LTDA</div></div>" +
                    "<div class='contacto'>+56 9 2615 2826</div>" +
                    "<div class='correo'>andesautopartscl@gmail.com</div>" +
                    "</div>" +
                    "<div class='label-body'>" +
                    "<div class='inline-edit codigo-field' data-field='codigo'>" +
                    "<div class='codigo label-display-only'>" + escapeHtml(label.codigo) + "</div>" +
                    "<input class='editable-input field-input edit-codigo' value='" + escapeHtml(label.codigo) + "' placeholder='Codigo'>" +
                    "</div>" +
                    "<div class='inline-edit descripcion-field' data-field='descripcion'>" +
                    "<div class='descripcion " + escapeHtml(label.name_class || "") + " label-display-only'>" + escapeHtml(label.descripcion || label.nombre || "") + "</div>" +
                    "<input class='editable-input field-input edit-descripcion' value='" + escapeHtml(label.descripcion || label.nombre || "") + "' placeholder='Descripcion'>" +
                    "</div>" +
                    "<div class='inline-edit modelo-field' data-field='modelo'>" +
                    "<div class='modelo label-display-only'>" + escapeHtml(label.modelo || " ") + "</div>" +
                    "<input class='editable-input field-input edit-modelo' value='" + escapeHtml(label.modelo || "") + "' placeholder='Modelo'>" +
                    "</div>" +
                    "<div class='label-footer'>" +
                    "<div class='meta-row'>" +
                    "<div class='fp-card inline-edit' data-field='fp'><span class='fp-label'>F° P</span><strong class='fp-value'>" + escapeHtml(label.fp || "-") + "</strong><input class='editable-input field-input edit-fp' value='" + escapeHtml(label.fp || "") + "' placeholder='F° P'></div>" +
                    "<div class='qr'><img src='data:image/png;base64," + label.qr_base64 + "' alt='QR'></div>" +
                    "</div>" +
                    "<div class='barcode'><img src='data:image/png;base64," + label.barcode_base64 + "' alt='CODE128'></div>" +
                    "</div>" +
                    "</div>" +
                    "</article>";
            }).join("");
            bindEditableFields();
        }

        function collectLabelsForHistory() {
            if (!sheet) return [];
            var out = [];
            var collected = {};
            sheet.querySelectorAll(".label").forEach(function (card) {
                var codeEl = card.querySelector(".codigo");
                var descEl = card.querySelector(".descripcion");
                var code = (codeEl && codeEl.textContent || "").trim();
                var description = (descEl && descEl.textContent || "").trim();
                if (!code) return;
                if (!collected[code]) {
                    collected[code] = { codigo: code, descripcion: description, cantidad: 0 };
                }
                collected[code].cantidad += 1;
            });
            Object.keys(collected).forEach(function (k) { out.push(collected[k]); });
            return out;
        }

        function registerPrintHistory() {
            var now = Date.now();
            if (now - lastPrintRegistrationAt < 4000) return;
            if (!registerUrl) return;
            var labels = collectLabelsForHistory();
            if (!labels.length) return;
            lastPrintRegistrationAt = now;
            var payload = {
                labels: labels,
                document_reference: (codigosInput && codigosInput.value || "").slice(0, 120)
            };
            fetch(registerUrl, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest"
                },
                body: JSON.stringify(payload)
            }).catch(function () {});
        }

        function refreshPreview() {
            var codigos = (codigosInput && codigosInput.value || "").trim();
            var fp = (fpInput && fpInput.value || "").trim();
            if (!codigos) {
                if (sheet) sheet.innerHTML = "";
                return;
            }
            if (!previewUrl) return;
            var preservedEdits = captureLabelSheetEdits();
            var params = new URLSearchParams({ ajax: "1", codigos: codigos, fp: fp });
            var fpMapVal = (fpPorCodigoJsonInput && fpPorCodigoJsonInput.value || "").trim();
            if (fpMapVal) {
                params.set("fp_por_codigo_json", fpMapVal);
            }
            fetch(previewUrl + "?" + params.toString(), {
                method: "GET",
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data && data.labels) {
                        renderLabels(data.labels);
                        applyLabelSheetEdits(preservedEdits);
                    }
                })
                .catch(function () {});
        }

        function debounce() {
            if (timer) clearTimeout(timer);
            timer = setTimeout(refreshPreview, 380);
        }

        if (codigosInput) codigosInput.addEventListener("input", debounce);
        if (fpInput) fpInput.addEventListener("input", debounce);
        if (etiquetasProductModal) {
            if (labelSearchInput) {
                labelSearchInput.addEventListener("keydown", function (e) {
                    if (e.key === "Enter") {
                        e.preventDefault();
                        openEtiquetasProductModal();
                    }
                });
            }
            if (labelSearchBtn) {
                labelSearchBtn.addEventListener("click", openEtiquetasProductModal);
            }
            if (etiquetasProductModalClose) {
                etiquetasProductModalClose.addEventListener("click", closeEtiquetasProductModal);
            }
            etiquetasProductModal.addEventListener("click", function (e) {
                if (e.target === etiquetasProductModal) closeEtiquetasProductModal();
            });
            if (etiquetasProductSearchBtn) {
                etiquetasProductSearchBtn.addEventListener("click", searchProductsInEtiquetasModal);
            }
            if (etiquetasProductSearchInput) {
                etiquetasProductSearchInput.addEventListener("keydown", function (e) {
                    if (e.key === "Enter") {
                        e.preventDefault();
                        searchProductsInEtiquetasModal();
                    }
                    if (e.key === "Escape") closeEtiquetasProductModal();
                });
            }
        } else {
            if (labelSearchInput) {
                labelSearchInput.addEventListener("input", debounceProductSearch);
                labelSearchInput.addEventListener("keydown", function (e) {
                    if (e.key === "Enter") {
                        e.preventDefault();
                        if (searchTimer) clearTimeout(searchTimer);
                        searchProductsForLabels();
                    }
                });
            }
            if (labelSearchBtn) {
                labelSearchBtn.addEventListener("click", function () {
                    if (labelSearchInput) labelSearchInput.focus();
                    if (searchTimer) clearTimeout(searchTimer);
                    searchProductsForLabels();
                });
            }
        }
        if (printModeSelect) {
            printModeSelect.addEventListener("change", function () {
                applyPrintMode(printModeSelect.value);
            });
        }
        if (btnPrintLabels) {
            btnPrintLabels.addEventListener("click", function () {
                window.print();
            });
        }
        bindEditableFields();

        var preferredMode = "a4";
        try {
            preferredMode = localStorage.getItem("etiquetas_print_mode") || "a4";
        } catch (e) {}
        applyPrintMode(preferredMode);
        optimizePrintGrid();

        if (!window.__bodegaResizeHandlerBound) {
            window.__bodegaResizeHandlerBound = true;
            window.addEventListener("resize", optimizePrintGrid);
            window.addEventListener("beforeprint", function () {
                optimizePrintGrid();
                registerPrintHistory();
            });
        }
    }

    function bindMarcaSugerenciasForm(form) {
        if (!form || form.dataset.marcasSugerenciasBound === "1") {
            return;
        }
        form.dataset.marcasSugerenciasBound = "1";
        var marcasUrl = (form.getAttribute("data-marcas-url") || "").trim();
        var codigoInput =
            form.querySelector("#codigo") ||
            form.querySelector("input[name='codigo_producto']");
        var dl = form.querySelector("datalist");
        if (!marcasUrl || !codigoInput || !dl) {
            return;
        }
        var timer = null;
        function fetchMarcas() {
            var code = (codigoInput.value || "").trim().toUpperCase();
            var url = marcasUrl + (code ? "?codigo=" + encodeURIComponent(code) : "");
            fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
                .then(function (res) {
                    return res.json();
                })
                .then(function (data) {
                    if (!data || !data.ok || !data.marcas) {
                        dl.innerHTML = "";
                        return;
                    }
                    dl.innerHTML = "";
                    data.marcas.forEach(function (m) {
                        var o = document.createElement("option");
                        o.value = m;
                        dl.appendChild(o);
                    });
                })
                .catch(function () {
                    dl.innerHTML = "";
                });
        }
        function schedule() {
            if (timer) {
                clearTimeout(timer);
            }
            timer = setTimeout(fetchMarcas, 320);
        }
        codigoInput.addEventListener("input", schedule);
        codigoInput.addEventListener("blur", function () {
            fetchMarcas();
        });
        fetchMarcas();
    }

    function initAjusteHelp(form) {
        if (!form || form.dataset.ajusteHelpBound === "1") {
            return;
        }
        var helpBtn = form.querySelector("#ajusteHelpBtn");
        var helpPanel = form.querySelector("#ajusteHelpPanel");
        var helpClose = form.querySelector("#ajusteHelpClose");
        if (!helpBtn || !helpPanel) {
            return;
        }
        form.dataset.ajusteHelpBound = "1";
        function closeAjusteHelp() {
            helpPanel.hidden = true;
            helpBtn.setAttribute("aria-expanded", "false");
        }
        function openAjusteHelp() {
            helpPanel.hidden = false;
            helpBtn.setAttribute("aria-expanded", "true");
        }
        function toggleAjusteHelp() {
            if (helpPanel.hidden) {
                openAjusteHelp();
            } else {
                closeAjusteHelp();
            }
        }
        helpBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            toggleAjusteHelp();
        });
        if (helpClose) {
            helpClose.addEventListener("click", closeAjusteHelp);
        }
        document.addEventListener("click", function (ev) {
            if (helpPanel.hidden) {
                return;
            }
            var t = ev.target;
            if (helpBtn.contains(t) || helpPanel.contains(t)) {
                return;
            }
            closeAjusteHelp();
        });
        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") {
                closeAjusteHelp();
            }
        });
    }

    function initAjusteAutoConsult(form) {
        if (!form || form.dataset.ajusteAutoConsultBound === "1") {
            return;
        }
        var codigo = form.querySelector("#codigo");
        var bodega = form.querySelector("#bodega");
        if (!codigo || !bodega) {
            return;
        }
        form.dataset.ajusteAutoConsultBound = "1";
        var marca = form.querySelector("#marca");
        var observacion = form.querySelector("#observacion");
        var nuevoStock = form.querySelector("#nuevo_stock");
        var baseUrl = (form.getAttribute("data-consult-url") || "/bodega/ajuste").split("?")[0];
        var timer = null;
        var lastKey = "";

        function syncKeyFromDom() {
            var c = (codigo.value || "").trim();
            var b = (bodega.value || "").trim();
            if (c && b) {
                lastKey = c.toUpperCase() + "|" + b;
            }
        }
        syncKeyFromDom();

        function buildQuery() {
            var p = new URLSearchParams();
            var c = (codigo.value || "").trim();
            if (c) {
                p.set("codigo", c);
            }
            if (bodega.value) {
                p.set("bodega", bodega.value);
            }
            if (marca && marca.value) {
                p.set("marca", marca.value.trim());
            }
            if (observacion && observacion.value) {
                p.set("observacion", observacion.value.trim());
            }
            if (nuevoStock && nuevoStock.getAttribute("name") && nuevoStock.value !== "") {
                p.set("nuevo_stock", nuevoStock.value);
            }
            return p.toString();
        }

        function consultNow() {
            var c = (codigo.value || "").trim();
            if (!c) {
                return;
            }
            var b = (bodega.value || "").trim();
            if (!b) {
                return;
            }
            var key = c.toUpperCase() + "|" + b;
            if (key === lastKey) {
                return;
            }
            lastKey = key;
            var qs = buildQuery();
            if (!qs) {
                return;
            }
            navigateBodegaConsult(baseUrl + "?" + qs);
        }

        function scheduleConsult() {
            if (timer) {
                clearTimeout(timer);
            }
            timer = setTimeout(function () {
                timer = null;
                consultNow();
            }, 480);
        }

        codigo.addEventListener("input", scheduleConsult);
        codigo.addEventListener("blur", function () {
            if (timer) {
                clearTimeout(timer);
                timer = null;
            }
            consultNow();
        });
        bodega.addEventListener("change", function () {
            lastKey = "";
            scheduleConsult();
        });
    }

    function initAjusteMultiRows(form) {
        if (!form) {
            return;
        }
        var body = form.querySelector("#ajusteVariantesBody");
        var btnAdd = form.querySelector("#ajusteAddVarianteRow");
        if (!body || !btnAdd) {
            return;
        }
        if (body.dataset.ajusteRowsBound === "1") {
            return;
        }
        body.dataset.ajusteRowsBound = "1";
        function bindRemove(tr) {
            var b = tr.querySelector(".btn-ajuste-remove-row");
            if (!b) {
                return;
            }
            b.addEventListener("click", function () {
                var rows = body.querySelectorAll("tr.ajuste-variante-row");
                if (rows.length <= 1) {
                    tr.querySelectorAll("input").forEach(function (inp) {
                        inp.value = "";
                    });
                    var tdAct = tr.querySelector(".ajuste-stock-act");
                    if (tdAct) {
                        tdAct.textContent = "—";
                    }
                    return;
                }
                tr.remove();
            });
        }
        body.querySelectorAll("tr.ajuste-variante-row").forEach(bindRemove);
        btnAdd.addEventListener("click", function () {
            var first = body.querySelector("tr.ajuste-variante-row");
            if (!first) {
                return;
            }
            var clone = first.cloneNode(true);
            clone.querySelectorAll("input").forEach(function (inp) {
                inp.value = "";
            });
            var tdAct = clone.querySelector(".ajuste-stock-act");
            if (tdAct) {
                tdAct.textContent = "—";
            }
            body.appendChild(clone);
            bindRemove(clone);
        });
    }

    /**
     * Buscar producto (API ventas) desde formularios de bodega: mismo flujo que Ajuste/Ingreso.
     * cfg: { datasetBound, modalId, openBtnId, closeBtnId, searchBtnId, searchInputId, statusId, resultsId, codigoSel? }
     */
    function initBodegaCodigoProductSearch(form, cfg) {
        if (!form || !cfg || form.dataset[cfg.datasetBound] === "1") {
            return;
        }
        var url = (form.getAttribute("data-product-search-url") || "").trim();
        var codigoSel = cfg.codigoSel || "#codigo";
        var codigo = form.querySelector(codigoSel);
        var modal = document.getElementById(cfg.modalId);
        if (!url || !codigo || !modal) {
            return;
        }
        form.dataset[cfg.datasetBound] = "1";
        var btnOpen = document.getElementById(cfg.openBtnId);
        var btnClose = document.getElementById(cfg.closeBtnId);
        var btnGo = document.getElementById(cfg.searchBtnId);
        var inpQ = document.getElementById(cfg.searchInputId);
        var statusEl = document.getElementById(cfg.statusId);
        var resultsEl = document.getElementById(cfg.resultsId);
        var marca = form.querySelector("#marca");
        var bodega = form.querySelector("#bodega");

        function closeModal() {
            modal.classList.remove("open");
            modal.setAttribute("aria-hidden", "true");
        }

        function openModal() {
            modal.classList.add("open");
            modal.setAttribute("aria-hidden", "false");
            if (statusEl) statusEl.textContent = "Escribe para buscar productos.";
            if (resultsEl) resultsEl.innerHTML = "";
            if (inpQ) {
                inpQ.value = "";
                setTimeout(function () {
                    inpQ.focus();
                }, 20);
            }
        }

        function applyItem(it) {
            if (!it) return;
            codigo.value = (it.codigo || "").toString().trim().toUpperCase();
            if (marca && (it.marca || "").trim()) {
                marca.value = (it.marca || "").trim().toUpperCase();
            }
            if (bodega && (it.bodega || "").trim()) {
                var want = (it.bodega || "").trim();
                var i;
                for (i = 0; i < bodega.options.length; i++) {
                    if (bodega.options[i].value === want) {
                        bodega.selectedIndex = i;
                        break;
                    }
                }
            }
            codigo.dispatchEvent(new Event("input", { bubbles: true }));
            if (marca) marca.dispatchEvent(new Event("input", { bubbles: true }));
            if (bodega) bodega.dispatchEvent(new Event("change", { bubbles: true }));
            closeModal();
        }

        function renderResults(items) {
            mountProductSearchResults(resultsEl, items, applyItem);
        }

        function doSearch() {
            if (!inpQ) return;
            var q = (inpQ.value || "").trim();
            if (q.length < 2) {
                if (statusEl) statusEl.textContent = "Escribe al menos 2 caracteres.";
                renderResults([]);
                return;
            }
            if (statusEl) statusEl.textContent = "Buscando...";
            fetch(url + "?q=" + encodeURIComponent(q) + "&limit=80", {
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) {
                    return res.json();
                })
                .then(function (data) {
                    var items = data && data.success && Array.isArray(data.items) ? data.items : [];
                    if (statusEl) statusEl.textContent = items.length + " resultado(s).";
                    renderResults(items);
                })
                .catch(function () {
                    if (statusEl) statusEl.textContent = "No se pudo buscar productos.";
                    renderResults([]);
                });
        }

        if (btnOpen) {
            btnOpen.addEventListener("click", function (e) {
                e.preventDefault();
                openModal();
            });
        }
        if (btnClose) {
            btnClose.addEventListener("click", closeModal);
        }
        modal.addEventListener("click", function (ev) {
            if (ev.target === modal) closeModal();
        });
        if (btnGo) {
            btnGo.addEventListener("click", doSearch);
        }
        if (inpQ) {
            inpQ.addEventListener("keydown", function (ev) {
                if (ev.key === "Enter") {
                    ev.preventDefault();
                    doSearch();
                }
                if (ev.key === "Escape") {
                    closeModal();
                }
            });
        }
        codigo.addEventListener("keydown", function (ev) {
            if (ev.key === "F2") {
                ev.preventDefault();
                openModal();
            }
        });
        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape" && modal.classList.contains("open")) {
                closeModal();
            }
        });
    }

    function initAjusteProductSearch(form) {
        initBodegaCodigoProductSearch(form, {
            datasetBound: "ajusteProductSearchBound",
            modalId: "ajusteProductModal",
            openBtnId: "ajusteCodigoSearchBtn",
            closeBtnId: "ajusteProductModalClose",
            searchBtnId: "ajusteProductSearchBtn",
            searchInputId: "ajusteProductSearchInput",
            statusId: "ajusteProductSearchStatus",
            resultsId: "ajusteProductResults"
        });
    }

    function initSalidaProductSearch(form) {
        initBodegaCodigoProductSearch(form, {
            datasetBound: "salidaProductSearchBound",
            modalId: "salidaProductModal",
            openBtnId: "salidaCodigoSearchBtn",
            closeBtnId: "salidaProductModalClose",
            searchBtnId: "salidaProductSearchBtn",
            searchInputId: "salidaProductSearchInput",
            statusId: "salidaProductSearchStatus",
            resultsId: "salidaProductResults"
        });
    }

    function initRecepcionProductSearch(form) {
        initBodegaCodigoProductSearch(form, {
            datasetBound: "recepcionProductSearchBound",
            modalId: "recepcionProductModal",
            openBtnId: "recepcionCodigoSearchBtn",
            closeBtnId: "recepcionProductModalClose",
            searchBtnId: "recepcionProductSearchBtn",
            searchInputId: "recepcionProductSearchInput",
            statusId: "recepcionProductSearchStatus",
            resultsId: "recepcionProductResults",
            codigoSel: "#codigo_producto"
        });
    }

    function initAjusteView(root) {
        var form = root.querySelector("#ajusteForm");
        if (!form) {
            return;
        }
        bindMarcaSugerenciasForm(form);
        initAjusteHelp(form);
        initAjusteProductSearch(form);
        initAjusteAutoConsult(form);
        initAjusteMultiRows(form);
        bindBodegaFormSpaSubmit(form);
    }

    function initSalidaAutoConsult(form) {
        if (!form || form.dataset.salidaAutoConsultBound === "1") {
            return;
        }
        var codigo = form.querySelector("#codigo");
        var bodega = form.querySelector("#bodega");
        if (!codigo || !bodega) {
            return;
        }
        form.dataset.salidaAutoConsultBound = "1";
        var marca = form.querySelector("#marca");
        var observacion = form.querySelector("#observacion");
        var cantidad = form.querySelector("#cantidad");
        var baseUrl = (form.getAttribute("data-consult-url") || "/bodega/salida").split("?")[0];
        var timer = null;
        var lastKey = "";

        function syncKeyFromDom() {
            var c = (codigo.value || "").trim();
            var b = (bodega.value || "").trim();
            var m = marca && marca.value ? marca.value.trim().toUpperCase() : "";
            if (c && b) {
                lastKey = c.toUpperCase() + "|" + b + "|" + m;
            }
        }
        syncKeyFromDom();

        function buildQuery() {
            var p = new URLSearchParams();
            var c = (codigo.value || "").trim();
            if (c) {
                p.set("codigo", c);
            }
            if (bodega.value) {
                p.set("bodega", bodega.value);
            }
            if (marca && marca.value) {
                p.set("marca", marca.value.trim());
            }
            if (observacion && observacion.value) {
                p.set("observacion", observacion.value.trim());
            }
            if (cantidad && cantidad.value !== "") {
                p.set("cantidad", cantidad.value);
            }
            return p.toString();
        }

        function consultNow() {
            var c = (codigo.value || "").trim();
            if (!c) {
                return;
            }
            var b = (bodega.value || "").trim();
            if (!b) {
                return;
            }
            var m = marca && marca.value ? marca.value.trim().toUpperCase() : "";
            var key = c.toUpperCase() + "|" + b + "|" + m;
            if (key === lastKey) {
                return;
            }
            lastKey = key;
            var qs = buildQuery();
            if (!qs) {
                return;
            }
            navigateBodegaConsult(baseUrl + "?" + qs);
        }

        function scheduleConsult() {
            if (timer) {
                clearTimeout(timer);
            }
            timer = setTimeout(function () {
                timer = null;
                consultNow();
            }, 480);
        }

        codigo.addEventListener("input", scheduleConsult);
        codigo.addEventListener("blur", function () {
            if (timer) {
                clearTimeout(timer);
                timer = null;
            }
            consultNow();
        });
        bodega.addEventListener("change", function () {
            lastKey = "";
            scheduleConsult();
        });
        if (marca) {
            marca.addEventListener("input", scheduleConsult);
            marca.addEventListener("blur", function () {
                if (timer) {
                    clearTimeout(timer);
                    timer = null;
                }
                consultNow();
            });
        }
    }

    function initSalidaView(root) {
        var form = root.querySelector("#salidaForm");
        bindMarcaSugerenciasForm(form);
        initSalidaProductSearch(form);
        initSalidaAutoConsult(form);
        bindBodegaFormSpaSubmit(form);
    }

    function initRecepcionHelp(form) {
        if (!form || form.dataset.recepcionHelpBound === "1") {
            return;
        }
        var helpBtn = form.querySelector("#recepcionHelpBtn");
        var helpPanel = form.querySelector("#recepcionHelpPanel");
        var helpClose = form.querySelector("#recepcionHelpClose");
        if (!helpBtn || !helpPanel) {
            return;
        }
        form.dataset.recepcionHelpBound = "1";
        function closeRecepcionHelp() {
            helpPanel.hidden = true;
            helpBtn.setAttribute("aria-expanded", "false");
        }
        function openRecepcionHelp() {
            helpPanel.hidden = false;
            helpBtn.setAttribute("aria-expanded", "true");
        }
        function toggleRecepcionHelp() {
            if (helpPanel.hidden) {
                openRecepcionHelp();
            } else {
                closeRecepcionHelp();
            }
        }
        helpBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            toggleRecepcionHelp();
        });
        if (helpClose) {
            helpClose.addEventListener("click", closeRecepcionHelp);
        }
        document.addEventListener("click", function (ev) {
            if (helpPanel.hidden) {
                return;
            }
            var t = ev.target;
            if (helpBtn.contains(t) || helpPanel.contains(t)) {
                return;
            }
            closeRecepcionHelp();
        });
        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") {
                closeRecepcionHelp();
            }
        });
    }

    function initRecepcionAutoConsult(form) {
        if (!form || form.dataset.recepcionAutoConsultBound === "1") {
            return;
        }
        var codigo =
            form.querySelector("#codigo_producto") || form.querySelector("input[name='codigo_producto']");
        var bodega = form.querySelector("#bodega");
        if (!codigo || !bodega) {
            return;
        }
        form.dataset.recepcionAutoConsultBound = "1";
        var proveedor = form.querySelector("#proveedor");
        var marca = form.querySelector("#marca");
        var observacion = form.querySelector("#observacion");
        var cantidad = form.querySelector("#cantidad");
        var baseUrl = (form.getAttribute("data-consult-url") || "/bodega/recepcion").split("?")[0];
        var timer = null;
        var lastKey = "";

        function syncKeyFromDom() {
            var c = (codigo.value || "").trim();
            var b = (bodega.value || "").trim();
            var m = marca && marca.value ? marca.value.trim().toUpperCase() : "";
            if (c && b) {
                lastKey = c.toUpperCase() + "|" + b + "|" + m;
            }
        }
        syncKeyFromDom();

        function buildQuery() {
            var p = new URLSearchParams();
            var c = (codigo.value || "").trim();
            if (c) {
                p.set("codigo_producto", c);
            }
            if (proveedor && proveedor.value) {
                p.set("proveedor", proveedor.value.trim());
            }
            if (bodega.value) {
                p.set("bodega", bodega.value);
            }
            if (marca && marca.value) {
                p.set("marca", marca.value.trim());
            }
            if (observacion && observacion.value) {
                p.set("observacion", observacion.value.trim());
            }
            if (cantidad && cantidad.value !== "") {
                p.set("cantidad", cantidad.value);
            }
            return p.toString();
        }

        function consultNow() {
            var c = (codigo.value || "").trim();
            if (!c) {
                return;
            }
            var b = (bodega.value || "").trim();
            if (!b) {
                return;
            }
            var m = marca && marca.value ? marca.value.trim().toUpperCase() : "";
            var key = c.toUpperCase() + "|" + b + "|" + m;
            if (key === lastKey) {
                return;
            }
            lastKey = key;
            var qs = buildQuery();
            if (!qs) {
                return;
            }
            navigateBodegaConsult(baseUrl + "?" + qs);
        }

        function scheduleConsult() {
            if (timer) {
                clearTimeout(timer);
            }
            timer = setTimeout(function () {
                timer = null;
                consultNow();
            }, 480);
        }

        codigo.addEventListener("input", scheduleConsult);
        codigo.addEventListener("blur", function () {
            if (timer) {
                clearTimeout(timer);
                timer = null;
            }
            consultNow();
        });
        bodega.addEventListener("change", function () {
            lastKey = "";
            scheduleConsult();
        });
        if (marca) {
            marca.addEventListener("input", scheduleConsult);
            marca.addEventListener("blur", function () {
                if (timer) {
                    clearTimeout(timer);
                    timer = null;
                }
                consultNow();
            });
        }
    }

    function initRecepcionView(root) {
        var form = root.querySelector("#recepcionForm");
        bindMarcaSugerenciasForm(form);
        initRecepcionHelp(form);
        initRecepcionProductSearch(form);
        initRecepcionAutoConsult(form);
        bindBodegaFormSpaSubmit(form);
    }

    window.initBodegaUI = function initBodegaUI() {
        /* SPA: scripts run after innerHTML but before pushState; pathname can still be "/" */
        var path = location.pathname || "";
        var onBodegaRoute = /^\/bodega(\/|$)/.test(path);
        var hasBodegaFragment =
            document.querySelector(
                "#salidaForm, #ingresoForm, #ajusteForm, #recepcionForm, #labelsForm, #bodegasCatalogoHelpBtn"
            ) != null;
        if (!onBodegaRoute && !hasBodegaFragment) {
            return;
        }
        var root = document;
        initDateInputs(root);
        initRutBindings(root);
        initIngresoView(root);
        initAjusteView(root);
        initSalidaView(root);
        initRecepcionView(root);
        initEtiquetasView(root);
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", window.initBodegaUI);
    } else {
        window.initBodegaUI();
    }
    document.addEventListener("app:module-loaded", window.initBodegaUI);
})();
