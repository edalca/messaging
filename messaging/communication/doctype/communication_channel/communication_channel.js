frappe.ui.form.on("Communication Channel", {
    async before_load(frm) {
        if (!frappe.all_timezones) {
            const { message } = await frappe.call({ method: "frappe.core.doctype.user.user.get_timezones" });
            frappe.all_timezones = message.timezones;
        }
        frm.fields_dict.quota_timezone?.set_data(frappe.all_timezones);
    },
    async refresh(frm) {
        if (frm.is_new()) {
            let providers = [];
            frm.set_query("provider", () => ({ filters: { enabled: 1, name: ["in", providers] } }));
            const { message } = await frappe.call({ method: "messaging.communication.doctype.communication_provider.communication_provider.channel_providers" });
            providers = message.map(provider => provider.id);
        }
        frm.set_df_property("provider", "read_only", !frm.is_new());
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.render(frm);
    },
    async provider(frm) {
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.selection_changed(frm, "provider");
    },
    enabled(frm) { if (window.messaging?.provider_parameters) messaging.provider_parameters.render(frm); },
    validate(frm) { messaging.provider_parameters.flush(frm); }
});
