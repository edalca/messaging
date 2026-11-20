from messaging.communication.doctype.communication_provider import communication_provider as provider_catalog
"""Ephemeral Code-First registrations exercising the exact private adapter boundaries, without network."""
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch
import unittest
import multiprocessing

import frappe

from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.conversation.doctype.chat_turn import ingress as turn_ingress
from messaging.chatbot.doctype.bot_dispatch import delivery as bot_delivery
from messaging.communication.doctype.communication_channel_account import communication_channel_account as channel_transport
from messaging.communication.doctype.communication_provider import communication_provider as provider_catalog
from messaging.communication.doctype.notification_rule import events as notification_events
from messaging.communication.doctype.communication_channel_account.communication_channel_account import resolve_binding
from messaging.conversation.doctype.chat_turn import test_chat_turn as native

PROBE = dict(id='CTRPRO', name='Contract Probe', version='1.0', app='messaging',
	capabilities=['text', 'interactive', 'delivery_status'], operations={
		'resolve_account': __name__ + '.resolve_account', 'send': __name__ + '.send',
	})


def adapter_call(method, provider, account, *args, **kwargs):
	# Synthetic adapter authentication precedes the private Messaging context.
	from messaging.emulator.doctype.emulator_contact.emulator_contact import require_manager
	from messaging.security.execution_context import _authenticated_adapter
	require_manager()
	with _authenticated_adapter(provider, account):
		return method(provider, account, *args, **kwargs)


def resolve_account(reference):
	return reference.account_id.startswith('m2_')


def send(envelope):
	return send_result(envelope)


def send_result(envelope):
	return dict(outcome='Accepted', reference='probe-' + envelope['dispatch'])


@contextmanager
def registered_providers(definitions=None):
	from messaging.communication.doctype.communication_provider.test_communication_provider import declaration
	from messaging.registry import _catalog
	original = _catalog.application_declarations
	def installed(app, *, kinds=None, **kwargs):
		result = original(app, kinds=kinds, **kwargs)
		if app == 'messaging' and 'communication_providers' in result:
			result['communication_providers'] = tuple(declaration(row) for row in (definitions if definitions is not None else [PROBE]))
		return result
	with patch.object(_catalog, 'application_declarations', side_effect=installed):
		yield




def bind(reference, *, provider='CTRPRO', name=None):
	channel = 'test' if provider == 'CTRPRO' else 'other_test'
	doc = frappe.get_doc(dict(doctype='Communication Channel Account', channel=channel,
		account_id=reference, account_name='Synthetic contract account', enabled=1))
	if not resolve_account(doc): frappe.throw('Synthetic unavailable account')
	return doc.insert(set_name=name)



def concurrent_adapter(site, sites_path, binding, event, barrier, results):
	frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
	try:
		with registered_providers(), patch('requests.sessions.Session.request', side_effect=AssertionError('Network is forbidden')):
			from messaging.conversation.doctype.chat_turn.inbox import drain_contact
			from messaging.conversation.doctype.chat_turn.chat_turn import turn_result
			import time
			barrier.wait(timeout=20)
			result = adapter_call(turn_ingress.receive, 'CTRPRO', binding, event)
			for _ in range(40):
				drained = drain_contact(result['contact'])
				if not (isinstance(drained, dict) and drained.get('busy')):
					break
				time.sleep(0.05)
			frappe.db.rollback()
			result = turn_result(frappe.get_doc('Chat Turn', result['turn']))
			delivery = bot_delivery.deliver(result['dispatch'])
			results.put(dict(turn=result['turn'], contact=result['contact'], dispatch=result['dispatch'], state=delivery['state']))
	except Exception as exc: results.put(dict(error=type(exc).__name__, detail=str(exc) if isinstance(exc, frappe.ValidationError) else 'omitted'))
	finally: frappe.db.rollback(); frappe.destroy()


class TestChannelContracts(unittest.TestCase):
	configure = native.TestChatTurn.configure
	ensure_test_channel = native.TestChatTurn.ensure_test_channel
	flow = native.TestChatTurn.flow
	parameter = native.TestChatTurn.parameter
	event = native.TestChatTurn.event
	state = native.TestChatTurn.state

	def setUp(self):
		native.TestChatTurn.setUp(self)
		self.registry = registered_providers([PROBE, dict(PROBE, id='CTROTH', name='Other Probe')])
		self.registry.__enter__()
		self.addCleanup(self.registry.__exit__, None, None, None)
		self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network is forbidden'))
		self.network.start(); self.addCleanup(self.network.stop)
		from messaging.chatbot.doctype.bot_settings.interpretation import Decision
		interpreter = patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', side_effect=lambda *a, **k: Decision())
		interpreter.start(); self.addCleanup(interpreter.stop)
		from messaging.communication.doctype.communication_provider.communication_provider import get_provider_states, set_provider_enabled
		self.provider_states_before = get_provider_states()
		from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
		sync_communication_providers()
		for provider, channel in [('CTRPRO','test'), ('CTROTH','other test')]:
			set_provider_enabled(provider, True)
			channel = frappe.get_doc(dict(doctype='Communication Channel', provider=provider, channel_name=channel)).insert()
			set_channel_status(channel.name, 'Enabled')
		self.bindings = [bind(self.prefix), bind(self.prefix + '_other', provider='CTROTH')]
		self.accounts.extend(doc.name for doc in self.bindings)
		frappe.db.commit()

	def tearDown(self):
		# Keep durable fixture cleanup independent of the paused site's background queue.
		with patch('frappe.enqueue'):
			self.registry.__exit__(None, None, None)
			native.TestChatTurn.tearDown(self)
			from messaging.communication.doctype.communication_provider.communication_provider import _lock_provider_states, _write_provider_states
			_lock_provider_states(); _write_provider_states(self.provider_states_before)
			for doc in self.bindings:
				if frappe.db.exists('Communication Channel Account', doc.name):
					frappe.delete_doc('Communication Channel Account', doc.name, ignore_permissions=True)
			from messaging.communication.doctype.communication_provider.communication_provider import catalog_sync
			with catalog_sync():
				for channel, provider in [('test','CTRPRO'), ('other_test','CTROTH')]:
					if frappe.db.exists('Communication Channel', channel):
						frappe.delete_doc('Communication Channel', channel, ignore_permissions=True)
					if frappe.db.exists('Communication Provider', provider):
						frappe.delete_doc('Communication Provider', provider, ignore_permissions=True)
			frappe.db.commit()

	def incoming(self, **values):
		raw = self.event(**values)
		return {k: raw[k] for k in ('channel', 'sender', 'event_id', 'event_at')} | dict(text=raw.get('text', ''), message_type='text')

	def receive(self, event=None, index=0):
		doc = self.bindings[index]
		event = event or self.incoming()
		if index: event = dict(event, channel=doc.channel)
		result = adapter_call(turn_ingress.receive, 'CTRPRO' if index == 0 else 'CTROTH', doc.name, event)
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		from messaging.conversation.doctype.chat_turn.chat_turn import turn_result
		# These contract tests control handoff separately after the worker prepares output.
		with patch('messaging.conversation.doctype.chat_turn.inbox._deliver_prepared',
			side_effect=lambda contact: not frappe.db.exists('Bot Dispatch', dict(contact=contact, status='Prepared'))):
			drain_contact(result['contact'])
		return turn_result(frappe.get_doc('Chat Turn', result['turn']))

	def test_registration_ownership_and_binding_identity(self):
		self.assertEqual(len([r for r in provider_catalog.provider_registry() if r['id'] in ('CTRPRO','CTROTH')]), 2)
		self.assertNotEqual(self.bindings[0].name, self.bindings[1].name)
		for definitions in ([dict(PROBE, app='frappe')], [dict(PROBE, operations=dict(PROBE['operations'], send='os.system'))], [PROBE, PROBE]):
			with registered_providers(definitions), self.assertRaises(frappe.ValidationError): provider_catalog.provider_registry()
		with self.assertRaises(frappe.ValidationError): provider_catalog.provider_definition('unregistered')
		with self.assertRaises(frappe.ValidationError): bind('missing')
		with self.assertRaises(frappe.DuplicateEntryError): bind(self.prefix)
		frappe.db.rollback()
		doc = self.bindings[0]; doc.account_id = self.prefix + '_changed'
		with self.assertRaises(frappe.ValidationError): doc.save()
		doc.reload(); doc.enabled = 0; doc.save(); frappe.db.commit()
		with self.assertRaises(frappe.PermissionError): self.receive()
		with self.assertRaises(frappe.DoesNotExistError): adapter_call(turn_ingress.receive, 'CTRPRO', 'missing', self.incoming())

	def test_normalized_ingress_dedupe_account_isolation_and_authorization(self):
		event = self.incoming(text='contract input')
		one = self.receive(event); duplicate = self.receive(event)
		self.assertEqual(one, duplicate)
		two = self.receive(event, index=1)
		self.assertNotEqual(one['contact'], two['contact'])
		self.assertEqual(frappe.db.count('Chat Turn', {'contact': one['contact']}), 1)
		self.assertEqual(frappe.db.count('Chat Message', {'contact': one['contact'], 'kind': 'Incoming'}), 1)
		for invalid in (dict(event, channel='other'), dict(event, method='os.system'), dict(event, message_type='invalid')):
			with self.assertRaises(frappe.ValidationError): self.receive(invalid)
		frappe.set_user('Guest')
		try:
			with self.assertRaises(frappe.PermissionError): self.receive(event)
		finally: frappe.set_user('Administrator')
		frappe.session.user = 'other-integration-user'
		try:
			with patch('messaging.conversation.doctype.chat_contact.chat_contact.frappe.has_permission', return_value=True):
				with self.assertRaises(frappe.PermissionError): self.receive(event)
		finally: frappe.set_user('Administrator')

	def test_transport_results_unknown_recovery_and_monotonic_delivery(self):
		self.flow()
		for outcome in ('Accepted', 'Rejected', 'Unknown'):
			with self.subTest(outcome=outcome):
				result = self.receive()
				with patch(__name__ + '.send_result', return_value=dict(outcome=outcome, reference='probe-ref')) as transport:
					first = bot_delivery.deliver(result['dispatch'])
					self.assertEqual(first['state'], outcome)
					bot_delivery.deliver(result['dispatch']); self.assertEqual(transport.call_count, 1)
					payload = transport.call_args.args[0]
					self.assertEqual((payload['provider'], payload['account_id'], payload['account']), ('CTRPRO', self.prefix, self.bindings[0].name))
					self.assertEqual(payload['destination'], 'external-user')
				if outcome == 'Unknown':
					binding = self.bindings[0]
					self.assertEqual(adapter_call(channel_transport.report_status, 'CTRPRO', binding.name, result['dispatch'], 'Read', 'probe-ref')['acceptance'], 'Accepted')
					for state in ('Sent', 'Failed', 'Delivered'):
						self.assertEqual(adapter_call(channel_transport.report_status, 'CTRPRO', binding.name, result['dispatch'], state, 'probe-ref')['delivery'], 'Read')
					with self.assertRaises(frappe.ValidationError): adapter_call(channel_transport.report_status, 'CTRPRO', binding.name, result['dispatch'], 'Read', 'wrong-ref')
					with self.assertRaises(frappe.PermissionError): adapter_call(channel_transport.report_result, 'CTROTH', self.bindings[1].name, result['dispatch'], dict(outcome='Accepted', reference='probe-ref'))
				if outcome == 'Rejected':
					with self.assertRaises(frappe.ValidationError): adapter_call(channel_transport.report_result, 'CTRPRO', self.bindings[0].name, result['dispatch'], dict(outcome='Accepted', reference='probe-ref'))
		for response in (TimeoutError('private'), dict(outcome='invalid'), dict(outcome='Accepted')):
			result = self.receive()
			kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
			with patch(__name__ + '.send_result', **kwargs): self.assertEqual(bot_delivery.deliver(result['dispatch'])['state'], 'Unknown')
			adapter_call(channel_transport.report_result, 'CTRPRO', self.bindings[0].name, result['dispatch'], dict(outcome='Rejected'))

	def test_interactive_selections_use_only_issued_current_menu(self):
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
		configuration = create_configuration(self.prefix + '_channel_ai', api_key='synthetic-channel-key')
		self.configure(interpretation_enabled=1, active_ai_configuration=configuration.name)
		self.addCleanup(lambda: (frappe.set_user('Administrator'), frappe.delete_doc('AI Configuration', configuration.name, delete_permanently=True), frappe.db.commit()))
		flow = self.flow()
		menu = self.receive()
		payload = bot_delivery.deliver(menu['dispatch'])
		option = next(item for item in payload['interactive'] if item['label'] == flow.name)
		event = dict(self.incoming(), message_type='interactive', interactive=dict(dispatch=menu['dispatch'], selection_id=option['id']))
		selected = self.receive(event)
		self.assertEqual(selected['response'], 'Result hours.')
		# The durable inbox waits for the preceding handoff before processing another input.
		bot_delivery.deliver(selected['dispatch'])
		for altered in (dict(event, event_id=frappe.generate_hash(), interactive=dict(dispatch=menu['dispatch'], selection_id='forged')),):
			self.assertEqual(self.receive(altered)['state'], 'Ignored')
		self.assertEqual(self.receive(dict(event, event_id=frappe.generate_hash()), index=1)['state'], 'Ignored')

	def test_quota_and_disabled_account_cannot_be_bypassed_by_adapter(self):
		self.flow()
		self.configure(daily_quota_limit=1, monthly_quota_limit=1)
		first = self.receive(); bot_delivery.deliver(first['dispatch'])
		self.assertIsNone(self.receive()['dispatch'])
		doc = self.bindings[0]; doc.enabled = 0; doc.save(); frappe.db.commit()
		with self.assertRaises(frappe.PermissionError): bot_delivery.deliver(first['dispatch'])

	def test_business_hook_without_consumers_does_no_work(self):
		from messaging import hooks
		self.assertEqual(hooks.doc_events['*']['on_update'], 'messaging.communication.doctype.notification_rule.events.document_event')
		frappe.local.messaging_business_observers = {}
		with patch('messaging.communication.doctype.notification_rule.events.rule_index', return_value={}), patch.object(frappe.db, 'sql', side_effect=AssertionError('Unexpected query')), patch.object(frappe, 'enqueue', side_effect=AssertionError('Unexpected queue')), patch.object(frappe, 'get_attr', side_effect=AssertionError('Unexpected import')):
			for doctype in ('Unrelated Document', 'WA Message', 'WA Log'):
				notification_events.document_event(frappe._dict(doctype=doctype, name='probe', is_new=lambda: False, flags=frappe._dict()), 'on_update')
		for boundary in (turn_ingress.receive, bot_delivery.deliver, channel_transport.report_result, channel_transport.report_status):
			self.assertNotIn(boundary, frappe.whitelisted)

	def test_registered_business_observer_receives_only_exact_event_after_commit(self):
		frappe.local.messaging_business_observers = {'Business Document': {'on_update': [__name__ + '.observe_business']}}
		try:
			with patch.object(type(frappe.db.after_commit), 'add') as after_commit, patch.object(frappe, 'get_attr') as resolve:
				notification_events.document_event(frappe._dict(doctype='Other Document', name='probe', is_new=lambda: False, flags=frappe._dict()), 'on_update')
				notification_events.document_event(frappe._dict(doctype='Business Document', name='probe', is_new=lambda: False, flags=frappe._dict()), 'after_insert')
				after_commit.assert_not_called()
				notification_events.document_event(frappe._dict(doctype='Business Document', name='probe', is_new=lambda: False, flags=frappe._dict()), 'on_update')
				resolve.assert_not_called()
				after_commit.assert_called_once()
				after_commit.call_args.args[0]()
				resolve.return_value.assert_called_once_with(doctype='Business Document', name='probe', event='on_update')
		finally: frappe.local.messaging_business_observers = {}


	def test_concurrent_adapter_receive_and_handoff_are_idempotent(self):
		from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
		flow = self.flow(show_in_menu=0)
		self.configure(greeting_enabled=1, greeting_flow=flow.name,
			greeting_states=[dict(state=state_name(flow.name, 'success'))])
		frappe.db.commit()
		event = self.incoming(text='hello')
		ctx = multiprocessing.get_context('spawn'); barrier = ctx.Barrier(2); results = ctx.Queue()
		children = [ctx.Process(target=concurrent_adapter, args=(frappe.local.site, str(frappe.local.sites_path), self.bindings[0].name, event, barrier, results)) for _ in range(2)]
		try:
			for child in children: child.start()
			out = [results.get(timeout=40) for _ in children]
			for child in children:
				child.join(timeout=5); self.assertEqual(child.exitcode, 0)
			self.assertFalse(any('error' in row for row in out), out)
			self.assertEqual(len({row['turn'] for row in out}), 1)
			self.assertEqual(len({row['dispatch'] for row in out}), 1)
			self.assertEqual({row['state'] for row in out}, {'Accepted'})
			frappe.db.rollback()
			self.assertEqual(frappe.db.count('Chat Turn', {'contact': out[0]['contact']}), 1)
			self.assertEqual(frappe.db.count('Process Execution Log', {'turn': out[0]['turn']}), 1)
			self.assertEqual(frappe.db.count('Bot Dispatch', {'contact': out[0]['contact']}), 1)
		finally:
			for child in children:
				if child.is_alive(): child.terminate(); child.join()

"""Focused native identity, authorization, context and account invariants."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import frappe

from messaging.security import execution_context as automation, service_identity as identity
from messaging.communication.doctype.communication_channel_account.communication_channel_account import dispatch_ingress
from messaging.chatbot.doctype.bot_settings import language as settings_module
from messaging.emulator.doctype.emulator_contact import emulator_contact as accounts


class TestAutomation(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.db.savepoint("automation_test")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback(save_point="automation_test")
		frappe.clear_cache(user=identity.SERVICE_USER)
		frappe.clear_cache(doctype="Chat Turn")

	def test_native_initial_creation_and_idempotency_without_credentials(self):
		# Only the synthetic service identity is removed inside a rollback scope.
		frappe.db.delete("Has Role", {"parent": identity.SERVICE_USER, "parenttype": "User"})
		frappe.db.delete("User", {"name": identity.SERVICE_USER})
		frappe.clear_cache(user=identity.SERVICE_USER)
		with patch("frappe.sendmail", side_effect=AssertionError("Unexpected email")), patch("frappe.enqueue"):
			self.assertEqual(identity.ensure_messaging_service_user(), identity.SERVICE_USER)
			before = frappe.get_doc("User", identity.SERVICE_USER).as_dict()
			self.assertEqual(identity.ensure_messaging_service_user(), identity.SERVICE_USER)
			self.assertEqual(frappe.get_doc("User", identity.SERVICE_USER).as_dict(), before)
		self.assertEqual(before["user_type"], "Website User")
		self.assertNotIn("System Manager", [r.role for r in before["roles"]])
		self.assertFalse(before["api_key"] or before["api_secret"] or before["send_welcome_email"])
		self.assertFalse(frappe.db.exists("__Auth", {"doctype": "User", "name": identity.SERVICE_USER}))

	def test_missing_disabled_and_insufficient_permissions_fail_without_creation(self):
		with patch("frappe.new_doc", side_effect=AssertionError("Unexpected provisioning")):
			frappe.db.set_value("User", identity.SERVICE_USER, "enabled", 0)
			with self.assertRaises(frappe.PermissionError): identity.require_messaging_service_user()
			frappe.db.set_value("User", identity.SERVICE_USER, "enabled", 1)
			frappe.db.delete("DocPerm", {"parent": "Chat Turn", "role": identity.AUTOMATION_ROLE})
			frappe.clear_cache(doctype="Chat Turn")
			with self.assertRaises(frappe.PermissionError): identity.require_messaging_service_user()
			frappe.db.delete("User", {"name": identity.SERVICE_USER})
			with self.assertRaises(frappe.PermissionError): identity.require_messaging_service_user()

	def test_native_business_role_needs_no_declaration_and_preserves_identity_guards(self):
		role = frappe.get_doc(dict(doctype="Role", role_name="Synthetic Business " + frappe.generate_hash(length=8),
			desk_access=0)).insert()
		user = frappe.get_doc("User", identity.SERVICE_USER)
		user.append("roles", dict(role=role.name))
		user.save()
		before = frappe.get_doc("User", user.name).as_dict()
		self.assertEqual(identity.require_messaging_service_user(), user.name)
		self.assertEqual(frappe.get_doc("User", user.name).as_dict(), before)
		for field in ("disabled", "desk_access"):
			frappe.db.set_value("Role", role.name, field, 1)
			with self.assertRaises(frappe.PermissionError): identity.require_messaging_service_user()
			frappe.db.set_value("Role", role.name, field, 0)
		self.assertEqual(identity.require_messaging_service_user(), user.name)
		original = frappe.has_permission
		def administrative_permission(doctype, ptype, *args, **kwargs):
			return True if (doctype, ptype) == ("User", "create") else original(doctype, ptype, *args, **kwargs)
		with patch.object(frappe, "has_permission", administrative_permission):
			with self.assertRaises(frappe.PermissionError): identity.require_messaging_service_user()

	def test_guest_cannot_setup_or_enter_engine_and_no_public_api(self):
		from messaging.conversation.doctype.chat_turn.chat_turn import process_event
		from messaging.conversation.doctype.chat_turn import ingress as turn_ingress
		from messaging.chatbot.doctype.bot_dispatch import delivery as bot_delivery
		from messaging.communication.doctype.communication_channel_account import communication_channel_account as channel_transport
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError): identity.ensure_messaging_service_user()
		with self.assertRaises(frappe.PermissionError): process_event({})
		for method in (identity.ensure_messaging_service_user, dispatch_ingress,
			turn_ingress.receive, channel_transport.report_status): self.assertNotIn(method, frappe.whitelisted)

	def test_context_restores_session_form_and_caches_even_on_exception(self):
		original_session = frappe.local.session
		original_form = frappe.local.form_dict
		original_cache = frappe.local.cache
		original_session.sid = "synthetic-http-session"
		original_session.data = frappe._dict(csrf_token="synthetic-csrf")
		original_form["synthetic"] = "request"
		try:
			with self.assertRaisesRegex(RuntimeError, "controlled"):
				with automation._automation_context():
					self.assertTrue(automation.is_automation())
					self.assertEqual(frappe.session.user, identity.SERVICE_USER)
					with automation._automation_context(): self.assertTrue(automation.is_automation())
					raise RuntimeError("controlled")
			self.assertIs(frappe.local.session, original_session)
			self.assertIs(frappe.local.form_dict, original_form)
			self.assertIs(frappe.local.cache, original_cache)
			self.assertEqual((frappe.session.user, frappe.session.sid), ("Administrator", "synthetic-http-session"))
			self.assertEqual(frappe.session.data.csrf_token, "synthetic-csrf")
			self.assertFalse(automation.is_automation())
		finally:
			original_session.sid = "Administrator"
			original_form.pop("synthetic", None)

	def test_process_reference_is_scoped_copied_and_restored(self):
		path = "integration_app.processes.report.prepare"
		with automation._automation_context():
			with automation.authorized_process(path, source_type="Bot Flow", source_name="water_issue",
				session="synthetic-session", turn="synthetic-turn", execution_id="a" * 64):
				context = automation.require_automation_process(path)
				self.assertEqual(context["execution_id"], "a" * 64)
				context["execution_id"] = "forged"
				self.assertEqual(automation.require_automation_process(path)["execution_id"], "a" * 64)
				with automation.authorized_process("other.process.path"):
					with self.assertRaises(frappe.PermissionError): automation.require_automation_process(path)
				self.assertEqual(automation.require_automation_process(path)["turn"], "synthetic-turn")
			with self.assertRaises(frappe.PermissionError): automation.require_automation_process(path)

	def test_unsupported_media_keeps_text_without_interpreting_attachment(self):
		from messaging.conversation.doctype.chat_turn.ingress import normalize_event
		from types import SimpleNamespace
		binding = SimpleNamespace(name="synthetic-account", channel="synthetic")
		event = dict(channel="synthetic", sender="synthetic", event_id="synthetic", event_at="2026-10-26T00:00:00Z",
			message_type="unsupported", text="Fuga en la calle de prueba")
		request = normalize_event({}, binding, event)
		self.assertFalse(request["unsupported"])
		self.assertEqual(request["text"], event["text"])
		self.assertEqual(event["message_type"], "unsupported")
		self.assertTrue(normalize_event({}, binding, {**event, "text": ""})["unsupported"])



	def test_automatic_executor_uses_configured_flow_without_allowlist(self):
		from messaging.communication.doctype.process_definition import execution as process
		settings = frappe.get_single("Bot Settings")
		code = "automatic_flow_" + frappe.generate_hash(length=10)
		path = "messaging.communication.doctype.process_definition.test_process_definition.technical_flow"
		from messaging.registry.processes import register_processes
		from messaging.registry.processes import BotProcess
		from messaging.communication.doctype.process_definition.test_process_definition import technical_business
		flow = frappe.new_doc("Bot Flow")
		flow.update(dict(workflow_code=code, workflow_name=code,
			semantic_description="Synthetic configured automatic Process.", enabled=0, priority=0,
			show_in_menu=0, process_method=technical_business.__module__ + ".technical_business", outputs=[dict(status_code="success", output_mode="Message")]))
		identifier = "messaging." + code
		register_processes(app="messaging", processes=[BotProcess(id=identifier, label="Technical automatic Process",
			definition=frappe.get_attr(path), handler=technical_business)])
		flow.process_definition = identifier
		flow.set("__islocal",1)
		from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, set_active
		from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
		flow.append('translations', dict(language=resolve_process_language(), semantic_description=flow.semantic_description))
		flow.outputs[0].output_mode = 'Message'
		original_sid = frappe.session.get('sid')
		frappe.session.sid = 'automation-test-' + frappe.generate_hash(length=12)
		try:
			flow.insert()
			self.assertEqual(validate_process(flow.as_dict())['status'], 'Passed')
			flow.reload()
			set_active(flow.as_dict(), 1)
			flow.reload()
		finally:
			frappe.session.sid = original_sid
		with automation._automation_context(), patch.object(settings_module, "get_bot_settings", return_value=settings):
			result = process.execute_process(process_method="messaging.communication.doctype.process_definition.test_process_definition.technical_business", arguments={},
				source_type="Bot Flow", source_name=flow.name)
			self.assertTrue(result["functional"])
			self.assertEqual(result["state"], "Complete")
			log = frappe.get_doc("Process Execution Log", result["execution_log"])
			self.assertEqual((log.owner, log.modified_by), (identity.SERVICE_USER, identity.SERVICE_USER))

	def account(self, **values):
		if not frappe.db.exists("Communication Channel", accounts.CHANNEL):
			provider_catalog.set_provider_enabled(accounts.PROVIDER, True)
			frappe.get_doc(dict(doctype="Communication Channel", provider=accounts.PROVIDER,
				channel_name="Synthetic emulator channel")).insert()
		return frappe.get_doc(dict(doctype="Communication Channel Account", channel=accounts.CHANNEL,
			account_name="Synthetic identity test", enabled=0) | values).insert()

	def test_emulator_time_format_unique_persistent_and_immutable(self):
		instant = datetime(2026, 10, 24, 1, 41, 25, 837452, tzinfo=timezone.utc)
		with patch.object(accounts, "datetime") as clock:
			local = instant.astimezone(accounts.ZoneInfo("America/Tegucigalpa"))
			clock.now.side_effect = [local, local.replace(microsecond=837453)]
			one, two = self.account(), self.account()
			self.assertEqual(clock.now.call_count, 2)
			self.assertTrue(all(str(call.args[0]) == "America/Tegucigalpa" for call in clock.now.call_args_list))
		self.assertEqual(one.account_id, "EMU-20261023194125837452")
		self.assertRegex(one.account_id, r"^EMU-[0-9]{20}$")
		self.assertNotEqual(one.account_id, two.account_id)
		self.assertEqual(one.meta.autoname, "field:account_id")
		for doc in (one, two):
			self.assertEqual(doc.name, doc.account_id)
			self.assertEqual(frappe.get_doc(doc.doctype, doc.name).account_id, doc.name)
			identity = doc.account_id
			doc.account_name = "Edited synthetic identity test"; doc.save(); doc.reload()
			self.assertEqual((doc.name, doc.account_id), (identity, identity))
		one.account_id = two.account_id
		with self.assertRaises(frappe.ValidationError): one.save()
		one.reload()
		self.assertEqual((one.name, one.account_id), ("EMU-20261023194125837452",) * 2)
		self.assertFalse(one.meta.has_field("integration_user"))

	def test_emulator_collision_retries_database_unique_violation(self):
		one = self.account()
		one.reload()
		before = one.as_dict()
		count = frappe.db.count(one.doctype)
		next_id = accounts.generate_emulator_account_id()
		insert = channel_transport.Document.db_insert
		collisions = []
		def record(doc, *args, **kwargs):
			try:
				return insert(doc, *args, **kwargs)
			except frappe.DuplicateEntryError as error:
				collisions.append(error)
				raise
		with patch.object(channel_transport.Document, "db_insert", autospec=True, side_effect=record), patch.object(
			accounts, "generate_emulator_account_id", side_effect=[one.account_id, next_id]) as generator:
			two = self.account(parameters=[dict(parameter_key="transport_outcome", value='"Accepted"')])
			self.assertEqual(generator.call_count, 2)
		self.assertEqual(len(collisions), 1)
		self.assertTrue(frappe.db.is_primary_key_violation(collisions[0].args[2]))
		two.reload()
		self.assertEqual((two.name, two.account_id), (next_id, next_id))
		self.assertEqual(frappe.get_doc(one.doctype, one.name).as_dict(), before)
		self.assertEqual(frappe.db.count(one.doctype), count + 1)
		self.assertEqual(frappe.db.count(one.doctype, {"account_id": one.account_id}), 1)
		self.assertEqual(frappe.db.count("Communication Provider Value", {"parent": one.name}), len(one.parameters))
		self.assertEqual(frappe.db.count("Communication Provider Value", {"parent": two.name}), 1)
		self.assertEqual(two.parameters[0].parent, two.name)

	def test_emulator_collision_exhaustion_leaves_no_partial_writes(self):
		one = self.account()
		one.reload()
		before = one.as_dict()
		counts = [frappe.db.count(dt) for dt in (one.doctype, "Communication Provider Value", "__Auth")]
		with patch.object(accounts, "generate_emulator_account_id", return_value=one.account_id) as generator:
			with self.assertRaises(frappe.DuplicateEntryError):
				self.account(parameters=[dict(parameter_key="transport_outcome", value='"Accepted"')])
			self.assertEqual(generator.call_count, 5)
		self.assertEqual([frappe.db.count(dt) for dt in (one.doctype, "Communication Provider Value", "__Auth")], counts)
		self.assertEqual(frappe.get_doc(one.doctype, one.name).as_dict(), before)

	def test_emulator_unique_account_id_collision_is_retried(self):
		# Normal insert synchronizes the field with the name. A raw, reversible
		# fixture isolates the secondary index without changing the site schema.
		self.account()
		one = frappe.get_doc(dict(doctype="Communication Channel Account", channel=accounts.CHANNEL,
			account_id=accounts.generate_emulator_account_id(), account_name="Synthetic unique index probe", enabled=0))
		one.name = "synthetic-" + frappe.generate_hash(length=12)
		one.db_insert()
		one.reload()
		before = one.as_dict()
		next_id = accounts.generate_emulator_account_id()
		insert = channel_transport.Document.db_insert
		collisions = []
		def record(doc, *args, **kwargs):
			try:
				return insert(doc, *args, **kwargs)
			except frappe.UniqueValidationError as error:
				collisions.append(error)
				raise
		with patch.object(channel_transport.Document, "db_insert", autospec=True, side_effect=record), patch.object(
			accounts, "generate_emulator_account_id", side_effect=[one.account_id, next_id]) as generator:
			two = self.account()
			self.assertEqual(generator.call_count, 2)
		self.assertEqual(len(collisions), 1)
		self.assertTrue(frappe.db.is_unique_key_violation(collisions[0].args[2]))
		self.assertIn("'account_id'", collisions[0].args[2].args[1])
		self.assertEqual((two.name, two.account_id), (next_id, next_id))
		self.assertEqual(frappe.get_doc(one.doctype, one.name).as_dict(), before)
		# The stronger global index normally masks the composite index. Exercise
		# its Frappe wrapper shape against the actual conflicting account row.
		probe = frappe.get_doc(dict(doctype=one.doctype, channel=one.channel, account_id=one.account_id))
		probe.name = one.account_id
		raw = collisions[0].args[2]
		database_error = type(raw)(raw.args[0], raw.args[1].replace("'account_id'", "'unique_channel_account_id'"))
		self.assertTrue(probe._generated_identity_collision(frappe.UniqueValidationError(probe.doctype, probe.name, database_error)))

	def test_emulator_unrelated_errors_are_not_retried(self):
		one = self.account()
		with self.assertRaises(frappe.DuplicateEntryError) as collision:
			frappe.get_doc(dict(doctype=one.doctype, account_id=one.account_id, name=one.name)).db_insert()
		raw = collision.exception.args[2]
		count = frappe.db.count(one.doctype)
		for error_type, key, value in (
			(frappe.UniqueValidationError, "unrelated", "unrelated"),
			(frappe.UniqueValidationError, "other_unique_channel_account_id", "unrelated"),
			(frappe.DuplicateEntryError, "unrelated", "PRIMARY"),
		):
			with self.subTest(key=key, error_type=error_type), patch.object(accounts, "generate_emulator_account_id") as generator:
				generator.return_value = one.account_id
				def fail(doc, *args, **kwargs):
					error = type(raw)(1062, "Duplicate entry '" + value + "' for key '" + key + "'")
					raise error_type(doc.doctype, doc.name, error)
				with patch.object(channel_transport.Document, "db_insert", autospec=True, side_effect=fail):
					with self.assertRaises(error_type): self.account()
				self.assertEqual(generator.call_count, 1)
		with patch.object(accounts, "generate_emulator_account_id", wraps=accounts.generate_emulator_account_id) as generator:
			with self.assertRaises(frappe.MandatoryError): self.account(account_name="")
			self.assertEqual(generator.call_count, 1)
			frappe.set_user("Guest")
			try:
				with self.assertRaises(frappe.PermissionError): self.account()
			finally: frappe.set_user("Administrator")
			self.assertEqual(generator.call_count, 1)
		self.assertEqual(frappe.db.count(one.doctype), count)

	def test_emulator_display_values_are_replaced_only_on_creation(self):
		for display in ("Automatic", "Autom\u00e1tico", "external-proposed-id"):
			with self.subTest(display=display):
				doc = self.account(account_id=display, account_name="Synthetic display test")
				self.assertRegex(doc.account_id, r"^EMU-[0-9]{20}$")
				self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, "account_id"), doc.account_id)
				identity = doc.account_id
				doc.account_name = "Edited synthetic display test"; doc.save()
				self.assertEqual(doc.account_id, identity)
				doc.account_id = display
				with self.assertRaises(frappe.ValidationError): doc.save()

	def test_nested_language_and_request_context_restores_inner_and_outer_state(self):
		original = {key: getattr(frappe.local, key, None) for key in ('session', 'form_dict', 'cache', 'role_permissions', 'lang')}
		with automation._automation_context():
			frappe.local.lang = 'es'
			outer_session = frappe.local.session
			outer_form = frappe.local.form_dict
			frappe.local.session.data['marker'] = 'outer'
			with self.assertRaises(RuntimeError):
				with automation._automation_context():
					frappe.local.lang = 'en'
					frappe.local.session.data['marker'] = 'inner'
					frappe.local.form_dict['marker'] = 'inner'
					raise RuntimeError('controlled')
			self.assertEqual(frappe.local.lang, 'es')
			self.assertIs(frappe.local.session, outer_session)
			self.assertIs(frappe.local.form_dict, outer_form)
			self.assertEqual(frappe.local.session.data['marker'], 'outer')
			self.assertNotIn('marker', outer_form)
		for key, value in original.items():
			if key == 'lang': self.assertEqual(frappe.local.lang, value)
			else: self.assertIs(getattr(frappe.local, key), value)

	def test_worker_requires_exact_native_job_and_no_guest_setup(self):
		@automation.automatic_job
		def controlled_job(): return frappe.session.user
		previous = getattr(frappe.local, 'job', None)
		frappe.set_user('Guest')
		try:
			for job in (None, frappe._dict(method='messaging.arbitrary.job')):
				frappe.local.job = job
				with self.assertRaises(frappe.PermissionError): controlled_job()
			frappe.local.job = frappe._dict(method=controlled_job.__module__ + '.controlled_job')
			self.assertEqual(controlled_job(), identity.SERVICE_USER)
			self.assertEqual(frappe.session.user, 'Guest')
		finally: frappe.local.job = previous

"""Registered cryptographic verifier and pre-authentication least privilege, without network."""
import hashlib
import hmac
import json
import unittest
from unittest.mock import patch

import frappe

from messaging.communication.doctype.communication_channel_account.communication_channel_account import dispatch_ingress
from messaging.communication.doctype.communication_provider import test_communication_provider as fixtures
from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
from messaging.communication.doctype.communication_provider_value.communication_provider_value import transport_parameters
from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
from messaging.security.execution_context import is_automation, require_authenticated_adapter
from messaging.security.service_identity import SERVICE_USER

VERIFICATIONS = []


def authenticate_probe(binding, request, credentials):
	# This assertion also detects accidental callback access to the outbound secret API.
	assert not is_automation() and frappe.session.user != SERVICE_USER
	assert set(credentials) == {'Channel', 'Account'}
	assert set(credentials['Channel']) == {'app_secret'} and not credentials['Account']
	try:
		transport_parameters(binding.provider, binding.account)
	except frappe.PermissionError:
		pass
	else:
		raise AssertionError('Pre-authentication access to transport parameters')
	VERIFICATIONS.append(frappe.session.user)
	body = request['body']
	expected = hmac.new(credentials['Channel']['app_secret'].encode(), body, hashlib.sha256).hexdigest()
	if not hmac.compare_digest(request['signature'], expected):
		raise frappe.PermissionError('Invalid synthetic signature')
	event = json.loads(body)
	if set(event) != {'account_id', 'event'} or event['account_id'] != binding.account_id:
		raise frappe.PermissionError('Invalid synthetic association')
	return dict(action='receive', payload=dict(event=event['event']))


@contextmanager
def registered_verifier(**kwargs):
	"""Replace only invocation, keeping the registered source function verifiable."""
	from unittest.mock import Mock
	verifier = Mock(**kwargs)
	original = frappe.get_attr
	with patch.object(frappe, 'get_attr', side_effect=lambda path: verifier if path == __name__ + '.authenticate_probe' else original(path)):
		yield verifier


class TestIngress(unittest.TestCase):
	channel = fixtures.TestCommunicationProvider.channel
	account = fixtures.TestCommunicationProvider.account
	tearDown = fixtures.TestCommunicationProvider.tearDown

	def setUp(self):
		fixtures.TestCommunicationProvider.setUp(self)
		self.declarations[0]['operations']['authenticate_ingress'] = __name__ + '.authenticate_probe'
		self.declarations[0]['ingress_parameters'] = {'Channel': ['app_secret']}
		sync_communication_providers()
		set_provider_enabled('TSTPV', True)
		self.binding = self.account(self.channel(enabled=1), enabled=1,
			parameters=[dict(parameter_key='token', secret_value='unrelated-outbound-secret')])
		set_provider_enabled('TSTPV', True)
		VERIFICATIONS.clear()
		self.original = frappe.local.session
		self.form = frappe.local.form_dict

	def request(self, **overrides):
		body = json.dumps(dict(account_id=self.binding.account_id, event=dict(channel=self.binding.channel,
			sender='synthetic-sender', event_id='synthetic-event', event_at='2026-10-23T00:00:00Z',
			message_type='text', text='synthetic')), sort_keys=True).encode()
		return dict(body=body, signature=hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest()) | overrides

	def test_valid_signature_grants_exact_context_only_after_verification(self):
		frappe.set_user('Guest')
		original_session, original_form = frappe.local.session, frappe.local.form_dict
		previous_lang = frappe.local.lang
		def receive(provider, account, event):
			self.assertTrue(is_automation())
			require_authenticated_adapter(provider, account)
			with self.assertRaises(frappe.PermissionError): require_authenticated_adapter('TSTOT', account)
			frappe.local.lang = 'en'
			return dict(turn='synthetic')
		with patch('messaging.conversation.doctype.chat_turn.ingress.receive', side_effect=receive):
			self.assertEqual(dispatch_ingress('TSTPV', self.binding.name, self.request()), {'turn': 'synthetic'})
		self.assertEqual(VERIFICATIONS, ['Guest'])
		self.assertFalse(is_automation())
		self.assertIs(frappe.local.session, original_session)
		self.assertIs(frappe.local.form_dict, original_form)
		self.assertEqual(frappe.local.lang, previous_lang)

	def test_invalid_signature_and_claimed_identity_never_enter_engine(self):
		frappe.set_user('Guest')
		with patch('messaging.security.execution_context._automation_context') as context, patch('messaging.conversation.doctype.chat_turn.ingress.receive') as receive:
			for request in (self.request(signature='invalid'), self.request(body=b'{"verified":true,"user":"Administrator"}')):
				with self.assertRaises(frappe.PermissionError): dispatch_ingress('TSTPV', self.binding.name, request)
			context.assert_not_called(); receive.assert_not_called()
		self.assertEqual(frappe.session.user, 'Guest')
		self.assertFalse(is_automation())

	def test_valid_signature_with_wrong_account_is_rejected(self):
		body = json.dumps(dict(account_id='other-account', event={})).encode()
		request = self.request(body=body, signature=hmac.new(b'synthetic-secret', body, hashlib.sha256).hexdigest())
		with patch('messaging.conversation.doctype.chat_turn.ingress.receive') as receive:
			with self.assertRaises(frappe.PermissionError): dispatch_ingress('TSTPV', self.binding.name, request)
			receive.assert_not_called()

	def test_disabled_missing_or_incompatible_binding_never_calls_verifier(self):
		with registered_verifier() as verifier:
			with self.assertRaises(frappe.ValidationError): dispatch_ingress('UNKNOWN', self.binding.name, {})
			with self.assertRaises(frappe.DoesNotExistError): dispatch_ingress('TSTPV', 'missing', {})
			self.binding.enabled = 0; self.binding.save()
			with self.assertRaises(frappe.PermissionError): dispatch_ingress('TSTPV', self.binding.name, {})
			verifier.assert_not_called()

	def test_undeclared_verification_keys_and_unregistered_routes_are_rejected(self):
		for selection in ({'Channel': ['token']}, {'Channel': ['app_secret', 'app_secret']}, {'Unknown': []}):
			self.declarations[0]['ingress_parameters'] = selection
			with self.assertRaises(frappe.ValidationError): sync_communication_providers()
		self.declarations[0]['ingress_parameters'] = {'Channel': ['app_secret']}
		self.declarations[0]['operations']['authenticate_ingress'] = 'os.system'
		with self.assertRaises(frappe.ValidationError): sync_communication_providers()

	def test_verifier_cannot_return_privilege_flags_or_executable_routes(self):
		for response in (True, {'verified': True}, {'action': 'os.system', 'payload': {}},
			{'action': 'receive', 'payload': {'event': {}, 'user': SERVICE_USER}}):
			with registered_verifier(return_value=response), patch('messaging.conversation.doctype.chat_turn.ingress.receive') as receive:
				with self.assertRaises(frappe.PermissionError): dispatch_ingress('TSTPV', self.binding.name, self.request())
				receive.assert_not_called()

	def test_engine_exception_restores_identity_and_adapter_scope(self):
		frappe.set_user('Guest')
		with patch('messaging.conversation.doctype.chat_turn.ingress.receive', side_effect=RuntimeError('controlled')):
			with self.assertRaises(RuntimeError): dispatch_ingress('TSTPV', self.binding.name, self.request())
		self.assertEqual(frappe.session.user, 'Guest')
		self.assertFalse(is_automation())
		with self.assertRaises(frappe.PermissionError): require_authenticated_adapter('TSTPV', self.binding.name)

	def test_malformed_normalized_event_never_adopts_identity(self):
		response = dict(action='receive', payload=dict(event=dict(channel=self.binding.channel,
			sender='synthetic', event_id='synthetic', event_at='invalid', message_type='text', text='synthetic')))
		with registered_verifier(return_value=response), patch('messaging.security.execution_context._automation_context') as context:
			with self.assertRaises(frappe.ValidationError): dispatch_ingress('TSTPV', self.binding.name, self.request())
			context.assert_not_called()

	def test_missing_authenticator_and_direct_receive_never_grant_context(self):
		self.declarations[0]['operations'].pop('authenticate_ingress')
		self.declarations[0].pop('ingress_parameters')
		sync_communication_providers()
		with patch('messaging.security.execution_context._automation_context') as context:
			with self.assertRaises(frappe.PermissionError): dispatch_ingress('TSTPV', self.binding.name, self.request())
			from messaging.conversation.doctype.chat_turn.ingress import receive
			with self.assertRaises(frappe.PermissionError): receive('TSTPV', self.binding.name, {})
			context.assert_not_called()
