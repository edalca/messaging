"""Focused tests for Notification Rule status Select and Code-First recipient redirection (EMULR & WHATSAPP)."""
from datetime import timedelta
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime, now_datetime

from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.communication.doctype.communication_provider.communication_provider import (
	get_provider_states,
	set_provider_enabled,
	_lock_provider_states,
	_write_provider_states,
)
from messaging.communication.doctype.notification_queue import dispatch
from messaging.communication.doctype.notification_rule import events
from messaging.communication.doctype.notification_rule.notification_rule import (
	configuration,
	invalidate_rules,
	notification_destination,
	validate_process,
	set_active,
)
from messaging.communication.doctype.process_definition import validation as process_testing
from messaging.registry.processes import (
	NotifyNoResponse,
	NotifyParams,
	NotifyProcess,
	NotifyRequest,
	NotifyResponse,
	ProcessOutcomes,
)
from messaging.registry.processes import register_processes
from messaging.whatsapp.api import Graph, GraphError
from messaging.whatsapp.provider import normalize_e164_phone


INVALID_ORIGINAL = "<invalid recipient> " * 12


def notice_def(*, params: NotifyParams, outcomes: ProcessOutcomes) -> None:
	outcomes.register(
		"payment_received",
		fields={"recipient": "string", "subscriber_name": "string", "amount": "string", "detail": "string"},
		mode=["template"],
	)


def notice_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
	doc = request.source.get_document()
	name = doc.get("display_name") or "Subscriber"
	if name.endswith("Skip"):
		return NotifyNoResponse("payment_not_eligible")
	recipient = "+50488887777"
	if name.endswith("No Phone"):
		recipient = ""
	elif name.endswith("Invalid Phone"):
		recipient = INVALID_ORIGINAL
	return NotifyResponse(
		"payment_received",
		data={
			"recipient": recipient,
			"subscriber_name": name,
			"amount": "250.00",
			"detail": "Cuota enero:\tL     160.00\r\nCuota febrero: L 90.00",
		},
	)


class TestNotificationRuleStatusAndRedirect(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.prefix = "NRSR" + frappe.generate_hash(length=6)
		self.states_before = get_provider_states()
		self.rules = []
		self.templates = []
		self.accounts = []
		self.channels = []
		self.contacts = []
		self.process_id = "messaging." + self.prefix.lower() + "_payment"
		from messaging.presets.processes.greeting import process as greeting_process
		from messaging.presets.processes.human_availability import process as human_process
		from messaging.presets.processes.registration import process as registration_process
		register_processes(
			app="messaging",
			processes=[
				greeting_process,
				human_process,
				registration_process,
				NotifyProcess(
					id=self.process_id,
					label="Synthetic payment receipt",
					definition=notice_def,
					handler=notice_handler,
				),
			],
		)
		set_provider_enabled("EMULR", True)
		set_provider_enabled("WHATSAPP", True)
		self.emulr_channel = frappe.db.get_value("Communication Channel", {"provider": "EMULR"}, "name")
		if not self.emulr_channel:
			ch = frappe.get_doc(
				dict(doctype="Communication Channel", provider="EMULR", channel_name="Channel Emulator")
			).insert()
			self.emulr_channel = ch.name
			self.channels.append(ch.name)
		set_channel_status(self.emulr_channel, "Enabled")
		self.emulr_account = frappe.get_doc(
			dict(
				doctype="Communication Channel Account",
				channel=self.emulr_channel,
				account_name=self.prefix + " Emulr Account",
				enabled=1,
				parameters=[dict(parameter_key="transport_outcome", value='"Accepted"')],
			)
		).insert()
		self.accounts.append(self.emulr_account.name)
		self.emulr_contact = frappe.get_doc(
			dict(doctype="Emulator Contact", display_name=self.prefix + " Emulated", enabled=1)
		).insert()
		self.contacts.append(self.emulr_contact.name)

		from messaging.whatsapp.configuration import webhook_token_write
		with webhook_token_write():
			self.wa_channel = frappe.get_doc(
				dict(
					doctype="Communication Channel",
					provider="WHATSAPP",
					channel_name="WaTest " + "".join(chr(ord("a") + int(c, 16)) for c in frappe.generate_hash(length=6)),
					parameters=[
						dict(parameter_key="app_id", value=json.dumps(str(910000000000 + int(frappe.generate_hash(length=5), 16) % 899999))),
						dict(parameter_key="app_secret", secret_value="synthetic-secret"),
						dict(parameter_key="verify_token", secret_value="synthetic-verify-token"),
						dict(parameter_key="graph_version", value='"v27.0"'),
					],
				)
			).insert()
		set_channel_status(self.wa_channel.name, "Enabled")
		self.channels.append(self.wa_channel.name)
		self.wa_account = frappe.get_doc(
			dict(
				doctype="Communication Channel Account",
				channel=self.wa_channel.name,
				account_id=str(810000000000 + int(frappe.generate_hash(length=5), 16) % 899999),
				account_name=self.prefix + " WA Account",
				enabled=1,
				parameters=[
					dict(parameter_key="waba_id", value='"770011223344"'),
					dict(parameter_key="access_token", secret_value="synthetic-access-token"),
				],
			)
		).insert()
		self.accounts.append(self.wa_account.name)

		self.emulr_template = frappe.get_doc(
			dict(
				doctype="Communication Template",
				channel=self.emulr_channel,
				template_name=self.prefix + "_emulr_tpl",
				language="es",
				status="Active",
				body="Pago de {{subscriber_name}} por {{amount}}",
				template_variables=[
					dict(parameter_name="subscriber_name"),
					dict(parameter_name="amount"),
				],
			)
		).insert()
		self.templates.append(self.emulr_template.name)

		frappe.db.commit()
		self.wa_template = self._make_published_wa_named_template()
		from messaging.security.service_identity import SERVICE_USER
		self.source = frappe.get_doc(
			dict(doctype="Emulator Contact", display_name=self.prefix + " Source", enabled=1)
		).insert()
		self.contacts.append(self.source.name)
		frappe.share.add("Emulator Contact", self.source.name, user=SERVICE_USER, read=1, notify=0)
		frappe.db.commit()

	def _make_published_wa_named_template(self):
		from messaging.communication.doctype.communication_template.communication_template import set_active
		from messaging.communication.doctype.communication_template.external import synchronize_templates

		tpl_name = (self.prefix + "_wa_named").lower()
		meta_template = dict(
			id="1001999",
			name=tpl_name,
			language="es",
			status="APPROVED",
			category="UTILITY",
			parameter_format="NAMED",
			components=[
				dict(
					type="BODY",
					text="Hola {{subscriber_name}}, recibimos su pago de {{amount}}.\n{{detail}}",
					example=dict(
						body_text_named_params=[
							dict(param_name="subscriber_name", example="Ana"),
							dict(param_name="amount", example="250.00"),
							dict(param_name="detail", example="Cuota enero: L 160.00"),
						]
					),
				)
			],
		)
		with patch.object(Graph, "templates", return_value=[meta_template]):
			synchronize_templates("WHATSAPP", self.wa_channel.name, self.wa_account.name)
		name = frappe.db.get_value(
			"Communication Template",
			{"channel": self.wa_channel.name, "account": self.wa_account.name, "template_name": tpl_name},
			"name",
		)
		tpl = frappe.get_doc("Communication Template", name)
		self.templates.append(tpl.name)
		set_active(dict(name=tpl.name, modified=str(tpl.modified)), 1)
		tpl.reload()
		return tpl

	def _make_rule(self, *, account, template, status="Untested", redirect_recipient=0, parameters=None):
		rule = frappe.get_doc(
			dict(
				doctype="Notification Rule",
				rule_name=self.prefix + " Rule " + frappe.generate_hash(length=5),
				status=status,
				notification_delay_minutes=60,
				source_application="messaging",
				source_doctype="Emulator Contact",
				events=[dict(document_event="after_insert")],
				account=account,
				redirect_recipient=redirect_recipient,
				parameters=parameters or [],
				process_definition=self.process_id,
				outputs=[
					dict(
						status_code="payment_received",
						output_mode="Template",
						template_reference=template,
						send=1,
					)
				],
			)
		)
		rule.insert()
		self.rules.append(rule.name)
		frappe.db.commit()
		return rule

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if self.rules:
			frappe.db.delete("Notification Queue", {"notification_rule": ["in", self.rules]})
			frappe.db.delete("Process Execution Log", {"source_name": ["in", self.rules]})
			frappe.db.delete("Notification Rule Event", {"parent": ["in", self.rules]})
			frappe.db.delete("Notification Outcome", {"parent": ["in", self.rules]})
			frappe.db.delete("Notification Rule", {"name": ["in", self.rules]})
		if self.rules or self.templates or self.accounts or self.channels:
			frappe.db.delete(
				"Communication Provider Value",
				{"parent": ["in", self.rules + self.templates + self.accounts + self.channels]},
			)
		if self.templates:
			frappe.db.delete("Communication Template Parameter", {"parent": ["in", self.templates]})
			frappe.db.delete("Communication Template Status", {"parent": ["in", self.templates]})
			frappe.db.delete("Communication Template", {"name": ["in", self.templates]})
		for name in self.accounts:
			if frappe.db.exists("Communication Channel Account", name):
				frappe.delete_doc("Communication Channel Account", name, ignore_permissions=True)
		for name in self.channels:
			if frappe.db.exists("Communication Channel", name):
				frappe.delete_doc("Communication Channel", name, ignore_permissions=True)
		for name in self.contacts:
			if frappe.db.exists("Emulator Contact", name):
				frappe.delete_doc("Emulator Contact", name, ignore_permissions=True)
		if frappe.db.exists("Process Definition", self.process_id):
			frappe.db.delete("Process Definition", {"name": self.process_id})
		from messaging.registry import synchronize_registry
		synchronize_registry()
		_lock_provider_states()
		_write_provider_states(self.states_before)
		invalidate_rules()
		frappe.db.commit()

	def test_status_select_lifecycle_validation_and_permissions(self):
		from messaging.communication.doctype.process_definition import validation
		status = frappe.get_meta("Notification Rule").get_field("status")
		self.assertEqual(status.fieldtype, "Select")
		self.assertEqual(status.options, "Untested\nDisabled\nEnabled\nUnavailable")
		self.assertEqual(status.read_only, 1)

		rule = self._make_rule(
			account=self.emulr_account.name,
			template=self.emulr_template.name,
			parameters=[dict(parameter_key="notification_contact", value=json.dumps(self.emulr_contact.name))],
		)
		self.assertEqual(rule.status, "Untested")
		rule.status = "Enabled"
		with self.assertRaises(frappe.PermissionError):
			rule.save()
		rule.reload()
		self.assertEqual(validate_process(rule.as_dict())["consumer_status"], "Disabled")
		rule.reload()
		evidence = (rule.approval_hash, rule.process_test_log, rule.approved_delivery_hash)
		self.assertTrue(all(evidence))
		with patch.object(validation, "run_test", side_effect=AssertionError("Unexpected validation")):
			set_active(rule.as_dict(), 1)
			rule.reload()
			rule.notification_delay_minutes = 90
			rule.rule_name += " updated"
			rule.save()
			self.assertEqual(rule.status, "Enabled")
			set_active(rule.as_dict(), 0)
		rule.reload()
		self.assertEqual(rule.status, "Disabled")
		self.assertEqual((rule.approval_hash, rule.process_test_log, rule.approved_delivery_hash), evidence)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			set_active(rule.as_dict(), 1)

	def test_execution_rejects_an_enabled_snapshot_after_concurrent_disable(self):
		from concurrent.futures import ThreadPoolExecutor

		from messaging.communication.doctype.process_definition.approval import lock_execution

		rule = self._make_rule(
			account=self.emulr_account.name,
			template=self.emulr_template.name,
			parameters=[dict(parameter_key="notification_contact", value=json.dumps(self.emulr_contact.name))],
		)
		self.assertEqual(validate_process(rule.as_dict())["consumer_status"], "Disabled")
		rule.reload()
		set_active(rule.as_dict(), 1)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Notification Rule", rule.name, "status"), "Enabled")
		site, sites_path = frappe.local.site, frappe.local.sites_path

		def disable():
			frappe.init(site=site, sites_path=sites_path)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				current = frappe.get_doc("Notification Rule", rule.name)
				set_active(current.as_dict(), 0)
				frappe.db.commit()
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=1) as pool:
			pool.submit(disable).result(timeout=15)
		with self.assertRaises(frappe.PermissionError):
			lock_execution("Notification Rule", rule.name)
		frappe.db.rollback()
		rule.reload()
		self.assertEqual(rule.status, "Disabled")
		self.assertTrue(rule.approval_hash)

	def test_dispatch_blocks_invalidation_after_durable_handoff_marker(self):
		from messaging.communication.doctype.process_definition import approval

		rule = self._make_rule(
			account=self.emulr_account.name,
			template=self.emulr_template.name,
			parameters=[dict(parameter_key="notification_contact", value=json.dumps(self.emulr_contact.name))],
		)
		self.assertEqual(validate_process(rule.as_dict())["consumer_status"], "Disabled")
		rule.reload()
		set_active(rule.as_dict(), 1)
		with patch.object(events, "rule_index", return_value={"Emulator Contact": {"after_insert": [rule.name]}}):
			events.observe(self.source, "after_insert")
		frappe.db.commit()
		name = frappe.db.get_value("Notification Queue", {"notification_rule": rule.name}, "name")
		queue = frappe.get_doc("Notification Queue", name)
		real_lock = approval.lock_execution

		def lock_before_handoff(doctype, docname):
			if frappe.db.get_value("Notification Queue", name, "handoff_at"):
				raise frappe.PermissionError("Synthetic invalidation at the commit boundary")
			return real_lock(doctype, docname)

		with (
			patch.object(approval, "lock_execution", side_effect=lock_before_handoff),
			patch.object(dispatch, "handoff", side_effect=AssertionError("Unapproved delivery")) as handoff,
		):
			dispatch.deliver(name, now=get_datetime(queue.scheduled_at) + timedelta(minutes=1))
		handoff.assert_not_called()
		queue.reload()
		self.assertEqual(queue.status, "Cancelled")
		self.assertEqual(queue.diagnostic_code, "rule_approval_obsolete")
		self.assertFalse(queue.handoff_at)

	def test_whatsapp_e164_validation_and_normalization(self):
		self.assertEqual(normalize_e164_phone("+50499999999"), "+50499999999")
		self.assertEqual(normalize_e164_phone("  +504 9999-9999  "), "+50499999999")
		self.assertEqual(normalize_e164_phone("+504 (9999) 9999"), "+50499999999")
		self.assertEqual(normalize_e164_phone("+14155552671"), "+14155552671")

		for invalid in (
			None,
			"",
			"50499999999",
			"+5049999999",
			"+50412345678",
			"+11111111111",
			"+99912345678",
			"+50499999999abc",
			"+504 (9999 9999",
			"+50499999999 x123",
			"++50499999999",
		):
			with self.subTest(invalid=invalid), self.assertRaises(frappe.ValidationError):
				normalize_e164_phone(invalid)

	def test_whatsapp_redirection_named_template_dedup_and_historical_preservation(self):
		info = configuration(account=self.wa_account.name)
		self.assertTrue(info["redirect_supported"])
		self.assertFalse(info["redirect_required"])

		# 1) Normal mode (no redirect) -> sends to original business recipient
		normal_rule = self._make_rule(
			account=self.wa_account.name,
			template=self.wa_template.name,
			redirect_recipient=0,
		)
		self.assertEqual(validate_process(normal_rule.as_dict())["status"], "Passed")
		normal_rule.reload()
		set_active(normal_rule.as_dict(), 1)
		normal_rule.reload()
		self.assertEqual(normal_rule.status, "Enabled")

		with patch.object(
			events, "rule_index", return_value={"Emulator Contact": {"after_insert": [normal_rule.name]}}
		):
			events.observe(self.source, "after_insert")
			# Deduplication: second observe for same occurrence creates no duplicate
			events.observe(self.source, "after_insert")
		frappe.db.commit()
		normal_rows = frappe.get_all(
			"Notification Queue", filters={"notification_rule": normal_rule.name}, pluck="name"
		)
		self.assertEqual(len(normal_rows), 1)
		nq_normal = frappe.get_doc("Notification Queue", normal_rows[0])
		self.assertEqual(nq_normal.recipient, "+50488887777")
		self.assertEqual(nq_normal.effective_recipient, "+50488887777")
		self.assertEqual(nq_normal.delivery_mode, "Normal")

		# 2) Redirected mode with formatted phone -> normalized to E.164 on save
		redir_rule = self._make_rule(
			account=self.wa_account.name,
			template=self.wa_template.name,
			redirect_recipient=1,
			parameters=[dict(parameter_key="notification_phone_number", value='"+504 9999-9999"')],
		)
		self.assertEqual(json.loads(redir_rule.parameters[0].value), "+50499999999")
		self.assertEqual(validate_process(redir_rule.as_dict())["status"], "Passed")
		redir_rule.reload()
		set_active(redir_rule.as_dict(), 1)
		redir_rule.reload()
		self.assertEqual(redir_rule.status, "Enabled")
		invalid_rule = frappe.get_doc("Notification Rule", redir_rule.name)
		invalid_rule.parameters[0].value = json.dumps("invalid")
		with self.assertRaises(frappe.ValidationError):
			notification_destination(invalid_rule, self.wa_account, "+50488887777")

		from messaging.security.service_identity import SERVICE_USER
		for label, diagnostic in (
			("No Phone", "recipient_missing"), ("Invalid Phone", "recipient_invalid"), ("Skip", "payment_not_eligible"),
		):
			source = frappe.get_doc(dict(
				doctype="Emulator Contact", display_name=f"{self.prefix} {label}", enabled=1,
			)).insert()
			self.contacts.append(source.name)
			frappe.share.add("Emulator Contact", source.name, user=SERVICE_USER, read=1, notify=0)
			with patch.object(events, "rule_index", return_value={"Emulator Contact": {"after_insert": [normal_rule.name]}}):
				events.observe(source, "after_insert")
				events.observe(source, "after_insert")
			self.assertFalse(frappe.db.exists("Notification Queue", {"notification_rule": normal_rule.name, "source_name": source.name}))
			logs = frappe.get_all("Process Execution Log", filters={"source_name": normal_rule.name, "source_document": source.name},
				fields=["execution_outcome", "functional_status", "diagnostic_detail"])
			self.assertEqual(len(logs), 1)
			self.assertIn(diagnostic, logs[0].diagnostic_detail)
			if label == "Skip":
				self.assertEqual(logs[0].execution_outcome, "No Result")
				self.assertFalse(logs[0].functional_status)
				continue
			self.assertEqual(logs[0].execution_outcome, "Functional Status")
			self.assertEqual(logs[0].functional_status, "payment_received")
			with patch.object(events, "rule_index", return_value={"Emulator Contact": {"after_insert": [redir_rule.name]}}):
				events.observe(source, "after_insert")
				events.observe(source, "after_insert")
			rows = frappe.get_all("Notification Queue", filters={"notification_rule": redir_rule.name, "source_name": source.name},
				fields=["name", "recipient", "effective_recipient", "scheduled_at"])
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0].recipient, "" if label == "No Phone" else INVALID_ORIGINAL)
			self.assertEqual(rows[0].effective_recipient, "+50499999999")
			if label == "No Phone":
				empty_original = rows[0]

		with patch.object(
			events, "rule_index", return_value={"Emulator Contact": {"after_insert": [redir_rule.name]}}
		):
			events.observe(self.source, "after_insert")
		frappe.db.commit()
		nq_redir = frappe.get_doc(
			"Notification Queue",
			frappe.db.get_value("Notification Queue", {"notification_rule": redir_rule.name}, "name"),
		)
		self.assertEqual(nq_redir.recipient, "+50488887777")
		self.assertEqual(nq_redir.effective_recipient, "+50499999999")
		self.assertEqual(nq_redir.delivery_mode, "Redirected")
		self.assertIsNone(nq_redir.emulator_contact)
		self.assertEqual(json.loads(nq_redir.variables_json), json.loads(nq_normal.variables_json))

		# Dispatch with mocked Meta Graph API: sends ONLY to 50499999999 with NAMED params, never original recipient
		sent_payloads = []

		def fake_send(self_graph, payload):
			sent_payloads.append(payload)
			return {"messages": [{"id": "wamid.HBgNNTA0OTk5OTk5OTkVAgARGBI1"}]}

		with (
			patch.object(Graph, "send", fake_send),
			patch(
				"messaging.communication.doctype.communication_template.status.notification_template_gate",
				return_value=None,
			),
		):
			out = dispatch.dispatch_due(
				names=[nq_redir.name, empty_original.name],
				now=max(get_datetime(nq_redir.scheduled_at), get_datetime(empty_original.scheduled_at)) + timedelta(seconds=1),
			)
		self.assertEqual([item["state"] for item in out], ["Accepted", "Accepted"])
		self.assertEqual([item["to"] for item in sent_payloads], ["50499999999", "50499999999"])
		self.assertEqual(sent_payloads[0]["template"]["name"], self.wa_template.template_name)
		params = sent_payloads[0]["template"]["components"][0]["parameters"]
		self.assertEqual({p["parameter_name"] for p in params}, {"subscriber_name", "amount", "detail"})
		self.assertEqual(next(p["text"] for p in params if p["parameter_name"] == "detail"),
			"Cuota enero: L 160.00; Cuota febrero: L 90.00")
		accepted_empty = frappe.get_doc("Notification Queue", empty_original.name)
		self.assertIn("\r\n", json.loads(accepted_empty.variables_json)["detail"])

		# 3) Meta rejection (e.g., unauthorized test number) -> Rejected, NEVER falls back to original recipient
		second_source = frappe.get_doc(
			dict(doctype="Emulator Contact", display_name=self.prefix + " Source 2", enabled=1)
		).insert()
		self.contacts.append(second_source.name)
		frappe.share.add("Emulator Contact", second_source.name, user=SERVICE_USER, read=1, notify=0)
		with patch.object(
			events, "rule_index", return_value={"Emulator Contact": {"after_insert": [redir_rule.name]}}
		):
			events.observe(second_source, "after_insert")
		frappe.db.commit()
		nq_reject = frappe.get_doc(
			"Notification Queue",
			frappe.db.get_value(
				"Notification Queue",
				{"notification_rule": redir_rule.name, "source_name": second_source.name},
				"name",
			),
		)
		reject_calls = []

		def fake_reject(self_graph, payload):
			reject_calls.append(payload)
			raise GraphError(rejected=True)

		with (
			patch.object(Graph, "send", fake_reject),
			patch(
				"messaging.communication.doctype.communication_template.status.notification_template_gate",
				return_value=None,
			),
		):
			out_rej = dispatch.dispatch_due(
				names=[nq_reject.name],
				now=get_datetime(nq_reject.scheduled_at) + timedelta(seconds=1),
			)
		self.assertEqual(out_rej[0]["state"], "Rejected")
		self.assertEqual(len(reject_calls), 1)
		self.assertEqual(reject_calls[0]["to"], "50499999999")

		# 4) Editing test number later preserves historical queue rows & cancels pending ones
		third_source = frappe.get_doc(
			dict(doctype="Emulator Contact", display_name=self.prefix + " Source 3", enabled=1)
		).insert()
		self.contacts.append(third_source.name)
		frappe.share.add("Emulator Contact", third_source.name, user=SERVICE_USER, read=1, notify=0)
		with patch.object(
			events, "rule_index", return_value={"Emulator Contact": {"after_insert": [redir_rule.name]}}
		):
			events.observe(third_source, "after_insert")
		frappe.db.commit()
		nq_pending = frappe.get_doc(
			"Notification Queue",
			frappe.db.get_value(
				"Notification Queue",
				{"notification_rule": redir_rule.name, "source_name": third_source.name},
				"name",
			),
		)
		self.assertEqual(nq_pending.effective_recipient, "+50499999999")

		# Explicit delivery changes withdraw the approval and keep the structural evidence.
		redir_rule.reload()
		structural = redir_rule.approved_contract_hash
		redir_rule.parameters[0].value = '"+50498887766"'
		redir_rule.save()
		self.assertEqual(redir_rule.status, "Unavailable")
		self.assertEqual(redir_rule.approved_contract_hash, structural)
		self.assertEqual(json.loads(redir_rule.approval_invalidation)[-1]["reason"], "delivery_contract_changed")
		self.assertEqual(validate_process(redir_rule.as_dict())["status"], "Passed")
		redir_rule.reload()
		set_active(redir_rule.as_dict(), 1)
		frappe.db.commit()

		# Historical accepted notification kept its exact original effective_recipient
		nq_redir.reload()
		self.assertEqual(nq_redir.effective_recipient, "+50499999999")
		self.assertEqual(nq_redir.recipient, "+50488887777")

		# Pending notification is NOT retroactively rewritten and is Cancelled on revalidation
		with (
			patch.object(Graph, "send", side_effect=AssertionError("Must not send changed destination")),
			patch(
				"messaging.communication.doctype.communication_template.status.notification_template_gate",
				return_value=None,
			),
		):
			out_cancelled = dispatch.dispatch_due(
				names=[nq_pending.name],
				now=get_datetime(nq_pending.scheduled_at) + timedelta(seconds=1),
			)
		self.assertEqual(out_cancelled[0]["state"], "Cancelled")
		nq_pending.reload()
		self.assertEqual(nq_pending.effective_recipient, "+50499999999")
		self.assertEqual(nq_pending.diagnostic_code, "delivery_configuration_changed")

	def test_emulr_preserved_and_account_switch_rejects_incompatible_params(self):
		info = configuration(account=self.emulr_account.name)
		self.assertFalse(info["redirect_supported"])
		self.assertTrue(info["redirect_required"])

		emulr_rule = self._make_rule(
			account=self.emulr_account.name,
			template=self.emulr_template.name,
			parameters=[dict(parameter_key="notification_contact", value=json.dumps(self.emulr_contact.name))],
		)
		dest = notification_destination(emulr_rule, self.emulr_account, "+50488887777")
		self.assertEqual(dest["effective_recipient"], self.emulr_contact.sender_id)
		self.assertEqual(dest["emulator_contact"], self.emulr_contact.name)
		self.assertEqual(dest["delivery_mode"], "Required")

		# Accountless draft switching from EMULR params to WHATSAPP account rejects incompatible notification_contact
		draft = self._make_rule(account=None, template=None, parameters=[])
		draft.account = self.wa_account.name
		draft.redirect_recipient = 1
		draft.append("parameters", dict(parameter_key="notification_contact", value=json.dumps(self.emulr_contact.name)))
		with self.assertRaises(frappe.ValidationError):
			draft.save()
