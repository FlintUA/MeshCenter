// UI1: the one app-wide dialog component. Replaces the browser's native confirm() /
// alert() / prompt(), which look foreign, cannot be themed or translated consistently,
// block the page and cannot be operated by automated checks.
//
//   const ok = await appConfirm({ title, message, confirmLabel, cancelLabel, danger });
//   const values = await appPrompt({ title, message, fields: [{ name, label, value,
//                                    inputmode, validate(value, all) -> error text | '' }] });
//   await appAlert({ title, message });
//
// confirm -> true / false, prompt -> { name: value } / null, alert -> resolves on close.
//
// Behaviour: the dialog is attached to <body> (a re-render of the page behind it never
// removes it); Esc and a click on the backdrop cancel; Enter confirms unless a button has
// focus (then that button's own click runs); Tab is trapped inside; focus returns to
// whatever had it before. A danger dialog starts with Cancel focused so a stray Enter
// can never trigger the destructive action. Texts are always set with textContent - they
// contain node names / SSIDs. A second call while one is open is queued.
//
// MCDialog.resolveOpen(accept, values) is a frontend-only test/agent hook: it presses
// the open dialog's Confirm (accept = true) or Cancel button exactly as a click would,
// so every check in a prompt dialog still runs - nothing server-side is bypassed.
(function (root) {
    'use strict';

    const queue = [];
    let active = null;

    function tr(key, fallback) {
        try {
            const value = root.I18N && root.I18N.t ? root.I18N.t(key) : '';
            if (value && value !== key) return value;
        } catch (error) { /* fall through */ }
        return fallback;
    }

    function make(tag, className, text) {
        const node = root.document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function open(spec) {
        return new Promise((resolve) => {
            queue.push({ spec, resolve });
            if (!active) showNext();
        });
    }

    function showNext() {
        const next = queue.shift();
        if (!next) return;
        const { spec, resolve } = next;
        const doc = root.document;
        const kind = spec.kind || 'confirm';
        const fields = kind === 'prompt' ? (spec.fields || []) : [];
        const uid = 'mcDialog' + Date.now().toString(36) + Math.floor(Math.random() * 1e4);

        const overlay = make('div', 'modal-overlay mc-dialog-overlay');
        const box = make('div', 'modal-content mc-dialog' + (spec.danger ? ' is-danger' : ''));
        box.setAttribute('role', kind === 'alert' ? 'alertdialog' : 'dialog');
        box.setAttribute('aria-modal', 'true');

        if (spec.title) {
            const title = make('h3', 'mc-dialog-title', spec.title);
            title.id = uid + 'Title';
            box.setAttribute('aria-labelledby', title.id);
            box.appendChild(title);
        }
        if (spec.message) {
            const message = make('p', 'mc-dialog-message', spec.message);
            message.id = uid + 'Msg';
            box.setAttribute('aria-describedby', message.id);
            box.appendChild(message);
        }

        const inputs = [];
        fields.forEach((field, index) => {
            const row = make('div', 'mc-dialog-field');
            const label = make('label', 'mc-dialog-label', field.label || '');
            const input = make('input', 'mc-dialog-input');
            input.id = uid + 'F' + index;
            label.htmlFor = input.id;
            input.type = field.type || 'text';
            input.value = field.value === undefined || field.value === null ? '' : String(field.value);
            if (field.inputmode) input.setAttribute('inputmode', field.inputmode);
            input.setAttribute('autocomplete', 'off');
            input.setAttribute('autocapitalize', 'off');
            input.setAttribute('spellcheck', 'false');
            const error = make('div', 'mc-dialog-error');
            error.id = input.id + 'Err';
            error.hidden = true;
            error.setAttribute('role', 'alert');
            input.addEventListener('input', () => { error.hidden = true; input.removeAttribute('aria-invalid'); });
            row.appendChild(label);
            row.appendChild(input);
            row.appendChild(error);
            box.appendChild(row);
            inputs.push({ field, input, error });
        });

        const actions = make('div', 'mc-dialog-actions');
        const cancelBtn = make('button', 'device-action-btn device-action-secondary', spec.cancelLabel || tr('dialog.cancel', 'Cancel'));
        cancelBtn.type = 'button';
        cancelBtn.setAttribute('data-dialog-action', 'cancel');
        const confirmBtn = make('button', 'device-action-btn device-action-primary is-primary' + (spec.danger ? ' is-danger' : ''),
            spec.confirmLabel || (kind === 'alert' ? tr('dialog.ok', 'OK') : tr('dialog.confirm', 'Confirm')));
        confirmBtn.type = 'button';
        confirmBtn.setAttribute('data-dialog-action', 'confirm');
        if (kind !== 'alert') actions.appendChild(cancelBtn);
        actions.appendChild(confirmBtn);
        box.appendChild(actions);
        overlay.appendChild(box);

        const previousFocus = doc.activeElement || null;
        let settled = false;

        function finish(result) {
            if (settled) return;
            settled = true;
            doc.removeEventListener('keydown', onKeydown, true);
            if (overlay.remove) overlay.remove();
            else if (overlay.parentNode) overlay.parentNode.removeChild(overlay);
            active = null;
            try { if (previousFocus && previousFocus.focus) previousFocus.focus(); } catch (error) { /* gone */ }
            resolve(result);
            showNext();
        }

        function collect() {
            const values = {};
            inputs.forEach(({ field, input }) => { values[field.name] = input.value; });
            return values;
        }

        function accept() {
            if (kind === 'prompt') {
                const values = collect();
                let firstBad = null;
                inputs.forEach(({ field, input, error }) => {
                    let message = '';
                    if (typeof field.validate === 'function') message = field.validate(values[field.name], values) || '';
                    if (message) {
                        error.textContent = message;
                        error.hidden = false;
                        input.setAttribute('aria-invalid', 'true');
                        input.setAttribute('aria-describedby', error.id);
                        if (!firstBad) firstBad = input;
                    }
                });
                if (firstBad) {
                    if (firstBad.focus) firstBad.focus();
                    return;
                }
                finish(values);
                return;
            }
            finish(kind === 'alert' ? undefined : true);
        }

        function cancel() {
            finish(kind === 'prompt' ? null : (kind === 'alert' ? undefined : false));
        }

        function focusables() {
            const list = inputs.map((entry) => entry.input);
            if (kind !== 'alert') list.push(cancelBtn);
            list.push(confirmBtn);
            return list;
        }

        function onKeydown(event) {
            if (event.key === 'Escape') {
                event.stopPropagation();
                if (event.preventDefault) event.preventDefault();
                cancel();
                return;
            }
            if (event.key === 'Enter') {
                const tag = event.target && event.target.tagName ? String(event.target.tagName).toUpperCase() : '';
                if (tag === 'BUTTON') return; // the focused button's own click runs
                event.stopPropagation();
                if (event.preventDefault) event.preventDefault();
                accept();
                return;
            }
            if (event.key === 'Tab') {
                const list = focusables();
                const index = list.indexOf(doc.activeElement);
                const last = list.length - 1;
                if (event.shiftKey && index <= 0) {
                    if (event.preventDefault) event.preventDefault();
                    list[last].focus();
                } else if (!event.shiftKey && (index === -1 || index === last)) {
                    if (event.preventDefault) event.preventDefault();
                    list[0].focus();
                }
            }
        }

        cancelBtn.addEventListener('click', cancel);
        confirmBtn.addEventListener('click', accept);
        overlay.addEventListener('click', (event) => { if (event.target === overlay) cancel(); });
        doc.addEventListener('keydown', onKeydown, true);

        active = { accept, cancel, collect, inputs, confirmBtn, cancelBtn, overlay, kind };
        doc.body.appendChild(overlay);

        if (inputs.length) {
            inputs[0].input.focus();
            if (inputs[0].input.select) inputs[0].input.select();
        } else if (spec.danger && kind !== 'alert') {
            cancelBtn.focus();
        } else {
            confirmBtn.focus();
        }
    }

    function str(value) {
        return value === undefined || value === null ? '' : String(value);
    }

    const api = {
        confirm: (options) => {
            const o = options || {};
            return open({ kind: 'confirm', title: str(o.title), message: str(o.message), confirmLabel: o.confirmLabel, cancelLabel: o.cancelLabel, danger: !!o.danger });
        },
        prompt: (options) => {
            const o = options || {};
            return open({ kind: 'prompt', title: str(o.title), message: str(o.message), fields: o.fields || [], confirmLabel: o.confirmLabel, cancelLabel: o.cancelLabel, danger: !!o.danger });
        },
        alert: (options) => {
            const o = options || {};
            return open({ kind: 'alert', title: str(o.title), message: str(o.message), confirmLabel: o.confirmLabel });
        },
        isOpen: () => {
            return !!active;
        },
        // Frontend-only hook for tests / agent checks - presses the real Confirm / Cancel.
        resolveOpen: (accept, values) => {
            if (!active) return false;
            if (accept && values && active.inputs) {
                active.inputs.forEach(({ field, input }) => {
                    if (Object.prototype.hasOwnProperty.call(values, field.name)) input.value = String(values[field.name]);
                });
            }
            (accept ? active.confirmBtn : active.cancelBtn).click();
            return true;
        },
    };

    root.MCDialog = api;
    root.appConfirm = api.confirm;
    root.appPrompt = api.prompt;
    root.appAlert = api.alert;
})(typeof window !== 'undefined' ? window : globalThis);
