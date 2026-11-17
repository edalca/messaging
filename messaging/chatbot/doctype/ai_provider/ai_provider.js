frappe.ui.form.on('AI Provider', {
    refresh(frm) {
        const internal = !frm.is_new() && frm.doc.provider_type === 'Internal';
        frm.set_df_property('provider_type', 'read_only', 1);
        frm.set_df_property('application', 'read_only', 1);
        frm.set_df_property('schema_version', 'read_only', 1);
        for (const field of ['decision', 'parameter_extraction']) frm.set_df_property(field, 'read_only', internal || !frm.is_new());
        frm.set_df_property('parameters', 'read_only', internal ? 1 : 0);
        if (!internal) return;
        // The toolbar is built before this event, so clear Save and the Delete entry here.
        frm.disable_save();
        frm.set_read_only();
        if (frm.perm && frm.perm[0]) frm.perm[0].delete = 0;
        const label = encodeURIComponent(__('Delete'));
        if (frm.page && frm.page.menu) frm.page.menu.find(`[data-label="${label}"]`).closest('li').remove();
        frm.savetrash = () => {
            frappe.msgprint(__('Internal AI providers are registered by their owning application.'));
        };
    }
});
