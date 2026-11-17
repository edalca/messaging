/* Native localized fields are the authoring contract; JSON is server generated. */
async function evaluation_case_scaffold(frm, populate = false) {
    if (!frm.doc.case_template) return;
    const {message: scenario} = await frappe.call({
        method: 'messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case.template_fields',
        args: {case_template: frm.doc.case_template}
    });
    frm._scenario = scenario;
    const options = {
        flow_texts: scenario.flows, flows: scenario.flows, excluded_flows: scenario.flows,
        expected_parameters: scenario.parameters.map(r => r.ref), expected_values: scenario.parameters.map(r => r.ref)
    };
    if (populate) await frm.set_value({profile_type: scenario.profile_type, group: scenario.group});
    const selectable = scenario.profile_type === 'Decision' ? ['flows', 'excluded_flows'] : ['expected_values'];
    for (const field of selectable) {
        const grid = frm.fields_dict[field].grid;
        grid.refresh();
        grid.update_docfield_property('ref', 'fieldtype', 'Select');
        grid.update_docfield_property('ref', 'options', [''].concat(options[field]).join('\n'));
    }
    if (!populate) return;
    const reconcile = (field, refs, defaults = {}) => {
        const previous = new Map((frm.doc[field] || []).map(row => [row.ref, row]));
        frm.clear_table(field);
        refs.forEach(ref => {
            const old = previous.get(ref) || {};
            const values = field === 'expected_parameters' ? {status: old.status || 'NOT_FOUND'} :
                {label: old.label || '', description: old.description || ''};
            frm.add_child(field, {ref, ...defaults, ...values});
        });
        frm.refresh_field(field);
    };
    reconcile('flow_texts', scenario.flows);
    reconcile('expected_parameters', scenario.parameters.map(r => r.ref));
    for (const field of ['flows', 'excluded_flows', 'expected_values']) {
        frm.doc[field] = (frm.doc[field] || []).filter(row => options[field].includes(row.ref));
        frm.refresh_field(field);
    }
    frm.dirty();
}

frappe.ui.form.on('AI Evaluation Case', {
    async refresh(frm) {
        frm.set_df_property('case_id', 'read_only', !frm.is_new());
        await evaluation_case_scaffold(frm);
        if (!frm.doc.application) return;
        frm.disable_save(); frm.set_read_only();
        if (frm.perm?.[0]) frm.perm[0].delete = 0;
        frm.page.menu?.find(`[data-label="${encodeURIComponent(__('Delete'))}"]`).closest('li').remove();
        frm.dashboard.set_headline_alert(__('Registered cases are managed by their owning application.'));
    },
    case_template(frm) { return evaluation_case_scaffold(frm, true); },
    setup(frm) { frm.set_query('case_template', () => ({})); }
});

frappe.ui.form.on('AI Evaluation Expected Value', {
    ref(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        const parameter = frm._scenario?.parameters.find(p => p.ref === row.ref);
        if (parameter) frappe.model.set_value(cdt, cdn, 'parameter_type', parameter.type);
    }
});
