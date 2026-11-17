function evaluation_template_references(frm) {
    if (frm.doc.profile_type !== 'Decision') return;
    const options = [''].concat((frm.doc.available_flows || []).map(r => r.ref).filter(Boolean)).join('\n');
    for (const field of ['previously_recognized_flows']) {
        const grid = frm.fields_dict[field].grid;
        grid.refresh();
        grid.update_docfield_property('ref', 'fieldtype', 'Select');
        grid.update_docfield_property('ref', 'options', options);
    }
}
frappe.ui.form.on('AI Evaluation Case Template', {
    refresh(frm) {
        for (const field of ['template_id', 'profile_type']) frm.set_df_property(field, 'read_only', !frm.is_new());
        evaluation_template_references(frm);
        if (!frm.is_new()) frm.add_custom_button(__('Cases'), () => frappe.set_route('List', 'AI Evaluation Case', {case_template: frm.doc.name}));
        if (frm.doc.origin !== 'Registered') return;
        frm.disable_save(); frm.set_read_only();
        if (frm.perm?.[0]) frm.perm[0].delete = 0;
        frm.page.menu?.find(`[data-label="${encodeURIComponent(__('Delete'))}"]`).closest('li').remove();
        frm.dashboard.set_headline_alert(__('Registered templates are managed by their owning application.'));
    },
    setup(frm) {
        frm.set_query('provider', () => ({filters: {[frm.doc.profile_type === 'Decision' ? 'decision' : 'parameter_extraction']: 1}}));
    },
    profile_type(frm) { frm.refresh_fields(); }
});
frappe.ui.form.on('AI Evaluation Flow', {ref: evaluation_template_references, available_flows_remove: evaluation_template_references});
