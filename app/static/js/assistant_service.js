/**
 * Andes Auto Parts — Assistant Service (Paso 3).
 *
 * Flujo:
 *   Assistant UI → /assistant/api/* (BFF, cookie+CSRF) → Agent Gateway (Bearer) → tools
 *
 * El navegador nunca habla con el Gateway ni conoce el token M2M.
 * Conectadas: search_catalog (/buscar), get_product (/producto),
 * get_inventory (/stock), get_stock_movements (/movimientos), get_ingresos (/ingresos),
 * get_purchase_orders (/oc), get_customer (/cliente), get_supplier (/proveedor)
 * get_supplier (/proveedor), check_stock (/disponibilidad) y get_dashboard_kpis (/kpis).
 * El resto responde localmente.
 */
(function (global) {
    'use strict';

    var STORAGE = {
        panelOpen: 'andes_assistant_panel_open',
        agentEnabled: 'andes_assistant_agent_enabled',
        expanded: 'andes_assistant_expanded'
    };

    var UNAVAILABLE = 'Esta función todavía no está disponible.';

    function readFlag(key, fallback) {
        try {
            var value = global.localStorage.getItem(key);
            if (value === null || value === undefined) return fallback;
            return value === '1';
        } catch (err) {
            return fallback;
        }
    }

    function writeFlag(key, value) {
        try {
            global.localStorage.setItem(key, value ? '1' : '0');
        } catch (err) { /* persistencia local no crítica */ }
    }

    function csrfToken() {
        if (typeof global.getCsrfToken === 'function') {
            return global.getCsrfToken() || '';
        }
        var meta = document.querySelector('meta[name="csrf-token"], meta[name="csrf_token"]');
        if (meta) return meta.getAttribute('content') || '';
        var root = document.getElementById('ap-assistant-root');
        return root ? (root.getAttribute('data-csrf-token') || '') : '';
    }

    function invokeUrl() {
        var root = document.getElementById('ap-assistant-root');
        return (root && root.getAttribute('data-invoke-url')) || '/assistant/api/invoke';
    }

    function parseBuscar(text) {
        var match = String(text || '').trim().match(/^\/buscar(?:\s+(.*))?$/i);
        if (!match) return null;
        return String(match[1] || '').trim();
    }

    function parseProducto(text) {
        var match = String(text || '').trim().match(/^\/producto(?:\s+(.*))?$/i);
        if (!match) return null;
        return String(match[1] || '').trim();
    }

    function parseStock(text) {
        var match = String(text || '').trim().match(/^\/stock(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { codigo: '' };
        var parts = rest.split(/\s+/);
        var out = { codigo: parts[0] };
        if (parts[1]) out.marca = parts[1];
        if (parts.length > 2) out.bodega = parts.slice(2).join(' ');
        return out;
    }

    function parseDisponibilidad(text) {
        var match = String(text || '').trim().match(/^\/disponibilidad(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { empty: true };
        var chunks = rest.split(/\s*,\s*/);
        var items = [];
        for (var i = 0; i < chunks.length; i++) {
            var parts = String(chunks[i] || '').trim().split(/\s+/).filter(Boolean);
            if (parts.length < 2) return { empty: true };
            var qty = parseInt(parts[1], 10);
            if (!qty || qty < 1) return { empty: true };
            var item = { codigo: parts[0], cantidad: qty };
            if (parts[2]) item.marca = parts[2];
            if (parts.length > 3) item.bodega = parts.slice(3).join(' ');
            items.push(item);
        }
        if (!items.length || items.length > 20) return { empty: true };
        return { items: items };
    }

    function parseMovimientos(text) {
        var match = String(text || '').trim().match(/^\/movimientos(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { codigo: '' };
        var parts = rest.split(/\s+/);
        var out = { codigo: parts[0] };
        var dateRe = /^\d{4}-\d{2}-\d{2}$/;
        if (parts[1] && dateRe.test(parts[1])) out.fecha_desde = parts[1];
        if (parts[2] && dateRe.test(parts[2])) out.fecha_hasta = parts[2];
        return out;
    }

    function parseIngresos(text) {
        var match = String(text || '').trim().match(/^\/ingresos(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { codigo: '' };
        var parts = rest.split(/\s+/);
        var out = { codigo: parts[0] };
        var dateRe = /^\d{4}-\d{2}-\d{2}$/;
        if (parts[1] && dateRe.test(parts[1])) out.fecha_desde = parts[1];
        if (parts[2] && dateRe.test(parts[2])) out.fecha_hasta = parts[2];
        return out;
    }

    function parseOc(text) {
        var match = String(text || '').trim().match(/^\/oc(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { empty: true };
        var parts = rest.split(/\s+/);
        var dateRe = /^\d{4}-\d{2}-\d{2}$/;
        var keyword = String(parts[0] || '').toLowerCase();
        var out = {};
        if (keyword === 'codigo') {
            if (!parts[1]) return { empty: true };
            out.codigo = parts[1];
            if (parts[2] && dateRe.test(parts[2])) out.fecha_desde = parts[2];
            if (parts[3] && dateRe.test(parts[3])) out.fecha_hasta = parts[3];
            return out;
        }
        if (keyword === 'estado') {
            if (!parts[1]) return { empty: true };
            out.estado = parts[1];
            return out;
        }
        if (keyword === 'proveedor') {
            if (!parts[1]) return { empty: true };
            out.proveedor = parts.slice(1).join(' ');
            return out;
        }
        out.numero = parts[0];
        if (parts[1] && dateRe.test(parts[1])) out.fecha_desde = parts[1];
        if (parts[2] && dateRe.test(parts[2])) out.fecha_hasta = parts[2];
        return out;
    }

    function parseCliente(text) {
        var match = String(text || '').trim().match(/^\/cliente(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { empty: true };
        var parts = rest.split(/\s+/);
        var keyword = String(parts[0] || '').toLowerCase();
        if (keyword === 'rut') {
            if (!parts[1]) return { empty: true };
            return { rut: parts.slice(1).join(' ') };
        }
        if (keyword === 'id') {
            if (!parts[1]) return { empty: true };
            var idNum = parseInt(parts[1], 10);
            if (!idNum || idNum < 1) return { empty: true };
            return { id: idNum };
        }
        return { q: rest };
    }

    function parseProveedor(text) {
        var match = String(text || '').trim().match(/^\/proveedor(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { empty: true };
        var parts = rest.split(/\s+/);
        var keyword = String(parts[0] || '').toLowerCase();
        if (keyword === 'rut') {
            if (!parts[1]) return { empty: true };
            return { rut: parts.slice(1).join(' ') };
        }
        if (keyword === 'id') {
            if (!parts[1]) return { empty: true };
            var idNum = parseInt(parts[1], 10);
            if (!idNum || idNum < 1) return { empty: true };
            return { id: idNum };
        }
        return { q: rest };
    }

    function parseKpis(text) {
        var match = String(text || '').trim().match(/^\/kpis(?:\s+(.*))?$/i);
        if (!match) return null;
        var rest = String(match[1] || '').trim();
        if (!rest) return { periodo: 'snapshot' };
        var parts = rest.split(/\s+/);
        var first = String(parts[0] || '').toLowerCase();
        var dateRe = /^\d{4}-\d{2}-\d{2}$/;
        if (first === 'hoy' || first === 'mes' || first === '7d' || first === '30d' || first === 'snapshot') {
            return { periodo: first };
        }
        if (dateRe.test(parts[0]) && parts[1] && dateRe.test(parts[1])) {
            return { periodo: 'custom', fecha_desde: parts[0], fecha_hasta: parts[1] };
        }
        return { invalid: true };
    }

    function formatCatalog(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            return 'No encontré productos para esa búsqueda.';
        }
        var lines = ['Encontré ' + items.length + ' producto(s):', ''];
        items.forEach(function (item, index) {
            var code = item.codigo || '';
            var desc = item.descripcion || '';
            var extra = [];
            if (item.marca) extra.push(item.marca);
            if (item.modelo) extra.push(item.modelo);
            var suffix = extra.length ? ' (' + extra.join(' · ') + ')' : '';
            lines.push((index + 1) + '. ' + code + (desc ? ' — ' + desc : '') + suffix);
        });
        if (payload.meta && payload.meta.truncated) {
            lines.push('', 'Mostrando los primeros ' + (payload.meta.limit || items.length) + ' resultados.');
        }
        return lines.join('\n');
    }

    function formatProduct(payload) {
        var data = (payload && payload.data) || {};
        var lines = [];
        if (data.codigo) lines.push(data.codigo);
        if (data.descripcion) lines.push(data.descripcion);
        if (data.marca) lines.push('Marca: ' + data.marca);
        if (data.modelo) lines.push('Modelo: ' + data.modelo);
        if (data.motor) lines.push('Motor: ' + data.motor);
        if (data.anio) lines.push('Año: ' + data.anio);
        var cats = [];
        if (data.categoria) cats.push(data.categoria);
        if (data.subcategoria) cats.push(data.subcategoria);
        if (cats.length) lines.push('Categoría: ' + cats.join(' / '));
        if (data.activo === false) lines.push('Estado: inactivo');
        else if (data.activo === true) lines.push('Estado: activo');
        return lines.length ? lines.join('\n') : 'Producto encontrado.';
    }

    function formatInventory(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        var header = data.codigo || 'Producto';
        if (data.descripcion) header += ' — ' + data.descripcion;
        if (!items.length) {
            return header + '\nSin stock registrado.';
        }
        var lines = [header, 'Total: ' + (data.total_stock != null ? data.total_stock : items.length), ''];
        items.forEach(function (item, index) {
            var extra = [];
            if (item.marca) extra.push(item.marca);
            if (item.bodega) extra.push(item.bodega);
            if (item.origen_compra) extra.push(item.origen_compra);
            var prefix = extra.length ? extra.join(' · ') : 'variante';
            lines.push((index + 1) + '. ' + prefix + ' — ' + (item.stock != null ? item.stock : 0));
        });
        return lines.join('\n');
    }

    function formatDisponibilidad(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        var available = !!data.available;
        var lines = [
            available ? 'Disponibilidad: OK' : 'Disponibilidad: insuficiente',
            ''
        ];
        if (!items.length) {
            lines.push('Sin líneas para evaluar.');
            return lines.join('\n');
        }
        items.forEach(function (item, index) {
            var bits = [];
            if (item.codigo) bits.push(item.codigo);
            bits.push('pide ' + (item.cantidad != null ? item.cantidad : 0));
            bits.push('hay ' + (item.disponible != null ? item.disponible : 0));
            if (item.marca) bits.push(item.marca);
            if (item.bodega) bits.push(item.bodega);
            bits.push(item.ok ? 'OK' : 'FALTA');
            lines.push((index + 1) + '. ' + bits.join(' · '));
        });
        return lines.join('\n');
    }

    function formatMoney(value) {
        if (value === null || value === undefined) return '— (sin permiso financiero)';
        try {
            return Number(value).toLocaleString('es-CL');
        } catch (err) {
            return String(value);
        }
    }

    function formatKpis(payload) {
        var data = (payload && payload.data) || {};
        var meta = (payload && payload.meta) || {};
        var lines = ['KPIs (' + (meta.periodo || 'snapshot') + ')'];
        if (meta.fecha_desde && meta.fecha_hasta) {
            lines.push('Ventana: ' + meta.fecha_desde + ' → ' + meta.fecha_hasta);
        }
        lines.push('');
        lines.push('Ventas hoy: ' + formatMoney(data.ventas_hoy));
        lines.push('Ventas mes: ' + formatMoney(data.ventas_mes));
        lines.push('Ventas período: ' + formatMoney(data.ventas_periodo));
        lines.push('Docs hoy: ' + (data.docs_hoy != null ? data.docs_hoy : 0));
        lines.push('Docs mes: ' + (data.docs_mes != null ? data.docs_mes : 0));
        lines.push('Docs período: ' + (data.docs_periodo != null ? data.docs_periodo : 0));
        var chart = Array.isArray(data.chart_data) ? data.chart_data : [];
        if (chart.length) {
            lines.push('', 'Serie (' + chart.length + ' días):');
            chart.slice(-7).forEach(function (point) {
                lines.push('- ' + (point.dia || '') + ': ' + formatMoney(point.total));
            });
        }
        var products = Array.isArray(data.top_productos) ? data.top_productos : [];
        if (products.length) {
            lines.push('', 'Top productos:');
            products.forEach(function (row, index) {
                lines.push(
                    (index + 1) + '. ' + (row.codigo || '') +
                    (row.descripcion ? ' — ' + row.descripcion : '') +
                    ' · qty ' + (row.qty != null ? row.qty : 0) +
                    ' · venta ' + formatMoney(row.venta)
                );
            });
        }
        var clients = Array.isArray(data.top_clientes) ? data.top_clientes : [];
        if (clients.length) {
            lines.push('', 'Top clientes:');
            clients.forEach(function (row, index) {
                lines.push(
                    (index + 1) + '. ' + (row.nombre || '') +
                    ' · docs ' + (row.docs != null ? row.docs : 0) +
                    ' · total ' + formatMoney(row.total)
                );
            });
        }
        if (meta.stock_incluido && Array.isArray(data.stock_critico)) {
            lines.push('', 'Stock crítico:');
            if (!data.stock_critico.length) {
                lines.push('- sin ítems bajo umbral');
            } else {
                data.stock_critico.forEach(function (row, index) {
                    lines.push(
                        (index + 1) + '. ' + (row.codigo || '') +
                        (row.marca ? ' · ' + row.marca : '') +
                        (row.bodega ? ' · ' + row.bodega : '') +
                        ' · stock ' + (row.stock != null ? row.stock : 0)
                    );
                });
            }
        } else if (meta.stock_incluido === false || data.stock_critico === null) {
            lines.push('', 'Stock crítico: no disponible (sin permiso ver_stock)');
        }
        return lines.join('\n');
    }

    function formatMovements(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        var header = 'Movimientos de ' + (data.codigo || 'producto');
        if (data.descripcion) header += ' — ' + data.descripcion;
        if (!items.length) {
            return header + '\nSin movimientos registrados.';
        }
        var lines = [header, 'Mostrando ' + items.length + ' movimiento(s).', ''];
        items.forEach(function (item, index) {
            var bits = [];
            if (item.fecha) bits.push(item.fecha);
            if (item.tipo) bits.push(item.tipo);
            bits.push('cant. ' + (item.cantidad != null ? item.cantidad : 0));
            var extra = [];
            if (item.marca) extra.push(item.marca);
            if (item.bodega) extra.push(item.bodega);
            if (item.origen_compra) extra.push(item.origen_compra);
            var line = (index + 1) + '. ' + bits.join(' · ');
            if (extra.length) line += ' (' + extra.join(' · ') + ')';
            lines.push(line);
        });
        if (payload.meta && payload.meta.truncated) {
            lines.push('', 'Mostrando los primeros ' + (payload.meta.limit || items.length) + ' movimientos.');
        }
        return lines.join('\n');
    }

    function formatIngresos(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        var header = 'Ingresos';
        if (data.codigo) header += ' de ' + data.codigo;
        if (data.descripcion) header += ' — ' + data.descripcion;
        if (!items.length) {
            return header + '\nSin ingresos registrados.';
        }
        var lines = [header, 'Mostrando ' + items.length + ' ingreso(s).', ''];
        items.forEach(function (item, index) {
            var bits = [];
            if (item.fecha) bits.push(item.fecha);
            if (item.numero_documento) bits.push('doc ' + item.numero_documento);
            bits.push(item.anulado ? 'ANULADO' : 'activo');
            bits.push('cant. ' + (item.cantidad != null ? item.cantidad : 0));
            var extra = [];
            if (item.proveedor) extra.push(item.proveedor);
            if (item.marca) extra.push(item.marca);
            if (item.bodega) extra.push(item.bodega);
            if (item.origen_compra) extra.push(item.origen_compra);
            var line = (index + 1) + '. ' + bits.join(' · ');
            if (extra.length) line += ' (' + extra.join(' · ') + ')';
            lines.push(line);
        });
        if (payload.meta && payload.meta.truncated) {
            lines.push('', 'Mostrando los primeros ' + (payload.meta.limit || items.length) + ' ingresos.');
        }
        return lines.join('\n');
    }

    function formatPurchaseOrders(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            return 'Sin órdenes de compra para esa consulta.';
        }
        var lines = ['Órdenes de compra: ' + items.length, ''];
        items.forEach(function (doc, index) {
            var bits = [];
            if (doc.numero) bits.push(doc.numero);
            if (doc.fecha) bits.push(doc.fecha);
            if (doc.estado) bits.push(doc.estado);
            if (doc.proveedor) bits.push(doc.proveedor);
            bits.push((doc.lineas != null ? doc.lineas : (doc.items || []).length) + ' línea(s)');
            if (doc.total != null) bits.push('total ' + doc.total);
            lines.push((index + 1) + '. ' + bits.join(' · '));
            (Array.isArray(doc.items) ? doc.items : []).forEach(function (line) {
                var extra = [];
                if (line.codigo) extra.push(line.codigo);
                if (line.descripcion) extra.push(line.descripcion);
                extra.push('cant. ' + (line.cantidad != null ? line.cantidad : 0));
                if (line.marca) extra.push(line.marca);
                if (line.bodega) extra.push(line.bodega);
                if (line.origen_compra) extra.push(line.origen_compra);
                if (line.precio != null) extra.push('precio ' + line.precio);
                lines.push('   - ' + extra.join(' · '));
            });
        });
        if (payload.meta && payload.meta.truncated) {
            lines.push('', 'Mostrando las primeras ' + (payload.meta.limit || items.length) + ' órdenes.');
        }
        return lines.join('\n');
    }

    function formatCustomers(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            return 'Sin clientes para esa consulta.';
        }
        var lines = ['Clientes: ' + items.length, ''];
        items.forEach(function (row, index) {
            var bits = [];
            if (row.id != null) bits.push('id ' + row.id);
            if (row.nombre) bits.push(row.nombre);
            if (row.rut) bits.push(row.rut);
            if (row.giro) bits.push(row.giro);
            var geo = [];
            if (row.comuna) geo.push(row.comuna);
            if (row.ciudad) geo.push(row.ciudad);
            if (row.region) geo.push(row.region);
            if (geo.length) bits.push(geo.join(', '));
            if (row.activo === false) bits.push('inactivo');
            if (row.cliente_mayorista === true) bits.push('mayorista');
            if (row.margen_descuento_pct != null) bits.push('margen ' + row.margen_descuento_pct + '%');
            lines.push((index + 1) + '. ' + bits.join(' · '));
        });
        if (payload.meta && payload.meta.truncated) {
            lines.push('', 'Mostrando los primeros ' + (payload.meta.limit || items.length) + ' clientes.');
        }
        return lines.join('\n');
    }

    function formatSuppliers(payload) {
        var data = (payload && payload.data) || {};
        var items = Array.isArray(data.items) ? data.items : [];
        if (!items.length) {
            return 'Sin proveedores para esa consulta.';
        }
        var lines = ['Proveedores: ' + items.length, ''];
        items.forEach(function (row, index) {
            var bits = [];
            if (row.id != null) bits.push('id ' + row.id);
            if (row.empresa) bits.push(row.empresa);
            if (row.nombre) bits.push(row.nombre);
            if (row.rut) bits.push(row.rut);
            if (row.giro) bits.push(row.giro);
            var geo = [];
            if (row.comuna) geo.push(row.comuna);
            if (row.ciudad) geo.push(row.ciudad);
            if (row.region) geo.push(row.region);
            if (geo.length) bits.push(geo.join(', '));
            if (row.activo === false) bits.push('inactivo');
            lines.push((index + 1) + '. ' + bits.join(' · '));
        });
        if (payload.meta && payload.meta.truncated) {
            lines.push('', 'Mostrando los primeros ' + (payload.meta.limit || items.length) + ' proveedores.');
        }
        return lines.join('\n');
    }

    function errorMessage(payload) {
        var code = payload && payload.error_code;
        var nested = payload && payload.error;
        if (!code && nested && nested.code) code = nested.code;
        if (code === 'permission_denied') {
            return 'No tienes permiso para esta consulta.';
        }
        if (code === 'not_found') {
            return (nested && nested.message) || (payload && payload.message) || 'Producto no encontrado';
        }
        if (code === 'agent_unavailable' || code === 'erp_unavailable' || code === 'agent_timeout') {
            return 'El Agent Gateway no está disponible. Comprueba que esté encendido e inténtalo de nuevo.';
        }
        if (code === 'invalid_args') {
            return payload.message || 'La consulta no es válida.';
        }
        if (code === 'unauthorized') {
            return 'Tu sesión expiró. Vuelve a iniciar sesión.';
        }
        return (payload && payload.message) || UNAVAILABLE;
    }

    function AssistantService() {
        this.conversationId = 'conv-' + Date.now();
    }

    AssistantService.prototype.getSettings = function () {
        return Promise.resolve({
            panelOpen: readFlag(STORAGE.panelOpen, false),
            agentEnabled: readFlag(STORAGE.agentEnabled, false),
            expanded: readFlag(STORAGE.expanded, false)
        });
    };

    AssistantService.prototype.saveSettings = function (settings) {
        settings = settings || {};
        if (typeof settings.panelOpen === 'boolean') {
            writeFlag(STORAGE.panelOpen, settings.panelOpen);
        }
        if (typeof settings.agentEnabled === 'boolean') {
            writeFlag(STORAGE.agentEnabled, settings.agentEnabled);
        }
        if (typeof settings.expanded === 'boolean') {
            writeFlag(STORAGE.expanded, settings.expanded);
        }
        return Promise.resolve({ ok: true });
    };

    AssistantService.prototype._postTool = function (tool, argumentsObj) {
        return fetch(invokeUrl(), {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': csrfToken(),
                'X-Requested-With': 'XMLHttpRequest'
            },
            body: JSON.stringify({
                tool: tool,
                conversation_id: this.conversationId,
                arguments: argumentsObj
            })
        }).then(function (resp) {
            return resp.json().catch(function () {
                return { ok: false, error_code: 'agent_error', message: UNAVAILABLE };
            }).then(function (body) {
                body = body || {};
                if (!resp.ok || !body.ok) {
                    return {
                        ok: false,
                        code: body.error_code || (body.error && body.error.code) || 'agent_error',
                        error: errorMessage(body)
                    };
                }
                return { ok: true, payload: body };
            });
        }).catch(function () {
            return {
                ok: false,
                code: 'network',
                error: 'El Agent Gateway no está disponible. Comprueba que esté encendido e inténtalo de nuevo.'
            };
        });
    };

    AssistantService.prototype._invokeSearchCatalog = function (query) {
        return this._postTool('search_catalog', { q: query, limit: 10 }).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'search_catalog',
                reply: formatCatalog(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetProduct = function (codigo) {
        return this._postTool('get_product', { codigo: codigo }).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_product',
                reply: formatProduct(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetInventory = function (args) {
        return this._postTool('get_inventory', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_inventory',
                reply: formatInventory(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeCheckStock = function (args) {
        return this._postTool('check_stock', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'check_stock',
                reply: formatDisponibilidad(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetStockMovements = function (args) {
        return this._postTool('get_stock_movements', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_stock_movements',
                reply: formatMovements(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetIngresos = function (args) {
        return this._postTool('get_ingresos', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_ingresos',
                reply: formatIngresos(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetPurchaseOrders = function (args) {
        return this._postTool('get_purchase_orders', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_purchase_orders',
                reply: formatPurchaseOrders(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetCustomer = function (args) {
        return this._postTool('get_customer', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_customer',
                reply: formatCustomers(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetSupplier = function (args) {
        return this._postTool('get_supplier', args).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_supplier',
                reply: formatSuppliers(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype._invokeGetDashboardKpis = function (args) {
        return this._postTool('get_dashboard_kpis', args || {}).then(function (result) {
            if (!result.ok) return result;
            return {
                ok: true,
                source: 'get_dashboard_kpis',
                reply: formatKpis(result.payload),
                payload: result.payload
            };
        });
    };

    AssistantService.prototype.sendMessage = function (text, context) {
        var enabled = !!(context && context.agentEnabled);
        if (!enabled) {
            return Promise.resolve({
                ok: false,
                code: 'agent_disabled',
                error: 'Agente desactivado'
            });
        }
        var trimmed = String(text || '').trim();
        if (!trimmed) {
            return Promise.resolve({
                ok: false,
                code: 'empty',
                error: 'Escribe una pregunta para continuar.'
            });
        }
        var query = parseBuscar(trimmed);
        if (query !== null) {
            if (!query) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /buscar seguido del texto. Ejemplo: /buscar filtro aceite'
                });
            }
            if (query.length < 2) {
                return Promise.resolve({
                    ok: false,
                    code: 'invalid_args',
                    error: 'Escribe al menos 2 caracteres después de /buscar.'
                });
            }
            return this._invokeSearchCatalog(query.slice(0, 80));
        }
        var codigo = parseProducto(trimmed);
        if (codigo !== null) {
            if (!codigo) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /producto seguido del código. Ejemplo: /producto ABC123'
                });
            }
            return this._invokeGetProduct(codigo.slice(0, 64));
        }
        var stock = parseStock(trimmed);
        if (stock !== null) {
            if (!stock.codigo) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /stock seguido del código. Ejemplo: /stock 2404'
                });
            }
            var args = { codigo: String(stock.codigo).slice(0, 64) };
            if (stock.marca) args.marca = String(stock.marca).slice(0, 120);
            if (stock.bodega) args.bodega = String(stock.bodega).slice(0, 120);
            return this._invokeGetInventory(args);
        }
        var disp = parseDisponibilidad(trimmed);
        if (disp !== null) {
            if (disp.empty || !disp.items || !disp.items.length) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /disponibilidad seguido de código y cantidad. Ejemplo: /disponibilidad 2404 2  ·  /disponibilidad 2404 2, ABC123 1'
                });
            }
            var dispItems = [];
            for (var di = 0; di < disp.items.length && di < 20; di++) {
                var row = disp.items[di];
                var entry = {
                    codigo: String(row.codigo || '').slice(0, 64),
                    cantidad: row.cantidad
                };
                if (row.marca) entry.marca = String(row.marca).slice(0, 64);
                if (row.bodega) entry.bodega = String(row.bodega).slice(0, 64);
                dispItems.push(entry);
            }
            return this._invokeCheckStock({ items: dispItems });
        }
        var mov = parseMovimientos(trimmed);
        if (mov !== null) {
            if (!mov.codigo) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /movimientos seguido del código. Ejemplo: /movimientos 2404'
                });
            }
            var movArgs = { codigo: String(mov.codigo).slice(0, 64), limit: 20 };
            if (mov.fecha_desde) movArgs.fecha_desde = mov.fecha_desde;
            if (mov.fecha_hasta) movArgs.fecha_hasta = mov.fecha_hasta;
            return this._invokeGetStockMovements(movArgs);
        }
        var ing = parseIngresos(trimmed);
        if (ing !== null) {
            if (!ing.codigo) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /ingresos seguido del código. Ejemplo: /ingresos 2404'
                });
            }
            var ingArgs = { codigo: String(ing.codigo).slice(0, 64), limit: 20 };
            if (ing.fecha_desde) ingArgs.fecha_desde = ing.fecha_desde;
            if (ing.fecha_hasta) ingArgs.fecha_hasta = ing.fecha_hasta;
            return this._invokeGetIngresos(ingArgs);
        }
        var oc = parseOc(trimmed);
        if (oc !== null) {
            if (oc.empty || (!oc.numero && !oc.codigo && !oc.estado && !oc.proveedor)) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /oc seguido del número. Ejemplo: /oc OC-1  ·  /oc codigo 2404'
                });
            }
            var ocArgs = { limit: 20 };
            if (oc.numero) ocArgs.numero = String(oc.numero).slice(0, 60);
            if (oc.codigo) ocArgs.codigo = String(oc.codigo).slice(0, 64);
            if (oc.estado) ocArgs.estado = String(oc.estado).slice(0, 40);
            if (oc.proveedor) ocArgs.proveedor = String(oc.proveedor).slice(0, 120);
            if (oc.fecha_desde) ocArgs.fecha_desde = oc.fecha_desde;
            if (oc.fecha_hasta) ocArgs.fecha_hasta = oc.fecha_hasta;
            return this._invokeGetPurchaseOrders(ocArgs);
        }
        var cli = parseCliente(trimmed);
        if (cli !== null) {
            if (cli.empty || (!cli.q && !cli.rut && !cli.id)) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /cliente seguido del nombre. Ejemplo: /cliente ALBERT  ·  /cliente rut 78.074.288-7  ·  /cliente id 1'
                });
            }
            var cliArgs = { limit: 20 };
            if (cli.q) cliArgs.q = String(cli.q).slice(0, 80);
            if (cli.rut) cliArgs.rut = String(cli.rut).slice(0, 20);
            if (cli.id) cliArgs.id = cli.id;
            return this._invokeGetCustomer(cliArgs);
        }
        var prov = parseProveedor(trimmed);
        if (prov !== null) {
            if (prov.empty || (!prov.q && !prov.rut && !prov.id)) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /proveedor seguido del nombre o empresa. Ejemplo: /proveedor ALBERT  ·  /proveedor rut 78.074.288-7  ·  /proveedor id 1'
                });
            }
            var provArgs = { limit: 20 };
            if (prov.q) provArgs.q = String(prov.q).slice(0, 80);
            if (prov.rut) provArgs.rut = String(prov.rut).slice(0, 40);
            if (prov.id) provArgs.id = prov.id;
            return this._invokeGetSupplier(provArgs);
        }
        var kpis = parseKpis(trimmed);
        if (kpis !== null) {
            if (kpis.invalid) {
                return Promise.resolve({
                    ok: true,
                    source: 'local',
                    reply: 'Usa /kpis, /kpis 7d, /kpis 30d o /kpis YYYY-MM-DD YYYY-MM-DD'
                });
            }
            var kpiArgs = { periodo: kpis.periodo || 'snapshot' };
            if (kpis.fecha_desde) kpiArgs.fecha_desde = kpis.fecha_desde;
            if (kpis.fecha_hasta) kpiArgs.fecha_hasta = kpis.fecha_hasta;
            return this._invokeGetDashboardKpis(kpiArgs);
        }
        return Promise.resolve({ ok: true, source: 'local', reply: UNAVAILABLE });
    };

    AssistantService.prototype.runQuickAction = function (actionId, context) {
        var enabled = !!(context && context.agentEnabled);
        if (!enabled) {
            return Promise.resolve({
                ok: false,
                code: 'agent_disabled',
                error: 'Activa el agente para usar esta función.'
            });
        }
        var id = String(actionId || '');
        if (id === 'sales_7d') {
            return this._invokeGetDashboardKpis({ periodo: '7d' });
        }
        if (id === 'top_products') {
            return this._invokeGetDashboardKpis({ periodo: '7d', top_limit: 10 });
        }
        return Promise.resolve({
            ok: true,
            source: 'local',
            actionId: actionId,
            reply: UNAVAILABLE
        });
    };

    global.AndesAssistant = global.AndesAssistant || {};
    global.AndesAssistant.AssistantService = AssistantService;
    global.AndesAssistant.MockAssistantService = AssistantService;
    global.AndesAssistant.createService = function () {
        return new AssistantService();
    };
    global.AndesAssistantService = global.AndesAssistant.createService();
})(window);
