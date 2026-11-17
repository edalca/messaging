frappe.ui.form.on('Process Definition', {
 refresh(frm) {
  frm.disable_save();
  frm.set_read_only();
  frm.page.set_indicator(__(frm.doc.status),
   frm.doc.status === 'Unavailable' ? 'orange' : 'green');
 }
});
