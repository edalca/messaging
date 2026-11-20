# Copyright (c) 2026, Edwin Carrillo and Contributors
# See license.txt

import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

import frappe
from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.communication.doctype.communication_provider.communication_provider import (
	_lock_provider_states,
	_write_provider_states,
	get_provider_states,
	set_provider_enabled,
)

JS_PATH = Path(__file__).with_name("communication_template.js")


def run_js_harness(script_body):
	"""Execute communication_template.js inside Node.js with a minimal Frappe Form harness."""
	harness = f"""
const fs = require("fs");
const assert = require("assert/strict");

global.window = global;
global.frappe = {{
	provide(ns) {{
		const parts = ns.split(".");
		let cur = global;
		for (const p of parts) {{
			cur[p] = cur[p] || {{}};
			cur = cur[p];
		}}
		return cur;
	}},
	utils: {{
		escape_html(str) {{
			return String(str ?? "")
				.replace(/&/g, "&amp;")
				.replace(/</g, "&lt;")
				.replace(/>/g, "&gt;")
				.replace(/"/g, "&quot;")
				.replace(/'/g, "&#39;");
		}},
	}},
	ui: {{
		form: {{
			handlers: {{}},
			on(doctype, handlers) {{
				this.handlers[doctype] = Object.assign(this.handlers[doctype] || {{}}, handlers);
			}},
		}},
	}},
	require: async () => {{}},
	call: async () => ({{message: ["emulator"]}}),
}};
global.messaging = global.frappe.provide("messaging");
global.__ = (txt, args) => {{
	if (!args) return txt;
	return txt.replace(/\\{{(\\d+)\\}}/g, (_, idx) => (args[Number(idx)] !== undefined ? args[Number(idx)] : _));
}};

const code = fs.readFileSync({json.dumps(str(JS_PATH))}, "utf8");
eval(code);

function createForm(initialDoc = {{}}) {{
	const isNew = Boolean(initialDoc.__islocal);
	const doc = Object.assign(
		{{
			doctype: "Communication Template",
			name: isNew ? "new-communication-template-1" : "TMPL-TEST",
			__islocal: isNew ? 1 : 0,
			template_name: "TMPL-TEST",
			channel: "emulator",
			language: "es",
			status: "Inactive",
			provider_status: "not_applicable",
			header: "",
			body: "",
			footer: "",
			template_variables: [],
			parameters: [],
			statuses: [],
			__onload: {{ template_status_supported: false }},
		}},
		initialDoc
	);
	doc.template_variables = (doc.template_variables || []).map((r, i) => ({{
		doctype: "Communication Template Parameter",
		name: r.name || "row-" + (i + 1),
		idx: i + 1,
		parameter_name: r.parameter_name,
	}}));

	const frm = {{
		doctype: "Communication Template",
		doc,
		perm: [{{ write: 1 }}],
		_dirty: false,
		_dirty_calls: 0,
		_refresh_calls: 0,
		_intro: null,
		_intro_color: null,
		_df_props: {{}},
		_display: {{}},
		_custom_buttons: {{}},
		_page_buttons: [],
		_indicator: null,
		fields_dict: {{}},
		page: {{
			set_indicator(label, color) {{
				frm._indicator = {{ label, color }};
			}},
			clear_indicator() {{
				frm._indicator = null;
			}},
			add_button(label, handler, opts) {{
				const btn = {{
					label,
					handler,
					opts,
					disabled: false,
					prop(k, v) {{
						if (k === "disabled") this.disabled = Boolean(v);
						return this;
					}},
					remove() {{
						frm._page_buttons = frm._page_buttons.filter((item) => item !== btn);
					}},
				}};
				frm._page_buttons.push(btn);
				return btn;
			}},
		}},
		dashboard: {{
			clear_headline() {{
				frm._intro = null;
				frm._intro_color = null;
			}},
		}},
		$wrapper: {{
			handlers: {{}},
			off(ev) {{
				delete this.handlers[ev];
				return this;
			}},
			on(ev, fn) {{
				this.handlers[ev] = fn;
				return this;
			}},
		}},
		is_new: () => Boolean(frm.doc.__islocal),
		is_dirty: () => frm._dirty,
		set_query() {{}},
		set_df_property(fieldname, prop, val) {{
			frm._df_props[fieldname] = frm._df_props[fieldname] || {{}};
			frm._df_props[fieldname][prop] = val;
		}},
		toggle_display(fieldname, visible) {{
			frm._display[fieldname] = Boolean(visible);
		}},
		add_custom_button(label, handler) {{
			frm._custom_buttons[label] = handler;
		}},
		remove_custom_button(label) {{
			delete frm._custom_buttons[label];
		}},
		set_intro(html, color) {{
			frm._intro = html || null;
			frm._intro_color = html ? color || "blue" : null;
		}},
		async reload_doc() {{}},
		add_child(fieldname, values) {{
			const list = frm.doc[fieldname] || (frm.doc[fieldname] = []);
			const row = Object.assign(
				{{
					doctype: "Communication Template Parameter",
					name: "row-" + (list.length + 1),
					idx: list.length + 1,
				}},
				values
			);
			list.push(row);
			frappe.ui.form.handlers["Communication Template Parameter"]?.template_variables_add?.(frm);
			return row;
		}},
		refresh_field(fieldname) {{
			if (fieldname === "template_variables") frm._refresh_calls += 1;
		}},
		dirty() {{
			frm._dirty = true;
			frm._dirty_calls += 1;
		}},
		async setComponent(field, value) {{
			frm.fields_dict[field].$input.val(value);
			await frm.blurComponent(field);
		}},
		async blurComponent(field) {{
			frm.fields_dict[field].$input.handlers["blur.template_variables"]();
			await frm._template_component_edits.pending;
		}},
		removeParameterRow(paramName) {{
			frm.doc.template_variables = frm.doc.template_variables.filter(
				(r) => r.parameter_name !== paramName
			);
			frappe.ui.form.handlers["Communication Template Parameter"].template_variables_remove(frm);
		}},
		triggerRefresh() {{
			return frappe.ui.form.handlers["Communication Template"].refresh(frm);
		}},
		triggerValidate() {{
			return frappe.ui.form.handlers["Communication Template"].validate(frm);
		}},
	}};
	for (const field of ["header", "body", "footer", "provider_status"]) {{
		frm.fields_dict[field] = createControl(frm, field);
	}}
	messaging.communication_template.bind_component_inputs(frm);
	return frm;
}}

function createControl(frm, field) {{
	let value = frm.doc[field] ?? "";
	const $input = {{
		0: {{ isConnected: true }},
		length: 1,
		handlers: {{}},
		val(next) {{
			if (next === undefined) return value;
			value = next;
		}},
		on(event, handler) {{
			assert.equal(this.handlers[event], undefined, "Listener must not be duplicated");
			this.handlers[event] = handler;
			return this;
		}},
		off(namespace) {{
			for (const event of Object.keys(this.handlers)) {{
				if (event.endsWith(namespace)) delete this.handlers[event];
			}}
			return this;
		}},
	}};
	return {{
		$input,
		get_input_value: () => value,
		get_model_value: () => frm.doc[field],
		async parse_validate_and_set_in_model(next) {{
			// Native ControlData writes asynchronously; confirmation must await it.
			await Promise.resolve();
			frm.doc[field] = next;
		}},
	}};
}}

(async () => {{
{script_body}
}})().catch((err) => {{
	console.error(err);
	process.exit(1);
}});
"""
	proc = subprocess.run(
		["node", "-e", harness],
		capture_output=True,
		text=True,
		check=False,
	)
	if proc.returncode != 0:
		raise AssertionError(f"JS harness failed ({proc.returncode}):\n{proc.stderr}\n{proc.stdout}")
	return json.loads(proc.stdout.strip())


class TestCommunicationTemplate(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.original_states = get_provider_states()
		cls.original_user = frappe.session.user
		cls.accounts = []
		cls.templates = []
		frappe.set_user("Administrator")
		cls.network = patch(
			"requests.sessions.Session.request",
			side_effect=AssertionError("External network forbidden"),
		)
		cls.network.start()
		set_provider_enabled("EMULR", True)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		if cls.templates:
			frappe.db.delete("Communication Provider Value", {"parent": ["in", cls.templates]})
			frappe.db.delete("Communication Template Parameter", {"parent": ["in", cls.templates]})
			frappe.db.delete("Communication Template", {"name": ["in", cls.templates]})
		for name in cls.accounts:
			if frappe.db.exists("Communication Channel Account", name):
				frappe.delete_doc("Communication Channel Account", name)
		_lock_provider_states()
		_write_provider_states(cls.original_states)
		frappe.db.commit()
		cls.network.stop()
		frappe.set_user(cls.original_user)

	def setUp(self):
		frappe.set_user("Administrator")
		if not self.accounts:
			acc = frappe.get_doc(
				dict(
					doctype="Communication Channel Account",
					channel="emulator",
					account_id="tmpl-auto-" + frappe.generate_hash(length=10),
					account_name="Synthetic template auto test",
					enabled=1,
				)
			).insert()
			self.accounts.append(acc.name)
			frappe.db.commit()
		self.account_name = self.accounts[0]

	def test_component_blur_requires_real_edit_and_live_control(self):
		"""Ignore typing and unchanged focus; bind once and discard replaced controls."""
		result = run_js_harness(
			"""
			const frm = createForm();
			const api = messaging.communication_template;
			for (const field of ["header", "body", "footer"]) {
				const control = frm.fields_dict[field];
				assert.deepEqual(Object.keys(control.$input.handlers), ["blur.template_variables"]);
				for (const value of ["{", "{{", "{{n", "{{nombre", "{{nombre_abonado}}"]) {
					control.$input.val(value);
					// Even native input/change model updates cannot trigger detection.
					await control.parse_validate_and_set_in_model(value);
					assert.deepEqual(frm.doc.template_variables, []);
					assert.equal(frm._intro, null);
				}
				await frm.blurComponent(field);
				assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
				frm.removeParameterRow("nombre_abonado");
				await frm.triggerRefresh();
				frm._dirty_calls = 0;
				await frm.blurComponent(field);
				await frm.triggerValidate();
				assert.deepEqual(frm.doc.template_variables, []);
				assert.equal(frm._dirty_calls, 0);
				await frm.setComponent(field, "Hola {{nombre_abonado}}");
				assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
				await frm.setComponent(field, "");
				frm.removeParameterRow("nombre_abonado");
			}
			const stale = frm.fields_dict.body;
			const oldBlur = stale.$input.handlers["blur.template_variables"];
			frm.fields_dict.body = createControl(frm, "body");
			api.bind_component_inputs(frm);
			api.bind_component_inputs(frm);
			assert.deepEqual(stale.$input.handlers, {});
			stale.$input.val("{{obsolete}}");
			oldBlur();
			await frm._template_component_edits.pending;
			assert.deepEqual(frm.doc.template_variables, []);
			assert.equal(frm.doc.body, "");
			await frm.setComponent("body", "{{current}}");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["current"]);
			frm.fields_dict.footer.$input[0].isConnected = false;
			frm.fields_dict.footer.$input.val("{{detached}}");
			await frm.blurComponent("footer");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["current"]);
			assert.equal(frm.doc.footer, "");
			console.log(JSON.stringify({ ok: true }));
			"""
		)
		self.assertTrue(result["ok"])

	def test_save_confirms_pending_controls_and_serializes_with_blur(self):
		"""Flush final DOM values before save without restoring manually deleted rows."""
		result = run_js_harness(
			"""
			const frm = createForm();
			frm.fields_dict.body.$input.val("Contrato {{numero_contrato}}.");
			assert.equal(frm.doc.body, "");
			await frm.triggerValidate();
			assert.equal(frm.doc.body, "Contrato {{numero_contrato}}.");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["numero_contrato"]);
			frm.removeParameterRow("numero_contrato");
			// Reverting an edit still flushes the model without restoring the row.
			frm.doc.body = "Contrato {{numero_con";
			await frm.triggerValidate();
			assert.equal(frm.doc.body, "Contrato {{numero_contrato}}.");
			await frm.blurComponent("body");
			assert.deepEqual(frm.doc.template_variables, []);
			frm.fields_dict.body.$input.val("Contrato {{numero_contrato}}, saldo {{monto}}.");
			const blur = frm.blurComponent("body");
			const save = frm.triggerValidate();
			await Promise.all([blur, save]);
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["numero_contrato", "monto"]);
			assert.equal(frm._refresh_calls, 2);
			frm._dirty_calls = 0;
			await frm.triggerValidate();
			await frm.blurComponent("body");
			assert.equal(frm._dirty_calls, 0);
			frm.fields_dict.footer.$input.val("{{incomplete");
			await frm.triggerValidate();
			assert.equal(frm.doc.footer, "{{incomplete");
			assert.equal(frm._template_validation.has_syntax_error, true);
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["numero_contrato", "monto"]);
			console.log(JSON.stringify({ ok: true }));
			"""
		)
		self.assertTrue(result["ok"])

	def test_automatic_variable_detection_and_idempotency(self):
		"""Confirm additive detection, appearance order, and idempotency after blur."""
		result = run_js_harness(
			"""
			// 1. New variable in Header
			let frm = createForm();
			await frm.setComponent("header", "Estimado {{nombre_abonado}}");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
			assert.equal(frm._dirty, true);
			assert.equal(frm._intro, null);

			// 2. New variable in Body
			frm = createForm({ header: "Aviso" });
			await frm.setComponent("body", "Su contrato {{numero_contrato}} está activo.");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["numero_contrato"]);
			assert.equal(frm._intro, null);

			// 3. New variable in Footer
			frm = createForm({ body: "Cuerpo fijo" });
			await frm.setComponent("footer", "Vence el {{fecha_suspension}}");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["fecha_suspension"]);
			assert.equal(frm._intro, null);

			// 4. Two new variables in Body
			frm = createForm();
			await frm.setComponent("body", "Contrato {{numero_contrato}} con saldo {{monto_pendiente}}.");
			assert.deepEqual(
				frm.doc.template_variables.map(r => r.parameter_name),
				["numero_contrato", "monto_pendiente"]
			);

			// 5. Previously declared variable & 8. Preservation of existing rows
			frm = createForm({
				template_variables: [{ parameter_name: "nombre_abonado" }],
			});
			await frm.setComponent("header", "Estimado {{nombre_abonado}}");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
			assert.equal(frm._dirty_calls, 0);

			// 7. Order of incorporation across Header, Body, Footer while preserving existing row order
			await frm.setComponent("footer", "Fecha {{fecha_suspension}}");
			assert.deepEqual(
				frm.doc.template_variables.map(r => r.parameter_name),
				["nombre_abonado", "fecha_suspension"]
			);
			await frm.setComponent("body", "Contrato {{numero_contrato}} y saldo {{monto_pendiente}}.");
			assert.deepEqual(
				frm.doc.template_variables.map(r => r.parameter_name),
				["nombre_abonado", "fecha_suspension", "numero_contrato", "monto_pendiente"]
			);

			// Fresh multi-component detection adds in Header -> Body -> Footer appearance order
			const orderedFrm = createForm({
				header: "Hola {{nombre_abonado}}",
				body: "Contrato {{numero_contrato}}, monto {{monto_pendiente}}",
				footer: "Suspensión {{fecha_suspension}}",
			});
			await orderedFrm.setComponent("body", orderedFrm.doc.body + ".");
			assert.deepEqual(
				orderedFrm.doc.template_variables.map(r => r.parameter_name),
				["nombre_abonado", "numero_contrato", "monto_pendiente", "fecha_suspension"]
			);

			// 6. Repeated executions without changing variables are strictly idempotent
			orderedFrm._dirty = false;
			orderedFrm._dirty_calls = 0;
			orderedFrm._refresh_calls = 0;
			await orderedFrm.setComponent("header", "Hola {{nombre_abonado}}");
			await orderedFrm.setComponent("body", "Contrato {{numero_contrato}}, monto {{monto_pendiente}}.");
			await orderedFrm.setComponent("footer", "Suspensión {{fecha_suspension}}");
			assert.deepEqual(
				orderedFrm.doc.template_variables.map(r => r.parameter_name),
				["nombre_abonado", "numero_contrato", "monto_pendiente", "fecha_suspension"]
			);
			assert.equal(orderedFrm._dirty, false);
			assert.equal(orderedFrm._dirty_calls, 0);
			assert.equal(orderedFrm._refresh_calls, 0);

			// 9. Template without variables
			const plainFrm = createForm();
			await plainFrm.setComponent("header", "Aviso informativo");
			await plainFrm.setComponent("body", "Su recibo mensual ya fue emitido.");
			await plainFrm.setComponent("footer", "Gracias por su preferencia.");
			assert.deepEqual(plainFrm.doc.template_variables, []);
			assert.equal(plainFrm._dirty_calls, 0);
			assert.equal(plainFrm._intro, null);

			console.log(JSON.stringify({ ok: true }));
			"""
		)
		self.assertTrue(result["ok"])

	def test_interactive_syntax_and_correspondence_validations(self):
		"""Keep syntax, correspondence, and renaming warnings after confirmed edits."""
		result = run_js_harness(
			"""
			// Invalid names (digit, uppercase, symbol, spaces, invalid underscores, reserved key)
			const invalidMarkers = [
				"{{nombre1}}",
				"{{NombreAbonado}}",
				"{{nombre-abonado}}",
				"{{nombre abonado}}",
				"{{_nombre}}",
				"{{nombre_}}",
				"{{nombre__abonado}}",
				"{{action}}",
			];
			for (const marker of invalidMarkers) {
				const frm = createForm({
					header: "Estimado {{nombre_abonado}}",
					template_variables: [{ parameter_name: "nombre_abonado" }],
				});
				await frm.setComponent("body", "Valor " + marker);
				assert.deepEqual(
					frm.doc.template_variables.map(r => r.parameter_name),
					["nombre_abonado"],
					"Must not add invalid marker: " + marker
				);
				assert.equal(frm._template_validation.valid, false);
				assert.equal(frm._template_validation.has_syntax_error, true);
				assert.ok(frm._intro && frm._intro.length > 0);
			}

			// Incomplete and malformed markers
			for (const badText of [
				"Hola {{nombre_abonado}",
				"Hola {{{nombre_abonado}}}",
				"Hola {{ nombre_abonado }}",
				"Hola {nombre_abonado}",
				"Hola {{}}",
				"Hola {% if x %}y{% endif %}",
			]) {
				const frm = createForm();
				await frm.setComponent("body", badText);
				assert.deepEqual(frm.doc.template_variables, []);
				assert.equal(frm._template_validation.valid, false);
				assert.equal(frm._template_validation.has_syntax_error, true);
				assert.match(frm._intro, /syntax|expressions/i);
			}

			// Subsequent correction of an error clears warning and adds variable
			const fixFrm = createForm();
			await fixFrm.setComponent("body", "Hola {{NombreAbonado}}");
			assert.deepEqual(fixFrm.doc.template_variables, []);
			assert.ok(fixFrm._intro);
			await fixFrm.setComponent("body", "Hola {{nombre_abonado}}");
			assert.deepEqual(fixFrm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
			assert.equal(fixFrm._template_validation.valid, true);
			assert.equal(fixFrm._intro, null);

			// Duplicate variable in Body
			const dupBodyFrm = createForm();
			await dupBodyFrm.setComponent("body", "Hola {{nombre_abonado}}, estimado {{nombre_abonado}}.");
			assert.deepEqual(dupBodyFrm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
			assert.equal(dupBodyFrm._template_validation.valid, false);
			assert.match(dupBodyFrm._intro, /more than once/);

			// Duplicate variable across Header and Body
			const dupCrossFrm = createForm();
			await dupCrossFrm.setComponent("header", "Hola {{nombre_abonado}}");
			await dupCrossFrm.setComponent("body", "Estimado {{nombre_abonado}}, su recibo está disponible.");
			assert.deepEqual(dupCrossFrm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
			assert.equal(dupCrossFrm._template_validation.valid, false);
			assert.match(dupCrossFrm._intro, /more than once/);

			// Two variables in Header & two variables in Footer
			const headerLimitFrm = createForm({ body: "Cuerpo fijo" });
			await headerLimitFrm.setComponent("header", "{{uno}} {{dos}}");
			assert.equal(headerLimitFrm._template_validation.valid, false);
			assert.match(headerLimitFrm._intro, /Header allows at most 1/);

			const footerLimitFrm = createForm({ body: "Cuerpo fijo" });
			await footerLimitFrm.setComponent("footer", "{{uno}} {{dos}}");
			assert.equal(footerLimitFrm._template_validation.valid, false);
			assert.match(footerLimitFrm._intro, /Footer allows at most 1/);

			// Renaming a variable (Section 4.4): adds new variable, preserves old variable, warns unused
			const renameFrm = createForm();
			await renameFrm.setComponent("body", "Hola {{nombre_abonado}}.");
			assert.deepEqual(renameFrm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);
			assert.equal(renameFrm._intro, null);
			await renameFrm.setComponent("body", "Hola {{cliente}}.");
			assert.deepEqual(
				renameFrm.doc.template_variables.map(r => r.parameter_name),
				["nombre_abonado", "cliente"]
			);
			assert.equal(renameFrm._template_validation.valid, false);
			assert.deepEqual(renameFrm._template_validation.unused, ["nombre_abonado"]);
			assert.match(renameFrm._intro, /not used.*nombre_abonado/);

			console.log(JSON.stringify({ ok: true }));
			"""
		)
		self.assertTrue(result["ok"])

	def test_manual_conservation_and_backend_authority_cases_a_to_e(self):
		"""Verify Section 6.3 Casos A, B, C, D, and E across form state and server persistence."""
		# Caso A: Add {{nombre_abonado}} -> row created automatically and saves on server
		state_a = run_js_harness(
			"""
			const frm = createForm({ body: "Hola, estimado abonado." });
			await frm.setComponent("body", "Hola {{nombre_abonado}}, bienvenido.");
			console.log(JSON.stringify({
				body: frm.doc.body,
				variables: frm.doc.template_variables.map(r => r.parameter_name),
				warnings: frm._template_validation.warnings,
			}));
			"""
		)
		self.assertEqual(state_a["variables"], ["nombre_abonado"])
		self.assertEqual(state_a["warnings"], [])

		tmpl = frappe.get_doc(
			dict(
				doctype='Communication Template', language='es',
				template_name="Auto var Caso A-E " + frappe.generate_hash(length=8),
				channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'),
				status='Inactive',
				header="",
				body=state_a["body"],
				footer="",
				template_variables=[dict(parameter_name=v) for v in state_a["variables"]],
			)
		).insert()
		self.templates.append(tmpl.name)
		frappe.db.commit()
		tmpl.reload()
		self.assertEqual([r.parameter_name for r in tmpl.template_variables], ["nombre_abonado"])

		# Caso B: Delete marker from content -> row remains in template_variables, warning displayed
		state_b = run_js_harness(
			"""
			const frm = createForm({
				body: "Hola {{nombre_abonado}}, bienvenido.",
				template_variables: [{ parameter_name: "nombre_abonado" }],
			});
			await frm.setComponent("body", "Hola, estimado abonado.");
			console.log(JSON.stringify({
				body: frm.doc.body,
				variables: frm.doc.template_variables.map(r => r.parameter_name),
				unused: frm._template_validation.unused,
				warnings: frm._template_validation.warnings,
			}));
			"""
		)
		self.assertEqual(state_b["variables"], ["nombre_abonado"])
		self.assertEqual(state_b["unused"], ["nombre_abonado"])
		self.assertEqual(len(state_b["warnings"]), 1)
		self.assertIn("not used", state_b["warnings"][0])

		# Caso C: Attempt to save with unused parameter -> backend rejects
		tmpl.body = state_b["body"]
		tmpl.set("template_variables", [dict(parameter_name=v) for v in state_b["variables"]])
		with self.assertRaisesRegex(frappe.ValidationError, "not used"):
			tmpl.save()
		frappe.db.rollback()

		# Caso D: Manually delete the unused row -> warning clears and document saves cleanly
		state_d = run_js_harness(
			"""
			const frm = createForm({
				body: "Hola, estimado abonado.",
				template_variables: [{ parameter_name: "nombre_abonado" }],
			});
			frm.removeParameterRow("nombre_abonado");
			console.log(JSON.stringify({
				body: frm.doc.body,
				variables: frm.doc.template_variables.map(r => r.parameter_name),
				warnings: frm._template_validation.warnings,
				intro: frm._intro,
			}));
			"""
		)
		self.assertEqual(state_d["variables"], [])
		self.assertEqual(state_d["warnings"], [])
		self.assertIsNone(state_d["intro"])

		tmpl.reload()
		tmpl.body = state_d["body"]
		tmpl.set("template_variables", [])
		tmpl.save()
		frappe.db.commit()
		tmpl.reload()
		self.assertEqual(len(tmpl.template_variables), 0)

		# Caso E: Manually delete a row while its marker still exists -> not recreated by refresh or validate/save; backend rejects save
		state_e = run_js_harness(
			"""
			const frm = createForm({ body: "Hola, estimado abonado." });
			await frm.setComponent("body", "Hola {{nombre_abonado}}, bienvenido.");
			assert.deepEqual(frm.doc.template_variables.map(r => r.parameter_name), ["nombre_abonado"]);

			// Administrator manually deletes the row while marker remains in body
			frm.removeParameterRow("nombre_abonado");
			assert.deepEqual(frm.doc.template_variables, []);
			assert.deepEqual(frm._template_validation.undeclared, ["nombre_abonado"]);

			// Refresh and validate must NOT recreate the manually deleted row
			await frm.triggerRefresh();
			await frm.triggerValidate();
			assert.deepEqual(frm.doc.template_variables, []);

			const beforeReedit = {
				body: frm.doc.body,
				variables: frm.doc.template_variables.map(r => r.parameter_name),
				undeclared: frm._template_validation.undeclared,
				warnings: frm._template_validation.warnings,
			};

			// Subsequent modification of Body detects and re-adds the marker normally
			await frm.setComponent("body", "Hola {{nombre_abonado}}, actualizado.");
			const afterReedit = {
				body: frm.doc.body,
				variables: frm.doc.template_variables.map(r => r.parameter_name),
				warnings: frm._template_validation.warnings,
			};
			console.log(JSON.stringify({ beforeReedit, afterReedit }));
			"""
		)
		self.assertEqual(state_e["beforeReedit"]["variables"], [])
		self.assertEqual(state_e["beforeReedit"]["undeclared"], ["nombre_abonado"])
		self.assertIn("undeclared", state_e["beforeReedit"]["warnings"][0])

		tmpl.reload()
		tmpl.body = state_e["beforeReedit"]["body"]
		tmpl.set("template_variables", [])
		with self.assertRaisesRegex(frappe.ValidationError, "undeclared"):
			tmpl.save()
		frappe.db.rollback()

		self.assertEqual(state_e["afterReedit"]["variables"], ["nombre_abonado"])
		self.assertEqual(state_e["afterReedit"]["warnings"], [])
		tmpl.reload()
		tmpl.body = state_e["afterReedit"]["body"]
		tmpl.set("template_variables", [dict(parameter_name=v) for v in state_e["afterReedit"]["variables"]])
		tmpl.save()
		frappe.db.commit()
		tmpl.reload()
		self.assertEqual([r.parameter_name for r in tmpl.template_variables], ["nombre_abonado"])


class TestGeneralTemplateJavascript(unittest.TestCase):
	"""Reuse the native blur/save harness without provider or account setup."""
	test_component_blur = TestCommunicationTemplate.test_component_blur_requires_real_edit_and_live_control
	test_pending_component_save = TestCommunicationTemplate.test_save_confirms_pending_controls_and_serializes_with_blur

	def test_duplicate_placeholders_preserve_single_declaration_and_warn(self):
		run_js_harness("""
const frm = createForm({header:'{{contract}}', body:'{{contract}}', footer:'{{contract}}',
 template_variables:[{parameter_name:'contract'}]});
const inspection = messaging.communication_template.inspect_template(frm.doc);
assert.equal(inspection.valid, false);
assert.deepEqual(inspection.unique_valid_variables, ['contract']);
assert(inspection.warnings.some(w => w.includes('more than once')));
console.log(JSON.stringify({passed:true}));
""")

"""Central template administration through real Process tests and Redis promotion."""
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import now_datetime

from messaging.communication.doctype.process_definition import validation as testing
from messaging.communication.doctype.notification_rule import notification_rule as templates
from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures
from messaging.registry.processes import register_processes

from messaging.registry.processes import NotifyNoResponse, NotifyProcess, NotifyRequest, NotifyResponse

def prepare_sync(*, params, outcomes):
    outcomes.register('send', fields={'recipient':'string','label':'string'}, mode=['template', 'message'])


def obtain_sync(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyResponse('send', data={'recipient':'', 'label':'Technical value'})


class TestTemplateRuleSync(unittest.TestCase):
    def setUp(self):
        ProcessFixtures.setUp(self)
        from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
        sync_communication_providers()
        register_processes(app='messaging', processes=[NotifyProcess(id='messaging.technical_template_sync',
            label='Technical template sync', definition=prepare_sync, handler=obtain_sync)])
    tearDown = ProcessFixtures.tearDown

    def fixtures(self):
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        set_provider_enabled('EMULR',True)
        if not frappe.db.exists('Communication Channel','emulator'):
            channel = frappe.get_doc(dict(doctype='Communication Channel',provider='EMULR',channel_name='Synthetic template channel')).insert()
            set_channel_status(channel.name, 'Enabled')
        contact = frappe.get_doc(dict(doctype='Emulator Contact', display_name='Synthetic sync context')).insert()
        account = frappe.get_doc(dict(doctype='Communication Channel Account', channel='emulator',
            account_id='template-sync-' + frappe.generate_hash(length=12), account_name='Synthetic sync account',
            enabled=0, parameters=[dict(parameter_key='transport_outcome', value='"Accepted"')])).insert()
        template = frappe.get_doc(dict(doctype='Communication Template', language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name='template-sync-' + frappe.generate_hash(length=12), body='Initial {{label}}', template_variables=[dict(parameter_name='label')])).insert()
        self.template_accounts = getattr(self, "template_accounts", {})
        self.template_accounts[template.name] = account.name
        return template, contact

    def rule(self, template):
        return frappe.get_doc(dict(doctype='Notification Rule', rule_name='Synthetic sync rule', enabled=0,
            source_application='messaging', source_doctype='Emulator Contact', events=[dict(document_event='after_insert')],
            process_definition='messaging.technical_template_sync', process_invocation_mode='Internal Call (frappe.call)',
            account=self.template_accounts[template.name], outputs=[dict(status_code='send',output_mode='Template',template_reference=template.name,send=1)],
            parameters=[dict(parameter_key='notification_contact', value=json.dumps(self.contact.name))])).insert()

    def approve(self, rule):
        answer = testing.test_method(rule.as_dict(), {'name': self.contact.name})
        self.assertEqual(answer['status'], 'Passed', answer)
        rule.save()
        rule.reload()
        self.assertEqual(testing.test_state(rule), 'Passed')
        return answer

    def setup_rules(self, count=1):
        template, self.contact = self.fixtures()
        return template, [self.rule(template) for _ in range(count)]

    def test_wording_edits_invalidate_approval_without_auto_testing_or_disabling(self):
        import inspect
        from messaging.communication.doctype.communication_template import communication_template as tpl_mod
        from messaging.communication.doctype.notification_rule import events
        from messaging.communication.doctype.notification_rule.notification_rule import (
            delivery_fingerprint,
            notification_destination,
            set_active as set_rule_active,
            state as rule_state,
            validate_process,
        )
        from messaging.communication.doctype.notification_queue.notification_queue import internal_write
        from messaging.communication.doctype.notification_queue.dispatch import revalidate
        from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log

        self.assertNotIn('messaging.communication.doctype.notification_rule', inspect.getsource(tpl_mod))
        self.assertFalse(hasattr(tpl_mod, 'template_contract_signature'))
        self.assertFalse(hasattr(templates, 'notification_rule_status'))
        self.assertFalse(hasattr(templates, 'revalidate_rules_for_template'))

        template, rules = self.setup_rules(2)
        for rule in rules:
            self.approve(rule)
        rules[0].enabled = 1
        rules[0].save()

        other, _ = self.fixtures()
        other_rule = self.rule(other)
        self.approve(other_rule)

        account = frappe.get_doc('Communication Channel Account', rules[0].account)
        destination = notification_destination(rules[0], account, '')
        log = record_process_execution_log(process_method=rules[0].process_method, source_type='Notification Rule',
            source_name=rules[0].name, execution_context='Administrative Test', execution_outcome='Functional Status', functional_status='prepared')
        scheduled = now_datetime() + timedelta(days=3)
        with internal_write():
            queue = frappe.get_doc(dict(doctype='Notification Queue', notification_rule=rules[0].name,
                source_application='messaging', source_doctype='Emulator Contact', source_name=self.contact.name,
                document_event='after_insert', occurrence='after_insert', occurrence_key=frappe.generate_hash(length=64),
                execution_log=log.name, account=account.name, channel=account.channel, provider='EMULR', account_id=account.account_id,
                recipient='', identity_key='synthetic-wording', content_type='Template', template_reference=template.name,
                variables_json=json.dumps({'label': 'Technical value'}), rendered_content='Initial Technical value',
                event_at=now_datetime(), scheduled_at=scheduled, next_attempt_at=scheduled, status='Pending',
                delivery_fingerprint=delivery_fingerprint(rules[0], destination), **destination)).insert()
        queue.reload()
        before_queue = queue.as_dict()
        before_revision = template.revision

        # External status noise alone must not alter template_revision or invalidate consumer approval.
        template.onload()
        self.assertNotIn('notification_rule_status', template.get_onload() or {})
        before_status_rev = template.revision
        template.provider_status = 'verified'
        self.assertEqual(tpl_mod.template_revision(template), before_status_rev)

        # Wording-only edits to body/header/footer change revision, leave Process logs untouched,
        # invalidate consumer approvals without disabling enabled rules, and preserve unrelated rules.
        log_count = frappe.db.count('Process Execution Log')
        for field, value in [('body', 'Updated body {{label}}'), ('header', 'Updated header'), ('footer', 'Updated footer')]:
            template.set(field, value)
            template.save()
        self.assertNotEqual(template.revision, before_revision)
        self.assertEqual(template.status, 'Active')
        self.assertEqual(frappe.db.count('Process Execution Log'), log_count)
        for rule in rules:
            rule.reload()
            self.assertEqual(testing.test_state(rule), 'Untested')
            with self.assertRaises(frappe.ValidationError):
                rule.require_approved()
        self.assertEqual([r.enabled for r in rules], [1, 0])
        self.assertEqual(rule_state(rules[0].name)['lifecycle_status'], 'Untested')
        self.assertEqual(testing.test_state(other_rule), 'Passed')

        # Backend runtime rejects obsolete approval even if the rule form was never opened.
        before_queue_count = frappe.db.count('Notification Queue', {'notification_rule': rules[0].name})
        with patch.object(events, 'rule_index', return_value={'Emulator Contact': {'after_insert': [rules[0].name]}}):
            events.observe(self.contact, 'after_insert')
        self.assertEqual(frappe.db.count('Notification Queue', {'notification_rule': rules[0].name}), before_queue_count)

        # Queued item is invalidated cleanly rather than silently delivered.
        queue.reload()
        self.assertEqual(before_queue, queue.as_dict())
        self.assertEqual(revalidate(queue), 'delivery_configuration_changed')

        # Rule cannot be activated via set_active while approval is obsolete; revalidating in Notification Rule restores approval.
        with self.assertRaisesRegex(frappe.ValidationError, 'Validate'):
            set_rule_active(dict(rules[1].as_dict()), 1)
        revalidated = validate_process(dict(rules[0].as_dict()))
        self.assertEqual(revalidated['status'], 'Passed')
        rules[0].reload()
        self.assertEqual(rules[0].enabled, 1)
        self.assertEqual(testing.test_state(rules[0]), 'Passed')

    def test_variables_parameters_deactivation_protection_and_queue_gate(self):
        from messaging.communication.doctype.communication_provider import communication_provider as cp
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        from messaging.communication.doctype.communication_template.communication_template import set_active
        from messaging.communication.doctype.notification_rule.notification_rule import (
            delivery_fingerprint,
            notification_destination,
            set_active as set_rule_active,
            validate_process,
        )
        from messaging.communication.doctype.notification_queue.notification_queue import internal_write
        from messaging.communication.doctype.notification_queue.dispatch import revalidate
        from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log

        template, rules = self.setup_rules(2)
        for rule in rules:
            self.approve(rule)
        rules[0].enabled = 1
        rules[0].save()

        # Removing variable updates revision and invalidates approval without auto-testing or changing enabled.
        template.body = 'Fixed body without variables'
        template.set('template_variables', [])
        template.save()
        for rule in rules:
            rule.reload()
            self.assertEqual(testing.test_state(rule), 'Untested')
        self.assertEqual([r.enabled for r in rules], [1, 0])
        self.assertEqual(validate_process(dict(rules[0].as_dict()))['status'], 'Passed')
        self.approve(rules[1])
        for rule in rules:
            rule.reload()

        # Adding an unknown variable saves cleanly on Communication Template and fails exclusively inside Notification Rule.
        template.body = 'Unknown {{unknown}}'
        template.set('template_variables', [dict(parameter_name='unknown')])
        template.save()
        for rule in rules:
            rule.reload()
            self.assertEqual(testing.test_state(rule), 'Untested')
            with self.assertRaises(frappe.ValidationError):
                rule.require_approved()
        self.assertEqual([r.enabled for r in rules], [1, 0])
        with self.assertRaisesRegex(frappe.ValidationError, 'unknown'):
            validate_process(dict(rules[0].as_dict()))
        with self.assertRaisesRegex(frappe.ValidationError, 'unknown'):
            rules[0].save()
        rules[0].reload()

        # Restoring compatible variable allows manual Notification Rule revalidation; obsolete approval blocks queue delivery even with matching fingerprint.
        template.body = 'Restored {{label}}'
        template.set('template_variables', [dict(parameter_name='label')])
        template.save()
        self.assertEqual(validate_process(dict(rules[0].as_dict()))['status'], 'Passed')
        self.approve(rules[1])
        rules[0].reload(); rules[1].reload()

        account = frappe.get_doc('Communication Channel Account', rules[0].account)
        destination = notification_destination(rules[0], account, '')
        log = record_process_execution_log(process_method=rules[0].process_method, source_type='Notification Rule',
            source_name=rules[0].name, execution_context='Administrative Test', execution_outcome='Functional Status', functional_status='prepared')
        scheduled = now_datetime() + timedelta(days=3)
        with internal_write():
            queue = frappe.get_doc(dict(doctype='Notification Queue', notification_rule=rules[0].name,
                source_application='messaging', source_doctype='Emulator Contact', source_name=self.contact.name,
                document_event='after_insert', occurrence='after_insert', occurrence_key=frappe.generate_hash(length=64),
                execution_log=log.name, account=account.name, channel=account.channel, provider='EMULR', account_id=account.account_id,
                recipient='', identity_key='synthetic-obsolete-rule', content_type='Template', template_reference=template.name,
                variables_json=json.dumps({'label': 'Technical value'}), rendered_content='Restored Technical value',
                event_at=now_datetime(), scheduled_at=scheduled, next_attempt_at=scheduled, status='Pending',
                delivery_fingerprint=delivery_fingerprint(rules[0], destination), **destination)).insert()
        frappe.db.set_value('Notification Rule', rules[0].name, 'process_test_status', 'Untested', update_modified=False)
        self.assertEqual(revalidate(queue), 'rule_approval_obsolete')
        self.assertEqual(validate_process(dict(rules[0].as_dict()))['status'], 'Passed')
        rules[0].reload()

        # Active rule deactivation protection:
        # 1) Obsolete-approval active rule still blocks deactivation.
        template.body = 'Updated wording {{label}}'
        template.save()
        rules[0].reload()
        self.assertEqual(rules[0].enabled, 1)
        self.assertEqual(testing.test_state(rules[0]), 'Untested')
        with self.assertRaisesRegex(frappe.ValidationError, rules[0].name):
            set_active(dict(name=template.name, modified=str(template.modified)), 0)

        # 2) Two active rules -> error identifies BOTH; disabling one -> still blocked identifying the remaining active rule;
        # disabling the second -> deactivation succeeds; disabled rule cannot activate against Inactive template.
        self.assertEqual(validate_process(dict(rules[0].as_dict()))['status'], 'Passed')
        self.assertEqual(validate_process(dict(rules[1].as_dict()))['status'], 'Passed')
        rules[0].reload(); rules[1].reload()
        with self.assertRaises(frappe.ValidationError) as two_err:
            set_active(dict(name=template.name, modified=str(template.modified)), 0)
        self.assertIn(rules[0].name, str(two_err.exception))
        self.assertIn(rules[1].name, str(two_err.exception))

        set_rule_active(dict(rules[0].as_dict()), 0)
        rules[0].reload()
        with self.assertRaises(frappe.ValidationError) as one_err:
            set_active(dict(name=template.name, modified=str(template.modified)), 0)
        self.assertNotIn(rules[0].name, str(one_err.exception))
        self.assertIn(rules[1].name, str(one_err.exception))

        # Removing the last sendable output on an enabled rule is rejected; disabling it first unblocks deactivation.
        rules[1].outputs[0].send = 0
        rules[1].outputs[0].template_reference = None
        with self.assertRaises(frappe.ValidationError):
            rules[1].save()
        rules[1].reload()
        set_rule_active(dict(rules[1].as_dict()), 0)
        rules[1].reload()
        rules[1].outputs[0].send = 0
        rules[1].outputs[0].template_reference = None
        rules[1].save()
        res = set_active(dict(name=template.name, modified=str(template.modified)), 0)
        self.assertEqual(res['status'], 'Inactive')
        template.reload()
        self.assertEqual(template.status, 'Inactive')

        # Disabled rule referencing an Inactive template cannot activate or pass Notification Rule validation.
        rules[0].reload()
        self.assertEqual(testing.test_state(rules[0]), 'Untested')
        with self.assertRaisesRegex(frappe.ValidationError, 'active'):
            validate_process(dict(rules[0].as_dict()))

        # Configurable provider parameter edit changes template_revision and invalidates rule approval.
        set_active(dict(name=template.name, modified=str(template.modified)), 1)
        template.reload()
        self.assertEqual(template.status, 'Active')

        from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers
        from messaging.communication.doctype.communication_provider.test_communication_provider import (
            PROBE,
            declaration,
            parameter,
            sync_communication_providers,
        )
        from messaging.registry import _installed_declarations
        custom_decl = declaration({
            **PROBE,
            'id': 'TSTTMPL',
            'name': 'Test Template Provider',
            'capabilities': ['text', 'templates'],
            'operations': {
                **PROBE['operations'],
                'validate_template': 'messaging.communication.doctype.communication_provider.test_communication_provider.validate_template',
                'validate_notification': cp.provider_definition('EMULR')['operations']['validate_notification'],
            },
            'parameters': [parameter('tone', 'Select', scope='Template', options='formal\ncasual', default_value='formal', required=True)],
        })

        with registered_providers(_installed_declarations('messaging', 'communication_providers') + (custom_decl,)):
            sync_communication_providers()
            set_provider_enabled('TSTTMPL', True)
            ch = frappe.get_doc(dict(doctype='Communication Channel', provider='TSTTMPL', channel_name='Custom param ch')).insert()
            set_channel_status(ch.name, 'Enabled')
            acc = frappe.get_doc(dict(doctype='Communication Channel Account', channel=ch.name, account_id='acc-' + frappe.generate_hash(length=8), account_name='Custom param acc', enabled=0)).insert()
            tpl = frappe.get_doc(dict(doctype='Communication Template', language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTTMPL'}, 'name'), status='Active',
                template_name='param-sync-' + frappe.generate_hash(length=8), body='Param {{label}}',
                template_variables=[dict(parameter_name='label')],
                parameters=[dict(parameter_key='tone', value='"formal"')])).insert()
            p_rule = frappe.get_doc(dict(doctype='Notification Rule', rule_name='Param rule', enabled=0,
                source_application='messaging', source_doctype='Emulator Contact', events=[dict(document_event='after_insert')],
                process_definition='messaging.technical_template_sync', process_invocation_mode='Internal Call (frappe.call)',
                account=acc.name, outputs=[dict(status_code='send', output_mode='Template', template_reference=tpl.name, send=1)])).insert()
            self.assertEqual(validate_process(dict(p_rule.as_dict()))['status'], 'Passed')
            p_rule.reload()
            self.assertEqual(p_rule.enabled, 1)
            self.assertEqual(testing.test_state(p_rule), 'Passed')
            prev_rev = tpl.revision
            tpl.parameters[0].value = '"casual"'
            tpl.save()
            self.assertNotEqual(tpl.revision, prev_rev)
            p_rule.reload()
            self.assertEqual(p_rule.enabled, 1)
            self.assertEqual(testing.test_state(p_rule), 'Untested')

class TestConcurrentTemplateRuleLocking(unittest.TestCase):
    def setUp(self):
        frappe.set_user('Administrator')
        self.created = []
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
        sync_communication_providers()
        set_provider_enabled('EMULR', True)
        register_processes(app='messaging', processes=[NotifyProcess(id='messaging.technical_template_sync',
            label='Technical template sync', definition=prepare_sync, handler=obtain_sync)])
        if not frappe.db.exists('Communication Channel', 'emulator'):
            channel = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR', channel_name='Emulator')).insert()
            set_channel_status(channel.name, 'Enabled')
            self.created.append(('Communication Channel', 'emulator'))
        contact = frappe.get_doc(dict(doctype='Emulator Contact', display_name='Concurrent sync context ' + frappe.generate_hash(length=6))).insert()
        self.created.append(('Emulator Contact', contact.name))
        account = frappe.get_doc(dict(doctype='Communication Channel Account', channel='emulator',
            account_id='concurrent-sync-' + frappe.generate_hash(length=10), account_name='Concurrent sync acc',
            enabled=0, parameters=[dict(parameter_key='transport_outcome', value='"Accepted"')])).insert()
        self.created.append(('Communication Channel Account', account.name))
        from messaging.chatbot.doctype.bot_settings.language import get_bot_settings
        settings = get_bot_settings()
        enabled_languages = [r.language for r in settings.chatbot_languages if r.enabled] or ['es']
        default_language = next(
            (r.language for r in settings.chatbot_languages if r.enabled and r.is_default),
            enabled_languages[0],
        )
        template_name = 'concurrent-sync-' + frappe.generate_hash(length=10)
        variants = {}
        for lang in enabled_languages:
            variant = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
                template_name=template_name, language=lang, body='Initial {{label}}',
                template_variables=[dict(parameter_name='label')])).insert()
            self.created.append(('Communication Template', variant.name))
            variants[lang] = variant
        template = variants[default_language]
        rule = frappe.get_doc(dict(doctype='Notification Rule', rule_name='Concurrent sync rule', enabled=0,
            source_application='messaging', source_doctype='Emulator Contact', events=[dict(document_event='after_insert')],
            process_definition='messaging.technical_template_sync', process_invocation_mode='Internal Call (frappe.call)',
            account=account.name, outputs=[dict(status_code='send', output_mode='Template', template_reference=template.name, send=1)],
            parameters=[dict(parameter_key='notification_contact', value=json.dumps(contact.name))])).insert()
        self.created.append(('Notification Rule', rule.name))
        answer = testing.test_method(rule.as_dict(), {'name': contact.name})
        self.assertEqual(answer['status'], 'Passed', answer)
        rule.save()
        rule.reload()
        self.template = template
        self.rule = rule
        frappe.db.commit()

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user('Administrator')
        rule_name = self.rule.name
        if frappe.db.exists('Notification Rule', rule_name):
            if frappe.db.get_value('Notification Rule', rule_name, 'enabled'):
                from messaging.communication.doctype.notification_rule.notification_rule import set_active as set_rule_active
                rule_doc = frappe.get_doc('Notification Rule', rule_name)
                set_rule_active(dict(rule_doc.as_dict()), 0)
            frappe.db.delete('Process Execution Log', {'source_type': 'Notification Rule', 'source_name': rule_name})
        for doctype, name in reversed(self.created):
            if frappe.db.exists(doctype, name):
                frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
        if frappe.db.exists('Process Definition', 'messaging.technical_template_sync'):
            from messaging.communication.doctype.process_definition.process_definition import _registry_write
            with _registry_write():
                frappe.delete_doc('Process Definition', 'messaging.technical_template_sync', ignore_permissions=True, force=True)
        frappe.db.commit()

    def test_concurrent_deactivation_and_rule_activation_serialization(self):
        import threading
        from messaging.communication.doctype.communication_template.communication_template import set_active as set_template_active
        from messaging.communication.doctype.notification_rule.notification_rule import set_active as set_rule_active

        template, rule = self.template, self.rule
        self.assertEqual(rule.enabled, 0)
        self.assertEqual(template.status, 'Active')

        site = frappe.local.site
        barrier = threading.Barrier(2)
        outcomes = {}

        def worker(key, fn):
            frappe.init(site=site)
            frappe.connect(site=site)
            try:
                frappe.set_user('Administrator')
                frappe.db.rollback()
                barrier.wait(timeout=10)
                outcomes[key] = ('ok', fn())
                frappe.db.commit()
            except Exception as err:
                frappe.db.rollback()
                outcomes[key] = ('err', type(err).__name__, str(err))
            finally:
                frappe.destroy()

        t_info = dict(name=template.name, modified=str(template.modified))
        r_info = dict(rule.as_dict())
        t1 = threading.Thread(target=worker, args=('deactivate_template', lambda: set_template_active(t_info, 0)))
        t2 = threading.Thread(target=worker, args=('activate_rule', lambda: set_rule_active(r_info, 1)))
        t1.start(); t2.start()
        t1.join(timeout=20); t2.join(timeout=20)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        # Refresh current connection snapshot after concurrent worker commits.
        frappe.db.rollback()
        template.reload()
        rule.reload()
        self.assertFalse(rule.enabled == 1 and template.status == 'Inactive',
            dict(outcomes=outcomes, rule_enabled=rule.enabled, template_status=template.status))
        self.assertEqual(sum(1 for o in outcomes.values() if o[0] == 'ok'), 1, outcomes)
        self.assertEqual(sum(1 for o in outcomes.values() if o[0] == 'err'), 1, outcomes)
        if outcomes['activate_rule'][0] == 'ok':
            self.assertIn(rule.name, outcomes['deactivate_template'][2], outcomes)
        else:
            self.assertIn('active', outcomes['activate_rule'][2].lower(), outcomes)
        for outcome in outcomes.values():
            if outcome[0] == 'err':
                self.assertNotIn('Deadlock', outcome[2], outcome)
                self.assertEqual(outcome[1], 'ValidationError', outcome)


class TestTemplateRuleSyncJavascript(unittest.TestCase):
    def test_manual_rule_sync_controls_removed_and_lifecycle_and_provider_capabilities_rendered(self):
        run_js_harness('''
const source = fs.readFileSync(''' + json.dumps(str(JS_PATH)) + ''', 'utf8');
assert(!source.includes('Synchronize rules'));
assert(!source.includes('Rule revalidation required'));
assert(!source.includes('synchronize_rule'));
assert(!source.includes('notification_rule_status'));
assert(!source.includes('Incompatible notification rules'));

let actionCalls = [];
frappe.user = { has_role: () => true };
frappe.msgprint = () => {};
frappe.utils.get_form_link = (dt, name) => '/desk/notification-rule/' + name;

// 1. New form unlocks identity, hides external status, hides lifecycle button.
const newFrm = createForm({ __islocal: 1, provider: 'EMULR', status: 'Inactive' });
await newFrm.triggerRefresh();
assert.equal(newFrm._df_props.template_name.read_only, false);
assert.equal(newFrm._df_props.channel.read_only, false);
assert.equal(newFrm._df_props.language.read_only, false);
assert.equal(newFrm._df_props.status.read_only, 1);
assert.equal(newFrm._display.external_status_section, false);
assert.equal(newFrm._page_buttons.length, 0);
assert.equal(newFrm._indicator, null);

// 2. Existing EMULR Inactive form locks identity, shows Activate, hides external sync and shows Not applicable.
const emulrFrm = createForm({
	provider: 'EMULR',
	status: 'Inactive',
	provider_status: 'not_synchronized',
	__onload: {
		template_status_supported: false,
	},
});
await emulrFrm.triggerRefresh();
assert.equal(emulrFrm._df_props.template_name.read_only, true);
assert.equal(emulrFrm._df_props.channel.read_only, true);
assert.equal(emulrFrm._df_props.language.read_only, true);
assert.equal(emulrFrm._df_props.status.read_only, 1);
assert.equal(emulrFrm._indicator.label, 'Inactive');
assert.equal(emulrFrm._page_buttons.map(b => b.label).join(','), 'Activate');
assert.equal(emulrFrm._display.external_status_section, false);
assert.equal(emulrFrm._custom_buttons['Synchronize status'], undefined);
assert.equal(emulrFrm.fields_dict.provider_status.$input.val(), 'Not applicable');
assert.equal(emulrFrm._intro, null);

// 3. Existing provider with template_status shows Synchronize status, external section, and external error only (no Notification Rule alerts).
const extFrm = createForm({
	provider: 'WA',
	status: 'Active',
	provider_status: 'not_synchronized',
	statuses: [{ scope_key: 'ACC-1', last_error: 'Rejected remotely' }],
	__onload: {
		template_status_supported: true,
		notification_rule_status: {
			rules: [{ name: 'rule-bad', compatible: false, state: 'Failed', error: 'Missing required variable' }],
		},
	},
});
await extFrm.triggerRefresh();
assert.equal(extFrm._indicator.label, 'Active');
assert.equal(extFrm._page_buttons.map(b => b.label).join(','), 'Deactivate');
assert.equal(extFrm._display.external_status_section, true);
assert.equal(typeof extFrm._custom_buttons['Synchronize status'], 'function');
assert.equal(extFrm.fields_dict.provider_status.$input.val(), 'Unverified');
assert(!extFrm._intro.includes('Incompatible notification rules'));
assert(!extFrm._intro.includes('Missing required variable'));
assert(extFrm._intro.includes('Rejected remotely'));

// 4. Double-click on Activate/Deactivate triggers only one POST set_active call.
frappe.call = async (args) => { actionCalls.push(args); return { message: { status: 'Active' } }; };
await Promise.all([
	messaging.communication_template.toggle_template_active(emulrFrm, 1),
	messaging.communication_template.toggle_template_active(emulrFrm, 1),
]);
assert.equal(actionCalls.length, 1);
assert.equal(actionCalls[0].method, 'messaging.communication.doctype.communication_template.communication_template.set_active');
assert.equal(actionCalls[0].type, 'POST');
assert.equal(actionCalls[0].args.active, 1);
console.log(JSON.stringify({ passed: true }));
''')


class TestTemplateUsage(ProcessFixtures, unittest.TestCase):
    def test_usage_account_language_variables_and_existing_content(self):
        from messaging.communication.doctype.communication_template.communication_template import resolve_output, set_active
        rule, source = self.notification()
        provider = frappe.db.get_value('Communication Channel', frappe.db.get_value('Communication Channel Account', rule.account, 'channel'), 'provider')
        flow_template = frappe.get_doc(dict(doctype='Bot Flow Template', template_name=self.prefix + ' flow',
            language='es', enabled=1, body='Fixed text.')).insert()
        self.assertFalse(frappe.get_meta('Bot Flow Template').get_field('provider'))
        self.assertEqual(resolve_output('Template', template_reference=flow_template.name, data={})['text'], 'Fixed text.')
        with self.assertRaisesRegex(frappe.ValidationError, 'Communication Channel'):
            frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix + ' providerless',
                language='es', status='Inactive', body='Fixed text.')).insert()
        notice = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix + ' notice',
            channel=frappe.db.get_value('Communication Channel', {'provider': provider}, 'name'), language='es', status='Inactive', body='Fixed text.')).insert()
        self.assertEqual(notice.status, 'Inactive')
        with self.assertRaises(frappe.ValidationError):
            resolve_output('Template', template_reference=notice.name, data={}, usage='Notification', account=rule.account)
        notice.status = 'Active'
        with self.assertRaisesRegex(frappe.ValidationError, 'Activate or Deactivate'):
            notice.save()
        notice.reload()
        set_active(dict(name=notice.name, modified=str(notice.modified)), 1)
        notice.reload()
        self.assertEqual(notice.status, 'Active')
        self.assertEqual(resolve_output('Template', template_reference=notice.name, data={}, usage='Notification', account=rule.account)['text'], 'Fixed text.')
        self.assertEqual(notice.body, 'Fixed text.')
        notice.body = 'Value {{value}}'
        notice.append('template_variables', dict(parameter_name='value'))
        notice.save()
        self.assertEqual(notice.status, 'Active')
        self.assertEqual(resolve_output('Template', template_reference=notice.name, data={'value': 'Public'}, usage='Notification', account=rule.account)['text'], 'Value Public')
        self.assertEqual(frappe.db.count('Bot Flow Template', {'name': flow_template.name}), 1)
        self.assertEqual(frappe.db.count('Communication Template', {'name': notice.name}), 1)

    def test_legacy_external_capability_requires_a_published_contract(self):
        from messaging.communication.doctype.communication_template import external
        from messaging.communication.doctype.communication_template.communication_template import resolve_output, validate_template_fields
        rule, _source = self.notification()
        channel = frappe.db.get_value('Communication Channel Account', rule.account, 'channel')
        template = frappe.get_doc(dict(doctype='Communication Template', channel=channel, status='Active',
            template_name=self.prefix, language='es', body='Value {{value}}',
            template_variables=[dict(parameter_name='value')])).insert()
        definition = external.provider_definition(template.provider)
        legacy = dict(definition, capabilities=[*definition['capabilities'], 'provider_templates'])
        with patch.object(external, 'provider_definition', return_value=legacy):
            with self.assertRaises(frappe.ValidationError):
                validate_template_fields(template.name, {'value': 'string'}, usage='Notification', account=rule.account)
            with self.assertRaises(frappe.ValidationError):
                resolve_output('Template', template_reference=template.name, data={'value': 'Public'},
                    usage='Notification', account=rule.account)

    def test_notification_template_uses_selected_language_and_provider_mode(self):
        from messaging.communication.doctype.communication_template.communication_template import resolve_output
        from messaging.communication.doctype.notification_rule.notification_rule import content_types
        rule,source=self.notification()
        settings=frappe.get_doc('Bot Settings');settings.append('chatbot_languages',dict(language='en',enabled=1,is_default=0));settings.save()
        provider=frappe.db.get_value('Communication Channel', frappe.db.get_value('Communication Channel Account', rule.account, 'channel'), 'provider')
        template=frappe.get_doc(dict(doctype='Communication Template',channel=frappe.db.get_value('Communication Channel', {'provider': provider}, 'name'),status='Active',
            template_name=self.prefix,language='en',body='English notification.')).insert()
        self.assertEqual(resolve_output('Template',template_reference=template.name,data={},usage='Notification',account=rule.account)['text'], 'English notification.')
        self.assertEqual(content_types(dict(capabilities=['provider_templates', 'notification_template_required'],
            operations={'validate_notification': True, 'prepare_provider_template': True},
            template_statuses={'Approved': {'can_send': True}})), ['Template'])


def resolve_dynamic_template(payload):
    from messaging.communication.doctype.communication_provider_value.communication_provider_value import transport_parameters
    values=transport_parameters('TSTPV',payload['account'],template=payload['reference'])['Template']
    return dict(reference=payload['reference'],account=payload['account'],language=payload['language'],
        revision=values['remote_name'],available=True,approved=True,variables=[])


def prepare_dynamic_template(payload):
    return dict(reference=payload['reference'],variables=payload.get('variables',{}))


class TestTemplateProviderParameters(ProcessFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from messaging.communication.doctype.communication_provider.test_communication_provider import TestCommunicationProvider, parameter
        from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        TestCommunicationProvider.setUp(self)
        self.channel=TestCommunicationProvider.channel.__get__(self)
        self.account=TestCommunicationProvider.account.__get__(self)
        self.declarations[0]['capabilities'].extend(['templates','provider_templates'])
        self.declarations[0]['operations'].update(validate_template='messaging.communication.doctype.communication_provider.test_communication_provider.validate_template',
            resolve_provider_template=__name__+'.resolve_dynamic_template',prepare_provider_template=__name__+'.prepare_dynamic_template')
        self.declarations[0]['parameters'].extend([
            parameter('remote_name',scope='Template',required=True),
            parameter('remote_category','Select',scope='Template',options='notice\nalert'),
            parameter('template_secret','Password',scope='Template'),
            parameter('template_count','Int',scope='Template'),
            parameter('template_ratio','Float',scope='Template'),
            parameter('template_flag','Check',scope='Template',default_value='0'),
            parameter('template_language','Link',scope='Template',options='Language')])
        sync_communication_providers();set_provider_enabled('TSTPV',True);set_provider_enabled('TSTOT',True)
        self.binding=self.account(self.channel(enabled=1),enabled=1)

    def template(self, *, enabled=1, values=True):
        provider=frappe.db.get_value('Communication Channel', self.binding.channel, 'provider')
        doc=frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': provider}, 'name'),
            status='Active' if enabled else 'Inactive', template_name=self.prefix, language='es', body='Fixed provider content.'))
        if values:
            for key,value in dict(remote_name='remote-synthetic',remote_category='notice',template_count=2,
                template_ratio=1.5,template_flag=True,template_language='es').items():
                doc.append('parameters',dict(parameter_key=key,value=json.dumps(value)))
            doc.append('parameters',dict(parameter_key='template_secret',secret_value='synthetic-template-secret'))
        return doc

    def test_dynamic_types_persist_edit_encrypt_and_reach_registered_adapter(self):
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema,transport_parameters
        from messaging.communication.doctype.communication_provider.communication_provider import operation, provider_definition
        from messaging.security.execution_context import _automation_context
        schema=parameter_schema('Communication Template', channel=self.binding.channel)
        self.assertEqual({p['parameter_key'] for p in schema['parameters']},
            {'remote_name','remote_category','template_secret','template_count','template_ratio','template_flag','template_language'})
        self.assertNotIn('app_id',{p['parameter_key'] for p in schema['parameters']})
        template=self.template().insert();template.reload()
        rows={row.parameter_key:row for row in template.parameters}
        self.assertEqual(json.loads(rows['template_count'].value),2)
        self.assertEqual(rows['template_secret'].secret_value,'********')
        self.assertIsNone(rows['template_secret'].value)
        self.assertNotIn('synthetic-template-secret',template.as_json())
        self.assertEqual(rows['template_secret'].get_password('secret_value'),'synthetic-template-secret')
        initial=template.revision
        rows['remote_name'].value='"updated-remote"';template.save();template.reload()
        self.assertNotEqual(template.revision,initial)
        with _automation_context():
            descriptor = operation(provider_definition('TSTPV'), 'resolve_provider_template',
                dict(account=self.binding.name, reference=template.name, language=template.language))
            self.assertEqual(descriptor['revision'], 'updated-remote')
            with self.assertRaises(frappe.PermissionError):transport_parameters('TSTPV',self.binding.name,template=template.name)
        self.network_mock.assert_not_called()

    def test_required_types_options_and_immutable_identity(self):
        from messaging.communication.doctype.communication_template.communication_template import set_active
        template=self.template(values=False)
        with self.assertRaises(frappe.ValidationError):template.insert()
        template=self.template(enabled=0,values=False).insert()
        self.assertEqual(template.status,'Inactive')
        self.assertEqual(json.loads(template.parameters[0].value),False)
        with self.assertRaises(frappe.ValidationError):
            set_active(dict(name=template.name, modified=str(template.modified)), 1)
        template.reload();template.append('parameters',dict(parameter_key='remote_name',value='"configured"'))
        template.save();template.reload()
        set_active(dict(name=template.name, modified=str(template.modified)), 1)
        template.reload()
        self.assertEqual(template.status,'Active')
        template.append('parameters',dict(parameter_key='remote_category',value='"not_allowed"'))
        with self.assertRaises(frappe.ValidationError):template.save()
        template.reload();template.append('parameters',dict(parameter_key='template_count',value='true'))
        with self.assertRaises(frappe.ValidationError):template.save()
        template.reload()
        self.account(self.channel('TSTOT',enabled=1,parameters=[]),account_id=self.prefix+'_other',enabled=1)
        template.channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTOT'}, 'name')
        with self.assertRaisesRegex(frappe.ValidationError,'immutable'):template.save()
        template.reload();template.channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTOT'}, 'name');template.set('parameters',[])
        with self.assertRaisesRegex(frappe.ValidationError,'immutable'):template.save()
        template.reload()
        with self.assertRaises(frappe.ValidationError):template.db_set('channel','other')
        with self.assertRaises(frappe.ValidationError):template.db_set('status','Inactive')
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema
        self.assertEqual(parameter_schema('Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTOT'}, 'name'))['parameters'],[])

    def test_bot_flow_no_provider_and_fixed_metadata_absent(self):
        template=frappe.get_doc(dict(doctype='Bot Flow Template',template_name=self.prefix,
            language='es',enabled=1,body='Conversational content.')).insert()
        self.assertFalse(frappe.get_meta('Bot Flow Template').get_field('provider'))
        self.assertFalse(frappe.get_meta('Bot Flow Template').get_field('status'))
        fields={f.fieldname for f in frappe.get_meta('Communication Template').fields}
        self.assertFalse({'external_reference','category','approval_status','usage_type','enabled','provider'} & fields)
        self.assertIn('account', fields)
        self.assertEqual(frappe.get_meta('Communication Template').get_field('parameters').options,'Communication Provider Value')

    def test_same_provider_account_and_template_ownership_are_exact(self):
        from messaging.communication.doctype.communication_provider.communication_provider import operation, provider_definition, used_keys
        from messaging.security.execution_context import _automation_context
        template=self.template().insert()
        self.assertIn('remote_name',used_keys('TSTPV'))
        other=self.account(frappe.get_doc('Communication Channel',self.binding.channel),account_id='other-synthetic',enabled=1)
        with _automation_context():
            same_provider = operation(provider_definition('TSTPV'), 'resolve_provider_template',
                dict(account=other.name, reference=template.name, language=template.language))
        self.assertEqual(same_provider['account'], other.name)
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        set_provider_enabled('TSTOT', True)
        foreign = self.account(self.channel('TSTOT', enabled=1, parameters=[]), account_id=self.prefix + '-foreign', enabled=1)
        with _automation_context():
            with self.assertRaises(frappe.ValidationError):
                operation(provider_definition('TSTPV'), 'resolve_provider_template',
                    dict(account=foreign.name, reference=template.name, language=template.language))
        copy=frappe.copy_doc(template);copy.template_name=self.prefix+'-copy'
        copy.parameters[0].name=template.parameters[0].name
        copy.name=self.prefix+'-copy';copy.set_parent_in_children()
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import validate_values
        with self.assertRaises(frappe.PermissionError):validate_values(copy)

    def test_provider_without_template_parameters_stores_no_extra_values(self):
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema
        from messaging.communication.doctype.communication_template.communication_template import resolve_output
        other=self.account(self.channel('TSTOT',enabled=1,parameters=[]),account_id=self.prefix+'_other',enabled=1)
        template=frappe.get_doc(dict(doctype='Communication Template',channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTOT'}, 'name'),status='Active',
            template_name=self.prefix,language='es',body='Provider-neutral notification.')).insert()
        self.assertFalse(template.parameters)
        self.assertEqual(parameter_schema('Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTOT'}, 'name'))['parameters'],[])
        self.assertEqual(resolve_output('Template',template_reference=template.name,account=other.name,usage='Notification',data={})['text'],template.body)


from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures


class TestTemplateIdentity(ProcessFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        settings = frappe.get_doc('Bot Settings')
        settings.append('chatbot_languages', dict(language='en', enabled=1, is_default=0))
        settings.save()

    def template(self, label, language='es', usage='Bot Flow', **values):
        if usage == 'Bot Flow':
            return frappe.get_doc(dict(doctype='Bot Flow Template', template_name=label, language=language,
                enabled=1, body='Synthetic content', **values))
        return frappe.get_doc(dict(doctype='Communication Template', template_name=label, channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), language=language,
            status='Inactive', body='Synthetic content', **values))

    def test_normalized_identity_and_multilingual_descriptive_name(self):
        from messaging.communication.doctype.communication_template.communication_template import template_identity
        label = self.prefix + ' Áccénts -- Greeting!'
        es = self.template(label).insert()
        en = self.template(label, language='en').insert()
        self.assertEqual(es.name, self.prefix + '_accents_greeting_es')
        self.assertEqual(en.name, self.prefix + '_accents_greeting_en')
        self.assertEqual(template_identity('Balance Query', 'es'), 'balance_query_es')
        self.assertEqual(template_identity('Balance Query', 'en'), 'balance_query_en')
        with self.assertRaises(frappe.ValidationError):
            template_identity('---', 'es')
        with self.assertRaises(frappe.ValidationError):
            template_identity('Valid', 'not-a-language')

    def test_collision_across_normalization_and_usage(self):
        label = self.prefix + ' Initial Greeting'
        self.template(label).insert()
        with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
            self.template(self.prefix + '-initial-greeting').insert()
        notice = self.template(label, usage='Notification').insert()
        self.assertEqual(notice.status, 'Inactive')
        self.assertNotEqual(notice.doctype, 'Bot Flow Template')

    def test_existing_identity_references_and_language_are_preserved(self):
        doc = self.template(self.prefix + ' Legacy Label').insert(set_name=self.prefix + '_legacy')
        flow = self.flow()
        flow.outputs[0].output_mode = 'Template'
        flow.append('output_translations', dict(language='es', status_code='success', template_reference=doc.name))
        flow.insert()
        old_name = doc.name
        doc.template_name = self.prefix + ' Edited Label'
        doc.save()
        self.assertEqual(doc.name, old_name)
        flow.reload()
        self.assertEqual(flow.output_translations[0].template_reference, old_name)
        with self.assertRaises(frappe.ValidationError):
            self.template(self.prefix + ' Edited-Label').insert()
        doc.language = 'en'
        with self.assertRaisesRegex(frappe.ValidationError, 'language is immutable'):
            doc.save()
        notice = self.template(self.prefix + ' Notice Identity', usage='Notification').insert()
        notice.template_name = self.prefix + ' Changed Identity'
        with self.assertRaisesRegex(frappe.ValidationError, 'immutable'):
            notice.save()
        notice.reload()
        notice.language = 'en'
        with self.assertRaisesRegex(frappe.ValidationError, 'language is immutable'):
            notice.save()
        notice.reload()
        with self.assertRaises(frappe.ValidationError):
            notice.run_method('before_rename', notice.name, self.prefix + '_renamed')
        with self.assertRaises(frappe.ValidationError):
            frappe.rename_doc('Communication Template', notice.name, self.prefix + '_renamed', force=True)


class TestRegisteredTemplateLocalization(unittest.TestCase):
    def test_generic_notification_presets_register_without_provider_or_bot_languages(self):
        from messaging.communication.doctype.communication_template.communication_template import (
            materialize_registered_template,
            registered_template_definition,
            save_registered_template,
        )
        frappe.set_user('Administrator')
        frappe.db.savepoint('template_localization')
        name = 'Registry Test ' + frappe.generate_hash(length=8)
        try:
            if not frappe.db.exists('Communication Channel', 'emulator'):
                from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
                set_provider_enabled('EMULR', True)
                frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR',
                    channel_name='Emulator')).insert()
            with patch('messaging.chatbot.doctype.bot_settings.language.get_bot_settings',
                    return_value=frappe._dict(chatbot_languages=[])), patch(
                    'messaging.chatbot.doctype.bot_settings.language.resolve_process_language',
                    side_effect=AssertionError('Preset registration cannot resolve a bot language')):
                definition = dict(template_name=name, languages={'en': {'body': 'Registered notification'}})
                identifier = save_registered_template(definition=definition, language='en')
                self.assertFalse(frappe.db.exists('Communication Template', identifier))
                entry = registered_template_definition(identifier)
                self.assertEqual(entry['language'], 'en')
                self.assertEqual(save_registered_template(definition=definition, language='en'), identifier)
                materialized = materialize_registered_template(identifier, channel='emulator')
                self.assertEqual(materialize_registered_template(identifier, channel='emulator'), materialized)
                template = frappe.get_doc('Communication Template', materialized)
                self.assertEqual(template.status, 'Inactive')
                self.assertEqual(template.provider, 'EMULR')
                self.assertEqual(template.language, 'en')
        finally:
            frappe.db.rollback(save_point='template_localization')


STATUS = {}


def query_template_status(template, account):
	if STATUS.get('fail'):
		return dict(outcome='error', template=template.name, scope_type='account',
			scope_key=account or template.provider, error_code='sync_failed')
	row = STATUS.get((template.name, account))
	if not row:
		return dict(outcome='unknown', template=template.name, scope_type='account',
			scope_key=account or template.provider, error_code='not_synchronized')
	return dict(outcome='reported', template=template.name, scope_type='account', scope_key=account,
		provider_status=row['status'], available=row['available'], reference=row.get('reference') or 'sync-1')


def template_available(template, account):
	from messaging.communication.doctype.communication_template.communication_template import template_content_revision
	row = next((item for item in template.statuses if item.scope_type == 'account' and item.scope_key == account and item.provider_status), None)
	if row is None:
		return dict(available=None, authorization_required=True, reason='unknown')
	if row.template_revision and row.template_revision != template_content_revision(template):
		return dict(available=None, authorization_required=True, reason='content_changed')
	if row.available == 'Available':
		return dict(available=True, authorization_required=True, reason='available')
	if row.available == 'Unavailable':
		return dict(available=False, authorization_required=True, reason='unavailable')
	return dict(available=None, authorization_required=True, reason='unknown')


def apply_template_status_update(payload):
	return dict(outcome='reported', template=payload['template'], scope_type='account', scope_key=payload['account'],
		provider_status=payload['status'], available=bool(payload['available']), reference='event-1')


class TestTemplateStatusCapability(ProcessFixtures, unittest.TestCase):
	def setUp(self):
		super().setUp()
		from messaging.communication.doctype.communication_provider.test_communication_provider import (
			TestCommunicationProvider, sync_communication_providers,
		)
		TestCommunicationProvider.setUp(self)
		self.declarations[0]['capabilities'].extend(['templates', 'template_status'])
		self.declarations[0]['operations'].update(
			validate_template='messaging.communication.doctype.communication_provider.test_communication_provider.validate_template',
			query_template_status=__name__ + '.query_template_status',
			template_available=__name__ + '.template_available',
			apply_template_status_update=__name__ + '.apply_template_status_update',
		)
		sync_communication_providers()
		from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
		set_provider_enabled('TSTPV', True)
		self.channel = TestCommunicationProvider.channel.__get__(self)
		self.account = TestCommunicationProvider.account.__get__(self)
		self.binding = self.account(self.channel(enabled=1), enabled=1)
		self.other = self.account(frappe.get_doc('Communication Channel', self.binding.channel), account_id=self.prefix + '-other', enabled=1)
		STATUS.clear()

	def template(self):
		return frappe.get_doc(dict(
			doctype='Communication Template', template_name=self.prefix + ' status', language='es',
			channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTPV'}, 'name'), status='Active', body='Status content',
		)).insert()

	def test_provider_required_on_create_and_activation_rules(self):
		from messaging.communication.doctype.communication_template.communication_template import resolve_output, set_active
		with self.assertRaisesRegex(frappe.ValidationError, 'Communication Channel'):
			frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix + ' missing provider',
				language='es', status='Inactive', body='Providerless content')).insert()
		with self.assertRaisesRegex(frappe.ValidationError, 'Active or Inactive'):
			frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix + ' retired draft',
				language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTPV'}, 'name'), status='Draft', body='Draft content')).insert()
		inactive = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix + ' inactive',
			language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTPV'}, 'name'), status='Inactive', body='Inactive content')).insert()
		self.assertEqual(inactive.status, 'Inactive')
		self.assertEqual(inactive.provider_status, 'not_synchronized')
		with self.assertRaises(frappe.ValidationError):
			resolve_output('Template', template_reference=inactive.name, data={}, usage='Notification', account=self.binding.name)
		inactive.reload()
		inactive.status = 'Active'
		with self.assertRaisesRegex(frappe.ValidationError, 'Activate or Deactivate'):
			inactive.save()
		inactive.reload()
		set_active(dict(name=inactive.name, modified=str(inactive.modified)), 1)
		inactive.reload()
		self.assertEqual(inactive.status, 'Active')

	def test_scoped_status_sync_failure_content_revision_and_send_decision(self):
		from messaging.communication.doctype.communication_template.communication_template import set_active, template_content_revision
		from messaging.communication.doctype.communication_template.status import (
			accept_template_status_update, notification_template_gate, sync_template_status,
		)
		template = self.template()
		STATUS[(template.name, self.binding.name)] = dict(status='APPROVED', available=True)
		STATUS[(template.name, self.other.name)] = dict(status='REJECTED', available=False)
		sync_template_status(template, self.binding.name)
		template.reload()
		sync_template_status(template, self.other.name)
		template.reload()
		rows = {(row.scope_key, row.provider_status, row.available) for row in template.statuses}
		self.assertEqual(rows, {
			(self.binding.name, 'APPROVED', 'Available'),
			(self.other.name, 'REJECTED', 'Unavailable'),
		})
		self.assertEqual(template.provider_status, 'multiple')
		self.assertEqual(template.status, 'Active')
		approved = next(row for row in template.statuses if row.scope_key == self.binding.name)
		self.assertEqual(approved.template_revision, template_content_revision(template))
		previous_sync = str(approved.synced_at)
		STATUS['fail'] = True
		sync_template_status(template, self.binding.name)
		template.reload()
		approved = next(row for row in template.statuses if row.scope_key == self.binding.name)
		self.assertEqual(approved.provider_status, 'APPROVED')
		self.assertEqual(approved.available, 'Available')
		self.assertEqual(str(approved.synced_at), previous_sync)
		self.assertEqual(approved.last_error, 'sync_failed')
		self.assertEqual(template.status, 'Active')
		self.assertEqual(notification_template_gate(template.name, self.binding.name), 'template_status_unknown')
		self.assertEqual(notification_template_gate(template.name, self.other.name), 'template_status_unknown')

		# Toggling administrative status keeps content revision intact and does not alter external status.
		set_active(dict(name=template.name, modified=str(template.modified)), 0)
		template.reload()
		self.assertEqual(template.status, 'Inactive')
		self.assertEqual(notification_template_gate(template.name, self.binding.name), 'template_not_active')
		self.assertEqual(next(row.provider_status for row in template.statuses if row.scope_key == self.binding.name), 'APPROVED')
		accept_template_status_update('TSTPV', dict(template=template.name, account=self.other.name, status='APPROVED', available=True))
		template.reload()
		self.assertEqual(template.status, 'Inactive')
		self.assertEqual(next(row.provider_status for row in template.statuses if row.scope_key == self.other.name), 'APPROVED')

		# A legacy status reading remains documentary after activation and edits.
		set_active(dict(name=template.name, modified=str(template.modified)), 1)
		template.reload()
		self.assertEqual(notification_template_gate(template.name, self.binding.name), 'template_status_unknown')
		template.body = 'Edited content needing fresh approval'
		template.save()
		self.assertEqual(template.status, 'Active')
		self.assertEqual(notification_template_gate(template.name, self.binding.name), 'template_status_unknown')
		STATUS['fail'] = False
		sync_template_status(template, self.binding.name)
		template.reload()
		self.assertEqual(notification_template_gate(template.name, self.binding.name), 'template_status_unknown')

	def test_provider_without_status_capability_does_not_block(self):
		from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
		from messaging.communication.doctype.communication_template.status import notification_template_gate
		set_provider_enabled('TSTOT', True)
		channel = self.channel('TSTOT', enabled=1, parameters=[])
		account = self.account(channel, account_id=self.prefix + '-plain', enabled=1)
		template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix + ' plain',
			language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'TSTOT'}, 'name'), status='Active', body='Plain content')).insert()
		self.assertEqual(template.provider_status, 'not_applicable')
		self.assertIsNone(notification_template_gate(template.name, account.name))
