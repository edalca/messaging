frappe.ui.form.on("Emulator Contact", {
    async refresh(frm) {
        if (frm.is_new() && !frm.doc.sender_id) {
            const { message } = await frappe.call({ method: "messaging.emulator.doctype.emulator_contact.emulator_contact.new_number", type: "POST" });
            if (frm.is_new() && !frm.doc.sender_id) await frm.set_value("sender_id", message);
        }
    }
});
