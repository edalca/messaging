frappe.ui.form.on('Communication Provider', {
 refresh(frm) {
  frm.disable_save();
  frm.set_read_only();
  if (frm.is_new() || !frappe.user.has_role('System Manager')) return;
  const enabled = frm.doc.status === 'Enabled';
  frm.page.set_secondary_action(enabled ? __('Disable') : __('Enable'), async () => {
   await frappe.call({method: 'messaging.communication.doctype.communication_provider.communication_provider.set_provider_enabled',
    type: 'POST', args: {provider: frm.doc.name, enabled: enabled ? 0 : 1}, freeze: true});
   await frm.reload_doc();
  });
 }
});
