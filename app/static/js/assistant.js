/**
 * Andes Auto Parts — Assistant UI controller (fase 1).
 * No llama al ERP ni a un LLM. Toda la inteligencia pasa por AndesAssistantService.
 */
(function () {
    'use strict';

    var root = document.getElementById('ap-assistant-root');
    if (!root) return;

    var service = window.AndesAssistantService;
    if (!service) return;

    var fab = document.getElementById('ap-assistant-fab');
    var drawer = document.getElementById('ap-assistant-drawer');
    var backdrop = document.getElementById('ap-assistant-backdrop');
    var closeBtn = document.getElementById('ap-assistant-close');
    var expandBtn = document.getElementById('ap-assistant-expand');
    var historyBtn = document.getElementById('ap-assistant-history-btn');
    var newBtn = document.getElementById('ap-assistant-new');
    var form = document.getElementById('ap-assistant-form');
    var input = document.getElementById('ap-assistant-input');
    var plusBtn = document.getElementById('ap-assistant-plus');
    var micBtn = document.getElementById('ap-assistant-mic');
    var sendBtn = document.getElementById('ap-assistant-send');
    var thread = document.getElementById('ap-assistant-thread');
    var loadingEl = document.getElementById('ap-assistant-loading');
    var errorEl = document.getElementById('ap-assistant-error');
    var hintEl = document.getElementById('ap-assistant-hint');
    var historyEl = document.getElementById('ap-assistant-history');
    var homeEl = document.getElementById('ap-assistant-home');
    var conversationEl = document.getElementById('ap-assistant-conversation');
    var chatEl = document.getElementById('ap-assistant-chat');
    var bodyEl = root.querySelector('.ap-assistant-body');

    var state = {
        panelOpen: false,
        agentEnabled: false,
        expanded: false,
        historyOpen: false,
        chatting: false,
        loading: false
    };

    function dispatchPanel(open) {
        try {
            window.dispatchEvent(new CustomEvent('andes-assistant-panel', { detail: { open: !!open } }));
        } catch (err) { /* ignore */ }
    }

    function persist() {
        if (typeof service.saveSettings === 'function') {
            service.saveSettings({
                panelOpen: state.panelOpen,
                agentEnabled: state.agentEnabled,
                expanded: state.expanded
            });
        }
    }

    function setHint(message) {
        if (!hintEl) return;
        if (!message) {
            hintEl.hidden = true;
            hintEl.textContent = '';
            return;
        }
        hintEl.hidden = false;
        hintEl.textContent = message;
    }

    function setError(message) {
        if (!errorEl) return;
        if (!message) {
            errorEl.hidden = true;
            errorEl.textContent = '';
            return;
        }
        errorEl.hidden = false;
        errorEl.textContent = message;
    }

    function setLoading(on) {
        state.loading = !!on;
        if (loadingEl) loadingEl.hidden = !on;
        if (sendBtn) sendBtn.disabled = !state.agentEnabled || !!on;
        if (input) input.disabled = !state.agentEnabled || !!on;
        if (on) scrollThread();
    }

    function scrollThread() {
        if (!bodyEl) return;
        bodyEl.scrollTop = bodyEl.scrollHeight;
    }

    function appendMessage(role, text) {
        if (!thread) return;
        var bubble = document.createElement('div');
        bubble.className = 'ap-assistant-msg ap-assistant-msg--' + (role === 'user' ? 'user' : 'agent');
        bubble.textContent = text;
        thread.appendChild(bubble);
        scrollThread();
    }

    function applyView() {
        var history = !!state.historyOpen;
        if (historyEl) historyEl.hidden = !history;
        if (conversationEl) conversationEl.hidden = history;
        if (homeEl) homeEl.hidden = history || state.chatting;
        if (chatEl) chatEl.hidden = history || !state.chatting;
        if (drawer) drawer.classList.toggle('is-chatting', !history && state.chatting);
        if (historyBtn) {
            historyBtn.classList.toggle('is-active', history);
            historyBtn.setAttribute('aria-pressed', history ? 'true' : 'false');
        }
        if (!history && state.chatting) {
            window.setTimeout(scrollThread, 40);
        }
    }

    function enterChat() {
        state.chatting = true;
        state.historyOpen = false;
        applyView();
    }

    function applyAgentEnabled() {
        var on = !!state.agentEnabled;
        if (form) form.classList.toggle('is-disabled', !on);

        var disable = !on || state.loading;
        if (input) {
            input.disabled = disable;
            input.placeholder = on ? 'Pregúntame...' : 'Activa el agente en el menú izquierdo';
        }
        if (plusBtn) plusBtn.disabled = disable;
        if (micBtn) micBtn.disabled = disable;
        if (sendBtn) sendBtn.disabled = disable;

        root.querySelectorAll('[data-assistant-action]').forEach(function (btn) {
            btn.disabled = disable;
        });

        var sidebarBtn = document.getElementById('sidebarAgentBtn');
        var sidebarLbl = document.getElementById('sidebarAgentLabel');
        if (sidebarBtn) {
            sidebarBtn.classList.toggle('is-on', on);
            sidebarBtn.setAttribute('aria-checked', on ? 'true' : 'false');
            sidebarBtn.title = on ? 'Desactivar el agente IA' : 'Activar el agente IA';
        }
        if (sidebarLbl) sidebarLbl.textContent = on ? 'Agente IA' : 'Agente inactivo';

        if (!on) setHint('Activa el agente en el menú izquierdo para preguntar.');
        else setHint('');
        document.documentElement.classList.toggle('andes-assistant-off', !on);
        if (!on && state.panelOpen) {
            setPanelOpen(false);
        }
    }

    function applyExpanded() {
        if (!drawer) return;
        drawer.classList.toggle('is-expanded', !!state.expanded);
        if (expandBtn) {
            expandBtn.classList.toggle('is-active', !!state.expanded);
            expandBtn.setAttribute('aria-pressed', state.expanded ? 'true' : 'false');
            expandBtn.setAttribute('aria-label', state.expanded ? 'Restablecer tamaño del panel' : 'Expandir panel');
            expandBtn.title = state.expanded ? 'Restablecer tamaño' : 'Expandir panel';
        }
    }

    function applyHistory() {
        applyView();
    }

    function setPanelOpen(open, options) {
        options = options || {};
        state.panelOpen = !!open;
        if (!open) state.historyOpen = false;
        if (drawer) {
            drawer.classList.toggle('is-open', state.panelOpen);
            drawer.setAttribute('aria-hidden', state.panelOpen ? 'false' : 'true');
        }
        if (backdrop) {
            backdrop.hidden = !state.panelOpen;
            backdrop.classList.toggle('is-open', state.panelOpen);
        }
        if (fab) {
            fab.classList.toggle('is-open', state.panelOpen);
            fab.setAttribute('aria-expanded', state.panelOpen ? 'true' : 'false');
            fab.setAttribute('aria-label', state.panelOpen ? 'Asistente abierto' : 'Abrir asistente de Andes Auto Parts');
        }
        document.documentElement.classList.toggle('andes-assistant-open', state.panelOpen);
        applyView();
        if (!options.skipPersist) persist();
        if (state.panelOpen) {
            dispatchPanel(true);
            if (state.agentEnabled && input && !options.skipFocus) {
                window.setTimeout(function () { input.focus(); }, 180);
            }
        }
    }

    function togglePanel() {
        setPanelOpen(!state.panelOpen);
    }

    function setAgentEnabled(on) {
        state.agentEnabled = !!on;
        applyAgentEnabled();
        persist();
    }

    function clearConversation() {
        if (thread) thread.innerHTML = '';
        setError('');
        setLoading(false);
        state.historyOpen = false;
        state.chatting = false;
        if (service) service.conversationId = 'conv-' + Date.now();
        applyView();
        setHint(state.agentEnabled ? '' : 'Activa el agente en el menú izquierdo para preguntar.');
    }

    function handleServiceResult(result) {
        setLoading(false);
        if (!result || !result.ok) {
            var err = (result && result.error) ? result.error : 'No se pudo completar la solicitud.';
            setError(err);
            return;
        }
        setError('');
        appendMessage('agent', result.reply || 'No se obtuvo respuesta del catálogo.');
    }

    function sendPrompt(text, meta) {
        if (state.loading) return;
        setError('');
        if (!state.agentEnabled) {
            setHint('Activa el agente en el menú izquierdo para preguntar.');
            return;
        }
        var value = String(text || '').trim();
        if (!value) return;
        enterChat();
        appendMessage('user', value);
        if (input) input.value = '';
        setHint('');
        setLoading(true);
        var context = { agentEnabled: true, source: (meta && meta.source) || 'composer' };
        var request = (meta && meta.actionId)
            ? service.runQuickAction(meta.actionId, context)
            : service.sendMessage(value, context);
        request.then(function (result) {
            handleServiceResult(result);
        }).catch(function () {
            setLoading(false);
            setError('Ocurrió un problema al contactar el servicio del asistente.');
        });
    }

    if (fab) {
        fab.addEventListener('click', function () {
            togglePanel();
        });
    }
    if (closeBtn) closeBtn.addEventListener('click', function () { setPanelOpen(false); });
    if (backdrop) backdrop.addEventListener('click', function () { setPanelOpen(false); });
    if (expandBtn) {
        expandBtn.addEventListener('click', function () {
            state.expanded = !state.expanded;
            applyExpanded();
            persist();
        });
    }
    if (historyBtn) {
        historyBtn.addEventListener('click', function () {
            state.historyOpen = !state.historyOpen;
            applyHistory();
        });
    }
    if (newBtn) {
        newBtn.addEventListener('click', function () {
            clearConversation();
        });
    }
    window.addEventListener('andes-agent-enabled', function (event) {
        var on = !!(event.detail && event.detail.enabled);
        if (on === state.agentEnabled) return;
        setAgentEnabled(on);
    });
    if (form) {
        form.addEventListener('submit', function (event) {
            event.preventDefault();
            sendPrompt(input ? input.value : '');
        });
    }
    if (plusBtn) {
        plusBtn.addEventListener('click', function () {
            setHint('Los adjuntos se conectarán en una fase posterior.');
        });
    }
    if (micBtn) {
        micBtn.addEventListener('click', function () {
            setHint('El micrófono se conectará en la siguiente fase.');
        });
    }
    root.querySelectorAll('[data-assistant-action]').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var label = btn.getAttribute('data-assistant-label') || btn.textContent.trim();
            sendPrompt(label, { source: 'quick_action', actionId: btn.getAttribute('data-assistant-action') });
        });
    });

    document.addEventListener('keydown', function (event) {
        if (event.key !== 'Escape' || !state.panelOpen) return;
        if (state.historyOpen) {
            state.historyOpen = false;
            applyView();
            return;
        }
        setPanelOpen(false);
    });

    window.addEventListener('andes-chat-panel', function (event) {
        if (event.detail && event.detail.open && state.panelOpen) {
            setPanelOpen(false);
        }
    });

    service.getSettings().then(function (settings) {
        settings = settings || {};
        state.agentEnabled = !!settings.agentEnabled;
        state.expanded = !!settings.expanded;
        applyAgentEnabled();
        applyExpanded();
        if (settings.panelOpen && state.agentEnabled) setPanelOpen(true, { skipFocus: true });
    }).catch(function () {
        applyAgentEnabled();
        applyExpanded();
    });
})();
