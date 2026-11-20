/* Shared Desk actions for persisted Process approvals. */
frappe.provide('messaging.process_approval');
(() => {
    const api = messaging.process_approval;
    const actions = {
        Untested: {label: () => __('Validate'), method: 'validate_process'},
        Disabled: {label: () => __('Enable'), method: 'set_active', active: 1},
        Enabled: {label: () => __('Disable', null, 'Process consumer'), method: 'set_active', active: 0},
        Unavailable: {label: () => __('Validate'), method: 'validate_process'},
    };

    async function request(method, args) {
        const response = await $.ajax({
            url: '/api/method/' + method,
            type: 'POST',
            data: {...args, candidate: JSON.stringify(args.candidate)},
            headers: {'X-Frappe-CSRF-Token': frappe.csrf_token},
            dataType: 'json',
        });
        if (!response?.message || response.exc) throw response;
        return response.message;
    }

    function message(text) {
        frappe.msgprint({indicator: 'orange', message: frappe.utils.escape_html(text)});
    }

    function errorText(error) {
        const body = error?.responseJSON || error;
        if (['ValidationError', 'PermissionError', 'TimestampMismatchError'].includes(body?.exc_type)) {
            try {
                const messages = JSON.parse(body._server_messages || '[]');
                if (messages.length) return JSON.parse(messages.at(-1)).message;
            } catch (_) { /* Use the generic administrative message. */ }
        }
        return __('The action could not be completed. Reload the document and try again.');
    }

    api.refresh = (frm, owner) => {
        const {doc} = frm;
        const action = actions[doc.status];
        const previous = frm.process_approval_action;
        if (previous) frm.remove_custom_button(previous.label);
        frm.process_approval_action = null;
        frm.$wrapper.off('dirty.process_approval');
        if (!action || frm.is_new()) return;
        frm.page.set_indicator(__(doc.status),
            doc.status === 'Enabled' ? 'green' : doc.status === 'Disabled' ? 'gray' : 'orange');
        if (!frm.perm?.[0]?.write || !frappe.user.has_role('System Manager')) return;

        const label = action.label();
        const route = frappe.get_route_str();
        const current = () => frm.doc === doc && frappe.get_route_str() === route;
        const button = frm.add_custom_button(label, async () => {
            if (!current() || frm.is_dirty() || frm.process_approval_pending) return;
            frm.process_approval_pending = true;
            button.prop('disabled', true);
            const modified = doc.modified;
            try {
                const args = {candidate: {name: doc.name, modified}};
                if (action.active !== undefined) args.active = action.active;
                const result = await request(owner + action.method, args);
                if (!current()) return;
                if (frm.is_dirty() || doc.modified !== modified) {
                    message(__('The document changed during the operation. Reload it to check its current state.'));
                    return;
                }
                if (result.status === 'Failed') {
                    message(result.execution_log
                        ? __('The Process could not be validated. Review Process Execution Log {0} for the technical diagnosis.', [result.execution_log])
                        : result.message || __('Process validation failed.'));
                } else if (result.message) {
                    message(result.message);
                }
                await frm.reload_doc();
            } catch (error) {
                if (current()) message(errorText(error));
            } finally {
                frm.process_approval_pending = false;
                if (current()) button.prop('disabled', frm.is_dirty());
            }
        });
        frm.process_approval_action = {label};
        const update = () => button.prop('disabled', frm.is_dirty() || Boolean(frm.process_approval_pending));
        frm.$wrapper.on('dirty.process_approval', update);
        update();
    };
})();
