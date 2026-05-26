console.log('[SII autofill] Script cargado, buscando campos RUT...');

(function (global) {
    'use strict';

    var LOG_PREFIX = '[SII autofill]';
    var SESSION_KEY = 'andes_sii_contribuyente_cache';
    var DEBOUNCE_MS = 400;
    var MIN_RUT_DIGITS = 8;
    var SPINNER_DELAY_MS = 1000;

    function log() {
        if (typeof console !== 'undefined' && console.log) {
            console.log.apply(console, [LOG_PREFIX].concat(Array.prototype.slice.call(arguments)));
        }
    }

    function logSii() {
        if (typeof console !== 'undefined' && console.log) {
            console.log.apply(console, ['[SII]'].concat(Array.prototype.slice.call(arguments)));
        }
    }

    function logSiiError() {
        if (typeof console !== 'undefined' && console.error) {
            console.error.apply(console, ['[SII]'].concat(Array.prototype.slice.call(arguments)));
        }
    }

    function normalize(value) {
        return String(value || '').trim().toLowerCase();
    }

    function rutCompact(raw) {
        return String(raw || '').replace(/[^0-9kK]/gi, '').toUpperCase();
    }

    function rutDigitCount(raw) {
        return rutCompact(raw).length;
    }

    function formatRutDisplay(body, dv) {
        var chunks = [];
        var b = body;
        while (b.length > 3) {
            chunks.unshift(b.slice(-3));
            b = b.slice(0, -3);
        }
        chunks.unshift(b);
        return chunks.join('.') + '-' + dv;
    }

    /**
     * Normaliza cualquier formato de RUT chileno.
     * @returns {{ valid: boolean, api: string, display: string, compact: string }}
     */
    function parseRut(raw) {
        var letters = rutCompact(raw);
        if (!letters) {
            return { valid: false, api: '', display: '', compact: '' };
        }

        var body;
        var dv;

        if (letters.length >= 8 && letters.length <= 10) {
            dv = letters.slice(-1);
            body = letters.slice(0, -1).replace(/\D/g, '');
        } else if (letters.length >= 2) {
            dv = letters.slice(-1);
            body = letters.slice(0, -1).replace(/\D/g, '');
        } else {
            return { valid: false, api: '', display: '', compact: '' };
        }

        if (!/^\d+$/.test(body) || body.length < 7 || body.length > 9) {
            return { valid: false, api: '', display: '', compact: '' };
        }
        if (!/^[0-9K]$/.test(dv)) {
            return { valid: false, api: '', display: '', compact: '' };
        }

        var compact = body + dv;
        var api = body + '-' + dv;
        var display = '';
        if (global.RutUtils && global.RutUtils.format) {
            display = global.RutUtils.format(compact);
        }
        if (!display) {
            display = formatRutDisplay(body, dv);
        }

        return { valid: true, api: api, display: display, compact: compact };
    }

    function formatRutForApi(raw) {
        var parsed = parseRut(raw);
        return parsed.valid ? parsed.api : '';
    }

    function cacheKeyFromRaw(raw) {
        var parsed = parseRut(raw);
        return parsed.valid ? parsed.compact : rutCompact(raw);
    }

    function isMaskedSiiText(text) {
        var s = String(text || '').trim();
        if (!s) return true;
        return !s.replace(/[\s*]+/g, '');
    }

    function normalizeApiPayload(data) {
        if (!data || data.error) return data;
        var out = {
            razon_social: data.razon_social || '',
            giro: data.giro || '',
            direccion: data.direccion || '',
            comuna: data.comuna || '',
            region: data.region || '',
            estado_sii: data.estado_sii || '',
            rut_valido_sii: !!data.rut_valido_sii,
            nombre_privado_sii: !!data.nombre_privado_sii,
        };
        if (isMaskedSiiText(out.razon_social)) out.razon_social = '';
        if (isMaskedSiiText(out.giro)) out.giro = '';
        if (out.nombre_privado_sii || (out.rut_valido_sii && !out.razon_social)) {
            out.nombre_privado_sii = true;
            out.razon_social = '';
            if (isMaskedSiiText(data.giro)) out.giro = '';
        }
        return out;
    }

    function readSessionStore() {
        try {
            var raw = global.sessionStorage.getItem(SESSION_KEY);
            if (!raw) return {};
            var parsed = JSON.parse(raw);
            return parsed && typeof parsed === 'object' ? parsed : {};
        } catch (_) {
            return {};
        }
    }

    function writeSessionStore(store) {
        try {
            global.sessionStorage.setItem(SESSION_KEY, JSON.stringify(store));
        } catch (_) {
            /* quota / private mode */
        }
    }

    function getSessionCache(key) {
        if (!key) return null;
        var store = readSessionStore();
        var entry = store[key];
        if (!entry) return null;
        if (entry.error) {
            return { error: entry.error, _fromSession: true };
        }
        if (entry.datos) {
            var copy = Object.assign({}, entry.datos);
            if (entry.local) copy._local = true;
            copy._fromSession = true;
            return copy;
        }
        return null;
    }

    /** ¿La entrada en sessionStorage evita llamar a la API? */
    function isSessionCacheSufficient(hit) {
        if (!hit) return false;
        if (hit.error) return true;
        if (hit._local && hit.razon_social && !isMaskedSiiText(hit.razon_social)) {
            return true;
        }
        if (hit.rut_valido_sii || hit.nombre_privado_sii) return true;
        if (hit.razon_social && !isMaskedSiiText(hit.razon_social)) return true;
        return false;
    }

    function setSessionCache(key, datos, isError) {
        if (!key) return;
        var store = readSessionStore();
        if (isError) {
            store[key] = { error: datos.error || 'RUT no encontrado', ts: Date.now() };
        } else {
            store[key] = { datos: datos, ts: Date.now() };
        }
        writeSessionStore(store);
    }

    function preloadPartiesFromConfig(cfg) {
        var list = cfg.partiesPreload;
        if (!Array.isArray(list) || !list.length) return;
        var store = readSessionStore();
        var added = 0;
        list.forEach(function (item) {
            if (!item || !item.rut) return;
            var key = cacheKeyFromRaw(item.rut);
            if (!key || key.length < MIN_RUT_DIGITS) return;
            if (store[key]) return;
            if (!(item.razon_social || '').trim()) return;
            store[key] = {
                datos: {
                    razon_social: item.razon_social || '',
                    giro: item.giro || '',
                    direccion: item.direccion || '',
                    comuna: item.comuna || '',
                    region: item.region || '',
                    estado_sii: item.estado_sii || 'REGISTRO LOCAL',
                },
                ts: Date.now(),
                local: true,
            };
            added += 1;
        });
        writeSessionStore(store);
        log('Precarga local:', added, 'RUTs en sessionStorage');
    }

    function readConfig() {
        var el = document.getElementById('siiContribuyenteConfig');
        if (!el) return null;
        try {
            var cfg = JSON.parse(el.textContent || '{}');
            global.siiAutofillConfig = cfg;
            return cfg;
        } catch (err) {
            log('Error al parsear config:', err);
            global.siiAutofillConfig = null;
            return null;
        }
    }

    function getEl(id) {
        return id ? document.getElementById(id) : null;
    }

    function readChileGeo() {
        var el = document.getElementById('chileGeoData');
        if (!el) return [];
        try {
            var data = JSON.parse(el.textContent || '[]');
            return Array.isArray(data) ? data : [];
        } catch (_) {
            return [];
        }
    }

    function matchRegionName(siiRegion, chileGeo) {
        var target = normalize(siiRegion).replace(/^region\s+/, '');
        if (!target) return '';
        for (var i = 0; i < chileGeo.length; i += 1) {
            var nombre = chileGeo[i].nombre || '';
            var rn = normalize(nombre);
            if (rn === target || target.indexOf(rn) !== -1 || rn.indexOf(target) !== -1) return nombre;
            if (target.indexOf('metropolitana') !== -1 && rn.indexOf('metropolitana') !== -1) return nombre;
        }
        return siiRegion || '';
    }

    function findRegionData(regionName, chileGeo) {
        return chileGeo.find(function (item) {
            return normalize(item.nombre) === normalize(regionName);
        });
    }

    function matchComunaName(comunaName, comunas) {
        var target = normalize(comunaName);
        if (!target) return '';
        var j;
        for (j = 0; j < comunas.length; j += 1) {
            if (normalize(comunas[j]) === target) return comunas[j];
        }
        for (j = 0; j < comunas.length; j += 1) {
            var cn = normalize(comunas[j]);
            if (cn.indexOf(target) !== -1 || target.indexOf(cn) !== -1) return comunas[j];
        }
        return comunaName || '';
    }

    function populateComunas(regionSelect, comunaSelect, chileGeo, regionName, selectedComuna) {
        var region = findRegionData(regionName, chileGeo);
        var comunas = region && Array.isArray(region.comunas) ? region.comunas : [];
        comunaSelect.innerHTML = '';
        var placeholder = document.createElement('option');
        placeholder.value = '';
        placeholder.textContent = 'Seleccionar comuna';
        comunaSelect.appendChild(placeholder);
        comunas.forEach(function (comuna) {
            var option = document.createElement('option');
            option.value = comuna;
            option.textContent = comuna;
            comunaSelect.appendChild(option);
        });
        var matched = matchComunaName(selectedComuna, comunas);
        if (matched) {
            if (comunas.indexOf(matched) === -1) {
                var extra = document.createElement('option');
                extra.value = matched;
                extra.textContent = matched;
                comunaSelect.appendChild(extra);
            }
            comunaSelect.value = matched;
        }
    }

    function isRegionMetropolitana(regionName) {
        return normalize(regionName).indexOf('metropolitana') !== -1;
    }

    function defaultCiudad(regionName, comuna) {
        if (!comuna) return '';
        return isRegionMetropolitana(regionName) ? 'Santiago' : comuna;
    }

    function isSinInicioActividades(estadoSii) {
        return String(estadoSii || '').toUpperCase().indexOf('SIN INICIO DE ACTIVIDADES') !== -1;
    }

    function setBadge(badgeEl, state, message, title) {
        if (!badgeEl) return;
        badgeEl.hidden = !state;
        if (!state) {
            badgeEl.removeAttribute('title');
            return;
        }
        badgeEl.textContent = message;
        badgeEl.className = 'sii-rut-badge';
        if (state === 'ok') badgeEl.classList.add('sii-rut-badge--ok');
        if (state === 'info') badgeEl.classList.add('sii-rut-badge--info');
        if (state === 'warn') badgeEl.classList.add('sii-rut-badge--warn');
        if (title) {
            badgeEl.setAttribute('title', title);
        } else {
            badgeEl.removeAttribute('title');
        }
    }

    function setSpinner(spinnerEl, visible) {
        if (!spinnerEl) return;
        spinnerEl.hidden = !visible;
        spinnerEl.setAttribute('aria-hidden', visible ? 'false' : 'true');
    }

    function applyChileGeoLocation(profile, region, comuna, ciudadSii) {
        var paisSelect = document.getElementById('pais');
        var regionSelect = getEl(profile.regionFieldId);
        var comunaSelect = getEl(profile.comunaFieldId);
        var regionText = document.getElementById('region_text');
        var comunaText = document.getElementById('comuna_text');
        var ciudadInput = getEl(profile.ciudadFieldId);
        if (!regionSelect || !comunaSelect) return;

        var chileGeo = readChileGeo();
        if (paisSelect) {
            var isChile =
                normalize(paisSelect.value) === 'chile' || normalize(paisSelect.value) === 'cl';
            if (!isChile) {
                paisSelect.value = 'Chile';
                paisSelect.dispatchEvent(new Event('change'));
            }
        }

        global.setTimeout(function () {
            var regionName = matchRegionName(region, chileGeo) || region;
            if (regionName) {
                var exists = Array.from(regionSelect.options).some(function (opt) {
                    return normalize(opt.value) === normalize(regionName);
                });
                if (exists) regionSelect.value = regionName;
            }
            if (regionText) regionText.value = regionSelect.value || regionName || '';
            populateComunas(regionSelect, comunaSelect, chileGeo, regionSelect.value, comuna);
            if (comunaText) comunaText.value = comunaSelect.value || comuna || '';
            if (ciudadInput) {
                var ciudad =
                    ciudadSii ||
                    defaultCiudad(regionSelect.value, comunaSelect.value || comuna);
                if (ciudad) ciudadInput.value = ciudad;
            }
            regionSelect.dispatchEvent(new Event('change'));
            comunaSelect.dispatchEvent(new Event('change'));
        }, 50);
    }

    function applyInlineCityLocation(profile, region, comuna) {
        var regionSelect = getEl(profile.regionFieldId);
        var ciudadSelect = getEl(profile.comunaFieldId);
        if (!regionSelect || !ciudadSelect) return;

        var chileGeo = readChileGeo();
        var regionName = matchRegionName(region, chileGeo) || region;
        if (regionName) {
            var exists = Array.from(regionSelect.options).some(function (opt) {
                return normalize(opt.value) === normalize(regionName);
            });
            if (exists) regionSelect.value = regionName;
            regionSelect.dispatchEvent(new Event('change'));
        }

        global.setTimeout(function () {
            if (!comuna) return;
            var matched = matchComunaName(comuna, Array.from(ciudadSelect.options).map(function (o) {
                return o.value;
            }).filter(Boolean));
            if (matched) {
                var hasOpt = Array.from(ciudadSelect.options).some(function (o) {
                    return o.value === matched;
                });
                if (!hasOpt) {
                    var extra = document.createElement('option');
                    extra.value = matched;
                    extra.textContent = matched;
                    ciudadSelect.appendChild(extra);
                }
                ciudadSelect.value = matched;
                ciudadSelect.dispatchEvent(new Event('change'));
            }
        }, 120);
    }

    function bindProfile(cfg, profile) {
        var rutInput = getEl(profile.rutInputId);
        if (!rutInput) {
            log('Perfil sin campo RUT #' + profile.rutInputId);
            return null;
        }
        var bindKey = profile.rutInputId || rutInput.id;
        if (rutInput.dataset.siiAutofillBound === bindKey) {
            return null;
        }
        rutInput.dataset.siiAutofillBound = bindKey;

        log('Enlazado #' + bindKey + ' (input+blur, debounce ' + DEBOUNCE_MS + 'ms)');

        var nombreInput = getEl(profile.nombreFieldId);
        var empresaInput = getEl(profile.empresaFieldId);
        var giroInput = getEl(profile.giroFieldId);
        var direccionInput = getEl(profile.direccionFieldId);
        var badgeEl = getEl(profile.badgeId);
        var spinnerEl = getEl(profile.spinnerId);

        var state = {
            lastRutApi: '',
            lastCacheKey: '',
            pendingData: null,
            pendingError: null,
            inflight: null,
            inflightKey: '',
            spinnerTimer: null,
            debounceTimer: null,
        };

        function showSpinnerDelayed() {
            if (state.spinnerTimer) global.clearTimeout(state.spinnerTimer);
            state.spinnerTimer = global.setTimeout(function () {
                setSpinner(spinnerEl, true);
                state.spinnerTimer = null;
            }, SPINNER_DELAY_MS);
        }

        function hideSpinnerNow() {
            if (state.spinnerTimer) {
                global.clearTimeout(state.spinnerTimer);
                state.spinnerTimer = null;
            }
            setSpinner(spinnerEl, false);
        }

        function applyFormattedRutToInput() {
            var parsed = parseRut(rutInput.value);
            if (parsed.valid) {
                rutInput.value = parsed.display;
            }
        }

        function fillFields(data) {
            if (!data || data.error) return;
            if (empresaInput && data.razon_social) empresaInput.value = data.razon_social;
            if (nombreInput && data.razon_social) nombreInput.value = data.razon_social;
            if (giroInput && data.giro) giroInput.value = data.giro;
            if (direccionInput && data.direccion) direccionInput.value = data.direccion;
            if (data.comuna || data.region) {
                if (profile.locationMode === 'inline_city') {
                    applyInlineCityLocation(profile, data.region, data.comuna);
                } else {
                    applyChileGeoLocation(profile, data.region, data.comuna, '');
                }
            }
        }

        function applyResult(data, opts) {
            opts = opts || {};
            if (opts.prefetchOnly) {
                if (!opts.applyNow) {
                    logSii('Prefetch listo (esperando blur para mostrar badge)');
                }
                return;
            }
            if (data && data.error) {
                setBadge(badgeEl, 'warn', 'RUT no encontrado en SII');
                return;
            }

            data = normalizeApiPayload(data);
            applyFormattedRutToInput();

            if (data.nombre_privado_sii || (data.rut_valido_sii && !data.razon_social)) {
                if (nombreInput) nombreInput.value = '';
                if (empresaInput) empresaInput.value = '';
                if (giroInput) giroInput.value = '';
                fillFields({
                    direccion: data.direccion,
                    comuna: data.comuna,
                    region: data.region,
                });
                if (isSinInicioActividades(data.estado_sii)) {
                    setBadge(
                        badgeEl,
                        'info',
                        'RUT válido · Sin inicio de actividades',
                        'RUT existe en el SII pero no tiene actividades registradas. Ingresa el nombre manualmente.'
                    );
                    var nombreTarget = nombreInput || empresaInput;
                    if (nombreTarget) {
                        if (!nombreTarget.dataset.siiPlaceholderOriginal) {
                            nombreTarget.dataset.siiPlaceholderOriginal =
                                nombreTarget.getAttribute('placeholder') || '';
                        }
                        nombreTarget.setAttribute('placeholder', 'Escribe el nombre completo');
                        global.setTimeout(function () {
                            nombreTarget.focus();
                        }, 60);
                    }
                } else {
                    setBadge(badgeEl, 'info', 'RUT válido en SII');
                }
                return;
            }

            fillFields(data);
            if (opts.fromLocal) {
                setBadge(badgeEl, 'ok', 'Datos locales');
            } else if (data.razon_social) {
                setBadge(badgeEl, 'ok', 'Verificado en SII');
            } else if (data.rut_valido_sii) {
                setBadge(badgeEl, 'info', 'RUT válido en SII');
            } else {
                logSii('applyResult: sin badge (datos vacíos)', data);
            }
        }

        function doFetch(rutApi, cacheKey, opts) {
            logSii('Consultando RUT:', rutApi, 'applyNow=', !!opts.applyNow);

            if (opts.applyNow) {
                showSpinnerDelayed();
                setBadge(badgeEl, null);
            }

            var url = new URL(cfg.apiUrl, global.location.origin);
            url.searchParams.set('rut', rutApi);
            logSii('URL fetch:', url.toString());

            try {
                if (state.inflight) {
                    state.inflight.abort();
                }
            } catch (_) {
                /* ignore */
            }
            state.inflight = new AbortController();
            state.inflightKey = cacheKey;

            fetch(url.toString(), {
                headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
                credentials: 'same-origin',
                signal: state.inflight.signal,
            })
                .then(function (resp) {
                    logSii('HTTP status:', resp.status, resp.statusText);
                    return resp.json().catch(function (parseErr) {
                        logSiiError('JSON inválido:', parseErr);
                        return {};
                    }).then(function (data) {
                        if (state.inflightKey !== cacheKey) {
                            logSii('Respuesta descartada (RUT cambió)');
                            return;
                        }

                        logSii('Respuesta:', data);

                        state.lastRutApi = rutApi;
                        state.lastCacheKey = cacheKey;

                        if (!resp.ok || data.error) {
                            setSessionCache(cacheKey, { error: data.error || 'RUT no encontrado' }, true);
                            state.pendingData = null;
                            state.pendingError = data.error || 'RUT no encontrado';
                            if (opts.applyNow) {
                                setBadge(badgeEl, 'warn', 'RUT no encontrado en SII');
                            }
                            return;
                        }

                        var payload = normalizeApiPayload(data);
                        setSessionCache(cacheKey, payload, false);
                        state.pendingData = payload;
                        state.pendingError = null;

                        applyResult(payload, opts.applyNow ? {} : { prefetchOnly: true });
                    });
                })
                .catch(function (err) {
                    if (err && err.name === 'AbortError') {
                        logSii('Fetch abortado (nueva consulta)');
                        return;
                    }
                    logSiiError('Error fetch:', err);
                    if (opts.applyNow) {
                        setBadge(badgeEl, 'warn', 'RUT no encontrado en SII');
                    }
                })
                .finally(function () {
                    if (state.inflightKey === cacheKey) {
                        hideSpinnerNow();
                        state.inflight = null;
                    }
                });
        }

        function resolveLookup(opts) {
            opts = opts || {};
            var raw = (rutInput.value || '').trim();
            var parsed = parseRut(raw);
            logSii('RUT parseado:', parsed, 'evento:', opts.applyNow ? 'blur' : 'input');

            var rutApi = parsed.valid ? parsed.api : '';
            var cacheKey = cacheKeyFromRaw(raw);

            if (!raw || rutDigitCount(raw) < MIN_RUT_DIGITS) {
                logSii('Corte: menos de', MIN_RUT_DIGITS, 'caracteres alfanuméricos');
                if (opts.applyNow) {
                    setBadge(badgeEl, null);
                    hideSpinnerNow();
                }
                return;
            }

            if (!rutApi) {
                logSii('Corte: parseRut inválido para valor:', raw);
                if (opts.applyNow) setBadge(badgeEl, null);
                return;
            }

            if (
                opts.applyNow &&
                state.pendingData &&
                state.lastCacheKey === cacheKey
            ) {
                logSii('blur: usando resultado en memoria (prefetch)');
                hideSpinnerNow();
                applyResult(state.pendingData, {});
                return;
            }

            var sessionHit = getSessionCache(cacheKey);
            if (sessionHit && sessionHit.error) {
                logSii('sessionStorage HIT (error previo):', cacheKey, sessionHit.error);
                if (opts.applyNow) {
                    hideSpinnerNow();
                    setBadge(badgeEl, 'warn', 'RUT no encontrado en SII');
                    return;
                }
                logSii('Prefetch: ignorando error en caché, se consultará API');
                sessionHit = null;
            }

            if (sessionHit && !sessionHit.error) {
                sessionHit = normalizeApiPayload(sessionHit);
            }

            if (sessionHit && isSessionCacheSufficient(sessionHit)) {
                logSii('sessionStorage HIT (suficiente):', cacheKey, sessionHit);
                state.pendingData = sessionHit;
                state.pendingError = null;
                state.lastRutApi = rutApi;
                state.lastCacheKey = cacheKey;
                if (opts.applyNow) {
                    hideSpinnerNow();
                    applyResult(sessionHit, { fromLocal: !!sessionHit._local });
                }
                return;
            }

            if (sessionHit) {
                logSii(
                    'sessionStorage tiene entrada incompleta para',
                    cacheKey,
                    '— se consultará API',
                    sessionHit
                );
            }

            if (!opts.applyNow && state.inflight && state.inflightKey === cacheKey) {
                logSii('Prefetch ya en curso para', cacheKey);
                return;
            }

            doFetch(rutApi, cacheKey, opts);
        }

        function onInputDebounced() {
            if (state.debounceTimer) global.clearTimeout(state.debounceTimer);
            state.debounceTimer = global.setTimeout(function () {
                state.debounceTimer = null;
                logSii('input debounce disparado, valor:', rutInput.value);
                if (rutDigitCount(rutInput.value) >= MIN_RUT_DIGITS) {
                    resolveLookup({ applyNow: false });
                }
            }, DEBOUNCE_MS);
        }

        function onBlur() {
            logSii('blur/focusout en #' + bindKey);
            global.setTimeout(function () {
                resolveLookup({ applyNow: true });
            }, 80);
        }

        rutInput.addEventListener('input', onInputDebounced);
        rutInput.addEventListener('blur', onBlur);
        rutInput.addEventListener('focusout', onBlur);

        profileInstances[bindKey] = {
            badgeEl: badgeEl,
            spinnerEl: spinnerEl,
            state: state,
        };

        return rutInput;
    }

    function collectProfiles(cfg) {
        if (cfg.profiles && cfg.profiles.length) {
            return cfg.profiles;
        }
        if (cfg.rutInputId || cfg.nombreFieldId) {
            return [cfg];
        }
        return [
            {
                rutInputId: 'rut',
                nombreFieldId: 'nombre',
                giroFieldId: 'giro',
                direccionFieldId: 'direccion',
                regionFieldId: 'region',
                comunaFieldId: 'comuna',
                ciudadFieldId: 'ciudad',
                locationMode: 'chile_geo',
                badgeId: 'siiRutBadge',
                spinnerId: 'siiRutSpinner',
            },
            {
                rutInputId: 'ic_rut',
                nombreFieldId: 'ic_nombre',
                giroFieldId: 'ic_giro',
                direccionFieldId: 'ic_direccion',
                regionFieldId: 'ic_region',
                comunaFieldId: 'ic_ciudad',
                ciudadFieldId: 'ic_ciudad',
                locationMode: 'inline_city',
                badgeId: 'siiIcRutBadge',
                spinnerId: 'siiIcRutSpinner',
                empresaFieldId: 'ic_empresa',
            },
        ];
    }

    var profileInstances = {};

    function resetRutProfile(rutInputId) {
        var inst = profileInstances[rutInputId];
        if (!inst) return;
        if (inst.state.spinnerTimer) {
            global.clearTimeout(inst.state.spinnerTimer);
            inst.state.spinnerTimer = null;
        }
        if (inst.state.debounceTimer) {
            global.clearTimeout(inst.state.debounceTimer);
            inst.state.debounceTimer = null;
        }
        setSpinner(inst.spinnerEl, false);
        setBadge(inst.badgeEl, null);
        inst.state.lastRutApi = '';
        inst.state.lastCacheKey = '';
        inst.state.pendingData = null;
        inst.state.pendingError = null;
        inst.state.inflight = null;
        inst.state.inflightKey = '';
    }

    function boot() {
        var cfg = readConfig();
        if (!cfg) {
            log('Sin #siiContribuyenteConfig en esta vista');
            return;
        }
        global.siiAutofillConfig = cfg;

        if (!cfg.enabled) {
            log('Deshabilitado (enabled=false). apiUrl:', cfg.apiUrl || '(vacío)');
            return;
        }
        if (!cfg.apiUrl) {
            log('Sin apiUrl');
            return;
        }

        preloadPartiesFromConfig(cfg);

        var profiles = collectProfiles(cfg);
        var bound = 0;
        profiles.forEach(function (profile) {
            if (bindProfile(cfg, profile)) bound += 1;
        });

        log(
            'Init: perfiles=' + profiles.length +
            ', enlazados=' + bound +
            ', sessionStorage keys=' + Object.keys(readSessionStore()).length
        );
    }

    global.SiiContribuyenteAutofill = {
        boot: boot,
        resetRutProfile: resetRutProfile,
        preloadFromConfig: function () {
            var cfg = readConfig();
            if (cfg) preloadPartiesFromConfig(cfg);
        },
        debugSessionCache: function (rut) {
            var store = readSessionStore();
            logSii('sessionStorage completo:', store);
            if (rut) {
                var key = cacheKeyFromRaw(rut);
                logSii('Entrada para', rut, '→', key, ':', store[key]);
                logSii('Suficiente:', isSessionCacheSufficient(getSessionCache(key)));
            }
        },
        clearSessionCache: function (rut) {
            var store = readSessionStore();
            if (rut) {
                delete store[cacheKeyFromRaw(rut)];
            } else {
                store = {};
            }
            writeSessionStore(store);
            logSii('sessionStorage limpiado', rut || '(todo)');
        },
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
    document.addEventListener('app:module-loaded', boot);
})(window);
