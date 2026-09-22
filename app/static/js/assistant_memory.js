/**
 * Andes Auto Parts — bandeja de memoria (FASE 10.2.4).
 *
 * Mismo patron que assistant.js: IIFE, estado plano, `apply*()` que pintan, y
 * DOM construido con createElement/textContent. NUNCA innerHTML.
 *
 * POR QUE ESO IMPORTA MAS AQUI QUE EN NINGUN OTRO SITIO
 *
 * Una memoria es contenido NO CONFIABLE. Sale de lo que el usuario escribio y
 * de lo que el sistema dedujo de la conversacion, y el proposito de este panel
 * es justamente mostrarsela a alguien para que decida. Si el valor de una
 * memoria pudiera escribir markup, la pantalla donde se revisa la memoria seria
 * la pantalla mas facil de atacar del asistente.
 *
 * LO QUE ESTE PANEL NO HACE
 *
 * - No reintenta una aprobacion con una version nueva. Si el backend dice que
 *   la memoria cambio, la operacion se detiene y se ofrece recargar. Reintentar
 *   solo significaria aprobar algo que nadie vio.
 * - No mueve la tarjeta hasta que el servidor confirma. Un movimiento optimista
 *   diria "aprobada" sobre algo que quizas fallo.
 * - No manda actor_user, status, status_by, permission_epoch, sensitivity ni
 *   source. El servidor es la autoridad.
 */
(function () {
    'use strict';

    var root = document.getElementById('ap-assistant-root');
    if (!root) return;

    var panel = document.getElementById('ap-assistant-memory');
    var service = window.AndesAssistantService;
    if (!panel || !service || typeof service.loadMemoryPanel !== 'function') return;

    var listEl = document.getElementById('ap-assistant-memory-list');
    var errorEl = document.getElementById('ap-assistant-memory-error');
    var noticeEl = document.getElementById('ap-assistant-memory-notice');
    var searchEl = document.getElementById('ap-assistant-memory-q');
    var refreshEl = document.getElementById('ap-assistant-memory-refresh');
    var badgeEl = document.getElementById('ap-assistant-memory-badge');
    var tabs = Array.prototype.slice.call(
        panel.querySelectorAll('[data-memory-tab]'));

    var modalEl = document.getElementById('ap-assistant-modal');
    var modal = modalEl ? {
        raiz: modalEl,
        velo: document.getElementById('ap-assistant-modal-veil'),
        titulo: document.getElementById('ap-assistant-modal-title'),
        lead: document.getElementById('ap-assistant-modal-lead'),
        cuerpo: document.getElementById('ap-assistant-modal-body'),
        motivoCaja: document.getElementById('ap-assistant-modal-reason-wrap'),
        motivo: document.getElementById('ap-assistant-modal-reason'),
        error: document.getElementById('ap-assistant-modal-error'),
        cancelar: document.getElementById('ap-assistant-modal-cancel'),
        ok: document.getElementById('ap-assistant-modal-ok')
    } : null;

    var ETIQUETAS = {
        suggested: 'Pendiente',
        approved: 'Aprobada',
        rejected: 'Rechazada',
        expired: 'Expirada'
    };
    var ALCANCES = {
        user: 'Toda tu cuenta',
        conversation: 'Solo esta conversación'
    };

    var state = {
        tab: 'suggested',
        q: '',
        items: [],
        counts: {},
        loading: false,
        busyId: null,
        loaded: false,
        // FASE 10.2.5 — el modal reemplaza a window.confirm/prompt.
        modalAbierto: false,
        modalOnOk: null,
        focoPrevio: null
    };

    // ── utilidades de presentacion ──────────────────────────────────────────

    function fecha(iso) {
        if (!iso) return '';
        var d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        try {
            return d.toLocaleDateString('es-CL', {
                day: '2-digit', month: 'short', year: 'numeric'
            });
        } catch (err) {
            return d.toISOString().slice(0, 10);
        }
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function setError(message) {
        if (!errorEl) return;
        errorEl.hidden = !message;
        errorEl.textContent = message || '';
    }

    function setNotice(message) {
        if (!noticeEl) return;
        noticeEl.hidden = !message;
        noticeEl.textContent = message || '';
    }

    function correlationId() {
        return 'mem-' + Date.now() + '-' + Math.random().toString(36).slice(2, 8);
    }

    // ── modal ───────────────────────────────────────────────────────────────
    //
    // Reemplaza a window.confirm/prompt. Tres razones, y ninguna es estetica:
    //
    //   1. `prompt` devuelve una cadena sin limite ni contexto; aqui el motivo
    //      es un input acotado a 200 —lo mismo que acepta el servidor.
    //   2. `confirm` no puede mostrar QUE se esta aprobando. El modal enseña
    //      los mismos campos de la tarjeta, asi que la decision se toma
    //      mirando el contenido, no el titulo.
    //   3. El error del backend tiene donde aparecer sin cerrar el dialogo.
    //
    // Todo lo variable entra por textContent. El resumen sale del valor de una
    // memoria, que es contenido no confiable.

    function focoUtil() {
        if (!modal) return [];
        var nodos = [modal.cancelar, modal.ok];
        if (modal.motivoCaja && !modal.motivoCaja.hidden) nodos.unshift(modal.motivo);
        return nodos.filter(function (n) { return n && !n.disabled; });
    }

    function cerrarModal() {
        if (!modal || !state.modalAbierto) return;
        state.modalAbierto = false;
        state.modalOnOk = null;
        modal.raiz.hidden = true;
        modal.ok.disabled = false;
        modal.cancelar.disabled = false;
        // Devolver el foco a donde estaba: quien pulso Aprobar vuelve a su
        // tarjeta, no al principio del panel.
        var previo = state.focoPrevio;
        state.focoPrevio = null;
        if (previo && typeof previo.focus === 'function') {
            try { previo.focus(); } catch (err) { /* el nodo pudo repintarse */ }
        }
    }

    function abrirModal(cfg) {
        if (!modal) return;
        state.focoPrevio = document.activeElement;
        state.modalAbierto = true;
        state.modalOnOk = cfg.onOk || null;

        modal.titulo.textContent = cfg.titulo || '';
        modal.lead.textContent = cfg.lead || '';
        modal.cuerpo.textContent = '';
        (cfg.campos || []).forEach(function (f) {
            var row = el('div', 'ap-assistant-field');
            row.appendChild(el('span', 'ap-assistant-field__name', f.name));
            row.appendChild(el('span', 'ap-assistant-field__value', f.value));
            modal.cuerpo.appendChild(row);
        });

        var pideMotivo = !!cfg.conMotivo;
        modal.motivoCaja.hidden = !pideMotivo;
        modal.motivo.value = '';
        modal.error.hidden = true;
        modal.error.textContent = '';

        modal.ok.textContent = cfg.textoOk || 'Aceptar';
        modal.ok.classList.toggle('is-danger', !!cfg.peligro);
        modal.ok.classList.toggle('is-primary', !cfg.peligro);
        modal.ok.disabled = false;
        modal.cancelar.disabled = false;
        modal.cancelar.textContent = cfg.textoCancelar || 'Cancelar';

        modal.raiz.hidden = false;
        // El foco arranca donde el usuario va a actuar.
        var destino = pideMotivo ? modal.motivo : modal.ok;
        window.setTimeout(function () {
            try { destino.focus(); } catch (err) { /* ignore */ }
        }, 20);
    }

    function errorEnModal(mensaje) {
        if (!modal) return;
        modal.error.hidden = !mensaje;
        modal.error.textContent = mensaje || '';
        modal.ok.disabled = false;
        modal.cancelar.disabled = false;
    }

    if (modal) {
        modal.cancelar.addEventListener('click', cerrarModal);
        modal.velo.addEventListener('click', cerrarModal);
        modal.ok.addEventListener('click', function () {
            var fn = state.modalOnOk;
            if (!fn) { cerrarModal(); return; }
            modal.ok.disabled = true;
            modal.cancelar.disabled = true;
            fn(String(modal.motivo.value || '').trim().slice(0, 200));
        });
        // Captura: assistant.js tiene su propio Escape para cerrar el drawer.
        // Con el modal abierto, Escape cierra el modal y NADA mas.
        document.addEventListener('keydown', function (event) {
            if (!state.modalAbierto) return;
            if (event.key === 'Escape') {
                event.stopPropagation();
                event.preventDefault();
                cerrarModal();
                return;
            }
            if (event.key !== 'Tab') return;
            // Trampa de foco: el tabulador no sale del dialogo.
            var nodos = focoUtil();
            if (!nodos.length) return;
            var primero = nodos[0];
            var ultimo = nodos[nodos.length - 1];
            if (event.shiftKey && document.activeElement === primero) {
                event.preventDefault();
                ultimo.focus();
            } else if (!event.shiftKey && document.activeElement === ultimo) {
                event.preventDefault();
                primero.focus();
            }
        }, true);
    }

    // ── tarjeta ─────────────────────────────────────────────────────────────

    function metaRow(nombre, valor) {
        if (!valor) return null;
        var row = el('div', 'ap-assistant-field');
        row.appendChild(el('span', 'ap-assistant-field__name', nombre));
        row.appendChild(el('span', 'ap-assistant-field__value', valor));
        return row;
    }

    function acciones(item) {
        var box = el('div', 'ap-assistant-memory-actions');
        var ocupada = state.busyId === item.id;

        function boton(texto, clase, handler, titulo) {
            var b = el('button', 'ap-assistant-memory-btn ' + clase, texto);
            b.type = 'button';
            b.disabled = ocupada || state.loading;
            if (titulo) b.title = titulo;
            b.addEventListener('click', handler);
            return b;
        }

        // Una caducada no se aprueba ni se rechaza: el TTL ya la cerro y el
        // backend responde 409. Ofrecer el boton solo serviria para enseñar un
        // error.
        if (item.status === 'expired') {
            box.appendChild(el('p', 'ap-assistant-memory-note',
                'Ya no está en uso. Para volver a tenerla, pídesela a Andes de nuevo.'));
            return box;
        }

        if (item.status === 'suggested') {
            box.appendChild(boton('Aprobar', 'is-primary', function () {
                abrirModal({
                    titulo: 'Aprobar esta memoria',
                    lead: 'Andes podrá usarla en tus próximas conversaciones.',
                    campos: item.fields || [],
                    textoOk: 'Aprobar',
                    onOk: function () { moderar(item, 'approve', {}); }
                });
            }));
            box.appendChild(boton('Rechazar', 'is-ghost', function () {
                abrirModal({
                    titulo: 'Rechazar esta memoria',
                    lead: 'Andes no la usará. Podrás reconsiderarla más tarde.',
                    campos: item.fields || [],
                    conMotivo: true,
                    peligro: true,
                    textoOk: 'Rechazar',
                    onOk: function (motivo) {
                        moderar(item, 'reject', motivo ? { reason: motivo } : {});
                    }
                });
            }));
            return box;
        }

        // Secundarias: exigen confirmacion explicita porque revierten una
        // decision anterior, no responden a una sugerencia nueva.
        // Retirar y Reconsiderar revierten una decision anterior: confirmacion
        // explicita, y el dialogo dice que cambia.
        if (item.status === 'approved') {
            box.appendChild(boton('Retirar', 'is-ghost is-secondary', function () {
                abrirModal({
                    titulo: 'Retirar una memoria aprobada',
                    lead: 'Andes dejará de usarla a partir de ahora.',
                    campos: item.fields || [],
                    conMotivo: true,
                    peligro: true,
                    textoOk: 'Retirar',
                    onOk: function (motivo) {
                        var extra = { revoke: true };
                        if (motivo) extra.reason = motivo;
                        moderar(item, 'reject', extra);
                    }
                });
            }, 'Retirar una memoria que ya estaba aprobada'));
        }
        if (item.status === 'rejected') {
            box.appendChild(boton('Reconsiderar', 'is-ghost is-secondary', function () {
                abrirModal({
                    titulo: 'Reconsiderar una memoria rechazada',
                    lead: 'Volverás a permitir que Andes la use.',
                    campos: item.fields || [],
                    textoOk: 'Reconsiderar',
                    onOk: function () {
                        moderar(item, 'approve', { reconsider: true });
                    }
                });
            }, 'Rehabilitar una memoria que rechazaste antes'));
        }
        return box;
    }

    function cardEl(item) {
        var card = el('article', 'ap-assistant-memory-card');
        card.setAttribute('data-memory-id', item.id || '');
        if (state.busyId === item.id) card.classList.add('is-busy');

        var head = el('header', 'ap-assistant-memory-card__head');
        head.appendChild(el('span', 'ap-assistant-memory-card__title', item.title));
        head.appendChild(el('span',
            'ap-assistant-memory-chip is-' + item.status,
            ETIQUETAS[item.status] || item.status));
        card.appendChild(head);

        // Lo que Andes recordaria. Si el servidor no proyecto campos, no se
        // inventa nada: los avisos explican por que.
        (item.fields || []).forEach(function (f) {
            var row = metaRow(f.name, f.value);
            if (row) card.appendChild(row);
        });

        card.appendChild(el('p', 'ap-assistant-memory-why', item.why));

        var meta = el('div', 'ap-assistant-memory-meta');
        [
            ['Alcance', ALCANCES[item.scope] || item.scope],
            ['Conversación', item.conversation_id ? String(item.conversation_id).slice(0, 12) : ''],
            ['Creada', fecha(item.created_at)],
            ['Caduca', fecha(item.expires_at)],
            ['Confianza', item.confidence != null ? String(item.confidence) : '']
        ].forEach(function (par) {
            var row = metaRow(par[0], par[1]);
            if (row) meta.appendChild(row);
        });
        if (meta.childNodes.length) card.appendChild(meta);

        // FASE 10.2.5 — tres frases distintas para tres situaciones distintas,
        // y ninguna inventa nada:
        //
        //   transicion  la proyecta el servidor desde la auditoria, asi que
        //               sobrevive a recargar la pagina.
        //   humana      alguien la modero: hay status_by.
        //   heredada    migracion o escritura del sistema. NO se pinta como
        //               una aprobacion humana, porque no lo fue.
        var trans = item.just_changed || item.last_transition;
        if (trans) {
            var desde = trans.from || trans.previous_status;
            var hasta = trans.to || trans.new_status;
            var quien = trans.actor_user || item.status_by;
            var cuando = fecha(trans.ts || item.status_changed_at);
            var linea = (ETIQUETAS[desde] || desde) + ' → ' + (ETIQUETAS[hasta] || hasta);
            if (quien) linea += ' · ' + quien;
            if (cuando) linea += ' · ' + cuando;
            card.appendChild(el('p',
                'ap-assistant-memory-trace' + (item.just_changed ? ' is-fresh' : ''),
                linea));
        } else if (item.approval_origin === 'human' && item.status_changed_at) {
            card.appendChild(el('p', 'ap-assistant-memory-trace',
                (ETIQUETAS[item.status] || item.status) + ' por ' +
                (item.status_by || '—') + ' · ' + fecha(item.status_changed_at)));
        } else if (item.origin_note) {
            card.appendChild(el('p', 'ap-assistant-memory-trace is-inherited',
                item.origin_note));
        }

        (item.warnings || []).forEach(function (w) {
            card.appendChild(el('p', 'ap-assistant-memory-warn', w));
        });

        card.appendChild(acciones(item));
        return card;
    }

    // ── pintado ─────────────────────────────────────────────────────────────

    function visibles() {
        return state.items.filter(function (i) { return i.status === state.tab; });
    }

    function applyTabs() {
        tabs.forEach(function (tab) {
            var name = tab.getAttribute('data-memory-tab');
            var on = name === state.tab;
            tab.classList.toggle('is-active', on);
            tab.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        Object.keys(ETIQUETAS).forEach(function (name) {
            var node = panel.querySelector('[data-memory-count="' + name + '"]');
            if (node) node.textContent = String(state.counts[name] || 0);
        });
        var pendientes = state.counts.suggested || 0;
        if (badgeEl) {
            badgeEl.hidden = !pendientes;
            badgeEl.textContent = pendientes > 9 ? '9+' : String(pendientes);
        }
    }

    function applyList() {
        if (!listEl) return;
        listEl.textContent = '';
        if (state.loading && !state.loaded) {
            listEl.appendChild(el('p', 'ap-assistant-memory-note', 'Cargando…'));
            return;
        }
        var items = visibles();
        if (!items.length) {
            listEl.appendChild(el('p', 'ap-assistant-memory-note', vacio()));
            return;
        }
        items.forEach(function (i) { listEl.appendChild(cardEl(i)); });
    }

    function vacio() {
        if (state.q) return 'Ninguna memoria coincide con la búsqueda.';
        if (state.tab === 'suggested') return 'No hay nada pendiente de revisar.';
        if (state.tab === 'approved') return 'Todavía no aprobaste ninguna memoria.';
        if (state.tab === 'rejected') return 'No rechazaste ninguna memoria.';
        return 'No hay memorias expiradas.';
    }

    function render() {
        applyTabs();
        applyList();
    }

    // ── datos ───────────────────────────────────────────────────────────────

    function load(options) {
        options = options || {};
        if (state.loading) return Promise.resolve();
        state.loading = true;
        if (!options.keepNotice) setNotice('');
        setError('');
        render();
        return service.loadMemoryPanel({ q: state.q }).then(function (res) {
            state.loading = false;
            if (!res || !res.ok) {
                state.loaded = true;
                // Memoria apagada no es un fallo del panel: es el estado del
                // sistema, y decirlo evita que parezca que algo se rompio.
                setError(res && res.code === 'memory_disabled'
                    ? 'La memoria del asistente está desactivada.'
                    : (res && res.error) || 'No se pudo cargar la memoria.');
                state.items = [];
                state.counts = {};
                render();
                return;
            }
            state.loaded = true;
            state.items = res.items || [];
            state.counts = res.counts || {};
            render();
        }).catch(function () {
            state.loading = false;
            state.loaded = true;
            setError('No se pudo cargar la memoria.');
            render();
        });
    }

    function conflicto(item, mensaje) {
        // El unico error que el usuario vera con alguna frecuencia. NUNCA se
        // reintenta con una version nueva: aprobar algo que cambio despues de
        // mostrarse es exactamente lo que la version existe para impedir.
        //
        // El texto viene del backend, ya unificado con el de la UI: un solo
        // mensaje para una sola condicion.
        setError('');
        setNotice('');
        if (!modal) return;
        abrirModal({
            titulo: 'La memoria cambió',
            lead: mensaje || 'Esta memoria cambió desde que la abriste. ' +
                'Actualízala para revisar su estado actual.',
            campos: [],
            textoOk: 'Actualizar',
            textoCancelar: 'Cerrar',
            onOk: function () {
                cerrarModal();
                load();
            }
        });
    }

    function moderar(item, operation, extra) {
        if (state.busyId) return;
        state.busyId = item.id;
        setError('');
        setNotice('');
        render();

        var payload = {
            version: item.version,
            correlation_id: correlationId(),
            conversation_id: item.scope === 'conversation' ? item.conversation_id : null
        };
        Object.keys(extra || {}).forEach(function (k) { payload[k] = extra[k]; });

        service.moderateMemory(item.id, operation, payload).then(function (res) {
            state.busyId = null;
            if (!res || !res.ok) {
                if (res && (res.code === 'version_conflict' || res.code === 'status_conflict')) {
                    render();
                    conflicto(item, res.error);
                    return;
                }
                // Nada se movio: el estado anterior se conserva tal cual, y el
                // error se enseña DENTRO del dialogo para no perder el contexto
                // de que se estaba decidiendo.
                errorEnModal((res && res.error) || 'No se pudo completar la operación.');
                render();
                return;
            }
            cerrarModal();
            aplicarCambio(item, res);
        }).catch(function () {
            state.busyId = null;
            errorEnModal('No se pudo contactar el asistente.');
            render();
        });
    }

    function aplicarCambio(item, res) {
        var nuevo = (res.item && res.item.status) || item.status;
        var anterior = res.previous_status || item.status;

        state.items = state.items.map(function (i) {
            if (i.id !== item.id) return i;
            var copia = {};
            Object.keys(i).forEach(function (k) { copia[k] = i[k]; });
            copia.status = nuevo;
            copia.version = (res.item && res.item.version) || i.version;
            copia.status_by = (res.item && res.item.status_by) || i.status_by;
            copia.status_changed_at =
                (res.item && res.item.status_changed_at) || i.status_changed_at;
            copia.approval_origin = 'human';
            copia.origin_note = null;
            copia.just_changed = res.changed
                ? { from: anterior, to: nuevo, actor_user: copia.status_by }
                : null;
            return copia;
        });

        // Los recuentos se recalculan de lo que hay, no se incrementan a mano:
        // sumar y restar a ciegas es como los contadores se desincronizan.
        var conteo = { suggested: 0, approved: 0, rejected: 0, expired: 0 };
        state.items.forEach(function (i) {
            conteo[i.status] = (conteo[i.status] || 0) + 1;
        });
        state.counts = conteo;

        setNotice(res.changed
            ? 'Listo: ' + (ETIQUETAS[anterior] || anterior) + ' → ' +
              (ETIQUETAS[nuevo] || nuevo) + '.'
            : 'Sin cambios: ya estaba ' + (ETIQUETAS[nuevo] || nuevo).toLowerCase() + '.');
        render();
    }

    // ── eventos ─────────────────────────────────────────────────────────────

    tabs.forEach(function (tab) {
        tab.addEventListener('click', function () {
            state.tab = tab.getAttribute('data-memory-tab') || 'suggested';
            setNotice('');
            setError('');
            render();
        });
    });

    if (refreshEl) {
        refreshEl.addEventListener('click', function () { load(); });
    }

    if (searchEl) {
        var timer = null;
        searchEl.addEventListener('input', function () {
            state.q = String(searchEl.value || '').trim().slice(0, 80);
            if (timer) window.clearTimeout(timer);
            timer = window.setTimeout(function () { load(); }, 220);
        });
    }

    // assistant.js es quien decide que vista esta a la vista; el panel solo
    // reacciona, igual que el historial.
    window.addEventListener('andes-assistant-memory', function (event) {
        var open = !!(event.detail && event.detail.open);
        if (!open) return;
        state.tab = 'suggested';
        load();
    });

    window.AndesAssistantMemory = {
        refresh: load,
        state: state
    };

    render();
})();
