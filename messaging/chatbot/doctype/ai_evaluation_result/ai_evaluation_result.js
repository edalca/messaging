frappe.ui.form.on('AI Evaluation Result', {
    refresh(frm) {
        frm.disable_save();
        frm.set_intro(__(frm.doc.diagnostic || frm.doc.status));
        frm.add_custom_button(__('View evaluation'), () => frappe.set_route('Form', 'AI Evaluation Run', frm.doc.evaluation_run));
    }
});
