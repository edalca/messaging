frappe.provide("messaging.provider_parameters");
messaging.provider_parameters = {
    async render(frm, placements = {}) {
        const ai = frm.doctype === "AI Configuration";
        const rule = frm.doctype === "Notification Rule";
        const template = frm.doctype === "Communication Template";
        const account = frm.doctype === "Communication Channel Account";
        const selection = template ? frm.doc.channel : rule ? frm.doc.account : account ? frm.doc.channel : frm.doc.provider;
        const serial = frm._parameter_serial = (frm._parameter_serial || 0) + 1;
        const wrapper = frm.fields_dict.parameter_controls.$wrapper.empty();
        frm._parameter_controls = [];
        if (frm.fields_dict.provider_section) frm.toggle_display("provider_section", false);
        if (!selection) return;
        const { message } = await frappe.call({
            method: ai ? "messaging.chatbot.doctype.ai_provider.ai_provider.parameter_schema" : "messaging.communication.doctype.communication_provider_value.communication_provider_value.parameter_schema",
            args: { doctype: frm.doctype, account: rule ? selection : undefined, provider: (template || account || rule) ? undefined : selection, channel: (template || account) ? selection : undefined }
        });
        if (serial !== frm._parameter_serial) return;
        frm._parameter_selection = selection;
        if (rule && !(frm.notification_redirect_required || frm.doc.redirect_recipient)) return;
        if (frm.fields_dict.provider_section) frm.toggle_display("provider_section", message.parameters.length > 0);
        for (const parameter of message.parameters.sort((a, b) => a.sort_order - b.sort_order)) {
            const row = (frm.doc.parameters || []).find(r => r.parameter_key === parameter.parameter_key);
            const formats = template && frm._template_capabilities?.parameter_formats;
            const format = parameter.parameter_key === "parameter_format" && formats?.length;
            if (format) {
                parameter.implicit_default = formats[0][0];
                parameter.read_only = !!(frm.doc.published_revision || frm.doc.__onload?.template_publication_pending);
            }
            const control = frappe.ui.form.make_control({
                parent: placements[parameter.parameter_key] || wrapper, render_input: true, df: {
                    fieldname: parameter.parameter_key, fieldtype: parameter.field_type, label: __(parameter.label),
                    length: parameter.field_type === "Password" ? 2048 : undefined,
                    options: parameter.options, description: frappe.utils.escape_html(__(parameter.description || "")),
                    reqd: !!parameter.required && (rule ? !!(frm.notification_redirect_required || frm.doc.redirect_recipient)
                        : frm.doc.status === "Active" || ((ai || frm.doctype === "Communication Channel") && frm.doc.status === "Enabled")),
                    read_only: !!parameter.read_only || !frm.perm?.[0]?.write,
                    change() {
                        // Native input events may arrive after a save has replaced these controls.
                        if (frm._rendering_parameters || serial !== frm._parameter_serial) return;
                        messaging.provider_parameters.store(frm, parameter, control);
                        if (format) return frm.script_manager?.trigger("provider_parameter_format");
                    }
                }
            });
            if (parameter.field_type === "Password") control.disable_password_checks();
            if (parameter.filters) control.get_query = () => ({ filters: parameter.filters });
            let value = row ? (parameter.field_type === "Password" ? row.secret_value || "" : JSON.parse(row.value || "null")) : parameter.default_value;
            if (format && !value) value = parameter.implicit_default;
            if (parameter.field_type === "Check") value = Number(value || 0);
            frm._rendering_parameters = true;
            await control.set_value(value ?? "");
            frm._rendering_parameters = false;
            frm._parameter_controls.push({ parameter, control, initial: control.get_value() });
        }
    },
    store(frm, parameter, control) {
        if (parameter.read_only) return;
        let row = (frm.doc.parameters || []).find(r => r.parameter_key === parameter.parameter_key);
        const value = control.get_value();
        if (!row && (value === "" || value == null || value === parameter.implicit_default)) return;
        if (!row) row = frm.add_child("parameters", { parameter_key: parameter.parameter_key });
        if (parameter.field_type === "Password") {
            // A blank control preserves the existing encrypted secret; masked values never decrypt.
            if (value) row.secret_value = value;
        } else {
            let scalar = value === "" || value == null ? null : value;
            if (parameter.field_type === "Check") scalar = !!Number(value);
            if (["Int", "Float"].includes(parameter.field_type) && scalar !== null) scalar = Number(value);
            row.value = JSON.stringify(scalar);
        }
        frm.dirty();
    },
    flush(frm) {
        for (const { parameter, control, initial } of frm._parameter_controls || []) {
            if (frm.doctype === "AI Configuration" && control.get_value() === initial
                && !(frm.doc.parameters || []).some(row => row.parameter_key === parameter.parameter_key)) continue;
            this.store(frm, parameter, control);
        }
    },
    async selection_changed(frm, field) {
        if (frm._restoring_parameter_selection) return false;
        const previous = frm._parameter_selection;
        const selected = frm.doc[field];
        if (previous && previous !== selected && (frm.doc.parameters || []).length) {
            const accepted = await new Promise(resolve => frappe.confirm(__("Changing the selection will discard unsaved provider parameters. Continue?"), () => resolve(true), () => resolve(false)));
            if (!accepted) {
                frm._restoring_parameter_selection = true;
                await frm.set_value(field, previous);
                frm._restoring_parameter_selection = false;
                return false;
            }
            frm.clear_table("parameters");
        }
        await this.render(frm);
        return true;
    }
};
