async function update_account_identity(frm) {
    const channel = frm.doc.channel;
    const schema = channel ? (await frappe.call({
        method: "messaging.communication.doctype.communication_provider_value.communication_provider_value.parameter_schema",
        args: {doctype: "Communication Channel Account", channel}
    })).message : {};
    if (channel !== frm.doc.channel) return;
    const automatic = !!schema.automatic_account_id;
    frm.set_df_property("channel", "read_only", !frm.is_new());
    frm.set_df_property("account_id", "read_only", automatic || !frm.is_new());
    frm.set_df_property("account_id", "reqd", !automatic);
    frm.set_df_property("account_id", "description", automatic && frm.is_new()
        ? __("Generated automatically when this account is saved.") : "");
    if (automatic && frm.is_new()) {
        if (!frm._automatic_account_id_display) {
            frm._automatic_account_id_display = {
                value: __("Automatic"),
                previous_value: frm.doc.account_id || ""
            };
        }
        await frm.set_value("account_id", frm._automatic_account_id_display.value);
    } else if (frm._automatic_account_id_display) {
        const display = frm._automatic_account_id_display;
        delete frm._automatic_account_id_display;
        if (frm.is_new() && frm.doc.account_id === display.value) {
            await frm.set_value("account_id", display.previous_value);
        }
    }
}

frappe.ui.form.on("Communication Channel Account", {
    setup(frm) { frm.set_query("channel", () => ({ filters: { include_disabled: 1 } })); },
    async refresh(frm) {
        await update_account_identity(frm);
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.render(frm);
    },
    async channel(frm) {
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.selection_changed(frm, "channel");
        await update_account_identity(frm);
    },
    enabled(frm) { if (window.messaging?.provider_parameters) messaging.provider_parameters.render(frm); },
    validate(frm) { messaging.provider_parameters.flush(frm); }
});
