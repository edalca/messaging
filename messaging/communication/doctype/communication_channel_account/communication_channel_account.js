async function update_account_identity(frm) {
    const channel = frm.doc.channel;
    const schema = channel ? (await frappe.call({
        method: "messaging.communication.doctype.communication_provider_value.communication_provider_value.parameter_schema",
        args: {doctype: "Communication Channel Account", channel}
    })).message : {};
    if (channel !== frm.doc.channel) return;
    const whatsapp = schema.provider === "WHATSAPP";
    const diagnostics = whatsapp && !frm.is_new() && frappe.user.has_role("System Manager");
    frm.toggle_display("account_id", whatsapp || frm.is_new());
    frm.set_df_property("account_id", "label", whatsapp ? "Phone Number ID" : "Account ID");
    frm.set_df_property("enabled", "read_only", whatsapp);
    frm.toggle_display("whatsapp_number_details", whatsapp);
    const number = frm.fields_dict.whatsapp_number_details.$wrapper.empty();
    if (whatsapp) {
        frappe.ui.form.make_control({
            parent: number,
            render_input: true,
            doc: { confirmed_number: frm.doc.whatsapp_display_phone_number || __("Unchecked") },
            df: { fieldname: "confirmed_number", fieldtype: "Data", label: __("WhatsApp Number"), read_only: 1 },
        });
    }
    for (const tab of frm.layout.tabs) {
        if (tab.df.fieldname === "diagnostics_tab") tab.df.hidden = !diagnostics;
    }
    frm.toggle_display(["status_summary_section", "summary_details"], diagnostics);
    frm.layout.refresh_tabs();
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
    return diagnostics;
}

const whatsappAccountMethod = "messaging.whatsapp.admin.";

function diagnosticStatus(diagnostic, confirmed = __("Confirmed")) {
    const status = ({
        confirmed,
        not_subscribed: __("Not subscribed"),
        failed: __("Failed"),
        meta_rejected: __("Rejected by Meta"),
        network_error: __("Connection error"),
        inconclusive: __("Inconclusive"),
    })[diagnostic?.result] || __("Unchecked");
    return diagnostic?.stale ? __("Historical ({0})", [status]) : status;
}

function diagnosticRows(parent, rows) {
    const list = $("<dl>", { class: "row small mb-3" }).appendTo(parent);
    for (const [label, value] of rows) {
        $("<dt>", { class: "col-sm-4", text: label }).appendTo(list);
        $("<dd>", { class: "col-sm-8 text-break", text: value ?? "—" }).appendTo(list);
    }
}

function needsAttention(diagnostic) {
    return ["failed", "meta_rejected", "network_error", "inconclusive"].includes(diagnostic?.result);
}

function renderDiagnostic(parent, diagnostic, identityLabel, interpretationShown = false) {
    if (!diagnostic) {
        $("<p>").text(__("Unchecked")).appendTo(parent);
        return;
    }
    const { request, response } = diagnostic;
    if (!interpretationShown) $("<p>", { text: diagnostic.interpretation }).appendTo(parent);
    $("<h6>").text(__("Request")).appendTo(parent);
    diagnosticRows(parent, [
        [__("Operation"), request.operation],
        [__("HTTP Method"), request.method],
        [__("Endpoint"), request.endpoint],
        [__("Query parameters"), JSON.stringify(request.parameters)],
        [identityLabel, request.technical_id],
        ...(request.graph_version ? [[__("Graph API Version"), request.graph_version]] : []),
    ]);
    $("<h6>").text(__("Response")).appendTo(parent);
    diagnosticRows(parent, [
        [__("HTTP status"), response.http_status],
        [__("Meta error code"), response.meta_code],
        [__("Meta error subcode"), response.meta_subcode],
        [__("Meta trace ID"), response.trace_id],
    ]);
    if (response.data.app_ids) {
        const { app_ids, app_count } = response.data;
        diagnosticRows(parent, [[__("Subscribed applications"), app_ids.join(", ") || __("None")]]);
        if (app_count > app_ids.length) {
            $("<p>", { class: "text-muted small", text: __("Showing {0} of {1} application IDs.", [app_ids.length, app_count]) }).appendTo(parent);
        }
    } else if (Object.keys(response.data).length) {
        $("<pre>", { class: "small p-2 border rounded" })
            .text(JSON.stringify(response.data, null, 2)).appendTo(parent);
    }
}

function renderWhatsAppDiagnostics(frm, local, expandedField) {
    const { connection, subscription } = local.results;
    const checkedAt = (date) => date ? frappe.datetime.str_to_user(date) : null;
    const statusWithDate = (diagnostic, confirmed) => [
        diagnosticStatus(diagnostic, confirmed), checkedAt(diagnostic?.checked_at),
    ].filter(Boolean).join(" · ");
    const section = (field, title, status, date, diagnostic) => {
        const panel = frm.fields_dict[field].$wrapper;
        const open = field === expandedField || panel.find("details").prop("open");
        const details = $("<details>", { class: "border rounded px-3 py-2 mb-2" })
            .prop("open", Boolean(open)).appendTo(panel.empty());
        const heading = $("<summary>").appendTo(details);
        $("<strong>", { text: title }).appendTo(heading);
        $("<span>", { class: "text-muted small ml-2", text: [status, checkedAt(date)].filter(Boolean).join(" · ") })
            .appendTo(heading);
        if (needsAttention(diagnostic)) {
            $("<span>", { class: "d-block text-danger small mt-1", text: diagnostic.interpretation }).appendTo(heading);
        }
        return $("<div>", { class: "mt-3" }).appendTo(details);
    };
    const connectionPanel = section("connection_details", __("Meta connection"),
        diagnosticStatus(connection), connection?.checked_at, connection);
    renderDiagnostic(connectionPanel, connection, __("Phone Number ID"), needsAttention(connection));
    const subscriptionStatus = __("Subscription confirmed");
    const subscriptionPanel = section("subscription_details", __("Subscriptions"),
        diagnosticStatus(subscription, subscriptionStatus), subscription?.checked_at, subscription);
    renderDiagnostic(subscriptionPanel, subscription, __("Messaging Account ID"), needsAttention(subscription));

    const summary = frm.fields_dict.summary_details.$wrapper.empty().attr("aria-live", "polite");
    diagnosticRows(summary, [
        [__("Meta connection"), statusWithDate(connection)],
        [__("Subscriptions"), statusWithDate(subscription, subscriptionStatus)],
        [__("Account status"), frm.doc.enabled ? __("Enabled") : __("Disabled")],
    ]);
}

async function runWhatsAppAction(frm, action) {
    if (frm.is_dirty()) {
        frappe.msgprint(__("Save pending account changes before running diagnostics or changing its status."));
        return;
    }
    const account = frm.doc.name;
    const args = { account, expected_modified: frm.doc.modified };
    const method = action === "disable" ? "disable_account" : "account_action";
    if (method === "account_action") args.action = action;
    await frappe.call({
        method: whatsappAccountMethod + method, type: "POST", args, freeze: true,
    });
    if (frm.doc.name !== account) return;
    if (action !== "disable") {
        const field = action === "subscription" ? "subscription_details" : "connection_details";
        frm._whatsapp_diagnostic_to_open = { account, field };
    }
    await frm.reload_doc();
}

async function setupWhatsAppDiagnostics(frm) {
    const account = frm.doc.name;
    const { message } = await frappe.call({
        method: whatsappAccountMethod + "account_diagnostics", type: "GET", args: { account },
    });
    if (frm.doc.name !== account) return;
    const target = frm._whatsapp_diagnostic_to_open;
    delete frm._whatsapp_diagnostic_to_open;
    const field = target?.account === account ? target.field : null;
    renderWhatsAppDiagnostics(frm, message, field);
    if (field) frm.layout.select_tab("diagnostics_tab");

    if (!frm.perm?.[0]?.write) return;
    if (frm.doc.enabled) {
        frm.add_custom_button(__("Disable"), () => frappe.confirm(
            __("Disable this number in Messaging?"), () => runWhatsAppAction(frm, "disable")
        ));
    } else {
        const label = __("Check and enable");
        frm.add_custom_button(label, () => runWhatsAppAction(frm, "enable"));
        frm.change_custom_button_type(label, null, "primary");
    }
    const group = __("WhatsApp Actions");
    frm.add_custom_button(__("Check connection"), () => runWhatsAppAction(frm, "check"), group);
    frm.add_custom_button(__("Check subscriptions"), () => runWhatsAppAction(frm, "subscription"), group);
}

frappe.ui.form.on("Communication Channel Account", {
    setup(frm) { frm.set_query("channel", () => ({ filters: { include_disabled: 1 } })); },
    async refresh(frm) {
        const diagnostics = await update_account_identity(frm);
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.render(frm);
        if (diagnostics) await setupWhatsAppDiagnostics(frm);
    },
    async channel(frm) {
        await frappe.require("/assets/messaging/js/provider_parameters.js");
        await messaging.provider_parameters.selection_changed(frm, "channel");
        await update_account_identity(frm);
    },
    enabled(frm) { if (window.messaging?.provider_parameters) messaging.provider_parameters.render(frm); },
    validate(frm) { messaging.provider_parameters.flush(frm); },
});
