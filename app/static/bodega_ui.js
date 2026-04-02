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
        rutInput.readOnly = false;
        rutInput.disabled = false;

        function collapseSupplierSection() {
            supplierFormWrap.classList.add("is-collapsed");
            supplierSummary.classList.add("is-visible");
            if (supplierCard) supplierCard.style.display = "none";
        }

        function expandSupplierSection() {
            supplierFormWrap.classList.remove("is-collapsed");
            supplierSummary.classList.remove("is-visible");
            if (supplierCard) supplierCard.style.display = "";
        }

        function updateSupplierSummary() {
            var rutFormatted = (rutInput.value || "").trim() || "Sin RUT";
            var name = (supplierNameInput.value || "").trim() || "Proveedor sin nombre";
            supplierSummaryName.textContent = name;
            supplierSummaryMeta.textContent = "RUT: " + rutFormatted;
        }

        function addRow() {
            var tr = document.createElement("tr");
            tr.className = "item-row";
            tr.innerHTML = "" +
                "<td><input name='codigo_producto[]' placeholder='Codigo' required></td>" +
                "<td><input name='marca_producto[]' placeholder='Marca'></td>" +
                "<td><input name='bodega_producto[]' value='Bodega 1' placeholder='Bodega'></td>" +
                "<td><input name='cantidad_producto[]' type='number' min='1' step='1' required></td>" +
                "<td><input name='nota_producto[]' placeholder='Nota opcional'></td>" +
                "<td><button type='button' class='btn btn-warn btnRemove btn-compact'>Quitar</button></td>";
            itemsBody.appendChild(tr);
        }

        function bindRemove(button) {
            if (!button || button.dataset.bound === "1") return;
            button.dataset.bound = "1";
            button.addEventListener("click", function () {
                var rows = itemsBody.querySelectorAll(".item-row");
                if (rows.length <= 1) {
                    var inputs = rows[0].querySelectorAll("input");
                    inputs.forEach(function (input, idx) {
                        input.value = idx === 2 ? "Bodega 1" : "";
                    });
                    return;
                }
                var row = button.closest("tr");
                if (row) row.remove();
            });
        }

        function fillSupplier(p) {
            form.querySelector("#supplier_name").value = p.name || "";
            form.querySelector("#supplier_giro").value = p.giro || "";
            form.querySelector("#supplier_email").value = p.email || "";
            form.querySelector("#supplier_address").value = p.address || "";
            form.querySelector("#supplier_comuna").value = p.comuna || "";
            form.querySelector("#supplier_region").value = p.region || "";
            form.querySelector("#supplier_country").value = p.country || "Chile";
        }

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

        rutInput.addEventListener("input", function () {
            rutInput.readOnly = false;
            rutInput.disabled = false;
            expandSupplierSection();
            updateSupplierSummary();
            rutStatus.style.color = "#64748b";
            rutStatus.textContent = "Ingresa el RUT para autocompletar proveedor.";
            scheduleAutoLookup();
        });
        rutInput.addEventListener("blur", function () {
            scheduleAutoLookup();
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

        var observer = new MutationObserver(function () {
            itemsBody.querySelectorAll(".btnRemove").forEach(bindRemove);
        });
        observer.observe(itemsBody, { childList: true, subtree: true });

        if ((supplierNameInput.value || "").trim() && (rutInput.value || "").trim()) {
            updateSupplierSummary();
            collapseSupplierSection();
        }
    }

    function initEtiquetasView(root) {
        var labelsForm = root.querySelector("#labelsForm");
        if (!labelsForm || labelsForm.dataset.bodegaUiBound === "1") return;
        labelsForm.dataset.bodegaUiBound = "1";

        var codigosInput = root.querySelector("#codigos");
        var fpInput = root.querySelector("#fp");
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

        function renderLabels(labels) {
            if (!sheet) return;
            if (!labels || !labels.length) {
                sheet.innerHTML = "";
                return;
            }
            sheet.innerHTML = labels.map(function (label) {
                return "<article class='label'>" +
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
            var params = new URLSearchParams({ ajax: "1", codigos: codigos, fp: fp });
            fetch(previewUrl + "?" + params.toString(), {
                method: "GET",
                headers: { "X-Requested-With": "XMLHttpRequest" }
            })
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data && data.labels) renderLabels(data.labels);
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

    window.initBodegaUI = function initBodegaUI() {
        if (!location.pathname.startsWith("/bodega")) return;
        var root = document;
        initDateInputs(root);
        initRutBindings(root);
        initIngresoView(root);
        initEtiquetasView(root);
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", window.initBodegaUI);
    } else {
        window.initBodegaUI();
    }
    document.addEventListener("app:module-loaded", window.initBodegaUI);
})();
