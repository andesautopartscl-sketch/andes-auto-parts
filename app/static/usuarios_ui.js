/* global fetch */
(function () {
    "use strict";

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function initOptionsUsersModal() {
        var modal = document.getElementById("optionsModal");
        if (!modal) return;
        if (modal.dataset.usuariosUiBound === "1") return;
        modal.dataset.usuariosUiBound = "1";

        var apiUsersUrl = modal.getAttribute("data-usuarios-api-url") || "/users";
        var apiRolesUrl = modal.getAttribute("data-roles-api-url") || "";
        var apiGeoUrl = modal.getAttribute("data-geo-api-url") || "";
        var apiCreateUrl = modal.getAttribute("data-usuarios-crear-url") || "";
        var apiGetUserUrlTemplate = modal.getAttribute("data-usuarios-get-url-template") || "";
        var apiToggleUserUrlTemplate = modal.getAttribute("data-usuarios-toggle-url-template") || "";
        var apiUnlockUserUrlTemplate = modal.getAttribute("data-usuarios-unlock-url-template") || "";
        var apiDeleteUserUrlTemplate = modal.getAttribute("data-usuarios-delete-url-template") || "";
        var apiPasswordResetUrl = modal.getAttribute("data-password-reset-api-url") || "";
        var apiPasswordResetResolveTemplate = modal.getAttribute("data-password-reset-resolve-url-template") || "";

        var statusNode = document.getElementById("optionsUsersStatus");
        var countNode = document.getElementById("optionsUsersCount");
        var activeCountNode = document.getElementById("optionsUsersActiveCount");
        var inactiveCountNode = document.getElementById("optionsUsersInactiveCount");

        var usersListCache = [];
        var passwordResetCache = [];
        var chileGeoCache = [];

        var optionsUsersLoaded = false;
        var isBusy = false;
        var usuariosGlobal = [];

        function updateStatus(message, isError) {
            if (!statusNode) return;
            statusNode.textContent = message;
            statusNode.style.color = isError ? "#b91c1c" : "#64748b";
        }

        function fillUrlTemplate(template, id) {
            if (!template) return "";
            return template.replace(/\/0\/?$/, "/" + id);
        }

        function setCreateMsg(message, kind) {
            var msg = document.getElementById("optionsCreateMsg");
            if (!msg) return;
            msg.className = "submodal-msg" + (kind === "error" ? " is-error" : kind === "success" ? " is-success" : "");
            msg.textContent = message || "";
        }

        async function loadRolesIntoCreateSelect() {
            var select = document.getElementById("optionsCreateRolSelect");
            if (!select) return;
            if (!apiRolesUrl) {
                select.innerHTML = "<option value=''>Sin roles</option>";
                return;
            }
            select.innerHTML = "<option value=''>Cargando roles...</option>";
            try {
                var res = await fetch(apiRolesUrl, {
                    headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                    credentials: "same-origin"
                });
                if (!res.ok) throw new Error("No fue posible cargar roles");
                var roles = await res.json();
                if (!Array.isArray(roles)) roles = [];
                select.innerHTML = "<option value=''>Selecciona un rol</option>" + roles.map(function (r) {
                    var label = escapeHtml(r.nombre || "Rol");
                    var nivel = (r.nivel != null && r.nivel !== "") ? (" (Nivel " + escapeHtml(String(r.nivel)) + ")") : "";
                    var desc = r.descripcion ? (" - " + escapeHtml(String(r.descripcion))) : "";
                    return "<option value='" + escapeHtml(String(r.id)) + "'>" + label + nivel + desc + "</option>";
                }).join("");
            } catch (e) {
                select.innerHTML = "<option value=''>Sin roles</option>";
            }
        }

        function fillCreateComunas(regionName, selectedComuna) {
            var comunaSelect = document.getElementById("optionsCreateComunaSelect");
            if (!comunaSelect) return;
            var regionData = chileGeoCache.find(function (item) {
                return item && String(item.nombre || "").toLowerCase() === String(regionName || "").toLowerCase();
            });
            var comunas = regionData && Array.isArray(regionData.comunas) ? regionData.comunas : [];
            if (!regionName) {
                comunaSelect.innerHTML = "<option value=''>Selecciona región primero</option>";
                comunaSelect.disabled = true;
                return;
            }
            var options = "<option value=''>Seleccionar ciudad/comuna</option>" + comunas.map(function (c) {
                var name = String(c || "").trim();
                var selected = selectedComuna && name === selectedComuna ? " selected" : "";
                return "<option value='" + escapeHtml(name) + "'" + selected + ">" + escapeHtml(name) + "</option>";
            }).join("");
            comunaSelect.innerHTML = options;
            comunaSelect.disabled = comunas.length === 0;
        }

        async function loadCreateGeoSelectors() {
            var regionSelect = document.getElementById("optionsCreateRegionSelect");
            var comunaSelect = document.getElementById("optionsCreateComunaSelect");
            if (!regionSelect || !comunaSelect) return;
            if (!apiGeoUrl) {
                regionSelect.innerHTML = "<option value=''>Sin regiones</option>";
                comunaSelect.innerHTML = "<option value=''>Sin ciudades</option>";
                comunaSelect.disabled = true;
                return;
            }
            regionSelect.innerHTML = "<option value=''>Cargando regiones...</option>";
            try {
                if (!Array.isArray(chileGeoCache) || !chileGeoCache.length) {
                    var res = await fetch(apiGeoUrl, {
                        headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                        credentials: "same-origin"
                    });
                    if (!res.ok) throw new Error("No fue posible cargar regiones");
                    var geo = await res.json();
                    chileGeoCache = Array.isArray(geo) ? geo : [];
                }
                regionSelect.innerHTML = "<option value=''>Seleccionar región</option>" + chileGeoCache.map(function (r) {
                    var nombre = String((r && r.nombre) || "").trim();
                    return "<option value='" + escapeHtml(nombre) + "'>" + escapeHtml(nombre) + "</option>";
                }).join("");
                fillCreateComunas("", "");
            } catch (e) {
                regionSelect.innerHTML = "<option value=''>Sin regiones</option>";
                comunaSelect.innerHTML = "<option value=''>Sin ciudades</option>";
                comunaSelect.disabled = true;
            }
        }

        function inferLocationFromAddress(address) {
            var value = String(address || "").trim();
            if (!value || !Array.isArray(chileGeoCache) || !chileGeoCache.length) {
                return { direccion: value, comuna: "", region: "" };
            }
            var parts = value.split(",").map(function (p) { return p.trim(); }).filter(Boolean);
            if (!parts.length) return { direccion: "", comuna: "", region: "" };

            var region = "";
            var comuna = "";
            var direccion = value;

            for (var i = parts.length - 1; i >= 0; i--) {
                var regionCandidate = parts[i];
                var regionMatch = chileGeoCache.find(function (r) {
                    return String((r && r.nombre) || "").toLowerCase() === regionCandidate.toLowerCase();
                });
                if (regionMatch) {
                    region = regionMatch.nombre || "";
                    var beforeRegion = parts.slice(0, i);
                    if (beforeRegion.length) {
                        var comunaCandidate = beforeRegion[beforeRegion.length - 1];
                        var comunaMatch = (regionMatch.comunas || []).find(function (c) {
                            return String(c || "").toLowerCase() === comunaCandidate.toLowerCase();
                        });
                        if (comunaMatch) {
                            comuna = comunaMatch;
                            direccion = beforeRegion.slice(0, -1).join(", ").trim();
                        } else {
                            direccion = beforeRegion.join(", ").trim();
                        }
                    } else {
                        direccion = "";
                    }
                    break;
                }
            }

            return {
                direccion: direccion || "",
                comuna: comuna || "",
                region: region || ""
            };
        }

        function fillEditComunas(regionName, selectedComuna) {
            var comunaSelect = document.getElementById("optionsEditComunaSelect");
            if (!comunaSelect) return;
            var regionData = chileGeoCache.find(function (item) {
                return item && String(item.nombre || "").toLowerCase() === String(regionName || "").toLowerCase();
            });
            var comunas = regionData && Array.isArray(regionData.comunas) ? regionData.comunas : [];
            if (!regionName) {
                comunaSelect.innerHTML = "<option value=''>Selecciona región primero</option>";
                comunaSelect.disabled = true;
                return;
            }
            comunaSelect.innerHTML = "<option value=''>Seleccionar ciudad/comuna</option>" + comunas.map(function (c) {
                var name = String(c || "").trim();
                var selected = selectedComuna && name === selectedComuna ? " selected" : "";
                return "<option value='" + escapeHtml(name) + "'" + selected + ">" + escapeHtml(name) + "</option>";
            }).join("");
            comunaSelect.disabled = comunas.length === 0;
        }

        async function loadEditGeoSelectors(selectedRegion, selectedComuna) {
            var regionSelect = document.getElementById("optionsEditRegionSelect");
            var comunaSelect = document.getElementById("optionsEditComunaSelect");
            if (!regionSelect || !comunaSelect) return;
            if (!apiGeoUrl) {
                regionSelect.innerHTML = "<option value=''>Sin regiones</option>";
                comunaSelect.innerHTML = "<option value=''>Sin ciudades</option>";
                comunaSelect.disabled = true;
                return;
            }
            try {
                if (!Array.isArray(chileGeoCache) || !chileGeoCache.length) {
                    var res = await fetch(apiGeoUrl, {
                        headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                        credentials: "same-origin"
                    });
                    if (!res.ok) throw new Error("No fue posible cargar regiones");
                    var geo = await res.json();
                    chileGeoCache = Array.isArray(geo) ? geo : [];
                }
                regionSelect.innerHTML = "<option value=''>Seleccionar región</option>" + chileGeoCache.map(function (r) {
                    var nombre = String((r && r.nombre) || "").trim();
                    var selected = selectedRegion && nombre === selectedRegion ? " selected" : "";
                    return "<option value='" + escapeHtml(nombre) + "'" + selected + ">" + escapeHtml(nombre) + "</option>";
                }).join("");
                fillEditComunas(selectedRegion || "", selectedComuna || "");
            } catch (e) {
                regionSelect.innerHTML = "<option value=''>Sin regiones</option>";
                comunaSelect.innerHTML = "<option value=''>Sin ciudades</option>";
                comunaSelect.disabled = true;
            }
        }

        function openCreateUserModal() {
            var sub = document.getElementById("optionsUserCreateModal");
            var form = document.getElementById("optionsUserCreateForm");
            if (!sub || !form) return;
            sub.classList.add("is-open");
            sub.setAttribute("aria-hidden", "false");
            setCreateMsg("", null);
            form.reset();
            loadRolesIntoCreateSelect();
            loadCreateGeoSelectors();
            var first = form.querySelector("input[name='nombre']");
            if (first) setTimeout(function () { first.focus(); }, 10);
        }

        function closeCreateUserModal() {
            var sub = document.getElementById("optionsUserCreateModal");
            if (!sub) return;
            sub.classList.remove("is-open");
            sub.setAttribute("aria-hidden", "true");
            setCreateMsg("", null);
        }

        function setEditMsg(message, kind) {
            var msg = document.getElementById("optionsEditMsg");
            if (!msg) return;
            msg.className = "submodal-msg" + (kind === "error" ? " is-error" : kind === "success" ? " is-success" : "");
            msg.textContent = message || "";
        }

        async function loadRolesIntoEditSelect(selectedId) {
            var select = document.getElementById("optionsEditRolSelect");
            if (!select) return;
            if (!apiRolesUrl) {
                select.innerHTML = "<option value=''>Sin roles</option>";
                return;
            }
            select.innerHTML = "<option value=''>Cargando roles...</option>";
            try {
                var res = await fetch(apiRolesUrl, {
                    headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                    credentials: "same-origin"
                });
                if (!res.ok) throw new Error("No fue posible cargar roles");
                var roles = await res.json();
                if (!Array.isArray(roles)) roles = [];
                select.innerHTML = "<option value=''>Selecciona un rol</option>" + roles.map(function (r) {
                    var id = String(r.id);
                    var label = escapeHtml(r.nombre || "Rol");
                    var nivel = (r.nivel != null && r.nivel !== "") ? (" (Nivel " + escapeHtml(String(r.nivel)) + ")") : "";
                    var desc = r.descripcion ? (" - " + escapeHtml(String(r.descripcion))) : "";
                    var selected = (selectedId != null && String(selectedId) === id) ? " selected" : "";
                    return "<option value='" + escapeHtml(id) + "'" + selected + ">" + label + nivel + desc + "</option>";
                }).join("");
            } catch (e) {
                select.innerHTML = "<option value=''>Sin roles</option>";
            }
        }

        async function fetchUserForEdit(userId) {
            if (!apiGetUserUrlTemplate) throw new Error("No hay endpoint para obtener usuario");
            var url = fillUrlTemplate(apiGetUserUrlTemplate, userId);
            var res = await fetch(url, {
                headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                credentials: "same-origin"
            });
            if (!res.ok) throw new Error("No se pudo cargar el usuario para edición");
            var json = await res.json().catch(function () { return null; });
            if (!json || json.success !== true || !json.data) throw new Error((json && json.error) ? json.error : "Respuesta inválida");
            return json.data;
        }

        function openEditUserModal() {
            var sub = document.getElementById("optionsUserEditModal");
            if (!sub) return;
            sub.classList.add("is-open");
            sub.setAttribute("aria-hidden", "false");
            setEditMsg("", null);
        }

        function closeEditUserModal() {
            var sub = document.getElementById("optionsUserEditModal");
            if (!sub) return;
            sub.classList.remove("is-open");
            sub.setAttribute("aria-hidden", "true");
            setEditMsg("", null);
        }

        function formatUserActivity(user) {
            var raw = "";
            if (user && user.last_login && user.last_login !== "-") {
                raw = String(user.last_login).trim();
            } else if (user && user.ultimo_acceso && user.ultimo_acceso !== "-") {
                raw = String(user.ultimo_acceso).trim();
            } else if (user && user.ultimo_ingreso && user.ultimo_ingreso !== "-") {
                raw = String(user.ultimo_ingreso).trim();
            }

            if (!raw || /sin registro|never|nunca/i.test(raw)) return "Nunca";

            // Si viene como dd-mm-yyyy HH:MM, convertir a Hoy/Ayer cuando corresponda.
            var m = raw.match(/^(\d{2})-(\d{2})-(\d{4})\s+(\d{2}:\d{2})/);
            if (!m) return raw;

            var dd = parseInt(m[1], 10);
            var mm = parseInt(m[2], 10) - 1;
            var yyyy = parseInt(m[3], 10);
            var hhmm = m[4];

            var d = new Date();
            var today = new Date(d.getFullYear(), d.getMonth(), d.getDate());
            var target = new Date(yyyy, mm, dd);
            var diffDays = Math.round((today - target) / 86400000);

            if (diffDays === 0) return "Hoy " + hhmm;
            if (diffDays === 1) return "Ayer " + hhmm;
            return raw;
        }

        function renderUsuarios(usuarios) {
            var tbody = modal.querySelector("#usuariosTableBody");
            if (!tbody) {
                return;
            }
            tbody.innerHTML = "";
            tbody.style.display = "";

            var list = Array.isArray(usuarios) ? usuarios : [];
            usersListCache = list.slice();
            if (countNode) countNode.textContent = String(list.length);
            if (activeCountNode) activeCountNode.textContent = String(list.filter(function (u) { return u && u.activo; }).length);
            if (inactiveCountNode) inactiveCountNode.textContent = String(list.filter(function (u) { return u && !u.activo; }).length);

            if (!list.length) {
                tbody.innerHTML =
                    '<tr><td colspan="6" style="text-align:center; padding:20px;">No hay usuarios disponibles</td></tr>';
                return;
            }

            list.forEach(function (user) {
                if (!user || user.id == null) return;
                var id = parseInt(user.id, 10);
                if (Number.isNaN(id)) return;

                var actividad = formatUserActivity(user);

                var displayName = escapeHtml(user.nombre ? user.nombre : "Usuario");
                var displayUser = escapeHtml(user.usuario ? user.usuario : "-");
                var displayRole = escapeHtml(user.rol ? user.rol : "Sin rol");

                var canDelete = user.can_delete !== false;
                var deleteDisabledAttr = canDelete ? "" : " disabled";
                var deleteReason = user.delete_reason ? escapeHtml(user.delete_reason) : "";
                var deleteTitle = deleteReason ? (' title="' + deleteReason + '"') : "";

                var tr = document.createElement("tr");
                tr.dataset.userId = String(id);
                tr.innerHTML = ""
                    + "<td>" + displayName + "</td>"
                    + "<td>@" + displayUser + "</td>"
                    + "<td>" + displayRole + "</td>"
                    + "<td>"
                    + (user.bloqueado_seguridad
                        ? '<span class="estado inactivo">Bloqueado</span>'
                        : user.activo
                        ? '<span class="estado activo">Activo</span>'
                        : '<span class="estado inactivo">Inactivo</span>')
                    + "</td>"
                    + "<td>" + escapeHtml(String(actividad || "—")) + "</td>"
                    + "<td class=\"acciones\">"
                    + "<button type=\"button\" class=\"action-btn icon edit\" title=\"Editar usuario\" aria-label=\"Editar usuario\" onclick=\"editarUsuario(" + id + "); event.stopPropagation()\">✏️</button>"
                    + "<button type=\"button\" class=\"action-btn icon toggle\" title=\"" + (user.activo ? "Desactivar usuario" : "Activar usuario") + "\" aria-label=\"" + (user.activo ? "Desactivar usuario" : "Activar usuario") + "\" onclick=\"toggleUsuario(" + id + "); event.stopPropagation()\">"
                    + (user.activo ? "🔒" : "🔓")
                    + "</button>"
                    + "<button type=\"button\" class=\"action-btn icon edit\" title=\"Desbloquear usuario\" aria-label=\"Desbloquear usuario\""
                    + (user.bloqueado_seguridad ? "" : " disabled")
                    + " onclick=\"desbloquearUsuario(" + id + "); event.stopPropagation()\">🛡️</button>"
                    + "<button type=\"button\" class=\"action-btn icon delete\" title=\"Eliminar usuario\" aria-label=\"Eliminar usuario\" onclick=\"eliminarUsuario(" + id + "); event.stopPropagation()\""
                    + deleteDisabledAttr
                    + deleteTitle
                    + ">🗑️</button>"
                    + "</td>";
                tbody.appendChild(tr);
            });
        }

        function renderPasswordResetRequests(items) {
            var tbody = modal.querySelector("#passwordResetTableBody");
            if (!tbody) return;
            var list = Array.isArray(items) ? items : [];
            passwordResetCache = list.slice();
            if (!list.length) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:20px;">Sin solicitudes pendientes</td></tr>';
                return;
            }
            tbody.innerHTML = "";
            list.forEach(function (row) {
                if (!row || row.id == null) return;
                var tr = document.createElement("tr");
                var estado = String(row.estado || "-");
                var canAttend = estado === "pendiente";
                tr.innerHTML = ""
                    + "<td>@" + escapeHtml(row.usuario || "-") + "</td>"
                    + "<td>" + escapeHtml(row.rol || "-") + "</td>"
                    + "<td>" + escapeHtml(row.creado_at || "-") + "</td>"
                    + "<td class='reset-row-note'>" + escapeHtml(row.motivo || "Sin detalle") + "</td>"
                    + "<td>" + escapeHtml(estado) + "</td>"
                    + "<td class='acciones'>"
                    + "<button type='button' class='action-btn edit'" + (canAttend ? "" : " disabled")
                    + " onclick='atenderSolicitudClave(" + Number(row.id) + "); event.stopPropagation()'>Atender</button>"
                    + "</td>";
                tbody.appendChild(tr);
            });
        }

        async function loadPasswordResetRequests() {
            if (!apiPasswordResetUrl) {
                renderPasswordResetRequests([]);
                return;
            }
            try {
                var res = await fetch(apiPasswordResetUrl + "?status=pendiente", {
                    headers: { "X-Requested-With": "XMLHttpRequest", "Accept": "application/json" },
                    credentials: "same-origin"
                });
                if (!res.ok) throw new Error("No fue posible cargar solicitudes de clave");
                var json = await res.json();
                var items = json && Array.isArray(json.items) ? json.items : [];
                renderPasswordResetRequests(items);
            } catch (e) {
                renderPasswordResetRequests([]);
            }
        }

        function filtrarUsuarios() {
            var searchEl = modal.querySelector("#searchUsuarios");
            var estadoEl = modal.querySelector("#filterEstado");

            var search = (searchEl && searchEl.value ? searchEl.value : "").toLowerCase();
            var estado = (estadoEl && estadoEl.value ? estadoEl.value : "all");

            var base = Array.isArray(usuariosGlobal) ? usuariosGlobal : [];
            var filtrados = base.filter(function (user) {
                if (!user) return false;
                var nombre = String(user.nombre || "").toLowerCase();
                var usuario = String(user.usuario || "").toLowerCase();

                var matchTexto = !search || nombre.indexOf(search) !== -1 || usuario.indexOf(search) !== -1;
                var matchEstado =
                    estado === "all" ||
                    (estado === "activo" && !!user.activo) ||
                    (estado === "inactivo" && !user.activo);

                return matchTexto && matchEstado;
            });

            renderUsuarios(filtrados);
        }

        async function loadOptionsUsers(forceRefresh) {
            if (isBusy) return;
            if (!apiUsersUrl) {
                renderUsuarios([]);
                updateStatus("No hay API de usuarios configurada.", true);
                return;
            }

            if (optionsUsersLoaded && !forceRefresh) {
                updateStatus("Datos sincronizados (sin recargar).", false);
                return;
            }

            isBusy = true;
            updateStatus("Cargando usuarios...", false);

            try {
                var endpoint = apiUsersUrl || "/users";
                var res = await fetch(endpoint, {
                    headers: {
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json"
                    },
                    credentials: "same-origin"
                });
                if (!res.ok) throw new Error("No se pudo consultar la API de usuarios");
                var usersData = await res.json();
                var users = (usersData && Array.isArray(usersData.users)) ? usersData.users : usersData;
                usuariosGlobal = Array.isArray(users) ? users : [];
                filtrarUsuarios();
                await loadPasswordResetRequests();
                updateStatus("Usuarios sincronizados correctamente.", false);
                optionsUsersLoaded = true;
            } catch (e) {
                renderUsuarios([]);
                updateStatus("No fue posible cargar la información de usuarios.", true);
            } finally {
                isBusy = false;
            }
        }

        function setResetMsg(message, kind) {
            var msg = document.getElementById("optionsResetMsg");
            if (!msg) return;
            msg.className = "submodal-msg" + (kind === "error" ? " is-error" : kind === "success" ? " is-success" : "");
            msg.textContent = message || "";
        }

        function openPasswordResetModal(item) {
            var sub = document.getElementById("optionsPasswordResetModal");
            if (!sub || !item) return;
            document.getElementById("optionsResetId").value = String(item.id || "");
            document.getElementById("optionsResetUsuario").value = item.usuario || "";
            document.getElementById("optionsResetRol").value = item.rol || "";
            document.getElementById("optionsResetPassword").value = "";
            document.getElementById("optionsResetNote").value = "";
            setResetMsg("", null);
            sub.classList.add("is-open");
            sub.setAttribute("aria-hidden", "false");
        }

        function closePasswordResetModal() {
            var sub = document.getElementById("optionsPasswordResetModal");
            if (!sub) return;
            sub.classList.remove("is-open");
            sub.setAttribute("aria-hidden", "true");
            setResetMsg("", null);
        }

        async function resolvePasswordReset(reqId, action, password, note) {
            if (!apiPasswordResetResolveTemplate) throw new Error("No hay endpoint para procesar solicitudes");
            var url = fillUrlTemplate(apiPasswordResetResolveTemplate, reqId);
            var payload = { action: action, note: note || "" };
            if (action === "approve") payload.new_password = password || "";
            var res = await fetch(url, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest"
                },
                credentials: "same-origin",
                body: JSON.stringify(payload)
            });
            var json = await res.json().catch(function () { return null; });
            if (!res.ok || !json || json.success !== true) {
                throw new Error((json && json.error) ? json.error : "No se pudo procesar la solicitud");
            }
        }

        async function toggleUser(userId) {
            if (!apiToggleUserUrlTemplate) throw new Error("No hay endpoint para activar/desactivar");
            var url = fillUrlTemplate(apiToggleUserUrlTemplate, userId);

            var res = await fetch(url, {
                method: "POST",
                headers: {
                    "X-Requested-With": "XMLHttpRequest"
                },
                credentials: "same-origin"
            });
            var json = await res.json().catch(function () { return null; });
            if (!res.ok || !json || json.success !== true) {
                throw new Error((json && json.error) ? json.error : "No se pudo actualizar el estado");
            }
        }

        async function deleteUser(userId) {
            if (!apiDeleteUserUrlTemplate) throw new Error("No hay endpoint para eliminar usuario");
            var url = fillUrlTemplate(apiDeleteUserUrlTemplate, userId);

            var res = await fetch(url, {
                method: "DELETE",
                headers: {
                    "X-Requested-With": "XMLHttpRequest"
                },
                credentials: "same-origin"
            });
            var json = await res.json().catch(function () { return null; });
            if (!res.ok || !json || json.success !== true) {
                throw new Error((json && json.error) ? json.error : "No se pudo eliminar el usuario");
            }
        }

        async function unlockUser(userId) {
            if (!apiUnlockUserUrlTemplate) throw new Error("No hay endpoint para desbloquear");
            var url = fillUrlTemplate(apiUnlockUserUrlTemplate, userId);
            var res = await fetch(url, {
                method: "POST",
                headers: {
                    "X-Requested-With": "XMLHttpRequest"
                },
                credentials: "same-origin"
            });
            var json = await res.json().catch(function () { return null; });
            if (!res.ok || !json || json.success !== true) {
                throw new Error((json && json.error) ? json.error : "No se pudo desbloquear el usuario");
            }
        }

        async function handleToggleOrDelete(action, userId) {
            if (userId == null) return;
            var uid = parseInt(userId, 10);
            if (Number.isNaN(uid)) return;

            if (action === "toggle") {
                if (isBusy) return;
                updateStatus("Actualizando estado...", false);
                isBusy = true;
                try {
                    await toggleUser(uid);
                    var idx = usersListCache.findIndex(function (x) { return x.id === uid; });
                    if (idx >= 0) {
                        usersListCache[idx].activo = !usersListCache[idx].activo;
                    }
                    renderUsuarios(usersListCache);
                    updateStatus("Estado actualizado.", false);
                } catch (e) {
                    updateStatus(e.message || "Error al actualizar estado.", true);
                } finally {
                    isBusy = false;
                }
                return;
            }

            if (action === "delete") {
                var target = usersListCache.find(function (x) { return x.id === uid; });
                if (target && target.can_delete === false) {
                    updateStatus(target.delete_reason || "No se puede eliminar este usuario.", true);
                    return;
                }

                if (isBusy) return;
                updateStatus("Eliminando usuario...", false);
                isBusy = true;
                try {
                    await deleteUser(uid);
                    usersListCache = usersListCache.filter(function (x) { return x.id !== uid; });
                    renderUsuarios(usersListCache);
                    updateStatus("Usuario eliminado.", false);
                } catch (e) {
                    updateStatus(e.message || "Error al eliminar usuario.", true);
                } finally {
                    isBusy = false;
                }
            }

            if (action === "unlock") {
                if (isBusy) return;
                updateStatus("Desbloqueando usuario...", false);
                isBusy = true;
                try {
                    await unlockUser(uid);
                    var idx2 = usersListCache.findIndex(function (x) { return x.id === uid; });
                    if (idx2 >= 0) {
                        usersListCache[idx2].bloqueado_seguridad = false;
                        usersListCache[idx2].bloqueado_at = null;
                        usersListCache[idx2].intentos_fallidos = 0;
                        usersListCache[idx2].activo = true;
                    }
                    renderUsuarios(usersListCache);
                    updateStatus("Usuario desbloqueado.", false);
                } catch (e2) {
                    updateStatus(e2.message || "Error al desbloquear usuario.", true);
                } finally {
                    isBusy = false;
                }
            }
        }

        function dispatchRowAction(action, id) {
            var uid = parseInt(id, 10);
            if (Number.isNaN(uid)) return;
            if (action === "edit") return;
            if (action === "toggle" || action === "delete") {
                handleToggleOrDelete(action, uid);
            } else if (action === "unlock") {
                handleToggleOrDelete(action, uid);
            }
        }

        window.editarUsuario = function editarUsuario(id) {
            var userId = parseInt(id, 10);
            if (Number.isNaN(userId)) return;
            if (isBusy) return;
            isBusy = true;
            updateStatus("Cargando usuario...", false);
            setEditMsg("Cargando usuario...", null);
            openEditUserModal();

            (async function () {
                try {
                    var data = await fetchUserForEdit(userId);
                    document.getElementById("optionsEditId").value = String(data.id);
                    document.getElementById("optionsEditNombre").value = data.nombre || "";
                    document.getElementById("optionsEditUsuario").value = data.usuario || "";
                    document.getElementById("optionsEditPassword").value = "";
                    document.getElementById("optionsEditCorreo").value = data.correo || "";
                    document.getElementById("optionsEditTelefono").value = data.telefono || "";
                    var parsedAddress = inferLocationFromAddress(data.direccion || "");
                    document.getElementById("optionsEditDireccion").value = parsedAddress.direccion || "";
                    document.getElementById("optionsEditGenero").value = data.genero || "";
                    document.getElementById("optionsEditFechaNac").value = data.fecha_nacimiento || "";
                    document.getElementById("optionsEditRut").value = data.rut || "";
                    document.getElementById("optionsEditActivo").checked = !!data.activo;
                    var perms = data.permisos || {};
                    document.getElementById("optionsEditPermFinanzas").checked = perms.ver_finanzas !== false;
                    document.getElementById("optionsEditPermPrecioMayor").checked = perms.ver_precio_mayor !== false;
                    await loadEditGeoSelectors(parsedAddress.region, parsedAddress.comuna);
                    await loadRolesIntoEditSelect(data.rol_id);
                    updateStatus("Listo para editar.", false);
                    setEditMsg("", null);
                } catch (e) {
                    updateStatus("No se pudo cargar para editar.", false);
                    setEditMsg(e.message || "Error al cargar usuario.", "error");
                } finally {
                    isBusy = false;
                }
            })();
        };
        window.toggleUsuario = function toggleUsuario(id) {
            dispatchRowAction("toggle", id);
        };
        window.eliminarUsuario = function eliminarUsuario(id) {
            if (!window.confirm("¿Eliminar usuario?")) return;
            dispatchRowAction("delete", id);
        };
        window.desbloquearUsuario = function desbloquearUsuario(id) {
            if (!window.confirm("¿Desbloquear este usuario?")) return;
            dispatchRowAction("unlock", id);
        };
        window.atenderSolicitudClave = function atenderSolicitudClave(id) {
            var reqId = parseInt(id, 10);
            if (Number.isNaN(reqId)) return;
            var item = passwordResetCache.find(function (x) { return Number(x.id) === reqId; });
            if (!item) return;
            openPasswordResetModal(item);
        };
        window.renderUsuarios = renderUsuarios;

        function openOptionsModal() {
            modal.hidden = false;
            modal.style.display = "flex";
            document.body.style.overflow = "hidden";

            var tbody = modal.querySelector("#usuariosTableBody");
            if (tbody) {
                tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; padding:20px;">Cargando usuarios...</td></tr>';
            }
            loadOptionsUsers(true);
        }

        function closeOptionsModal() {
            modal.style.display = "none";
            modal.hidden = true;
            document.body.style.overflow = "";
        }

        window.openOptionsModal = openOptionsModal;
        window.closeOptionsModal = closeOptionsModal;
        window.loadOptionsUsers = loadOptionsUsers;
        window.cargarUsuarios = function () { return loadOptionsUsers(true); };
        window.abrirCrearUsuario = function () {
            openCreateUserModal();
        };

        (function bindCreateModal() {
            var sub = document.getElementById("optionsUserCreateModal");
            var form = document.getElementById("optionsUserCreateForm");
            if (!sub || !form) return;

            sub.querySelectorAll("[data-options-create-close]").forEach(function (btn) {
                if (btn.dataset.optionsCreateCloseBound === "1") return;
                btn.dataset.optionsCreateCloseBound = "1";
                btn.addEventListener("click", function (e) {
                    e.preventDefault();
                    closeCreateUserModal();
                });
            });
            sub.addEventListener("click", function (e) {
                if (e.target === sub) closeCreateUserModal();
            });

            if (form.dataset.optionsCreateBound !== "1") {
                form.dataset.optionsCreateBound = "1";
                form.addEventListener("submit", async function (e) {
                    e.preventDefault();
                    if (!apiCreateUrl) {
                        setCreateMsg("No hay endpoint para crear usuarios.", "error");
                        return;
                    }
                    if (isBusy) return;
                    setCreateMsg("Creando usuario...", null);
                    isBusy = true;
                    try {
                        var fd = new FormData(form);
                        var payload = {};
                        fd.forEach(function (v, k) {
                            payload[k] = String(v || "").trim();
                        });
                        Object.keys(payload).forEach(function (k) { if (payload[k] === "") delete payload[k]; });

                        var res = await fetch(apiCreateUrl, {
                            method: "POST",
                            headers: {
                                "X-Requested-With": "XMLHttpRequest",
                                "Content-Type": "application/json"
                            },
                            credentials: "same-origin",
                            body: JSON.stringify(payload)
                        });
                        var json = await res.json().catch(function () { return null; });
                        if (!res.ok || !json || json.success !== true) {
                            throw new Error((json && json.error) ? json.error : "No se pudo crear el usuario");
                        }
                        setCreateMsg("Usuario creado correctamente.", "success");
                        await loadOptionsUsers(true);
                        setTimeout(function () {
                            closeCreateUserModal();
                        }, 600);
                    } catch (err) {
                        setCreateMsg(err.message || "Error al crear usuario.", "error");
                    } finally {
                        isBusy = false;
                    }
                });
            }
        })();

        (function bindEditModal() {
            var sub = document.getElementById("optionsUserEditModal");
            var form = document.getElementById("optionsUserEditForm");
            if (!sub || !form) return;

            sub.querySelectorAll("[data-options-edit-close]").forEach(function (btn) {
                if (btn.dataset.optionsEditCloseBound === "1") return;
                btn.dataset.optionsEditCloseBound = "1";
                btn.addEventListener("click", function (e) {
                    e.preventDefault();
                    closeEditUserModal();
                });
            });
            sub.addEventListener("click", function (e) {
                if (e.target === sub) closeEditUserModal();
            });

            if (form.dataset.optionsEditBound !== "1") {
                form.dataset.optionsEditBound = "1";
                form.addEventListener("submit", async function (e) {
                    e.preventDefault();
                    if (!apiGetUserUrlTemplate) {
                        setEditMsg("No hay endpoint para usuario.", "error");
                        return;
                    }
                    var idValue = document.getElementById("optionsEditId").value;
                    var userId = parseInt(idValue, 10);
                    if (!userId) {
                        setEditMsg("ID inválido.", "error");
                        return;
                    }
                    var url = fillUrlTemplate(modal.getAttribute("data-usuarios-edit-url-template") || "", userId);
                    if (!url) {
                        setEditMsg("No hay endpoint para editar.", "error");
                        return;
                    }
                    if (isBusy) return;
                    isBusy = true;
                    setEditMsg("Guardando cambios...", null);
                    try {
                        var payload = {
                            nombre: (document.getElementById("optionsEditNombre").value || "").trim(),
                            usuario: (document.getElementById("optionsEditUsuario").value || "").trim(),
                            rol_id: (document.getElementById("optionsEditRolSelect").value || "").trim(),
                            activo: !!document.getElementById("optionsEditActivo").checked,
                            correo: (document.getElementById("optionsEditCorreo").value || "").trim(),
                            telefono: (document.getElementById("optionsEditTelefono").value || "").trim(),
                            direccion: (document.getElementById("optionsEditDireccion").value || "").trim(),
                            region: (document.getElementById("optionsEditRegionSelect").value || "").trim(),
                            comuna: (document.getElementById("optionsEditComunaSelect").value || "").trim(),
                            genero: (document.getElementById("optionsEditGenero").value || "").trim(),
                            fecha_nacimiento: (document.getElementById("optionsEditFechaNac").value || "").trim(),
                            rut: (document.getElementById("optionsEditRut").value || "").trim(),
                            permisos: {
                                ver_finanzas: !!document.getElementById("optionsEditPermFinanzas").checked,
                                ver_precio_mayor: !!document.getElementById("optionsEditPermPrecioMayor").checked
                            }
                        };
                        var password = (document.getElementById("optionsEditPassword").value || "").trim();
                        if (password) payload.password = password;
                        Object.keys(payload).forEach(function (k) { if (payload[k] === "") delete payload[k]; });

                        var res = await fetch(url, {
                            method: "PUT",
                            headers: {
                                "X-Requested-With": "XMLHttpRequest",
                                "Content-Type": "application/json"
                            },
                            credentials: "same-origin",
                            body: JSON.stringify(payload)
                        });
                        var json = await res.json().catch(function () { return null; });
                        if (!res.ok || !json || json.success !== true) {
                            throw new Error((json && json.error) ? json.error : "No se pudo actualizar el usuario");
                        }
                        setEditMsg("Cambios guardados.", "success");
                        await loadOptionsUsers(true);
                        setTimeout(function () {
                            closeEditUserModal();
                        }, 600);
                    } catch (err) {
                        setEditMsg(err.message || "Error al guardar.", "error");
                    } finally {
                        isBusy = false;
                    }
                });
            }
        })();

        (function bindResetPasswordModal() {
            var sub = document.getElementById("optionsPasswordResetModal");
            var form = document.getElementById("optionsPasswordResetForm");
            var rejectBtn = document.getElementById("optionsResetRejectBtn");
            if (!sub || !form || !rejectBtn) return;

            sub.querySelectorAll("[data-options-reset-close]").forEach(function (btn) {
                if (btn.dataset.optionsResetCloseBound === "1") return;
                btn.dataset.optionsResetCloseBound = "1";
                btn.addEventListener("click", function (e) {
                    e.preventDefault();
                    closePasswordResetModal();
                });
            });
            sub.addEventListener("click", function (e) {
                if (e.target === sub) closePasswordResetModal();
            });

            if (rejectBtn.dataset.optionsResetRejectBound !== "1") {
                rejectBtn.dataset.optionsResetRejectBound = "1";
                rejectBtn.addEventListener("click", async function () {
                    var idValue = parseInt(document.getElementById("optionsResetId").value, 10);
                    var note = (document.getElementById("optionsResetNote").value || "").trim();
                    if (!idValue) return;
                    if (isBusy) return;
                    isBusy = true;
                    setResetMsg("Rechazando solicitud...", null);
                    try {
                        await resolvePasswordReset(idValue, "reject", "", note);
                        setResetMsg("Solicitud rechazada.", "success");
                        await loadPasswordResetRequests();
                        setTimeout(closePasswordResetModal, 500);
                    } catch (err) {
                        setResetMsg(err.message || "Error al rechazar solicitud.", "error");
                    } finally {
                        isBusy = false;
                    }
                });
            }

            if (form.dataset.optionsResetBound !== "1") {
                form.dataset.optionsResetBound = "1";
                form.addEventListener("submit", async function (e) {
                    e.preventDefault();
                    var idValue = parseInt(document.getElementById("optionsResetId").value, 10);
                    var password = (document.getElementById("optionsResetPassword").value || "").trim();
                    var note = (document.getElementById("optionsResetNote").value || "").trim();
                    if (!idValue) return;
                    if (password.length < 6) {
                        setResetMsg("La nueva contraseña debe tener al menos 6 caracteres.", "error");
                        return;
                    }
                    if (isBusy) return;
                    isBusy = true;
                    setResetMsg("Aplicando nueva contraseña...", null);
                    try {
                        await resolvePasswordReset(idValue, "approve", password, note);
                        setResetMsg("Contraseña reasignada correctamente.", "success");
                        await loadPasswordResetRequests();
                        setTimeout(closePasswordResetModal, 500);
                    } catch (err) {
                        setResetMsg(err.message || "Error al asignar contraseña.", "error");
                    } finally {
                        isBusy = false;
                    }
                });
            }
        })();

        // Cerrar desde botón dedicado
        var closeBtn = modal.querySelector('[data-modal-close="options"]');
        if (closeBtn && !closeBtn.dataset.usuariosUiCloseBound) {
            closeBtn.dataset.usuariosUiCloseBound = "1";
            closeBtn.addEventListener("click", function (e) {
                e.preventDefault();
                closeOptionsModal();
            });
        }

        // Cerrar al hacer click sobre el backdrop
        modal.addEventListener("click", function (event) {
            if (event.target === modal) {
                closeOptionsModal();
            }
        });

        // ESC para cerrar el modal de opciones
        if (!window.__usuariosOptionsEscBound) {
            window.__usuariosOptionsEscBound = true;
            document.addEventListener("keydown", function (event) {
                if (event.key !== "Escape") return;
                if (!modal.hidden) closeOptionsModal();
            });
        }

        // Refresh buttons (sin inline onclick)
        modal.querySelectorAll("[data-options-refresh-users]").forEach(function (btn) {
            if (btn.dataset.usuariosUiRefreshBound === "1") return;
            btn.dataset.usuariosUiRefreshBound = "1";
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                loadOptionsUsers(true);
            });
        });

        // Search + filter toolbar (bind once)
        var searchInput = modal.querySelector("#searchUsuarios");
        var estadoSelect = modal.querySelector("#filterEstado");
        if (searchInput && searchInput.dataset.usuariosUiBound !== "1") {
            searchInput.dataset.usuariosUiBound = "1";
            searchInput.addEventListener("input", filtrarUsuarios);
        }
        if (estadoSelect && estadoSelect.dataset.usuariosUiBound !== "1") {
            estadoSelect.dataset.usuariosUiBound = "1";
            estadoSelect.addEventListener("change", filtrarUsuarios);
        }
        var createRegion = document.getElementById("optionsCreateRegionSelect");
        if (createRegion && createRegion.dataset.usuariosUiBound !== "1") {
            createRegion.dataset.usuariosUiBound = "1";
            createRegion.addEventListener("change", function () {
                fillCreateComunas(createRegion.value || "", "");
            });
        }
        var editRegion = document.getElementById("optionsEditRegionSelect");
        if (editRegion && editRegion.dataset.usuariosUiBound !== "1") {
            editRegion.dataset.usuariosUiBound = "1";
            editRegion.addEventListener("change", function () {
                fillEditComunas(editRegion.value || "", "");
            });
        }
    }

    window.initUsuariosUI = function initUsuariosUI() {
        var modal = document.getElementById("optionsModal");
        if (!modal) return;
        initOptionsUsersModal();
    };

    document.addEventListener("DOMContentLoaded", function () {
        if (typeof window.initUsuariosUI === "function") window.initUsuariosUI();
    });
    document.addEventListener("app:module-loaded", function () {
        if (typeof window.initUsuariosUI === "function") window.initUsuariosUI();
    });
})();

