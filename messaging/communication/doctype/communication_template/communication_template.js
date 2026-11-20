frappe.provide("messaging.communication_template");

(() => {
	const COMPONENT_FIELDS = ["header", "body", "footer"];
	const COMPONENT_LABELS = { header: "Header", body: "Body", footer: "Footer" };
	const DEFAULT_COMPONENT_VARIABLE_LIMITS = { header: 1, body: null, footer: 1 };
	const TEMPLATE_VARIABLE_PATTERN = /^[a-z]+(?:_[a-z]+)*$/;
	const RAW_TOKEN_PATTERN = /\{\{([\s\S]*?)\}\}/g;
	const RESERVED_TEMPLATE_KEYS = new Set([
		"action",
		"text",
		"recipient",
		"variables",
		"version",
		"status",
		"template_reference",
		"content_type",
	]);

	function validate_variable_name(name) {
		if (
			typeof name !== "string" ||
			name.length < 1 ||
			name.length > 64 ||
			!TEMPLATE_VARIABLE_PATTERN.test(name)
		) {
			return __(
				"Invalid template parameter name. Use lowercase snake_case letters only (for example, 'numero_contrato')."
			);
		}
		if (RESERVED_TEMPLATE_KEYS.has(name)) {
			return __("Template parameter '{0}' is a reserved technical field.", [name]);
		}
		return null;
	}

	function inspect_component(text, component_label) {
		if (text === null || text === undefined || text === "") {
			return { variables: [], errors: [], has_syntax_error: false };
		}
		const translated_label = __(component_label);
		if (typeof text !== "string" || text.includes("\x00")) {
			return {
				variables: [],
				errors: [__("Invalid template {0} content.", [translated_label])],
				has_syntax_error: true,
			};
		}
		const errors = [];
		let has_syntax_error = false;

		if (component_label === "Header" || component_label === "Footer") {
			if (text.length > 140 || text.includes("\n") || text.includes("\r")) {
				errors.push(
					__("Template {0} must be a single line of at most 140 characters.", [
						translated_label,
					])
				);
			} else if (!text.trim()) {
				errors.push(__("Template {0} cannot contain only whitespace.", [translated_label]));
			}
		} else {
			if (text.length > 16000) {
				errors.push(__("Template Body exceeds the maximum allowed length."));
			} else if (!text.trim()) {
				errors.push(__("Template Body cannot contain only whitespace."));
			}
		}

		if (
			text.includes("{%") ||
			text.includes("%}") ||
			text.includes("{#") ||
			text.includes("#}")
		) {
			errors.push(
				__("Executable template expressions are not allowed in {0}.", [translated_label])
			);
			return { variables: [], errors, has_syntax_error: true };
		}

		const open_count = (text.match(/\{\{/g) || []).length;
		const close_count = (text.match(/\}\}/g) || []).length;
		const stripped = text.replace(RAW_TOKEN_PATTERN, "");
		const placeholder_example = "{{" + __("variable_name") + "}}";
		if (open_count !== close_count || stripped.includes("{") || stripped.includes("}")) {
			errors.push(
				__("Malformed placeholder in {0}. Use exact '{1}' syntax.", [
					translated_label,
					placeholder_example,
				])
			);
			return { variables: [], errors, has_syntax_error: true };
		}

		const variables = [];
		for (const match of text.matchAll(RAW_TOKEN_PATTERN)) {
			const raw_token = match[1];
			if (
				!raw_token ||
				raw_token.includes("{") ||
				raw_token.includes("}") ||
				/\s/.test(raw_token)
			) {
				errors.push(
					__("Malformed placeholder in {0}. Use exact '{1}' syntax.", [
						translated_label,
						placeholder_example,
					])
				);
				has_syntax_error = true;
				continue;
			}
			const name_error = validate_variable_name(raw_token);
			if (name_error) {
				errors.push(name_error);
				has_syntax_error = true;
				continue;
			}
			variables.push(raw_token);
		}

		return { variables, errors, has_syntax_error };
	}

	function inspect_template(doc, formats = doc?.__onload?.template_capabilities?.parameter_formats) {
		const format = doc?.parameters?.find(row => row.parameter_key === "parameter_format");
		const selected = (format && JSON.parse(format.value || "null")) || formats?.[0]?.[0];
		const repeat = formats?.some(([code, allowed]) => code === selected && allowed);
		const warnings = [];
		const push_warning = (msg) => {
			if (msg && !warnings.includes(msg)) warnings.push(msg);
		};

		const declared = [];
		const declared_seen = new Set();
		for (const row of doc?.template_variables || []) {
			const param_name = row?.parameter_name;
			if (typeof param_name !== "string" || !param_name) {
				push_warning(__("Each template parameter row must define a parameter name."));
				continue;
			}
			const name_error = validate_variable_name(param_name);
			if (name_error) {
				push_warning(name_error);
			}
			if (declared_seen.has(param_name)) {
				push_warning(__("Duplicate template parameter declaration: '{0}'.", [param_name]));
			} else {
				declared_seen.add(param_name);
				declared.push(param_name);
			}
		}

		let has_syntax_error = false;
		const used_occurrences = [];
		const used_seen = new Set();
		const unique_valid_variables = [];

		for (const field of COMPONENT_FIELDS) {
			const label = COMPONENT_LABELS[field];
			const comp = inspect_component(doc?.[field], label);
			if (comp.has_syntax_error) has_syntax_error = true;
			for (const err of comp.errors) push_warning(err);

			const limit = DEFAULT_COMPONENT_VARIABLE_LIMITS[field];
			if (limit !== null && comp.variables.length > limit) {
				push_warning(
					__("Template {0} allows at most {1} variable(s).", [__(label), limit])
				);
			}

			for (const var_name of comp.variables) {
				if (used_seen.has(var_name)) {
					if (repeat) continue;
					push_warning(
						__(
							"Template variable '{0}' cannot be used more than once across Header, Body, and Footer.",
							[var_name]
						)
					);
				} else {
					used_seen.add(var_name);
					unique_valid_variables.push(var_name);
				}
				used_occurrences.push(var_name);
			}
		}

		const undeclared = unique_valid_variables.filter((v) => !declared_seen.has(v));
		const unused = declared.filter((p) => !used_seen.has(p));

		if (!has_syntax_error) {
			if (undeclared.length > 0) {
				push_warning(
					__("Template uses undeclared parameter(s): {0}.", [undeclared.join(", ")])
				);
			}
			if (unused.length > 0) {
				push_warning(
					__(
						"Declared template parameter(s) are not used in Header, Body, or Footer: {0}.",
						[unused.join(", ")]
					)
				);
			}
		}

		return {
			valid: warnings.length === 0,
			has_syntax_error,
			warnings,
			declared,
			declared_seen,
			used_occurrences,
			used_seen,
			unique_valid_variables,
			undeclared,
			unused,
		};
	}

	function render_warnings(frm, inspection) {
		if (!frm?.doc) return null;
		const state = inspection || inspect_template(frm.doc, frm._template_capabilities?.parameter_formats);
		frm._template_validation = state;
		if (typeof frm.set_intro === "function") {
			const warnings = state.warnings.map((w) => frappe.utils.escape_html(w));
			const issue = frm.doc.last_sync_error || frm.doc.__onload?.template_sync_issue;
			if (issue) warnings.push(frappe.utils.escape_html(__(issue)));
			if (frm.doc.__onload?.template_status_supported && !frm.is_new()) {
				const syncErrors = (frm.doc.statuses || []).filter((row) => row.last_error);
				if (syncErrors.length) {
					warnings.push(
						syncErrors
							.map((row) =>
								frappe.utils.escape_html(
									__("External status error ({0}): {1}", [row.scope_key || row.scope_type, row.last_error])
								)
							)
							.join("<br>")
					);
				}
			}
			if (warnings.length > 0) {
				const html = warnings.join("<br>");
				frm.set_intro(html, "orange");
			} else {
				frm.set_intro(null);
				frm.dashboard?.clear_headline?.();
			}
		}
		return state;
	}

	function sync_component_variables(frm) {
		if (!frm?.doc || frm._syncing_template_variables) return null;
		const initial = inspect_template(frm.doc, frm._template_capabilities?.parameter_formats);
		if (!initial.has_syntax_error) {
			const missing = initial.unique_valid_variables.filter(
				(name) => !initial.declared_seen.has(name)
			);
			if (missing.length > 0) {
				frm._syncing_template_variables = true;
				try {
					for (const parameter_name of missing) {
						frm.add_child("template_variables", { parameter_name });
					}
					frm.refresh_field?.("template_variables");
					if (!frm.is_dirty?.()) frm.dirty?.();
				} finally {
					frm._syncing_template_variables = false;
				}
				return render_warnings(frm);
			}
		}
		return render_warnings(frm, initial);
	}

	function bind_component_inputs(frm) {
		let state = frm._template_component_edits;
		if (!state || state.doc !== frm.doc) {
			for (const binding of Object.values(state?.bindings || {})) {
				binding.$input.off(".template_variables");
			}
			state = { doc: frm.doc, bindings: {}, pending: Promise.resolve() };
			frm._template_component_edits = state;
		}
		for (const field of COMPONENT_FIELDS) {
			const control = frm.fields_dict?.[field];
			const $input = control?.$input;
			const previous = state.bindings[field];
			if (previous?.control === control && previous?.$input[0] === $input?.[0]) continue;
			previous?.$input.off(".template_variables");
			delete state.bindings[field];
			if (!$input?.length) continue;
			const binding = { control, $input, confirmed: control.get_input_value() };
			state.bindings[field] = binding;
			$input.off(".template_variables").on("blur.template_variables", () => {
				if (!is_current_binding(frm, state, field, binding)) return;
				confirm_component_edits(frm, [field]).catch(console.error);
			});
		}
		return state;
	}

	function is_current_binding(frm, state, field, binding) {
		return (
			frm.doc === state.doc &&
			frm._template_component_edits === state &&
			state.bindings[field] === binding &&
			frm.fields_dict?.[field] === binding.control &&
			binding.control.$input?.[0] === binding.$input[0] &&
			binding.$input[0].isConnected
		);
	}

	function confirm_component_edits(frm, fields = COMPONENT_FIELDS) {
		const state = frm._template_component_edits || bind_component_inputs(frm);
		// Serialize blur and save so a confirmed edit is consumed only once.
		const pending = state.pending.then(async () => {
			let changed = false;
			for (const field of fields) {
				const binding = state.bindings[field];
				if (!binding || !is_current_binding(frm, state, field, binding)) continue;
				const { control } = binding;
				const value = control.get_input_value();
				// ControlData also updates on change and debounced input. Await the
				// native setter when needed instead of relying on their event order.
				if ((control.get_model_value() ?? "") !== value) {
					await control.parse_validate_and_set_in_model(value);
				}
				if (!is_current_binding(frm, state, field, binding)) continue;
				if (value === binding.confirmed) continue;
				binding.confirmed = value;
				changed = true;
			}
			if (changed) return sync_component_variables(frm);
		});
		state.pending = pending.catch(() => {});
		return pending;
	}

	function lock_template_identity(frm) {
		const locked = !frm.is_new();
		for (const field of ["template_name", "channel", "account", "provider_language"]) {
			frm.set_df_property(field, "read_only", locked);
		}
		frm.set_df_property("language", "read_only", locked && !frm.doc.provider_language);
		frm.set_df_property("status", "read_only", 1);
	}

	function show_template_status(frm) {
		if (frm.is_new()) {
			frm.page?.clear_indicator?.();
			return;
		}
		const status = frm.doc.status || "Inactive";
		frm.page?.set_indicator?.(__(status), status === "Active" ? "green" : "gray");
	}

	async function toggle_template_active(frm, active) {
		if (frm._template_status_busy) return;
		if (frm.is_new() || frm.is_dirty()) {
			frappe.msgprint(__("Save pending changes before changing template status."));
			return;
		}
		frm._template_status_busy = true;
		frm.template_lifecycle_button?.prop?.("disabled", true);
		try {
			const response = await frappe.call({
				method: "messaging.communication.doctype.communication_template.communication_template.set_active",
				type: "POST",
				args: {
					candidate: { name: frm.doc.name, modified: frm.doc.modified },
					active,
				},
				freeze: true,
				freeze_message: active ? __("Activating template...") : __("Deactivating template..."),
			});
			if (response?.message && typeof frm.reload_doc === "function") {
				await frm.reload_doc();
			}
		} finally {
			frm._template_status_busy = false;
			refresh_template_lifecycle(frm);
		}
	}

	function refresh_template_lifecycle(frm) {
		frm.template_lifecycle_button?.remove?.();
		frm.template_lifecycle_button = null;
		show_template_status(frm);
		if (frm.is_new() || !frappe.user?.has_role("System Manager") || !frm.perm?.[0]?.write) {
			return;
		}
		const isActive = frm.doc.status === "Active";
		const label = isActive ? __("Deactivate") : __("Activate");
		const button = frm.page?.add_button?.(
			label,
			() => toggle_template_active(frm, isActive ? 0 : 1),
			{ btn_class: isActive ? "btn-default" : "btn-primary" }
		);
		frm.template_lifecycle_button = button || null;
		const syncDirtyState = () => {
			frm.template_lifecycle_button?.prop?.("disabled", frm.is_dirty() || Boolean(frm._template_status_busy));
		};
		frm.$wrapper?.off("dirty.template_lifecycle").on("dirty.template_lifecycle", syncDirtyState);
		syncDirtyState();
	}

	Object.assign(messaging.communication_template, {
		validate_variable_name,
		inspect_component,
		inspect_template,
		render_warnings,
		sync_component_variables,
		bind_component_inputs,
		confirm_component_edits,
		lock_template_identity,
		show_template_status,
		refresh_template_lifecycle,
		toggle_template_active,
	});
})();

frappe.ui.form.on("Communication Template", {
	setup(frm) {
		frm.set_query("account", () => ({filters: {channel: frm.doc.channel || ""}}));
		frm.set_query("language", () => ({filters: {enabled: 1}}));
	},
	async refresh(frm) {
		messaging.communication_template.lock_template_identity(frm);
		messaging.communication_template.bind_component_inputs(frm);
		messaging.communication_template.refresh_template_lifecycle(frm);
		await refresh_template_capabilities(frm);
		messaging.communication_template.render_warnings(frm);
		if (frm.fields_dict.parameter_controls) {
			await frappe.require('/assets/messaging/js/provider_parameters.js');
			await messaging.provider_parameters.render(frm);
		}
	},
	template_variables_delete(frm) {
		if (frm._syncing_template_variables) return;
		messaging.communication_template.render_warnings(frm);
	},
	async validate(frm) {
		await messaging.communication_template.confirm_component_edits(frm);
		messaging.provider_parameters?.flush(frm);
	},
	async channel(frm) {
		if (!frm.is_new()) return;
		if (frm.doc.account) await frm.set_value("account", null);
		if (frm.doc.provider_language) {
			if (frm.doc.language_mapping !== "Manual") await frm.set_value("language", null);
			await frm.set_value({provider_language: "", language_mapping: ""});
		}
		await refresh_template_capabilities(frm);
		await frappe.require('/assets/messaging/js/provider_parameters.js');
		await messaging.provider_parameters.selection_changed(frm, 'channel');
		messaging.communication_template.render_warnings(frm);
	},
	provider_parameter_format(frm) {
		messaging.communication_template.render_warnings(frm);
	},
	async provider_language(frm) {
		const doc = frm.doc;
		const {channel, provider_language, language} = doc;
		if (!frm.is_new() || !channel) return;
		if (!provider_language) {
			if (doc.language_mapping !== "Manual") await frm.set_value("language", null);
			await frm.set_value("language_mapping", "");
			return;
		}
		const {message} = await frappe.call({
			method: template_external_api + "template_language_preview", type: "GET",
			args: {channel, provider_language},
		});
		if (frm.doc !== doc || doc.channel !== channel || doc.provider_language !== provider_language
			|| doc.language !== language) return;
		frm._applying_template_language = true;
		try {
			await frm.set_value("language", message.language || null);
			await frm.set_value("language_mapping", message.origin);
		} finally {
			frm._applying_template_language = false;
		}
	},
	language(frm) {
		if (frm.doc.provider_language && !frm._applying_template_language) {
			return frm.set_value("language_mapping", frm.doc.language ? "Manual" : "Unassigned");
		}
	},
});

frappe.ui.form.on("Communication Template Parameter", {
	parameter_name(frm) {
		if (frm._syncing_template_variables) return;
		messaging.communication_template.render_warnings(frm);
	},
	template_variables_add(frm) {
		if (frm._syncing_template_variables) return;
		messaging.communication_template.render_warnings(frm);
	},
	template_variables_remove(frm) {
		if (frm._syncing_template_variables) return;
		messaging.communication_template.render_warnings(frm);
	},
});


const template_external_api = "messaging.communication.doctype.communication_template.external.";

async function refresh_template_capabilities(frm) {
	const channel = frm.doc.channel;
	let capabilities = frm.doc.__onload?.template_capabilities;
	const manager = frappe.user?.has_role("System Manager");
	if (frm.is_new() && channel && manager) {
		const result = await frappe.call({method: template_external_api + "template_capabilities", type: "GET", args: {channel}});
		if (frm.doc.channel !== channel) return;
		capabilities = result.message;
	}
	frm._template_capabilities = capabilities;
	const scoped = capabilities?.scope === "Account";
	const languages = capabilities?.languages || [];
	const hasCatalog = Boolean(languages.length || frm.doc.provider_language);
	if (hasCatalog) {
		const escape = frappe.utils.escape_html;
		const options = ["", ...languages.map(({code, label}) => ({
			value: code, label: escape(`${__(label)} (${code})`),
		}))];
		if (frm.doc.provider_language && !languages.some(row => row.code === frm.doc.provider_language)) {
			options.push({value: frm.doc.provider_language, label: escape(frm.doc.provider_language), disabled: true});
		}
		frm.set_df_property("provider_language", "options", options);
	}
	frm.toggle_display?.(["provider_language", "language_mapping"], hasCatalog);
	frm.toggle_reqd?.("provider_language", hasCatalog);
	frm.toggle_reqd?.("language", !hasCatalog || frm.doc.status === "Active");
	frm.toggle_display?.("account", scoped || Boolean(frm.doc.account));
	frm.toggle_reqd?.("account", scoped);
	frm.toggle_display?.(["provider_status", "published_revision", "last_sync_error"], Boolean(capabilities?.query));
	frm.toggle_display?.("external_status_section", Boolean(capabilities?.query && !capabilities.managed && !frm.is_new()));
	const unverified = Boolean(capabilities?.managed && !frm.doc.published_revision);
	frm.toggle_display?.("unverified_revision", unverified);
	if (unverified) {
		const escape = frappe.utils.escape_html;
		frm.fields_dict.unverified_revision.$wrapper.html(
			`<label class="control-label">${escape(__("Published Revision"))}</label>
			<div class="control-value like-disabled-input">${escape(__("Unverified"))}</div>`
		);
	}

	const providerLabel = {not_applicable: __("Not applicable"), not_synchronized: __("Unverified"), multiple: __("Multiple statuses")};
	const label = providerLabel[frm.doc.provider_status];
	if (label) frm.fields_dict.provider_status?.$input?.val(label);

	for (const label of ["Synchronize status", "Publish to provider", "Use provider content"]) {
		frm.remove_custom_button?.(__(label));
	}
	if (frm.is_new() || !manager || !frm.perm?.[0]?.write) return;
	if (capabilities?.query) {
		frm.add_custom_button(__("Synchronize status"), () => template_action(frm,
			"messaging.communication.doctype.communication_template.status.synchronize_template_status"));
	}
	if (capabilities?.publish && !frm.doc.__onload?.template_publication_pending) {
		frm.add_custom_button(__("Publish to provider"), () => publish_template(frm, capabilities));
	}
	if (capabilities?.managed && frm.doc.sync_state === "Conflict") {
		frm.add_custom_button(__("Use provider content"), () => frappe.confirm(
			__("Replace this local draft with the current provider content?"),
			() => template_action(frm, template_external_api + "adopt_external_template")));
	}
}

async function template_action(frm, method, options) {
	if (frm.is_dirty() || frm.is_new()) {
		frappe.msgprint(__("Save pending template changes first."));
		return;
	}
	const args = {name: frm.doc.name};
	if (!method.endsWith(".synchronize_template_status")) args.modified = frm.doc.modified;
	if (options) args.options = options;
	await frappe.call({method, type: "POST", args, freeze: true});
	await frm.reload_doc();
}

function publish_template(frm, capabilities) {
	if (frm.is_dirty()) return frappe.msgprint(__("Save pending template changes first."));
	const fields = (capabilities.fields || []).map(([fieldname, label, fieldtype, choices]) => ({
		fieldname, label: __(label), fieldtype, options: choices.join("\n"),
		reqd: fieldtype === "Select" ? 1 : 0,
	}));
	if (frm.doc.sync_state === "Conflict") {
		fields.push({fieldname: "confirm_conflict", label: __("Replace the current provider content"), fieldtype: "Check"});
	}
	const publish = (options) => template_action(frm, template_external_api + "publish_template", options);
	if (fields.length) {
		frappe.prompt(fields, publish, __("Publish to provider"), __("Publish"));
	} else {
		frappe.confirm(__("Publish this saved draft to the provider?"), () => publish({}));
	}
}
