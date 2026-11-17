frappe.ui.form.on("Bot Parameter", {
	setup(frm) {
		frm.set_query('language', 'request_phrases', () => ({
			query: 'messaging.chatbot.doctype.bot_parameter.bot_parameter.language_options'
		}));
	},
	refresh(frm) {
		if (!frm.is_dirty()) frm.__parameter_baseline = parameter_values(frm);
		if (frm.__referenced_save) return;
		const original = frm.save.bind(frm);
		frm.__referenced_save = true;
		frm.save = function (save_action, callback, btn, on_error, resolve, reject) {
			if (frm.is_new() || !frm.is_dirty() || frm.__referenced_passthrough) {
				return original(save_action, callback, btn, on_error, resolve, reject);
			}
			if (frm.__referenced_busy) return Promise.resolve();
			frm.__referenced_busy = true;
			return Promise.resolve().then(() => confirm_referenced_update(frm, original, [save_action, callback, btn, on_error, resolve, reject]))
				.catch((error) => {
					if (typeof on_error === "function") on_error();
					if (typeof reject === "function") reject(error);
					throw error;
				})
				.finally(() => {
					frm.__referenced_busy = false;
				});
		};
	},
	async parameter_type(frm) {
		if (frm.doc.parameter_type === "Expression") {
			await frm.set_value("allow_ai_extraction", 0);
			return;
		}
		if (!frm.doc.expression) return;
		const confirmed = await new Promise((resolve) => {
			frappe.confirm(
				__("Changing the parameter type clears the expression patterns when you save. Continue?"),
				() => resolve(true),
				() => resolve(false)
			);
		});
		if (confirmed) {
			await frm.set_value("expression", "");
		} else {
			await frm.set_value("parameter_type", "Expression");
		}
	},
});

const REFERENCED_UPDATE = "messaging.chatbot.doctype.bot_parameter.bot_parameter.";
const PARAMETER_FIELDS = [
	"label", "enabled", "parameter_type", "expression", "string_mode",
	"allow_contextual_name_extraction", "allow_ai_extraction", "description",
	"min_length", "max_length", "min_value", "max_value", "request_phrases",
];
const CHECK_FIELDS = ["enabled", "allow_contextual_name_extraction", "allow_ai_extraction"];
const INT_FIELDS = ["min_length", "max_length", "min_value", "max_value"];

function parameter_values(frm) {
	const values = {};
	for (const field of PARAMETER_FIELDS) values[field] = field === "request_phrases"
        ? (frm.doc.request_phrases || []).map(row => ({language: row.language, request_phrase: row.request_phrase || ""}))
        : frm.doc[field];
	return values;
}

function same_value(field, current, baseline) {
    if (field === 'request_phrases') {
        const normalize = rows => (rows || []).map(row => ({language: row.language, request_phrase: row.request_phrase || ''}));
        return JSON.stringify(normalize(current)) === JSON.stringify(normalize(baseline));
    }
	const empty = (value) => value === null || value === undefined || value === "";
	if (CHECK_FIELDS.includes(field)) return cint(current) === cint(baseline);
	if (INT_FIELDS.includes(field)) {
		const normalize = (value) => (empty(value) ? null : cint(value));
		return normalize(current) === normalize(baseline);
	}
	return String(current || "") === String(baseline || "");
}

function changed_values(frm) {
	const baseline = frm.__parameter_baseline || {};
	const values = {};
	for (const field of PARAMETER_FIELDS) {
		if (!same_value(field, frm.doc[field], baseline[field])) values[field] = parameter_values(frm)[field];
	}
	return values;
}

function escape(value) {
	return frappe.utils.escape_html(String(value ?? ""));
}

function confirm_referenced_update(frm, original, save_args) {
	const values = changed_values(frm);
	if (!Object.keys(values).some(field => field !== "request_phrases")) return original(...save_args);
	return frappe.call({
		method: REFERENCED_UPDATE + "preview_referenced_update",
		type: "POST",
		args: {name: frm.doc.name, values},
	}).then(({message}) => {
		if (!message.confirmation_required) return original(...save_args);
		return ask_referenced_update(frm, values, message);
	});
}

function ask_referenced_update(frm, values, preview) {
	const flows = (preview.consumers || []).filter((row) => row.doctype === "Bot Flow");
	const registration = (preview.consumers || []).some((row) => row.doctype === "Bot Settings");
	const count = flows.length || (preview.consumers || []).length;
	const items = flows.map((row) => `<li>${escape(row.label || row.name)}</li>`).join("");
	let body = `<p>${__("The parameter {0} is linked to {1} bot flows.", [escape(frm.doc.name), count])}</p>`;
	body += `<p>${__("The changes can affect recognition and validation of the parameters used by these flows:")}</p>`;
	body += `<ul>${items}</ul>`;
	if (registration) body += `<p>${__("Bot Settings registration also uses this parameter.")}</p>`;
	body += `<p>${__("If you continue, Messaging will save the parameter and validate the affected flows again. Previous approvals that are no longer valid will be invalidated.")}</p>`;
	return new Promise((resolve) => {
		let accepted = false;
		const dialog = new frappe.ui.Dialog({
			title: __("Update related flows"),
			primary_action_label: __("Update and revalidate ({0})", [count]),
			primary_action: async () => {
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const {message} = await frappe.call({
						method: REFERENCED_UPDATE + "apply_referenced_update",
						type: "POST",
						args: {name: frm.doc.name, values, dependency_token: preview.dependency_token},
						freeze: true,
					});
					accepted = true;
					dialog.hide();
					if (message.applied) await frm.reload_doc();
					show_referenced_result(message);
					resolve();
				} catch (error) {
					dialog.get_primary_btn().prop("disabled", false);
				}
			},
			secondary_action_label: __("Cancel"),
			secondary_action: () => dialog.hide(),
			onhide() {
				if (!accepted) resolve();
			},
		});
		dialog.$body.html(body);
		dialog.show();
	});
}

function approval_text(row) {
	if (row.structural === "incompatible") return __("Incompatible configuration");
	if (row.doctype !== "Bot Flow") return __("Registration checked");
	if (row.approval === "Passed") return __("Approved");
	if (row.approval === "Failed") return __("Failed");
	return __("Pending validation");
}

function show_referenced_result(message) {
	const failed = !message.applied || (message.needs_correction || []).length > 0;
	const rows = (message.consumers || []).map((row) => {
		const detail = row.structural_message ? ` — ${escape(row.structural_message)}` : "";
		const action = row.doctype === "Bot Flow" && row.structural === "valid" && row.approval !== "Passed"
			&& frappe.user.has_role("System Manager")
			? ` <button type="button" class="btn btn-xs btn-default" data-flow="${escape(row.name)}">${escape(__("Open Flow"))}</button>`
			: "";
		return `<li><strong>${escape(row.label || row.name)}</strong>: ${escape(approval_text(row))}${detail}${action}</li>`;
	}).join("");
	const intro = message.applied
		? __("Parameter updated")
		: __("The parameter was not changed because a related flow is incompatible.");
	const html = [
		`<p>${escape(intro)}</p>`,
		`<p>${escape(__("Affected flows"))}: ${escape(message.affected || 0)}</p>`,
		`<p>${escape(__("Flows validated"))}: ${escape((message.validated || []).join(", ") || "—")}</p>`,
		`<p>${escape(__("Flows that need corrections"))}: ${escape((message.needs_correction || []).join(", ") || "—")}</p>`,
		`<ul>${rows}</ul>`,
	].join("");
	const dialog = frappe.msgprint({
		title: __(failed ? "Related flows need attention" : "Related flows updated"),
		indicator: failed ? "orange" : "green",
		message: html,
	});
	dialog.$body.find("[data-flow]").on("click", (event) => {
		frappe.set_route("Form", "Bot Flow", event.currentTarget.dataset.flow);
	});
}
