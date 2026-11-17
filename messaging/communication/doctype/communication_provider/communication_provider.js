frappe.ui.form.on('Communication Provider', {
 refresh(frm) {
  frm.disable_save();
  frm.set_read_only();
  if (frm.is_new() || !frappe.user.has_role('System Manager')) return;
  frm.add_custom_button(__(frm.doc.enabled ? 'Disable' : 'Enable'), async () => {
   await frappe.call({method: 'messaging.communication.doctype.communication_provider.communication_provider.set_provider_enabled',
    type: 'POST', args: {provider: frm.doc.name, enabled: frm.doc.enabled ? 0 : 1}, freeze: true});
   await frm.reload_doc();
  });
 }
});
