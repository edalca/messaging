/* Shared inline validation of the current form; approval evidence stays on the server. */
frappe.provide('messaging.process_test');
(() => {
    const api = messaging.process_test;
    if (api.installed) return;
    api.installed = true;
    const method = 'messaging.communication.doctype.process_definition.validation.';
    const receiptFields = ['process_test_section', 'process_test_status', 'process_test_fingerprint',
        'process_test_checked_at', 'process_test_checked_by', 'process_test_log', 'process_test_evidence'];
    const labels = {
        Untested: () => __('Not validated'), Validating: () => __('Validating method...'),
        Passed: () => __('Method validated successfully'), Failed: () => __('Method validation failed'),
        Expired: () => __('Validation expired. Validate the method again before saving.')
    };
    function status(frm, state, message = '') {
        const control = frm.process_validation;
        if (!control) return;
        control.state = state;
        control.status.removeClass('text-success text-danger text-muted')
            .addClass(state === 'Passed' ? 'text-success' : ['Failed', 'Expired'].includes(state) ? 'text-danger' : 'text-muted')
            .text(labels[state]() + (message ? ': ' + message : ''));
    }
    function candidate(frm) {
        // Exclude existing persistent receipt fields; never submit an approval token or fingerprint.
        const doc = JSON.parse(JSON.stringify(frm.doc));
        for (const field of receiptFields) delete doc[field];
        return doc;
    }
    async function describe(frm) {
        const {message} = await frappe.call({method: method + 'describe', type: 'POST', args: {candidate: candidate(frm)}});
        return message;
    }
    function sameCondition(item, row) {
        const value = text => { try { return JSON.parse(text); } catch { return text || ''; } };
        return item.document_event === row.document_event && (item.field_name || '') === (row.field_name || '') &&
            value(item.value) === value(row.value);
    }
    function showState(frm, schema) {
        if (frm.doctype === 'Notification Rule') {
            const rows = schema.conditions || [];
            for (const row of frm.doc.events || []) {
                const state = rows.find(item => sameCondition(item, row));
                row.validation_status = state?.status === 'Passed' ? __('Validated') : __('Not validated');
            }
            frm.refresh_field('events');
        }
        if (!frm.process_validation) return;
        clearTimeout(frm.process_validation.expiry);
        clearTimeout(frm.process_validation.conditionExpiry);
        status(frm, schema.status);
        const expirations = [schema.expires_at, ...(schema.conditions || []).map(row => row.expires_at), ...(schema.outputs || []).map(row => row.expires_at)].filter(Boolean);
        if (expirations.length) {
            const expires = Math.min(...expirations);
            frm.process_validation.expiry = setTimeout(() => {
                status(frm, 'Expired');
                if (frm.doctype === 'Notification Rule') changed(frm);
            }, Math.max(0, expires * 1000 - Date.now()) + 20);
        }
    }
    function errorBody(error) {
        const candidate = Array.isArray(error) ? error[0] : error;
        if (!candidate || typeof candidate !== 'object') return null;
        if (candidate.responseJSON && typeof candidate.responseJSON === 'object') return candidate.responseJSON;
        if (typeof candidate.responseText === 'string' && candidate.responseText.trim().startsWith('{')) {
            try {
                const parsed = JSON.parse(candidate.responseText);
                if (parsed && typeof parsed === 'object') return parsed;
            } catch (_) { /* The body is not the administrative JSON payload. */ }
        }
        return candidate.exc_type || candidate._server_messages || candidate.exception ? candidate : null;
    }
    function plainText(value) {
        return String(value || '').replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
    }
    function errorText(error) {
        const response = errorBody(error);
        const administrative = ['ValidationError', 'PermissionError', 'TimestampMismatchError',
            'MandatoryError', 'LinkValidationError', 'ProcessUnavailableError'].includes(response?.exc_type);
        if (administrative) {
            try {
                const messages = JSON.parse(response._server_messages || '[]');
                if (messages.length) {
                    const text = plainText(JSON.parse(messages[messages.length - 1]).message);
                    if (text) return text;
                }
            } catch (_) { /* Use the administrative exception text without exposing traceback details. */ }
            if (typeof response.exception === 'string' && !response.exception.includes('Traceback') && !response.exception.includes('File "')) {
                const text = plainText(response.exception.replace(/^[^:]+:\s*/, ''));
                if (text) return text;
            }
        }
        return __('Check the configuration, authorization, inputs and output contract, then validate the method again.');
    }
    api.error_text = errorText;
    async function validate(frm) {
        let schema;
        try {
            schema = await describe(frm);
            if (schema.configuration_error) {
                status(frm, 'Failed', schema.configuration_error);
                return;
            }
        } catch (error) {
            status(frm, 'Failed', errorText(error));
            return;
        }
        frm.process_validation.button.prop('disabled', true);
        const revision = frm.process_validation.revision;
        status(frm, 'Validating');
        try {
            const {message: result} = await frappe.call({method: method + 'test_method', type: 'POST',
                args: {candidate: candidate(frm)}, freeze: true});
            if (revision !== frm.process_validation.revision) {
                status(frm, 'Untested', __('The configuration changed. Validate the method again.'));
                return;
            }
            showState(frm, await describe(frm));
            if (result.status === 'Failed') status(frm, 'Failed', result.message);
            else {
                frm.process_validation.wrapper.find('.process-validation-result').remove();
                $('<div class="process-validation-result small mt-2">').text(result.message)
                    .appendTo(frm.process_validation.wrapper);
            }
        } catch (error) {
            status(frm, 'Failed', errorText(error));
        } finally {
            frm.process_validation.button.prop('disabled', false);
        }
    }
    api.validate_condition = async frm => validate(frm);
    api.refresh = async function (frm) {
        if (frm.doctype !== 'Notification Rule') return;
        for (const field of receiptFields) if (frm.fields_dict[field]) frm.toggle_display(field, false);
        if (!frappe.user.has_role('System Manager')) return;
        const field = frm.fields_dict.process_method;
        if (!field) return;
        if (frm.process_validation) {
            clearTimeout(frm.process_validation.expiry);
            clearTimeout(frm.process_validation.pending);
            frm.process_validation.wrapper.remove();
        }
        const wrapper = $('<div class="process-validation mt-2">').appendTo(field.$wrapper);
        const button = $('<button type="button" class="btn btn-default btn-sm">').text(__('Validate method')).appendTo(wrapper);
        const state = $('<div role="status" aria-live="polite" class="small mt-2" style="overflow-wrap:anywhere;white-space:pre-wrap">').appendTo(wrapper);
        frm.process_validation = {wrapper, button, status: state, revision: 0};
        button.on('click', () => validate(frm));
        frm.$wrapper.off('dirty.process_validation').on('dirty.process_validation', () => changed(frm));
        status(frm, 'Untested');
        if (!frm.is_new()) {
            const control = frm.process_validation;
            const revision = control.revision;
            try {
                const schema = await describe(frm);
                if (frm.process_validation === control && revision === control.revision) showState(frm, schema);
            }
            catch (error) { status(frm, 'Failed', errorText(error)); }
        }
    };
    function changed(frm) {
        if (!frm.process_validation) return;
        const control = frm.process_validation;
        control.revision++;
        clearTimeout(control.expiry);
        clearTimeout(control.pending);
        control.wrapper.find('.process-validation-result').remove();
        status(frm, 'Untested');
        control.pending = setTimeout(async () => {
            const revision = control.revision;
            try {
                const schema = await describe(frm);
                if (frm.process_validation === control && revision === control.revision) showState(frm, schema);
            } catch (error) { if (revision === control.revision) status(frm, 'Failed', errorText(error)); }
        }, 350);
    }
})();
