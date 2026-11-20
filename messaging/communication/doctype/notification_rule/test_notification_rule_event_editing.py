"""Focused tests for Notification Rule event editing by lifecycle status."""
import json
import unittest

import frappe

from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
from messaging.communication.doctype.notification_rule.notification_rule import (
	configuration,
	invalidate_rules,
	rule_index,
	set_active,
	validate_process,
)
from messaging.registry.processes import (
	NotifyNoResponse,
	NotifyParams,
	NotifyProcess,
	NotifyRequest,
	NotifyResponse,
	ProcessOutcomes,
)
from messaging.registry.processes import register_processes


def suspension_def(*, params: NotifyParams, outcomes: ProcessOutcomes) -> None:
	outcomes.register(
		"suspension_notice",
		fields={"recipient": "string", "name": "string", "suspension": "string", "contract": "string", "message": "string"},
		mode=["template"],
	)


def suspension_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
	return NotifyResponse(
		"suspension_notice",
		data={
			"recipient": "+50488887777",
			"name": "Abonado Sintético",
			"suspension": "SS-00001",
			"contract": "SC-00001",
			"message": "Notificación sintética",
		},
	)


class TestNotificationRuleEventEditing(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.prefix = "NREE" + frappe.generate_hash(length=6)
		self.rules = []
		self.templates = []
		self.accounts = []
		self.channels = []
		self.contacts = []
		self.process_id = "messaging." + self.prefix.lower() + "_suspension"
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
					label="Synthetic suspension notice",
					definition=suspension_def,
					handler=suspension_handler,
				),
			],
		)
		set_provider_enabled("EMULR", True)
		self.emulr_channel = frappe.db.get_value("Communication Channel", {"provider": "EMULR"}, "name")
		if not self.emulr_channel:
			ch = frappe.get_doc(
				dict(doctype="Communication Channel", provider="EMULR", channel_name="Channel Emulator " + self.prefix)
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
		self.emulr_template = frappe.get_doc(
			dict(
				doctype="Communication Template",
				channel=self.emulr_channel,
				template_name=self.prefix + "_emulr_tpl",
				language="es",
				status="Active",
				body="Aviso {{name}} {{suspension}} {{contract}}: {{message}}",
				template_variables=[
					dict(parameter_name="name"),
					dict(parameter_name="suspension"),
					dict(parameter_name="contract"),
					dict(parameter_name="message"),
				],
			)
		).insert()
		self.templates.append(self.emulr_template.name)

	def tearDown(self):
		frappe.set_user("Administrator")
		for rule_name in reversed(self.rules):
			if frappe.db.exists("Notification Rule", rule_name):
				if frappe.db.get_value("Notification Rule", rule_name, "status") == "Enabled":
					doc = frappe.get_doc("Notification Rule", rule_name)
					set_active(dict(name=doc.name, modified=str(doc.modified)), 0)
				frappe.delete_doc("Notification Rule", rule_name, force=True, ignore_permissions=True)
		for name in reversed(self.templates):
			if frappe.db.exists("Communication Template", name):
				frappe.delete_doc("Communication Template", name, force=True, ignore_permissions=True)
		for name in reversed(self.accounts):
			if frappe.db.exists("Communication Channel Account", name):
				frappe.delete_doc("Communication Channel Account", name, force=True, ignore_permissions=True)
		for name in reversed(self.channels):
			if frappe.db.exists("Communication Channel", name):
				frappe.delete_doc("Communication Channel", name, force=True, ignore_permissions=True)
		for name in reversed(self.contacts):
			if frappe.db.exists("Emulator Contact", name):
				frappe.delete_doc("Emulator Contact", name, force=True, ignore_permissions=True)
		from messaging.presets.processes.greeting import process as greeting_process
		from messaging.presets.processes.human_availability import process as human_process
		from messaging.presets.processes.registration import process as registration_process
		register_processes(
			app="messaging",
			processes=[greeting_process, human_process, registration_process],
		)
		invalidate_rules()

	def _make_suspension_rule(self):
		rule = frappe.get_doc(
			dict(
				doctype="Notification Rule",
				rule_name=self.prefix + " Suspension Rule",
				notification_delay_minutes=60,
				source_application="qota",
				source_doctype="Service Suspension",
				account=self.emulr_account.name,
				process_definition=self.process_id,
				events=[
					dict(document_event="on_submit", field_name="", value=""),
					dict(document_event="on_update", field_name="status", value='"Executed"'),
				],
				parameters=[dict(parameter_key="notification_contact", value=json.dumps(self.emulr_contact.name))],
				outputs=[
					dict(
						status_code="suspension_notice",
						output_mode="Template",
						send=1,
						template_reference=self.emulr_template.name,
					)
				],
			)
		).insert()
		self.rules.append(rule.name)
		return rule

	def test_event_editing_lifecycle_and_approval_invalidation(self):
		# 1. Untested: editing events is permitted and stays Untested
		rule = self._make_suspension_rule()
		self.assertEqual(rule.status, "Untested")
		rule.append("events", dict(document_event="on_cancel", field_name="", value=""))
		rule.save()
		self.assertEqual(rule.status, "Untested")
		self.assertEqual(len(rule.events), 3)

		# Remove the extra row in Untested
		rule.events = rule.events[:2]
		rule.save()
		self.assertEqual(rule.status, "Untested")
		self.assertEqual(len(rule.events), 2)

		# 2. Validate -> Disabled with approval_hash
		res = validate_process(dict(name=rule.name, modified=str(rule.modified)))
		self.assertEqual(res["consumer_status"], "Disabled")
		rule.reload()
		first_hash = rule.approval_hash
		first_log = rule.process_test_log
		self.assertTrue(first_hash)
		self.assertTrue(first_log)

		# 3. Enable -> Enabled; editing events must be rejected
		res = set_active(dict(name=rule.name, modified=str(rule.modified)), 1)
		self.assertEqual(res["consumer_status"], "Enabled")
		rule.reload()
		rule.events[1].document_event = "on_update_after_submit"
		with self.assertRaises(frappe.ValidationError):
			rule.save()

		# 4. Disable without changing events -> Disabled, approval_hash preserved
		rule.reload()
		res = set_active(dict(name=rule.name, modified=str(rule.modified)), 0)
		self.assertEqual(res["consumer_status"], "Disabled")
		rule.reload()
		self.assertEqual(rule.approval_hash, first_hash)
		self.assertFalse(rule.approval_invalidation)

		# 5. Disabled: change on_update -> on_update_after_submit (keeping on_submit) -> Unavailable
		rule.events[1].document_event = "on_update_after_submit"
		rule.save()
		self.assertEqual(rule.status, "Unavailable")
		self.assertEqual(rule.approval_hash, first_hash)
		self.assertEqual(rule.process_test_log, first_log)
		history = json.loads(rule.approval_invalidation)
		self.assertEqual(len(history), 1)
		self.assertEqual(history[0]["reason"], "consumer_contract_changed")
		self.assertEqual(history[0]["approval_hash"], first_hash)

		# 6. Unavailable: add, modify, remove events -> stays Unavailable
		rule.append("events", dict(document_event="on_cancel", field_name="status", value='"Draft"'))
		rule.save()
		self.assertEqual(rule.status, "Unavailable")
		self.assertEqual(len(rule.events), 3)
		rule.events = rule.events[:2]
		rule.save()
		self.assertEqual(rule.status, "Unavailable")
		self.assertEqual(len(rule.events), 2)

		# 7. Revalidate -> Disabled with new approval_hash
		res = validate_process(dict(name=rule.name, modified=str(rule.modified)))
		self.assertEqual(res["consumer_status"], "Disabled")
		rule.reload()
		self.assertNotEqual(rule.approval_hash, first_hash)

		# 8. Enable -> Enabled; rule_index contains on_update_after_submit and not on_update
		res = set_active(dict(name=rule.name, modified=str(rule.modified)), 1)
		self.assertEqual(res["consumer_status"], "Enabled")
		invalidate_rules()
		idx = rule_index().get("Service Suspension", {})
		self.assertIn(rule.name, idx.get("on_submit", []))
		self.assertIn(rule.name, idx.get("on_update_after_submit", []))
		self.assertNotIn(rule.name, idx.get("on_update", []))

	def test_structural_fields_and_approval_protection_preserved(self):
		rule = self._make_suspension_rule()
		# Structural field source_doctype remains immutable even when not Enabled
		rule.source_doctype = "Service Contract"
		with self.assertRaises(frappe.ValidationError):
			rule.save()
		rule.reload()

		# Tampering status or approval_hash via save() or db_set() is rejected
		rule.status = "Enabled"
		with self.assertRaises(frappe.PermissionError):
			rule.save()
		rule.reload()
		with self.assertRaises(frappe.PermissionError):
			rule.db_set("status", "Enabled")
		with self.assertRaises(frappe.PermissionError):
			rule.db_set("approval_hash", "forged")

	def test_submittable_event_availability_and_spanish_translations(self):
		cfg = configuration(source_application="qota", source_doctype="Service Suspension")
		self.assertIn("on_update_after_submit", cfg["events"])
		self.assertEqual(frappe._("on_update", lang="es"), "Al actualizar")
		self.assertEqual(
			frappe._("on_update_after_submit", lang="es"),
			"Al actualizar después de confirmar",
		)
