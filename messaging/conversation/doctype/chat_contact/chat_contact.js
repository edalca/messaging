frappe.ui.form.on("Chat Contact", {
    refresh(frm) {
        frm.disable_save();
        frm.set_read_only();
        if (!frm.is_new()) {
            frm.page.set_title_sub([frm.doc.channel, frm.doc.account].filter(Boolean).join(" · "));
        }
        const wrapper = frm.fields_dict.profile_values.$wrapper.empty();
        const fields = frm.doc.__onload?.profile_fields;
        if (!fields) return;
        if (!fields.length) {
            $("<p class='text-muted'>")
                .text(__("No profile values have been registered."))
                .appendTo(wrapper);
            return;
        }
        for (const [index, field] of fields.entries()) {
            const control = frappe.ui.form.make_control({
                parent: wrapper,
                df: {
                    fieldname: `profile_value_${index}`,
                    fieldtype: field.fieldtype,
                    label: frappe.utils.escape_html(field.label),
                    read_only: 1,
                    description: field.historical
                        ? frappe.utils.escape_html(__("No current parameter definition is available."))
                        : ""
                }
            });
            // Read-only controls format the stored value without parsing or model writes.
            control.value = field.value;
            control.refresh();
            if (["Data", "Small Text"].includes(field.fieldtype)) {
                $(control.disp_area).css("white-space", "pre-wrap");
            }
        }
    }
});
