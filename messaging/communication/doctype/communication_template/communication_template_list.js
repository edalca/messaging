frappe.listview_settings["Communication Template"] = {
	async onload(list) {
		if (!frappe.user.has_role("System Manager")) return;
		const api = "messaging.communication.doctype.communication_template.external.";
		const {message: providers} = await frappe.call({method: api + "template_capabilities", type: "GET"});
		for (const entry of providers || []) {
			list.page.add_inner_button(__(entry.label), () => synchronize_templates(list, api, entry),
				__("Synchronize templates"));
		}
	},
};

function synchronize_templates(list, api, entry) {
	const fields = [{
		fieldname: "channel", fieldtype: "Link", options: "Communication Channel",
		label: __("Communication Channel"), reqd: 1,
		get_query: () => ({filters: {provider: entry.provider, status: "Enabled"}}),
		onchange() {
			if (entry.scope === "Account") dialog.set_value("account", null);
		},
	}];
	if (entry.scope === "Account") {
		fields.push({
			fieldname: "account", fieldtype: "Link", options: "Communication Channel Account",
			label: __("Communication Channel Account"), reqd: 1,
			get_query: () => ({filters: {channel: dialog.get_value("channel") || "", enabled: 1}}),
		});
	}
	const dialog = new frappe.ui.Dialog({
		title: __("Synchronize templates"), fields, primary_action_label: __("Synchronize"),
		async primary_action(values) {
			const {message} = await frappe.call({
				method: api + "synchronize_templates", type: "POST", freeze: true,
				args: {provider: entry.provider, ...values},
			});
			dialog.hide();
			await list.refresh();
			const escape = frappe.utils.escape_html;
			const summary = message.success
				? __("Created {0}, updated {1}, omitted {2}.", [message.created, message.updated, message.skipped.length])
				: __("The provider template list could not be read completely.");
			const entries = (rows) => rows.map(row => {
				const identity = [row.name || row.reference, row.provider_language, row.category].filter(Boolean);
				const reason = __(row.reason, row.language ? [row.language] : []);
				return `<li>${identity.map(value => escape(value)).join(" · ")} — ${escape(reason)}</li>`;
			}).join("");
			const skipped = entries(message.skipped);
			const warnings = entries(message.language_warnings || []);
			frappe.msgprint(escape(summary) + (skipped ? `<ul>${skipped}</ul>` : "")
				+ (warnings ? `<p>${escape(__("Language Mapping"))}</p><ul>${warnings}</ul>` : ""));
		},
	});
	dialog.show();
}
