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

    function initIngresoView(root) {
        var form = root.querySelector("#ingresoForm");
        if (!form || form.dataset.bodegaUiBound === "1") return;
        form.dataset.bodegaUiBound = "1";

        var itemsBody = form.querySelector("#itemsBody");
        var addRowBtn = form.querySelector("#btnAddRow");
        var rutInput = form.querySelector("#supplier_rut");
        var btnBuscar = form.querySelector("#btnBuscarProveedor");
        var rutStatus = form.querySelector("#rutStatus");
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

        function collapseSupplierSection() {
            supplierFormWrap.classList.add("is-collapsed");
            supplierSummary.classList.add("is-visible");
            if (supplierCard) {
                supplierCard.classList.add("ingreso-supplier-card--hidden");
                supplierCard.style.display = "none";
            }
        }

        function expandSupplierSection() {
            supplierFormWrap.classList.remove("is-collapsed");
            supplierSummary.classList.remove("is-visible");
            if (supplierCard) {
                supplierCard.classList.remove("ingreso-supplier-card--hidden");
                supplierCard.style.display = "";
            }
        }

        function hideSupplierRegistrationCard() {
            supplierSummary.classList.remove("is-visible");
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
                var valorUnitario = parseValorNetoLine(inpValor ? inpValor.value : "");
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

        function clearMarcaSuggestions(row) {
            var dl = row.querySelector("datalist.ingreso-marca-datalist");
            var hint = row.querySelector(".ingreso-marca-hint");
            if (dl) {
                dl.innerHTML = "";
            }
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

        function loadMarcasForRow(row) {
            if (!marcasUrl) {
                return;
            }
            var codeInput = row.querySelector("input[name='codigo_producto[]']");
            var dl = row.querySelector("datalist.ingreso-marca-datalist");
            var marcaInput = row.querySelector("input.ingreso-marca-input");
            var hint = row.querySelector(".ingreso-marca-hint");
            if (!codeInput || !dl || !marcaInput) {
                return;
            }
            var code = (codeInput.value || "").trim().toUpperCase();
            if (!code) {
                clearMarcaSuggestions(row);
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
                    dl.innerHTML = "";
                    data.marcas.forEach(function (m) {
                        var o = document.createElement("option");
                        o.value = m;
                        dl.appendChild(o);
                    });
                    if (hint) {
                        var regs = Array.isArray(data.marcas_registradas) ? data.marcas_registradas : [];
                        if (regs.length > 0) {
                            hint.textContent =
                                "Variantes ya registradas para este código: " +
                                regs.length +
                                " (aparecen primero). También podés escribir una nueva.";
                        } else {
                            hint.textContent =
                                "Sin variantes previas para este código: elegí una sugerencia o escribí una nueva.";
                        }
                        hint.style.display = "block";
                    }
                })
                .catch(function () {
                    clearMarcaSuggestions(row);
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
            var vn = parseChileFloat((vnInp && vnInp.value) || "");
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
            var vnStr = (vnInp.value || "").trim();
            if (
                vnStr &&
                !isNaN(prev) &&
                prev > 0 &&
                newC > 0 &&
                prev !== newC
            ) {
                var vn = parseChileFloat(vnStr);
                if (vn != null && vn > 0) {
                    var scaled = vn * (newC / prev);
                    vnInp.value = String(Math.round(scaled * 100) / 100);
                }
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
            var vn = parseChileFloat((vnInp && vnInp.value) || "");
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

        function nextIngresoMarcaDlId() {
            return "ingreso-marca-dl-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
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
            var dlId = nextIngresoMarcaDlId();
            var dlMarca = document.createElement("datalist");
            dlMarca.className = "ingreso-marca-datalist";
            dlMarca.id = dlId;
            marcaStack.appendChild(dlMarca);
            var inpMarca = document.createElement("input");
            inpMarca.name = "marca_producto[]";
            inpMarca.className = "ingreso-marca-input";
            inpMarca.placeholder = "Marca / variante";
            inpMarca.setAttribute("autocomplete", "off");
            inpMarca.setAttribute("aria-label", "Marca o variante");
            inpMarca.setAttribute("list", dlId);
            marcaStack.appendChild(inpMarca);
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
            if (!productResults) return;
            productResults.innerHTML = "";
            if (!Array.isArray(items) || items.length === 0) {
                productResults.innerHTML = '<div class="history-empty" style="border:none;background:#fff;padding:14px;">Sin resultados.</div>';
                return;
            }
            function esc(v) {
                return String(v == null ? "" : v)
                    .replace(/&/g, "&amp;")
                    .replace(/</g, "&lt;")
                    .replace(/>/g, "&gt;")
                    .replace(/"/g, "&quot;")
                    .replace(/'/g, "&#39;");
            }
            items.forEach(function (it) {
                var row = document.createElement("div");
                row.className = "ingreso-product-item";
                row.innerHTML =
                    '<div><strong>' + esc(it.codigo || "") + "</strong></div>" +
                    '<div><div>' + esc(it.descripcion || "") + '</div><div class="ingreso-product-meta">' + esc(it.modelo || "—") + "</div></div>" +
                    '<div>' + esc(((it.marca || "").trim() || "—")) + "</div>" +
                    '<div>' + (parseInt(it.variant_stock || it.stock || 0, 10) || 0) + "</div>" +
                    '<div><button type="button" class="btn btn-primary btn-sm">Seleccionar</button></div>';
                var btnSel = row.querySelector("button");
                if (btnSel) {
                    btnSel.addEventListener("click", function () {
                        applyProductToRow(currentProductSearchRow, it);
                        closeProductSearchModal();
                    });
                }
                productResults.appendChild(row);
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

        form.addEventListener("submit", function (ev) {
            if (form.dataset.ingresoSubmitting === "1") {
                form.dataset.ingresoSubmitting = "0";
                return;
            }
            if (!marcasUrl) {
                return;
            }
            ev.preventDefault();
            var vpCheck = validateMargenYPrecioVentaIngresoRows();
            if (!vpCheck.ok) {
                window.alert(vpCheck.message);
                if (vpCheck.focusEl) {
                    vpCheck.focusEl.focus();
                }
                return;
            }
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
                form.dataset.ingresoSubmitting = "1";
                form.submit();
                return;
            }
            Promise.all(promises).then(function (results) {
                var ok = results.every(function (r) {
                    return r !== false;
                });
                if (!ok) {
                    window.alert(
                        "Hay códigos internos que no existen en el catálogo o están inactivos. Revisá los campos en rojo."
                    );
                    var firstBad = itemsBody.querySelector("input.ingreso-codigo-interno-invalido");
                    if (firstBad) {
                        firstBad.focus();
                    }
                    return;
                }
                form.dataset.ingresoSubmitting = "1";
                form.submit();
            });
        });

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
            var previewPdf = document.getElementById("ingresoFacturaPreviewPdf");
            var previewPdfName = document.getElementById("ingresoFacturaPreviewPdfName");
            var statusEl = document.getElementById("ingresoFacturaScanStatus");
            var extractedBox = document.getElementById("ingresoFacturaExtracted");
            var extractedList = document.getElementById("ingresoFacturaExtractedList");
            var autoBadge = document.getElementById("ingresoFacturaAutoBadge");
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
                modal.classList.add("open");
                modal.setAttribute("aria-hidden", "false");
            }

            function closeModal() {
                modal.classList.remove("open");
                modal.setAttribute("aria-hidden", "true");
            }

            function setStatus(msg, kind) {
                if (!statusEl) return;
                statusEl.textContent = msg || "";
                statusEl.classList.remove("is-loading", "is-error", "muted");
                if (kind === "loading") statusEl.classList.add("is-loading");
                else if (kind === "error") statusEl.classList.add("is-error");
                else statusEl.classList.add("muted");
            }

            function resetPreview() {
                if (previewImg) {
                    previewImg.hidden = true;
                    previewImg.removeAttribute("src");
                }
                if (previewPdf) previewPdf.hidden = true;
                if (extractedBox) extractedBox.hidden = true;
                if (extractedList) extractedList.innerHTML = "";
                if (btnApply) btnApply.disabled = true;
                extractedData = null;
            }

            function showFilePreview(file) {
                resetPreview();
                pendingFile = file;
                pendingMediaType = (file.type || "image/jpeg").toLowerCase();
                if (pendingMediaType === "image/jpg") pendingMediaType = "image/jpeg";

                if (pendingMediaType === "application/pdf") {
                    if (previewImg) previewImg.hidden = true;
                    if (previewPdf) {
                        previewPdf.hidden = false;
                        if (previewPdfName) previewPdfName.textContent = file.name || "documento.pdf";
                    }
                } else if (previewImg) {
                    previewImg.hidden = false;
                    try {
                        previewImg.src = URL.createObjectURL(file);
                    } catch (e) {
                        previewImg.removeAttribute("src");
                    }
                    if (previewPdf) previewPdf.hidden = true;
                }
                setStatus("Archivo listo. Presioná «Analizar factura».", null);
                openModal();
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

            function renderExtractedPreview(data) {
                if (!extractedList || !extractedBox) return;
                extractedList.innerHTML = "";
                dlRow("RUT proveedor", data.rut_proveedor);
                dlRow("N° documento", data.numero_documento);
                dlRow("Fecha", data.fecha);
                dlRow("Método de pago", data.metodo_pago);
                if (data.total != null) dlRow("Total", data.total);
                if (data.iva != null) dlRow("IVA", data.iva);
                var prods = data.productos || [];
                if (prods.length) {
                    dlRow("Productos", prods.length + " línea(s)");
                    prods.slice(0, 8).forEach(function (p, i) {
                        var desc = (p.descripcion || p.codigo_proveedor || "Ítem " + (i + 1));
                        var extra = [];
                        if (p.cantidad != null) extra.push("cant. " + p.cantidad);
                        if (p.valor_neto != null) extra.push("neto " + p.valor_neto);
                        dlRow("  · " + desc, extra.join(" · ") || "—");
                    });
                    if (prods.length > 8) {
                        dlRow("…", "+" + (prods.length - 8) + " más");
                    }
                }
                extractedBox.hidden = false;
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

            function fillIngresoRowFromProduct(row, prod) {
                if (!row || !prod) return 0;
                var n = 0;
                var inpProv = row.querySelector("input[name='codigo_proveedor_producto[]']");
                var inpCant = row.querySelector("input[name='cantidad_producto[]']");
                var inpVN = row.querySelector("input[name='valor_neto_producto[]']");
                var inpPV = row.querySelector("input.ingreso-precio-venta-input");
                var inpNota = row.querySelector("input[name='nota_producto[]']");
                if (inpProv && prod.codigo_proveedor) {
                    inpProv.value = String(prod.codigo_proveedor).trim();
                    n++;
                }
                if (inpCant && prod.cantidad != null && prod.cantidad !== "") {
                    inpCant.value = String(Math.max(1, parseInt(prod.cantidad, 10) || 1));
                    n++;
                }
                if (inpVN && prod.valor_neto != null && prod.valor_neto !== "") {
                    inpVN.value = formatMontoInput(prod.valor_neto);
                    n++;
                }
                if (inpPV && prod.precio_venta != null && prod.precio_venta !== "") {
                    inpPV.value = formatMontoInput(prod.precio_venta);
                    n++;
                }
                if (inpNota && prod.descripcion) {
                    inpNota.value = String(prod.descripcion).trim();
                    n++;
                }
                return n;
            }

            function markAutoFilled(el) {
                if (!el) return;
                el.classList.add("ingreso-auto-filled");
                window.setTimeout(function () {
                    el.classList.remove("ingreso-auto-filled");
                }, 4000);
            }

            function applyExtractedData(data) {
                if (!data) return 0;
                var count = 0;

                if (data.rut_proveedor && rutInput) {
                    var rutRaw = String(data.rut_proveedor).trim();
                    rutInput.value =
                        window.RutUtils && window.RutUtils.format
                            ? window.RutUtils.format(rutRaw)
                            : rutRaw;
                    markAutoFilled(rutInput);
                    searchSupplier(true);
                    count++;
                }
                if (data.numero_documento && numeroDocInput) {
                    numeroDocInput.value = String(data.numero_documento).trim();
                    markAutoFilled(numeroDocInput);
                    count++;
                }
                if (data.fecha && fechaInput) {
                    var fv = fechaToInputValue(data.fecha);
                    if (fv) {
                        fechaInput.value = fv;
                        markAutoFilled(fechaInput);
                        count++;
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
                if (inpTotalFactura && data.total != null && data.total !== "") {
                    inpTotalFactura.value = formatMontoInput(data.total);
                    markAutoFilled(inpTotalFactura);
                    count++;
                }

                var productos = Array.isArray(data.productos) ? data.productos : [];
                if (productos.length) {
                    var rows = itemsBody.querySelectorAll("tr.item-row");
                    productos.forEach(function (prod, idx) {
                        var row;
                        if (idx === 0 && rows[0] && isIngresoRowEmpty(rows[0])) {
                            row = rows[0];
                        } else {
                            addRow();
                            var all = itemsBody.querySelectorAll("tr.item-row");
                            row = all[all.length - 1];
                        }
                        count += fillIngresoRowFromProduct(row, prod);
                    });
                    refreshIngresoItemNumbers();
                    updateIngresoTotals();
                }

                if (autoBadge) {
                    if (count > 0) {
                        autoBadge.hidden = false;
                        autoBadge.textContent = "✅ " + count + " campo(s) completado(s) automáticamente";
                    } else {
                        autoBadge.hidden = true;
                        autoBadge.textContent = "";
                    }
                }
                return count;
            }

            function runAnalyze() {
                if (!pendingFile) {
                    setStatus("Seleccioná una imagen primero.", "error");
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
                        extractedData = pack.body.data || {};
                        renderExtractedPreview(extractedData);
                        if (btnApply) btnApply.disabled = false;
                        setStatus("Análisis listo. Revisá los datos y presioná «Aplicar datos».", null);
                    })
                    .catch(function (err) {
                        console.log("analizar-factura error", err);
                        setStatus(err.message || "Error al analizar la factura.", "error");
                    })
                    .finally(function () {
                        if (btnAnalyze) btnAnalyze.disabled = false;
                    });
            }

            btnScan.addEventListener("click", function () {
                fileInput.value = "";
                fileInput.click();
            });

            fileInput.addEventListener("change", function () {
                var f = fileInput.files && fileInput.files[0];
                if (!f) return;
                var okType =
                    /^image\/(jpeg|png|webp)$/i.test(f.type) ||
                    /\.(jpe?g|png|webp)$/i.test(f.name || "");
                if (!okType) {
                    setStatus("Formato no válido. Use JPG, PNG o WEBP.", "error");
                    openModal();
                    return;
                }
                if (f.size > 12 * 1024 * 1024) {
                    setStatus("El archivo supera 12 MB.", "error");
                    openModal();
                    return;
                }
                showFilePreview(f);
            });

            if (btnAnalyze) btnAnalyze.addEventListener("click", runAnalyze);
            if (btnApply) {
                btnApply.addEventListener("click", function () {
                    if (!extractedData) return;
                    applyExtractedData(extractedData);
                    setStatus("Datos aplicados al formulario.", null);
                    closeModal();
                });
            }
            function cancelScan() {
                closeModal();
                pendingFile = null;
                pendingBase64 = "";
                resetPreview();
            }
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
        var labelSearchResults = root.querySelector("#label-search-results");
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

        function ensureThermalPageStyle(enabled) {
            var existing = document.getElementById(thermalStyleId);
            if (!enabled) {
                if (existing) existing.remove();
                return;
            }
            if (existing) return;
            var style = document.createElement("style");
            style.id = thermalStyleId;
            style.textContent = "@media print { @page { size: 60mm 40mm; margin: 0; } }";
            document.head.appendChild(style);
        }

        function optimizePrintGrid() {
            var mode = document.documentElement.getAttribute("data-print-mode") || "a4";
            if (mode === "thermal") {
                document.documentElement.style.setProperty("--print-label-width", "60mm");
                document.documentElement.style.setProperty("--print-col-gap", "0");
                document.documentElement.style.setProperty("--print-row-gap", "0");
                document.documentElement.style.setProperty("--print-justify", "start");
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
            var normalized = mode === "thermal" ? "thermal" : "a4";
            document.documentElement.setAttribute("data-print-mode", normalized);
            if (printModeSelect) printModeSelect.value = normalized;
            ensureThermalPageStyle(normalized === "thermal");
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
        if (labelSearchInput) labelSearchInput.addEventListener("input", debounceProductSearch);
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
            window.location.href = baseUrl + "?" + qs;
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

        function esc(v) {
            return String(v == null ? "" : v)
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#39;");
        }

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
            if (!resultsEl) return;
            resultsEl.innerHTML = "";
            if (!Array.isArray(items) || items.length === 0) {
                resultsEl.innerHTML =
                    '<div class="history-empty" style="border:none;background:#fff;padding:14px;">Sin resultados.</div>';
                return;
            }
            items.forEach(function (it) {
                var row = document.createElement("div");
                row.className = "ingreso-product-item";
                row.innerHTML =
                    '<div><strong>' +
                    esc(it.codigo || "") +
                    "</strong></div>" +
                    '<div><div>' +
                    esc(it.descripcion || "") +
                    '</div><div class="ingreso-product-meta">' +
                    esc(it.modelo || "—") +
                    "</div></div>" +
                    "<div>" +
                    esc(((it.marca || "").trim() || "—")) +
                    "</div>" +
                    "<div>" +
                    (parseInt(it.variant_stock || it.stock || 0, 10) || 0) +
                    "</div>" +
                    '<div><button type="button" class="btn btn-primary btn-sm">Seleccionar</button></div>';
                var btn = row.querySelector("button");
                if (btn) {
                    btn.addEventListener("click", function () {
                        applyItem(it);
                    });
                }
                resultsEl.appendChild(row);
            });
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
        bindMarcaSugerenciasForm(form);
        initAjusteHelp(form);
        initAjusteProductSearch(form);
        initAjusteAutoConsult(form);
        initAjusteMultiRows(form);
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
            window.location.href = baseUrl + "?" + qs;
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
            window.location.href = baseUrl + "?" + qs;
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
