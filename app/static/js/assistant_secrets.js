/**
 * FASE 10.3.4-A — panel de secretos.
 *
 * REGLA UNICA DE ESTE ARCHIVO
 *
 *     El valor de un secreto existe en UNA variable local, dentro de UNA
 *     funcion, durante el tiempo de UN fetch. No entra en `state`, no se
 *     guarda en una variable de modulo, no se escribe en el DOM, y no se
 *     mete en localStorage ni en sessionStorage.
 *
 * Todo lo demas se sigue de ahi. No hay un `state.value`, no hay un borrador
 * que sobreviva al cambio de vista, y el <input> se vacia en cuanto su
 * contenido se ha enviado —o en cuanto se abandona el formulario—.
 *
 * MARCADO: createElement + textContent, nunca innerHTML. El nombre de un
 * secreto lo escribe el usuario y el recurso de una aprobacion puede venir de
 * una sugerencia del asistente: los dos son contenido no confiable. Con
 * innerHTML, un nombre con markup seria ejecucion de codigo; con textContent
 * es, como mucho, un nombre feo.
 *
 * LA PANTALLA DE APROBACION no se construye a mano. Se pinta recorriendo el
 * diccionario canonico que devuelve el servidor —el mismo que se convierte en
 * huella— para que no pueda ensenar una cosa mientras se firma otra.
 */
(function () {
    'use strict';

    var BASE = '/assistant/api/secrets';

    var panel = document.getElementById('ap-assistant-secrets');
    if (!panel) return;

    var listEl = document.getElementById('ap-assistant-secrets-list');
    var grantsEl = document.getElementById('ap-assistant-secrets-grants');
    var detailEl = document.getElementById('ap-assistant-secrets-detail');
    var approvalEl = document.getElementById('ap-assistant-secrets-approval');
    var errorEl = document.getElementById('ap-assistant-secrets-error');
    var noticeEl = document.getElementById('ap-assistant-secrets-notice');
    var formEl = document.getElementById('ap-assistant-secrets-form');
    var formErrorEl = document.getElementById('ap-assistant-secrets-formerror');
    var formTitleEl = document.getElementById('ap-assistant-secrets-formtitle');
    var kekEl = document.getElementById('ap-assistant-secrets-kek');
    var valueEl = document.getElementById('ap-assistant-secrets-value');
    var nameEl = document.getElementById('ap-assistant-secrets-name');
    var providerEl = document.getElementById('ap-assistant-secrets-provider');
    var purposesEl = document.getElementById('ap-assistant-secrets-purposes');
    var expiresEl = document.getElementById('ap-assistant-secrets-expires');
    var saveEl = document.getElementById('ap-assistant-secrets-save');
    var newEl = document.getElementById('ap-assistant-secrets-new');
    var refreshEl = document.getElementById('ap-assistant-secrets-refresh');

    var vistas = {
        list: document.getElementById('ap-assistant-secrets-view-list'),
        detail: document.getElementById('ap-assistant-secrets-view-detail'),
        form: document.getElementById('ap-assistant-secrets-view-form'),
        approval: document.getElementById('ap-assistant-secrets-view-approval'),
        grants: document.getElementById('ap-assistant-secrets-view-grants')
    };

    /**
     * Estado del panel. Metadatos y nada mas.
     *
     * Si alguna vez hiciera falta anadir un campo aqui, la pregunta es si su
     * contenido puede aparecer en un volcado de `state` — porque acabara
     * apareciendo.
     */
    var state = {
        items: [],
        counts: {},
        grants: [],
        tab: 'active',
        nav: 'secrets',
        view: 'list',
        config: null,
        rotating: null,   // id del secreto que se esta rotando, o null
        approval: null,   // { canonical, fingerprint, secret, purpose }
        loading: false
    };

    // ── utilidades de DOM ───────────────────────────────────────────────

    function el(tag, cls, text) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (text !== undefined && text !== null) n.textContent = String(text);
        return n;
    }

    function vaciar(node) {
        if (!node) return;
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"], meta[name="csrf_token"]');
        if (meta) return meta.getAttribute('content') || '';
        var root = document.getElementById('ap-assistant-root');
        return root ? (root.getAttribute('data-csrf-token') || '') : '';
    }

    function setError(msg) {
        if (!errorEl) return;
        errorEl.hidden = !msg;
        errorEl.textContent = msg || '';
    }

    function setFormError(msg) {
        if (!formErrorEl) return;
        formErrorEl.hidden = !msg;
        formErrorEl.textContent = msg || '';
    }

    function setNotice(msg) {
        if (!noticeEl) return;
        noticeEl.hidden = !msg;
        noticeEl.textContent = msg || '';
        if (msg) window.setTimeout(function () {
            if (noticeEl.textContent === msg) { noticeEl.hidden = true; noticeEl.textContent = ''; }
        }, 4000);
    }

    /**
     * Fetch con CSRF. `cuerpo` se serializa y se suelta: quien llama es
     * responsable de no guardarse una referencia a lo que metio dentro.
     */
    function pedir(metodo, ruta, cuerpo) {
        var opciones = {
            method: metodo,
            credentials: 'same-origin',
            headers: {
                'Accept': 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRF-Token': csrfToken()
            }
        };
        if (cuerpo !== undefined) {
            opciones.headers['Content-Type'] = 'application/json';
            opciones.body = JSON.stringify(cuerpo);
        }
        return fetch(ruta, opciones).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (datos) {
                return { status: r.status, datos: datos || {} };
            });
        });
    }

    function mensajeDe(datos, porDefecto) {
        return (datos && typeof datos.message === 'string' && datos.message)
            ? datos.message : porDefecto;
    }

    // ── navegacion interna ──────────────────────────────────────────────

    function mostrar(vista) {
        state.view = vista;
        Object.keys(vistas).forEach(function (k) {
            if (vistas[k]) vistas[k].hidden = (k !== vista);
        });
        setError('');
        if (vista !== 'form') limpiarFormulario();
    }

    /**
     * Vaciar el formulario NO es cosmetico.
     *
     * Un <input type=password> conserva su valor mientras el nodo viva, y el
     * drawer no se destruye al cambiar de vista. Sin esto, un valor escrito a
     * medias seguiria en el DOM hasta recargar la pagina.
     */
    function limpiarFormulario() {
        if (valueEl) valueEl.value = '';
        if (nameEl) nameEl.value = '';
        if (expiresEl) expiresEl.value = '';
        if (purposesEl) {
            Array.prototype.forEach.call(
                purposesEl.querySelectorAll('input[type=checkbox]'),
                function (c) { c.checked = false; });
        }
        setFormError('');
        state.rotating = null;
    }

    function irA(vista) {
        mostrar(vista);
        if (vista === 'grants') cargarGrants();
    }

    // ── carga ───────────────────────────────────────────────────────────

    function cargarConfig() {
        return pedir('GET', BASE + '/config').then(function (r) {
            if (r.status === 200 && r.datos.ok) {
                state.config = r.datos;
                pintarOpcionesFormulario();
            }
            return state.config;
        }).catch(function () { return null; });
    }

    function cargar() {
        if (state.loading) return Promise.resolve();
        state.loading = true;
        return pedir('GET', BASE).then(function (r) {
            state.loading = false;
            if (r.status !== 200 || !r.datos.ok) {
                setError(mensajeDe(r.datos, 'No se pudo cargar la lista.'));
                return;
            }
            state.items = Array.isArray(r.datos.items) ? r.datos.items : [];
            state.counts = r.datos.counts || {};
            pintarLista();
        }).catch(function () {
            state.loading = false;
            setError('No se pudo cargar la lista.');
        });
    }

    function cargarGrants() {
        return pedir('GET', BASE + '/grants').then(function (r) {
            if (r.status !== 200 || !r.datos.ok) {
                setError(mensajeDe(r.datos, 'No se pudieron cargar los permisos.'));
                return;
            }
            state.grants = Array.isArray(r.datos.items) ? r.datos.items : [];
            pintarGrants();
        }).catch(function () { setError('No se pudieron cargar los permisos.'); });
    }

    // ── pintado: inventario ─────────────────────────────────────────────

    var ETIQUETA_ESTADO = {
        active: 'Activo', expiring: 'Por vencer',
        revoked: 'Revocado', expired: 'Expirado'
    };

    function fecha(iso) {
        if (!iso) return '—';
        var d = new Date(String(iso));
        if (isNaN(d.getTime())) return String(iso);
        return d.toLocaleDateString('es-CL', {
            year: 'numeric', month: 'short', day: 'numeric'
        });
    }

    function campo(etiqueta, valor) {
        var f = el('div', 'ap-assistant-secrets-field-row');
        f.appendChild(el('span', 'ap-assistant-secrets-field-key', etiqueta));
        f.appendChild(el('span', 'ap-assistant-secrets-field-val',
            (valor === null || valor === undefined || valor === '') ? '—' : valor));
        return f;
    }

    function tarjeta(item) {
        var card = el('article', 'ap-assistant-secrets-card');
        card.setAttribute('data-secret-id', item.id);

        var head = el('header', 'ap-assistant-secrets-card__head');
        head.appendChild(el('span', 'ap-assistant-secrets-card__title', item.name));
        head.appendChild(el('span',
            'ap-assistant-secrets-chip is-' + item.bucket,
            ETIQUETA_ESTADO[item.bucket] || item.bucket));
        card.appendChild(head);

        var meta = el('div', 'ap-assistant-secrets-meta');
        meta.appendChild(campo('Proveedor', item.provider));
        meta.appendChild(campo('Versión', item.current_version !== null
            ? 'v' + item.current_version : '—'));
        meta.appendChild(campo('Usos permitidos',
            (item.purposes || []).join(', ')));
        meta.appendChild(campo('Vence', fecha(item.expires_at)));
        card.appendChild(meta);

        var acciones = el('div', 'ap-assistant-secrets-actions');
        var ver = el('button', 'ap-assistant-memory-btn', 'Ver detalle');
        ver.type = 'button';
        ver.addEventListener('click', function () { abrirDetalle(item.id); });
        acciones.appendChild(ver);

        if (item.bucket === 'active' || item.bucket === 'expiring') {
            var rot = el('button', 'ap-assistant-memory-btn', 'Rotar');
            rot.type = 'button';
            rot.addEventListener('click', function () { abrirRotacion(item); });
            acciones.appendChild(rot);

            var rev = el('button', 'ap-assistant-memory-btn', 'Revocar');
            rev.type = 'button';
            rev.addEventListener('click', function () { revocar(item); });
            acciones.appendChild(rev);
        }
        card.appendChild(acciones);
        return card;
    }

    function pintarLista() {
        Object.keys(ETIQUETA_ESTADO).forEach(function (b) {
            var n = panel.querySelector('[data-secrets-count="' + b + '"]');
            if (n) n.textContent = String(state.counts[b] || 0);
        });
        vaciar(listEl);
        var visibles = state.items.filter(function (i) { return i.bucket === state.tab; });
        if (!visibles.length) {
            listEl.appendChild(el('p', 'ap-assistant-secrets-empty',
                state.tab === 'active'
                    ? 'No tienes secretos guardados todavía.'
                    : 'Nada en esta pestaña.'));
            return;
        }
        visibles.forEach(function (i) { listEl.appendChild(tarjeta(i)); });
    }

    // ── pintado: detalle ────────────────────────────────────────────────

    function abrirDetalle(id) {
        pedir('GET', BASE + '/' + encodeURIComponent(id)).then(function (r) {
            if (r.status !== 200 || !r.datos.ok) {
                setError(mensajeDe(r.datos, 'No se pudo abrir el secreto.'));
                return;
            }
            pintarDetalle(r.datos);
            mostrar('detail');
        }).catch(function () { setError('No se pudo abrir el secreto.'); });
    }

    function pintarDetalle(datos) {
        var item = datos.item || {};
        vaciar(detailEl);

        detailEl.appendChild(el('h4', 'ap-assistant-secrets-formtitle', item.name));

        var meta = el('div', 'ap-assistant-secrets-meta');
        meta.appendChild(campo('Proveedor', item.provider));
        meta.appendChild(campo('Estado', ETIQUETA_ESTADO[item.bucket] || item.status));
        meta.appendChild(campo('Versión actual', item.current_version !== null
            ? 'v' + item.current_version : '—'));
        meta.appendChild(campo('Usos permitidos', (item.purposes || []).join(', ')));
        meta.appendChild(campo('Creado', fecha(item.created_at)));
        meta.appendChild(campo('Creado por', item.created_by));
        meta.appendChild(campo('Última modificación', fecha(item.updated_at)));
        meta.appendChild(campo('Vence', fecha(item.expires_at)));
        detailEl.appendChild(meta);

        // Versiones. Se ensena que existen y en que estado; el contenido de
        // cada una no existe para esta pantalla.
        detailEl.appendChild(el('h5', 'ap-assistant-secrets-subtitle', 'Versiones'));
        var vers = el('div', 'ap-assistant-secrets-versions');
        (datos.versions || []).forEach(function (v) {
            var fila = el('div', 'ap-assistant-secrets-version');
            var etiqueta = 'v' + v.version;
            if (v.version === item.current_version && v.status === 'active') {
                etiqueta += ' · actual';
            }
            fila.appendChild(el('span', 'ap-assistant-secrets-version__id', etiqueta));
            fila.appendChild(el('span', 'ap-assistant-secrets-version__st', v.status));
            fila.appendChild(el('span', 'ap-assistant-secrets-version__dt', fecha(v.created_at)));
            vers.appendChild(fila);
        });
        if (!(datos.versions || []).length) {
            vers.appendChild(el('p', 'ap-assistant-secrets-empty', 'Sin versiones.'));
        }
        detailEl.appendChild(vers);

        // Historial de uso: quien autorizo que, y como acabo.
        detailEl.appendChild(el('h5', 'ap-assistant-secrets-subtitle', 'Historial de uso'));
        var uso = el('div', 'ap-assistant-secrets-usage');
        (datos.usage || []).forEach(function (u) {
            var fila = el('article', 'ap-assistant-secrets-use');
            fila.appendChild(el('span', 'ap-assistant-secrets-use__act', u.action));
            fila.appendChild(el('span', 'ap-assistant-secrets-use__st', u.status));
            fila.appendChild(el('span', 'ap-assistant-secrets-use__dt', fecha(u.issued_at)));
            fila.appendChild(el('span', 'ap-assistant-secrets-use__by',
                'autorizó ' + (u.approved_by || '—')));
            uso.appendChild(fila);
        });
        if (!(datos.usage || []).length) {
            uso.appendChild(el('p', 'ap-assistant-secrets-empty',
                'Todavía no se ha usado.'));
        }
        detailEl.appendChild(uso);
    }

    // ── pintado: grants ─────────────────────────────────────────────────

    function segundos(n) {
        n = Math.max(0, parseInt(n, 10) || 0);
        var m = Math.floor(n / 60);
        return m > 0 ? (m + ' min ' + (n % 60) + ' s') : (n + ' s');
    }

    function pintarGrants() {
        var vivos = state.grants.filter(function (g) { return g.status === 'issued'; });
        var n = panel.querySelector('[data-secrets-grantcount]');
        if (n) n.textContent = String(vivos.length);

        vaciar(grantsEl);
        if (!state.grants.length) {
            grantsEl.appendChild(el('p', 'ap-assistant-secrets-empty',
                'No has autorizado ningún uso.'));
            return;
        }
        state.grants.forEach(function (g) {
            var card = el('article', 'ap-assistant-secrets-card');
            var head = el('header', 'ap-assistant-secrets-card__head');
            head.appendChild(el('span', 'ap-assistant-secrets-card__title', g.action));
            head.appendChild(el('span', 'ap-assistant-secrets-chip is-' + g.status, g.status));
            card.appendChild(head);

            var meta = el('div', 'ap-assistant-secrets-meta');
            meta.appendChild(campo('Propósito', g.purpose));
            meta.appendChild(campo('Recurso', g.resource));
            meta.appendChild(campo('Solicitado por', g.actor));
            meta.appendChild(campo('Versión', 'v' + g.secret_version));
            if (g.status === 'issued') {
                meta.appendChild(campo('Caduca en', segundos(g.seconds_left)));
            } else if (g.consumed_at) {
                meta.appendChild(campo('Usado', fecha(g.consumed_at)));
            }
            card.appendChild(meta);

            if (g.status === 'issued') {
                var acciones = el('div', 'ap-assistant-secrets-actions');
                var b = el('button', 'ap-assistant-memory-btn', 'Cancelar permiso');
                b.type = 'button';
                b.addEventListener('click', function () { revocarGrant(g.id); });
                acciones.appendChild(b);
                card.appendChild(acciones);
            }
            grantsEl.appendChild(card);
        });
    }

    function revocarGrant(id) {
        pedir('POST', BASE + '/grants/' + encodeURIComponent(id) + '/revoke', {})
            .then(function (r) {
                if (r.status !== 200 || !r.datos.ok) {
                    setError(mensajeDe(r.datos, 'No se pudo cancelar.'));
                    return;
                }
                setNotice('Permiso cancelado.');
                cargarGrants();
            }).catch(function () { setError('No se pudo cancelar.'); });
    }

    // ── pantalla de aprobacion ──────────────────────────────────────────

    /**
     * Etiquetas de los campos canonicos. Si el servidor anade un campo nuevo a
     * la estructura, aqui no hay etiqueta para el — y en vez de esconderlo se
     * pinta con su clave cruda. Es feo a proposito: un campo firmado que no se
     * ve es exactamente el fallo que esta pantalla existe para evitar.
     */
    var ETIQUETA_CANONICA = {
        action: 'Acción',
        method: 'Método',
        resource: 'Recurso',
        header_keys: 'Cabeceras que se envían',
        slots: 'Huecos de secreto',
        has_body: 'Lleva cuerpo'
    };

    function valorCanonico(v) {
        if (Array.isArray(v)) return v.length ? v.join(', ') : '(ninguna)';
        if (typeof v === 'boolean') return v ? 'Sí' : 'No';
        return String(v);
    }

    function pintarAprobacion(datos) {
        state.approval = datos;
        vaciar(approvalEl);

        var secreto = datos.secret || {};
        var meta = el('div', 'ap-assistant-secrets-meta');
        meta.appendChild(campo('Secreto', secreto.name));
        meta.appendChild(campo('Versión', secreto.current_version !== null
            ? 'v' + secreto.current_version : '—'));
        meta.appendChild(campo('Propósito', datos.purpose));
        meta.appendChild(campo('El permiso caduca en',
            segundos(datos.grant_ttl_seconds)));
        approvalEl.appendChild(meta);

        approvalEl.appendChild(el('h5', 'ap-assistant-secrets-subtitle',
            'Operación autorizada'));

        // Se recorre la estructura, no una lista escrita a mano. Lo que se ve
        // es lo que entra en la huella, campo por campo.
        var canon = datos.canonical || {};
        var caja = el('div', 'ap-assistant-secrets-canon');
        Object.keys(canon).sort().forEach(function (k) {
            caja.appendChild(campo(ETIQUETA_CANONICA[k] || k, valorCanonico(canon[k])));
        });
        approvalEl.appendChild(caja);

        approvalEl.appendChild(el('p', 'ap-assistant-secrets-hint',
            'El valor del secreto no viaja a esta pantalla en ningún momento. ' +
            'Se añade en el servidor, solo al ejecutar.'));

        var acciones = el('div', 'ap-assistant-secrets-actions');
        var cancelar = el('button', 'ap-assistant-memory-btn', 'Cancelar');
        cancelar.type = 'button';
        cancelar.addEventListener('click', function () { irA('list'); });
        acciones.appendChild(cancelar);

        var ok = el('button', 'ap-assistant-memory-btn is-primary', 'Autorizar');
        ok.type = 'button';
        ok.addEventListener('click', confirmarAprobacion);
        acciones.appendChild(ok);
        approvalEl.appendChild(acciones);
    }

    /**
     * Al confirmar se devuelve la huella QUE SE MOSTRO. El servidor recalcula
     * la suya y rechaza si no coinciden. Sin esto, "lo que ves es lo que
     * firmas" seria una promesa; con esto es una comprobacion.
     */
    function confirmarAprobacion() {
        if (!state.approval) return;
        var a = state.approval;
        var cuerpo = {
            secret_id: (a.secret || {}).id,
            purpose: a.purpose,
            action: a.canonical.action,
            resource: a.canonical.resource,
            method: a.canonical.method,
            headers: a.headers_map || {},
            has_body: !!a.canonical.has_body,
            body_slot: a.body_slot || '',
            fingerprint: a.fingerprint
        };
        pedir('POST', BASE + '/approvals', cuerpo).then(function (r) {
            if (r.status !== 201 || !r.datos.ok) {
                setError(mensajeDe(r.datos, 'No se pudo autorizar.'));
                return;
            }
            state.approval = null;
            setNotice('Uso autorizado. El permiso caduca solo.');
            state.nav = 'grants';
            pintarNav();
            irA('grants');
        }).catch(function () { setError('No se pudo autorizar.'); });
    }

    /**
     * Punto de entrada desde fuera: quien quiera pedir una aprobacion dispara
     * este evento con la forma de la peticion. El secreto no entra aqui —no
     * hay parametro donde meterlo—.
     */
    window.addEventListener('andes-assistant-secret-approval', function (ev) {
        var d = (ev && ev.detail) || {};
        pedir('POST', BASE + '/approvals/preview', d).then(function (r) {
            if (r.status !== 200 || !r.datos.ok) {
                setError(mensajeDe(r.datos, 'No se pudo preparar la autorización.'));
                return;
            }
            var datos = r.datos;
            datos.headers_map = d.headers || {};
            datos.body_slot = d.body_slot || '';
            pintarAprobacion(datos);
            mostrar('approval');
        }).catch(function () { setError('No se pudo preparar la autorización.'); });
    });

    // ── alta y rotacion ─────────────────────────────────────────────────

    function pintarOpcionesFormulario() {
        if (!state.config) return;
        if (providerEl) {
            vaciar(providerEl);
            (state.config.providers || []).forEach(function (p) {
                var o = document.createElement('option');
                o.value = p;
                o.textContent = p;
                providerEl.appendChild(o);
            });
        }
        if (purposesEl) {
            vaciar(purposesEl);
            (state.config.purposes || []).forEach(function (p) {
                var id = 'ap-secrets-purpose-' + p;
                var wrap = el('label', 'ap-assistant-secrets-check');
                var c = document.createElement('input');
                c.type = 'checkbox';
                c.value = p;
                c.id = id;
                wrap.appendChild(c);
                wrap.appendChild(el('span', null, p));
                purposesEl.appendChild(wrap);
            });
        }
    }

    function soloDeCreacion(mostrarlos) {
        Array.prototype.forEach.call(
            panel.querySelectorAll('[data-secrets-only="create"]'),
            function (n) { n.hidden = !mostrarlos; });
    }

    function abrirAlta() {
        limpiarFormulario();
        state.rotating = null;
        if (formTitleEl) formTitleEl.textContent = 'Añadir secreto';
        if (saveEl) saveEl.textContent = 'Guardar';
        soloDeCreacion(true);
        // El aviso de la clave se ensena antes del primero, que es cuando
        // significa algo.
        if (kekEl) kekEl.hidden = !(state.config && state.config.first_secret);
        mostrar('form');
        if (valueEl) window.setTimeout(function () { valueEl.focus(); }, 60);
    }

    function abrirRotacion(item) {
        limpiarFormulario();
        state.rotating = item.id;
        if (formTitleEl) {
            formTitleEl.textContent = 'Crear nueva versión';
        }
        if (saveEl) saveEl.textContent = 'Crear versión';
        soloDeCreacion(false);
        if (kekEl) kekEl.hidden = true;
        mostrar('form');
        setFormError('');
        if (valueEl) window.setTimeout(function () { valueEl.focus(); }, 60);
    }

    /**
     * El unico sitio del archivo que toca texto plano.
     *
     * Se lee del input a una local, se vacia el input INMEDIATAMENTE, se
     * construye el cuerpo, se envia, y la local se suelta al salir. No se
     * guarda en `state`, no se reintenta con el valor en memoria, y si el
     * envio falla el usuario lo pega otra vez: un reintento comodo costaria
     * tener el secreto vivo indefinidamente.
     */
    function enviar(ev) {
        if (ev) ev.preventDefault();
        setFormError('');

        var valor = valueEl ? valueEl.value : '';
        if (valueEl) valueEl.value = '';
        if (!valor) {
            setFormError('Pega el valor del secreto.');
            return;
        }

        var rotando = state.rotating;
        var ruta, cuerpo;
        if (rotando) {
            ruta = BASE + '/' + encodeURIComponent(rotando) + '/rotate';
            cuerpo = { value: valor };
        } else {
            var propositos = [];
            if (purposesEl) {
                Array.prototype.forEach.call(
                    purposesEl.querySelectorAll('input[type=checkbox]'),
                    function (c) { if (c.checked) propositos.push(c.value); });
            }
            if (!nameEl || !nameEl.value.trim()) {
                setFormError('Ponle un nombre.');
                valor = '';
                return;
            }
            if (!propositos.length) {
                setFormError('Marca al menos un uso permitido.');
                valor = '';
                return;
            }
            ruta = BASE;
            cuerpo = {
                name: nameEl.value.trim(),
                provider: providerEl ? providerEl.value : '',
                purposes: propositos,
                value: valor
            };
            if (expiresEl && expiresEl.value) cuerpo.expires_at = expiresEl.value;
        }

        if (saveEl) saveEl.disabled = true;
        pedir('POST', ruta, cuerpo).then(function (r) {
            // El cuerpo se suelta en cuanto la peticion sale.
            cuerpo.value = '';
            valor = '';
            if (saveEl) saveEl.disabled = false;
            if ((r.status !== 201 && r.status !== 200) || !r.datos.ok) {
                setFormError(mensajeDe(r.datos, 'No se pudo guardar.'));
                return;
            }
            limpiarFormulario();
            setNotice(rotando ? 'Nueva versión creada.' : 'Secreto guardado.');
            state.config = null;
            cargarConfig();
            cargar().then(function () { irA('list'); });
        }).catch(function () {
            cuerpo.value = '';
            valor = '';
            if (saveEl) saveEl.disabled = false;
            setFormError('No se pudo guardar.');
        });
    }

    function revocar(item) {
        pedir('POST', BASE + '/' + encodeURIComponent(item.id) + '/revoke', {})
            .then(function (r) {
                if (r.status !== 200 || !r.datos.ok) {
                    setError(mensajeDe(r.datos, 'No se pudo revocar.'));
                    return;
                }
                setNotice('Secreto revocado. Las versiones quedan guardadas.');
                cargar();
            }).catch(function () { setError('No se pudo revocar.'); });
    }

    // ── enganches ───────────────────────────────────────────────────────

    function pintarNav() {
        Array.prototype.forEach.call(
            panel.querySelectorAll('[data-secrets-nav]'), function (b) {
                var activo = b.getAttribute('data-secrets-nav') === state.nav;
                b.classList.toggle('is-active', activo);
                b.setAttribute('aria-selected', activo ? 'true' : 'false');
            });
    }

    Array.prototype.forEach.call(
        panel.querySelectorAll('[data-secrets-nav]'), function (b) {
            b.addEventListener('click', function () {
                state.nav = b.getAttribute('data-secrets-nav');
                pintarNav();
                irA(state.nav === 'grants' ? 'grants' : 'list');
            });
        });

    Array.prototype.forEach.call(
        panel.querySelectorAll('[data-secrets-tab]'), function (b) {
            b.addEventListener('click', function () {
                state.tab = b.getAttribute('data-secrets-tab');
                Array.prototype.forEach.call(
                    panel.querySelectorAll('[data-secrets-tab]'), function (o) {
                        var activo = o === b;
                        o.classList.toggle('is-active', activo);
                        o.setAttribute('aria-selected', activo ? 'true' : 'false');
                    });
                pintarLista();
            });
        });

    Array.prototype.forEach.call(
        panel.querySelectorAll('[data-secrets-back]'), function (b) {
            b.addEventListener('click', function () {
                state.nav = 'secrets';
                pintarNav();
                irA('list');
            });
        });

    if (newEl) newEl.addEventListener('click', abrirAlta);
    if (refreshEl) refreshEl.addEventListener('click', function () { cargar(); });
    if (formEl) formEl.addEventListener('submit', enviar);

    var cargado = false;
    window.addEventListener('andes-assistant-secrets', function (ev) {
        var abierto = !!(ev && ev.detail && ev.detail.open);
        if (!abierto) {
            // Al cerrar el panel se vacia el formulario. Un valor a medio
            // escribir no tiene por que sobrevivir a un cambio de vista.
            limpiarFormulario();
            return;
        }
        if (!cargado) {
            cargado = true;
            cargarConfig().then(function () { return cargar(); });
        } else {
            cargar();
        }
    });
})();
