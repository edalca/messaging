frappe.ui.form.on("Communication Channel", {
    async before_load(frm) {
        if (!frappe.all_timezones) {
            const { message } = await frappe.call({ method: "frappe.core.doctype.user.user.get_timezones" });
            frappe.all_timezones = message.timezones;
        }
        frm.fields_dict.quota_timezone?.set_data(frappe.all_timezones);
    },
    async refresh(frm) {
        if (!frm.is_new() && frm.perm?.[0]?.write && frappe.user.has_role("System Manager")) {
            const enabled = frm.doc.status === "Enabled";
            frm.page.set_secondary_action(enabled ? __("Disable") : __("Enable"), async () => {
                if (frm.is_dirty()) {
                    frappe.msgprint(__("Save pending changes before changing channel status."));
                    return;
                }
                await frappe.call({
                    method: "messaging.communication.doctype.communication_channel.communication_channel.set_channel_status",
                    type: "POST", args: { channel: frm.doc.name, status: enabled ? "Disabled" : "Enabled" }, freeze: true
                });
                await frm.reload_doc();
            });
        }
        if (frm.is_new()) {
            let providers = [];
            frm.set_query("provider", () => ({ filters: { status: "Enabled", name: ["in", providers] } }));
            const { message } = await frappe.call({ method: "messaging.communication.doctype.communication_provider.communication_provider.channel_providers" });
            providers = message.map(provider => provider.id);
        }
        frm.set_df_property("provider", "read_only", !frm.is_new());
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        const token = frm.fields_dict.meta_webhook_token?.$wrapper.empty();
        const placements = !frm.is_new() && frm.doc.provider === "WHATSAPP" ? { verify_token: token } : {};
        await messaging.provider_parameters.render(frm, placements);
    },
    async provider(frm) {
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.selection_changed(frm, "provider");
    },
    async validate(frm) {
        const label = (frm.doc.channel_name || "").replace(/ +/g, " ").replace(/^ | $/g, "").normalize("NFC");
        if (!label) frappe.throw(__("Channel Name is required."));
        if (!/^[A-Za-z](?:[A-Za-z\u0300-\u036f]| [A-Za-z])*$/.test(label.normalize("NFD"))) {
            frappe.throw(__("Channel Name may contain only letters and spaces."));
        }
        if (label !== frm.doc.channel_name) await frm.set_value("channel_name", label);
        messaging.provider_parameters.flush(frm);
    }
});
