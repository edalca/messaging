const whatsappChannelMethod = "messaging.whatsapp.admin.";

function renderWebhookStatus(parent, evidence) {
    const section = $("<div>", { class: "mt-3" }).appendTo(parent);
    $("<h6>", { text: __("Webhook Status") }).appendTo(section);
    const row = $("<div>", { class: "row" }).appendTo(section);

    function show(title, summary, details = []) {
        const column = $("<div>", { class: "col-sm-6 mb-2" }).appendTo(row);
        $("<div>", { class: "font-weight-bold", text: title }).appendTo(column);
        $("<div>", { text: summary }).appendTo(column);
        for (const [label, value] of details) {
            const detail = $("<div>", { class: "small" }).appendTo(column);
            $("<span>", { class: "text-muted mr-2", text: label }).appendTo(detail);
            $("<span>", { text: value }).appendTo(detail);
        }
    }

    const get = evidence?.get;
    show(__("URL Verification"), get?.challenge_answered ? __("Last challenge answered successfully") : __("Unchecked"),
        get ? [
            [__("Checked at"), frappe.datetime.str_to_user(get.received_at)],
            [__("HTTP Method"), "GET"],
            [__("Verification Token"), get.token_valid ? __("Validated") : __("Unchecked")],
            [__("Response"), `HTTP ${get.http_status}`],
        ] : []);

    const post = evidence?.post;
    const types = {
        messages: __("Messages"),
        statuses: __("Delivery statuses"),
        template: __("Template status"),
        mixed: __("Multiple event types"),
        other: __("Other event"),
        unknown: __("Unknown"),
    };
    const routing = {
        unconfirmed: __("Account routing unconfirmed"),
        matched: __("Account matched"),
        routed: __("Routed to an account"),
    };
    const processing = {
        recorded: __("Message receipt recorded"),
        pending: __("Pending worker processing"),
        running: __("Processing in progress"),
        blocked: __("Processing requires recovery"),
        ignored: __("Message ignored"),
        human_paused: __("Automation paused for human support"),
        quota_blocked: __("Processing stopped by quota"),
        complete: __("Message processed"),
        delivery_report_handled: __("Delivery report handled"),
        template_refresh_scheduled: __("Template status refresh scheduled"),
        unconfirmed: __("Processing unconfirmed"),
    };
    show(__("Event Reception"), post ? __("Signed webhook received") : __("Unchecked"),
        post ? [
            [__("Received at"), frappe.datetime.str_to_user(post.received_at)],
            [__("HTTP Method"), "POST"],
            [__("Event type"), types[post.event_type]],
            [__("HMAC signature"), post.hmac_valid ? __("Validated") : __("Unchecked")],
            [__("Routing"), routing[post.routing]],
            [__("Processing Status"), processing[post.processing]],
        ] : []);
}

frappe.ui.form.on("Communication Channel", {
    async refresh(frm) {
        if (frm.is_new() || frm.doc.provider !== "WHATSAPP" || !frappe.user.has_role("System Manager")) return;

        const channel = frm.doc.name;
        const { message } = await frappe.call({
            method: whatsappChannelMethod + "get_webhook_url",
            type: "GET",
            args: { channel },
        });
        if (frm.doc.name !== channel) return;

        const missingUrl = __("Meta needs a public HTTPS URL that is accessible from the internet. Configure the site host name to show the callback URL.");
        const parent = frm.fields_dict.meta_webhook_details.$wrapper.empty();
        if (message.url) {
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
        } else {
            $("<div>", { class: "alert alert-warning", role: "status", text: missingUrl }).appendTo(parent);
        }
        renderWebhookStatus(parent, message.evidence);

        const group = __("Webhook Actions");
        frm.add_custom_button(__("Copy webhook URL"), () => {
            if (message.url) frappe.utils.copy_to_clipboard(message.url);
            else frappe.msgprint(missingUrl);
        }, group);
        if (!frm.perm?.[0]?.write) return;

        frm.add_custom_button(__("Copy verification token"), async () => {
            const { message } = await frappe.call({
                method: whatsappChannelMethod + "copy_webhook_token",
                type: "POST",
                args: { channel },
            });
            frappe.utils.copy_to_clipboard(message.token);
        }, group);

        const hasToken = (frm.doc.parameters || []).some(row => row.parameter_key === "verify_token" && row.secret_value);
        const tokenAction = hasToken ? __("Regenerate verification token") : __("Generate verification token");
        frm.add_custom_button(tokenAction, async () => {
            if (frm.is_dirty()) {
                frappe.msgprint(__("Save pending changes before changing the webhook verify token."));
                return;
            }
            const args = { channel, expected_modified: frm.doc.modified };
            const update = async () => {
                await frappe.call({
                    method: whatsappChannelMethod + (hasToken ? "regenerate_webhook_token" : "generate_webhook_token"),
                    type: "POST", args, freeze: true,
                });
                await frm.reload_doc();
            };
            if (hasToken) {
                frappe.confirm(__("Regenerate the webhook verify token? You will need to update it in Meta Developers."), update);
            } else {
                await update();
            }
        }, group);
    },
});
