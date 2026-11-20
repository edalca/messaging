frappe.ui.form.on('AI Provider', {
    refresh(frm) {
        const internal = !frm.is_new() && frm.doc.provider_type === 'Internal';
        frm.set_df_property('provider_type', 'read_only', 1);
        frm.set_df_property('application', 'read_only', 1);
        frm.set_df_property('schema_version', 'read_only', 1);
        for (const field of ['decision', 'parameter_extraction']) frm.set_df_property(field, 'read_only', internal || !frm.is_new());
        frm.set_df_property('parameters', 'read_only', internal ? 1 : 0);
        if (!internal) {
            if (!frm.is_new() && frm.perm?.[0]?.write && frappe.user.has_role('System Manager')) {
                frm.add_custom_button(__('Manage availability'), () => {
                    if (frm.is_dirty()) return;
                    const doc = frm.doc;
                    frappe.prompt({
                        fieldname: 'status', fieldtype: 'Select', label: __('Status'), reqd: 1,
                        options: ['Enabled', 'Disabled'].join('\n'), default: doc.status
                    }, async ({status}) => {
                        if (frm.doc !== doc || frm.is_dirty()) return;
                        await frappe.call({
                            method: 'messaging.chatbot.doctype.ai_provider.ai_provider.set_provider_enabled',
                            args: {provider: doc.name, enabled: status === 'Enabled'}, type: 'POST', freeze: true
                        });
                        if (frm.doc === doc) await frm.reload_doc();
                    }, __('Manage availability'), __('Apply'));
                });
            }
            return;
        }
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
