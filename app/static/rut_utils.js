(function (global) {
    function clean(value) {
        var compact = String(value || '').replace(/[^0-9kK]/g, '');
        if (!compact) return '';
        // Allow partial typing (1 char) without erasing input.
        if (compact.length < 2) return compact.toUpperCase();
        var dv = compact.slice(-1).toUpperCase();
        var body = compact.slice(0, -1).replace(/\D/g, '');
        if (!body) return '';
        return body + dv;
    }

    function format(value) {
        var normalized = clean(value);
        // Keep single-character input visible while typing.
        if (normalized.length < 2) return normalized;
        var body = normalized.slice(0, -1);
        var dv = normalized.slice(-1);
        var chunks = [];
        while (body.length > 3) {
            chunks.unshift(body.slice(-3));
            body = body.slice(0, -3);
        }
        chunks.unshift(body);
        return chunks.join('.') + '-' + dv;
    }

    function computeDv(body) {
        var factors = [2, 3, 4, 5, 6, 7];
        var total = 0;
        var reversed = body.split('').reverse();
        for (var i = 0; i < reversed.length; i += 1) {
            total += Number(reversed[i]) * factors[i % factors.length];
        }
        var remainder = 11 - (total % 11);
        if (remainder === 11) return '0';
        if (remainder === 10) return 'K';
        return String(remainder);
    }

    function isValid(value) {
        var normalized = clean(value);
        if (normalized.length < 8) return false;
        var body = normalized.slice(0, -1);
        var dv = normalized.slice(-1);
        if (!/^\d+$/.test(body)) return false;
        return computeDv(body) === dv;
    }

    function alnumBeforeCaret(text, caret) {
        var count = 0;
        for (var i = 0; i < Math.max(0, caret || 0); i += 1) {
            if (/[0-9kK]/.test(text.charAt(i))) count += 1;
        }
        return count;
    }

    function caretFromAlnumPosition(formatted, alnumPos) {
        if (alnumPos <= 0) return 0;
        var count = 0;
        for (var i = 0; i < formatted.length; i += 1) {
            if (/[0-9K]/.test(formatted.charAt(i))) count += 1;
            if (count >= alnumPos) return i + 1;
        }
        return formatted.length;
    }

    function validateInput(input, options) {
        var required = !!(input.required || (options && options.required));
        var raw = input.value || '';
        var normalized = clean(raw);
        var message = (options && options.message) || 'Ingresa un RUT valido (ej: 78.074.288-7).';

        if (!normalized) {
            input.setCustomValidity(required ? 'El RUT es obligatorio.' : '');
            return;
        }

        if (!isValid(normalized)) {
            input.setCustomValidity(message);
            return;
        }

        input.setCustomValidity('');
    }

    function bindInput(input, options) {
        if (!input || input.dataset.rutBound === '1') return;
        input.dataset.rutBound = '1';
        options = options || {};

        function onInput() {
            var start = input.selectionStart || 0;
            var alnumPos = alnumBeforeCaret(input.value, start);
            var formatted = format(input.value);
            input.value = formatted;
            var nextCaret = caretFromAlnumPosition(formatted, alnumPos);
            try {
                input.setSelectionRange(nextCaret, nextCaret);
            } catch (_) {}
            validateInput(input, options);
        }

        function onBlur() {
            input.value = format(input.value);
            validateInput(input, options);
        }

        input.addEventListener('input', onInput);
        input.addEventListener('blur', onBlur);
        input.addEventListener('change', onBlur);
        input.addEventListener('invalid', function () {
            validateInput(input, options);
        });

        input.value = format(input.value);
        validateInput(input, options);

        if (input.form && !input.form.dataset.rutSubmitBound) {
            input.form.dataset.rutSubmitBound = '1';
            input.form.addEventListener('submit', function (event) {
                var rutInputs = input.form.querySelectorAll('input[data-rut-input], input[name*=rut i], input[id*=rut i]');
                for (var i = 0; i < rutInputs.length; i += 1) {
                    var el = rutInputs[i];
                    var required = !!(el.required || el.dataset.rutRequired === '1');
                    validateInput(el, { required: required });
                    if (!el.checkValidity()) {
                        event.preventDefault();
                        event.stopPropagation();
                        try {
                            el.reportValidity();
                        } catch (_) {}
                        return;
                    }
                    el.value = clean(el.value);
                }
            });
        }
    }

    function autoBindRutInputs(root) {
        var scope = root || document;
        var selector = 'input[data-rut-input], input[name*=rut i], input[id*=rut i]';
        var inputs = scope.querySelectorAll(selector);
        for (var i = 0; i < inputs.length; i += 1) {
            var input = inputs[i];
            if (input.type === 'search' || input.readOnly || input.disabled) continue;
            bindInput(input, {
                required: input.required || input.dataset.rutRequired === '1',
                message: input.dataset.rutMessage || 'Ingresa un RUT valido (ej: 78.074.288-7).'
            });
        }
    }

    global.RutUtils = {
        clean: clean,
        format: format,
        isValid: isValid,
        bindInput: bindInput,
        autoBindRutInputs: autoBindRutInputs
    };
})(window);
