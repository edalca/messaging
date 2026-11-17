frappe.ui.form.on('Bot Flow Presets', {
    refresh(frm) {
        frm.disable_save();
        frm.set_read_only();
    }
});
