frappe.ui.form.on('AI Configuration', {
    setup(frm) {
        frm._parameters_loaded = frappe.require('/assets/messaging/js/provider_parameters.js');
        frm.set_query('provider', () => ({filters: {enabled: 1, [frm.doc.profile_type === 'Parameter Extraction' ? 'parameter_extraction' : 'decision']: 1}}));
    },
    async refresh(frm) {
        frm.set_df_property('provider', 'label', __('AI Provider'));
        frm.set_df_property('provider', 'read_only', !frm.is_new());
        if (!frm.is_new() && frm.doc.profile_type === 'Decision') {
            const button = frm.add_custom_button(__('Test connection'), async () => {
                if (frm._ai_connection_pending) return;
                if (frm.is_dirty()) { frappe.msgprint(__('Save the configuration before testing its connection.')); return; }
                const doc = frm.doc;
                frm._ai_connection_pending = true;
                button.prop('disabled', true);
                try {
                    const {message: result} = await frappe.call({
                        method: 'messaging.chatbot.doctype.ai_configuration.ai_configuration.test_connection',
                        args: {configuration: doc.name}, type: 'POST', freeze: true,
                        freeze_message: __('Checking the AI connection...')
                    });
                    if (!result || result.configuration !== doc.name || typeof result.operational !== 'boolean'
                        || !result.status || !result.diagnostic || !result.last_checked || !result.message
                        || result.operational !== (result.status === 'operational')) throw new Error('Invalid connection result');
                    if (frm.doc === doc) {
                        // Server-owned diagnostics must not mark the document dirty or replace local edits.
                        Object.assign(doc, {validation_status: result.status, last_checked: result.last_checked,
                            diagnostic: result.diagnostic});
                        for (const field of ['validation_status', 'last_checked', 'diagnostic']) frm.refresh_field(field);
                        doc.__onload = {...doc.__onload, ai_operational_status: result.diagnostic};
                        frm.trigger('connection_intro');
                    }
                    frappe.msgprint({title: __('Test connection'), message: frappe.utils.escape_html(result.message),
                        indicator: result.operational ? 'green' : 'red'});
                } catch {
                    frappe.msgprint({title: __('Test connection'), indicator: 'red',
                        message: __('The connection check could not be completed. Try again or consult the server error log.')});
                } finally {
                    frm._ai_connection_pending = false;
                    button.prop('disabled', false);
                    if (frm.doc === doc) frm.custom_buttons[__('Test connection')]?.prop('disabled', false);
                }
            });
            button.prop('disabled', !!frm._ai_connection_pending);
        }
        frm.trigger('connection_intro');
        if (!frm.is_new()) await functional_evaluation(frm);
        await frm._parameters_loaded;
        await messaging.provider_parameters.render(frm);
    },
    profile_type(frm) {
        frm.remove_custom_button(__('Test connection'));
        frm.trigger('connection_intro');
    },
    connection_intro(frm) {
        if (frm.doc.profile_type === 'Parameter Extraction') {
            frm.set_intro(__('Parameter extraction is not available. This profile stores configuration only.'));
            return;
        }
        const status = frm.doc.__onload?.ai_operational_status;
        const notice = __('Saving does not call the AI provider. Connection checks and profile evaluations are explicit actions and may incur usage.');
        frm.set_intro([status, notice].filter(Boolean).map(frappe.utils.escape_html).join('<br>'));
    },
    async provider(frm) {
        await frm._parameters_loaded;
        await messaging.provider_parameters.selection_changed(frm, 'provider');
    },
    validate(frm) { messaging.provider_parameters.flush(frm); }
});


async function functional_evaluation(frm) {
    const base = 'messaging.chatbot.doctype.ai_evaluation_run.runtime.';
    const {message: state} = await frappe.call({method: base + 'approval_status', args: {configuration: frm.doc.name}, type: 'POST'});
    const escape = frappe.utils.escape_html;
    const panel = frm.fields_dict.evaluation_panel.$wrapper;
    const technical = state.technical || {};
    const functional = state.functional || {};
    panel.empty().append($('<p>').text(state.diagnostic));
    if (state.run) {
        panel.append($('<p>').text(__('Technical validation: {0}/{1}.', [technical.valid || 0, technical.executed || 0])));
        panel.append($('<p>').text(__('Functional evaluation: {0}/{1}.', [functional.matched || 0, functional.applicable || 0])));
        panel.append($('<p>').text(__('Functional discrepancies: {0}. Cases outside the operational route: {1}.', [functional.differed || 0, functional.not_applicable || 0])));
        panel.append($('<p>').text(__('Approval policy: {0}.', [state.policy || ''])));
        panel.append($('<a>').attr('href', '/desk/ai-evaluation-run/' + encodeURIComponent(state.run)).text(__('View evaluation')));
    }
    frm.add_custom_button(__('Evaluate profile'), async () => {
        if (frm.is_dirty()) { frappe.msgprint(__('Save the configuration before evaluating it.')); return; }
        const {message: plan} = await frappe.call({method: base + 'preview', args: {configuration: frm.doc.name}});
        const dialog = new frappe.ui.Dialog({title: __('Profile evaluation'), fields: [
            {fieldname: 'details', fieldtype: 'HTML'},
            {fieldname: 'seed', fieldtype: 'Data', label: __('Random seed'), description: __('Leave empty for a new random selection.')},
            {fieldname: 'optional', fieldtype: 'MultiSelectPills', label: __('Optional case templates'),
                get_data: () => plan.optional_templates.map(r => ({label: r.template_name, value: r.name}))}
        ], primary_action_label: __('Start evaluation'), primary_action: async values => {
            const {message: updated} = await frappe.call({method: base + 'preview', args: {configuration: frm.doc.name, optional: JSON.stringify(values.optional || [])}});
            if (updated.insufficient.some(r => r.mandatory !== false)) {
                frappe.msgprint(__('Insufficient evaluation coverage: {0}.', [updated.insufficient.map(r =>
                    escape(`${r.case_template}/${r.language}/${r.group}: ${r.available}/${r.required}`)).join('; ')])); return;
            }
            const {message: run} = await frappe.call({method: base + 'request_evaluation', type: 'POST', freeze: true,
                args: {configuration: frm.doc.name, seed: values.seed || null, optional: JSON.stringify(values.optional || [])}});
            dialog.hide(); frappe.set_route('Form', 'AI Evaluation Run', run.name);
        }});
        dialog.fields_dict.details.$wrapper.text(__('This action may consume the configured provider: {0} samples, at most {1} calls, {2} seconds overall, no automatic retries. Languages: {3}.',
            [plan.selected_count, plan.limits.max_calls, plan.limits.global_seconds, plan.context.languages.join(', ')]));
        if (!plan.executor_available) dialog.fields_dict.details.$wrapper.append($('<p>').text(__('No compatible evaluation executor is available.')));
        dialog.show();
    });
}
