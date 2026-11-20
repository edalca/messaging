frappe.ui.form.on("Communication Channel", {
    refresh(frm) {
        if (frm.is_new() || frm.doc.provider !== "WHATSAPP" || !frappe.user.has_role("System Manager")) return;

        const parent = frm.fields_dict.meta_webhook_details.$wrapper.empty();
        const webhook = renderMetaWebhook(frm, $("<div>").appendTo(parent));
        if (!frm.perm?.[0]?.write) return webhook;
        const actions = $("<div>").appendTo(parent);
        const button = (fieldname, label, click) => frappe.ui.form.make_control({
            parent: actions, render_input: true, df: { fieldtype: "Button", fieldname, label, click },
        });
        const hasToken = (frm.doc.parameters || []).some(row => row.parameter_key === "verify_token" && row.secret_value);
        const changeToken = async (replace) => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save pending changes before changing the webhook verify token."));
                return;
            }
            const args = { channel: frm.doc.name, expected_modified: frm.doc.modified };
            const update = async () => {
                await frappe.call({
                    method: replace ? "messaging.whatsapp.admin.regenerate_webhook_token" : "messaging.whatsapp.admin.generate_webhook_token",
                    type: "POST", args, freeze: true
                });
                await frm.reload_doc();
            };
            if (replace) {
                frappe.confirm(__("Regenerate the webhook verify token? You will need to update it in Meta."), update);
            } else {
                await update();
            }
        };
        if (!hasToken) {
            button("generate_webhook_token", __("Generate Token"), () => changeToken(false));
            return webhook;
        }

        button("copy_webhook_token", __("Copy Token"), async () => {
            const { message } = await frappe.call({
                method: "messaging.whatsapp.admin.copy_webhook_token",
                type: "POST", args: { channel: frm.doc.name }
            });
            frappe.utils.copy_to_clipboard(message.token);
        });
        button("regenerate_webhook_token", __("Regenerate Token"), () => changeToken(true));
        return webhook;
    }
});

async function renderMetaWebhook(frm, parent) {
    const { message } = await frappe.call({
        method: "messaging.whatsapp.admin.get_webhook_url",
        type: "GET",
        args: { channel: frm.doc.name },
    });
    if (!message.url) {
        $("<div>", { class: "alert alert-warning", role: "status" })
            .text(__("Meta needs a public HTTPS URL that is accessible from the internet. Configure the site host name to show the callback URL."))
            .appendTo(parent);
        return;
    }
    frappe.ui.form.make_control({
        parent,
        render_input: true,
        doc: { webhook_callback_url: message.url },
        df: {
            fieldtype: "Data",
            fieldname: "webhook_callback_url",
            label: __("Webhook URL"),
            read_only: 1,
        },
    });
    frappe.ui.form.make_control({
        parent,
        render_input: true,
        df: {
            fieldtype: "Button",
            fieldname: "copy_webhook_url",
            label: __("Copy URL"),
            click: () => frappe.utils.copy_to_clipboard(message.url),
        },
    });
}
