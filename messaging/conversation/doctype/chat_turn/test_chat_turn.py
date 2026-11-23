from messaging.chatbot.doctype.bot_settings.localization import text as settings_text
"""Integrated backend tests with isolated fixtures and real MariaDB transactions."""
import json
import multiprocessing
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import frappe

from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import claim, envelope, month_start, reconcile, simulate_dispatch
from messaging.common.primitives import utcnow
from messaging.conversation.doctype.chat_message.chat_message import dispatch_content
from messaging.chatbot.doctype.bot_settings.bot_settings import get_internal_error_message
from messaging.chatbot.doctype.bot_settings.test_bot_settings import restore_bot_settings
from messaging.conversation.doctype.chat_session.chat_session import read_state
from messaging.conversation.doctype.chat_turn.chat_turn import process_event, reconcile_execution


from messaging.communication.doctype.process_definition.test_process_definition import (
    controlled_process,
    controlled_values,
    controlled_human,
    controlled_other_status,
    controlled_invalid,
    controlled_exception,
    controlled_type_error,
    controlled_timeout,
    controlled_required,
    controlled_language_only,
    controlled_contracts_language,
    dialogue_handler,
    dialogue_result,
    controlled_values_with_other,
    controlled_mixed,
    controlled_capture,
    controlled_uncertain,
    controlled_consumer,
    CALLS,
    MIXED_CALLS,
    _dialogue_contracts,
    async_probe_handler,
    async_probe_result,
)

from messaging.registry.processes import BotProcess
from messaging.registry.processes import register_processes


def concurrent_worker(site, sites_path, request, barrier, results):
	"""Independent connection and transaction, synchronized immediately before ingestion."""
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.set_user("Administrator")
	try:
		barrier.wait(timeout=20)
		result = process_event(request)
		results.put(dict(state=result["state"], turn=result["turn"], contact=result["contact"],
						 dispatch=result["dispatch"], error=None))
	except Exception as exc:
		results.put(dict(error=type(exc).__name__))
	finally:
		frappe.db.rollback()
		frappe.destroy()


class TestChatTurn(unittest.TestCase):
	def setUp(self):
		from messaging.chatbot.doctype.ai_provider.test_ai_provider import install_synthetic_provider
		frappe.set_user("Administrator")
		install_synthetic_provider(self)
		# Frappe v16 uses this process flag, not local.flags.in_test, for synchronous cleanup.
		self.test_mode = patch.object(frappe, "in_test", True)
		self.test_mode.start()
		self.addCleanup(self.test_mode.stop)
		# Conversation fixtures isolate dialogue behavior from administrative provider certification.
		functional = patch('messaging.chatbot.doctype.ai_evaluation_run.runtime.approval', return_value={'approved': True})
		functional.start()
		self.addCleanup(functional.stop)
		self.inbox_enqueue = patch("messaging.conversation.doctype.chat_contact.chat_contact._enqueue")
		self.inbox_enqueue.start()
		self.addCleanup(self.inbox_enqueue.stop)
		frappe.set_user("Administrator")
		self.registration_language = patch("messaging.conversation.doctype.chat_session.chat_session.resolve_process_language", return_value="en")
		self.registration_language.start()
		self.addCleanup(self.registration_language.stop)
		self.settings_before = frappe.get_single("Bot Settings").as_dict()
		self.channels_before = {row.name: row for row in frappe.get_all("Communication Channel", fields=["*"])}
		self.fixture_channels = []
		self.configured_channels = set()
		self.quota_policy = dict(
			quotas_enabled=1, daily_quota_limit=20, monthly_quota_limit=60,
			quota_timezone="UTC", quota_consumption_basis="Provider Accepted",
			composite_response_single_unit=1, quota_reservation_ttl_seconds=120,
			reconcile_quota_on_acceptance=1, quota_courtesy_enabled=0, quota_courtesy_max_per_cycle=0,
		)
		self.enabled_flows_before = frappe.get_all("Bot Flow", filters={"status": "Enabled"}, pluck="name")
		if self.enabled_flows_before:
			frappe.db.set_value("Bot Flow", {"name": ["in", self.enabled_flows_before]}, "status", "Disabled", update_modified=False)
		from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings
		self.addCleanup(restore_bot_settings, self.settings_before, self.enabled_flows_before, self.channels_before, self.fixture_channels)
		configure_test_settings()
		self.prefix = "m2_"+frappe.generate_hash(length=10)
		self.accounts = [self.prefix]
		self.flows = []
		self.fixture_definitions = []
		self.fixture_templates = []
		self.fixture_test_logs = []
		self.parameters = []
		CALLS.clear()
		self.configure(human_attention_enabled=0, registration_enabled=0, quotas_enabled=1, daily_quota_limit=20,
					   exempt_registration_from_quotas=1, max_registration_exempt_messages=3,
					   monthly_quota_limit=60, quota_courtesy_enabled=0,
					   greeting_enabled=0,
					   informative_message="Choose an option.", informative_show_available_categories=1,
					   default_internal_error_message="Safe technical error.",
					   show_menu_on_greeting_only=0,
					    max_invalid_attempts=3, show_menu_on_attempts_exhausted=1,
					   attempts_exhausted_message="Attempts exhausted.")

	def tearDown(self):
		frappe.db.rollback()
		# The engine owns durable transactions, so every fixture has explicit scoped cleanup.
		frappe.set_user("Administrator")
		restore_bot_settings(self.settings_before, self.enabled_flows_before, self.channels_before, self.fixture_channels)
		contacts = frappe.get_all("Chat Contact", filters={"account": ["in", self.accounts]}, pluck="name")
		if contacts:
			turns = frappe.get_all("Chat Turn", filters={"contact": ["in", contacts]}, pluck="name")
			if turns:
				ai_logs = frappe.get_all("AI Request Log", filters={"turn":["in",turns]}, fields=["error_log"])
				for log in ai_logs:
					if log.error_log: frappe.db.delete("Error Log", {"name":log.error_log})
				frappe.db.delete("AI Request Log", {"turn":["in",turns]})
				frappe.db.delete("Error Log", {"method":"Messaging AI Evaluation Error", "reference_name":["in",turns]})
				frappe.db.delete("Error Log", {"method": "Messaging Interpretation Error",
					"reference_doctype": "Chat Turn", "reference_name": ["in", turns]})
				logs = frappe.get_all("Process Execution Log", filters={"turn": ["in", turns]}, fields=["name", "frappe_error_log"])
				for log in logs:
					if log.frappe_error_log:
						frappe.db.delete("Error Log", {"name": log.frappe_error_log})
				frappe.db.delete("Process Execution Log", {"turn": ["in", turns]})
			for dt in ("Chat Message", "Bot Dispatch", "Chat Turn", "Chat Session", "Chat Contact"):
				frappe.db.delete(dt, {"name" if dt == "Chat Contact" else "contact": ["in", contacts]})
		fixture_logs = frappe.get_all('Process Execution Log', filters={'name': ['in', getattr(self, 'fixture_test_logs', []) or ['']]}, fields=['name','frappe_error_log'])
		for name in reversed(self.flows):
			frappe.delete_doc("Bot Flow", name, delete_permanently=True)
		for log in fixture_logs:
			if log.frappe_error_log: frappe.db.delete('Error Log', {'name': log.frappe_error_log})
			frappe.db.delete('Process Execution Log', {'name': log.name})
		for name in getattr(self, "fixture_templates", []):
			frappe.delete_doc("Bot Flow Template", name, ignore_permissions=True)
		for definition in getattr(self, "fixture_definitions", []):
			frappe.db.delete("Process Definition", {"name": definition.id})
		if getattr(self, "fixture_definitions", None):
			from messaging.presets.register import register as setup_processes
			setup_processes()
		for name in self.parameters:
			frappe.delete_doc("Bot Parameter", name, delete_permanently=True)
		frappe.db.commit()

	def test_unconfigured_chatbot_receives_safely_without_ai_or_dispatch(self):
		from messaging.chatbot.doctype.bot_settings.test_bot_settings import TestEmptySettingsAndMatrices
		settings = TestEmptySettingsAndMatrices.empty(self)
		with patch.object(settings, 'get_doc_before_save', return_value=None):
			settings.save()
		frappe.db.commit()
		with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('No configured AI')):
			result = process_event(self.event(text='Hello'), interpret_natural=True)
		self.assertTrue(result['configuration_required'])
		self.assertFalse(result['dispatch'])
		self.assertFalse(result['response'])
		self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', result['turn'], 'decisions')),
			[dict(kind='Configuration Required')])
		self.assertEqual(frappe.db.get_value('Chat Turn', result['turn'], 'inbox_diagnostic'), 'automation_not_configured')

	def test_settings_restore_survives_fixture_cleanup_failure(self):
		self.flow()
		self.configure(informative_message="Temporary synthetic configuration.")
		with patch.object(frappe, "delete_doc", side_effect=RuntimeError("Synthetic cleanup failure")):
			with self.assertRaisesRegex(RuntimeError, "Synthetic cleanup failure"):
				TestChatTurn.tearDown(self)
		current = frappe.get_single("Bot Settings")
		for field in current.meta.fields:
			if field.fieldtype not in ("Tab Break", "Section Break", "Column Break", "HTML", "Table", "Table MultiSelect"):
				self.assertEqual(current.get(field.fieldname), self.settings_before.get(field.fieldname))
		self.assertTrue(frappe.in_test)

	def ensure_test_channel(self, channel_name="test"):
		if not isinstance(channel_name, str) or not channel_name.strip():
			return
		if not frappe.db.exists("Communication Channel", channel_name):
			doc = frappe.new_doc("Communication Channel")
			doc.name = channel_name
			doc.provider = "EMULR"
			doc.channel_name = f"Synthetic {channel_name}"
			doc.status = "Enabled"
			doc.update(self.quota_policy)
			doc.db_insert()
			self.fixture_channels.append(channel_name)
			self.configured_channels.add(channel_name)
			frappe.clear_document_cache("Communication Channel", channel_name)
			frappe.db.commit()
		elif channel_name not in self.configured_channels:
			frappe.db.set_value("Communication Channel", channel_name, self.quota_policy, update_modified=False)
			frappe.clear_document_cache("Communication Channel", channel_name)
			self.configured_channels.add(channel_name)
			frappe.db.commit()

	def configure(self, **values):
		from messaging.communication.doctype.communication_channel.communication_channel import QUOTA_POLICY_FIELDS
		settings = frappe.get_single("Bot Settings")
		from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS, text
		default = next(row.language for row in settings.chatbot_languages if row.is_default)
		if 'chatbot_languages' in values:
			# Synthetic fixtures explicitly carry their text into their chosen test language.
			previous = {key: text(settings, key, default) for key in MESSAGE_KEYS}
			settings.set('chatbot_languages', values['chatbot_languages'])
			allowed = {row.language for row in settings.chatbot_languages if row.enabled}
			settings.set('message_translations', [row for row in settings.message_translations if row.language in allowed])
			default = next(row.language for row in settings.chatbot_languages if row.is_default)
			for key in MESSAGE_KEYS:
				if not any(row.message_key == key and row.language == default for row in settings.message_translations):
					settings.append('message_translations', dict(message_key=key, language=default, message=previous[key]))
		quota_updates = {}
		for key, value in values.items():
			if key == 'chatbot_languages':
				continue
			if key in QUOTA_POLICY_FIELDS:
				quota_updates[key] = value
				continue
			if key not in MESSAGE_KEYS:
				settings.set(key, value)
			if key in MESSAGE_KEYS:
				row = next((row for row in settings.message_translations if row.message_key == key and row.language == default), None)
				if row is None:
					row = settings.append('message_translations', dict(message_key=key, language=default))
				row.message = value
		if quota_updates:
			self.quota_policy.update(quota_updates)
			if self.quota_policy.get("quotas_enabled"):
				self.quota_policy["quota_consumption_basis"] = "Provider Accepted"
				self.quota_policy["composite_response_single_unit"] = 1
				self.quota_policy["reconcile_quota_on_acceptance"] = 1
		for ch_name in frappe.get_all("Communication Channel", pluck="name"):
			frappe.db.set_value("Communication Channel", ch_name, self.quota_policy, update_modified=False)
			frappe.clear_document_cache("Communication Channel", ch_name)
			self.configured_channels.add(ch_name)
		# Controlled execution fixtures may intentionally retain an unconfigured interpreter.
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True, ignore_version=True)
		frappe.db.commit()
		return settings

	def phrase(self, name, value):
		parameter = frappe.get_doc('Bot Parameter', name)
		parameter.set('request_phrases', [dict(language=next(row.language for row in frappe.get_single('Bot Settings').chatbot_languages if row.is_default), request_phrase=value)])
		parameter.save()
		return name

	def parameter(self, kind=None, mask="##-@@", **values):
		code = self.prefix+"_p"+str(len(self.parameters))
		doc = frappe.get_doc(dict(doctype="Bot Parameter", name=code, label=code,
								  parameter_type=kind or "Expression", expression=mask if kind is None else None,
								  enabled=1, request_phrases=[dict(language=next(row.language for row in frappe.get_single("Bot Settings").chatbot_languages if row.is_default), request_phrase="Please provide " + code + ".")], **values)).insert()
		self.parameters.append(doc.name)
		frappe.db.commit()
		return doc

	def flow(self, parameters=None, priority=None, method="controlled_process", test_values=None, output_fields=None, handler=None, business=None, **values):
		code = self.prefix+"_f"+str(len(self.flows))
		path = __name__+"."+method
		parameters = parameters or []
		if priority is None:
			priority = 0 if not parameters else 100000+int(frappe.generate_hash(length=6), 16)
		payload = dict(doctype="Bot Flow", workflow_code=code, workflow_name=code,
					   semantic_description="Controlled test Flow.", status="Enabled", priority=priority,
					   parameters=parameters, process_method=path, show_in_menu=1,
					   outputs=[dict(status_code="success", output_mode="Template", body="Result {{result}}.")])
		if parameters:
			payload.update(input_initial_message="Initial input.", input_continuation_message="Continuation input.")
		payload.update(values)
		payload['parameters'] = [dict(allow_multiple_values=0, minimum_values=0, maximum_values=0) | row
			for row in payload['parameters']]
		identifier = 'messaging.' + code + '_process'
		inputs = [dict(name=row.get('process_argument') or row['parameter'],
			type={'Int': 'integer', 'Date': 'date'}.get(frappe.get_doc('Bot Parameter', row['parameter']).parameter_type, 'string'),
			multiple=bool(row.get('allow_multiple_values')),
			required=bool(row.get('required', 1))) for row in payload['parameters']]
		default_template = 'outputs' not in values and not output_fields
		if method == 'controlled_values':
			fields = {'count': 'integer'}
		elif default_template or method in ('controlled_process', 'controlled_required', 'controlled_timeout'):
			fields = {'result': 'string', 'optional': 'string'}
		else:
			fields = {}
		_dialogue_contracts[identifier] = dict(method=method, inputs=inputs,
			outputs=output_fields or {row['status_code']: dict(fields) for row in payload['outputs']},
            actions={row['status_code']: {'Complete Flow': 'complete_flow', 'Cancel Flow': 'cancel_flow', 'Retry Parameter': 'retry_parameter'}.get(row.get('flow_action'), 'complete_flow') for row in payload['outputs']})
		definitions = getattr(self, 'fixture_definitions', [])
		definitions.append(BotProcess(id=identifier, label='Synthetic dialogue Process',
			definition=handler or dialogue_handler, handler=business or dialogue_result))
		self.fixture_definitions = definitions
		from messaging.registry import load_declarations
		from messaging.presets import register as defaults
		default_definitions = lambda: load_declarations(defaults.__file__, 'processes')
		register_processes(app='messaging', processes=[*default_definitions(), *definitions])
		payload['process_definition'] = identifier
		payload.pop('process_method', None)
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		language = resolve_process_language()
		localized_templates = {}
		for row in payload['outputs']:
			body = row.pop('body', None)
			reference = row.pop('template_reference', None)
			if body:
				from messaging.communication.doctype.communication_template.communication_template import extract_component_variables
				template = frappe.get_doc(dict(doctype='Bot Flow Template', template_name=code, enabled=1, body=body, language=language,
					template_variables=[dict(parameter_name=ref) for ref in extract_component_variables(body, 'Body')])).insert()
				self.fixture_templates.append(template.name)
				reference = template.name
			if reference: localized_templates[row['status_code']] = reference
		doc = frappe.new_doc("Bot Flow")
		doc.update(payload)
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, set_active
		language = resolve_process_language()
		doc.append('translations', dict(language=language, semantic_description=doc.semantic_description,
			menu_label=doc.get("menu_label") or doc.workflow_name))
		if parameters:
			doc.append('input_translations', dict(language=language,
				input_initial_message=doc.input_initial_message, input_continuation_message=doc.input_continuation_message))
		for row in doc.outputs:
			if row.status_code in localized_templates:
				doc.append('output_translations', dict(language=language, status_code=row.status_code, template_reference=localized_templates[row.status_code]))
		wanted_active = doc.status == "Enabled"
		doc.status = "Untested"
		doc.insert()
		approval = validate_process(doc.as_dict())
		if approval.get('execution_log'): self.fixture_test_logs.append(approval['execution_log'])
		self.assertEqual(approval['status'], 'Passed', approval)
		doc.reload()
		if wanted_active:
			set_active(doc.as_dict(), 1)
			doc.reload()

		self.flows.append(doc.name)
		frappe.db.commit()
		return doc

	def event(self, event_id=None, **values):
		request = dict(channel="test", account=self.prefix, sender="external-user", event_id=event_id or frappe.generate_hash(),
					   event_at=datetime.now(timezone.utc).isoformat())
		request.update(values)
		self.ensure_test_channel(request.get("channel"))
		return request

	def send(self, **values):
		return process_event(self.event(**values))

	def state(self, result):
		return read_state(frappe.get_doc("Chat Session", result["session"]), "dialogue_state")

	def test_multiple_flows_continuation_capture_reuse_and_duplicate(self):
		param = self.parameter()
		zero = self.flow()
		first = self.flow(parameters=[dict(parameter=param.name, process_argument="value", required=1, capture_order=1)])
		second = self.flow(parameters=[dict(parameter=param.name, process_argument="value", required=1, capture_order=1)], priority=first.priority+1)
		request = self.event(flows=[second.name, first.name, zero.name])
		result = process_event(request)
		self.assertIn("Result hours.\n\nInitial input.\nPlease provide " + param.name + ".", result["response"])
		self.assertEqual([item["flow"] for item in self.state(result)["queue"]], [first.name, second.name])
		self.assertEqual(CALLS, [None])
		self.assertEqual(process_event(request)["dispatch"], result["dispatch"])
		self.assertEqual(CALLS, [None])
		simulate_dispatch(result["dispatch"])
		invalid = self.send(text="invalid")
		self.assertIn("invalid or ambiguous", invalid["response"])
		self.assertEqual(len(CALLS), 1)
		valid = self.send(text="12-AB")
		self.assertEqual(valid["response"], "Result 12-AB.\n\nResult 12-AB.")
		self.assertEqual(CALLS, [None, "12-AB", "12-AB"])
		self.assertEqual(self.state(valid)["queue"], [])
		self.assertEqual(self.state(valid)["completed_count"], 0)
		self.assertNotIn("active_request", self.state(valid))
		accepted = simulate_dispatch(valid["dispatch"])
		self.assertEqual(accepted["state"], "Accepted")
		self.assertEqual(simulate_dispatch(valid["dispatch"])["outbound_id"], accepted["outbound_id"])
		self.assertEqual(frappe.db.count("Process Execution Log", {"session": valid["session"]}), 3)
		log = frappe.get_last_doc("Process Execution Log", filters={"turn": valid["turn"]})
		self.assertNotIn("12-AB", log.redacted_arguments_json)
		self.assertNotIn("12-AB", log.response_summary_json)


	def test_identity_isolation_stale_event_human_pause_and_expiration(self):
		param = self.parameter()
		flow = self.flow(parameters=[dict(parameter=param.name, process_argument="value", required=1, capture_order=1)])
		original = self.send(flows=[flow.name])
		self.assertIn("Initial input.", original["response"])
		earlier = self.send(text="12-AB", event_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
		self.assertEqual(earlier["state"], "Ignored")
		self.assertEqual(self.state(original)["values"], {})
		other_account = self.prefix+"other"
		self.accounts.append(other_account)
		other = self.send(account=other_account, parameters={param.name: "45-CD"}, flows=[flow.name])
		other_channel = self.send(channel="another")
		self.assertNotEqual(original["contact"], other["contact"])
		self.assertNotEqual(original["contact"], other_channel["contact"])
		frappe.db.set_value("Chat Session", original["session"], "expires_at", utcnow()-timedelta(minutes=1))
		frappe.db.commit()
		resumed = self.send(text="56-EF")
		self.assertEqual(resumed["session"], original["session"])
		self.assertEqual(resumed["response"], "Result 56-EF.")
		frappe.db.set_value("Chat Session", original["session"], "status", "Human Active")
		frappe.db.commit()
		paused = self.send(flows=[flow.name], parameters={param.name: "77-GH"})
		self.assertEqual(paused["state"], "Human Paused")
		self.assertIsNone(paused["dispatch"])

	def test_greeting_menu_informative_and_direct_flow(self):
		from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
		flow = self.flow()
		greeting = self.flow(show_in_menu=0, outputs=[dict(status_code="success", output_mode="Template", body="Hello!")])
		self.configure(greeting_enabled=1, greeting_flow=greeting.name, show_menu_on_greeting_only=1,
			greeting_states=[dict(state=state_name(greeting.name, 'success'))])
		alone = self.send(flows=[greeting.name])
		self.assertIn("Hello!", alone["response"])
		self.assertIn("1. " + flow.workflow_name, alone["response"])
		info = self.send(text="unrecognized natural language")
		self.assertIn("Choose an option.", info["response"])
		self.assertNotIn("Hello", info["response"])
		direct = self.send(sender="direct-flow", menu_selection=flow.name)
		self.assertEqual(direct["response"], "Result hours.")
		combined = self.send(sender="another-user", flows=[greeting.name, flow.name])
		self.assertEqual(combined["response"], "Hello!\n\nResult hours.")
		self.assertNotIn("1. " + flow.workflow_name, combined["response"])

	def test_deterministic_capture_and_optional_signature(self):
		a = self.parameter("Int", min_value=1, max_value=99)
		b = self.parameter()
		flow = self.flow(parameters=[dict(parameter=a.name, process_argument="value", required=1, capture_order=1),
			dict(parameter=b.name, process_argument="optional", required=1, capture_order=2)])
		self.send(flows=[flow.name])
		partial = self.send(text="11-CD")
		self.assertEqual(self.state(partial)["values"], {b.name: "11-CD"})
		complete = self.send(text="7")
		self.assertEqual(complete["response"], "Result 7.")
		optional = self.flow(parameters=[dict(parameter=a.name, process_argument="value", required=1, capture_order=1),
			dict(parameter=b.name, process_argument="optional", required=0, capture_order=2)])
		result = self.send(sender="optional", flows=[optional.name], parameters={a.name: 8})
		self.assertEqual(result["response"], "Result 8.")


	def test_expression_lists_and_persisted_values_preserve_text(self):
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import valid_values
		parameter, flow = self.values_flow()
		self.send(flows=[flow.name])
		complete = self.send(text="00123 y SC-12345")
		self.assertEqual(CALLS, [["00123", "SC-12345"]])
		self.assertEqual(valid_values({parameter.name: ["00123", "SC-12345"]}, [parameter.name]),
			{parameter.name: ["00123", "SC-12345"]})
		self.assertEqual(valid_values({parameter.name: [123, "SC-12345"]}, [parameter.name]), {})
		self.assertEqual(self.state(complete)["queue"], [])

	def test_language_injection_without_capture_and_with_multiple_expression_values(self):
		self.configure(chatbot_languages=[dict(language="es", enabled=1, is_default=1)], interpretation_enabled=0)
		outputs = [dict(status_code="success", output_mode="Message")]
		flow = self.flow(method="controlled_language_only", outputs=outputs)
		complete = self.send(flows=[flow.name])
		self.assertEqual(complete["response"], "Language: es")
		self.assertEqual(CALLS, [dict(language="es")])
		parameter = self.parameter(mask="#####\nSC-#####")
		flow = self.flow(method="controlled_contracts_language", outputs=outputs,
			parameters=[dict(parameter=parameter.name, process_argument="contracts", required=1,
				capture_order=1, allow_multiple_values=1)])
		self.send(flows=[flow.name])
		self.assertEqual(len(CALLS), 1)
		complete = self.send(text="00123 y SC-67890")
		self.assertEqual(CALLS[-1], dict(contracts=["00123", "SC-67890"], language="es"))
		self.assertEqual(complete["response"], "Language: es")
		self.assertEqual(self.state(complete)["queue"], [])

	def test_runtime_ambiguous_values_and_constraint_revalidation(self):
		string = self.parameter("String")
		integer = self.parameter("Int", min_value=1, max_value=50)
		flow = self.flow(parameters=[dict(parameter=integer.name, process_argument="value", required=1, capture_order=1),
									dict(parameter=string.name, process_argument="optional", required=1, capture_order=2)])
		self.send(flows=[flow.name])
		result = self.send(text="99")
		self.assertIn("invalid or ambiguous", result["response"])
		self.assertEqual(self.state(result)["values"], {})
		done = self.send(parameters={string.name: "label", integer.name: 12})
		self.assertEqual(done["response"], "Result 12.")
		frappe.db.set_value("Bot Parameter", integer.name, "max_value", 10)
		frappe.clear_document_cache("Bot Parameter", integer.name)
		from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, set_active
		flow.reload()
		approval = validate_process(flow.as_dict())
		if approval.get("execution_log"):
			self.fixture_test_logs.append(approval["execution_log"])
		flow.reload()
		set_active(flow.as_dict(), 1)
		frappe.db.commit()
		again = self.send(flows=[flow.name])
		self.assertIn(integer.name, again["response"])
		self.assertNotIn(string.name, again["response"])
		self.assertEqual(self.state(again)["values"], {})

	def test_process_modes_failures_safe_logs_and_recovery(self):
		transport = patch("messaging.chatbot.doctype.bot_dispatch.delivery.deliver", side_effect=simulate_dispatch)
		transport.start(); self.addCleanup(transport.stop)
		for method, expected in [("controlled_invalid", "Invalid Schema Response"),
								 ("controlled_required", "Internal Execution Error"),
								 ("missing_method", "Internal Execution Error"),
								 ("controlled_other_status", "Invalid Schema Response")]:
			with self.subTest(method=method):
				flow = self.flow(method=method)
				result = self.send(sender=method, flows=[flow.name])
				self.assertEqual(result["response"], get_internal_error_message())
				log = frappe.get_last_doc("Process Execution Log", filters={"turn": result["turn"]})
				self.assertEqual(log.execution_outcome, expected)
				self.assertEqual((log.source_type,log.source_name),("Bot Flow",flow.name))
				self.assertEqual(frappe.db.count("Process Execution Log",{"turn":result["turn"]}),1)
				self.assertTrue(log.frappe_error_log)
				self.assertTrue(log.execution_id)
		functional = self.flow(method="controlled_other_status", outputs=[dict(status_code="unavailable",output_mode="Message")])
		self.assertEqual(self.send(flows=[functional.name])["response"], "Unavailable")
		not_whitelisted = self.flow(method="controlled_other_status", process_invocation_mode="Whitelisted API")
		self.assertEqual(self.send(sender="rejected-mode", flows=[not_whitelisted.name])["response"], get_internal_error_message())
		whitelist = self.flow(process_invocation_mode="Whitelisted API", outputs=[dict(status_code="success",output_mode="Message")])
		self.assertEqual(self.send(flows=[whitelist.name])["response"], "Process reply")
		self.configure()
		allowed = self.send(flows=[whitelist.name])
		self.assertEqual(allowed["response"], "Process reply")
		failing = self.flow(method="controlled_exception")
		queued = self.flow()
		request = self.event(sender="failure", flows=[failing.name, queued.name])
		result = process_event(request)
		self.assertEqual(result["state"], "Recovery Required")
		self.assertEqual(CALLS.count("exception"), 1)
		self.assertEqual(process_event(request)["turn"], result["turn"])
		later = self.send(sender="failure", flows=[queued.name])
		self.assertEqual(later["state"], "Recovery Required")
		self.assertIsNone(later["dispatch"])
		self.assertEqual(later["session"], result["session"])
		self.assertEqual(CALLS.count("exception"), 1)
		logs = frappe.get_all("Process Execution Log", filters={"turn": result["turn"]}, fields=["diagnostic_detail","response_summary_json","redacted_arguments_json","frappe_error_log"])
		self.assertNotIn("private-value", str(logs))
		error_text = frappe.db.get_value("Error Log", logs[0].frappe_error_log, "error")
		self.assertNotIn("secret", error_text)
		with patch("messaging.chatbot.doctype.bot_dispatch.delivery.deliver", side_effect=simulate_dispatch):
			simulate_dispatch(result["dispatch"])
		self.assertEqual(frappe.db.get_value("Chat Session", result["session"], "status"), "Closed")
		held = frappe.get_doc("Chat Turn", later["turn"])
		self.assertEqual(held.inbox_state, "Pending")
		from messaging.conversation.doctype.chat_turn.chat_turn import pending_input_request
		resumed = process_event(pending_input_request(held), interpret_natural=True, _resume_turn=held.name)
		self.assertEqual(resumed["state"], "Complete")
		self.assertNotEqual(resumed["session"], result["session"])
		self.assertIn("Result hours.", resumed["response"])
		self.assertEqual(CALLS.count("exception"), 1)

	def test_durable_crash_marker_prevents_retry_and_preserves_queue(self):
		first = self.flow()
		second = self.flow()
		request = self.event(flows=[first.name, second.name])
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke", side_effect=SystemExit("crash")):
			with self.assertRaises(SystemExit):
				process_event(request)
		# Disconnect-like rollback: the Running checkpoint was committed before the interruption.
		frappe.db.rollback()
		result = process_event(request)
		self.assertEqual(result["state"], "Recovery Required")
		self.assertEqual(len(self.state(result)["queue"]), 2)
		self.assertEqual(CALLS, [])
		self.assertIsNone(claim(result["dispatch"]))
		execution = json.loads(frappe.get_doc("Chat Turn", result["turn"]).executions)[0]
		reconciled = reconcile_execution(result["turn"], execution["id"], dict(version=1, recipient="",status="success",data=dict(result="verified",optional="default"),message=""))
		self.assertEqual(reconciled["response"], "Result verified.")
		self.assertEqual(reconciled["dispatch_state"], "Prepared")

	def test_verified_final_result_completes_without_replay_or_extra_dispatch(self):
		flow = self.flow()
		request = self.event(flows=[flow.name])
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke", side_effect=SystemExit):
			with self.assertRaises(SystemExit):
				process_event(request)
		frappe.db.rollback()
		uncertain = process_event(request)
		execution = json.loads(frappe.get_doc("Chat Turn", uncertain["turn"]).executions)[0]["id"]
		response = dict(version=1, recipient="", status="success", data=dict(result="verified", optional="default"), message="")
		resolved = reconcile_execution(uncertain["turn"], execution, response)
		self.assertEqual(resolved["response"], "Result verified.")
		self.assertEqual(resolved["state"], "Complete")
		self.assertEqual(resolved["dispatch"], uncertain["dispatch"])
		self.assertEqual(self.state(resolved)["queue"], [])
		self.assertEqual(self.state(resolved)["completed_count"], 0)
		self.assertNotIn("active_request", self.state(resolved))
		self.assertEqual(reconcile_execution(uncertain["turn"], execution, response), resolved)
		self.assertEqual(process_event(request), resolved)
		self.assertEqual(CALLS, [])
		self.assertEqual(frappe.db.count("Process Execution Log", {"turn": resolved["turn"]}), 1)
		self.assertEqual(frappe.db.count("Bot Dispatch", {"turn": resolved["turn"]}), 1)
		self.assertEqual(frappe.db.count("Chat Message", {"turn": resolved["turn"], "kind": "Automatic"}), 1)
		accepted = simulate_dispatch(resolved["dispatch"])
		self.assertEqual(simulate_dispatch(resolved["dispatch"])["outbound_id"], accepted["outbound_id"])
		self.assertEqual(frappe.db.count("Bot Dispatch", {"contact": resolved["contact"], "status": "Accepted"}), 1)

	def test_cancel_result_keeps_its_message_without_an_extra_dispatch(self):
		flow = self.flow(outputs=[dict(status_code="success", output_mode="Message", flow_action="Cancel Flow")])
		request = self.event(flows=[flow.name])
		result = process_event(request)
		self.assertEqual(result["response"], "Process reply")
		self.assertEqual(result["state"], "Complete")
		self.assertEqual(self.state(result)["queue"], [])
		self.assertEqual(process_event(request), result)
		self.assertEqual(CALLS, [None])
		self.assertEqual(frappe.db.count("Bot Dispatch", {"turn": result["turn"]}), 1)
		self.assertEqual(frappe.db.count("Process Execution Log", {"turn": result["turn"]}), 1)

	def test_empty_functional_presentation_does_not_fabricate_a_response(self):
		flow = self.flow()
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke", return_value=dict(
			state="Complete", status="success", functional=True, text="", flow_action="Complete Flow")), patch(
			"messaging.conversation.doctype.chat_session.chat_session.compose_informative_response",
			side_effect=AssertionError("A resolved functional output cannot fabricate a response")):
			result = self.send(flows=[flow.name])
		self.assertIsNone(result["response"])
		self.assertEqual(result["state"], "Complete")
		self.assertEqual(self.state(result)["queue"], [])
		self.assertEqual(self.state(result)["completed_count"], 0)
		self.assertEqual(CALLS, [])

	def test_process_timeout_is_uncertain_without_automatic_retry(self):
		flow = self.flow(method="controlled_timeout")
		request = self.event(flows=[flow.name])
		with patch("messaging.communication.doctype.process_definition.execution.PROCESS_TIMEOUT_SECONDS", 0.5):
			result = process_event(request)
		self.assertEqual(result["state"], "Recovery Required")
		self.assertEqual(result["response"], get_internal_error_message())
		self.assertEqual(process_event(request)["turn"], result["turn"])
		self.assertEqual(CALLS, [])

	def test_transport_accept_reject_unknown_limit_and_idempotence(self):
		first = self.send()
		self.assertEqual(simulate_dispatch(first["dispatch"], "Rejected")["state"], "Rejected")
		self.assertIsNone(claim(first["dispatch"]))
		second = self.send()
		simulate_dispatch(second["dispatch"], "Unknown")
		frappe.db.set_value("Bot Dispatch", second["dispatch"], "review_after", utcnow()-timedelta(days=2));frappe.db.commit()
		self.assertEqual(simulate_dispatch(second["dispatch"])["state"], "Unknown")
		accepted = reconcile(second["dispatch"], "Accepted")
		self.assertEqual(accepted["state"], "Accepted")
		stamp = frappe.db.get_value("Bot Dispatch", second["dispatch"], "accepted_at")
		reconcile(second["dispatch"], "Accepted")
		self.assertEqual(frappe.db.get_value("Bot Dispatch", second["dispatch"], "accepted_at"), stamp)
		with self.assertRaises(frappe.ValidationError):
			reconcile(second["dispatch"], "Rejected")
		limited = self.send(max_text_length=5)
		self.assertEqual(limited["dispatch_state"], "Held")
		self.assertIsNone(claim(limited["dispatch"]))
		self.assertEqual(envelope(second["dispatch"])["destination"], "external-user")

	def test_quota_exhaustion_courtesy_and_functional_renewal(self):
		flow = self.flow()
		self.configure(daily_quota_limit=1, monthly_quota_limit=10, quota_courtesy_enabled=1,
					   quota_courtesy_max_per_cycle=1, quota_courtesy_message="Quota notice.")
		first = self.send()
		simulate_dispatch(first["dispatch"])
		blocked = self.send(flows=[flow.name])
		self.assertEqual(blocked["response"], "Quota notice.")
		self.assertEqual(CALLS, [])
		simulate_dispatch(blocked["dispatch"])
		self.assertIsNone(self.send()["dispatch"])
		self.assertEqual(self.state(blocked)["queue"][0]["flow"], flow.name)
		# Availability returns, then an accepted functional Output starts a new courtesy cycle.
		frappe.db.set_value("Bot Dispatch", first["dispatch"], "accepted_at", utcnow()-timedelta(hours=25));frappe.db.commit()
		functional = self.send()
		self.assertEqual(functional["response"], "Result hours.")
		simulate_dispatch(functional["dispatch"])
		self.assertEqual(frappe.db.get_value("Chat Contact", functional["contact"], "quota_courtesy_cycle"), 1)
		again = self.send()
		self.assertEqual(again["response"], "Quota notice.")

	def test_per_channel_policy_and_per_account_independent_quota_consumption(self):
		flow = self.flow()
		self.configure(quota_courtesy_message="Shared courtesy from Bot Settings.")
		self.ensure_test_channel("channel_strict")
		self.ensure_test_channel("channel_relaxed")
		self.ensure_test_channel("channel_unmetered")
		frappe.db.set_value("Communication Channel", "channel_strict", dict(
			quotas_enabled=1, daily_quota_limit=1, monthly_quota_limit=5, quota_timezone="UTC",
			quota_reservation_ttl_seconds=60, quota_courtesy_enabled=1, quota_courtesy_max_per_cycle=1,
		), update_modified=False)
		frappe.db.set_value("Communication Channel", "channel_relaxed", dict(
			quotas_enabled=1, daily_quota_limit=2, monthly_quota_limit=10, quota_timezone="UTC",
			quota_reservation_ttl_seconds=60, quota_courtesy_enabled=0, quota_courtesy_max_per_cycle=0,
		), update_modified=False)
		frappe.db.set_value("Communication Channel", "channel_unmetered", dict(
			quotas_enabled=0, daily_quota_limit=0, monthly_quota_limit=0, quota_timezone="",
			quota_reservation_ttl_seconds=60, quota_courtesy_enabled=0, quota_courtesy_max_per_cycle=0,
		), update_modified=False)
		for ch_name in ("channel_strict", "channel_relaxed", "channel_unmetered"):
			frappe.clear_document_cache("Communication Channel", ch_name)
		frappe.db.commit()

		acct_a = self.prefix + "_acct_a"
		acct_b = self.prefix + "_acct_b"
		acct_relaxed = self.prefix + "_acct_rel"
		acct_unmetered = self.prefix + "_acct_unm"
		self.accounts.extend([acct_a, acct_b, acct_relaxed, acct_unmetered])

		# 1. Exhaust quota on (channel_strict, acct_a, sender_1) -> next gets courtesy -> next gets None
		r1 = self.send(channel="channel_strict", account=acct_a, sender="sender-1", flows=[flow.name])
		self.assertEqual(r1["response"], "Result hours.")
		self.assertEqual(frappe.db.get_value("Bot Dispatch", r1["dispatch"], "quota_kind"), "Standard")
		simulate_dispatch(r1["dispatch"])

		r2 = self.send(channel="channel_strict", account=acct_a, sender="sender-1", flows=[flow.name])
		self.assertEqual(r2["response"], "Shared courtesy from Bot Settings.")
		self.assertEqual(frappe.db.get_value("Bot Dispatch", r2["dispatch"], "quota_kind"), "Courtesy")
		simulate_dispatch(r2["dispatch"])

		r3 = self.send(channel="channel_strict", account=acct_a, sender="sender-1", flows=[flow.name])
		self.assertIsNone(r3["dispatch"])

		# 2. Different contact on the SAME account (channel_strict, acct_a, sender_2) is NOT blocked
		other_contact = self.send(channel="channel_strict", account=acct_a, sender="sender-2", flows=[flow.name])
		self.assertEqual(other_contact["response"], "Result hours.")
		simulate_dispatch(other_contact["dispatch"])

		# 3. SAME sender on a DIFFERENT account of the SAME channel (channel_strict, acct_b, sender_1) has independent quota
		other_acct = self.send(channel="channel_strict", account=acct_b, sender="sender-1", flows=[flow.name])
		self.assertEqual(other_acct["response"], "Result hours.")
		self.assertNotEqual(other_acct["contact"], r1["contact"])
		simulate_dispatch(other_acct["dispatch"])
		other_acct_courtesy = self.send(channel="channel_strict", account=acct_b, sender="sender-1", flows=[flow.name])
		self.assertEqual(other_acct_courtesy["response"], "Shared courtesy from Bot Settings.")
		simulate_dispatch(other_acct_courtesy["dispatch"])

		# 4. SAME sender on a DIFFERENT channel (channel_relaxed, daily_quota_limit=2, courtesy=0) uses that channel's policy
		rel1 = self.send(channel="channel_relaxed", account=acct_relaxed, sender="sender-1", flows=[flow.name])
		self.assertEqual(rel1["response"], "Result hours.")
		simulate_dispatch(rel1["dispatch"])
		rel2 = self.send(channel="channel_relaxed", account=acct_relaxed, sender="sender-1", flows=[flow.name])
		self.assertEqual(rel2["response"], "Result hours.")
		simulate_dispatch(rel2["dispatch"])
		rel3 = self.send(channel="channel_relaxed", account=acct_relaxed, sender="sender-1", flows=[flow.name])
		self.assertIsNone(rel3["dispatch"])

		# 5. Unmetered channel (quotas_enabled=0) allows unlimited replies with quota_kind='Unmetered'
		for idx in range(3):
			unm = self.send(channel="channel_unmetered", account=acct_unmetered, sender="sender-1", flows=[flow.name])
			self.assertEqual(unm["response"], "Result hours.")
			self.assertEqual(frappe.db.get_value("Bot Dispatch", unm["dispatch"], "quota_kind"), "Unmetered")
			simulate_dispatch(unm["dispatch"])


	def test_real_database_concurrency_quota_and_duplicate_event(self):
		self.configure(daily_quota_limit=1, monthly_quota_limit=1)
		flow = self.flow()
		context = multiprocessing.get_context("spawn")
		for duplicate in (False, True):
			with self.subTest(duplicate=duplicate):
				sender = "duplicate" if duplicate else "distinct"
				one = self.event(sender=sender, flows=[flow.name])
				two = one if duplicate else self.event(sender=sender, flows=[flow.name])
				barrier = context.Barrier(2)
				queue = context.Queue()
				workers = [context.Process(target=concurrent_worker, args=(frappe.local.site, frappe.local.sites_path, req, barrier, queue)) for req in [one,two]]
				for worker in workers:worker.start()
				results = [queue.get(timeout=30) for worker in workers]
				for worker in workers:
					worker.join(timeout=5)
					if worker.is_alive():worker.terminate();worker.join()
					self.assertEqual(worker.exitcode, 0)
				self.assertEqual([result["error"] for result in results], [None,None])
				self.assertEqual(len({result["contact"] for result in results}), 1)
				self.assertEqual(len({result["turn"] for result in results}), 1 if duplicate else 2)
				self.assertEqual(len({result["dispatch"] for result in results if result["dispatch"]}), 1)
				frappe.db.rollback()
				self.assertEqual(frappe.db.count("Bot Dispatch", {"contact": results[0]["contact"]}), 1)
				self.assertEqual(frappe.db.count("Process Execution Log", {"session": frappe.db.get_value("Chat Contact", results[0]["contact"], "active_session")}), 1)


	def test_quota_time_windows_and_unknown_reservations_survive_expiration(self):
		self.configure(daily_quota_limit=1, monthly_quota_limit=20)
		base = datetime(2026, 10, 10, 12)
		def at(stamp, **values):
			with patch("messaging.conversation.doctype.chat_turn.chat_turn.utcnow", return_value=stamp):
				return self.send(event_at=stamp.replace(tzinfo=timezone.utc).isoformat(), **values)
		first = at(base)
		claim(first["dispatch"])
		reconcile(first["dispatch"], "Accepted", now=base)
		self.assertIsNone(at(base+timedelta(hours=23,minutes=59))["dispatch"])
		# Exactly 24 hours is outside the rolling interval (now-24h, now].
		next_day = at(base+timedelta(hours=24))
		self.assertIsNotNone(next_day["dispatch"])
		claim(next_day["dispatch"])
		reconcile(next_day["dispatch"], "Unknown")
		pending = at(base+timedelta(days=90))
		self.assertIsNone(pending["dispatch"])
		reconcile(next_day["dispatch"], "Rejected")
		self.assertIsNotNone(at(base+timedelta(days=90,minutes=1))["dispatch"])

		self.configure(daily_quota_limit=2, monthly_quota_limit=2, quota_timezone="America/Tegucigalpa")
		boundary = datetime(2026,11,1,6)
		self.assertEqual(month_start(boundary-timedelta(seconds=1), "America/Tegucigalpa"), datetime(2026,10,1,6))
		self.assertEqual(month_start(boundary, "America/Tegucigalpa"), boundary)
		for stamp in [boundary-timedelta(days=2), boundary-timedelta(minutes=2)]:
			result = at(stamp,sender="monthly")
			claim(result["dispatch"]);reconcile(result["dispatch"], "Accepted", now=stamp)
		self.assertIsNone(at(boundary-timedelta(minutes=1),sender="monthly")["dispatch"])
		self.assertIsNotNone(at(boundary,sender="monthly")["dispatch"])

	def test_metadata_unique_indexes_and_permission_boundary(self):
		from frappe.desk.form.load import getdoctype
		for dt, key, module in [("Chat Contact","identity_key","Conversation"), ("Chat Session","active_key","Conversation"),
								("Chat Turn","event_key","Conversation"), ("Bot Dispatch","outbound_key","Chatbot")]:
			meta = frappe.get_meta(dt)
			self.assertEqual(meta.module,module)
			self.assertTrue(any(row.fieldtype == "Tab Break" for row in meta.fields))
			getdoctype(dt)
			indexes=frappe.db.sql(f"SHOW INDEX FROM `tab{dt}`",as_dict=True)
			self.assertTrue(any(row.Column_name == key and not row.Non_unique for row in indexes))
			self.assertFalse(frappe.has_permission(dt,"read",user="Guest"))
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				self.send()
		finally:
			frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			process_event(self.event(process_method="os.system"))

	def values_flow(self, **options):
		parameter = self.parameter(mask="#####\nSC-#####")
		flow = self.flow(parameters=[dict(parameter=parameter.name, process_argument="values", required=1,
			capture_order=1, allow_multiple_values=1)], method="controlled_values",
			outputs=[dict(status_code="success", output_mode="Message", flow_action="Complete Flow"),
				dict(status_code="invalid", output_mode="Message", flow_action="Retry Parameter", retry_parameter=parameter.name)], **options)
		return parameter, flow

	def test_multiple_initial_continuation_order_and_new_execution(self):
		p, flow = self.values_flow()
		for index, (text, expected) in enumerate([("Consulta SC-12345, 67890 y sc-12345.", ["SC-12345", "67890"]),
			("12345", ["12345"]), ("12345 y SC-12346.", ["12345", "SC-12346"]) ]):
			start = self.send(sender=str(index), flows=[flow.name], text=text if index == 0 else "")
			done = start if index == 0 else self.send(sender=str(index), text=text)
			self.assertEqual(CALLS[-1], expected)
			self.assertIn("Values: " + ", ".join(expected), done["response"])
			self.assertNotIn(settings_text(frappe.get_single("Bot Settings"), "informative_categories_header"), done["response"])
			self.assertEqual(self.state(done)["values"], {})
			self.assertIsNone(frappe.db.get_value("Chat Session", done["session"], "active_flow"))
			self.assertEqual(frappe.db.get_value("Chat Session", done["session"], "status"), "Active")
			simulate_dispatch(done["dispatch"])
			fresh = self.send(sender=str(index), flows=[flow.name])
			self.assertIn("Initial input", fresh["response"])
			self.assertEqual(self.state(fresh)["queue"][0]["invalid_attempts"], 0)

	def test_three_format_rejections_duplicate_and_restart(self):
		p, flow = self.values_flow()
		start = self.send(flows=[flow.name])
		for count in range(1, 4):
			request = self.event(text="123")
			result = process_event(request)
			self.assertEqual(process_event(request), result)
			decisions = json.loads(frappe.get_doc("Chat Turn", result["turn"]).decisions)
			self.assertEqual([d["count"] for d in decisions if d["kind"] == "Invalid Attempt"], [count])
			if count < 3:
				self.assertEqual(self.state(result)["queue"][0]["invalid_attempts"], count)
			else:
				self.assertIn("Attempts exhausted.", result["response"])
				self.assertNotIn("Required information", result["response"])
				self.assertEqual(self.state(result)["queue"], [])
		self.assertEqual(CALLS, [])
		self.assertEqual(self.state(result)["values"], {})
		again = self.send(flows=[flow.name])
		self.assertEqual(again["session"], start["session"])
		self.assertEqual(self.state(again)["queue"][0]["invalid_attempts"], 0)
		self.assertEqual(self.send(text="12345")["response"].split("\n\n")[0], "Values: 12345")

	def test_combined_automatic_business_rejections_replace_entire_set(self):
		p, flow = self.values_flow()
		self.send(flows=[flow.name])
		one = self.send(text="12")
		two = self.send(text="12345 y 99999")
		self.assertEqual(self.state(two)["queue"][0]["invalid_attempts"], 2)
		self.assertEqual(self.state(two)["values"], {})
		self.assertEqual(CALLS, [["12345", "99999"]])
		three = self.send(text="12345 y 12")
		self.assertEqual(CALLS, [["12345", "99999"]])
		self.assertEqual(self.state(three)["queue"], [])
		self.assertIn("Attempts exhausted", three["response"])
		# A separate execution gets a new set and never retains the rejected values.
		self.send(flows=[flow.name])
		self.send(text="67890")
		self.assertEqual(CALLS[-1], ["67890"])

	def test_retry_preserves_other_parameters_and_process_identity(self):
		p = self.parameter(mask="#####\nSC-#####")
		other = self.parameter("String", min_length=1)
		flow = self.flow(method="controlled_values_with_other", parameters=[
			dict(parameter=p.name, process_argument="values", required=1, allow_multiple_values=1, capture_order=1),
			dict(parameter=other.name, process_argument="other", required=1, capture_order=2)],
			outputs=[dict(status_code="success",output_mode="Message",flow_action="Complete Flow"),
				dict(status_code="invalid",output_mode="Message",flow_action="Retry Parameter",retry_parameter=p.name)],
			output_fields={"success":{"count":"integer"},"invalid":{"count":"integer"}})
		first = self.send(flows=[flow.name], parameters={p.name:["99999"], other.name:"retained"})
		self.assertEqual(self.state(first)["queue"][0]["values"], {other.name:"retained"})
		second = self.send(text="12345")
		self.assertIn("Values: 12345", second["response"])
		ids = [json.loads(frappe.get_doc("Chat Turn", r["turn"]).executions)[0]["id"] for r in (first,second)]
		self.assertNotEqual(*ids)

	def test_actions_validate_foreign_parameter_and_template_retry(self):
		p, flow = self.values_flow()
		foreign = self.parameter()
		flow.outputs[1].retry_parameter=foreign.name
		with self.assertRaises(frappe.ValidationError):flow.save()
		flow.reload();flow.outputs[1].output_mode="Template"
		template = frappe.get_doc(dict(doctype='Bot Flow Template', template_name=self.prefix + " retry", language="es",
			enabled=1, body="Rejected 1.")).insert()
		self.fixture_templates.append(template.name)
		flow.append("output_translations", dict(language="es", status_code=flow.outputs[1].status_code, template_reference=template.name))
		flow.save()
		from messaging.chatbot.doctype.bot_flow.bot_flow import set_active, validate_process
		approval = validate_process(flow.as_dict())
		if approval.get("execution_log"):
			self.fixture_test_logs.append(approval["execution_log"])
		self.assertEqual(approval["status"], "Passed", approval)
		flow.reload()
		set_active(flow.as_dict(), 1)
		flow.reload()
		frappe.db.commit()
		result = self.send(flows=[flow.name], parameters={p.name:["99999"]})
		self.assertIn("Rejected 1.", result["response"])
		self.assertEqual(self.state(result)["queue"][0]["invalid_attempts"],1)
		flow.outputs[0].flow_action="Complete Flow";flow.save();frappe.db.commit()
		done = self.send(text="12345")
		self.assertEqual(done["response"],"Values: 12345")

	def test_invalid_count_without_quota_and_accepted_composite_unit(self):
		p, flow = self.values_flow()
		self.configure(daily_quota_limit=1,monthly_quota_limit=2)
		first = self.send(flows=[flow.name]);simulate_dispatch(first["dispatch"])
		request=self.event(text="123")
		blocked=process_event(request)
		self.assertIsNone(blocked["dispatch"])
		self.assertEqual(self.state(blocked)["queue"][0]["invalid_attempts"],1)
		self.assertEqual(process_event(request),blocked)
		self.assertEqual(CALLS,[])
		self.configure(daily_quota_limit=3,monthly_quota_limit=4)
		done=self.send(text="12345")
		simulate_dispatch(done["dispatch"])
		self.assertEqual(frappe.db.count("Bot Dispatch",{"contact":done["contact"],"status":"Accepted"}),2)
		self.assertEqual(self.state(done)["queue"],[])

	def test_completion_and_exhaustion_preserve_remaining_intents(self):
		self.configure(max_invalid_attempts=1)
		p, flow = self.values_flow()
		later = self.flow(parameters=[dict(parameter=p.name, process_argument="values",required=1,allow_multiple_values=1,capture_order=1)],
			priority=flow.priority+1,method="controlled_values",outputs=[dict(status_code="success",output_mode="Message")])
		self.send(flows=[later.name,flow.name])
		cancelled=self.send(text="123")
		self.assertEqual([item["flow"] for item in self.state(cancelled)["queue"]],[later.name])
		self.assertIn("Attempts exhausted",cancelled["response"])
		self.assertTrue(self.state(cancelled).get("menu_pending"))
		self.assertFalse(any(d.get("kind")=="Main Menu" for d in json.loads(frappe.get_doc("Chat Turn",cancelled["turn"]).decisions)))
		done=self.send(text="12345")
		self.assertIn("Values: 12345", done["response"])
		self.assertIn(settings_text(frappe.get_single('Bot Settings'), 'informative_categories_header'), done['response'])
		self.assertTrue(any(d.get("kind")=="Main Menu" for d in json.loads(frappe.get_doc("Chat Turn", done["turn"]).decisions)))
		self.assertNotIn('menu_pending', self.state(done))
		self.assertEqual(CALLS,[["12345"]])
		# Both remaining flows run in order without adding a completion menu.
		both=self.send(flows=[flow.name,later.name],parameters={p.name:["67890"]})
		self.assertEqual(both["response"].count("Values: 67890"),2)
		self.assertEqual(self.state(both)["queue"],[])

	def test_verified_unknown_retry_is_idempotent_and_technical_failure_is_not_invalid(self):
		p, flow = self.values_flow()
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke",return_value=dict(
			state="Unknown",status=None,text="Safe technical error.",functional=False)):
			uncertain=self.send(flows=[flow.name],parameters={p.name:["12345"]})
		self.assertEqual(self.state(uncertain)["queue"][0]["invalid_attempts"],0)
		execution=json.loads(frappe.get_doc("Chat Turn",uncertain["turn"]).executions)[0]["id"]
		response=dict(version=1,status="invalid",recipient="",data={"count": 1},message="Business rejection.")
		resolved=reconcile_execution(uncertain["turn"],execution,response)
		self.assertEqual(self.state(resolved)["queue"][0]["invalid_attempts"],1)
		reconcile_execution(uncertain["turn"],execution,response)
		self.assertEqual(self.state(resolved)["queue"][0]["invalid_attempts"],1)
		self.assertEqual(CALLS,[])
		self.send(text="67890")
		self.assertEqual(CALLS,[["67890"]])

	def test_concurrent_duplicate_invalid_turn_consumes_one_attempt(self):
		p, flow = self.values_flow()
		first = self.send(flows=[flow.name])
		request = self.event(text="123")
		context = multiprocessing.get_context("spawn")
		barrier = context.Barrier(2)
		queue = context.Queue()
		workers = [context.Process(target=concurrent_worker,
			args=(frappe.local.site, frappe.local.sites_path, request, barrier, queue)) for _ in range(2)]
		try:
			for worker in workers:worker.start()
			results = [queue.get(timeout=20) for _ in workers]
		finally:
			for worker in workers:
				worker.join(timeout=5)
				if worker.is_alive():worker.terminate();worker.join()
		self.assertEqual([r['error'] for r in results], [None, None])
		self.assertEqual(len({r['turn'] for r in results}), 1)
		frappe.db.rollback()
		self.assertEqual(self.state(first)['queue'][0]['invalid_attempts'], 1)
		self.assertEqual(CALLS, [])


	def test_mixed_identifiers_never_hide_an_invalid_embedded_value(self):
		p, flow=self.values_flow()
		self.send(flows=[flow.name])
		for count,text in enumerate(('12345 y A12345','12345 y 123456'),1):
			result=self.send(text=text)
			self.assertEqual(self.state(result)['queue'][0]['invalid_attempts'],count)
			self.assertEqual(self.state(result)['values'],{})
			self.assertEqual(CALLS,[])
		free=self.parameter('String')
		from messaging.chatbot.doctype.bot_parameter.bot_parameter import validate_parameter_values
		self.assertEqual(validate_parameter_values(free,['Example','example'],multiple=True),(True,['Example','example']))


"""Focused interpretation contracts and integration; network access is explicit and opt-in."""

import copy
import json
import os
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import frappe
import requests

from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import simulate_dispatch
from messaging.chatbot.doctype.bot_settings import interpretation as engine
import sys
native = sys.modules[__name__]
from messaging.conversation.doctype.chat_turn.chat_turn import process_message
from messaging.common.primitives import utcnow


class InterpretationFixtures:
	# Reuse the native fixture lifecycle, not its unrelated test cases.
	def setUp(self):
		native.TestChatTurn.setUp(self)
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
		self.ai_configuration = create_configuration(self.prefix + '_ai', api_key='synthetic-interpretation-key')
		self.configure(interpretation_enabled=1, active_ai_configuration=self.ai_configuration.name)
		# Every synthetic test has an offline default; explicit scenarios override it.
		self.default_decision = self.proposal()
		self.default_decision.start()
		self.addCleanup(self.default_decision.stop)
		self.direct_ai_errors_before = set(frappe.get_all("Error Log", filters={"method":"Messaging AI Evaluation Error", "reference_name":["is","not set"]}, pluck="name"))

	def tearDown(self):
		# Error Log can outlive transaction rollback; delete only this fixture's direct-call diagnostics.
		frappe.set_user("Administrator")
		created = set(frappe.get_all("Error Log", filters={"method":"Messaging AI Evaluation Error", "reference_name":["is","not set"]}, pluck="name")) - self.direct_ai_errors_before
		for name in created:
			frappe.db.delete("Error Log", {"name":name})
		native.TestChatTurn.tearDown(self)
		frappe.delete_doc("AI Configuration", self.ai_configuration.name, delete_permanently=True)
		frappe.db.commit()

	configure = native.TestChatTurn.configure
	ensure_test_channel = native.TestChatTurn.ensure_test_channel
	parameter = native.TestChatTurn.parameter
	phrase = native.TestChatTurn.phrase
	flow = native.TestChatTurn.flow
	event = native.TestChatTurn.event
	state = native.TestChatTurn.state

	def proposal(
		self,
		*,
		requested=(),
		excluded=(),
		greeting=False,
		show_menu=False,
		human=False,
		humannegation=False,
		reference=False,
		candidates=None,
		capture_answer=False,
		confidence=0.95,
	):
		"""A closed provider double for contract tests, never production classification."""

		from messaging.chatbot.doctype.bot_settings.interpretation import Decision
		from messaging.registry import ai_providers as wire
		def evaluate(provider, values, request, **kwargs):
			settings = frappe.get_single('Bot Settings')
			wanted, denied = set(requested), set(excluded)
			catalog = engine.catalog()
			by_label = {flow['name']: code for code, flow in catalog.items()}
			refs = {by_label[row.label]: row.ref for row in request.available_flows}
			selected = [ref for code, ref in refs.items() if code in wanted]
			excluded_refs = [ref for code, ref in refs.items() if code in denied]
			ambiguous = bool(reference and selected)
			decision = Decision(flows=[] if ambiguous else selected, excluded_flows=excluded_refs,
				greeting_detected=greeting, ambiguous=ambiguous, outcome='ambiguous' if ambiguous else 'recognized' if selected or excluded_refs or greeting else 'unrecognized')
			from messaging.chatbot.doctype.ai_provider.test_ai_provider import synthetic_wire_response
			return synthetic_wire_response(request, decision, confidence)
		return patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=evaluate)

	def restore_validation(self):
		from messaging.chatbot.doctype.ai_configuration import ai_configuration as config
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import successful_probe
		with patch.object(config, 'run', side_effect=successful_probe):
			config.test_connection(self.ai_configuration.name)
		frappe.db.commit()

	def decision(self, result):
		return json.loads(frappe.get_doc("Chat Turn", result["turn"]).decisions)[0]



import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe

from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import simulate_dispatch
import sys
native = sys.modules[__name__]
from messaging.conversation.doctype.chat_turn.chat_turn import process_message, reconcile_execution
from messaging.common.primitives import utcnow


class TestMechanicalMenu(unittest.TestCase):
	configure = native.TestChatTurn.configure
	ensure_test_channel = native.TestChatTurn.ensure_test_channel
	parameter = native.TestChatTurn.parameter
	phrase = native.TestChatTurn.phrase
	flow = native.TestChatTurn.flow
	event = native.TestChatTurn.event
	state = native.TestChatTurn.state
	tearDown = native.TestChatTurn.tearDown

	def setUp(self):
		native.TestChatTurn.setUp(self)
		self.configure(interpretation_enabled=0, quotas_enabled=0, informative_categories_header="",
			chatbot_languages=[dict(language="en", enabled=1, is_default=1)])
		from messaging.chatbot.doctype.bot_settings.bot_settings import get_menu_flows
		menu_catalog = patch("messaging.conversation.doctype.chat_session.chat_session.get_menu_flows",
			side_effect=lambda settings: [row for row in get_menu_flows(settings)
				if row["workflow_code"] in self.flows])
		menu_catalog.start()
		self.addCleanup(menu_catalog.stop)
		MIXED_CALLS.clear()
		for path in ("messaging.conversation.doctype.chat_turn.turn_decision.interpret",
			"messaging.chatbot.doctype.ai_configuration.execution.run"):
			guard = patch(path, side_effect=AssertionError("Mechanical interaction cannot call AI"))
			mock = guard.start()
			self.addCleanup(guard.stop)
			self.addCleanup(mock.assert_not_called)

	def send(self, text, sender="external-user", *, accept=True, **values):
		result = process_message(self.event(text=text, sender=sender, **values))
		if accept and result.get("dispatch") and result.get("dispatch_state") == "Prepared":
			simulate_dispatch(result["dispatch"], "Accepted")
		return result

	def select(self, flow, sender="external-user"):
		menu = self.send("menu", sender)
		number = next(n for n, option in self.state(menu)["mechanical_menu"]["options"].items()
			if option["flow"] == flow.name)
		return self.send(number, sender)

	def relation(self, parameter, argument="value", order=1):
		return dict(parameter=parameter.name, process_argument=argument, capture_order=order, required=1)

	def test_menu_exposes_enabled_visible_flows_without_allowlist_and_plain_text(self):
		visible = self.flow(menu_label="Public service")
		hidden = self.flow(show_in_menu=0)
		disabled = self.flow(status="Disabled")
		unlisted = self.flow(method="controlled_other_status", menu_label="Unlisted service",
			outputs=[dict(status_code="unavailable", output_mode="Message")])
		first = self.send("1")
		options = self.state(first)["mechanical_menu"]["options"]
		self.assertEqual({row["flow"] for row in options.values()}, {visible.name, unlisted.name})
		self.assertIn("1. Public service", first["response"])
		for flow in (visible, hidden, disabled, unlisted):
			self.assertNotIn(flow.name, first["response"])
		self.assertEqual(native.CALLS, [])
		from messaging.chatbot.doctype.bot_dispatch.delivery import _issue_menu
		dispatch = frappe.get_doc("Bot Dispatch", first["dispatch"])
		_issue_menu(dispatch, dict(capabilities=["text", "interactive"]))
		self.assertFalse(dispatch.menu_options)
		self.assertEqual(self.state(self.send("0"))["queue"], [])

		selected = self.send("2")
		self.assertIn("Unavailable", selected["response"])
		executions = json.loads(frappe.get_doc("Chat Turn", selected["turn"]).executions)
		self.assertEqual(executions[0]["flow"], unlisted.name)

	def test_new_interactions_greet_once_and_do_not_infer_business_intents(self):
		flow = self.flow(menu_label="Balance")
		self.configure(greeting_enabled=0, show_menu_on_greeting_only=1)
		for index, text in enumerate(("Hola", "Quiero saber cuánto debo de mi contrato", "1")):
			with self.subTest(text=text):
				result = self.send(text, sender=str(index))
				self.assertEqual(result["response"], "Select an option:\n1. Balance\n0. Return to the main menu")
				self.assertEqual(len(native.CALLS), index)
				repeated = self.send("99", sender=str(index))
				self.assertNotIn("Configured greeting.", repeated["response"])
				complete = self.send("1", sender=str(index))
				self.assertEqual(complete["response"], "Result hours.")
				self.assertNotIn("mechanical_menu", self.state(complete))
				self.assertEqual(len(native.CALLS), index + 1)
				fresh = self.send("1", sender=str(index))
				self.assertEqual(fresh["response"], result["response"])
				self.assertEqual(len(native.CALLS), index + 1)
		self.configure(greeting_enabled=0)
		self.assertTrue(self.send("Hola", sender="no-greeting")["response"].startswith("Select an option:"))
		self.assertEqual(flow.status, "Enabled")

	def test_configured_prompt_and_functional_result_are_not_duplicated(self):
		parameter = self.parameter("Int", min_value=0, max_value=10)
		flow = self.flow(parameters=[dict(parameter=self.phrase(parameter.name, 'Enter the contract number.'), process_argument='value', required=1)],
			input_initial_message="Begin capture.")
		pending = self.select(flow)
		self.assertEqual(pending["response"], "Begin capture.\nEnter the contract number.")
		invalid = self.send("invalid")
		self.assertEqual(invalid["response"], "The value is invalid or ambiguous. Please provide the pending information.\nContinuation input.\nEnter the contract number.")
		self.assertEqual(self.state(invalid)["queue"][0]["invalid_attempts"], 1)
		event = self.event(text="1")
		complete = process_message(event)
		self.assertEqual(complete["response"], "Result 1.")
		self.assertEqual(process_message(event), complete)
		self.assertEqual(native.CALLS, [1])
		self.assertNotIn("mechanical_menu", self.state(complete))

	def test_relation_questions_request_only_pending_parameters(self):
		first = self.parameter("Int", min_value=0, max_value=10)
		second = self.parameter("String")
		flow = self.flow(parameters=[dict(self.relation(frappe.get_doc('Bot Parameter', self.phrase(first.name, 'How many contracts?')))),
			dict(self.relation(frappe.get_doc('Bot Parameter', self.phrase(second.name, 'Describe the issue.')), 'optional', 2))])
		self.assertEqual(self.select(flow)["response"], "Initial input.\nHow many contracts?")
		self.assertEqual(self.send("1")["response"], "Continuation input.\nDescribe the issue.")
		complete = self.send("Synthetic issue")
		self.assertEqual(self.state(complete)["queue"], [])

	def test_numbers_stay_bound_to_displayed_flows_and_invalid_selection_does_not_execute(self):
		first = self.flow(menu_label="First", menu_order=1)
		second = self.flow(menu_label="Second", menu_order=2, method="controlled_other_status")
		menu = self.send("hello")
		self.assertEqual(self.state(menu)["mechanical_menu"]["options"]["1"]["flow"], first.name)
		first.menu_order = 3
		first.save()
		frappe.db.commit()
		invalid = self.send("99")
		self.assertIn("valid menu option", invalid["response"])
		self.assertEqual(self.state(invalid)["mechanical_menu"]["options"]["1"]["flow"], first.name)
		self.assertEqual(native.CALLS, [])
		result = self.send("1")
		self.assertEqual(json.loads(frappe.get_doc("Chat Turn", result["turn"]).executions)[0]["flow"], first.name)
		self.assertIn("Result", result["response"])
		self.assertNotIn("mechanical_menu", self.state(result))
		self.assertEqual(native.CALLS, [None])
		# Another identity has no displayed mapping, even if it sends a familiar number.
		self.send("1", sender="another-contact")
		self.assertEqual(native.CALLS, [None])
		frappe.db.set_value("Chat Session", result["session"], "expires_at", utcnow() - timedelta(minutes=1))
		frappe.db.commit()
		fresh = self.send("1")
		self.assertNotEqual(result["session"], fresh["session"])
		self.assertEqual(native.CALLS, [None])
		self.assertNotEqual(first.name, second.name)

	def test_removed_disabled_options_do_not_retarget_numbers(self):
		first = self.flow(menu_order=1)
		second = self.flow(menu_order=2)
		third = self.flow(menu_order=3, method="controlled_other_status")
		self.send("hello")
		from messaging.chatbot.doctype.bot_flow.bot_flow import set_active
		set_active(first.as_dict(), 0)
		frappe.delete_doc("Bot Flow", second.name)
		self.flows.remove(second.name)
		set_active(third.as_dict(), 0)
		frappe.db.commit()
		for number in ("1", "2", "3"):
			result = self.send(number)
			self.assertEqual(self.state(result)["queue"], [])
			self.assertEqual(native.CALLS, [])
		self.assertEqual(self.state(result)["mechanical_menu"]["options"], {})

	def test_unaccepted_menu_and_quota_blocked_menu_cannot_select(self):
		self.flow()
		menu = self.send("hello", accept=False)
		self.configure(quotas_enabled=1, daily_quota_limit=1)
		result = self.send("1")
		self.assertEqual(native.CALLS, [])
		self.assertIn("valid menu option", result["response"])
		self.assertEqual(frappe.db.get_value("Bot Dispatch", menu["dispatch"], "status"), "Prepared")
		blocked = self.send("0")
		self.assertEqual(blocked["state"], "Quota Blocked")
		self.assertEqual(self.state(blocked)["mechanical_menu"]["dispatch"], result["dispatch"])

	def test_expression_capture_duplicate_and_return_to_menu(self):
		contract = self.parameter(mask="SC-#####")
		flow = self.flow(parameters=[dict(self.relation(frappe.get_doc('Bot Parameter', self.phrase(contract.name, 'Provide your contract.'))))], input_initial_message="Provide your contract.")
		pending = self.select(flow)
		self.assertIn("Provide your contract.", pending["response"])
		answer = self.event(text="SC-00123")
		complete = process_message(answer)
		self.assertEqual(process_message(answer), complete)
		self.assertEqual(native.CALLS, ["SC-00123"])
		self.assertEqual(self.state(complete)["queue"], [])
		self.assertNotIn("0. Return to the main menu", complete["response"])
		self.assertNotIn("mechanical_menu", self.state(complete))

	def test_pending_integer_owns_zero_and_menu_numbers(self):
		integer = self.parameter("Int", min_value=0, max_value=10)
		flow = self.flow(parameters=[self.relation(integer)])
		for value in ("0", "1"):
			with self.subTest(value=value):
				self.select(flow, sender=value)
				complete = self.send(value, sender=value)
				self.assertEqual(native.CALLS[-1], int(value))
				self.assertEqual(self.state(complete)["queue"], [])

	def test_name_and_identity_are_captured_together_or_requested_individually(self):
		person = self.parameter("Name", allow_contextual_name_extraction=1)
		identity = self.parameter(mask="#############")
		flow = self.flow(parameters=[self.relation(person), self.relation(identity, "optional", 2)])
		self.select(flow)
		complete = self.send("Me llamo Juan Pérez y mi identidad es 0801199012345.")
		self.assertEqual(native.CALLS, ["Juan Pérez"])
		self.assertEqual(self.state(complete)["queue"], [])
		person = self.parameter("Name", allow_contextual_name_extraction=0)
		flow = self.flow(parameters=[self.relation(person), self.relation(identity, "optional", 2)])
		self.select(flow, sender="partial")
		partial = self.send("Me llamo Juan Pérez y mi identidad es 0801199012345.", sender="partial")
		self.assertEqual(self.state(partial)["queue"][0]["values"], {identity.name: "0801199012345"})
		self.assertIn(person.label, partial["response"])
		complete = self.send("李小龍", sender="partial")
		self.assertEqual(native.CALLS[-1], "李小龍")
		self.assertEqual(self.state(complete)["queue"], [])

	def test_date_format_single_word_and_general_text(self):
		date = self.parameter("Date")
		word = self.parameter("String", string_mode="Single Word")
		description = self.parameter("String", string_mode="Text")
		for parameter, invalid, valid, expected in (
			(date, "2026-10-27", "29/02/2024", "2024-02-29"),
			(word, "El color de mi casa es verde.", "Verde", "Verde"),
			(description, None, "Hay una fuga junto a 2 casas. Necesita revisión.", "Hay una fuga junto a 2 casas. Necesita revisión."),
		):
			with self.subTest(kind=parameter.parameter_type, mode=parameter.string_mode):
				flow = self.flow(parameters=[self.relation(parameter)])
				self.select(flow, sender=parameter.name)
				if invalid:
					bad = self.send(invalid, sender=parameter.name)
					self.assertEqual(self.state(bad)["queue"][0]["values"], {})
					self.assertIn("invalid", bad["response"])
					if parameter.parameter_type == "Date":
						self.assertIn("DD/MM/YYYY", bad["response"])
				complete = self.send(valid, sender=parameter.name)
				self.assertEqual(native.CALLS[-1], expected)
				self.assertEqual(self.state(complete)["queue"], [])

	def test_mixed_native_values_preserve_capture_and_request_only_missing_fields(self):
		person = self.parameter("Name", allow_contextual_name_extraction=1, allow_ai_extraction=1)
		identity = self.parameter(mask="#############")
		date = self.parameter("Date", allow_ai_extraction=1)
		description = self.parameter("String", allow_ai_extraction=1)
		flow = self.flow(parameters=[self.relation(person, "person"), self.relation(identity, "identity", 2),
			self.relation(date, "visit_date", 3), self.relation(description, "description", 4)],

			method="controlled_mixed", outputs=[dict(status_code="success", output_mode="Message")])
		self.select(flow, sender="mixed")
		partial = self.send("Me llamo Juan Pérez y mi identidad es 0801199012345.", sender="mixed")
		self.assertEqual(self.state(partial)["queue"][0]["values"], {person.name: "Juan Pérez", identity.name: "0801199012345"})
		self.assertIn("DD/MM/YYYY", partial["response"])
		self.assertNotIn(description.label, partial["response"])
		partial = self.send("27/10/2026", sender="mixed")
		self.assertEqual(self.state(partial)["queue"][0]["values"][person.name], "Juan Pérez")
		self.assertIn(description.label, partial["response"])
		complete = self.send("La tubería junto a 2 casas está rota.", sender="mixed")
		self.assertEqual(self.state(complete)["queue"], [])
		self.assertIn("Business response.", complete["response"])
		self.assertEqual(MIXED_CALLS[-1], dict(person="Juan Pérez", identity="0801199012345", visit_date="2026-10-27",
			description="La tubería junto a 2 casas está rota.", language="en"))

	def test_uncertain_execution_requires_reconciliation_and_a_new_displayed_menu(self):
		self.flow(outputs=[dict(status_code="success", output_mode="Message")])
		self.send("hello")
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke", return_value=dict(
			state="Unknown", status=None, text="Safe technical error.", functional=False)):
			uncertain = self.send("1")
		self.assertEqual(uncertain["state"], "Recovery Required")
		self.assertNotIn("mechanical_menu", self.state(uncertain))
		held = self.send("1")
		self.assertEqual(held["state"], "Recovery Required")
		self.assertFalse(held.get("dispatch"))
		execution = json.loads(frappe.get_doc("Chat Turn", uncertain["turn"]).executions)[0]["id"]
		with patch("messaging.chatbot.doctype.bot_dispatch.delivery.deliver", side_effect=simulate_dispatch):
			reconciled = reconcile_execution(uncertain["turn"], execution,
				dict(version=1, status="success", recipient="", data={"result": "verified", "optional": "default"}, message="Verified response."))
		self.assertEqual(self.state(reconciled)["queue"], [])
		# The held number may display a new menu, but cannot reuse the consumed one.
		self.assertEqual(native.CALLS, [])
		menu = self.state(reconciled).get("mechanical_menu")
		if menu:
			self.assertNotEqual(menu["dispatch"], uncertain["dispatch"])
		reconcile_execution(uncertain["turn"], execution,
			dict(version=1, status="success", recipient="", data={"result": "verified", "optional": "default"}, message="Verified response."))
		self.assertEqual(native.CALLS, [])

	def test_context_capture_then_requests_the_pending_text_parameter(self):
		identity = self.parameter(mask="SC-#####")
		description = self.parameter("String")
		flow = self.flow(parameters=[self.relation(identity), self.relation(description, "optional", 2)])
		self.select(flow)
		partial = self.send("Mi contrato es SC-00123 y hay una fuga.")
		self.assertEqual(self.state(partial)["queue"][0]["values"], {identity.name: "SC-00123"})
		self.assertIn(description.label, partial["response"])
		complete = self.send("Hay una fuga en la tubería principal.")
		self.assertEqual(native.CALLS, ["SC-00123"])
		self.assertEqual(self.state(complete)["queue"], [])

	def test_ambiguous_native_candidates_remain_pending_without_losing_other_values(self):
		person = self.parameter("Name", allow_contextual_name_extraction=1)
		identity = self.parameter(mask="SC-#####")
		flow = self.flow(parameters=[self.relation(person), self.relation(identity, "optional", 2)])
		self.select(flow)
		partial = self.send("Me llamo Juan Pérez y mi esposa se llama Ana López. Contrato SC-00123.")
		self.assertEqual(self.state(partial)["queue"][0]["values"], {identity.name: "SC-00123"})
		complete = self.send("Juan Pérez")
		self.assertEqual(native.CALLS, ["Juan Pérez"])
		self.assertEqual(self.state(complete)["queue"], [])


	def remove_configuration(self, name):
		native.restore_bot_settings(self.settings_before)
		frappe.delete_doc("AI Configuration", name, force=True)
		frappe.db.commit()

	def test_spanish_menu_uses_resolved_language(self):
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		self.configure(chatbot_languages=[dict(language="es", enabled=1, is_default=1)])
		self.flow(menu_label="Consultar servicio")
		with patch("messaging.conversation.doctype.chat_session.chat_session.resolve_process_language", side_effect=resolve_process_language):
			menu = self.send("hola")
			self.assertIn("Seleccione una opción:", menu["response"])
			self.assertIn("0. Volver al menú principal", menu["response"])
			self.assertIn("1. Consultar servicio", menu["response"])

"""Terminal episode failures, deferred canonical inputs and transport evidence."""
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe

from messaging.common.primitives import utcnow
from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import simulate_dispatch, prepare
import sys
native = sys.modules[__name__]
from messaging.conversation.doctype.chat_turn.chat_turn import process_event, reconcile_execution
from messaging.conversation.doctype.chat_session.chat_session import read_state, write_state

SAFE_ES = "Lo sentimos, no pudimos completar tu solicitud por un problema técnico. Puedes intentarlo nuevamente."


class TestRecovery(unittest.TestCase):
	configure = TestChatTurn.configure
	flow = TestChatTurn.flow
	parameter = TestChatTurn.parameter

	def setUp(self):
		from messaging.emulator.doctype.emulator_contact.fixtures import bind_emulator
		TestChatTurn.setUp(self)
		bind_emulator(self)

	def tearDown(self):
		from messaging.emulator.doctype.emulator_contact.fixtures import release_emulator
		release_emulator(self, lambda: TestChatTurn.tearDown(self))

	def account(self, *args, **kwargs):
		from messaging.emulator.doctype.emulator_contact.fixtures import emulator_account
		return emulator_account(self, *args, **kwargs)

	def contact(self, **values):
		from messaging.emulator.doctype.emulator_contact.fixtures import emulator_contact
		return emulator_contact(self, **values)

	def message(self, account=None, sender=None, text='Hello', client_id=None, **kwargs):
		from messaging.emulator.doctype.emulator_contact.fixtures import emulator_message
		return emulator_message(self, account, sender, text, client_id, **kwargs)

	def failed_flow(self):
		return self.flow(method="controlled_type_error", outputs=[dict(status_code="success", output_mode="Message")])

	def transport_outcome(self, outcome):
		doc = frappe.get_doc("Communication Channel Account", self.a)
		doc.parameters[0].value = json.dumps(outcome)
		doc.save(); frappe.db.commit()

	def natural_recognition(self):
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
		from messaging.chatbot.doctype.bot_settings.interpretation import Decision
		configuration = create_configuration(self.prefix + "_recovery_ai", api_key="synthetic-recovery-key")
		self.configure(interpretation_enabled=1, active_ai_configuration=configuration.name)
		self.addCleanup(lambda: (
			frappe.set_user("Administrator"),
			frappe.delete_doc("AI Configuration", configuration.name, delete_permanently=True),
			frappe.db.commit(),
		))
		def _recognize(text, context, flows, allowed_active, settings, **kwargs):
			matches = [text] if text in (flows or {}) else []
			return Decision(outcome="recognized" if matches else "unrecognized", flows=matches)
		patcher = patch("messaging.conversation.doctype.chat_turn.turn_decision.interpret", side_effect=_recognize)
		patcher.start()
		self.addCleanup(patcher.stop)

	def session(self, result):
		return frappe.get_doc("Chat Session", result["session"])

	def assert_retired(self, result, status="Closed"):
		session = self.session(result)
		self.assertEqual(session.status, status)
		self.assertFalse(session.active_flow or session.active_key)
		state = read_state(session, "dialogue_state")
		self.assertEqual((state["queue"], state["values"]), ([], {}))
		self.assertEqual(session.close_reason, "Technical Error")

	def test_terminal_type_error_localized_notice_closed_then_new_message(self):
		self.configure(interpretation_enabled=0, default_internal_error_message=SAFE_ES)
		bad, good = self.failed_flow(), self.flow()
		menu = self.message(text='Show options')
		options = read_state(self.session(menu), 'dialogue_state')['mechanical_menu']['options']
		number = next(n for n, option in options.items() if option['flow'] == bad.name)
		result = self.message(text=number)
		self.assertEqual(result["response"], SAFE_ES)
		self.assertEqual(result["dispatch_state"], "Accepted")
		self.assert_retired(result)
		log = frappe.get_last_doc("Process Execution Log", filters={"turn": result["turn"]})
		self.assertEqual(log.diagnostic_error_type, "TypeError")
		self.assertNotIn("Synthetic private", log.as_json())
		menu = self.message(text='Show options')
		options = read_state(self.session(menu), 'dialogue_state')['mechanical_menu']['options']
		number = next(n for n, option in options.items() if option['flow'] == good.name)
		next_message = self.message(text=number)
		self.assertNotEqual(next_message["session"], result["session"])
		self.assertEqual(self.session(next_message).status, "Active")
		self.assertEqual(native.CALLS, ["type_error", None])

	def test_input_during_unknown_transport_duplicate_resumes_once_on_acceptance(self):
		self.natural_recognition()
		bad, good = self.failed_flow(), self.flow()
		self.transport_outcome("Unknown")
		failed = self.message(text=bad.name)
		held = self.message(text=good.name, client_id=self.prefix + "_pending")
		duplicate = self.message(text=good.name, client_id=self.prefix + "_pending")
		self.assertEqual(held["turn"], duplicate["turn"])
		self.assertEqual(held["state"], "Pending")
		self.assertEqual(native.CALLS, ["type_error"])
		self.assertEqual(frappe.db.count("Chat Message", {"turn": held["turn"], "kind": "Incoming"}), 1)
		self.transport_outcome("Accepted")
		from messaging.emulator.page.emulator_console.emulator_console import simulate_result
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		simulate_result(self.a, self.sender, failed["dispatch"], "Accepted")
		drain_contact(failed["contact"])
		self.assert_retired(failed)
		resumed = frappe.get_doc("Chat Turn", held["turn"])
		self.assertEqual(resumed.status, "Complete")
		self.assertNotEqual(resumed.session, failed["session"])
		self.assertFalse(resumed.request_context)
		self.assertEqual(native.CALLS, ["type_error", None])
		self.assertEqual(frappe.db.get_value("Bot Dispatch", resumed.dispatch, "status"), "Accepted")
		incoming = frappe.get_doc("Chat Message", {"turn": held["turn"], "kind": "Incoming"})
		self.assertEqual(incoming.session, resumed.session)
		self.assertIsNone(json.loads(resumed.input_summary)["received_session"])
		simulate_result(self.a, self.sender, failed["dispatch"], "Accepted")
		self.assertEqual(native.CALLS, ["type_error", None])

	def test_rejected_notice_is_terminal_without_resend(self):
		self.natural_recognition()
		self.transport_outcome("Rejected")
		bad = self.failed_flow()
		result = self.message(text=bad.name, client_id=self.prefix + "_rejected")
		self.assert_retired(result)
		self.message(text=bad.name, client_id=self.prefix + "_rejected")
		self.assertEqual(native.CALLS, ["type_error"])
		self.assertEqual(frappe.db.get_value("Bot Dispatch", result["dispatch"], "status"), "Rejected")

	def test_expired_failed_episode_waits_for_unknown_then_retires(self):
		self.natural_recognition()
		self.transport_outcome("Unknown")
		bad, good = self.failed_flow(), self.flow()
		result = self.message(text=bad.name)
		session = self.session(result); session.expires_at = utcnow() - timedelta(minutes=1); session.save(ignore_permissions=True); frappe.db.commit()
		held = self.message(text=good.name)
		self.assertEqual(self.session(result).status, "Recovery Required")
		self.assertEqual(native.CALLS, ["type_error"])
		self.transport_outcome("Accepted")
		from messaging.emulator.page.emulator_console.emulator_console import simulate_result
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		simulate_result(self.a, self.sender, result["dispatch"], "Accepted")
		drain_contact(result["contact"])
		self.assert_retired(result, "Expired")
		self.assertEqual(frappe.db.get_value("Chat Turn", held["turn"], "status"), "Complete")
		self.assertEqual(native.CALLS, ["type_error", None])


	def test_valid_business_retry_and_partial_do_not_close_episode(self):
		self.natural_recognition()
		parameter = self.parameter(mask="#####")
		flow = self.flow(method="controlled_values", parameters=[dict(parameter=parameter.name, process_argument="values",
			required=1, capture_order=1, allow_multiple_values=1)], outputs=[
				dict(status_code="success", output_mode="Message"),
				dict(status_code="invalid", output_mode="Message", flow_action="Retry Parameter", retry_parameter=parameter.name)])
		self.message(text=flow.name)
		retry = self.message(text="99999")
		self.assertEqual(self.session(retry).status, "Capturing")
		complete = self.message(text="00123")
		self.assertEqual(self.session(complete).status, "Active")

	def test_human_active_and_paused_keep_control_after_transport_reconciliation(self):
		self.natural_recognition()
		for state in ("Human Active", "Human Paused"):
			sender = self.contact().sender_id
			self.transport_outcome("Unknown")
			bad = self.failed_flow()
			result = self.message(sender=sender, text=bad.name)
			pending = self.message(sender=sender, text=bad.name)
			session = self.session(result); session.status=state; session.save(ignore_permissions=True); frappe.db.commit()
			from messaging.emulator.page.emulator_console.emulator_console import simulate_result
			from messaging.conversation.doctype.chat_turn.inbox import drain_contact
			simulate_result(self.a, sender, result["dispatch"], "Accepted")
			drain_contact(result["contact"])
			self.assertEqual(self.session(result).status, state)
			self.assertEqual(frappe.db.get_value("Chat Turn", pending["turn"], "status"), "Human Paused")
			self.assertFalse(frappe.db.get_value("Chat Turn", pending["turn"], "request_context"))
			new = self.message(sender=sender, text=bad.name)
			self.assertEqual(new["state"], "Human Paused")
			self.assertFalse(new["dispatch"])
		self.assertEqual(native.CALLS, ["type_error", "type_error"])

	def test_legacy_type_error_unknown_recovers_from_receipt_without_rewriting_notice(self):
		self.natural_recognition()
		self.transport_outcome("Unknown")
		bad = self.failed_flow(); result = self.message(text=bad.name)
		owner = frappe.get_doc("Chat Turn", result["turn"])
		executions = json.loads(owner.executions); executions[0]["state"]="Unknown"; owner.executions=json.dumps(executions); owner.save(ignore_permissions=True)
		session=self.session(result); state=read_state(session,"dialogue_state");state.pop("technical_failure");write_state(session,"dialogue_state",state);session.save(ignore_permissions=True);frappe.db.commit()
		before=frappe.db.get_value("Chat Message",{"dispatch":result["dispatch"]},"content")
		from messaging.emulator.page.emulator_console.emulator_console import simulate_result
		simulate_result(self.a,self.sender,result["dispatch"],"Accepted")
		self.assert_retired(result)
		self.assertEqual(frappe.db.get_value("Chat Message",{"dispatch":result["dispatch"]},"content"),before)
		self.assertEqual(native.CALLS,["type_error"])

	def test_interrupted_process_requires_explicit_terminal_recovery_without_reexecution(self):
		self.natural_recognition()
		bad, good = self.failed_flow(), self.flow()
		request=dict(channel="emulator",account=self.a,sender=self.sender,event_id=self.prefix+"_crash",event_at=utcnow().isoformat()+"+00:00",flows=[bad.name])
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke",side_effect=SystemExit):
			with self.assertRaises(SystemExit):process_event(request)
		frappe.db.rollback()
		result=process_event(request)
		held=self.message(text=good.name)
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		execution=json.loads(frappe.db.get_value("Chat Turn",result["turn"],"executions"))[0]["id"]
		reconcile_execution(result["turn"],execution,terminal_error=True)
		drain_contact(result["contact"])
		self.assert_retired(result)
		self.assertEqual(frappe.db.get_value("Chat Turn",held["turn"],"status"),"Complete")
		self.assertEqual(native.CALLS,[None])

	def test_render_failure_is_terminal_and_not_a_business_success(self):
		self.natural_recognition()
		self.configure(default_internal_error_message=SAFE_ES)
		bad=self.flow(method="controlled_invalid")
		result=self.message(text=bad.name)
		self.assert_retired(result)
		self.assertEqual(result["response"],SAFE_ES)

	def test_interrupted_notice_preparation_recovers_without_duplicate_notice(self):
		self.natural_recognition()
		bad, good = self.failed_flow(), self.flow()
		request = dict(channel="emulator", account=self.a, sender=self.sender, event_id=self.prefix+"_notice_crash",
			event_at=utcnow().isoformat()+"+00:00", flows=[bad.name])
		with patch("messaging.conversation.doctype.chat_turn.chat_turn.prepare", side_effect=SystemExit):
			with self.assertRaises(SystemExit): process_event(request)
		frappe.db.rollback()
		pending = self.message(text=good.name)
		self.assertEqual(self.session(pending).status, "Active")
		old = frappe.get_last_doc("Chat Turn", filters={"contact":pending["contact"],"session":["!=",pending["session"]]})
		self.assertEqual(frappe.db.get_value("Chat Session",old.session,"status"),"Closed")
		self.assertEqual(frappe.db.count("Chat Message",{"turn":old.name,"kind":"Automatic"}),1)
		self.assertEqual(native.CALLS,["type_error",None])

	def test_verified_process_recovery_waits_for_transport_before_resuming_input(self):
		bad, good = self.failed_flow(), self.flow()
		request = dict(channel="emulator", account=self.a, sender=self.sender, event_id=self.prefix+"_uncertain",
			event_at=utcnow().isoformat()+"+00:00", flows=[bad.name])
		with patch("messaging.conversation.doctype.chat_session.chat_session.invoke",side_effect=SystemExit):
			with self.assertRaises(SystemExit):process_event(request)
		frappe.db.rollback(); result=process_event(request)
		execution=json.loads(frappe.db.get_value("Chat Turn",result["turn"],"executions"))[0]["id"]
		# A known result resumes the existing action, but no uncertain handoff is replayed.
		from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import claim,reconcile
		dispatch=frappe.get_doc("Bot Dispatch",result["dispatch"]);prepare(dispatch,SAFE_ES);frappe.db.commit()
		claim(dispatch.name);reconcile(dispatch.name,"Unknown")
		held=self.message(text=good.name)
		reconcile_execution(result["turn"],execution,dict(version=1,status="success",recipient="",data={},message="Verified"))
		self.assertEqual(self.session(result).status,"Recovery Required")
		self.assertEqual(native.CALLS,[])
		from messaging.emulator.page.emulator_console.emulator_console import simulate_result
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		simulate_result(self.a,self.sender,dispatch.name,"Accepted")
		drain_contact(result["contact"])
		self.assertEqual(frappe.db.get_value("Chat Turn",held["turn"],"status"),"Complete")
		self.assertEqual(native.CALLS,[])
		resumed = frappe.get_doc('Chat Turn', held['turn'])
		state = read_state(frappe.get_doc('Chat Session', resumed.session), 'dialogue_state')
		self.assertIn(good.name, {option['flow'] for option in state['mechanical_menu']['options'].values()})

	def test_old_event_cannot_restart_a_closed_failure(self):
		self.natural_recognition()
		bad, good = self.failed_flow(), self.flow()
		result=self.message(text=bad.name)
		old=dict(channel="emulator",account=self.a,sender=self.sender,event_id=self.prefix+"_old",
			event_at=(utcnow()-timedelta(minutes=2)).isoformat()+"+00:00",flows=[good.name])
		ignored=process_event(old)
		self.assertEqual(ignored["state"],"Ignored")
		self.assertEqual(native.CALLS,["type_error"])
		self.assert_retired(result)
		self.assertEqual(frappe.db.count("Chat Session", {"contact": result["contact"]}), 1)


class TestFlowConsumers(unittest.TestCase):
    """Real executor and MariaDB; synthetic business callbacks and classification doubles."""
    configure = TestChatTurn.configure
    ensure_test_channel = TestChatTurn.ensure_test_channel
    parameter = TestChatTurn.parameter
    phrase = TestChatTurn.phrase
    flow = TestChatTurn.flow
    event = TestChatTurn.event
    send = TestChatTurn.send
    state = TestChatTurn.state

    def setUp(self):
        TestChatTurn.setUp(self)
        self.configure(quotas_enabled=0, greeting_enabled=0)
        self.ai_names = []

    def tearDown(self):
        TestChatTurn.tearDown(self)
        for name in self.ai_names:
            frappe.delete_doc('AI Configuration', name, delete_permanently=True)
        frappe.db.commit()

    def consumer_flow(self, *, parameter=True):
        value = self.parameter('Int', min_value=0, max_value=9) if parameter else None
        declarations = {'registered': {'full_name': 'string', 'identity_number': 'string'},
            'already_registered': {'full_name': 'string', 'reference': 'string'},
            'unlisted': {'full_name': 'string'}, 'cancelled': {}}
        if value:
            declarations['retry'] = {}
        outputs = [dict(status_code=status, output_mode='Message', flow_action=
            'Retry Parameter' if status == 'retry' else 'Cancel Flow' if status == 'cancelled' else 'Complete Flow',
            retry_parameter=value.name if status == 'retry' else None) for status in declarations]
        flow = self.flow(method='controlled_consumer', show_in_menu=0,
            parameters=[dict(parameter=value.name, process_argument='value', required=1, capture_order=1)] if value else [],
            outputs=outputs, output_fields=declarations)
        return flow, value

    def select(self, consumer, flow, states=('registered', 'already_registered'), keep=('full_name', 'identity_number', 'reference'), *, enabled=True):
        from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
        from messaging.chatbot.doctype.bot_settings.flow_consumers import options
        state_field = 'registration_states' if consumer == 'registration' else 'human_transfer_states'
        enabled_field = 'registration_enabled' if consumer == 'registration' else 'human_attention_enabled'
        selected = [state_name(flow.name, status) for status in states]
        fields = options(flow.name, frappe.as_json(selected), consumer=consumer)['fields']
        settings = self.configure(**{consumer + '_flow': flow.name, enabled_field: 0,
            state_field: [dict(state=state) for state in selected], consumer + '_fields': [
                dict(field_key=item['field_key'], label=item['field_key'], keep=0,
                    **({'states': ', '.join(item['states'])} if consumer == 'human' else {})) for item in fields]})
        for row in settings.get(consumer + '_fields'):
            row.keep = int(row.field_key in keep)
            row.label = {'full_name': 'Nombre completo', 'identity_number': 'Número de identidad', 'reference': 'Referencia'}[row.field_key]
        settings.set(enabled_field, int(enabled))
        settings.save(); frappe.db.commit()
        return settings

    def natural(self):
        from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
        ai = create_configuration(self.prefix + '_ai', api_key='synthetic-consumer-key')
        self.ai_names.append(ai.name)
        return self.configure(interpretation_enabled=1, active_ai_configuration=ai.name)

    def test_enabled_registration_requires_required_capture_output_and_selection(self):
        flow, _ = self.consumer_flow(parameter=False)
        settings = self.select('registration', flow, enabled=False)
        settings.registration_enabled = 1
        with self.assertRaisesRegex(frappe.ValidationError, 'at least one required input'):
            settings.save()
        settings.reload()
        self.assertFalse(settings.registration_enabled)
        parameter = self.parameter('Int', min_value=0, max_value=9)
        empty = self.flow(method='controlled_consumer',
            parameters=[dict(parameter=parameter.name, process_argument='value', required=1, capture_order=1)],
            outputs=[dict(status_code='registered', output_mode='Message')], output_fields={'registered': {}})
        settings = self.select('registration', empty, states=('registered',), enabled=False)
        settings.registration_enabled = 1
        with self.assertRaisesRegex(frappe.ValidationError, 'must declare at least one output field'):
            settings.save()
        settings.reload()
        self.assertFalse(settings.registration_enabled)
        valid, _ = self.consumer_flow()
        settings = self.select('registration', valid, enabled=False)
        settings.registration_enabled = 1
        for row in settings.registration_fields:
            row.keep = 0
        with self.assertRaisesRegex(frappe.ValidationError, 'Select Add to Registration'):
            settings.save()
        settings.reload()
        settings.registration_enabled = 1
        for row in settings.registration_fields:
            row.keep = int(row.field_key == 'full_name')
        settings.save(); settings.reload()
        self.assertEqual([row.field_key for row in settings.registration_fields if row.keep], ['full_name'])
        self.assertEqual(settings.registration_fields[0].label, 'Nombre completo')
        settings.set('registration_states', [])
        with self.assertRaisesRegex(frappe.ValidationError, 'Select at least one Complete Flow state'):
            settings.save()
        settings.reload()
        settings.registration_fields[0].field_key = 'invented'
        with self.assertRaises(frappe.ValidationError):
            settings.save()

    def test_enabled_registration_protects_required_inputs_and_selected_outputs_on_flow_save(self):
        from messaging.registry import load_declarations
        from messaging.presets import register as defaults
        default_definitions = lambda: load_declarations(defaults.__file__, 'processes')
        flow, _ = self.consumer_flow()
        self.select('registration', flow)
        declaration = _dialogue_contracts[flow.process_definition]
        original_inputs = declaration['inputs']
        declaration['inputs'] = [{**item, 'required': False} for item in original_inputs]
        flow.parameters[0].required = 0
        with self.assertRaisesRegex(frappe.ValidationError, 'Process parameter requirements are defined by the contract'):
            flow.save()
        declaration['inputs'] = original_inputs
        flow.reload()
        original_outputs = declaration['outputs']
        declaration['outputs'] = {**original_outputs, 'registered': {}}
        register_processes(app='messaging', processes=[*default_definitions(), *self.fixture_definitions])
        with self.assertRaises(frappe.ValidationError):
            flow.save()
        declaration['outputs'] = original_outputs
        register_processes(app='messaging', processes=[*default_definitions(), *self.fixture_definitions])
        flow.reload()
        self.assertTrue(flow.parameters[0].required)
        flow.workflow_name = 'Updated registration label'
        flow.save()
        self.assertEqual(flow.workflow_name, 'Updated registration label')
        self.configure(registration_enabled=0)
        declaration['inputs'] = [{**item, 'required': False} for item in original_inputs]
        register_processes(app='messaging', processes=[*default_definitions(), *self.fixture_definitions])
        flow.parameters[0].required = 0
        flow.save()
        self.assertFalse(flow.parameters[0].required)

    def test_native_multiselect_union_membership_and_type_consistency(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import options, configuration, field_union
        from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
        flow, _ = self.consumer_flow()
        settings = self.select('registration', flow, enabled=False)
        meta = frappe.get_meta('Bot Settings')
        for field in ('registration_states', 'human_transfer_states'):
            self.assertEqual(meta.get_field(field).fieldtype, 'Table MultiSelect')
            child = frappe.get_meta(meta.get_field(field).options)
            self.assertEqual(child.get_field('state').options, 'Bot Flow State')
        available = options(flow.name)['states']
        self.assertEqual({row['status_code'] for row in available}, {'registered', 'already_registered', 'unlisted'})
        self.assertEqual({row.field_key for row in settings.registration_fields}, {'full_name', 'identity_number', 'reference'})
        self.assertEqual(len(settings.registration_fields), 3)
        self.assertEqual(configuration(settings, 'registration')['fields']['full_name']['states'], ['registered', 'already_registered'])
        settings.registration_enabled = 1
        settings.save()
        for states in [['cancelled'], ['retry'], ['registered', 'registered'], ['absent']]:
            settings.reload()
            settings.set('registration_states', [dict(state=state_name(flow.name, status)) for status in states])
            with self.subTest(states=states), self.assertRaises(frappe.ValidationError): settings.save()
        with self.assertRaises(frappe.ValidationError):
            field_union({'one': dict(action='Complete Flow', fields={'same': 'string'}),
                'two': dict(action='Complete Flow', fields={'same': 'integer'})}, ['one', 'two'])
        settings.reload(); settings.registration_fields[0].field_key = 'invented'
        with self.assertRaises(frappe.ValidationError): settings.save()
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(dict(doctype='Bot Flow State', bot_flow=flow.name, status_code='fake')).insert(ignore_permissions=True)

    def test_registration_precedes_ai_persists_labels_and_resumes_original_once(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        from messaging.chatbot.doctype.bot_settings.bot_settings import is_registration_complete
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message
        flow, value = self.consumer_flow()
        business = self.flow(outputs=[dict(status_code='success', output_mode='Message')])
        self.select('registration', flow)
        self.natural()
        original = self.event(text='Please process my original request.')
        seen = []
        def classify(text, *args, **kwargs):
            contact = frappe.get_doc('Chat Contact', first['contact'])
            self.assertTrue(is_registration_complete(contact))
            self.assertEqual(json.loads(contact.parameter_values)['full_name'], 'Juan Pérez')
            seen.append(text)
            return Decision(flows=[business.name], outcome='recognized')
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', side_effect=classify) as ai:
            first = process_message(original)
            self.assertEqual(ai.call_count, 0)
            self.assertIn('Initial input.', first['response'])
            held = self.state(first)['registration_request']
            contact = frappe.get_doc('Chat Contact', first['contact'])
            contact.parameter_values = json.dumps({'historical': 'preserved'})
            contact.save(ignore_permissions=True); frappe.db.commit()
            invalid = process_message(self.event(text='invalid'))
            self.assertEqual(ai.call_count, 0)
            self.assertEqual(self.state(invalid)['registration_request'], held)
            done_request = self.event(text='1')
            done = process_message(done_request)
            self.assertEqual(seen, ['Please process my original request.'])
            self.assertEqual(CALLS, [1, None])
            self.assertLess(done['response'].index('Consumer result: registered.'), done['response'].index('Process reply'))
            self.assertEqual(process_message(done_request), done)
            self.assertEqual(ai.call_count, 1)
        contact.reload()
        self.assertEqual(json.loads(contact.parameter_values), {'historical': 'preserved', 'full_name': 'Juan Pérez', 'identity_number': '0801199012345'})
        self.assertEqual(json.loads(contact.profile_labels)['full_name'], 'Nombre completo')
        self.assertTrue(is_registration_complete(contact))
        self.assertFalse(self.state(done).get('registration_request'))
        self.assertEqual({row['key']: row['label'] for row in contact.profile_fields()}['full_name'], 'Nombre completo')

    def test_pending_mechanical_registration_keeps_its_mode_after_interpretation_is_enabled(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message
        registration, _ = self.consumer_flow()
        business = self.flow(outputs=[dict(status_code='success', output_mode='Message')])
        self.select('registration', registration)
        self.natural()
        self.configure(interpretation_enabled=0)
        first = process_message(self.event(text='Please show the menu.'))
        self.assertEqual(self.state(first)['interaction_mode'], 'Mechanical')
        self.configure(interpretation_enabled=1)
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
            return_value=Decision(flows=[business.name], outcome='recognized')) as ai:
            done = process_message(self.event(text='1'))
            self.assertEqual(ai.call_count, 0)
            self.assertIn('Consumer result: registered.', done['response'])
            self.assertEqual(self.state(done)['interaction_mode'], 'Mechanical')
            self.assertTrue(self.state(done).get('mechanical_menu'))
            following = process_message(self.event(text='Process this new natural request.'))
            self.assertEqual(ai.call_count, 1)
            self.assertNotIn('Consumer result: registered.', following['response'])

    def test_registration_uses_only_fields_of_actual_state_and_rejects_fieldless_selection(self):
        from messaging.chatbot.doctype.bot_settings.bot_settings import is_registration_complete
        flow, value = self.consumer_flow()
        self.select('registration', flow)
        self.send(text='original')
        done = self.send(text='2')
        contact = frappe.get_doc('Chat Contact', done['contact'])
        self.assertEqual(json.loads(contact.parameter_values), {'full_name': 'Juan Pérez', 'reference': 'REF-1'})
        self.assertTrue(is_registration_complete(contact))
        self.assertNotIn('identity_number', json.loads(contact.parameter_values))
        self.configure(registration_enabled=0)
        with self.assertRaisesRegex(frappe.ValidationError, 'Select Add to Registration'):
            self.select('registration', flow, keep=())

    def test_registration_cancel_unlisted_retry_and_technical_failure_retain_original(self):
        flow, value = self.consumer_flow()
        self.select('registration', flow)
        for number in (6, 3, 0, 4, 5):
            sender = 'case-' + str(number)
            first = self.send(sender=sender, text='retained')
            done = self.send(sender=sender, text=str(number))
            contact = frappe.get_doc('Chat Contact', done['contact'])
            self.assertNotEqual(contact.registration_status, 'Complete')
            self.assertEqual(self.state(first)['registration_request'], self.state(done)['registration_request'])
            self.assertEqual(json.loads(contact.parameter_values), {})
            if number == 0:
                self.assertEqual(self.state(done)['queue'][0]['process_attempt'], 1)
                completed = self.send(sender=sender, text='1')
                self.assertIn('Consumer result: registered.', completed['response'])
            elif number in (4, 5):
                self.assertNotIn('Consumer result:', done['response'])
                self.assertEqual(frappe.db.get_value('Chat Session', done['session'], 'status'), 'Recovery Required')

    def test_failed_profile_persistence_never_confirms_or_reinvokes(self):
        flow, _ = self.consumer_flow()
        self.select('registration', flow)
        self.send(text='original')
        with patch('messaging.conversation.doctype.chat_contact.chat_contact.complete_registration', side_effect=frappe.ValidationError('Synthetic persistence failure')):
            result = self.send(text='1')
        self.assertNotIn('Consumer result: registered.', result['response'])
        self.assertTrue(self.state(result)['registration_request'])
        self.assertEqual(CALLS, [1])
        self.send(text='again')
        self.assertEqual(CALLS, [1])

    def test_human_direct_flow_retry_and_per_request_data_never_update_profile(self):
        flow, _ = self.consumer_flow()
        self.select('human', flow, keep=('full_name', 'reference'))
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None), \
             patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('No AI during Human capture')):
            first = self.send(human_requested=True)
            retry = self.send(text='0')
            self.assertEqual(self.state(retry)['queue'][0]['process_attempt'], 1)
            done_request = self.event(text='2')
            done = process_event(done_request)
            self.assertEqual(process_event(done_request), done)
        session = frappe.get_doc('Chat Session', done['session'])
        self.assertEqual(session.status, 'Human Queued')
        self.assertEqual(done['response'], 'Consumer result: already_registered.')
        self.assertNotIn('Synthetic human active', done['response'])
        fields = json.loads(session.human_requests)[0]['fields']
        self.assertEqual({r['key']: r['value'] for r in fields}, {'full_name': 'Juan Pérez', 'reference': 'REF-1'})
        self.assertEqual(json.loads(frappe.db.get_value('Chat Contact', done['contact'], 'parameter_values')), {})
        self.assertEqual(len(json.loads(session.human_requests)), 1)
        from messaging.conversation.doctype.chat_session.inbox import history
        self.assertEqual(history(session.name)['human_requests'][0]['fields'], fields)
        self.assertEqual(CALLS, [0, 2])

    def test_human_cancel_unlisted_disabled_and_effective_assignment(self):
        from messaging.chatbot.doctype.bot_settings.localization import text
        flow, _ = self.consumer_flow()
        self.select('human', flow, keep=())
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            for number in (6, 3):
                self.send(sender=str(number), human_requested=True)
                done = self.send(sender=str(number), text=str(number))
                self.assertEqual(done['response'], 'Consumer result: ' + ('cancelled' if number == 6 else 'unlisted') + '.')
                self.assertEqual(frappe.db.get_value('Chat Session', done['session'], 'status'), 'Active')
        self.send(sender='assigned', human_requested=True)
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value='Administrator') as cap_check:
            done = self.send(sender='assigned', text='1')
        cap_check.assert_not_called()
        self.assertEqual(done['response'], 'Consumer result: registered.')
        self.assertEqual(frappe.db.get_value('Chat Session', done['session'], 'status'), 'Human Queued')
        self.assertFalse(frappe.db.get_value('Chat Session', done['session'], 'assigned_agent'))
        from messaging.conversation.doctype.chat_session import human
        from messaging.conversation.doctype.chat_message.chat_message import dispatch_content
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value='Administrator'), \
             patch('messaging.chatbot.doctype.bot_dispatch.delivery.deliver_human_locked') as delivered:
            human.act(done['session'], 'Take')
        self.assertEqual(delivered.call_count, 1)
        self.assertEqual(frappe.db.get_value('Chat Session', done['session'], 'status'), 'Human Active')
        self.assertEqual(frappe.db.get_value('Chat Session', done['session'], 'assigned_agent'), 'Administrator')
        notice = frappe.get_doc('Bot Dispatch', {'session': done['session'], 'message_type': 'Human'})
        self.assertEqual(dispatch_content(notice), text(frappe.get_single('Bot Settings'), 'human_active_message'))
        self.configure(human_attention_enabled=0)
        count = len(CALLS)
        disabled = self.send(sender='disabled', human_requested=True)
        self.assertEqual(disabled['response'], text(frappe.get_single('Bot Settings'), 'human_disabled_message'))
        self.assertEqual(len(CALLS), count)

    def test_enabled_registration_rejects_removing_the_success_action(self):
        flow, _ = self.consumer_flow()
        self.select('registration', flow)
        _dialogue_contracts[flow.process_definition]['actions']['registered'] = 'cancel_flow'
        from messaging.registry import load_declarations
        from messaging.presets import register as defaults
        default_definitions = lambda: load_declarations(defaults.__file__, 'processes')
        register_processes(app='messaging', processes=[*default_definitions(), *self.fixture_definitions])
        flow.process_contract = None
        flow.outputs[0].flow_action = 'Cancel Flow'
        with self.assertRaises(frappe.ValidationError):
            flow.save()
        _dialogue_contracts[flow.process_definition]['actions']['registered'] = 'complete_flow'
        register_processes(app='messaging', processes=[*default_definitions(), *self.fixture_definitions])
        flow.reload()
        self.assertEqual(flow.outputs[0].flow_action, 'Complete Flow')
        frappe.get_single('Bot Settings').require_operational_configuration()

    def test_registration_quota_exemption_does_not_cover_resumed_business(self):
        flow, _ = self.consumer_flow()
        business = self.flow(outputs=[dict(status_code='success', output_mode='Message')])
        self.select('registration', flow)
        self.configure(quotas_enabled=1, daily_quota_limit=1, monthly_quota_limit=10)
        first = self.send(flows=[business.name])
        self.assertEqual(frappe.db.get_value('Bot Dispatch', first['dispatch'], 'quota_kind'), 'Registration')
        done = self.send(text='1')
        self.assertEqual(frappe.db.get_value('Bot Dispatch', done['dispatch'], 'quota_kind'), 'Standard')
        self.assertEqual(CALLS, [1, None])

    def test_pending_consumers_reject_another_flow_before_business_invocation(self):
        flow, _ = self.consumer_flow()
        other, _ = self.consumer_flow()
        for consumer in ('registration', 'human'):
            state_field = 'registration_states' if consumer == 'registration' else 'human_transfer_states'
            enabled_field = 'registration_enabled' if consumer == 'registration' else 'human_attention_enabled'
            self.select(consumer, flow)
            first = self.send(sender=consumer, human_requested=consumer == 'human', text='original')
            self.configure(**{enabled_field: 0, state_field: [], consumer + '_fields': [], consumer + '_flow': other.name})
            self.select(consumer, other)
            changed = self.send(sender=consumer, text='1')
            self.assertEqual(CALLS, [])
            self.assertNotIn('Consumer result:', changed['response'])
            if consumer == 'registration':
                self.assertEqual(self.state(changed)['registration_request'], self.state(first)['registration_request'])
                self.configure(registration_enabled=0)

    def test_configuration_is_checked_again_after_durable_running_marker(self):
        from messaging.conversation.doctype.chat_session import chat_session
        flow, _ = self.consumer_flow()
        self.select('registration', flow)
        first = self.send(text='original')
        original_commit = frappe.db.commit
        changed = False
        def change_at_checkpoint():
            nonlocal changed
            original_commit()
            if not changed and frappe.db.exists('Chat Session', {'name':first['session'], 'status':'Executing'}):
                changed = True
                self.configure(registration_enabled=0)
        with patch.object(frappe.db, 'commit', side_effect=change_at_checkpoint):
            done = self.send(text='1')
        self.assertEqual(CALLS, [])
        self.assertNotIn('Consumer result:', done['response'])
        self.assertTrue(frappe.db.exists('Process Execution Log', dict(turn=done['turn'], diagnostic_error_type='ConsumerConfigurationChanged')))
        self.assertEqual(self.state(first)['registration_request'], self.state(done)['registration_request'])

    def test_registration_retry_after_definitive_technical_notice_preserves_original(self):
        flow, _ = self.consumer_flow()
        self.select('registration', flow)
        first = self.send(text='original')
        failure = self.send(text='4')
        simulate_dispatch(failure['dispatch'])
        prompt = self.send(text='retry')
        self.assertEqual(self.state(first)['registration_request'], self.state(prompt)['registration_request'])
        self.assertEqual(CALLS, [4])
        done = self.send(text='1')
        self.assertIn('Consumer result: registered.', done['response'])
        self.assertFalse(self.state(done).get('registration_request'))
        self.assertEqual(CALLS, [4, 1])

    def test_registration_unknown_reconciliation_never_reinvokes_business(self):
        flow, _ = self.consumer_flow()
        self.select('registration', flow)
        first = self.send(text='original')
        unknown = self.send(text='7')
        execution = json.loads(frappe.db.get_value('Chat Turn', unknown['turn'], 'executions'))[0]['id']
        response = dict(version=1, recipient='', status='registered',
            data={'full_name': 'Juan Pérez', 'identity_number': '0801199012345'}, message='Verified registration.')
        recovered = reconcile_execution(unknown['turn'], execution, response)
        self.assertEqual(CALLS, [7])
        self.assertEqual(frappe.db.get_value('Chat Contact', first['contact'], 'registration_status'), 'Complete')
        self.assertTrue(self.state(recovered)['registration_request'])
        reconcile_execution(unknown['turn'], execution, response)
        done = self.send(text='continue')
        self.assertEqual(CALLS, [7])
        self.assertFalse(self.state(done).get('registration_request'))

    def test_retained_registration_input_is_redacted_before_provider_classification(self):
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import synthetic_wire_response as response
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message
        flow, _ = self.consumer_flow()
        business = self.flow(outputs=[dict(status_code='success', output_mode='Message')])
        self.select('registration', flow)
        self.natural()
        seen = []
        def provider(_provider, _values, request, **kwargs):
            seen.append(request.message.text)
            self.assertNotIn('number 8.', request.message.text)
            self.assertNotIn('capture_pending', __import__('dataclasses').asdict(request.context))
            ref = next(row.ref for row in request.available_flows if row.label == business.workflow_name)
            return response(request, Decision(flows=[ref], outcome='recognized'), .99)
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=provider) as ai:
            process_message(self.event(text='Please process my request with number 8.'))
            self.assertEqual(ai.call_count, 0)
            done = process_message(self.event(text='1'))
            self.assertEqual(ai.call_count, 1)
            self.assertIn('Process reply', done['response'])
        self.assertEqual(CALLS, [1, None])

    def test_mechanical_registration_precedes_menu_and_replays_original(self):
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message
        flow, _ = self.consumer_flow()
        business = self.flow(outputs=[dict(status_code='success', output_mode='Message')])
        self.select('registration', flow)
        self.configure(interpretation_enabled=0)
        original = 'I would like my balance.'
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret') as ai:
            prompt = process_message(self.event(text=original))
            self.assertTrue(self.state(prompt)['registration_request'])
            self.assertNotIn('mechanical_menu', self.state(prompt))
            self.assertEqual(CALLS, [])
            done = process_message(self.event(text='1'))
            self.assertEqual(CALLS, [1])
            self.assertIn('Consumer result: registered.', done['response'])
            self.assertTrue(self.state(done).get('mechanical_menu'))
            ai.assert_not_called()

    def greeting_flow(self):
        from messaging.presets.processes import greeting as greeting_process
        from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
        flow = self.flow(handler=greeting_process.definition, business=greeting_process.handle, show_in_menu=0,
            outputs=[dict(status_code='greeted', output_mode='Template', body='Configured greeting.')])
        self.configure(greeting_enabled=1, greeting_flow=flow.name,
            greeting_states=[dict(state=state_name(flow.name, 'greeted'))], show_menu_on_greeting_only=0)
        return flow

    def test_one_classification_greeting_multiple_flows_capture_and_next_request(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision, catalog
        greeting = self.greeting_flow()
        parameter = self.parameter('Int', min_value=1, max_value=9)
        inputs=[dict(parameter=parameter.name,process_argument='value',required=1,capture_order=1)]
        first_flow=self.flow(parameters=inputs)
        second_flow=self.flow(parameters=inputs)
        next_flow=self.flow()
        self.natural()
        self.assertNotIn(greeting.name,catalog())
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
            side_effect=[Decision(flows=[first_flow.name,second_flow.name],greeting_detected=True,outcome='recognized'),
                Decision(flows=[next_flow.name],outcome='recognized')]) as ai:
            start=process_message(self.event(text='Hello, balance and service status.'))
            self.assertEqual(ai.call_count,1)
            self.assertIn('Configured greeting.',start['response'])
            self.assertEqual(self.state(start)['queue'][0]['invalid_attempts'],0)
            simulate_dispatch(start['dispatch'])
            wrong_request=self.event(text='Also tell me your opening hours.')
            wrong=process_message(wrong_request)
            self.assertEqual(ai.call_count,1)
            self.assertEqual(self.state(wrong)['queue'][0]['invalid_attempts'],1)
            self.assertEqual(len(self.state(wrong)['queue']),2)
            self.assertNotIn('Configured greeting.',wrong['response'])
            self.assertEqual(process_message(wrong_request),wrong)
            simulate_dispatch(wrong['dispatch'])
            done=process_message(self.event(text='1'))
            self.assertEqual(ai.call_count,1)
            self.assertFalse(self.state(done)['queue'])
            self.assertEqual(CALLS,[1,1])
            simulate_dispatch(done['dispatch'])
            fresh=process_message(self.event(text='What are your opening hours?'))
            self.assertEqual(ai.call_count,2)
            self.assertEqual(fresh['session'],start['session'])

    def test_decision_human_signal_enters_filter_and_preserves_mixed_queue(self):
        from messaging.registry.ai_providers import DecisionResponse, DecisionOutcome
        human, _ = self.consumer_flow()
        self.select('human', human, keep=())
        greeting = self.greeting_flow()
        business = self.flow()
        self.natural()
        cases = [
            ('Quiero hablar con un operador', False, False, False, ('1',), True),
            ('Me quiero comunicar con una persona', False, False, False, ('0', '2'), True),
            ('Necesito atención humana y mi saldo', False, True, False, ('6',), False),
            ('Hola, quisiera hablar con un agente', True, False, False, ('1',), True),
            ('Quiero una persona y mi saldo', False, True, False, ('1',), True),
            ('Quiero una persona y esa otra consulta', False, False, True, ('1',), True),
        ]
        for index, (text, greet, mixed, ambiguous, answers, transfers) in enumerate(cases):
            with self.subTest(text=text):
                CALLS.clear()
                def classify(provider, values, request, **kwargs):
                    self.assertTrue(request.human_attention_available)
                    self.assertFalse({human.workflow_name, greeting.workflow_name} & {row.label for row in request.available_flows})
                    refs = tuple(row.ref for row in request.available_flows if row.label == business.workflow_name) if mixed else ()
                    return DecisionResponse(request.request_id,
                        DecisionOutcome.AMBIGUOUS if ambiguous else DecisionOutcome.RECOGNIZED,
                        refs, greeting_detected=greet, human_attention_requested=True)
                with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=classify) as ai, \
                     patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
                    start = process_message(self.event(sender=str(index), text=text))
                    self.assertEqual(ai.call_count, 1)
                    self.assertEqual(frappe.get_doc('Chat Session', start['session']).status, 'Capturing')
                    self.assertEqual(self.state(start)['queue'][0]['flow'], human.name)
                    self.assertEqual(CALLS, [])
                    self.assertEqual(json.loads(frappe.get_doc('Chat Session', start['session']).human_requests or '[]'), [])
                    simulate_dispatch(start['dispatch'])
                    for answer in answers:
                        done = process_message(self.event(sender=str(index), text=answer))
                        simulate_dispatch(done['dispatch'])
                    self.assertEqual(ai.call_count, 1)
                session = frappe.get_doc('Chat Session', done['session'])
                self.assertEqual(session.status, 'Human Queued' if transfers else 'Active')
                self.assertFalse(session.assigned_agent)
                self.assertEqual(len(json.loads(session.human_requests or '[]')), int(transfers))
                self.assertFalse(self.state(done)['queue'])
                self.assertEqual(CALLS, [int(answer) for answer in answers] + ([None] if mixed and not transfers else []))
                self.assertNotIn('clarify', done['response'])

    def test_human_decision_waits_for_registration_and_classifies_original_once(self):
        from messaging.registry.ai_providers import DecisionResponse, DecisionOutcome
        registration, _ = self.consumer_flow()
        self.select('registration', registration)
        human, _ = self.consumer_flow()
        self.select('human', human, keep=())
        self.natural()
        original = 'Me quiero comunicar con una persona'
        def classify(provider, values, request, **kwargs):
            self.assertEqual(request.message.text, original)
            self.assertTrue(request.human_attention_available)
            return DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, human_attention_requested=True)
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=classify) as ai, \
             patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            first = process_message(self.event(text=original))
            ai.assert_not_called()
            self.assertEqual(self.state(first)['queue'][0]['flow'], registration.name)
            simulate_dispatch(first['dispatch'])
            registered = process_message(self.event(text='1'))
            self.assertEqual(ai.call_count, 1)
            self.assertEqual(self.state(registered)['queue'][0]['flow'], human.name)
            simulate_dispatch(registered['dispatch'])
            done = process_message(self.event(text='2'))
            self.assertEqual(ai.call_count, 1)
        self.assertEqual(frappe.get_doc('Chat Session', done['session']).status, 'Human Queued')
        self.assertEqual(CALLS, [1, 2])

    def test_disabled_human_signal_cannot_start_filter(self):
        from messaging.registry.ai_providers import DecisionResponse, DecisionOutcome
        human, _ = self.consumer_flow()
        self.select('human', human, keep=(), enabled=False)
        self.natural()
        def classify(provider, values, request, **kwargs):
            self.assertFalse(request.human_attention_available)
            return DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, human_attention_requested=True)
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=classify) as ai:
            result = process_message(self.event(text='Me quiero comunicar con una persona'))
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(CALLS, [])
        self.assertFalse(self.state(result)['queue'])
        self.assertEqual(frappe.get_doc('Chat Session', result['session']).status, 'Active')
        self.assertEqual(json.loads(frappe.get_doc('Chat Turn', result['turn']).decisions)[0]['outcome'], 'invalid')

    def test_mechanical_human_option_still_starts_the_same_filter_without_decision(self):
        human, _ = self.consumer_flow()
        self.select('human', human, keep=())
        self.configure(interpretation_enabled=0)
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('Mechanical input cannot call Decision')), \
             patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            menu = process_message(self.event(text='Me quiero comunicar con una persona'))
            self.assertFalse(self.state(menu)['queue'])
            number = next(key for key, value in self.state(menu)['mechanical_menu']['options'].items()
                if value.get('action') == 'Human')
            simulate_dispatch(menu['dispatch'])
            pending = process_message(self.event(text=number))
            self.assertEqual(self.state(pending)['queue'][0]['flow'], human.name)
            self.assertTrue(self.state(pending)['human_attention'])
            simulate_dispatch(pending['dispatch'])
            done = process_message(self.event(text='1'))
        self.assertEqual(frappe.get_doc('Chat Session', done['session']).status, 'Human Queued')
        self.assertEqual(CALLS, [1])

    def test_mechanical_initial_request_always_greets_then_shows_menu(self):
        greeting=self.greeting_flow()
        business=self.flow()
        self.configure(interpretation_enabled=0,informative_categories_header='Business categories:')
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret') as ai:
            first=process_message(self.event(text='I would like my balance.'))
            self.assertIn('Configured greeting.',first['response'])
            self.assertLess(first['response'].index('Configured greeting.'),first['response'].index('Business categories:'))
            options=self.state(first)['mechanical_menu']['options']
            self.assertEqual({row['flow'] for row in options.values()},{business.name})
            ai.assert_not_called()
        self.configure(greeting_enabled=0)
        second=process_message(self.event(sender='another-contact',text='A free text business request.'))
        self.assertNotIn('Configured greeting.',second['response'])
        self.assertIn('Business categories:',second['response'])

    def test_ambiguous_business_can_greet_without_executing_business(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        self.greeting_flow();self.flow();self.natural()
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
            return_value=Decision(greeting_detected=True,ambiguous=True,outcome='ambiguous')):
            result=process_message(self.event(text='Hello, that other thing.'))
        self.assertIn('Configured greeting.',result['response'])
        self.assertIn('clarify',result['response'])
        self.assertEqual(CALLS,[])

    def test_unusable_decision_configuration_uses_mechanical_menu(self):
        self.greeting_flow();self.flow();self.natural()
        with patch('messaging.chatbot.doctype.ai_configuration.ai_configuration.availability',return_value={'operational':False}), \
            patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret') as ai:
            result=process_message(self.event(text='A business request.'))
        self.assertEqual(self.state(result)['interaction_mode'],'Mechanical')
        self.assertIn('Configured greeting.',result['response'])
        self.assertTrue(self.state(result)['mechanical_menu'])
        ai.assert_not_called()

    def test_structured_selection_cannot_extend_an_active_capture(self):
        parameter=self.parameter('Int',min_value=1,max_value=9)
        owner=self.flow(parameters=[dict(parameter=parameter.name,process_argument='value',required=1,capture_order=1)])
        other=self.flow()
        self.send(flows=[owner.name])
        result=self.send(flows=[other.name],text='Also tell me the hours.')
        self.assertEqual([row['flow'] for row in self.state(result)['queue']],[owner.name])
        self.assertEqual(CALLS,[])

    def test_ai_disabled_greeting_does_not_change_selected_business(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        greeting=self.greeting_flow();business=self.flow();self.natural();self.configure(greeting_enabled=0)
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
            return_value=Decision(flows=[business.name],outcome='recognized')):
            result=process_message(self.event(text='Hello, my request.'))
        self.assertNotIn('Configured greeting.',result['response'])
        self.assertEqual(CALLS,[None])


def inbox_arrival(site, sites_path, account, sender, results):
    frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
    try:
        from messaging.emulator.page.emulator_console.emulator_console import send_message
        with patch('messaging.conversation.doctype.chat_contact.chat_contact._enqueue'):
            results.put(send_message(account, sender, 'd' * 32, text='Later independent request'))
    except Exception as exc:
        results.put({'error': type(exc).__name__, 'detail': str(exc)})
    finally:
        frappe.db.rollback(); frappe.destroy()


def inbox_competitor(site, sites_path, contact, results):
    frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
    try:
        from messaging.conversation.doctype.chat_turn.inbox import drain_contact
        results.put(drain_contact(contact))
    except Exception as exc:
        results.put({'error': type(exc).__name__, 'detail': str(exc)})
    finally:
        frappe.db.rollback(); frappe.destroy()


def rq_deadline_probe():
    """Exercise actual deadline expiry inside an isolated native RQ job without mutating SIGALRM."""
    import signal
    import threading
    from messaging.communication.doctype.process_definition import execution
    interrupted = False
    with patch.object(execution, 'PROCESS_TIMEOUT_SECONDS', .02):
        try:
            with execution.execution_deadline():
                time.sleep(.1)
        except TimeoutError:
            interrupted = True
    return dict(main_thread=threading.current_thread() is threading.main_thread(),
        interrupted=interrupted, outer_timer_preserved=signal.getitimer(signal.ITIMER_REAL)[0] > 0)


def shared_source_reader(site, sites_path, flow, results):
    frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
    try:
        from messaging.common.locking import shared_document
        source = shared_document('Bot Flow', flow)
        rejected = False
        try:
            frappe.db.sql('SELECT name FROM `tabBot Flow` WHERE name=%s FOR UPDATE NOWAIT', (flow,))
        except frappe.QueryTimeoutError:
            rejected = True
        results.put(dict(source=source.name, exclusive_rejected=rejected))
    except Exception as exc:
        results.put(dict(error=type(exc).__name__, detail=str(exc)))
    finally:
        frappe.db.rollback(); frappe.destroy()


class TestAsyncInbox(unittest.TestCase):
    """Ingress persistence, ordered workers, native receipts and isolated real RQ evidence."""
    configure = TestChatTurn.configure
    ensure_test_channel = TestChatTurn.ensure_test_channel
    flow = TestChatTurn.flow
    parameter = TestChatTurn.parameter

    def setUp(self):
        from messaging.emulator.doctype.emulator_contact.fixtures import bind_emulator
        TestChatTurn.setUp(self)
        bind_emulator(self)

    def tearDown(self):
        from messaging.emulator.doctype.emulator_contact.fixtures import release_emulator
        release_emulator(self, lambda: TestChatTurn.tearDown(self))

    def account(self, *args, **kwargs):
        from messaging.emulator.doctype.emulator_contact.fixtures import emulator_account
        return emulator_account(self, *args, **kwargs)

    def contact(self, **values):
        from messaging.emulator.doctype.emulator_contact.fixtures import emulator_contact
        return emulator_contact(self, **values)

    def message(self, account=None, sender=None, text='Hello', client_id=None, **kwargs):
        from messaging.emulator.page.emulator_console.emulator_console import send_message
        return send_message(account or self.a, sender or self.sender,
            client_id or frappe.generate_hash(length=32), text=text, **kwargs)

    def natural(self):
        from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
        configuration = create_configuration(self.prefix + '_ai', api_key='synthetic-inbox-key')
        def cleanup():
            frappe.set_user('Administrator')
            frappe.delete_doc('AI Configuration', configuration.name, delete_permanently=True)
            frappe.db.commit()
        self.addCleanup(cleanup)
        self.configure(interpretation_enabled=1, active_ai_configuration=configuration.name, greeting_enabled=0)

    def turn(self, result):
        return frappe.get_doc('Chat Turn', result['turn'])

    def drain(self, result):
        from messaging.conversation.doctype.chat_turn.inbox import drain_contact
        drain_contact(result['contact'])
        return self.turn(result)

    def test_receipt_commit_dedupe_and_enqueue_failure_recovery(self):
        from messaging.conversation.doctype.chat_contact import chat_contact as contacts
        from messaging.conversation.doctype.chat_turn.inbox import recover_pending
        from redis.exceptions import ConnectionError
        seen = []
        def after_commit(contact):
            # A second real connection can see the complete receipt at enqueue time.
            db = frappe.db._get_connection()
            try:
                cursor = db.cursor()
                cursor.execute('SELECT COUNT(*) FROM `tabChat Message` WHERE contact=%s', (contact,))
                seen.append(cursor.fetchone()[0])
            finally:
                db.close()
        with patch.object(contacts, '_enqueue', side_effect=after_commit):
            first = self.message(text='Durable before Redis', client_id='a' * 32)
        self.assertEqual(seen, [1])
        self.assertEqual(self.turn(first).inbox_state, 'Pending')
        self.assertFalse(first['dispatch'])
        self.assertEqual(self.message(text='Durable before Redis', client_id='a' * 32), first)
        self.inbox_enqueue.stop()
        try:
            with patch('frappe.enqueue', side_effect=ConnectionError('Synthetic Redis failure')):
                # Invoke the real safe callback, normally installed on the transaction.
                contacts._enqueue(first['contact'])
        finally:
            self.inbox_enqueue.start()
        self.assertEqual(self.turn(first).inbox_state, 'Pending')
        with patch.object(contacts, '_enqueue') as enqueue:
            recover_pending()
        enqueue.assert_any_call(first['contact'])
        self.drain(first)
        self.assertEqual(self.turn(first).inbox_state, 'Done')
        self.assertEqual(frappe.db.count('Chat Message', {'contact': first['contact'], 'kind': 'Incoming'}), 1)

    def test_callback_is_discarded_on_rollback(self):
        from messaging.conversation.doctype.chat_turn.inbox import schedule_contact
        with patch('messaging.conversation.doctype.chat_contact.chat_contact._enqueue') as enqueue:
            schedule_contact('synthetic-rollback')
            enqueue.assert_not_called()
            frappe.db.rollback()
            frappe.db.commit()
            enqueue.assert_not_called()

    def test_two_workers_same_contact_and_other_contact_progresses(self):
        from messaging.common.locking import contact_lock
        one = self.message(text='A1')
        other = self.message(sender=self.contact().sender_id, text='B1')
        key = frappe.db.get_value('Chat Contact', one['contact'], 'identity_key')
        ctx = multiprocessing.get_context('spawn')
        results = ctx.Queue()
        with contact_lock(key):
            process = ctx.Process(target=inbox_competitor, args=(frappe.local.site, frappe.local.sites_path, one['contact'], results))
            process.start(); process.join(20)
            self.assertFalse(process.is_alive())
            self.assertEqual(results.get(timeout=5), {'busy': True})
            # A second process drains B while this process still owns A.
            other_process = ctx.Process(target=inbox_competitor, args=(frappe.local.site, frappe.local.sites_path, other['contact'], results))
            other_process.start(); other_process.join(20)
            self.assertFalse(other_process.is_alive())
            self.assertIsNone(results.get(timeout=5))
            frappe.db.commit()
            self.assertEqual(self.turn(other).inbox_state, 'Done')
        self.drain(one)
        self.assertEqual(self.turn(one).inbox_state, 'Done')

    def test_order_anticipated_parameters_and_single_quota(self):
        param = self.parameter(mask='#####')
        flow = self.flow(parameters=[dict(parameter=param.name, process_argument='value', required=1, capture_order=1)])
        self.natural()
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', return_value=Decision(outcome='recognized', flows=[flow.name])) as classifier:
            first = self.message(text='Please check my balance')
            second = self.message(text='26120')
            third = self.message(text='Now show the opening hours')
            turn = self.drain(first)
        self.assertEqual(self.turn(second).consumed_by, turn.name)
        self.assertEqual(self.turn(second).inbox_state, 'Done')
        self.assertEqual(CALLS, ['26120'])
        self.assertLess(turn.receipt_order, self.turn(second).receipt_order)
        self.assertEqual(frappe.db.count('Bot Dispatch', {'turn': second['turn']}), 0)
        self.assertNotIn('Initial input.', dispatch_content(frappe.get_doc('Bot Dispatch', turn.dispatch)))
        self.assertEqual(self.turn(third).inbox_state, 'Done')
        self.assertEqual(classifier.call_count, 2)
        count = frappe.db.count('Bot Dispatch', {'contact': turn.contact})
        self.drain(first)
        self.assertEqual(frappe.db.count('Bot Dispatch', {'contact': turn.contact}), count)
        self.assertEqual(CALLS, ['26120'])

    def test_multiple_anticipated_values_are_one_immutable_invocation(self):
        param = self.parameter(mask='#####')
        flow = self.flow(method='controlled_values', parameters=[dict(parameter=param.name, process_argument='values',
            required=1, capture_order=1, allow_multiple_values=1)], outputs=[dict(status_code='success', output_mode='Message')])
        self.natural()
        first = self.message(text=flow.name)
        second = self.message(text='26120')
        third = self.message(text='Y también el 12345')
        self.drain(first)
        self.assertEqual(CALLS, [['26120', '12345']])
        self.assertEqual(self.turn(second).consumed_by, first['turn'])
        self.assertEqual(self.turn(third).consumed_by, first['turn'])

    def test_lost_claim_before_execution_is_recoverable_and_running_receipt_is_blocked(self):
        from messaging.conversation.doctype.chat_turn.inbox import drain_contact
        first = self.message(text='Before invocation')
        turn = self.turn(first); turn.inbox_state = 'Running'; turn.save(ignore_permissions=True); frappe.db.commit()
        self.drain(first)
        self.assertEqual(self.turn(first).inbox_state, 'Done')
        second = self.message(text='Uncertain operation')
        turn = self.turn(second)
        turn.session = self.turn(first).session
        turn.status = 'Processing'; turn.inbox_state = 'Running'
        turn.executions = json.dumps([dict(id='synthetic-uncertain', flow='synthetic', state='Running')])
        turn.save(ignore_permissions=True); frappe.db.commit()
        with patch('messaging.conversation.doctype.chat_turn.chat_turn.process_event', side_effect=AssertionError('Must not replay')):
            drain_contact(second['contact'])
            drain_contact(second['contact'])
        self.assertEqual(self.turn(second).inbox_state, 'Blocked')
        self.assertEqual(CALLS, [])

    def test_arrival_during_execution_does_not_change_inputs_and_runs_afterwards(self):
        flow = self.flow()
        self.natural()
        first = self.message(text=flow.name)
        from messaging.conversation.doctype.chat_session import chat_session as engine
        invoke = engine.invoke
        arrived = []
        def running(*args, **kwargs):
            ctx = multiprocessing.get_context('spawn')
            results = ctx.Queue()
            process = ctx.Process(target=inbox_arrival, args=(frappe.local.site, frappe.local.sites_path, self.a, self.sender, results))
            process.start(); process.join(20)
            self.assertFalse(process.is_alive())
            arrived.append(results.get(timeout=5))
            self.assertNotIn('error', arrived[-1])
            return invoke(*args, **kwargs)
        with patch.object(engine, 'invoke', side_effect=running):
            self.drain(first)
        self.assertEqual(CALLS, [None])
        self.assertEqual(len(arrived), 1)
        self.assertEqual(self.turn(arrived[0]).inbox_state, 'Done')
        self.assertFalse(self.turn(arrived[0]).consumed_by)

    def test_connection_fence_and_secondary_thread_deadline_enforcement(self):
        from messaging.conversation.doctype.chat_turn.inbox import _fenced_connection
        from messaging.common.locking import contact_lock
        import threading
        first = self.message(text='Pending')
        results = []
        # Verify the executor deadline works and interrupts inside a secondary thread.
        from messaging.communication.doctype.process_definition import execution
        def worker_thread():
            try:
                with execution.execution_deadline():
                    results.append('entered')
                with patch.object(execution, 'PROCESS_TIMEOUT_SECONDS', 0.02):
                    with execution.execution_deadline():
                        while True:
                            pass
            except TimeoutError:
                results.append('timed_out')
            except Exception as exc:
                results.append(type(exc).__name__ + ': ' + str(exc))
        thread = threading.Thread(target=worker_thread); thread.start(); thread.join(timeout=5)
        self.assertEqual(results, ['entered', 'timed_out'])
        key = frappe.db.get_value('Chat Contact', first['contact'], 'identity_key')
        with contact_lock(key):
            with _fenced_connection(key):
                # Release the actual lock; the next database operation must be rejected.
                frappe.db.sql('SELECT RELEASE_LOCK(%s)', ('messaging:' + key[:54],))
                with self.assertRaisesRegex(RuntimeError, 'exclusivity was lost'):
                    frappe.db.sql('SELECT 1')
        self.assertEqual(self.turn(first).inbox_state, 'Pending')
        with patch.object(frappe.local, 'request', object(), create=True):
            with self.assertRaisesRegex(frappe.PermissionError, 'background worker'):
                process_event({})

    def test_native_human_drain_contact_and_concurrent_secondary_threads(self):
        import threading
        from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
        from messaging.communication.doctype.process_definition import execution as runtime
        from messaging.conversation.doctype.chat_turn.chat_turn import turn_result
        from messaging.conversation.doctype.chat_turn.inbox import drain_contact
        from messaging.presets.processes.human_availability import (
            definition as human_definition,
            handle as human_handle,
        )

        outputs = []
        for status in ('open', 'closed', 'filtered'):
            template = frappe.get_doc(dict(
                doctype='Bot Flow Template',
                template_name=self.prefix + '_drain_' + status,
                enabled=1,
                language='es',
                body='Drain support ' + status + '.',
            )).insert()
            self.fixture_templates.append(template.name)
            outputs.append(dict(
                status_code=status,
                output_mode='Template',
                template_reference=template.name,
                flow_action='Complete Flow' if status == 'open' else 'Cancel Flow',
            ))
        flow = self.flow(handler=human_definition, business=human_handle, show_in_menu=0, outputs=outputs)
        all_open = [dict(weekday=day, enabled=1, start_time='00:00:00', end_time='23:59:59')
            for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
        all_closed = [dict(weekday=day, enabled=0)
            for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
        self.configure(
            interpretation_enabled=0,
            greeting_enabled=0,
            quotas_enabled=0,
            human_attention_enabled=1,
            human_flow=flow.name,
            human_transfer_states=[dict(state=state_name(flow.name, 'open'))],
            human_requests_required=2,
            human_support_week=all_closed,
        )

        tracked_contacts = set()
        def cleanup_human_redis():
            if not tracked_contacts:
                return
            for row in frappe.get_all(
                'Chat Message',
                filters={'contact': ['in', list(tracked_contacts)], 'kind': 'Incoming'},
                fields=['name', 'contact'],
            ):
                frappe.cache.delete(*runtime._human_request_keys(row.contact, row.name))
        self.addCleanup(cleanup_human_redis)

        def human_option_number(turn):
            state = read_state(frappe.get_doc('Chat Session', turn.session), 'dialogue_state')
            return next(key for key, row in state['mechanical_menu']['options'].items() if row.get('action') == 'Human')

        # 1. Closed schedule via real EMULR ingress + drain_contact -> 'closed', no Redis increment, no transfer.
        menu_turn = self.drain(self.message(text='0'))
        tracked_contacts.add(menu_turn.contact)
        option = human_option_number(menu_turn)
        closed_req = self.message(text=option)
        closed_turn = self.drain(closed_req)
        self.assertEqual(closed_turn.inbox_state, 'Done')
        self.assertIn('Drain support closed.', turn_result(closed_turn)['response'])
        self.assertEqual(frappe.db.get_value('Chat Session', closed_turn.session, 'status'), 'Active')
        self.assertEqual(
            frappe.db.get_value('Process Execution Log', {'turn': closed_turn.name, 'source_name': flow.name}, 'functional_status'),
            'closed',
        )

        # 2. Open schedule with threshold=2: 1st request -> 'filtered', repeat drain is idempotent, 2nd -> 'open' + transfer + reset.
        self.configure(human_support_week=all_open, human_requests_required=2)
        menu_turn = self.drain(self.message(text='0'))
        option = human_option_number(menu_turn)
        first_req = self.message(text=option)
        first_turn = self.drain(first_req)
        self.assertIn('Drain support filtered.', turn_result(first_turn)['response'])
        self.assertEqual(frappe.db.get_value('Chat Session', first_turn.session, 'status'), 'Active')
        # Re-draining the same contact does not duplicate dispatches or increment the counter again.
        drain_contact(first_turn.contact)
        self.assertEqual(frappe.db.count('Bot Dispatch', {'turn': first_turn.name}), 1)
        self.assertEqual(frappe.db.count('Process Execution Log', {'turn': first_turn.name}), 1)

        menu_turn = self.drain(self.message(text='0'))
        option = human_option_number(menu_turn)
        second_req = self.message(text=option)
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            second_turn = self.drain(second_req)
        self.assertIn('Drain support open.', turn_result(second_turn)['response'])
        self.assertEqual(frappe.db.get_value('Chat Session', second_turn.session, 'status'), 'Human Queued')
        second_msg = frappe.db.get_value('Chat Message', {'turn': second_turn.name, 'kind': 'Incoming'}, 'name')
        counter_key, receipt_key = runtime._human_request_keys(second_turn.contact, second_msg)
        self.assertFalse(frappe.cache.exists(counter_key, shared=True))
        self.assertTrue(frappe.cache.exists(receipt_key, shared=True))

        # 3. Concurrent execution in secondary threads (gthread compatibility) for two independent contacts.
        self.configure(human_support_week=all_open, human_requests_required=1)
        sender_a = self.contact().sender_id
        sender_b = self.contact().sender_id
        menu_a = self.drain(self.message(sender=sender_a, text='0'))
        menu_b = self.drain(self.message(sender=sender_b, text='0'))
        tracked_contacts.update((menu_a.contact, menu_b.contact))
        req_a = self.message(sender=sender_a, text=human_option_number(menu_a))
        req_b = self.message(sender=sender_b, text=human_option_number(menu_b))
        frappe.db.commit()

        site, sites_path = frappe.local.site, frappe.local.sites_path
        barrier = threading.Barrier(2)
        errors = {}

        def drain_in_secondary_thread(label, contact_name):
            frappe.init(site=site, sites_path=sites_path)
            frappe.connect()
            frappe.set_user('Administrator')
            try:
                barrier.wait(timeout=10)
                with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
                    drain_contact(contact_name)
                frappe.db.commit()
            except Exception as exc:
                frappe.db.rollback()
                errors[label] = exc
            finally:
                frappe.destroy()

        threads = [
            threading.Thread(target=drain_in_secondary_thread, args=('a', req_a['contact'])),
            threading.Thread(target=drain_in_secondary_thread, args=('b', req_b['contact'])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, {})
        frappe.db.rollback()
        for req in (req_a, req_b):
            turn = self.turn(req)
            self.assertEqual(turn.inbox_state, 'Done')
            self.assertIn('Drain support open.', turn_result(turn)['response'])
            self.assertEqual(frappe.db.get_value('Chat Session', turn.session, 'status'), 'Human Queued')
            self.assertEqual(
                frappe.db.get_value('Process Execution Log', {'turn': turn.name, 'source_name': flow.name}, 'functional_status'),
                'open',
            )

    def test_real_native_rq_ingress_execution_and_response(self):
        from frappe.utils.background_jobs import get_job
        self.configure(interpretation_enabled=0, greeting_enabled=0, quotas_enabled=0)
        flow = self.flow(handler=async_probe_handler, business=async_probe_result)
        # This test uses installed Redis and the already running native worker, no service changes.
        self.inbox_enqueue.stop()
        from redis.exceptions import ConnectionError
        from messaging.conversation.doctype.chat_turn.inbox import recover_pending
        with patch('frappe.enqueue', side_effect=ConnectionError('Synthetic Redis outage at receipt')):
            first = self.message(text='Hello')
        self.assertEqual(self.turn(first).inbox_state, 'Pending')
        recover_pending()
        job_id = 'messaging-inbox-' + first['contact']
        for _index in range(150):
            frappe.db.rollback()
            turn = self.turn(first)
            if turn.inbox_state == 'Done' and turn.dispatch and frappe.db.get_value('Bot Dispatch', turn.dispatch, 'status') == 'Accepted':
                break
            time.sleep(.2)
        else:
            job = get_job(job_id)
            self.fail('Native RQ did not finish: ' + str(job.get_status() if job else None) + ' / ' + str(job.exc_info if job else None))
        state = read_state(frappe.get_doc('Chat Session', turn.session), 'dialogue_state')
        number = next(n for n, row in state['mechanical_menu']['options'].items() if row['flow'] == flow.name)
        selected = self.message(text=number)
        for _index in range(200):
            frappe.db.rollback()
            turn = self.turn(selected)
            if turn.inbox_state == 'Done' and turn.dispatch and frappe.db.get_value('Bot Dispatch', turn.dispatch, 'status') == 'Accepted':
                break
            time.sleep(.2)
        else:
            job = get_job(job_id)
            self.fail('Native Process did not finish: ' + str(job.exc_info if job else None))
        self.assertIn('native worker', dispatch_content(frappe.get_doc('Bot Dispatch', turn.dispatch)))
        logs = frappe.get_all('Process Execution Log', filters={'turn': turn.name}, fields=['name', 'execution_outcome', 'functional_status'])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].functional_status, 'success')
        job = get_job(job_id)
        print('ASYNC_RQ_EVIDENCE', json.dumps(dict(job=job.id, worker=job.worker_name, origin=job.origin,
            timeout=job.timeout, site=job.kwargs['site'], turn=turn.name, dispatch=turn.dispatch,
            execution_log=logs[0].name, status=str(job.get_status()), result='native worker'), default=str))
        # Wait for job completion before deleting its controlled fixtures.
        for _index in range(50):
            if str(job.get_status(refresh=True)) not in ('JobStatus.STARTED', 'JobStatus.QUEUED'):
                break
            time.sleep(.1)
        job.delete()
        probe = frappe.enqueue(__name__ + '.rq_deadline_probe', queue='long', timeout=30,
            job_id='messaging-deadline-' + first['contact'])
        for _index in range(150):
            if str(probe.get_status(refresh=True)) == 'JobStatus.FINISHED':
                break
            time.sleep(.2)
        self.assertEqual(probe.return_value(), dict(main_thread=True, interrupted=True, outer_timer_preserved=True))
        print('ASYNC_RQ_DEADLINE', json.dumps(dict(job=probe.id, worker=probe.worker_name, result=probe.return_value())))
        probe.delete()

    consumer_flow = TestFlowConsumers.consumer_flow
    select_consumer = TestFlowConsumers.select

    def test_consecutive_registration_resumes_original_before_ai(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        from messaging.chatbot.doctype.bot_settings.bot_settings import is_registration_complete
        registration, _parameter = self.consumer_flow()
        business = self.flow(outputs=[dict(status_code='success', output_mode='Message')])
        self.select_consumer('registration', registration)
        self.natural()
        first = self.message(text='Preserved original business request')
        answer = self.message(text='1')
        seen = []
        def classify(text, *args, **kwargs):
            self.assertTrue(is_registration_complete(frappe.get_doc('Chat Contact', first['contact'])))
            seen.append(text)
            return Decision(outcome='recognized', flows=[business.name])
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', side_effect=classify):
            turn = self.drain(first)
        self.assertEqual(seen, ['Preserved original business request'])
        self.assertEqual(CALLS, [1, None])
        self.assertEqual(self.turn(answer).consumed_by, turn.name)
        self.assertEqual(json.loads(self.turn(answer).input_summary)['language'], json.loads(turn.input_summary)['language'])
        content = dispatch_content(frappe.get_doc('Bot Dispatch', turn.dispatch))
        self.assertLess(content.index('Consumer result: registered.'), content.index('Process reply'))
        self.assertNotIn('Initial input.', content)
        self.drain(first)
        self.assertEqual(CALLS, [1, None])

    def test_pending_messages_respect_effective_human_transfer(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        human, _parameter = self.consumer_flow(parameter=False)
        self.select_consumer('human', human)
        self.natural()
        first = self.message(text='Please connect me to an agent')
        second = self.message(text='A later message for the agent')
        human.semantic_description = "Speak with a person."
        for row in human.translations:
            row.semantic_description = human.semantic_description
        human.save(ignore_version=True)
        frappe.db.commit()
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
            return_value=Decision(outcome='recognized', human_attention_requested=True)) as classifier, \
            patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            self.drain(first)
        self.assertEqual(classifier.call_count, 1)
        self.assertEqual(self.turn(second).status, 'Human Paused')
        self.assertFalse(self.turn(second).dispatch)
        self.assertEqual(CALLS, [None])

    def test_worker_death_during_invocation_never_replays_business(self):
        flow = self.flow()
        self.natural()
        first = self.message(text=flow.name)
        with patch('messaging.conversation.doctype.chat_session.chat_session.invoke', side_effect=SystemExit('Synthetic worker death')):
            with self.assertRaises(SystemExit):
                self.drain(first)
        frappe.db.rollback()
        self.assertEqual(json.loads(self.turn(first).executions)[0]['state'], 'Running')
        with patch('messaging.conversation.doctype.chat_session.chat_session.invoke', side_effect=AssertionError('No replay')):
            self.drain(first)
        self.assertEqual(self.turn(first).inbox_state, 'Blocked')
        self.assertEqual(CALLS, [])
        execution_id = json.loads(self.turn(first).executions)[0]['id']
        reconcile_execution(first['turn'], execution_id, terminal_error=True)
        self.drain(first)
        self.assertEqual(self.turn(first).inbox_state, 'Done')
        self.assertEqual(CALLS, [])

    def test_arrival_at_worker_release_survives_lost_wakeup(self):
        from contextlib import contextmanager
        from messaging.conversation.doctype.chat_turn import inbox as worker
        first = self.message(text='First receipt')
        lock = worker.contact_lock
        arrivals = []
        @contextmanager
        def releasing(key, **kwargs):
            with lock(key, **kwargs) as acquired:
                yield acquired
                ctx = multiprocessing.get_context('spawn')
                results = ctx.Queue()
                process = ctx.Process(target=inbox_arrival, args=(frappe.local.site, frappe.local.sites_path, self.a, self.sender, results))
                process.start(); process.join(20)
                self.assertFalse(process.is_alive())
                arrivals.append(results.get(timeout=5))
        with patch.object(worker, 'contact_lock', releasing):
            self.drain(first)
        self.assertNotIn('error', arrivals[0])
        self.assertEqual(self.turn(arrivals[0]).inbox_state, 'Pending')
        from messaging.conversation.doctype.chat_contact import chat_contact as contacts
        with patch.object(contacts, '_enqueue') as enqueue:
            worker.recover_pending()
        enqueue.assert_any_call(first['contact'])
        self.drain(arrivals[0])
        self.assertEqual(self.turn(arrivals[0]).inbox_state, 'Done')

    def test_unknown_transport_keeps_output_and_never_resends(self):
        account = self.account(outcome='Unknown')
        self.flow()
        first = self.message(account=account, text='Hello')
        second = self.message(account=account, text='Later')
        turn = self.drain(first)
        self.assertEqual(frappe.db.get_value('Bot Dispatch', turn.dispatch, 'status'), 'Unknown')
        self.assertTrue(frappe.db.get_value('Chat Message', {'dispatch': turn.dispatch}, 'content'))
        self.assertEqual(self.turn(second).inbox_state, 'Pending')
        with patch('messaging.communication.doctype.communication_provider.communication_provider.handoff', side_effect=AssertionError('No resend')):
            self.drain(first)
        from messaging.emulator.page.emulator_console.emulator_console import simulate_result
        simulate_result(account, self.sender, turn.dispatch, 'Accepted')
        self.assertEqual(self.turn(second).inbox_state, 'Pending')
        self.drain(second)
        self.assertEqual(self.turn(second).inbox_state, 'Done')

    def test_same_flow_readers_are_concurrent_and_administrative_edits_wait(self):
        from messaging.common.locking import shared_document
        flow = self.flow()
        shared_document('Bot Flow', flow.name)
        ctx = multiprocessing.get_context('spawn')
        results = ctx.Queue()
        process = ctx.Process(target=shared_source_reader, args=(frappe.local.site, frappe.local.sites_path, flow.name, results))
        process.start(); process.join(20)
        self.assertFalse(process.is_alive())
        self.assertEqual(results.get(timeout=5), dict(source=flow.name, exclusive_rejected=True))
        frappe.db.rollback()

    def test_recovery_cursor_advances_past_a_page_of_blocked_contacts(self):
        from messaging.conversation.doctype.chat_turn import inbox as worker
        key = 'messaging_inbox_recovery_cursor'
        before = frappe.cache.get_value(key)
        original = frappe.db.sql
        pages = [[f'CTC-PAGE-{index:04}' for index in range(200)], ['CTC-PAGE-0200']]
        cursors = []
        def scan(query, values=None, **kwargs):
            if 'UNION SELECT contact' in str(query):
                cursors.append(values[0])
                return pages.pop(0)
            return original(query, **kwargs) if values is None else original(query, values, **kwargs)
        try:
            frappe.cache.set_value(key, '')
            with patch.object(frappe.db, 'sql', side_effect=scan), patch.object(worker, 'schedule_contact') as schedule:
                worker.recover_pending()
                worker.recover_pending()
            self.assertEqual(cursors, ['', 'CTC-PAGE-0199'])
            self.assertEqual(schedule.call_count, 201)
            schedule.assert_any_call('CTC-PAGE-0200')
            self.assertEqual(frappe.cache.get_value(key), '')
        finally:
            if before is None:
                frappe.cache.delete_value(key)
            else:
                frappe.cache.set_value(key, before)


class TestNativeFlowConsumers(unittest.TestCase):
    """Native handlers through the ordinary synthetic dialogue fixtures and durable engine."""
    configure = TestChatTurn.configure
    ensure_test_channel = TestChatTurn.ensure_test_channel
    parameter = TestChatTurn.parameter
    flow = TestChatTurn.flow
    event = TestChatTurn.event
    send = TestChatTurn.send
    state = TestChatTurn.state
    natural = TestFlowConsumers.natural

    def setUp(self):
        TestChatTurn.setUp(self)
        self.configure(quotas_enabled=0, greeting_enabled=0)
        self.ai_names = []

    def tearDown(self):
        try:
            if getattr(self, "human_counter_cleanup", None):
                self.human_counter_cleanup()
        finally:
            TestFlowConsumers.tearDown(self)

    def select_native(self, consumer, flow, status, **values):
        from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
        state_field = 'registration_states' if consumer == 'registration' else 'human_transfer_states'
        enabled_field = 'registration_enabled' if consumer == 'registration' else 'human_attention_enabled'
        if consumer == 'registration' and 'registration_fields' not in values:
            from messaging.chatbot.doctype.bot_settings.flow_consumers import options
            fields = options(flow.name, frappe.as_json([state_name(flow.name, status)]))['fields']
            values['registration_fields'] = [dict(field_key=item['field_key'], label=item['field_key'], keep=1)
                for item in fields]
        return self.configure(**{consumer + '_flow': flow.name, enabled_field: 1,
            state_field: [dict(state=state_name(flow.name, status))]}, **values)

    def test_native_registration_captures_without_ai_persists_original_keys_and_resumes_once(self):
        from messaging.presets.processes import registration as registration_process
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        from messaging.chatbot.doctype.bot_settings.bot_settings import is_registration_complete
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message
        required = self.parameter('Name')
        optional = self.parameter('Int', min_value=1, max_value=9)
        registration = self.flow(handler=registration_process.definition, business=registration_process.handle, show_in_menu=0,
            parameters=[dict(parameter=required.name, process_argument='name', required=1, capture_order=1),
                dict(parameter=optional.name, required=0, capture_order=2)],
            outputs=[dict(status_code='registered', output_mode='Template', body='Registration complete.')])
        business = self.flow(show_in_menu=0, outputs=[dict(status_code='success', output_mode='Message')])
        settings = self.select_native('registration', registration, 'registered')
        for row in settings.registration_fields:
            row.keep = int(row.field_key == 'name')
            row.label = 'Collected name' if row.keep else 'Optional value'
        settings.save(); frappe.db.commit()
        self.natural()
        original = self.event(text='Please resume this original request.')
        seen = []
        def classify(text, *args, **kwargs):
            contact = frappe.get_doc('Chat Contact', first['contact'])
            self.assertTrue(is_registration_complete(contact))
            self.assertEqual(json.loads(contact.parameter_values)['name'], 'Juan Pérez')
            seen.append(text)
            return Decision(flows=[business.name], outcome='recognized')
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', side_effect=classify) as ai:
            first = process_message(original)
            self.assertEqual(ai.call_count, 0)
            held = self.state(first)['registration_request']
            invalid = process_message(self.event(text='123'))
            self.assertEqual(ai.call_count, 0)
            self.assertEqual(self.state(invalid)['registration_request'], held)
            final_request = self.event(text='Juan Pérez')
            done = process_message(final_request)
            self.assertEqual(seen, ['Please resume this original request.'])
            self.assertIn('Registration complete.', done['response'])
            self.assertEqual(process_message(final_request), done)
            self.assertEqual(ai.call_count, 1)
        contact = frappe.get_doc('Chat Contact', first['contact'])
        self.assertEqual(json.loads(contact.parameter_values), {'name': 'Juan Pérez'})
        self.assertNotIn(optional.name, json.loads(contact.parameter_values))
        self.assertFalse(self.state(done).get('registration_request'))

    def test_native_human_open_transfers_only_selected_complete_state_and_closed_does_not_transfer(self):
        from messaging.presets.processes import human_availability as human_process
        from messaging.conversation.doctype.chat_session import human
        outputs = []
        for status in ('open', 'closed', 'filtered'):
            template = frappe.get_doc(dict(doctype='Bot Flow Template', template_name=self.prefix + '_' + status,
                enabled=1, language='es', body='Support ' + status + '.')).insert()
            self.fixture_templates.append(template.name)
            outputs.append(dict(status_code=status, output_mode='Template', template_reference=template.name,
                flow_action='Complete Flow' if status == 'open' else 'Cancel Flow'))
        flow = self.flow(handler=human_process.definition, business=human_process.handle, outputs=outputs, show_in_menu=0)
        week = [dict(weekday=day, enabled=day == 'Monday', start_time='09:00:00' if day == 'Monday' else None,
            end_time='17:00:00' if day == 'Monday' else None)
            for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
        self.select_native('human', flow, 'open', human_support_week=week)
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None), \
            patch('messaging.conversation.doctype.chat_session.human.transfer', wraps=human.transfer) as transfer:
            with patch('frappe.utils.now_datetime', return_value=datetime(2026, 11, 2, 10)):
                opened = self.send(human_requested=True, sender='native-open')
            self.assertIn('Support open.', opened['response'])
            self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'status'), 'Human Queued')
            self.assertEqual(transfer.call_count, 1)
            with patch('frappe.utils.now_datetime', return_value=datetime(2026, 11, 2, 18)):
                closed = self.send(human_requested=True, sender='native-closed')
            self.assertIn('Support closed.', closed['response'])
            self.assertFalse(frappe.db.get_value('Chat Session', closed['session'], 'status').startswith('Human'))
            self.assertEqual(transfer.call_count, 1)

    def test_native_greeting_duplicate_event_keeps_one_execution_and_dispatch(self):
        from messaging.presets.processes import greeting as greeting_process
        from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
        flow = self.flow(handler=greeting_process.definition, business=greeting_process.handle, show_in_menu=0,
            outputs=[dict(status_code='greeted', output_mode='Template', body='Native greeting.')])
        self.configure(greeting_enabled=1, greeting_flow=flow.name,
            greeting_states=[dict(state=state_name(flow.name, 'greeted'))],
            show_menu_on_greeting_only=0, informative_show_available_categories=0)
        request = self.event(flows=[flow.name])
        first = process_event(request)
        self.assertEqual(first['response'], 'Native greeting.')
        self.assertEqual(process_event(request), first)
        self.assertEqual(frappe.db.count('Process Execution Log', {'turn': first['turn'], 'functional_status': 'greeted'}), 1)
        self.assertEqual(frappe.db.count('Bot Dispatch', {'turn': first['turn']}), 1)


    def native_human_counter(self, required=3):
        from messaging.presets.processes.human_availability import definition as human_definition, handle as human_handle
        outputs = []
        for status in ('open', 'closed', 'filtered'):
            template = frappe.get_doc(dict(doctype='Bot Flow Template',
                template_name=self.prefix + '_' + status, enabled=1, language='es', body='Support ' + status + '.')).insert()
            self.fixture_templates.append(template.name)
            outputs.append(dict(status_code=status, output_mode='Template', template_reference=template.name,
                flow_action='Complete Flow' if status == 'open' else 'Cancel Flow'))
        flow = self.flow(handler=human_definition, business=human_handle, show_in_menu=0, outputs=outputs)
        self.select_native('human', flow, 'open', human_requests_required=required,
            human_support_week=[dict(weekday=day, enabled=1, start_time='00:00:00', end_time='23:59:59')
                for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')])
        # Only these synthetic contacts' opaque Redis keys belong to the fixture.
        from messaging.communication.doctype.process_definition.execution import _human_request_keys
        def cleanup():
            contacts = frappe.get_all('Chat Contact', filters={'account': self.prefix}, pluck='name')
            for row in frappe.get_all('Chat Message', filters={'contact': ['in', contacts or ['']], 'kind': 'Incoming'},
                    fields=['name', 'contact']):
                frappe.cache.delete(*_human_request_keys(row.contact, row.name))
        self.human_counter_cleanup = cleanup
        return flow

    def test_native_human_counter_across_sessions_and_confirmed_transfer(self):
        from messaging.communication.doctype.process_definition import execution as runtime
        self.native_human_counter()
        request = self.event(human_requested=True)
        first = process_event(request)
        self.assertIn('Support filtered.', first['response'])
        self.assertEqual(process_event(request), first)
        self.assertEqual(frappe.get_doc('Chat Session', first['session']).status, 'Active')
        self.assertEqual(json.loads(frappe.get_doc('Chat Session', first['session']).human_requests or '[]'), [])
        other = self.send(human_requested=True, sender='independent-contact')
        self.assertIn('Support filtered.', other['response'])
        # The normal idle-expiry coordinator opens the next session.
        frappe.db.set_value('Chat Session', first['session'], 'expires_at', utcnow() - timedelta(seconds=1))
        frappe.db.commit()
        second = self.send(human_requested=True)
        self.assertNotEqual(second['session'], first['session'])
        self.assertEqual(second['contact'], first['contact'])
        self.assertIn('Support filtered.', second['response'])
        self.assertEqual(json.loads(frappe.get_doc('Chat Session', second['session']).human_requests or '[]'), [])
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            third = self.send(human_requested=True)
        self.assertIn('Support open.', third['response'])
        queued = frappe.get_doc('Chat Session', third['session'])
        self.assertEqual(queued.status, 'Human Queued')
        self.assertFalse(queued.assigned_agent)
        message = frappe.db.get_value('Chat Message', {'turn': third['turn'], 'kind': 'Incoming'}, 'name')
        counter, receipt = runtime._human_request_keys(third['contact'], message)
        self.assertFalse(frappe.cache.exists(counter, shared=True))
        self.assertTrue(frappe.cache.exists(receipt, shared=True))

    def test_native_human_counter_preserves_threshold_when_transfer_fails(self):
        self.native_human_counter()
        self.send(human_requested=True)
        self.send(human_requested=True)
        with patch('messaging.chatbot.doctype.bot_flow.execution.require_execution_result',
                side_effect=frappe.PermissionError('Transient transfer authorization failure')):
            failed = self.send(human_requested=True)
        self.assertEqual(frappe.get_doc('Chat Session', failed['session']).status, 'Active')
        simulate_dispatch(failed['dispatch'])
        with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
            retried = self.send(human_requested=True)
        self.assertIn('Support open.', retried.get('response') or '', (retried, frappe.get_doc('Chat Session', failed['session']).status))
        self.assertEqual(frappe.get_doc('Chat Session', retried['session']).status, 'Human Queued')

    def test_native_human_counter_same_process_for_decision_and_mechanical_menu(self):
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message
        from messaging.chatbot.doctype.bot_settings.interpretation import Decision
        flow = self.native_human_counter()
        self.natural()
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
                return_value=Decision(flows=[], human_attention_requested=True, outcome='recognized')):
            first = process_message(self.event(text='Connect me with a person.'))
        self.assertIn('Support filtered.', first['response'])
        self.configure(interpretation_enabled=0)
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('No Decision in menu mode')):
            simulate_dispatch(first['dispatch'])
            menu = process_message(self.event(text='0'))
            number = next(key for key, row in self.state(menu)['mechanical_menu']['options'].items() if row.get('action') == 'Human')
            simulate_dispatch(menu['dispatch'])
            second = process_message(self.event(text=number))
            self.assertIn('Support filtered.', second['response'])
            simulate_dispatch(second['dispatch'])
            menu = process_message(self.event(text='0'))
            number = next(key for key, row in self.state(menu)['mechanical_menu']['options'].items() if row.get('action') == 'Human')
            simulate_dispatch(menu['dispatch'])
            with patch('messaging.conversation.doctype.chat_session.human.capacity', return_value=None):
                third = process_message(self.event(text=number))
        self.assertIn('Support open.', third['response'])
        self.assertEqual(frappe.get_doc('Chat Session', third['session']).status, 'Human Queued')
        for result, status in ((first, 'filtered'), (second, 'filtered'), (third, 'open')):
            self.assertTrue(frappe.db.exists('Process Execution Log', {'turn': result['turn'], 'source_name': flow.name, 'functional_status': status}))


    def test_native_human_counter_disabled_closed_and_administrative_validation_have_no_effects(self):
        from messaging.communication.doctype.process_definition import execution as runtime
        from messaging.chatbot.doctype.bot_flow.bot_flow import set_active, validate_process
        flow = self.native_human_counter()
        next(r for r in flow.outputs if r.status_code == 'filtered').output_mode = 'Message'
        flow.save()
        frappe.db.commit()
        flow.reload()
        original = frappe.cache.eval
        def only_technical_scripts(script, *args, **kwargs):
            self.assertNotEqual(script, runtime.HUMAN_REQUEST_INCREMENT)
            return original(script, *args, **kwargs)
        with patch.object(frappe.cache, 'eval', side_effect=only_technical_scripts):
            self.assertEqual(validate_process(flow.as_dict())['status'], 'Passed')
            flow.reload()
            set_active(flow.as_dict(), 1)
            self.configure(human_support_week=[dict(weekday=day, enabled=0)
                for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')])
            closed = self.send(human_requested=True)
            self.assertIn('Support closed.', closed['response'])
            self.configure(human_attention_enabled=0)
            disabled = self.send(human_requested=True, sender='disabled-contact')
            self.assertFalse(frappe.db.exists('Process Execution Log', {'turn': disabled['turn']}))
            self.assertNotIn(frappe.get_doc('Chat Session', disabled['session']).status, ('Human Queued', 'Human Active'))


class TestMultilingualConversation(unittest.TestCase):
    configure = TestChatTurn.configure
    ensure_test_channel = TestChatTurn.ensure_test_channel
    parameter = TestChatTurn.parameter
    flow = TestChatTurn.flow
    event = TestChatTurn.event
    send = TestChatTurn.send
    state = TestChatTurn.state
    select_native = TestNativeFlowConsumers.select_native
    natural = TestFlowConsumers.natural
    tearDown = TestFlowConsumers.tearDown

    def setUp(self):
        TestChatTurn.setUp(self)
        self.registration_language.stop()
        self.ai_names = []
        self.configure(quotas_enabled=0, greeting_enabled=0,
            chatbot_languages=[dict(language='es', enabled=1, is_default=1), dict(language='en', enabled=1)])

    def test_detected_language_survives_registration_capture_and_original_resume(self):
        from messaging.presets.processes import registration as registration_process
        from messaging.chatbot.doctype.bot_settings.language import detect_language, resolve_process_language
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process
        required = self.parameter('Name')
        required.append('request_phrases', dict(language='en', request_phrase='Please provide your name.'))
        required.save()
        registration = self.flow(handler=registration_process.definition, business=registration_process.handle, show_in_menu=0,
            parameters=[dict(parameter=required.name, process_argument='name', required=1, capture_order=1)],
            outputs=[dict(status_code='registered', output_mode='Template', body='Registro completo.')])
        registration.append('input_translations', dict(language='en', input_initial_message='Please register.',
            input_continuation_message='Please complete registration.'))
        english = frappe.get_doc(dict(doctype='Bot Flow Template',
            template_name=self.prefix + ' English registration', language='en', enabled=1, body='Registration complete.')).insert()
        self.fixture_templates.append(english.name)
        registration.append('output_translations', dict(language='en', status_code='registered', template_reference=english.name))
        registration.save()
        approval = validate_process(registration.as_dict())
        self.fixture_test_logs.append(approval['execution_log'])
        self.assertEqual(approval['status'], 'Passed', approval)
        registration.reload()
        business = self.flow(method='controlled_language_only', show_in_menu=0,
            outputs=[dict(status_code='success', output_mode='Message')])
        self.select_native('registration', registration, 'registered')
        self.natural()
        cases = [
            ('Quiero consultar mi saldo pendiente.', 'es'),
            ('Hello, I want to check my balance.', 'en'),
            ('hola hello saldo balance please gracias', 'es'),
            ('Je voudrais consulter le solde de mon compte.', 'es'),
        ]
        seen = []
        def classify(text, *args, **kwargs):
            seen.append((text, resolve_process_language()))
            return engine.Decision(flows=[business.name], outcome='recognized')
        with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', side_effect=classify) as ai, patch(
            'messaging.chatbot.doctype.bot_settings.language.detect_language', wraps=detect_language) as detector:
            for index, (text, language) in enumerate(cases):
                sender = 'multilingual_' + str(index)
                before = ai.call_count
                first = process_message(self.event(sender=sender, text=text))
                self.assertEqual(ai.call_count, before)
                self.assertEqual(self.state(first)['registration_language'], language)
                self.assertIn('Please register.' if language == 'en' else 'Initial input.', first['response'])
                done = process_message(self.event(sender=sender, text='Juan Pérez'))
                self.assertEqual(seen[-1], (text, language))
                self.assertEqual(ai.call_count, before + 1)
                self.assertIn('Registration complete.' if language == 'en' else 'Registro completo.', done['response'])
                self.assertIn('Language: ' + language, done['response'])
                summary = json.loads(frappe.db.get_value('Chat Turn', done['turn'], 'input_summary'))
                self.assertEqual(summary['language'], language)
                self.assertFalse(self.state(done).get('registration_language'))
            self.assertEqual(detector.call_count, len(cases))

    def test_default_content_fallback_preserves_selected_language_and_contact_isolation(self):
        from messaging.chatbot.doctype.bot_settings import language
        from messaging.chatbot.doctype.bot_settings.localization import text as settings_text
        flow = self.flow(show_in_menu=1, menu_label='Menú de saldo',
            outputs=[dict(status_code='success', output_mode='Template', body='Respuesta predeterminada.')])
        result = self.send(sender='english', text='Hello, I want to check my balance.', flows=[flow.name])
        self.assertEqual(result['response'], 'Respuesta predeterminada.')
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', result['turn'], 'input_summary'))['language'], 'en')
        self.assertFalse(frappe.db.exists('Flow Output Translation', dict(parent=flow.name, language='en')))
        second = self.send(sender='spanish', text='Quiero consultar mi saldo pendiente.', flows=[flow.name])
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', second['turn'], 'input_summary'))['language'], 'es')
        self.assertNotEqual(result['contact'], second['contact'])
        with language.presentation_language('en'):
            self.assertEqual(settings_text(frappe.get_single('Bot Settings'), 'informative_message'), 'Choose an option.')
            self.assertEqual(language.resolve_process_language(), 'en')
        self.assertIsNone(language.current_language())
        self.assertEqual([(r.language, r.enabled, r.is_default) for r in frappe.get_single('Bot Settings').chatbot_languages],
            [('es', 1, 1), ('en', 1, 0)])
        from messaging.conversation.doctype.chat_turn.chat_turn import validate_request
        for field, value in [('language', 'fr'), ('chatbot_languages', [])]:
            with self.assertRaises(frappe.ValidationError):
                validate_request(dict(self.event(), **{field: value}))

    def test_deferred_human_notice_and_process_context_use_turn_language(self):
        from messaging.conversation.doctype.chat_session.human import assignment_notice
        from messaging.communication.doctype.process_definition.execution import build_context
        from messaging.chatbot.doctype.bot_settings.language import presentation_language
        from messaging.chatbot.doctype.bot_settings.test_bot_settings import set_localized_text
        settings = frappe.get_single('Bot Settings')
        set_localized_text(settings, 'human_active_message', 'English assignment.', language='en')
        settings.save(); frappe.db.commit()
        flow = self.flow(show_in_menu=0, outputs=[dict(status_code='success', output_mode='Message')])
        original = self.send(text='Hello, I want to check my balance.', flows=[flow.name])
        session = frappe.get_doc('Chat Session', original['session'])
        contact = frappe.get_doc('Chat Contact', original['contact'])
        session.human_request_turn = original['turn']
        session.assigned_agent = 'Administrator'
        session.human_assignment_dispatch = None
        with patch('messaging.chatbot.doctype.bot_settings.language.detect_language', side_effect=AssertionError('A deferred notice must not detect again')):
            notice = assignment_notice(session, contact)
        self.assertEqual(dispatch_content(notice), 'English assignment.')
        definition = frappe.get_doc('Process Definition', flow.process_definition)
        context = build_context(definition, flow, {}, administrative_test=False,
            execution=dict(session=session.name, turn=original['turn']))
        self.assertEqual(context.language, 'en')
        with patch.object(frappe.local, 'lang', 'fr'):
            context = build_context(definition, flow, {}, administrative_test=False,
                execution=dict(session=session.name, turn=original['turn']))
            self.assertEqual(context.language, 'en')
        from messaging.communication.doctype.process_definition.execution import execute_process
        from messaging.chatbot.doctype.bot_settings.language import resolve_process_language, current_language
        from messaging.chatbot.doctype.bot_flow.bot_flow import resolve_flow_output
        def render(raw):
            self.assertEqual(resolve_process_language(), 'en')
            return dict(resolve_flow_output(flow, raw), rendered_message=frappe._('Select an option:', lang=resolve_process_language()))
        with patch.object(frappe.local, 'lang', 'fr'):
            result = execute_process(process_method=flow.process_method, arguments={},
                source_type='Bot Flow', source_name=flow.name, workflow=flow,
                session=session.name, turn=original['turn'], execution_id='deferred_' + frappe.generate_hash(),
                render_result=render)
        self.assertTrue(result['functional'], result)
        self.assertEqual(result['text'], 'Select an option:')
        self.assertIsNone(current_language())

    def test_mechanical_menu_preserves_language_across_numeric_inputs_and_falls_back_to_default(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import set_active
        from messaging.chatbot.doctype.bot_settings.test_bot_settings import set_localized_text
        from messaging.conversation.doctype.chat_turn.chat_turn import process_message

        parameter = self.parameter(mask='#####')
        parameter.set('request_phrases', [
            dict(language='es', request_phrase='Solicitud en español.'),
            dict(language='en', request_phrase='English parameter prompt.'),
        ])
        parameter.save()
        flow = self.flow(
            method='controlled_contracts_language',
            menu_label='Opción español',
            status='Disabled',
            parameters=[dict(
                parameter=parameter.name,
                process_argument='contracts',
                required=1,
                allow_multiple_values=1,
                minimum_values=1,
                maximum_values=3,
                capture_order=1,
            )],
            outputs=[dict(status_code='success', output_mode='Message')],
        )
        flow.append('translations', dict(language='en', semantic_description='English semantic.', menu_label='English option'))
        flow.append('input_translations', dict(language='en', input_initial_message='Initial English.', input_continuation_message='Continue English.'))
        flow.save()
        set_active(flow.as_dict(), 1)
        flow.reload()
        self.assertEqual(flow.status, 'Enabled')
        settings = frappe.get_single('Bot Settings')
        set_localized_text(settings, 'informative_categories_header', 'Choose an option:', language='en')
        set_localized_text(settings, 'informative_categories_header', 'Selecciona una opción:', language='es')
        settings.interpretation_enabled = 0
        settings.save()
        frappe.db.commit()

        menu = process_message(self.event(sender='mech_en', text='Hello, I want to check my account options please.'))
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', menu['turn'], 'input_summary'))['language'], 'en')
        self.assertEqual(self.state(menu)['language'], 'en')
        self.assertIn('English option', menu['response'])
        self.assertIn('0. Return to the main menu', menu['response'])
        number = next(key for key, row in self.state(menu)['mechanical_menu']['options'].items() if row['flow'] == flow.name)

        simulate_dispatch(menu['dispatch'])
        prompt = process_message(self.event(sender='mech_en', text=number))
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', prompt['turn'], 'input_summary'))['language'], 'en')
        self.assertIn('English parameter prompt.', prompt['response'])

        simulate_dispatch(prompt['dispatch'])
        completed = process_message(self.event(sender='mech_en', text='26747'))
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', completed['turn'], 'input_summary'))['language'], 'en')
        self.assertIn('Language: en', completed['response'])

        simulate_dispatch(completed['dispatch'])
        refreshed = process_message(self.event(sender='mech_en', text='0'))
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', refreshed['turn'], 'input_summary'))['language'], 'en')
        self.assertIn('0. Return to the main menu', refreshed['response'])

        frappe.db.set_value('Chat Session', refreshed['session'], 'expires_at', utcnow() - timedelta(seconds=1))
        frappe.db.commit()
        new_session_menu = process_message(self.event(sender='mech_en', text='0'))
        self.assertNotEqual(new_session_menu['session'], refreshed['session'])
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', new_session_menu['turn'], 'input_summary'))['language'], 'en')
        self.assertIn('0. Return to the main menu', new_session_menu['response'])

        fallback_menu = process_message(self.event(sender='mech_default', text='0'))
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', fallback_menu['turn'], 'input_summary'))['language'], 'es')
        self.assertIn('Opción español', fallback_menu['response'])
        simulate_dispatch(fallback_menu['dispatch'])
        fallback_prompt = process_message(self.event(sender='mech_default', text=number))
        self.assertEqual(json.loads(frappe.db.get_value('Chat Turn', fallback_prompt['turn'], 'input_summary'))['language'], 'es')
        self.assertIn('Solicitud en español.', fallback_prompt['response'])
