import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings, is_registration_complete
from messaging.conversation.doctype.chat_contact.chat_contact import get_contact


class TestChatContact(FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.prefix = "profile_" + frappe.generate_hash(length=10)
		maintenance = patch.object(frappe, "in_test", True)
		maintenance.start()
		self.addCleanup(maintenance.stop)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		super().tearDown()

	def parameter(self, parameter_type, **values):
		return frappe.get_doc(dict(doctype="Bot Parameter", name=self.prefix + "_" + parameter_type.lower(),
			label="Readable " + parameter_type, parameter_type=parameter_type, enabled=1, **values)).insert()

	def contact(self, values):
		contact = get_contact("profile-test", self.prefix, "unicode-sender")
		contact.parameter_values = json.dumps(values, ensure_ascii=False)
		contact.save(ignore_permissions=True)
		return contact

	def test_profile_native_types_labels_and_canonical_keys(self):
		parameters = [self.parameter("String", string_mode="Text"), self.parameter("Int"),
			self.parameter("Date"), self.parameter("Name"), self.parameter("Expression", expression="#####")]
		values = dict(zip([p.name for p in parameters], ["Text\n  preserved", 0, "2024-02-29", "María 鄭", "00042"]))
		contact = self.contact(values)
		settings = get_bot_settings()
		settings.registration_enabled = 0
		settings.save()
		before = frappe.get_doc("Chat Contact", contact.name).as_dict()
		contact.onload()
		fields = contact.get_onload("profile_fields")
		self.assertEqual([f["fieldtype"] for f in fields], ["Small Text", "Int", "Date", "Data", "Data"])
		self.assertEqual({f["key"]: f["value"] for f in fields}, values)
		self.assertEqual([f["label"] for f in fields], [p.label for p in parameters])
		self.assertNotIn("greeting_alias", [f["key"] for f in fields])
		self.assertEqual(frappe.get_doc("Chat Contact", contact.name).as_dict(), before)
		parameters[0].label = "Renamed label"
		parameters[0].save()
		settings.save()
		parameters[0].string_mode = "Single Word"
		parameters[0].save()
		self.assertEqual(contact.profile_fields()[0]["label"], "Renamed label")
		self.assertEqual(contact.profile_fields()[0]["fieldtype"], "Data")
		self.assertEqual(json.loads(frappe.get_doc("Chat Contact", contact.name).parameter_values), values)

	def test_empty_and_historical_profile_with_removed_definition(self):
		contact = self.contact({})
		contact.onload()
		self.assertEqual(contact.get_onload("profile_fields"), [])
		parameter = self.parameter("Expression", expression="#####")
		contact.parameter_values = json.dumps({parameter.name: "00042"})
		contact.save(ignore_permissions=True)
		frappe.delete_doc("Bot Parameter", parameter.name)
		field = contact.profile_fields()[0]
		self.assertTrue(field["historical"])
		self.assertEqual(field["value"], "00042")
		self.assertIn(parameter.name, field["label"])
		self.assertEqual(field["fieldtype"], "Small Text")

	def test_profile_load_preserves_document_and_field_permissions(self):
		contact = self.contact({"historical": "Private profile"})
		with patch.object(contact, "check_permission", side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				contact.onload()
		with patch.dict(frappe.local.session, user="read-without-profile"), \
			patch.object(contact, "check_permission"), patch.object(contact, "has_permlevel_access_to", return_value=False):
			contact.onload()
			self.assertIsNone(contact.get_onload("profile_fields"))
		self.assertTrue(contact.meta.get_field("parameter_values").read_only)
		self.assertTrue(contact.meta.get_field("parameter_values").hidden)
		self.assertEqual(contact.meta.get_field("profile_values").permlevel, 1)
		self.assertTrue(contact.meta.get_field("quota_courtesy_cycle"))
		self.assertFalse(any(p.write for p in contact.meta.permissions))

	def test_profile_native_agent_scope_and_read_only_access(self):
		user = frappe.get_doc(dict(doctype="User", email=self.prefix + "@example.invalid",
			first_name="Profile fixture", enabled=1, send_welcome_email=0,
			roles=[dict(role="Messaging Agent")])).insert(ignore_permissions=True)
		contact = self.contact({"historical": "Private profile"})
		contact = frappe.get_doc("Chat Contact", contact.name)
		frappe.set_user(user.name)
		with self.assertRaises(frappe.PermissionError):
			contact.onload()
		frappe.set_user("Administrator")
		frappe.get_doc(dict(doctype="Chat Agent", user=user.name, channel=contact.channel,
			account=contact.account, enabled=1, capacity=1)).insert()
		frappe.set_user(user.name)
		contact.onload()
		self.assertEqual(contact.get_onload("profile_fields")[0]["value"], "Private profile")
		self.assertFalse(contact.has_permission("write"))
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			contact.onload()



	def test_output_profile_type_is_not_reinterpreted_by_an_unrelated_parameter(self):
		parameter = self.parameter('Int')
		contact = self.contact({parameter.name: 'declared string output'})
		contact.profile_labels = json.dumps({parameter.name: 'Output label'})
		contact.save(ignore_permissions=True)
		field = contact.profile_fields()[0]
		self.assertEqual(field['fieldtype'], 'Small Text')
		self.assertEqual(field['value'], 'declared string output')
		self.assertEqual(field['label'], 'Output label')
		self.assertFalse(field['historical'])

	def registered_contact(self):
		from messaging.common.primitives import utcnow
		from messaging.conversation.doctype.chat_contact.chat_contact import _registration_write, profile_digest
		from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log
		contact = self.contact({'name': 'Registered Person', 'unrelated': 'Preserved'})
		session = frappe.get_doc(dict(doctype='Chat Session', contact=contact.name,
			channel=contact.channel, account=contact.account, status='Active')).insert(ignore_permissions=True)
		turn = frappe.get_doc(dict(doctype='Chat Turn', event_key=self.prefix, contact=contact.name,
			session=session.name, status='Complete', received_at=utcnow(), event_at=utcnow())).insert(ignore_permissions=True)
		log = record_process_execution_log(process_method='messaging.presets.processes.registration.handle',
			source_name=self.prefix + '_registration', session=session.name, turn=turn.name,
			execution_id=self.prefix, execution_outcome='Functional Status', functional_status='registered',
			configuration_fingerprint='historical-configuration')
		contact.registration_status = 'Complete'
		contact.registration_receipt = json.dumps(dict(flow=self.prefix + '_registration', status='registered',
			revision='historical-revision', fields=['name'], digest=profile_digest({'name': 'Registered Person'}),
			execution_log=log.name, session=session.name, turn=turn.name, execution_id=self.prefix))
		with _registration_write():
			contact.save(ignore_permissions=True)
		return contact

	def test_registration_gate_follows_settings_and_persisted_contact_completion(self):
		pending = self.contact({})
		self.assertTrue(is_registration_complete(pending, frappe._dict(registration_enabled=0)))
		self.assertFalse(is_registration_complete(pending, frappe._dict(registration_enabled=1)))
		registered = self.registered_contact()
		self.assertTrue(is_registration_complete(registered, frappe._dict(registration_enabled=1)))
		self.assertTrue(is_registration_complete(registered, frappe._dict(registration_enabled=0)))

	def test_completed_registration_does_not_resolve_or_reexecute_changed_flow(self):
		registered = self.registered_contact()
		settings = frappe._dict(registration_enabled=1, registration_flow='another-flow')
		with patch('messaging.chatbot.doctype.bot_settings.flow_consumers.configuration',
			side_effect=AssertionError('Completed registration must not resolve the current Flow')):
			self.assertTrue(is_registration_complete(registered, settings))
			registered.parameter_values = json.dumps({'name': 'Registered Person', 'unrelated': 'Changed'})
			self.assertTrue(is_registration_complete(registered, settings))

	def test_registration_rejects_unverified_status_modified_values_and_foreign_receipts(self):
		from copy import deepcopy
		settings = frappe._dict(registration_enabled=1)
		registered = self.registered_contact()
		for changes in ({'registration_status': 'Pending'}, {'registration_receipt': None},
			{'parameter_values': json.dumps({'name': 'Another Person'})}):
			altered = deepcopy(registered)
			altered.update(changes)
			self.assertFalse(is_registration_complete(altered, settings))
		receipt = json.loads(registered.registration_receipt)
		for changes in ({'flow': 'foreign-flow'}, {'turn': 'foreign-turn'}, {'execution_id': 'foreign-execution'},
			{'status': 'another-status'}, {'fields': []}, {'fields': ['name', 'name']}):
			altered = deepcopy(registered)
			altered.registration_receipt = json.dumps(dict(receipt, **changes))
			self.assertFalse(is_registration_complete(altered, settings))
		foreign = self.contact({'name': 'Registered Person'})
		foreign.name = 'foreign-contact'
		foreign.registration_status = 'Complete'
		foreign.registration_receipt = registered.registration_receipt
		self.assertFalse(is_registration_complete(foreign, settings))

	def test_current_flow_side_effects_still_require_current_configuration(self):
		from messaging.chatbot.doctype.bot_flow.execution import require_execution_result
		registered = self.registered_contact()
		receipt = json.loads(registered.registration_receipt)
		flow = frappe._dict(name=receipt['flow'], process_definition=None)
		result = dict(functional=True, execution_log=receipt['execution_log'], status=receipt['status'])
		with patch('messaging.chatbot.doctype.bot_flow.execution.fingerprint', return_value='changed-configuration'):
			with self.assertRaises(frappe.ValidationError):
				require_execution_result(flow, result, session=receipt['session'], turn=receipt['turn'], execution_id=receipt['execution_id'])
