"""Transactional preparation, durable dispatch and real emulator integration fixtures."""
from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.registry import load_declarations
from messaging.communication.doctype.communication_provider import communication_provider as provider_catalog
from contextlib import contextmanager, nullcontext
from datetime import timedelta
import json
import multiprocessing
import time
import traceback
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime, now_datetime

from messaging.conversation.doctype.chat_turn import ingress as turn_ingress
from messaging.chatbot.doctype.bot_dispatch import delivery as bot_delivery
from messaging.communication.doctype.communication_channel_account import communication_channel_account as transport
from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import adapter_call
from messaging.communication.doctype.notification_rule import events
from messaging.communication.doctype.notification_rule.notification_rule import invalidate_rules, configuration
from messaging.communication.doctype.notification_queue import dispatch
from messaging.communication.doctype.notification_queue.notification_queue import internal_write
from messaging.communication.doctype.communication_provider.communication_provider import (
	get_provider_states, set_provider_enabled, _lock_provider_states, _write_provider_states,
)
from messaging.emulator.page.emulator_console import emulator_console as console
from messaging.common.locking import contact_lock
from messaging.conversation.doctype.chat_contact.chat_contact import get_contact
from messaging.conversation.doctype.chat_session import human
from messaging.conversation.doctype.chat_session.chat_session import get_session

_calls = []


def controlled_prepare(doctype, name, event, occurrence):
	"""Synthetic source-owned Process; all data is fictitious and no network is used."""
	_calls.append((name, occurrence))
	doc = frappe.get_doc(doctype, name)
	mode = doc.display_name
	if mode == 'skip': data = {'action': 'skip'}
	elif mode == 'exception': raise RuntimeError('private-diagnostic-token')
	elif mode == 'timeout':
		time.sleep(0.2)
		data = {'action': 'skip'}
	elif mode == 'invalid': data = {'recipient': doc.sender_id}
	else:
		data = dict(action='send', name=doc.display_name, amount='L 250.00')
	return dict(version=1, recipient="" if mode in ("skip", "timeout") else "+50499991234", status='prepared', data=data, message='')


def controlled_text(doctype, name, event, occurrence):
	_calls.append((name, occurrence))
	doc = frappe.get_doc(doctype, name)
	return dict(version=1, recipient="+50499991234", status='prepared', data=dict(action='send', text='Prepared '+doc.display_name), message='')


def available():
	return dict(version=1, recipient="", status='success', data=dict(available=True, next_available=None), message='')


def probe_account(binding):
	return binding.doctype == 'Communication Channel Account'


def probe_content(payload):
	return payload['content_type'] in ('Text', 'Template')


def probe_validate_template(template, context=None):
	return True


def probe_send(payload):
	return dict(outcome='Accepted', reference='probe-'+payload['outbound_id'])


@contextmanager
def patch_transport_operation(target_module, op_name, handler):
	orig = target_module.operation

	def wrapped(definition, name, *args):
		if name == op_name:
			return handler(definition, name, *args, orig=orig)
		return orig(definition, name, *args)

	with patch.object(target_module, 'operation', side_effect=wrapped):
		yield


def parallel_task(site, sites_path, action, name, barrier, output):
	frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
	_calls.clear()
	try:
		barrier.wait(timeout=20)
		if action == 'event':
			frappe.get_doc('Emulator Contact', name).run_method('after_insert')
			frappe.db.commit()
			value = len(_calls)
		else:
			value = dispatch.deliver(name, now=now_datetime()+timedelta(hours=2))['state']
		output.put(dict(value=value))
	except Exception as exc:
		output.put(dict(error=type(exc).__name__, trace=traceback.format_exc()))
	finally:
		frappe.db.rollback(); frappe.destroy()


def race_task(site, sites_path, action, name, started, release, output):
	frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
	try:
		if action == 'send':
			def slow(definition, name, *args, orig):
				started.set(); release.wait(15)
				return orig(definition, name, *args)
			with patch_transport_operation(provider_catalog, 'send', slow):
				value = dispatch.deliver(name, now=now_datetime()+timedelta(hours=2))
		elif action == 'receive':
			doc = frappe.get_doc('Notification Queue', name)
			with patch('frappe.enqueue'):
				value = console.send_message(doc.account, doc.effective_recipient, 'concurrent-'+frappe.generate_hash(length=20), text='Synthetic incoming message')
		elif action == 'open_human':
			from datetime import datetime, timezone
			from messaging.conversation.doctype.chat_turn.chat_turn import process_event
			doc = frappe.get_doc('Notification Queue', name)
			with patch('frappe.enqueue'):
				value = process_event(dict(channel=doc.channel, account=doc.account, sender=doc.effective_recipient,
					event_id='human-open-'+frappe.generate_hash(length=16), event_at=datetime.now(timezone.utc).isoformat(),
					text='Speak to agent', human_requested=True), interpret_natural=False)
		else:
			value = human.act(name, 'Take')
		output.put(dict(value=value))
	except Exception as exc:
		output.put(dict(error=type(exc).__name__, detail=str(exc)))
	finally:
		frappe.db.rollback(); frappe.destroy()


def prepare_controlled_def(*, params, outcomes):
	outcomes.register('send', fields={'recipient': 'string', 'name': 'string', 'amount': 'string'}, mode=['template'])
	outcomes.register('skip', fields={}, mode=['template'])


def prepare_controlled_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
	res = controlled_prepare(request.source.doctype, request.source.name, request.event, '')
	action = (res.get('data') or {}).get('action')
	if action == 'skip':
		return NotifyResponse('skip')
	data = {k: v for k, v in (res.get('data') or {}).items() if k != 'action'}
	return NotifyResponse('send', data=dict(data, recipient=res['recipient']))


def prepare_controlled_text_def(*, params, outcomes):
	outcomes.register('send', fields={'recipient': 'string', 'notice_text': 'string'}, mode=['template'])


def prepare_controlled_text_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
	res = controlled_text(request.source.doctype, request.source.name, request.event, '')
	return NotifyResponse('send', data={'recipient': res['recipient'], 'notice_text': res['data']['text']})


from messaging.registry.processes import BotParams, BotProcess, BotRequest, BotResponse, ProcessOutcomes


def human_open_def(*, params: BotParams, outcomes: ProcessOutcomes) -> None:
	outcomes.register('open', fields={}, mode=['message'], action='complete_flow')


def human_open_handler(request: BotRequest) -> BotResponse:
	return BotResponse('open', message='Synthetic human open')


class TestNotificationRule(unittest.TestCase):
	RULE_PROCESS_IDS = ('messaging.test_rule_controlled', 'messaging.test_rule_text', 'messaging.test_human_open')

	def setUp(self):
		frappe.set_user('Administrator')
		self.states_before = get_provider_states()
		self.rules, self.contacts, self.accounts, self.templates, self.logs = [], [], [], [], []
		_calls.clear()
		from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
		sync_communication_providers()
		from frappe.permissions import add_permission
		add_permission('Emulator Contact', 'Messaging Automation', ptype='read')
		frappe.clear_cache(doctype='Emulator Contact')
		set_provider_enabled('EMULR', True)
		if not frappe.db.exists('Communication Channel', 'emulator'):
			channel = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR', channel_name='Synthetic emulator')).insert()
			set_channel_status(channel.name, 'Enabled')
		from messaging.registry.processes import BotProcess
		register_processes(app='messaging', processes=[
			NotifyProcess(id='messaging.test_rule_controlled', label='Test rule controlled',
				definition=prepare_controlled_def, handler=prepare_controlled_handler),
			NotifyProcess(id='messaging.test_rule_text', label='Test rule text',
				definition=prepare_controlled_text_def, handler=prepare_controlled_text_handler),
			BotProcess(id='messaging.test_human_open', label='Test human open',
				definition=human_open_def, handler=human_open_handler),
		])
		self.account = self.new_account()
		self.destination = self.contact('Synthetic destination')
		frappe.db.commit()
		self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
		self.network.start(); self.addCleanup(self.network.stop)

	def tearDown(self):
		frappe.db.rollback(); frappe.set_user('Administrator')
		if self.rules:
			frappe.db.delete('Notification Queue', {'notification_rule': ['in', self.rules]})
			logs = frappe.get_all('Process Execution Log', filters={'source_name': ['in', self.rules]}, fields=['name', 'frappe_error_log'])
			for log in logs:
				if log.frappe_error_log: frappe.db.delete('Error Log', {'name': log.frappe_error_log})
			frappe.db.delete('Process Execution Log', {'source_name': ['in', self.rules]})
			frappe.db.delete('Notification Rule Event', {'parent': ['in', self.rules]})
			frappe.db.delete('Notification Outcome', {'parent': ['in', self.rules]})
			frappe.db.delete('Notification Rule', {'name': ['in', self.rules]})
		for log_name in self.logs:
			if log_name: frappe.db.delete('Process Execution Log', {'name': log_name})
		if self.rules or self.templates:
			frappe.db.delete('Communication Provider Value', {'parent': ['in', self.rules + self.templates]})
		if self.templates:
			frappe.db.delete('Communication Template Parameter', {'parent': ['in', self.templates]})
			frappe.db.delete('Communication Template', {'name': ['in', self.templates]})
		frappe.db.delete('Process Definition', {'name': ['in', list(self.RULE_PROCESS_IDS)]})
		from messaging.registry import synchronize_registry
		synchronize_registry()
		sessions = frappe.get_all('Chat Session', filters={'account': ['in', self.accounts]}, pluck='name') if self.accounts else []
		chat_contacts = frappe.get_all('Chat Contact', filters={'account': ['in', self.accounts]}, pluck='name') if self.accounts else []
		turns = list(dict.fromkeys(
			(frappe.get_all('Chat Turn', filters={'session': ['in', sessions]}, pluck='name') if sessions else [])
			+ (frappe.get_all('Chat Turn', filters={'contact': ['in', chat_contacts]}, pluck='name') if chat_contacts else [])
		))
		if turns:
			frappe.db.delete('Error Log', {'method': 'Messaging Interpretation Error', 'reference_name': ['in', turns]})
		if self.contacts:
			contact_logs = frappe.get_all('Process Execution Log', filters={'source_document': ['in', self.contacts]}, fields=['name', 'frappe_error_log'])
			for row in contact_logs:
				if row.frappe_error_log: frappe.db.delete('Error Log', {'name': row.frappe_error_log})
			frappe.db.delete('Process Execution Log', {'source_document': ['in', self.contacts]})
			frappe.db.delete('Notification Queue', {'emulator_contact': ['in', self.contacts]})
			frappe.db.delete('Notification Queue', {'source_doctype': 'Emulator Contact', 'source_name': ['in', self.contacts]})
		if sessions:
			frappe.db.delete('Process Execution Log', {'session': ['in', sessions]})
			frappe.db.delete('Chat Message', {'session': ['in', sessions]})
			frappe.db.delete('Chat Turn', {'session': ['in', sessions]})
		if turns:
			frappe.db.delete('Process Execution Log', {'turn': ['in', turns]})
		if chat_contacts:
			frappe.db.delete('Chat Message', {'contact': ['in', chat_contacts]})
			frappe.db.delete('Chat Turn', {'contact': ['in', chat_contacts]})
		if self.accounts:
			for dt in ('Bot Dispatch', 'Chat Session', 'Chat Contact', 'Chat Agent'):
				frappe.db.delete(dt, {'account': ['in', self.accounts]})
		for name in self.contacts:
			if frappe.db.exists('Emulator Contact', name): frappe.delete_doc('Emulator Contact', name)
		for name in self.accounts:
			if frappe.db.exists('Communication Channel Account', name): frappe.delete_doc('Communication Channel Account', name)
		_lock_provider_states(); _write_provider_states(self.states_before)
		invalidate_rules(); frappe.db.commit()

	def enable_human_take(self, account):
		if 'Messaging Agent' not in frappe.get_roles('Administrator'):
			frappe.get_doc('User', 'Administrator').add_roles('Messaging Agent')
		if not frappe.db.exists('Chat Agent', {'channel': 'emulator', 'account': account, 'user': 'Administrator'}):
			frappe.get_doc(dict(doctype='Chat Agent', channel='emulator', account=account, user='Administrator', enabled=1, available=1, capacity=5)).insert(ignore_permissions=True)
		frappe.db.commit()

	def new_account(self, outcome='Accepted'):
		doc = frappe.get_doc(dict(doctype='Communication Channel Account', channel='emulator',
			account_id='notification-test-'+frappe.generate_hash(length=12), account_name='Synthetic notification account',
			enabled=1, parameters=[dict(parameter_key='transport_outcome', value=json.dumps(outcome))])).insert()
		self.accounts.append(doc.name)
		return doc.name

	def new_template(self, *, provider='EMULR', body='Notice {{name}} {{amount}}', variables=None):
		if variables is None:
			import re
			variables = []
			for m in re.findall(r'\{\{([a-z]+(?:_[a-z]+)*)\}\}', body):
				if m not in variables:
					variables.append(m)
		doc = frappe.get_doc(dict(doctype='Communication Template',
			channel=frappe.db.get_value('Communication Channel', {'provider': provider}, 'name'), status='Active',
			template_name='rule-test-' + frappe.generate_hash(length=10), language='es', body=body,
			template_variables=[dict(parameter_name=v) for v in variables])).insert()
		self.templates.append(doc.name)
		return doc

	def rule(self, **values):
		account = values.get('account', self.account)
		provider = values.pop('template_provider', None)
		if not provider and account:
			channel = frappe.db.get_value('Communication Channel Account', account, 'channel')
			provider = frappe.db.get_value('Communication Channel', channel, 'provider')
		process_definition = values.pop('process_definition', 'messaging.test_rule_controlled')
		template_body = values.pop('template_body', '{{notice_text}}' if process_definition == 'messaging.test_rule_text' else 'Notice {{name}} {{amount}}')
		template_ref = values.pop('template_reference', None)
		outputs = values.pop('outputs', None)
		if outputs is None:
			if not template_ref and provider:
				template_ref = self.new_template(provider=provider, body=template_body).name
			outputs = [dict(status_code='send', output_mode='Template', template_reference=template_ref, send=1)]
			if process_definition == 'messaging.test_rule_controlled':
				outputs.append(dict(status_code='skip', output_mode='Template', send=0))
		parameters = values.pop('parameters', [dict(parameter_key='notification_contact', value=json.dumps(self.destination.name))] if account else [])
		data = dict(doctype='Notification Rule', rule_name='Synthetic notification rule', status='Enabled', notification_delay_minutes=60,
			source_application='messaging', source_doctype='Emulator Contact', events=[dict(document_event='after_insert')],
			process_definition=process_definition, process_invocation_mode='Internal Call (frappe.call)',
			account=account, parameters=parameters, outputs=outputs)
		data.update(values)
		doc = frappe.get_doc(dict(data, __islocal=1))
		if data.get('status', 'Enabled') == 'Enabled':
			answer = process_testing.test_method(doc.as_dict(), {'name': self.destination.name})
			self.logs.append(answer.get('execution_log'))
			self.assertEqual(answer['status'], 'Passed', answer)
		doc.insert()
		self.rules.append(doc.name); frappe.db.commit()
		return doc

	def contact(self, mode='Snapshot one', commit=True):
		doc = frappe.get_doc(dict(doctype='Emulator Contact', display_name=mode)).insert()
		self.contacts.append(doc.name)
		if commit: frappe.db.commit()
		return doc

	def queue(self, rule=None):
		return frappe.get_doc('Notification Queue', frappe.db.get_value('Notification Queue', {'notification_rule': (rule or self.rules[-1])}, 'name', order_by='creation desc, name desc'))

	def due(self, doc):
		return dispatch.dispatch_due(names=[doc.name], now=get_datetime(doc.scheduled_at)+timedelta(seconds=1))

	def session(self, sender, account=None):
		from messaging.common.primitives import utcnow
		account = account or self.account
		from messaging.common.primitives import identity_key
		with contact_lock(identity_key('emulator', account, sender)):
			contact = get_contact('emulator', account, sender)
			session = get_session(contact, utcnow())
			frappe.db.commit()
		return session

	def parallel(self, action, name):
		ctx = multiprocessing.get_context('spawn'); barrier = ctx.Barrier(2); output = ctx.Queue()
		children = [ctx.Process(target=parallel_task, args=(frappe.local.site, str(frappe.local.sites_path), action, name, barrier, output)) for _ in range(2)]
		for child in children: child.start()
		for child in children: child.join(35)
		for child in children:
			if child.is_alive(): child.terminate(); child.join(); self.fail('Concurrent worker timed out')
		results = [output.get(timeout=5) for _ in children]
		self.assertFalse(any('error' in row for row in results), results)
		frappe.db.rollback()
		return [row['value'] for row in results]

	def test_rule_validation_permissions_and_native_configuration(self):
		for values in [dict(source_application='missing'), dict(source_application='frappe'),
			dict(events=[dict(document_event='on_submit')]), dict(events=[dict(document_event='on_cancel')]),
			dict(process_definition='messaging.not.authorized'),
			dict(template_reference='../ invalid'), dict(source_doctype='Notification Queue')]:
			with self.subTest(values=values), self.assertRaises((frappe.ValidationError, AssertionError)): self.rule(status='Disabled', **values)
		data = configuration('messaging', 'Emulator Contact', self.account)
		self.assertEqual(data['events'], ['after_insert', 'on_update'])
		self.assertEqual(data['content_types'], ['Template'])
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError): configuration()
		with self.assertRaises(frappe.PermissionError): self.rule()
		with self.assertRaises(frappe.PermissionError): dispatch.dispatch_due(names=[])

	def test_event_without_rule_returns_before_source_queries(self):
		doc = self.contact()
		with patch.object(events, 'rule_index', return_value={}), patch.object(events, 'prepare', side_effect=AssertionError('Unexpected preparation')):
			events.observe(doc, 'after_insert')
		self.assertEqual(_calls, [])

	def test_immediate_preparation_multiple_rules_snapshot_and_no_second_process(self):
		one = self.rule(); two = self.rule()
		source = self.contact()
		self.assertEqual(len(_calls), 2)
		a, b = self.queue(one.name), self.queue(two.name)
		self.assertNotEqual(a.occurrence_key, b.occurrence_key)
		self.assertEqual(get_datetime(a.scheduled_at)-get_datetime(a.event_at), timedelta(minutes=60))
		log = frappe.get_doc('Process Execution Log', a.execution_log)
		self.assertEqual((log.source_type, log.source_name, log.source_document), ('Notification Rule', one.name, source.name))
		self.assertNotIn('L 250.00', log.response_summary_json)
		self.assertNotIn('+50499991234', log.response_summary_json)
		with patch('messaging.communication.doctype.process_definition.execution.execute_process', side_effect=AssertionError('Scheduler called Process')):
			self.assertEqual(dispatch.dispatch_due(names=[a.name], now=get_datetime(a.event_at)), [])
		self.assertEqual(self.due(a)[0]['state'], 'Accepted')
		source.display_name = 'Snapshot later'; source.save(); frappe.db.commit()
		with patch.object(dispatch, 'handoff', side_effect=AssertionError('Stale snapshot sent')):
			self.assertEqual(self.due(b)[0]['state'], 'Cancelled')
		b.reload(); self.assertEqual(b.diagnostic_code, 'business_snapshot_changed')
		self.assertEqual(json.loads(b.variables_json)['name'], 'Snapshot one')
		a.reload(); self.assertEqual(json.loads(a.variables_json)['name'], 'Snapshot one')
		self.assertEqual(len(_calls), 4)
		sessions = frappe.get_all('Chat Session', filters={'account': self.account}, pluck='name')
		self.assertEqual(frappe.db.count('Chat Turn', {'session': ['in', sessions]}) if sessions else 0, 0)
		self.assertEqual(frappe.db.count('Bot Dispatch', {'account': self.account}), 0)
		self.assertEqual(frappe.db.count('Chat Contact', {'account': self.account}), 0)
		self.assertEqual(console.history(self.account, self.destination.sender_id)['notifications'][0].name, a.name)

	def test_skip_error_invalid_timeout_are_logged_without_ready_queue(self):
		rule = self.rule()
		for mode, outcome in [('skip', 'Functional Status'), ('exception', 'Internal Execution Error'), ('invalid', 'Invalid Schema Response'), ('timeout', 'Internal Execution Error')]:
			timeout_ctx = patch('messaging.communication.doctype.process_definition.execution.PROCESS_TIMEOUT_SECONDS', 0.05) if mode == 'timeout' else nullcontext()
			with self.subTest(mode=mode), timeout_ctx:
				source = self.contact(mode)
				log = frappe.get_doc('Process Execution Log', {'source_name': rule.name, 'source_document': source.name})
				self.assertEqual(log.execution_outcome, outcome)
				self.assertNotIn('private-diagnostic-token', log.diagnostic_detail or '')
				count = len(_calls); source.run_method('after_insert'); frappe.db.commit()
				self.assertEqual(len(_calls), count)
		self.assertEqual(frappe.db.count('Notification Queue', {'notification_rule': rule.name}), 0)

	def test_rollback_and_failed_queue_insert_never_leave_confirmed_notifications(self):
		rule = self.rule()
		source = self.contact(commit=False)
		self.assertEqual(len(_calls), 1); frappe.db.rollback()
		self.assertFalse(frappe.db.exists('Notification Queue', {'notification_rule': rule.name}))
		self.assertFalse(frappe.db.exists('Process Execution Log', {'source_name': rule.name}))
		self.contacts.remove(source.name)
		with patch('messaging.communication.doctype.notification_queue.notification_queue.NotificationQueue.validate', side_effect=frappe.ValidationError('Synthetic queue failure')):
			with self.assertRaises(frappe.ValidationError): self.contact(commit=False)
		frappe.db.rollback()
		self.assertFalse(frappe.db.exists('Notification Queue', {'notification_rule': rule.name}))

	def test_unfiltered_update_is_once_per_document_including_creation(self):
		rule = self.rule(events=[dict(document_event='on_update')])
		source = self.contact('Occurrence A'); self.assertEqual(len(_calls), 1)
		source.save(); frappe.db.commit(); self.assertEqual(len(_calls), 1)
		source.save(); frappe.db.commit(); self.assertEqual(len(_calls), 1)
		source.display_name = 'Occurrence B'; source.save(); frappe.db.commit()
		self.assertEqual(len(_calls), 1)
		self.assertEqual(frappe.db.count('Notification Queue', {'notification_rule': rule.name}), 1)

	def test_snapshot_and_receipt_immutability_and_persistent_uniqueness(self):
		self.rule(); self.contact(); doc = self.queue()
		doc.recipient = 'changed'
		with self.assertRaises(frappe.PermissionError): doc.save(ignore_permissions=True)
		doc.reload()
		with internal_write():
			doc.prepared_text = 'changed'
			with self.assertRaises(frappe.ValidationError): doc.save(ignore_permissions=True)
		frappe.db.rollback(); doc.reload()
		with internal_write(), self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			copy = frappe.copy_doc(doc); copy.insert(ignore_permissions=True)
		frappe.db.rollback()
		with self.assertRaises(frappe.PermissionError): frappe.delete_doc('Process Execution Log', doc.execution_log)
		log = frappe.get_doc('Process Execution Log', doc.execution_log)
		log.notification_key = None
		with self.assertRaises(frappe.PermissionError): log.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError): frappe.delete_doc('Notification Queue', doc.name)

	def test_concurrent_preparation_and_claim_use_real_independent_connections(self):
		source = self.contact(); self.rule()
		self.assertEqual(sorted(self.parallel('event', source.name)), [0, 1])
		doc = self.queue()
		self.assertEqual(self.parallel('send', doc.name), ['Accepted', 'Accepted'])
		doc.reload(); self.assertEqual(doc.attempt_count, 1)

	def test_outcomes_reconciliation_delivery_and_cross_account_rejection(self):
		for outcome in ('Accepted', 'Rejected', 'Unknown'):
			account = frappe.get_doc('Communication Channel Account', self.account)
			account.parameters[0].value = json.dumps(outcome); account.save(); frappe.db.commit()
			rule = self.rule(); source = self.contact(outcome); doc = self.queue(rule.name)
			self.assertEqual(self.due(doc)[0]['state'], outcome)
			with patch.object(dispatch, 'handoff', side_effect=AssertionError('Unsafe resend')):
				dispatch.deliver(doc.name)
			if outcome == 'Unknown':
				self.assertEqual(console.simulate_notification(self.account, self.destination.sender_id, doc.name, status='Failed')['state'], 'Unknown')
				self.assertEqual(console.simulate_notification(self.account, self.destination.sender_id, doc.name, status='Sent')['state'], 'Accepted')
				for state in ('Delivered', 'Failed'):
					console.simulate_notification(self.account, self.destination.sender_id, doc.name, status=state)
				doc.reload(); self.assertEqual(doc.delivery_status, 'Delivered')
				other = self.new_account(); frappe.db.commit()
				with self.assertRaises(frappe.PermissionError): adapter_call(transport.report_result, 'EMULR', other, doc.name, {'outcome':'Accepted','reference':doc.transport_reference}, source_type='Notification Queue')
				with self.assertRaises(frappe.ValidationError): adapter_call(transport.report_status, 'EMULR', self.account, doc.name, 'Read', 'wrong-ref', source_type='Notification Queue')
			if outcome == 'Rejected':
				with self.assertRaises(frappe.ValidationError): adapter_call(transport.report_result, 'EMULR', self.account, doc.name, {'outcome':'Accepted','reference':'fake'}, source_type='Notification Queue')

	def test_uncertain_handoff_and_recovery_before_and_after_send(self):
		self.rule(); self.contact(); doc = self.queue()
		def fail_send(definition, name, *args, orig):
			raise TimeoutError('sensitive')
		with patch_transport_operation(provider_catalog, 'send', fail_send):
			self.assertEqual(self.due(doc)[0]['state'], 'Unknown')
		for after in (False, True):
			self.contact(); doc = self.queue()
			doc.status = 'Processing'; doc.claim_id = 'interrupted'; doc.last_attempt_at = now_datetime()-timedelta(minutes=10)
			if after: doc.handoff_at = doc.last_attempt_at
			with internal_write(): doc.save(ignore_permissions=True)
			frappe.db.commit(); dispatch.recover_claims(now_datetime()); doc.reload()
			self.assertEqual(doc.status, 'Unknown' if after else 'Deferred')

	def test_operational_deferral_and_rehabilitation_keep_snapshot(self):
		self.rule(); self.contact(); doc = self.queue(); original = doc.variables_json
		for target in ('provider', 'channel', 'account', 'rule'):
			if target == 'provider': set_provider_enabled('EMULR', False)
			elif target == 'channel': set_channel_status('emulator', 'Disabled')
			else:
				dt, name = {'account':('Communication Channel Account',self.account), 'rule':('Notification Rule',self.rules[0])}[target]
				item = frappe.get_doc(dt, name); item.enabled = 0; item.save()
			frappe.db.commit()
			result = dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(days=1))
			self.assertEqual(result['state'], 'Deferred')
			if target == 'provider': set_provider_enabled('EMULR', True)
			elif target == 'channel': set_channel_status('emulator', 'Enabled')
			else: item.reload(); item.enabled = 1; item.save()
			frappe.db.commit(); doc.reload()
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(days=2))['state'], 'Accepted')
		doc.reload(); self.assertEqual(doc.variables_json, original)

	def test_human_open_paused_closed_and_same_sender_in_another_account(self):
		self.rule(); source = self.contact(); doc = self.queue(); session = self.session(self.destination.sender_id)
		self.enable_human_take(self.account)
		# Use the real administrative taking and closing policy; no synthetic turns.
		session.status = 'Human Queued'; session.save(ignore_permissions=True); frappe.db.commit()
		self.assertIn(human.act(session.name, 'Take')['status'], human.HUMAN_OPEN)
		self.assertEqual(self.due(doc)[0]['state'], 'Deferred')
		session.reload(); session.status = 'Human Paused'; session.save(ignore_permissions=True); frappe.db.commit()
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=5))['state'], 'Deferred')
		other = self.new_account(); self.rule(account=other)
		source.run_method('after_insert'); frappe.db.commit(); other_doc = self.queue()
		self.assertEqual(self.due(other_doc)[0]['state'], 'Accepted')
		human.act(session.name, 'Close')
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=10))['state'], 'Accepted')
		self.assertEqual(len(_calls), 4)

	def test_human_opening_waits_until_effective_handoff_finishes(self):
		from messaging.chatbot.doctype.bot_flow.bot_flow import set_active as set_flow_active, validate_process
		from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
		self.rule(); source = self.contact(); doc = self.queue(); session = self.session(self.destination.sender_id)
		self.enable_human_take(self.account)
		# Human Queued blocks notifications before any handoff attempt.
		session.status = 'Human Queued'; session.save(ignore_permissions=True); frappe.db.commit()
		with patch.object(dispatch, 'handoff', side_effect=AssertionError('Blocked human session sent')):
			self.assertEqual(self.due(doc)[0]['state'], 'Deferred')
		doc.reload(); self.assertEqual(doc.diagnostic_code, 'human_attention'); self.assertFalse(doc.handoff_at)
		# Closing unblocks delivery; a concurrent Human opening must wait until handoff finishes.
		human.act(session.name, 'Close')
		settings = frappe.get_single('Bot Settings')
		enabled_languages = [r.language for r in settings.chatbot_languages if r.enabled] or ['es']
		orig_human_settings = settings.as_dict()
		orig_enabled_flows = frappe.get_all('Bot Flow', filters={'status': 'Enabled'}, pluck='name')
		flow_code = 'synthetic_human_open_' + frappe.generate_hash(length=8)
		human_flow = frappe.get_doc(dict(
			doctype='Bot Flow', workflow_code=flow_code, workflow_name=flow_code,
			semantic_description='Synthetic human opening race flow', status='Disabled', priority=0,
			show_in_menu=0, process_definition='messaging.test_human_open',
			translations=[dict(language=lang, semantic_description='Synthetic human opening race flow') for lang in enabled_languages],
			outputs=[dict(status_code='open', output_mode='Message', flow_action='Complete Flow')],
		)).insert()
		approval = validate_process(human_flow.as_dict())
		self.assertEqual(approval['status'], 'Passed', approval)
		if approval.get('execution_log'):
			self.logs.append(approval['execution_log'])
		human_flow.reload()
		set_flow_active(human_flow.as_dict(), 1)
		settings = frappe.get_single('Bot Settings')
		settings.registration_enabled = 0
		settings.greeting_enabled = 0
		settings.interpretation_enabled = 0
		settings.human_attention_enabled = 1
		settings.human_flow = human_flow.name
		settings.human_requests_required = 1
		settings.human_inactivity_minutes = 15
		settings.set('human_transfer_states', [dict(state=state_name(human_flow.name, 'open'))])
		settings.set('human_support_week', [
			dict(weekday=day, enabled=1, start_time='00:00:00', end_time='23:59:59')
			for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
		])
		settings.flags.ignore_mandatory = True
		settings.save(ignore_permissions=True, ignore_version=True)
		frappe.db.commit()
		ctx = multiprocessing.get_context('spawn'); started = ctx.Event(); release = ctx.Event(); output = ctx.Queue()
		sender = ctx.Process(target=race_task, args=(frappe.local.site, str(frappe.local.sites_path), 'send', doc.name, started, release, output))
		opener = ctx.Process(target=race_task, args=(frappe.local.site, str(frappe.local.sites_path), 'open_human', doc.name, started, release, output))
		try:
			sender.start(); self.assertTrue(started.wait(20)); opener.start(); time.sleep(0.4)
			self.assertTrue(opener.is_alive()); self.assertTrue(output.empty())
			release.set(); sender.join(25); opener.join(25)
			values = [output.get(timeout=3) for _ in range(2)]
			self.assertFalse(any('error' in v for v in values), values)
			frappe.db.rollback(); doc.reload()
			self.assertEqual(doc.status, 'Accepted')
			self.assertEqual(doc.attempt_count, 2)
			opened_session = frappe.db.get_value('Chat Session', {'account': self.account, 'status': ['in', human.HUMAN_OPEN]}, 'status')
			self.assertEqual(opened_session, 'Human Queued')
		finally:
			release.set()
			for child in (sender, opener):
				if child.pid and child.is_alive(): child.terminate(); child.join()
			frappe.db.rollback()
			from messaging.registry import synchronize_registry
			settings = frappe.get_single('Bot Settings')
			settings.registration_enabled = 0
			settings.greeting_enabled = 0
			settings.interpretation_enabled = 0
			settings.human_attention_enabled = 0
			settings.human_flow = None
			settings.human_requests_required = orig_human_settings['human_requests_required']
			settings.human_inactivity_minutes = orig_human_settings['human_inactivity_minutes']
			settings.set('human_transfer_states', [])
			settings.set('human_support_week', [])
			settings.flags.ignore_mandatory = True
			settings.save(ignore_permissions=True, ignore_version=True)
			frappe.db.delete('Bot Dispatch', {'account': self.account})
			frappe.db.delete('Process Execution Log', {'workflow': human_flow.name})
			frappe.db.delete('Bot Flow State', {'bot_flow': human_flow.name})
			if frappe.db.exists('Bot Flow', human_flow.name):
				human_flow.reload()
				set_flow_active(human_flow.as_dict(), 0)
				frappe.delete_doc('Bot Flow', human_flow.name, ignore_permissions=True)
			synchronize_registry()
			frappe.db.commit()
			from messaging.chatbot.doctype.bot_settings.test_bot_settings import restore_bot_settings
			restore_bot_settings(orig_human_settings, enabled_flows=orig_enabled_flows)

	def test_text_notifications_and_invalid_destination_are_definitive(self):
		self.rule(process_definition='messaging.test_rule_text')
		source = self.contact(); doc = self.queue()
		self.assertEqual(self.due(doc)[0]['state'], 'Accepted')
		self.assertEqual(console.history(self.account, self.destination.sender_id)['notifications'][0].prepared_text, 'Prepared Snapshot one')
		self.contact('Disabled destination'); doc=self.queue(); other = self.destination; other.enabled=0; other.save(); frappe.db.commit()
		with patch.object(dispatch, 'handoff', side_effect=AssertionError('Invalid destination sent')):
			self.assertEqual(self.due(doc)[0]['state'], 'Cancelled')
		doc.reload(); self.assertEqual(doc.diagnostic_code, 'recipient_configuration_invalid'); self.assertFalse(doc.handoff_at)
		other.reload(); other.enabled=1; other.save(); frappe.db.commit()
		self.contact('Provider rejected destination'); rejected = self.queue()
		with patch.object(dispatch, 'handoff', side_effect=AssertionError('Provider validation failure sent')), \
				patch_transport_operation(dispatch, 'validate_notification', lambda *a, **k: False):
			self.assertEqual(self.due(rejected)[0]['state'], 'Rejected')
		rejected.reload(); self.assertEqual(rejected.diagnostic_code, 'destination_or_content_invalid'); self.assertFalse(rejected.handoff_at)

	def test_exact_lifecycle_events_and_bounded_scheduler_batch(self):
		# Exercise native Frappe submit/cancel hooks with transient metadata only.
		meta = frappe.get_meta('Emulator Contact')
		with patch.object(meta, 'is_submittable', 1):
			update = self.rule(events=[dict(document_event='on_update')])
			submit = self.rule(events=[dict(document_event='on_submit')])
			cancel = self.rule(events=[dict(document_event='on_cancel')])
			source = self.contact('Lifecycle token')
			source.save(); source.submit(); source.cancel(); frappe.db.commit()
			self.assertEqual(len(_calls), 3)
			for rule in (update, submit, cancel):
				self.assertEqual(frappe.db.count('Notification Queue', {'notification_rule': rule.name}), 1)
			names = frappe.get_all('Notification Queue', filters={'notification_rule':['in', self.rules]}, pluck='name')
			with patch.object(dispatch, 'BATCH_SIZE', 2):
				self.assertEqual(len(dispatch.dispatch_due(names=names, now=now_datetime()+timedelta(hours=2))), 2)
			self.assertEqual(len(dispatch.dispatch_due(names=names, now=now_datetime()+timedelta(hours=2))), 1)

	def test_revoked_process_and_missing_adapter_or_parameters_are_safe(self):
		self.rule(); source = self.contact()
		rule = frappe.get_doc('Notification Rule', self.rules[0])
		valid_fingerprint = rule.process_test_fingerprint
		frappe.db.set_value('Notification Rule', rule.name, 'process_test_fingerprint', 'obsolete-approval', update_modified=False); frappe.db.commit()
		other = self.contact('Revoked preparation')
		self.assertEqual(len(_calls), 1)
		self.assertEqual(frappe.db.count('Notification Queue', {'source_name': other.name}), 0)
		frappe.db.set_value('Notification Rule', rule.name, 'process_test_fingerprint', valid_fingerprint, update_modified=False); frappe.db.commit()
		doc = self.queue()
		with patch('messaging.communication.doctype.communication_channel_account.binding.validate_values', side_effect=frappe.ValidationError('Incomplete required parameters')):
			self.assertEqual(self.due(doc)[0]['state'], 'Deferred')
		with patch('messaging.communication.doctype.communication_channel_account.binding.is_available', return_value=False):
			self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(days=1))['state'], 'Deferred')
		with patch('messaging.communication.doctype.communication_channel_account.binding.provider_definition', side_effect=frappe.ValidationError('Provider absent')):
			self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(days=2))['state'], 'Deferred')
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(days=3))['state'], 'Accepted')

	def test_notification_validation_and_forged_receipts_are_rejected(self):
		from messaging.communication.doctype.process_definition.validation import ProcessSchemaValidationError
		rule = self.rule()
		non_redirect = {**provider_catalog.provider_definition('EMULR'), 'capabilities': ['text', 'templates', 'delivery_status']}
		with patch('messaging.communication.doctype.notification_rule.contract.provider_definition', return_value=non_redirect):
			with self.assertRaises(ProcessSchemaValidationError):
				events.prepared_data({'version':1, 'status':'send', 'recipient':'', 'data':{'name':'valid', 'amount':'L 1'}, 'message':''}, rule)
		for recipient, data in [(' valid', {'name':'valid', 'amount':'L 1'}),
			('valid', {'name':'valid', 'variables':{}}), ('valid', {'name':'valid', 'value':10**400}),
			('valid', {'name':'valid', 'value':float('inf')})]:
			with self.assertRaises(ProcessSchemaValidationError): events.prepared_data({'version':1, 'status':'send', 'recipient':recipient, 'data':data, 'message':''}, rule)

		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(dict(doctype='Process Execution Log', notification_key='forged', process_method=rule.process_method,
				execution_outcome='Running',execution_context='Notification Preparation')).insert()
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(dict(doctype='Notification Queue',status='Pending')).insert(ignore_permissions=True)

	def test_neutral_notifications_isolate_two_channels_and_report_authorization(self):
		from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers
		from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
		from messaging.communication.doctype.communication_provider.communication_provider import catalog_sync
		from messaging.presets.communication_providers.emulr import definition as emulator_definition
		native = [emulator_definition()]
		probe = dict(id='NOTPRO', name='Synthetic Notification Provider', version='1.0', app='messaging',
			capabilities=['text', 'templates', 'delivery_status'], operations={
				'resolve_account': __name__+'.probe_account', 'send': __name__+'.probe_send',
				'validate_template': __name__+'.probe_validate_template',
				'validate_notification': __name__+'.probe_content'})
		bindings, channels_created = [], []
		with registered_providers(native+[probe]):
			try:
				sync_communication_providers(); set_provider_enabled('NOTPRO', True)
				for index in range(2):
					channel=frappe.get_doc(dict(doctype='Communication Channel', provider='NOTPRO', channel_name='Synthetic notification channel')).insert()
					set_channel_status(channel.name, 'Enabled')
					channels_created.append(channel.name)
					binding=frappe.get_doc(dict(doctype='Communication Channel Account', channel=channel.name, account_id='synthetic-opaque-id-'+str(index), account_name='Synthetic channel binding', enabled=1)).insert()
					bindings.append(binding.name); self.accounts.append(binding.name)
					rule = self.rule(account=binding.name, parameters=[])
					self.assertFalse(rule.parameters)
				source=self.contact()
				first, second=[self.queue(n) for n in self.rules]
				self.assertNotEqual(first.identity_key, second.identity_key)
				self.assertNotEqual(first.channel, second.channel)
				self.assertNotEqual(first.account_id, second.account_id)
				for doc in (first, second): self.assertEqual(self.due(doc)[0]['state'], 'Accepted')
				with self.assertRaises(frappe.PermissionError):
					adapter_call(transport.report_status, 'NOTPRO', bindings[1], first.name, 'Read', 'probe-'+first.occurrence_key, source_type='Notification Queue')
			finally:
				frappe.db.rollback()
				if self.rules:
					frappe.db.delete('Notification Queue', {'notification_rule':['in',self.rules]})
					frappe.db.delete('Process Execution Log', {'source_name':['in',self.rules]})
					frappe.db.delete('Notification Rule Event', {'parent':['in',self.rules]})
					frappe.db.delete('Notification Outcome', {'parent':['in',self.rules]})
					frappe.db.delete('Notification Rule', {'name':['in',self.rules]})
				if self.rules or self.templates:
					frappe.db.delete('Communication Provider Value', {'parent':['in',self.rules + self.templates]})
				if self.templates:
					frappe.db.delete('Communication Template Parameter', {'parent':['in',self.templates]})
					frappe.db.delete('Communication Template', {'name':['in',self.templates]})
				for name in bindings:
					if frappe.db.exists('Communication Channel Account', name):
						frappe.delete_doc('Communication Channel Account', name)
					if name in self.accounts:
						self.accounts.remove(name)
				for name in channels_created:
					if frappe.db.exists('Communication Channel', name):
						frappe.delete_doc('Communication Channel', name)
				_lock_provider_states(); _write_provider_states(self.states_before)
				if frappe.db.exists('Communication Provider', 'NOTPRO'):
					with catalog_sync(): frappe.delete_doc('Communication Provider','NOTPRO')
				invalidate_rules(); frappe.db.commit()

	def test_emulator_notification_cursor_is_bounded_and_scoped(self):
		rule = self.rule(events=[dict(document_event='on_update')])
		for index in range(61):
			self.contact('Cursor document '+str(index))
		frappe.db.commit()
		names = frappe.get_all('Notification Queue', filters={'notification_rule': rule.name}, pluck='name')
		self.assertEqual(len(names), 61)
		for expected in (50, 11):
			self.assertEqual(len(dispatch.dispatch_due(names=names, now=now_datetime()+timedelta(hours=2))), expected)
		first = console.history(self.account, self.destination.sender_id)
		self.assertEqual(len(first['notifications']), 60)
		self.assertTrue(first['more_notifications'])
		second = console.history(self.account, self.destination.sender_id, notification_before=first['notifications'][0].name)
		self.assertEqual(len(second['notifications']), 1)
		self.assertFalse(second['more_notifications'])
		self.assertNotIn(second['notifications'][0].name, {row.name for row in first['notifications']})
		other = self.new_account(); frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			console.history(other, self.destination.sender_id, notification_before=first['notifications'][0].name)

	def test_new_delay_applies_only_to_new_snapshots_and_disabled_rules_ignore_events(self):
		rule = self.rule(); self.contact('Original delay'); first = self.queue()
		original_schedule = get_datetime(first.scheduled_at)
		rule.notification_delay_minutes = 5
		with self.assertRaises(frappe.ValidationError):
			rule.save()
		rule.reload()
		rule.status = 'Disabled'; rule.notification_delay_minutes = 5; rule.save()
		answer = process_testing.test_method(rule.as_dict())
		self.logs.append(answer['execution_log']); self.assertEqual(answer['status'], 'Passed')
		rule.status = 'Enabled'; rule.save()
		frappe.db.commit(); self.contact('New delay'); second = self.queue(rule.name)
		self.assertEqual(get_datetime(second.scheduled_at)-get_datetime(second.event_at), timedelta(minutes=5))
		first.reload(); self.assertEqual(get_datetime(first.scheduled_at), original_schedule)
		rule.status = 'Disabled'; rule.save(); frappe.db.commit(); ignored = self.contact('Disabled rule')
		self.assertEqual(len(_calls), 2)
		self.assertFalse(frappe.db.exists('Process Execution Log', {'source_name': rule.name, 'source_document': ignored.name}))

	def test_missing_account_cancels_without_transport_and_preserves_audit_snapshot(self):
		self.rule(); self.contact(); doc = self.queue()
		# Deliberately remove only this synthetic fixture to exercise a broken reference.
		# Ordinary account deletion is already prevented by native queue links.
		frappe.db.delete('Communication Provider Value', {'parent': self.account, 'parenttype': 'Communication Channel Account'})
		frappe.db.delete('Communication Channel Account', {'name': self.account})
		self.accounts.remove(self.account); frappe.db.commit()
		with patch('messaging.emulator.provider.send', side_effect=AssertionError('Missing account was sent')):
			self.assertEqual(self.due(doc)[0]['state'], 'Cancelled')
		doc.reload(); self.assertEqual(doc.diagnostic_code, 'account_identity_invalid')
		self.assertFalse(doc.handoff_at)
		self.assertEqual(doc.account, self.account)

	def test_emulated_contact_validation_and_original_effective_snapshot(self):
		for selected in (None, 'missing-emulated-contact'):
			with self.assertRaises(frappe.ValidationError):
				self.rule(parameters=[dict(parameter_key='notification_contact', value=json.dumps(selected))])
		disabled = self.contact('Disabled selection'); disabled.enabled = 0; disabled.save(); frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			self.rule(parameters=[dict(parameter_key='notification_contact', value=json.dumps(disabled.name))])
		rule = self.rule(status='Disabled', account=None, parameters=[], template_provider='EMULR')
		self.assertFalse(rule.parameters)
		rule.account = self.account
		rule.set('parameters', [dict(parameter_key='notification_contact', value=json.dumps(self.destination.name))])
		rule.save()
		answer = process_testing.test_method(rule.as_dict(), {'name': self.destination.name})
		self.logs.append(answer.get('execution_log'))
		self.assertEqual(answer['status'], 'Passed', answer)
		rule.status = 'Enabled'; rule.save(); frappe.db.commit()
		self.contact(); doc = self.queue()
		self.assertEqual(doc.recipient, '+50499991234')
		self.assertEqual(doc.effective_recipient, self.destination.sender_id)
		self.assertEqual(doc.emulator_contact, self.destination.name)
		original = (doc.account, doc.channel, doc.provider, doc.effective_recipient, doc.scheduled_at)
		other = self.contact('Another destination'); rule.account = self.new_account()
		rule.parameters[0].value = json.dumps(other.name)
		with self.assertRaises(frappe.ValidationError):
			rule.save()
		frappe.db.commit()
		self.assertEqual(self.due(doc)[0]['state'], 'Accepted')
		doc.reload(); self.assertEqual((doc.account, doc.channel, doc.provider, doc.effective_recipient, doc.scheduled_at), original)
		self.assertEqual(len(console.history(self.account, self.destination.sender_id)['notifications']), 1)
		self.assertEqual(console.history(rule.account, self.destination.sender_id)['notifications'], [])
		self.assertEqual(frappe.db.count('Chat Contact', {'account':self.account}), 0)

	def test_chatbot_episode_retention_expiry_and_processing(self):
		from messaging.common.primitives import utcnow
		self.rule(); self.contact(); doc = self.queue(); session = self.session(self.destination.sender_id)
		original = (doc.variables_json, doc.scheduled_at, doc.event_at, doc.occurrence_key)
		# Compare a due notification against a genuinely live episode, not a future clock past its TTL.
		session.expires_at = utcnow()+timedelta(hours=3); session.save(ignore_permissions=True); frappe.db.commit()
		with patch.object(dispatch, 'handoff', side_effect=AssertionError('Busy contact sent')):
			self.assertEqual(self.due(doc)[0]['state'], 'Deferred')
		doc.reload(); self.assertEqual(doc.diagnostic_code, 'chatbot_episode'); self.assertFalse(doc.handoff_at)
		self.assertEqual(console.history(self.account, self.destination.sender_id)['notifications'], [])
		other = self.new_account(); self.rule(account=other); self.contact('Other account')
		self.assertEqual(self.due(self.queue())[0]['state'], 'Accepted')
		for status in ('Executing', 'Recovery Required'):
			session.reload(); session.status = status; session.expires_at = utcnow()-timedelta(minutes=1)
			session.save(ignore_permissions=True); frappe.db.commit()
			self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=5))['state'], 'Deferred')
			doc.reload(); self.assertEqual(doc.diagnostic_code, 'chatbot_processing')
		session.reload(); session.status = 'Capturing'
		session.dialogue_state = json.dumps({'queue': [], 'registration_pending': True})
		session.save(ignore_permissions=True); frappe.db.commit()
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=8))['state'], 'Deferred')
		doc.reload(); self.assertEqual(doc.diagnostic_code, 'chatbot_episode')
		session.reload(); session.status = 'Active'; session.dialogue_state = json.dumps({'queue': []})
		session.save(ignore_permissions=True); frappe.db.commit()
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=10))['state'], 'Accepted')
		doc.reload(); self.assertEqual((doc.variables_json, doc.scheduled_at, doc.event_at, doc.occurrence_key), original)
		self.assertEqual(len(_calls), 5)

	def test_all_human_states_hold_same_snapshot_until_close(self):
		self.rule(); self.contact(); doc = self.queue(); session = self.session(self.destination.sender_id)
		for index, status in enumerate(human.HUMAN_STATES):
			session.reload(); session.status = status; session.save(ignore_permissions=True); frappe.db.commit()
			self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=index*2+1))['state'], 'Deferred')
			doc.reload(); self.assertEqual(doc.diagnostic_code, 'human_attention'); self.assertFalse(doc.handoff_at)
		session.reload(); session.status = 'Closed'; session.save(ignore_permissions=True); frappe.db.commit()
		self.assertEqual(dispatch.deliver(doc.name, now=get_datetime(doc.scheduled_at)+timedelta(minutes=15))['state'], 'Accepted')
		self.assertEqual(len(_calls), 2)

	def test_real_reception_waits_for_notification_handoff(self):
		from messaging.conversation.doctype.chat_turn import chat_turn
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		self.rule(); self.contact(); doc = self.queue()
		ctx = multiprocessing.get_context('spawn'); started = ctx.Event(); release = ctx.Event(); output = ctx.Queue()
		sender = ctx.Process(target=race_task, args=(frappe.local.site, str(frappe.local.sites_path), 'send', doc.name, started, release, output))
		receiver = ctx.Process(target=race_task, args=(frappe.local.site, str(frappe.local.sites_path), 'receive', doc.name, started, release, output))
		try:
			sender.start(); self.assertTrue(started.wait(20))
			# Durable reception succeeds without blocking on slow notification handoff.
			receiver.start(); receiver.join(25)
			received = output.get(timeout=3)
			self.assertNotIn('error', received, received)
			turn_info = received['value']
			self.assertEqual(turn_info['state'], 'Pending')
			self.assertIsNone(turn_info['session'])
			# Inbox draining respects the contact connection lock while handoff is in flight.
			frappe.db.rollback()
			with patch('frappe.enqueue'):
				self.assertEqual(drain_contact(turn_info['contact']), {'busy': True})
			turn = frappe.get_doc('Chat Turn', turn_info['turn'])
			self.assertEqual((turn.status, turn.inbox_state, turn.session), ('Pending', 'Pending', None))
			release.set(); sender.join(25)
			sent = output.get(timeout=3)
			self.assertNotIn('error', sent, sent)
			frappe.db.rollback(); doc.reload()
			self.assertEqual((doc.status, doc.attempt_count), ('Accepted', 1))
			# Once handoff releases the lock, draining assigns a session and dispatches the reply.
			settings = frappe.get_single('Bot Settings')
			settings.update(dict(
				interpretation_enabled=0, registration_enabled=0, greeting_enabled=0,
				human_attention_enabled=0, informative_show_available_categories=0, max_invalid_attempts=3,
			))
			with patch('frappe.enqueue'), \
					patch.object(chat_turn, 'get_bot_settings', return_value=settings), \
					patch.object(chat_turn, 'mechanical_menu_flows',
						return_value=[dict(workflow_code='synthetic_menu', menu_label='Menu', menu_description='Menu')]):
				drain_contact(turn_info['contact'])
			turn.reload()
			self.assertEqual((turn.status, turn.inbox_state), ('Complete', 'Done'))
			self.assertTrue(turn.session)
			self.assertTrue(turn.dispatch)
			history = console.history(self.account, self.destination.sender_id)
			self.assertEqual(len(history['messages']), 2)
			self.assertEqual(len(history['notifications']), 1)
		finally:
			release.set()
			for child in (sender, receiver):
				if child.pid and child.is_alive(): child.terminate(); child.join()

	def test_contact_activity_requires_captured_notification_and_exact_account(self):
		rule = self.rule(process_definition='messaging.test_rule_text')
		self.contact('Visible notification')
		queue = self.queue(rule.name)
		before = console.contacts(self.account)
		self.assertTrue(all(not row['last_activity'] for row in before))
		self.assertEqual(self.queue(rule.name).status, 'Pending')
		from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
		set_provider_enabled('EMULR', False)
		frappe.db.commit()
		self.due(queue)
		self.assertEqual(self.queue(rule.name).status, 'Deferred')
		self.assertEqual(console.contacts(self.account), before)
		set_provider_enabled('EMULR', True)
		frappe.db.commit()
		dispatch.deliver(queue.name, now=get_datetime(queue.scheduled_at)+timedelta(hours=2))
		rows = console.contacts(self.account)
		self.assertEqual((rows[0]['sender_id'], rows[0]['activity_kind']), (self.destination.sender_id, 'Notification'))
		self.assertEqual(rows[0]['preview'], 'Prepared Visible notification')
		self.assertEqual(rows[0]['last_activity'], console.history(self.account, self.destination.sender_id)['notifications'][0]['timeline_at'])
		self.assertFalse(frappe.db.count('Chat Session', {'account': self.account}))
		other = self.new_account()
		self.assertTrue(all(not row['last_activity'] for row in console.contacts(other)))


	def test_emulator_notification_client_receipts_without_conversation_artifacts(self):
		rule = self.rule(); self.contact(); doc = self.queue(rule.name); self.due(doc); doc.reload()
		self.assertEqual((doc.status, doc.delivery_status), ('Accepted', 'Sent'))
		counts = {dt: frappe.db.count(dt) for dt in ('Chat Contact', 'Chat Session', 'Chat Turn', 'Chat Message', 'Bot Dispatch')}
		row = next(n for n in console.history(self.account, self.destination.sender_id)['notifications'] if n.name == doc.name)
		self.assertEqual(frappe.db.get_value('Notification Queue', doc.name, 'delivery_status'), 'Sent')
		args = dict(account=self.account, sender=self.destination.sender_id, source_type='Notification Queue', name=doc.name, receipt=row.client_receipt)
		console.client_status(**args, status='Delivered')
		issued = time.time()
		with patch('messaging.emulator.provider.time.time', return_value=issued) as clock:
			proof = console.observe_message(**args)['receipt']
			with self.assertRaises(frappe.PermissionError):
				console.client_status(**dict(args, receipt=proof), status='Read')
			clock.return_value = issued + 1
			console.client_status(**dict(args, receipt=proof), status='Read')
			doc.reload(); read_at = doc.read_at
			console.client_status(**dict(args, receipt=proof), status='Read'); doc.reload()
		self.assertEqual((doc.delivery_status, doc.read_at), ('Read', read_at))
		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts)
		with self.assertRaises(frappe.PermissionError): console.simulate_notification(self.account, self.destination.sender_id, doc.name, status='Read')

"""Scalar condition validation, transactional dedupe and channel template reuse."""
import json
import unittest
from unittest.mock import patch

import frappe
from messaging.communication.doctype.process_definition import validation as testing
from messaging.communication.doctype.notification_rule import conditions as c, events
from messaging.communication.doctype.notification_rule.notification_rule import validate_rule, configuration
from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures
from messaging.registry.processes import register_processes

from messaging.registry.processes import NotifyNoResponse, NotifyProcess, NotifyRequest, NotifyResponse

def prepare_condition(*, params, outcomes):
    outcomes.register('send', fields={'recipient': 'string'}, mode=['message', 'template'])
    outcomes.register('skip', fields={}, mode=['message', 'template'])


def condition_result(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    display_name = request.source.get_document().get('display_name')
    if display_name == 'Skip':
        return NotifyResponse('skip')
    if display_name == 'Invalid output':
        return NotifyResponse('send', data={'recipient': 123}, message='Technical notification')
    return NotifyResponse('send', data={'recipient': 'technical-recipient'}, message='Technical notification')


class TestEventConditions(unittest.TestCase):
    def setUp(self):
        ProcessFixtures.setUp(self)
        from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
        sync_communication_providers()
        from frappe.permissions import add_permission
        add_permission('Emulator Contact', 'Messaging Automation', ptype='read')
        frappe.clear_cache(doctype='Emulator Contact')
        register_processes(app='messaging', processes=[NotifyProcess(id='messaging.technical_condition',
            label='Technical condition', definition=prepare_condition, handler=condition_result)])
    def tearDown(self):
        frappe.set_user('Administrator')
        ProcessFixtures.tearDown(self)
        frappe.clear_cache(doctype='Emulator Contact')

    def fixture(self, rows=None, outputs=None):
        source = frappe.get_doc(dict(doctype='Emulator Contact', display_name='Match', enabled=1)).insert()
        account = frappe.get_doc(dict(doctype='Communication Channel Account', channel='emulator',
            account_id='condition-' + frappe.generate_hash(length=12), account_name='Synthetic condition account',
            enabled=1)).insert()
        tmpl = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name='cond-tmpl-' + frappe.generate_hash(length=10), language='es', body='Event notice')).insert()
        resolved_outputs = outputs or [
            dict(status_code='send', output_mode='Template', template_reference=tmpl.name, send=1),
            dict(status_code='skip', output_mode='Template', send=0),
        ]
        for row in resolved_outputs:
            if row.get('send') and not row.get('template_reference'):
                row['template_reference'] = tmpl.name
            if row.get('output_mode') != 'Template':
                row['output_mode'] = 'Template'
        rule = frappe.get_doc(dict(doctype='Notification Rule', notification_delay_minutes=60, rule_name='Condition ' + frappe.generate_hash(length=12),
            source_application='messaging', source_doctype='Emulator Contact', status='Enabled', account=account.name,
            events=rows or [dict(document_event='on_update',field_name='display_name',value='"Match"')],
            process_definition='messaging.technical_condition',
            process_invocation_mode='Internal Call (frappe.call)',
            outputs=resolved_outputs,
            parameters=[dict(parameter_key='notification_contact',value=json.dumps(source.name))]))
        if any(row.document_event == 'on_submit' for row in rule.events):
            frappe.db.set_value(source.doctype, source.name, 'docstatus', 1, update_modified=False)
        for condition in c.canonical_conditions(rule):
            result = testing.test_method(dict(rule.as_dict(), __islocal=1), dict(name=source.name,
                condition=c.condition_token(condition)))
            self.assertEqual(result['status'], 'Passed', result)
        rule.insert()
        return source,account,rule

    def observe(self,rule,source,event='on_update'):
        with patch.object(events,'rule_index',return_value={source.doctype:{event:[rule.name]}}):
            events.observe(source,event)

    def test_scalar_values_and_metadata_allowlist(self):
        def field(kind,**kw): return frappe._dict(fieldtype=kind,**kw)
        cases=[('Int','200','200'),('Float','200.00','200'),('Currency','200.0','200'),('Percent','2.50','2.5'),
            ('Check',False,'false'),('Check',True,'true'),('Date','2026-10-29','"2026-10-29"'),
            ('Datetime','2026-10-29 12:00:00','"2026-10-29 12:00:00.000000"'),('Time','12:30','"12:30:00.000000"'),
            ('Data','Text','"Text"'),('Small Text','Two\nlines','"Two\\nlines"'),('Select','Executed','"Executed"')]
        for kind,raw,expected in cases:
            with self.subTest(kind=kind,raw=raw):
                self.assertEqual(c.normalize_value(field(kind,options='Draft\nExecuted'),raw),expected)
        for kind,raw in [('Int','2.5'),('Float','nan'),('Currency','200,00'),('Check','false'),
                         ('Date','2026-02-30'),('Datetime','not a date'),('Time','25:00'),('Select','Unknown'),('Data',{}),('Data',None)]:
            with self.subTest(invalid=(kind,raw)),self.assertRaises(frappe.ValidationError):
                c.normalize_value(field(kind,options='Draft\nExecuted'),raw)
        rows=[field(k,fieldname=k) for k in list(c.FIELD_TYPES)+['HTML','JSON','Geolocation','Attach','Attach Image','Table','Text Editor','Code','Password','Signature','Image','Table MultiSelect','Dynamic Link']]
        rows += [field('Data',fieldname='hidden',hidden=1),field('Data',fieldname='virtual',is_virtual=1),field('Data',fieldname='secret',permlevel=1),field('Data',fieldname='absent')]
        meta=frappe._dict(fields=rows,get_valid_columns=lambda:[r.fieldname for r in rows if r.fieldname!='absent'])
        self.assertEqual({f.fieldtype for f in c.condition_fields(meta)},c.FIELD_TYPES)
        self.assertFalse({'hidden','virtual','secret','absent'} & {f.fieldname for f in c.condition_fields(meta)})

    def test_server_rejects_invalid_conditions_and_links(self):
        source,account,rule=self.fixture()
        for row in [dict(document_event='invalid'),dict(document_event='on_submit'),
            dict(document_event='on_update',field_name='missing',value='"x"'),
            dict(document_event='on_update',field_name='enabled',value='"bad"'),
            dict(document_event='on_update',value='false')]:
            with self.subTest(row=row),self.assertRaises(frappe.ValidationError):
                rule.set('events',[row]);validate_rule(rule)
        rule.set('events',[dict(document_event='on_update'),dict(document_event='on_update')])
        with self.assertRaises(frappe.ValidationError):validate_rule(rule)
        field=frappe._dict(fieldtype='Link',options='Emulator Contact')
        self.assertEqual(c.normalize_value(field,source.name,check_reference=True),json.dumps(source.name))
        with self.assertRaises(frappe.DoesNotExistError):c.normalize_value(field,'nonexistent-condition',check_reference=True)
        with patch('frappe.model.document.Document.check_permission',side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):c.normalize_value(field,source.name,check_reference=True)
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError):configuration(source_doctype='Emulator Contact')

    def test_nonmatching_does_not_claim_and_repeated_return_deduplicates(self):
        source,account,rule=self.fixture()
        frappe.db.set_value(source.doctype,source.name,'display_name','Other',update_modified=False)
        self.observe(rule,source)
        self.assertEqual(frappe.db.count('Process Execution Log',{'source_name':rule.name,'execution_context':'Notification Preparation'}),0)
        frappe.db.set_value(source.doctype,source.name,'display_name','Match',update_modified=False)
        for _ in range(2):self.observe(rule,source)
        frappe.db.set_value(source.doctype,source.name,'display_name','Other',update_modified=False);self.observe(rule,source)
        frappe.db.set_value(source.doctype,source.name,'display_name','Match',update_modified=False);self.observe(rule,source)
        self.assertEqual(frappe.db.count('Process Execution Log', {'source_name': rule.name, 'execution_context': 'Notification Preparation', 'execution_outcome': 'Functional Status'}),1)
        queue=frappe.get_doc('Notification Queue',{'notification_rule':rule.name})
        self.assertEqual((queue.condition_field,queue.condition_value),('display_name','"Match"'))
        self.assertEqual(queue.occurrence_key,c.occurrence_key(rule,source,c.canonical_conditions(rule)[0]))
        self.assertTrue(frappe.db.sql("SHOW INDEX FROM `tabNotification Queue` WHERE Column_name='occurrence_key' AND Non_unique=0"))
        self.assertTrue(frappe.db.sql("SHOW INDEX FROM `tabProcess Execution Log` WHERE Column_name='notification_key' AND Non_unique=0"))
        from messaging.communication.doctype.notification_queue.notification_queue import internal_write
        with internal_write(),self.assertRaises(frappe.ValidationError):
            queue.condition_value='"Other"';queue.save()

    def test_initial_on_update_or_independent_conditions(self):
        rows=[dict(document_event='on_update',field_name='display_name',value='"Match"'),
              dict(document_event='on_update',field_name='enabled',value='true')]
        source,account,rule=self.fixture(rows)
        with patch.object(events,'rule_index',return_value={'Emulator Contact':{'on_update':[rule.name]}}):
            created=frappe.get_doc(dict(doctype='Emulator Contact',display_name='Match',enabled=1)).insert()
        self.assertEqual(frappe.db.count('Notification Queue',{'notification_rule':rule.name,'source_name':created.name}),2)
        self.assertEqual(frappe.db.count('Process Execution Log', {'source_name': rule.name, 'execution_context': 'Notification Preparation', 'execution_outcome': 'Functional Status'}),2)
        self.observe(rule,created)
        self.assertEqual(frappe.db.count('Process Execution Log', {'source_name': rule.name, 'execution_context': 'Notification Preparation', 'execution_outcome': 'Functional Status'}),2)

    def test_fingerprint_set_order_and_real_approval_invalidation(self):
        rows=[dict(document_event='after_insert'),dict(document_event='on_update',field_name='enabled',value='true')]
        source,account,rule=self.fixture(rows)
        before=testing.fingerprint(rule)
        rule.set('events',list(reversed([r.as_dict() for r in rule.events])))
        self.assertEqual(testing.fingerprint(rule),before)
        rule.save();self.assertEqual(testing.test_state(rule),'Passed')
        rule.events[0].value='false'
        self.assertNotEqual(testing.fingerprint(rule),before)
        with self.assertRaises(frappe.ValidationError):rule.save()
        self.assertEqual(testing.test_state(frappe.get_doc('Notification Rule',rule.name)),'Passed')

    def test_submit_without_filter(self):
        meta=frappe.get_meta('Emulator Contact')
        with patch.object(meta,'is_submittable',1):
            source,account,rule=self.fixture([dict(document_event='on_submit')])
            self.observe(rule,source,event='on_submit')
            self.observe(rule,source,event='on_submit')
            queue=frappe.get_doc('Notification Queue',{'notification_rule':rule.name})
            self.assertEqual((queue.document_event,queue.condition_field,queue.condition_value,queue.occurrence),('on_submit','','','on_submit'))
            self.assertEqual(frappe.db.count('Process Execution Log', {'source_name': rule.name, 'execution_context': 'Notification Preparation', 'execution_outcome': 'Functional Status'}),1)


    def test_general_template_multiple_accounts_and_channels(self):
        source,account,rule=self.fixture()
        from messaging.communication.doctype.communication_template.communication_template import validate_template
        template=frappe.get_doc(dict(doctype='Bot Flow Template', template_name='Channel '+frappe.generate_hash(length=10),
            language='es', enabled=1, body='Synthetic fixed text')).insert()
        second=frappe.get_doc(dict(doctype='Communication Channel Account',channel='emulator',account_id='second-'+frappe.generate_hash(length=10),account_name='Second synthetic')).insert()
        for name in (account.name,second.name):validate_template(template,account=name)
        # General content is independent of the account's stored channel.
        native=frappe.db.get_value
        with patch.object(frappe.db,'get_value',side_effect=lambda *a,**kw: 'other-channel' if len(a)>=3 and a[:3]==('Communication Channel Account',second.name,'channel') else native(*a,**kw)):
            validate_template(template, account=second.name)

    def test_matching_event_skip_and_invalid_output_never_enqueue(self):
        outputs = [
            dict(status_code='send', output_mode='Message', send=1),
            dict(status_code='skip', output_mode='Message', send=0),
        ]
        source, account, rule = self.fixture(
            [dict(document_event='on_update', field_name='enabled', value='true')],
            outputs=outputs,
        )
        for display_name, expected in [('Skip', 'Functional Status'), ('Invalid output', 'Invalid Schema Response')]:
            frappe.db.set_value(source.doctype, source.name, 'display_name', display_name, update_modified=False)
            source.reload()
            condition = c.canonical_conditions(rule)[0]
            from frappe.utils import now_datetime
            events.prepare(rule, source, 'on_update', c.condition_token(condition),
                frappe.generate_hash(length=64), now_datetime(), condition=condition)
            self.assertFalse(frappe.db.count('Notification Queue', {'notification_rule':rule.name}))
            logs = frappe.get_all('Process Execution Log', filters={'source_name':rule.name,
                'execution_context':'Notification Preparation'}, fields=['execution_outcome','functional_status'], order_by='creation desc')
            self.assertEqual(logs[0].execution_outcome, expected)
            if display_name == 'Skip':
                self.assertEqual(logs[0].functional_status, 'skip')

"""Focused provider-neutral notification tests with synthetic events and local transport."""
from copy import deepcopy
from datetime import timedelta
import json
import multiprocessing
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import get_datetime
from messaging.communication.doctype.communication_provider import communication_provider as provider_catalog
from messaging.communication.doctype.notification_rule import events as notification_events
from messaging.emulator import provider as emulator
from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema
from messaging.communication.doctype.communication_provider.communication_provider import get_provider_states, set_provider_enabled, _lock_provider_states, _write_provider_states
from messaging.communication.doctype.notification_rule import events
from messaging.communication.doctype.notification_rule.notification_rule import configuration, invalidate_rules, notification_destination, validate_rule
from messaging.communication.doctype.notification_queue import dispatch
from messaging.communication.doctype.process_definition import validation as process_testing
from messaging.emulator.page.emulator_console import emulator_console as console
from messaging.registry.processes import NotifyNoResponse, NotifyProcess, NotifyRequest, NotifyResponse
from messaging.registry.processes import register_processes


_calls = []


def prepare_notice_def(*, params, outcomes):
    outcomes.register('send', fields={'recipient': 'string', 'notice_text': 'string'}, mode=['template', 'message'])


def prepare_notice_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    """Only enabled synthetic notification events qualify, in either execution mode."""
    if not request.language or request.source.doctype != 'Emulator Contact' or request.event not in ('after_insert', 'on_update'):
        raise ValueError('Invalid synthetic event')
    doc = request.source.get_document()
    _calls.append(dict(name=doc.name, language=request.language))
    if not doc.enabled or not doc.display_name.startswith('Notify synthetic'):
        return NotifyNoResponse('not_eligible')
    recipient = '' if 'without recipient' in doc.display_name else 'synthetic-business-recipient'
    return NotifyResponse('send', data={'recipient': recipient, 'notice_text': 'Synthetic notification: ' + doc.display_name})


def prepare_template_def(*, params, outcomes):
    outcomes.register('send', fields={'recipient': 'string', 'label': 'string', 'amount': 'string'}, mode=['template'])


def prepare_template_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    if not request.language or request.source.doctype != 'Emulator Contact' or request.event not in ('after_insert', 'on_update'):
        raise ValueError('Invalid synthetic event')
    doc = request.source.get_document()
    _calls.append(dict(name=doc.name, language=request.language))
    if not doc.enabled or not doc.display_name.startswith('Notify synthetic'):
        return NotifyNoResponse('not_eligible')
    recipient = '' if 'without recipient' in doc.display_name else 'synthetic-business-recipient'
    return NotifyResponse('send', data={'recipient': recipient, 'label': 'Synthetic event', 'amount': '25'})


_contract_response_box = [None]


def prepare_contract_def(*, params, outcomes):
    outcomes.register('send', fields={
        'recipient': 'string',
        'nombre_abonado': 'string',
        'numero_contrato': 'string',
        'fecha_suspension': 'string',
        'monto_pendiente': 'string',
    }, mode=['template'])
    outcomes.register('skip', fields={}, mode=['template', 'message'])


def prepare_contract_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    if not request.language or request.source.doctype != 'Emulator Contact':
        raise ValueError('Invalid synthetic contract event')
    _calls.append(dict(name=request.source.name, language=request.language))
    if _contract_response_box[0] is not None:
        return deepcopy(_contract_response_box[0])
    return NotifyResponse(
        'send',
        data=dict(
            recipient='',
            nombre_abonado='Juan Pérez',
            numero_contrato='SC-00000',
            fecha_suspension='05 de noviembre de 2026',
            monto_pendiente='L 1,250.00',
        ),
    )


def prepare_zero_contract_def(*, params, outcomes):
    outcomes.register('send', fields={'recipient': 'string'}, mode=['template'])


def prepare_zero_contract_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    if _contract_response_box[0] is not None:
        return deepcopy(_contract_response_box[0])
    return NotifyResponse('send', data={'recipient': ''})




def probe_resolve(binding):
    return True


def probe_send(envelope):
    return dict(outcome='Accepted', reference='synthetic-'+envelope['outbound_id'])


def probe_validate(envelope):
    return bool(envelope['destination'])


def probe_destination(rule, account, recipient=None):
    if recipient is not None:
        return dict(effective_recipient=recipient, emulator_contact=None)
    row = next((r for r in rule.parameters if r.parameter_key == 'test_destination'), None)
    value = json.loads(row.value) if row and row.value else None
    if not value or not value.startswith('test:'):
        raise frappe.ValidationError('Invalid synthetic test destination')
    return dict(effective_recipient=value, emulator_contact=None)


def concurrent_notification(site, sites_path, action, source, rule_name, start, output):
    frappe.init(site=site,sites_path=sites_path);frappe.connect();frappe.set_user('Administrator')
    try:
        start.wait(timeout=20)
        with patch('requests.sessions.Session.request',side_effect=AssertionError('External network forbidden')):
            if action=='prepare':
                rule=frappe.get_doc('Notification Rule',rule_name)
                with patch.object(events,'rule_index',return_value={'Emulator Contact':{rule.events[0].document_event:[rule.name]}}):
                    events.observe(frappe.get_doc('Emulator Contact',source),rule.events[0].document_event)
                frappe.db.commit();value='prepared'
            else:
                doc=frappe.get_doc('Notification Queue',source)
                value=dispatch.deliver(doc.name,now=get_datetime(doc.scheduled_at)+timedelta(seconds=1))['state']
        output.put(dict(value=value))
    except Exception as exc:
        output.put(dict(error=type(exc).__name__))
    finally:
        frappe.db.rollback();frappe.destroy()


PROBE = dict(id='NTPRB', name='Synthetic notification transport', app='messaging', version='1',
    capabilities=['text', 'recipient_redirect'], operations=dict(resolve_account=__name__+'.probe_resolve',
    send=__name__+'.probe_send', validate_notification=__name__+'.probe_validate', notification_destination=__name__+'.probe_destination'),
    parameters=[dict(parameter_key='test_destination', label='Synthetic test destination', scope='Notification Rule', field_type='Data', required=1),
        dict(parameter_key='channel_label', label='Synthetic channel parameter', scope='Channel', field_type='Data')])


class TestNotificationDelivery(unittest.TestCase):
    PROCESS_IDS = (
        'messaging.test_delivery_notice',
        'messaging.test_delivery_template',
        'messaging.test_delivery_contract_matrix',
        'messaging.test_delivery_zero_contract',
    )

    @classmethod
    def setUpClass(cls):
        cls.original_states = get_provider_states()
        cls.original_session = frappe.local.session
        cls.accounts, cls.contacts, cls.rules, cls.templates, cls.channels, cls.logs = [], [], [], [], [], []
        cls.original_user = frappe.session.user
        frappe.set_user('Administrator')
        # The runner supplies a native authenticated administrative session.
        assert frappe.session.get('sid') and frappe.session.sid != 'Guest'
        cls.network = patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        cls.network.start()
        set_provider_enabled('EMULR', True)
        if not frappe.db.exists('Communication Channel', 'emulator'):
            ch = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR', channel_name='Synthetic emulator')).insert()
            set_channel_status(ch.name, 'Enabled')
            cls.channels.append(ch.name)
        sync_communication_providers()
        from frappe.permissions import add_permission
        add_permission('Emulator Contact', 'Messaging Automation', ptype='read')
        frappe.clear_cache(doctype='Emulator Contact')
        from messaging.presets.register import register as register_native
        register_native()
        register_processes(app='messaging', processes=[
            NotifyProcess(id='messaging.test_delivery_notice', label='Test delivery notice',
                definition=prepare_notice_def, handler=prepare_notice_handler),
            NotifyProcess(id='messaging.test_delivery_template', label='Test delivery template',
                definition=prepare_template_def, handler=prepare_template_handler),
            NotifyProcess(id='messaging.test_delivery_contract_matrix', label='Test delivery contract',
                definition=prepare_contract_def, handler=prepare_contract_handler),
            NotifyProcess(id='messaging.test_delivery_zero_contract', label='Test delivery zero contract',
                definition=prepare_zero_contract_def, handler=prepare_zero_contract_handler),
        ])
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        frappe.db.rollback(); frappe.set_user('Administrator')
        for dt, filters in [('Notification Queue', {'notification_rule':['in',cls.rules]}),
            ('Process Execution Log',{'source_name':['in',cls.rules]}),('Notification Rule Event',{'parent':['in',cls.rules]}),('Notification Outcome',{'parent':['in',cls.rules]}),('Notification Rule',{'name':['in',cls.rules]}),
            ('Communication Provider Value',{'parent':['in',cls.templates+cls.accounts+cls.channels+cls.rules]}),
            ('Communication Template Parameter',{'parent':['in',cls.templates]}),
            ('Communication Template',{'name':['in',cls.templates]})]:
            frappe.db.delete(dt,filters)
        if cls.contacts:
            frappe.db.delete('Process Execution Log',{'source_document':['in',cls.contacts]})
            frappe.db.delete('Notification Queue',{'emulator_contact':['in',cls.contacts]})
        for name in cls.logs:
            frappe.db.delete('Process Execution Log',{'name':name})
        sessions=frappe.get_all('Chat Session',filters={'account':['in',cls.accounts]},pluck='name')
        for dt, filters in [('Chat Message',{'session':['in',sessions]}),('Chat Turn',{'session':['in',sessions]}),
            ('Bot Dispatch',{'account':['in',cls.accounts]}),('Chat Session',{'account':['in',cls.accounts]}),
            ('Chat Contact',{'account':['in',cls.accounts]})]:frappe.db.delete(dt,filters)
        for name in cls.contacts:
            if frappe.db.exists('Emulator Contact',name):frappe.delete_doc('Emulator Contact',name)
        for name in cls.accounts:
            if frappe.db.exists('Communication Channel Account',name):frappe.delete_doc('Communication Channel Account',name)
        for name in cls.channels:
            if frappe.db.exists('Communication Channel',name):frappe.delete_doc('Communication Channel',name)
        frappe.db.delete('Communication Provider Parameter',{'parent':'NTPRB'})
        frappe.db.delete('Communication Provider',{'name':'NTPRB'})
        for dt in ('Process Definition',):
            parent_filter = {'name': ['in', list(cls.PROCESS_IDS)]}
            frappe.db.delete(dt, parent_filter)
        from messaging.registry import synchronize_registry
        synchronize_registry()
        _lock_provider_states(); _write_provider_states(cls.original_states)
        invalidate_rules(); frappe.db.commit()
        cls.network.stop(); frappe.local.session=cls.original_session

    def setUp(self):
        frappe.set_user('Administrator');_calls.clear()
        self.dest=self.contact('Destination synthetic')
        self.account=self.new_account()
        self.source=self.contact('Notify synthetic without recipient')
        self.created_rules=[]

    def tearDown(self):
        frappe.db.rollback();frappe.set_user('Administrator')
        for name in self.created_rules:
            frappe.db.set_value('Notification Rule',name,'status','Disabled',update_modified=False)
        invalidate_rules();frappe.db.commit()

    def contact(self,label):
        # Fixtures bypass events until explicitly requested for their exact rule.
        with patch.object(events,'rule_index',return_value={}):
            doc=frappe.get_doc(dict(doctype='Emulator Contact',display_name=label)).insert()
        self.contacts.append(doc.name);frappe.db.commit();return doc

    def new_account(self,channel='emulator',parameters=None):
        doc=frappe.get_doc(dict(doctype='Communication Channel Account',channel=channel,
            account_id='synthetic-'+frappe.generate_hash(length=12),account_name='Synthetic notification test',enabled=1,
            parameters=parameters or [])).insert()
        self.accounts.append(doc.name);frappe.db.commit();return doc

    def configure_destination(self,name,rule):
        rule.parameters[0].value=json.dumps(name)
        answer=process_testing.test_method(rule.as_dict(),{'name':self.source.name})
        self.logs.append(answer['execution_log']);self.assertEqual(answer['status'],'Passed')
        rule.save();frappe.db.commit()

    def template(self,body='Event {{label}}: {{amount}}',account=None,enabled=1,header='',footer='',template_variables=None,parameters=None):
        if template_variables is None:
            import re
            names=[]
            for part in (header,body,footer):
                for match in re.findall(r'\{\{([a-z]+(?:_[a-z]+)*)\}\}',part or ''):
                    if match not in names:
                        names.append(match)
            template_variables=[dict(parameter_name=n) for n in names]
        account_doc = account or self.account
        doc = frappe.get_doc(dict(doctype='Communication Template',
            template_name='Synthetic template ' + frappe.generate_hash(length=10), language='es',
            channel=account_doc.channel, status='Active' if enabled else 'Inactive',
            header=header, body=body, footer=footer, template_variables=template_variables,
            parameters=parameters or [])).insert()
        self.templates.append(doc.name)
        frappe.db.commit()
        return doc

    def rule(self,**values):
        account = values.get('account') or self.account.name
        template_ref = values.pop('template_reference', None)
        values.pop('content_type', None)
        process_definition = values.pop('process_definition', 'messaging.test_delivery_notice')
        if not template_ref and 'outputs' not in values:
            account_doc = frappe.get_doc('Communication Channel Account', account)
            tmpl = self.template(body='{{notice_text}}', account=account_doc)
            template_ref = tmpl.name
        outputs = values.pop('outputs', [dict(status_code='send', output_mode='Template', template_reference=template_ref, send=1)])
        data=dict(doctype='Notification Rule',notification_delay_minutes=60,rule_name='Synthetic notice '+frappe.generate_hash(length=10),status='Enabled',
            source_application='messaging',source_doctype='Emulator Contact',events=[dict(document_event='after_insert')],account=account,
            parameters=[dict(parameter_key='notification_contact',value=json.dumps(self.dest.name))],
            process_definition=process_definition,outputs=outputs,process_invocation_mode='Internal Call (frappe.call)')
        data.update(values);doc=frappe.get_doc(dict(data,__islocal=1))
        answer=process_testing.test_method(doc.as_dict(),{'name':self.source.name})
        self.logs.append(answer['execution_log'])
        self.assertEqual(answer['status'],'Passed',answer)
        candidate=process_testing._configuration(doc.as_dict())
        process_testing._validate_candidate(candidate)
        key,approval=process_testing._read_approval(candidate)
        self.assertTrue(approval);self.assertGreater(frappe.cache.ttl(key),0);self.assertLessEqual(frappe.cache.ttl(key),900)
        doc.insert();self.rules.append(doc.name);self.created_rules.append(doc.name);frappe.db.commit()
        self.assertEqual(process_testing.test_state(doc),'Passed')
        return doc

    def prepare(self,rule,source=None):
        source=source or self.source
        with patch.object(events,'rule_index',return_value={'Emulator Contact':{rule.events[0].document_event:[rule.name]}}):
            events.observe(source,rule.events[0].document_event)
        frappe.db.commit()
        name=frappe.db.get_value('Notification Queue',{'notification_rule':rule.name,'source_name':source.name},'name')
        self.assertTrue(name,'Preparation must create a ready snapshot')
        return frappe.get_doc('Notification Queue',name)

    def due(self,queue,extra=0):
        return dispatch.deliver(queue.name,now=get_datetime(queue.scheduled_at)+timedelta(minutes=extra,seconds=1))

    def test_emulr_e2e_no_business_recipient_delay_dedupe_and_history(self):
        rule=self.rule();queue=self.prepare(rule)
        self.assertEqual(queue.recipient,'');self.assertEqual(queue.effective_recipient,self.dest.sender_id)
        self.assertEqual(queue.delivery_mode,'Required')
        self.assertEqual(self.prepare(rule).name,queue.name)
        self.assertEqual(dispatch.dispatch_due(names=[queue.name],now=queue.event_at),[])
        self.assertEqual(self.due(queue)['state'],'Accepted')
        queue.reload();self.assertEqual(queue.delivery_status,'Sent');self.assertFalse(queue.delivered_at)
        before=len(_calls);self.assertEqual(self.due(queue)['state'],'Accepted');self.assertEqual(len(_calls),before)
        rows=console.history(self.account.name,self.dest.sender_id)['notifications']
        self.assertEqual(rows[-1].name,queue.name)
        console.simulate_notification(self.account.name,self.dest.sender_id,queue.name,status='Delivered')
        console.simulate_notification(self.account.name,self.dest.sender_id,queue.name,status='Delivered')
        queue.reload();self.assertEqual(queue.delivery_status,'Delivered')
        self.assertEqual(len(_calls), 2)

    def test_templates_variables_incompatibility_and_adapter_representation(self):
        from messaging.communication.doctype.communication_template.communication_template import set_active
        body='Aviso {{label}}.\nMonto: {{amount}}.\n¡Gracias! 🙂'
        template=self.template(body=body);template.reload()
        self.assertEqual(template.body,body)
        self.assertEqual([r.parameter_name for r in template.template_variables],['label','amount'])
        schema=parameter_schema('Communication Template',channel='emulator')
        self.assertEqual(schema['parameters'],[])
        rule=self.rule(template_reference=template.name,process_definition='messaging.test_delivery_template')
        queue=self.prepare(rule);result=self.due(queue);queue.reload();self.assertEqual(result['state'],'Accepted',queue.diagnostic_code)
        queue.reload();self.assertEqual(queue.rendered_content,'Aviso Synthetic event.\nMonto: 25.\n¡Gracias! 🙂')
        self.assertEqual(console.history(self.account.name,self.dest.sender_id)['notifications'][-1].rendered_content,queue.rendered_content)
        other=self.new_account()
        from messaging.communication.doctype.communication_template.communication_template import validate_template
        validate_template(template,account=other.name,variables={'label':'x','amount':'25'})
        with self.assertRaises(frappe.ValidationError):self.template(body='',template_variables=[])
        invalid=self.template(body='Needs {{missing}}',template_variables=[dict(parameter_name='missing')])
        candidate=frappe.get_doc(dict(rule.as_dict(),outputs=[dict(status_code='send',output_mode='Template',template_reference=invalid.name,send=1)]))
        with self.assertRaisesRegex(frappe.ValidationError,'missing'):
            process_testing.test_method(candidate.as_dict(),{'name':self.source.name})
        from messaging.communication.doctype.notification_rule.notification_rule import set_active as set_rule_active
        with self.assertRaisesRegex(frappe.ValidationError, rule.name):
            set_active(dict(name=template.name, modified=str(template.modified)), 0)
        set_rule_active(dict(rule.as_dict()), 0)
        set_active(dict(name=template.name, modified=str(template.modified)), 0)
        frappe.db.commit()
        rule.reload()
        self.assertEqual(rule.status, 'Disabled')
        self.assertEqual(process_testing.test_state(rule), 'Untested')

    def test_whatsapp_named_queue_uses_the_published_revision_until_it_changes(self):
        from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
        from messaging.communication.doctype.communication_template.communication_template import set_active
        from messaging.communication.doctype.communication_template.external import synchronize_templates
        from messaging.whatsapp import admin
        from messaging.whatsapp.api import Graph

        set_provider_enabled('WHATSAPP', True)
        channel = frappe.get_doc(dict(doctype='Communication Channel', provider='WHATSAPP',
            channel_name='Synthetic named notification channel', parameters=[
                dict(parameter_key='app_id', value='"199001"'),
                dict(parameter_key='app_secret', secret_value='synthetic-secret'),
            ])).insert()
        self.channels.append(channel.name)
        admin.generate_webhook_token(channel.name, str(channel.modified))
        set_channel_status(channel.name, 'Enabled')
        account = frappe.get_doc(dict(doctype='Communication Channel Account', channel=channel.name,
            account_id='199002', account_name='Synthetic named notification account', enabled=1,
            parameters=[
                dict(parameter_key='waba_id', value='"199003"'),
                dict(parameter_key='access_token', secret_value='synthetic-token'),
            ])).insert()
        self.accounts.append(account.name)
        body = 'Hello {{nombre_abonado}}, account {{numero_contrato}}. Pay {{monto_pendiente}} ({{monto_pendiente}}).'
        names = ('nombre_abonado', 'numero_contrato', 'monto_pendiente')
        template = frappe.get_doc(dict(doctype='Communication Template', channel=channel.name, account=account.name,
            template_name='Named delivery fixture', language='es', provider_language='es_HN', status='Inactive',
            body=body, template_variables=[dict(parameter_name=key) for key in names], parameters=[
                dict(parameter_key='parameter_format', value='"NAMED"'),
            ])).insert()
        self.templates.append(template.name)
        frappe.db.commit()
        remote = dict(id='199004', name='named_delivery_fixture', language='es_HN',
            parameter_format='NAMED', category='UTILITY', status='APPROVED',
            components=[dict(type='BODY', text=body)])
        values = dict(nombre_abonado='Juan Pérez', numero_contrato='001245', monto_pendiente='L 850.00')
        self.enterContext(patch.object(Graph, 'templates', side_effect=lambda *_: [remote]))
        send = self.enterContext(patch.object(Graph, 'send', return_value=dict(messages=[dict(id='wamid.named_fixture')])))
        self.assertTrue(synchronize_templates('WHATSAPP', channel.name, account.name)['success'])
        template.reload()
        set_active(dict(name=template.name, modified=str(template.modified)), 1)
        template.reload()
        first_revision = template.published_revision
        response = NotifyResponse('send', data=dict(values, recipient='+15550000001', fecha_suspension='Tomorrow'))
        _contract_response_box[0] = response
        self.addCleanup(_contract_response_box.__setitem__, 0, None)
        rule = self.rule(account=account.name, parameters=[], outputs=[
            dict(status_code='send', send=1, template_reference=template.name), dict(status_code='skip', send=0),
        ],
            process_definition='messaging.test_delivery_contract_matrix')

        template.body += ' Due {{fecha_suspension}}.'
        template.append('template_variables', dict(parameter_name='fecha_suspension'))
        template.save()
        frappe.db.commit()
        queue = self.prepare(rule)
        exact_contract = queue.template_contract_json
        self.assertEqual(queue.template_revision, first_revision)
        self.assertEqual(json.loads(queue.variables_json), values)
        self.assertEqual(json.loads(exact_contract)['delivery']['names']['body'],
            {key: key for key in names})
        remote['status'] = 'PAUSED'
        self.assertEqual(self.due(queue)['state'], 'Deferred')
        self.assertEqual(queue.reload().diagnostic_code, 'template_unavailable')
        send.assert_not_called()

        remote['status'] = 'APPROVED'
        self.assertEqual(self.due(queue, extra=5)['state'], 'Accepted', queue.reload().diagnostic_code)
        outgoing = send.call_args.args[0]['template']
        self.assertEqual(outgoing['language'], {'code': 'es_HN'})
        self.assertEqual(outgoing['components'][0]['type'], 'body')
        parameters = outgoing['components'][0]['parameters']
        self.assertEqual(len(parameters), 3)
        self.assertEqual({row['parameter_name']: row['text'] for row in parameters}, values)

        remote['components'][0]['text'] += ' Due {{fecha_suspension}}.'
        self.assertEqual(synchronize_templates('WHATSAPP', channel.name, account.name)['updated'], 1)
        self.assertNotEqual(template.reload().published_revision, first_revision)
        self.assertNotEqual(rule.reload().test_state(), 'Passed')
        self.assertEqual(dispatch.revalidate(queue), 'template_unavailable')
        self.assertEqual((queue.reload().template_revision, queue.template_contract_json), (first_revision, exact_contract))
        send.assert_called_once()

    def test_rule_destination_changes_disable_and_no_fallback(self):
        for change in ['destination','disabled_contact','rule_disabled']:
            with self.subTest(change=change):
                rule=self.rule();queue=self.prepare(rule)
                if change=='destination':
                    other_dest=self.contact('Other synthetic').name
                    with self.assertRaises(frappe.ValidationError):
                        self.configure_destination(other_dest,rule)
                    rule.reload()
                    frappe.db.set_value('Communication Provider Value',rule.parameters[0].name,'value',json.dumps(other_dest),update_modified=False)
                    frappe.db.commit()
                elif change=='disabled_contact':
                    self.dest.enabled=0;self.dest.save();frappe.db.commit()
                else:
                    rule.status='Disabled';rule.save();frappe.db.commit()
                with patch.object(emulator,'send',side_effect=AssertionError('Unsafe handoff')):
                    state=self.due(queue)['state']
                self.assertIn(state,('Cancelled','Deferred'));queue.reload();self.assertFalse(queue.handoff_at)
                self.dest.reload();self.dest.enabled=1;self.dest.save();frappe.db.commit()

    def test_human_active_paused_close_and_business_revalidation(self):
        from messaging.common.locking import contact_lock
        from messaging.common.primitives import utcnow
        from messaging.conversation.doctype.chat_contact.chat_contact import get_contact
        from messaging.conversation.doctype.chat_session.chat_session import get_session
        rule=self.rule();queue=self.prepare(rule)
        with contact_lock(queue.identity_key):
            session=get_session(get_contact('emulator',self.account.name,self.dest.sender_id),utcnow())
            session.status='Human Active';session.save(ignore_permissions=True);frappe.db.commit()
        for state in ['Human Active','Human Paused']:
            session.status=state;session.save(ignore_permissions=True);frappe.db.commit()
            self.assertEqual(self.due(queue,extra=5)['state'],'Deferred')
            queue.reload();self.assertEqual(queue.diagnostic_code,'human_attention');self.assertFalse(queue.handoff_at)
        session.status='Closed';session.active_key=None;session.save(ignore_permissions=True);frappe.db.commit()
        with patch.object(events,'rule_index',return_value={}):
            self.source.display_name='Event no longer valid';self.source.save();frappe.db.commit()
        self.assertEqual(self.due(queue,extra=10)['state'],'Cancelled')
        queue.reload();self.assertEqual(queue.diagnostic_code,'event_no_longer_eligible');self.assertFalse(queue.handoff_at)

    def test_optional_redirect_plain_provider_and_shared_test_destination(self):
        from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers
        from messaging.presets.communication_providers.emulr import definition as emulator_definition
        with registered_providers([emulator_definition(), deepcopy(PROBE)]):
            sync_communication_providers()
            set_provider_enabled('NTPRB',True)
            channel=frappe.get_doc(dict(doctype='Communication Channel',provider='NTPRB',channel_name='Synthetic provider',
                parameters=[dict(parameter_key='channel_label',value='"Synthetic channel"')])).insert()
            set_channel_status(channel.name, 'Enabled')
            self.channels.append(channel.name);frappe.db.commit()
            account=self.new_account(channel.name)
            self.source.display_name='Notify synthetic real-format-independent';self.source.save();frappe.db.commit()
            info=configuration(account=account.name)
            self.assertTrue(info['redirect_supported']);self.assertFalse(info['redirect_required']);self.assertEqual(info['content_types'],['Template'])
            self.assertEqual(parameter_schema('Communication Template', channel=account.channel)['parameters'], [])
            normal=self.rule(account=account.name,parameters=[]);a=self.prepare(normal)
            self.assertEqual(a.effective_recipient,a.recipient);self.assertEqual(a.delivery_mode,'Normal')
            redirected=self.rule(account=account.name,redirect_recipient=1,parameters=[dict(parameter_key='test_destination',value='"test:shared"')]);b=self.prepare(redirected)
            self.assertEqual(b.effective_recipient,'test:shared');self.assertEqual(b.delivery_mode,'Redirected')
            other=self.contact('Notify synthetic second event');c=self.prepare(redirected,other)
            self.assertNotEqual(b.occurrence_key,c.occurrence_key)
            self.assertEqual(b.identity_key,c.identity_key)
            self.assertEqual(self.due(a)['state'],'Accepted')
            from messaging.common.primitives import identity_key,utcnow
            from messaging.common.locking import contact_lock
            from messaging.conversation.doctype.chat_contact.chat_contact import get_contact
            from messaging.conversation.doctype.chat_session.chat_session import get_session
            key=identity_key(channel.name,account.name,a.recipient)
            with contact_lock(key):
                session=get_session(get_contact(channel.name,account.name,a.recipient),utcnow())
                session.status='Human Active';session.save(ignore_permissions=True);frappe.db.commit()
            self.assertEqual(self.due(b)['state'],'Deferred')
            b.reload();self.assertEqual(b.diagnostic_code,'human_attention')
            session.status='Closed';session.active_key=None;session.save(ignore_permissions=True);frappe.db.commit()
            self.assertEqual(self.due(b,extra=6)['state'],'Accepted')
            redirected.redirect_recipient=0
            with self.assertRaises(frappe.ValidationError):
                process_testing.test_method(redirected.as_dict(),{'name':self.source.name})
            with self.assertRaises(frappe.ValidationError):redirected.save()
            frappe.db.set_value('Notification Rule',redirected.name,'redirect_recipient',0,update_modified=False);frappe.db.commit()
            with patch.object(dispatch,'handoff',side_effect=AssertionError('No fallback')):
                self.assertEqual(self.due(c)['state'],'Cancelled')
            for value in [None,'invalid']:
                redirected.redirect_recipient=1;redirected.parameters[0].value=json.dumps(value)
                with self.assertRaises(frappe.ValidationError):notification_destination(redirected,account,'business')

    def test_permissions_required_destination_and_approval_binding(self):
        info=configuration(account=self.account.name)
        self.assertFalse(info['redirect_supported']);self.assertTrue(info['redirect_required']);self.assertNotIn('emulator_contact',info)
        account=self.new_account()
        self.assertTrue(emulator.resolve_account(account))
        with self.assertRaises(frappe.ValidationError):notification_destination(frappe.get_doc(dict(doctype='Notification Rule', account=account.name, status='Enabled', parameters=[])), account)
        rule=self.rule();queue=self.prepare(rule)
        queue.delivery_mode='Normal'
        with self.assertRaises(frappe.PermissionError):queue.save(ignore_permissions=True)
        queue.reload();queue.delivery_fingerprint='forged'
        from messaging.communication.doctype.notification_queue.notification_queue import internal_write
        with internal_write(),self.assertRaises(frappe.ValidationError):queue.save(ignore_permissions=True)
        rule.parameters[0].value=json.dumps(self.contact('Changed synthetic').name)
        self.assertEqual(process_testing.test_state(rule),'Untested')
        frappe.set_user('Guest')
        for doctype,action in [('Communication Template','create'),('Communication Channel Account','write'),('Notification Rule','create')]:
            self.assertFalse(frappe.has_permission(doctype,action))
        with self.assertRaises(frappe.PermissionError):configuration(account=self.account.name)
        with self.assertRaises(frappe.PermissionError):parameter_schema('Communication Template',channel=self.account.channel)
        with self.assertRaises(frappe.PermissionError):dispatch.deliver('invalid')
        with self.assertRaises(frappe.PermissionError):process_testing.describe(rule.as_dict())

    def test_conversation_account_without_notification_destination(self):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        from messaging.conversation.doctype.chat_turn import chat_turn
        from messaging.conversation.doctype.chat_turn.inbox import drain_contact
        settings=frappe.get_single('Bot Settings')
        settings.update(dict(
            interpretation_enabled=0, registration_enabled=0, greeting_enabled=0,
            human_attention_enabled=0, informative_show_available_categories=0,
            max_invalid_attempts=3,
        ))
        settings.set('message_translations', [dict(message_key=k, language='es', message='Synthetic ' + k) for k in MESSAGE_KEYS])
        with patch.object(chat_turn,'get_bot_settings',return_value=settings), \
                patch.object(chat_turn,'mechanical_menu_flows',return_value=[dict(workflow_code='synthetic_menu',menu_label='Menu',menu_description='Menu')]):
            result=console.send_message(self.account.name,self.dest.sender_id,frappe.generate_hash(length=32),text='Synthetic menu request')
            drain_contact(result['contact'])
        turn=frappe.get_doc('Chat Turn',result['turn'])
        self.assertTrue(turn.dispatch)
        outbound=frappe.get_doc('Bot Dispatch',turn.dispatch)
        self.assertEqual(outbound.status,'Accepted');self.assertEqual(outbound.delivery_status,'Sent')
        rows=console.history(self.account.name,self.dest.sender_id)['messages']
        self.assertTrue(any(r.kind=='Incoming' and r.content=='Synthetic menu request' for r in rows))
        self.assertTrue(any(r.name==outbound.message for r in rows))
        self.account.reload();self.assertEqual([r.parameter_key for r in self.account.parameters],['transport_outcome'])

    def test_two_rules_share_account_with_independent_destinations(self):
        body='Aviso {{label}}.\nMonto: {{amount}}.\n¡Gracias!'
        template=self.template(body=body)
        a=self.rule(template_reference=template.name,process_definition='messaging.test_delivery_template')
        other=self.contact('Destination synthetic B');source_b=self.contact('Notify synthetic business B')
        b=self.rule(template_reference=template.name,process_definition='messaging.test_delivery_template',
            parameters=[dict(parameter_key='notification_contact',value=json.dumps(other.name))])
        qa=self.prepare(a);qb=self.prepare(b,source_b)
        self.assertEqual(qa.account,qb.account);self.assertNotEqual(qa.occurrence_key,qb.occurrence_key)
        self.assertEqual(qa.recipient,'');self.assertEqual(qb.recipient,'synthetic-business-recipient')
        self.assertNotEqual(qa.identity_key,qb.identity_key)
        self.assertEqual(self.due(qa)['state'],'Accepted');self.assertEqual(self.due(qb)['state'],'Accepted')
        history_a={r.name for r in console.history(self.account.name,self.dest.sender_id)['notifications']}
        history_b={r.name for r in console.history(self.account.name,other.sender_id)['notifications']}
        self.assertIn(qa.name,history_a);self.assertNotIn(qb.name,history_a)
        self.assertIn(qb.name,history_b);self.assertNotIn(qa.name,history_b)
        qa.reload();qb.reload();self.assertEqual(qa.rendered_content,qb.rendered_content)
        self.assertEqual(qa.rendered_content,'Aviso Synthetic event.\nMonto: 25.\n¡Gracias!')
        self.assertEqual([r.parameter_key for r in self.account.parameters],['transport_outcome'])

    def test_rule_parameter_validation_and_account_switch(self):
        schema=parameter_schema('Notification Rule',account=self.account.name)['parameters']
        self.assertEqual([(r['parameter_key'],r['field_type'],r['options'],r['required']) for r in schema],
            [('notification_contact','Link','Emulator Contact',1)])
        self.assertEqual(schema[0]['filters'],{'enabled':1})
        rule=self.rule();original=rule.as_dict()
        for value in [None,'missing-synthetic-contact',42]:
            candidate=frappe.get_doc(original);candidate.parameters[0].value=json.dumps(value)
            with self.assertRaises(frappe.ValidationError):validate_rule(candidate)
        self.dest.enabled=0;self.dest.save();frappe.db.commit()
        with self.assertRaises(frappe.ValidationError):validate_rule(frappe.get_doc(original))
        self.dest.enabled=1;self.dest.save();frappe.db.commit()
        other=self.new_account();rule.account=other.name
        self.assertEqual(process_testing.test_state(rule),'Untested')
        with self.assertRaises(frappe.ValidationError):rule.save()
        frappe.db.rollback();rule.reload();rule.account=other.name
        with self.assertRaises(frappe.ValidationError):
            process_testing.test_method(rule.as_dict(),{'name':self.source.name})
        with self.assertRaises(frappe.ValidationError):rule.save()
        frappe.db.rollback()
        switched=self.rule(account=other.name)
        self.assertEqual(switched.account,other.name);self.assertEqual(process_testing.test_state(switched),'Passed')
        with self.assertRaises(frappe.ValidationError):
            self.account.append('parameters',dict(parameter_key='notification_contact',value=json.dumps(self.dest.name)))
            self.account.save()

    def test_retry_before_handoff_uncertain_result_and_template_change(self):
        rule=self.rule();queue=self.prepare(rule)
        def fail_validate(definition, name, *args, orig):
            raise RuntimeError('Synthetic transient failure')
        with patch_transport_operation(dispatch, 'validate_notification', fail_validate):
            self.assertEqual(self.due(queue)['state'],'Deferred')
        queue.reload();self.assertEqual(queue.diagnostic_code,'content_validation_unavailable');self.assertFalse(queue.handoff_at)
        self.assertEqual(self.due(queue,extra=6)['state'],'Accepted')
        template=self.template(body='{{label}} {{amount}}');rule=self.rule(template_reference=template.name,process_definition='messaging.test_delivery_template')
        queue=self.prepare(rule);template.body='Changed {{label}} {{amount}}';template.save();frappe.db.commit()
        with patch.object(dispatch,'handoff',side_effect=AssertionError('Changed template must not send')):
            self.assertEqual(self.due(queue)['state'],'Cancelled')
        # Unknown handoffs are never retried automatically.
        rule=self.rule();queue=self.prepare(rule)
        with patch.object(dispatch,'handoff',return_value={'outcome':'Unknown'}):
            self.assertEqual(self.due(queue)['state'],'Unknown')
        before=queue.attempt_count;self.assertEqual(self.due(queue,extra=30)['state'],'Unknown')
        queue.reload();self.assertEqual(queue.attempt_count,before+1)


    def test_concurrent_preparation_and_dispatch_exactly_once(self):
        rule=self.rule(events=[dict(document_event='on_update',field_name='enabled',value='true')])
        def race(action,source):
            ctx=multiprocessing.get_context('spawn');start=ctx.Barrier(2);output=ctx.Queue()
            children=[ctx.Process(target=concurrent_notification,args=(frappe.local.site,str(frappe.local.sites_path),action,source,rule.name,start,output)) for _ in range(2)]
            for child in children:child.start()
            for child in children:child.join(25)
            for child in children:
                if child.is_alive():child.terminate();child.join();self.fail('Synthetic concurrency timeout')
            rows=[output.get(timeout=3) for _ in children]
            self.assertFalse(any('error' in r for r in rows),rows)
            frappe.db.rollback();return rows
        race('prepare',self.source.name)
        names=frappe.get_all('Notification Queue',filters={'notification_rule':rule.name,'source_name':self.source.name},pluck='name')
        self.assertEqual(len(names),1)
        self.assertEqual(frappe.db.count('Process Execution Log',{'notification_key':frappe.db.get_value('Notification Queue',names[0],'occurrence_key')}),1)
        self.assertEqual([r['value'] for r in race('deliver',names[0])],['Accepted','Accepted'])
        doc=frappe.get_doc('Notification Queue',names[0]);self.assertEqual(doc.attempt_count,1)

    def test_required_template_capability_and_invalid_registration(self):
        from messaging.communication.doctype.notification_rule.notification_rule import content_types
        definition=deepcopy(provider_catalog.provider_definition('EMULR'))
        definition['capabilities'].append('notification_template_required')
        self.assertEqual(content_types(definition),['Template'])
        definition['capabilities']=['text'];self.assertEqual(content_types(definition),['Template'])
        # A provider cannot publish template definitions while declaring no support.
        from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers
        invalid=dict(deepcopy(PROBE),parameters=[dict(parameter_key='illegal',label='Illegal',field_type='Data',scope='Template')])
        with registered_providers([invalid]):
            with self.assertRaises(frappe.ValidationError):provider_catalog.provider_definitions()

    def test_template_native_components_child_doctype_and_universal_validation(self):
        # 6.1 Structure and metadata
        tmpl_meta=frappe.get_meta('Communication Template')
        self.assertEqual(tmpl_meta.get_field('header').fieldtype,'Data')
        self.assertEqual(tmpl_meta.get_field('body').fieldtype,'Long Text')
        self.assertEqual(tmpl_meta.get_field('footer').fieldtype,'Data')
        self.assertEqual(tmpl_meta.get_field('template_variables').fieldtype,'Table')
        self.assertEqual(tmpl_meta.get_field('template_variables').options,'Communication Template Parameter')
        self.assertEqual(tmpl_meta.get_field('parameters').options,'Communication Provider Value')

        child_meta=frappe.get_meta('Communication Template Parameter')
        self.assertEqual(child_meta.istable,1)
        self.assertEqual([f.fieldname for f in child_meta.fields],['parameter_name'])
        self.assertEqual(child_meta.get_field('parameter_name').fieldtype,'Data')
        self.assertEqual(child_meta.get_field('parameter_name').reqd,1)

        # Direct child CRUD/rename blocked
        valid_tmpl=self.template(
            header='Estimado {{nombre_abonado}}',
            body='Contrato {{numero_contrato}}\nFecha {{fecha_suspension}}\nMonto {{monto_pendiente}}',
            footer='Referencia {{contrato}}',
            template_variables=[
                dict(parameter_name='nombre_abonado'),
                dict(parameter_name='numero_contrato'),
                dict(parameter_name='fecha_suspension'),
                dict(parameter_name='monto_pendiente'),
                dict(parameter_name='contrato'),
            ],
        )
        valid_tmpl.reload()
        self.assertIn('\n',valid_tmpl.body)
        child_row=frappe.get_doc('Communication Template Parameter',valid_tmpl.template_variables[0].name)
        with self.assertRaises(frappe.PermissionError):
            child_row.save(ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc('Communication Template Parameter',child_row.name,ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            child_row.run_method('before_rename',child_row.doctype,child_row.name,'forged')
        # Row stealing across persisted templates blocked
        stolen_doc=self.template(
            body='Hola {{nombre_abonado}}',
            template_variables=[dict(parameter_name='nombre_abonado')],
        )
        stolen_doc.template_variables[0].name=child_row.name
        with self.assertRaises(frappe.PermissionError):
            stolen_doc.save()

        def build(header='',body='Texto fijo',footer='',variables=()):
            return frappe.get_doc(dict(
                doctype='Bot Flow Template',
                template_name='Universal validation '+frappe.generate_hash(length=10),
                language='es',
                enabled=1,
                header=header,
                body=body,
                footer=footer,
                template_variables=[dict(parameter_name=v) for v in variables],
            ))

        # 6.2 #16: Zero-variable template accepted
        zero_var=self.template(header='Aviso general',body='Sin marcadores\nSegunda línea',footer='QOTA',template_variables=[])
        self.assertEqual(len(zero_var.template_variables),0)

        # 6.2 #11, #13, #15: Header 1 var, Footer 1 var, Body multiple vars accepted (validated above in valid_tmpl)
        self.assertEqual(len(valid_tmpl.template_variables),5)

        # 6.2 #2-#5: Invalid variable names (digits, uppercase, symbols/spaces, invalid underscores, reserved keys)
        for bad_name in [
            'contrato1','numero_2','NombreAbonado','nombre-Abonado','nombre abonado',
            '_contrato','contrato_','nombre__abonado','action','text','variables',
        ]:
            with self.subTest(bad_name=bad_name),self.assertRaises(frappe.ValidationError):
                build(body=f'Valor {{{{{bad_name}}}}}',variables=(bad_name,)).insert()

        # 6.2 #6: Duplicate declaration rejected
        with self.assertRaisesRegex(frappe.ValidationError,'Duplicate template parameter'):
            build(body='Hola {{contrato}}',variables=('contrato','contrato')).insert()

        # 6.2 #7: Duplicate usage rejected (same component and across Header/Body/Footer)
        with self.assertRaisesRegex(frappe.ValidationError,'more than once'):
            build(body='Hola {{nombre_abonado}} y {{nombre_abonado}}',variables=('nombre_abonado',)).insert()
        with self.assertRaisesRegex(frappe.ValidationError,'more than once'):
            build(header='Estimado {{nombre_abonado}}',body='Hola {{nombre_abonado}}, su recibo está disponible.',variables=('nombre_abonado',)).insert()

        # 6.2 #8: Used without declaration rejected
        with self.assertRaisesRegex(frappe.ValidationError,'undeclared'):
            build(body='Hola {{nombre_abonado}}',variables=()).insert()

        # 6.2 #9: Declared without usage rejected
        with self.assertRaisesRegex(frappe.ValidationError,'not used'):
            build(body='Hola {{nombre_abonado}}',variables=('nombre_abonado','monto_pendiente')).insert()

        # 6.2 #10: Malformed placeholders and Jinja expressions rejected
        for bad_body in [
            'Hola {{ nombre_abonado }}',
            'Hola {nombre_abonado}',
            'Hola {{nombre_abonado}',
            'Hola nombre_abonado}}',
            'Hola {{{nombre_abonado}}}',
            'Hola {{}}',
            'Hola {{nombre_abonado + 1}}',
            'Hola {% if nombre_abonado %}x{% endif %}',
            'Hola {# comentario #}',
        ]:
            with self.subTest(bad_body=bad_body),self.assertRaises(frappe.ValidationError):
                build(body=bad_body,variables=('nombre_abonado',)).insert()

        # 6.2 #12 & #14: Header with 2 variables rejected; Footer with 2 variables rejected
        with self.assertRaisesRegex(frappe.ValidationError,'at most 1'):
            build(header='{{uno}} {{dos}}',body='Cuerpo fijo',variables=('uno','dos')).insert()
        with self.assertRaisesRegex(frappe.ValidationError,'at most 1'):
            build(body='Cuerpo fijo',footer='{{uno}} {{dos}}',variables=('uno','dos')).insert()

    def test_process_flat_string_contract_administrative_skip_and_operational_validation(self):
        tmpl=self.template(
            header='Estimado {{nombre_abonado}}',
            body='Contrato {{numero_contrato}} vence el {{fecha_suspension}} por {{monto_pendiente}}.',
            footer='Gracias por su atención.',
            template_variables=[
                dict(parameter_name='nombre_abonado'),
                dict(parameter_name='numero_contrato'),
                dict(parameter_name='fecha_suspension'),
                dict(parameter_name='monto_pendiente'),
            ],
        )
        candidate_data=dict(
            doctype='Notification Rule',
            rule_name='Contract matrix '+frappe.generate_hash(length=8),
            status='Enabled',
            source_application='messaging',
            source_doctype='Emulator Contact',
            events=[dict(document_event='after_insert')],
            account=self.account.name,
            parameters=[dict(parameter_key='notification_contact',value=json.dumps(self.dest.name))],
            process_definition='messaging.test_delivery_contract_matrix',
            outputs=[
                dict(status_code='send',output_mode='Template',template_reference=tmpl.name,send=1),
                dict(status_code='skip',output_mode='Message',send=0),
            ],
            process_invocation_mode='Internal Call (frappe.call)',
        )

        # Structural administrative test validates contract without executing business handler logic
        struct_res=process_testing.test_method(dict(candidate_data,__islocal=1),{'name':self.source.name})
        self.logs.append(struct_res['execution_log'])
        self.assertEqual(struct_res['status'],'Passed',struct_res)
        rule=frappe.get_doc(dict(candidate_data,__islocal=1)).insert()
        self.rules.append(rule.name);self.created_rules.append(rule.name);frappe.db.commit()
        self.assertEqual(process_testing.test_state(rule),'Passed')

        # Reject missing, extra, null, int, float, bool, nested dict/list, empty/whitespace, and undeclared outcome
        from messaging.registry.processes import ProcessSchemaValidationError
        base_valid={
            'recipient':'',
            'nombre_abonado':'Juan Pérez',
            'numero_contrato':'SC-00000',
            'fecha_suspension':'05 de noviembre de 2026',
            'monto_pendiente':'L 1,250.00',
        }
        for label, bad_value in [
            ('nested_dict', {'raw': 'L 1,250.00'}),
            ('nested_list', ['L 1,250.00']),
        ]:
            with self.subTest(case=label), self.assertRaises(ProcessSchemaValidationError):
                NotifyResponse('send', data=dict(base_valid, monto_pendiente=bad_value))
        invalid_cases=[
            ('missing',NotifyResponse('send',data=dict(recipient='',nombre_abonado='Juan Pérez',numero_contrato='SC-00000',fecha_suspension='05 de noviembre de 2026'))),
            ('extra',NotifyResponse('send',data=dict(base_valid,extra_campo='valor'))),
            ('null',NotifyResponse('send',data=dict(base_valid,monto_pendiente=None))),
            ('int',NotifyResponse('send',data=dict(base_valid,monto_pendiente=1250))),
            ('float',NotifyResponse('send',data=dict(base_valid,monto_pendiente=1250.5))),
            ('bool',NotifyResponse('send',data=dict(base_valid,monto_pendiente=True))),
            ('empty',NotifyResponse('send',data=dict(base_valid,monto_pendiente=''))),
            ('spaces',NotifyResponse('send',data=dict(base_valid,monto_pendiente='   '))),
            ('bad_status',NotifyResponse('template',data=dict(base_valid))),
        ]
        for label,response_payload in invalid_cases:
            with self.subTest(case=label):
                case_source=self.contact('Contract '+label)
                _contract_response_box[0]=response_payload
                try:
                    with patch.object(events,'rule_index',return_value={'Emulator Contact':{'after_insert':[rule.name]}}):
                        events.observe(case_source,'after_insert')
                    frappe.db.commit()
                finally:
                    _contract_response_box[0]=None
                self.assertFalse(frappe.db.exists('Notification Queue',{'notification_rule':rule.name,'source_name':case_source.name}))
                op_log=frappe.get_all('Process Execution Log',filters={'source_name':rule.name,'source_document':case_source.name},fields=['name','execution_outcome','diagnostic_detail'])
                self.assertEqual(len(op_log),1)
                self.assertEqual(op_log[0].execution_outcome,'Invalid Schema Response')
                log_detail=op_log[0].diagnostic_detail or ''
                self.assertNotIn('Juan Pérez',log_detail)
                self.assertNotIn('SC-00000',log_detail)

        # Operational skip (send=0) is legitimate and produces zero queue rows
        _contract_response_box[0]=NotifyResponse('skip')
        try:
            with patch.object(events,'rule_index',return_value={'Emulator Contact':{'after_insert':[rule.name]}}):
                events.observe(self.source,'after_insert')
            frappe.db.commit()
        finally:
            _contract_response_box[0]=None
        self.assertFalse(frappe.db.exists('Notification Queue',{'notification_rule':rule.name,'source_name':self.source.name}))

        # Operational invalid data (int instead of str) logs Invalid Schema Response and creates zero queue rows
        bad_source=self.contact('Operational invalid int synthetic')
        _contract_response_box[0]=NotifyResponse('send',data=dict(base_valid,monto_pendiente=999))
        try:
            with patch.object(events,'rule_index',return_value={'Emulator Contact':{'after_insert':[rule.name]}}):
                events.observe(bad_source,'after_insert')
            frappe.db.commit()
        finally:
            _contract_response_box[0]=None
        self.assertFalse(frappe.db.exists('Notification Queue',{'notification_rule':rule.name,'source_name':bad_source.name}))
        op_log=frappe.get_all('Process Execution Log',filters={'source_name':rule.name,'source_document':bad_source.name},fields=['name','execution_outcome'])
        self.assertEqual(len(op_log),1)
        self.assertEqual(op_log[0].execution_outcome,'Invalid Schema Response')

        # Zero-variable template requires empty business variable set in Process data
        zero_tmpl=self.template(header='Aviso',body='Mantenimiento programado hoy.',footer='QOTA',template_variables=[])
        zero_rule_dict=dict(
            candidate_data,
            rule_name='Zero var '+frappe.generate_hash(length=8),
            process_definition='messaging.test_delivery_zero_contract',
            outputs=[dict(status_code='send',output_mode='Template',template_reference=zero_tmpl.name,send=1)],
        )
        ok_zero=process_testing.test_method(dict(zero_rule_dict,__islocal=1),{'name':self.source.name})
        self.logs.append(ok_zero['execution_log'])
        self.assertEqual(ok_zero['status'],'Passed',ok_zero)
        zero_rule=frappe.get_doc(dict(zero_rule_dict,__islocal=1)).insert()
        self.rules.append(zero_rule.name);self.created_rules.append(zero_rule.name);frappe.db.commit()
        zero_bad_source=self.contact('Zero extra operational')
        _contract_response_box[0]=NotifyResponse('send',data=dict(recipient='',extra='no'))
        try:
            with patch.object(events,'rule_index',return_value={'Emulator Contact':{'after_insert':[zero_rule.name]}}):
                events.observe(zero_bad_source,'after_insert')
            frappe.db.commit()
        finally:
            _contract_response_box[0]=None
        self.assertFalse(frappe.db.exists('Notification Queue',{'notification_rule':zero_rule.name,'source_name':zero_bad_source.name}))
        zero_log=frappe.get_all('Process Execution Log',filters={'source_name':zero_rule.name,'source_document':zero_bad_source.name},fields=['name','execution_outcome'])
        self.assertEqual(len(zero_log),1)
        self.assertEqual(zero_log[0].execution_outcome,'Invalid Schema Response')

        # Wording-only Header change invalidates rule approval to Untested (keeping enabled=1) and cancels queued snapshots
        ok_source=self.contact('Operational valid before header edit')
        queue=self.prepare(rule,ok_source)
        tmpl.header='Estimado cliente {{nombre_abonado}}'
        tmpl.save();frappe.db.commit()
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')
        self.assertEqual(process_testing.test_state(rule),'Untested')
        with self.assertRaises(frappe.ValidationError):
            rule.require_approved()
        with patch.object(dispatch,'handoff',side_effect=AssertionError('Modified Header must cancel snapshot')):
            self.assertEqual(self.due(queue)['state'],'Cancelled')
        queue.reload()
        self.assertEqual(queue.diagnostic_code,'delivery_configuration_changed')


    def test_rule_delay_defaults_strict_values_and_server_save(self):
        from messaging.communication.doctype.notification_rule.notification_rule import validate_notification_delay
        self.assertEqual(frappe.new_doc('Notification Rule').notification_delay_minutes, 60)
        rule = self.rule()
        self.assertEqual(rule.notification_delay_minutes, 60)
        for value in (1, 2, 60, 525600, '1', '2', '525600'):
            self.assertEqual(validate_notification_delay(value), int(value))
        for value in (0, '0', -1, 525601, 2.5, 2.0, '', None, True, False, 'abc', '2.0', '2e0', '+2', '02', ' 2', '2 ', '٢', [], {}):
            with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
                validate_notification_delay(value)
            candidate = frappe.get_doc('Notification Rule', rule.name)
            candidate.notification_delay_minutes = value
            with self.assertRaises(frappe.ValidationError):
                candidate.save()
        rule.reload()
        self.assertEqual(rule.notification_delay_minutes, 60)
        self.assertEqual(process_testing.test_state(rule), 'Passed')

    def test_independent_rule_schedules_minimum_and_dispatch_due(self):
        rules = [self.rule(notification_delay_minutes=minutes) for minutes in (2, 60, 1)]
        queues = [self.prepare(rule) for rule in rules]
        for queue, minutes in zip(queues, (2, 60, 1)):
            self.assertEqual(get_datetime(queue.scheduled_at)-get_datetime(queue.event_at), timedelta(minutes=minutes))
            self.assertEqual(queue.scheduled_at, queue.next_attempt_at)
        from unittest.mock import patch
        with patch.object(dispatch, 'deliver', return_value={'state':'selected'}) as deliver:
            instant = min(get_datetime(q.event_at) for q in queues)
            self.assertEqual(dispatch.dispatch_due(names=[q.name for q in queues],now=instant-timedelta(microseconds=1)), [])
            self.assertEqual(deliver.call_count, 0)
            instant = max(get_datetime(q.event_at) for q in queues)
            self.assertEqual(dispatch.dispatch_due(names=[q.name for q in queues],now=instant), [])
            instant = get_datetime(queues[2].scheduled_at)
            self.assertEqual(len(dispatch.dispatch_due(names=[q.name for q in queues],now=instant)), 1)
            self.assertEqual(deliver.call_args.args[0], queues[2].name)
        for q in queues:
            q.reload()
            self.assertEqual(q.status, 'Pending')
        self.assertEqual([frappe.db.get_value('Notification Rule',r.name,'notification_delay_minutes') for r in rules], [2,60,1])

    def test_delay_requires_authentic_approval_without_changing_existing_delivery(self):
        from messaging.communication.doctype.notification_rule.notification_rule import delivery_fingerprint
        rule = self.rule(notification_delay_minutes=60)
        queue = self.prepare(rule)
        snapshot = queue.as_dict()
        destination = notification_destination(rule,self.account,queue.recipient)
        previous_delivery = delivery_fingerprint(rule,destination)
        previous_approval = process_testing.fingerprint(rule)
        rule.notification_delay_minutes = 2
        self.assertNotEqual(process_testing.fingerprint(rule),previous_approval)
        self.assertEqual(delivery_fingerprint(rule,destination),previous_delivery)
        self.assertEqual(process_testing.test_state(rule),'Untested')
        with self.assertRaises(frappe.ValidationError):rule.save()
        rule.reload();rule.status='Disabled';rule.notification_delay_minutes=2;rule.save()
        self.assertEqual(process_testing.test_state(rule),'Untested')
        answer=process_testing.test_method(rule.as_dict())
        self.logs.append(answer['execution_log']);self.assertEqual(answer['status'],'Passed')
        rule.status='Enabled';rule.save();frappe.db.commit()
        self.assertEqual(process_testing.test_state(rule),'Passed')
        queue.reload();self.assertEqual(queue.as_dict(),snapshot)
        source=self.contact('Notify synthetic changed delay')
        fresh=self.prepare(rule,source)
        self.assertEqual(get_datetime(fresh.scheduled_at)-get_datetime(fresh.event_at),timedelta(minutes=2))
        with patch('messaging.communication.doctype.communication_provider.communication_provider.handoff',side_effect=AssertionError('No external transport expected')):
            # Revalidation uses the approved current Process; scheduling stays materialized.
            self.assertIsNone(dispatch.revalidate(queue))
        self.assertEqual(self.due(queue)['state'],'Accepted')
        queue.reload()
        for field in ('event_at','scheduled_at','occurrence_key','prepared_text','variables_json','delivery_fingerprint'):
            self.assertEqual(queue.get(field),snapshot[field])
        with self.assertRaises(frappe.ValidationError):
            from messaging.communication.doctype.notification_queue.notification_queue import internal_write
            with internal_write():
                queue.scheduled_at=get_datetime(queue.scheduled_at)+timedelta(minutes=1)
                queue.save(ignore_permissions=True)

    def test_delivery_hash_still_detects_account_template_conditions_and_destination(self):
        from messaging.communication.doctype.notification_rule.notification_rule import delivery_fingerprint
        rule=self.rule();queue=self.prepare(rule)
        destination=notification_destination(rule,self.account,queue.recipient)
        before=delivery_fingerprint(rule,destination)
        self.assertEqual(queue.delivery_fingerprint,before)
        candidate=frappe.get_doc('Notification Rule',rule.name);candidate.account=self.new_account().name
        self.assertNotEqual(delivery_fingerprint(candidate,destination),before,'account')
        candidate=frappe.get_doc('Notification Rule',rule.name);candidate.outputs[0].template_reference=self.template(body='{{label}}').name
        self.assertNotEqual(delivery_fingerprint(candidate,destination),before,'template_reference')
        candidate=frappe.get_doc('Notification Rule',rule.name)
        candidate.events[0].field_name='enabled';candidate.events[0].value='true'
        self.assertNotEqual(delivery_fingerprint(candidate,destination),before)
        self.assertNotEqual(delivery_fingerprint(rule,dict(destination,effective_recipient='changed')),before)
        rule.events[0].field_name='enabled';rule.events[0].value='true'
        with self.assertRaises(frappe.ValidationError):
            process_testing.test_method(rule.as_dict(),{'name':self.source.name})
        with self.assertRaises(frappe.ValidationError):rule.save()
        frappe.db.rollback();frappe.db.set_value('Notification Queue',queue.name,'delivery_fingerprint','stale-fingerprint',update_modified=False);frappe.db.commit()
        self.assertEqual(self.due(queue)['state'],'Cancelled')
        queue.reload();self.assertEqual(queue.diagnostic_code,'delivery_configuration_changed')

    def test_removed_settings_and_preserved_navigation(self):
        self.assertFalse(frappe.db.exists('DocType','Communication Settings'))
        self.assertEqual(frappe.db.count('Singles',{'doctype':'Communication Settings'}),0)
        sidebar=frappe.get_doc('Sidebar','Communication').as_dict()
        self.assertNotIn('Communication Settings',json.dumps(sidebar,default=str))
        for dt in ('Communication Provider','Communication Channel','Communication Channel Account','Communication Template','Notification Rule','Notification Queue'):
            self.assertTrue(frappe.db.exists('DocType',dt))
            self.assertIn(dt,json.dumps(sidebar,default=str))
        self.assertEqual([f.fieldname for f in frappe.get_meta('Notification Rule Event').fields],['document_event','field_name','value','validation_status'])

    def test_manual_rule_and_queue_series_preserve_code_first_and_legacy(self):
        rule = self.rule()
        self.assertRegex(rule.name, r'^NRL-[0-9]{5}$')
        queue = self.prepare(rule)
        self.assertRegex(queue.name, r'^NTF-[0-9]{6}$')
        self.assertEqual(queue.notification_rule, rule.name)
        self.assertTrue(queue.occurrence_key)
        legacy_queue = frappe.get_all('Notification Queue', filters={'name': ['not like', 'NTF-%']}, pluck='name', limit=1)
        if legacy_queue:
            self.assertTrue(frappe.get_doc('Notification Queue', legacy_queue[0]).occurrence_key)


from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures


class TestPresetNotificationLifecycle(ProcessFixtures, unittest.TestCase):
    def test_localized_references_render_and_participate_in_approval_and_related_rules(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize, selected_output
        from messaging.communication.doctype.notification_rule.events import prepared_data
        from messaging.communication.doctype.notification_rule.notification_rule import set_active as set_rule_active, validate_process
        from messaging.communication.doctype.process_definition.validation import test_state
        from messaging.chatbot.doctype.bot_settings.language import presentation_language
        rule, _source = self.notification()
        rule.process_definition = self.register('empty_notification', 'notification')
        synchronize(rule)
        settings = frappe.get_doc('Bot Settings')
        settings.append('chatbot_languages', dict(language='en', enabled=1, is_default=0))
        settings.save()
        references = {}
        for language in ('es', 'en'):
            template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix,
                language=language, channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
                body='Synthetic ' + language)).insert()
            references[language] = template.name
        rule.outputs[0].template_reference = references['es']
        self.assertEqual(self.approve(rule)['status'], 'Passed')
        foreign = frappe.copy_doc(rule)
        foreign.outputs[0].name = rule.outputs[0].name
        foreign.name = self.prefix + '_foreign'
        with self.assertRaises(frappe.PermissionError):
            synchronize(foreign)
        rule.status = 'Enabled'; rule.save()
        for language in ('es', 'en'):
            with presentation_language(language):
                self.assertEqual(selected_output(rule, 'ready').template_reference, references['es'])
                result = prepared_data(dict(version=1, status='ready', recipient='', data={'action': 'send'}, message=''), rule)
                self.assertEqual(result['prepared_text'], 'Synthetic es')
            tpl_doc = frappe.get_doc('Communication Template', references[language])
            tpl_doc.onload()
            self.assertNotIn('notification_rule_status', tpl_doc.get_onload() or {})
        english = frappe.get_doc('Communication Template', references['es'])
        english.body = 'Changed English content'; english.save()
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')
        self.assertEqual(test_state(rule), 'Untested')
        from messaging.communication.doctype.communication_template.communication_template import set_active
        with self.assertRaisesRegex(frappe.ValidationError, rule.name):
            set_active(dict(name=english.name, modified=str(english.modified)), 0)
        revalidated = validate_process(dict(rule.as_dict()))
        self.assertEqual(revalidated['status'], 'Passed')
        rule.reload()
        self.assertEqual(test_state(rule), 'Passed')
        set_rule_active(dict(rule.as_dict()), 0)
        rule.reload()
        self.assertEqual(rule.status, 'Disabled')
        set_active(dict(name=english.name, modified=str(english.modified)), 0)
        english.reload()
        self.assertEqual(english.status, 'Inactive')
        rule.reload()
        self.assertEqual(test_state(rule), 'Untested')
        with self.assertRaisesRegex(frappe.ValidationError, 'active|Validate'):
            set_rule_active(dict(rule.as_dict()), 1)
        self.network_mock.assert_not_called()

    def test_accountless_draft_can_bind_once_and_cannot_change_bound_structure(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule, _source = self.notification('empty_notification')
        account, parameters = rule.account, [row.as_dict() for row in rule.parameters]
        template = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix + '_draft_bind', language='es', body='Draft bind')).insert()
        rule.account = None; rule.set('parameters', [])
        synchronize(rule)
        rule.outputs[0].template_reference = template.name
        rule.insert()
        self.assertEqual(rule.status, 'Disabled')
        rule.account = account
        with self.assertRaises(frappe.ValidationError):
            rule.save()
        rule.reload()
        self.assertFalse(rule.account)
        rule.account = account
        for row in parameters:
            rule.append('parameters', {key: value for key, value in row.items()
                if key in ('parameter_key', 'value', 'secret_value')})
        rule.save()
        self.assertEqual(rule.account, account)
        rule.account = None
        with self.assertRaisesRegex(frappe.ValidationError, 'immutable'):
            rule.save()


    def test_send_checkbox_governs_notification_emission_regardless_of_status_name(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.events import prepared_data
        rule, _source = self.notification()
        rule.process_definition = self.register('empty_notification', 'notification')
        synchronize(rule)
        template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix,
            language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active', body='Body')).insert()
        rule.outputs[0].template_reference = template.name

        # 1. Functional status 'ready': send=1 yields data, send=0 yields None
        rule.outputs[0].send = 1
        res1 = prepared_data(dict(version=1, status='ready', recipient='', data={'action': 'any'}, message=''), rule)
        self.assertIsNotNone(res1)
        rule.outputs[0].send = 0
        res0 = prepared_data(dict(version=1, status='ready', recipient='', data={'action': 'any'}, message=''), rule)
        self.assertIsNone(res0)

        # 2. Functional status 'skip': send=1 yields data (not suppressed), send=0 yields None
        rule.outputs[0].status_code = 'skip'
        rule.outputs[0].send = 1
        res_skip1 = prepared_data(dict(version=1, status='skip', recipient='', data={'action': 'skip'}, message=''), rule)
        self.assertIsNotNone(res_skip1)
        rule.outputs[0].send = 0
        res_skip0 = prepared_data(dict(version=1, status='skip', recipient='', data={'action': 'skip'}, message=''), rule)
        self.assertIsNone(res_skip0)

        # 3. Functional status 'send': send=1 yields data, send=0 yields None
        rule.outputs[0].status_code = 'send'
        rule.outputs[0].send = 1
        res_send1 = prepared_data(dict(version=1, status='send', recipient='', data={'action': 'send'}, message=''), rule)
        self.assertIsNotNone(res_send1)
        rule.outputs[0].send = 0
        res_send0 = prepared_data(dict(version=1, status='send', recipient='', data={'action': 'send'}, message=''), rule)
        self.assertIsNone(res_send0)

        # 4. Functional status 'rejected': send=1 yields data, send=0 yields None
        rule.outputs[0].status_code = 'rejected'
        rule.outputs[0].send = 1
        res_rej1 = prepared_data(dict(version=1, status='rejected', recipient='', data={}, message=''), rule)
        self.assertIsNotNone(res_rej1)
        rule.outputs[0].send = 0
        res_rej0 = prepared_data(dict(version=1, status='rejected', recipient='', data={}, message=''), rule)
        self.assertIsNone(res_rej0)

    def test_notification_never_uses_process_message_as_content(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize, prepared_data
        rule, _source = self.notification()
        rule.process_definition = self.register('empty_notification', 'notification')
        synchronize(rule)
        rule.outputs[0].template_reference = None
        rule.outputs[0].send = 1
        with self.assertRaises(frappe.ValidationError):
            prepared_data(dict(version=1,status='ready',recipient='',data={},message='Direct process message'),rule)

    def test_process_definition_change_reconciles_parameters_and_outcomes(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule, _source = self.notification()
        rule.process_definition = self.register('empty_notification', 'notification')
        synchronize(rule)
        template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix,
            language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active', body='Body')).insert()
        rule.outputs[0].template_reference = template.name
        self.approve(rule)
        rule.status = 'Enabled'
        rule.save()
        self.assertEqual(rule.process_test_status, 'Passed')

        # Change process definition
        rule.status = 'Disabled'
        rule.process_definition = self.register('component_notification', 'notification')
        synchronize(rule)
        self.assertEqual(rule.process_test_status, 'Untested')
        self.assertIsNone(rule.process_test_fingerprint)

    def test_lifecycle_validation_and_activation_buttons(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.notification_rule import (
            validate_process,
            set_active,
            state as rule_state,
        )
        rule, _source = self.notification()
        rule.process_definition = self.register('empty_notification', 'notification')
        synchronize(rule)
        template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix,
            language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active', body='Body')).insert()
        rule.outputs[0].template_reference = template.name
        rule.status = 'Disabled'
        rule.save()

        # Initial state should be Untested
        st = rule_state(rule.name)
        self.assertEqual(st['lifecycle_status'], 'Untested')

        # Validate process candidate
        res = validate_process(rule.as_dict())
        self.assertEqual(res['status'], 'Passed')
        self.assertEqual(res['lifecycle_status'], 'Enabled')
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')
        self.assertEqual(rule.process_test_status, 'Passed')

        # State should be Enabled
        st = rule_state(rule.name)
        self.assertEqual(st['lifecycle_status'], 'Enabled')

        # Deactivate
        deact = set_active(rule.as_dict(), 0)
        self.assertEqual(deact['lifecycle_status'], 'Disabled')
        rule.reload()
        self.assertEqual(rule.status, 'Disabled')
        st = rule_state(rule.name)
        self.assertEqual(st['lifecycle_status'], 'Disabled')

        # Activate
        act = set_active(rule.as_dict(), 1)
        self.assertEqual(act['lifecycle_status'], 'Enabled')
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')
        st = rule_state(rule.name)
        self.assertEqual(st['lifecycle_status'], 'Enabled')


    def test_three_component_delivery_and_missing_values_use_current_process_contract(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.communication.doctype.notification_rule.events import prepare
        from messaging.communication.doctype.notification_queue.dispatch import deliver
        from messaging.security.execution_context import _automation_context
        from messaging.security.service_identity import SERVICE_USER
        from messaging.common.primitives import utcnow
        from frappe.share import add
        from frappe.utils import get_datetime
        rule, source = self.notification()
        account = frappe.get_doc('Communication Channel Account', rule.account)
        account.enabled = 1; account.save()
        rule.process_definition = self.register('component_notification', 'notification')
        synchronize(rule)
        template = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix, language='es',
            header='Dear {{contact_name}}', body='Contract {{contract_code}} has {{pending_amount}} pending.',
            footer='Thank you.', template_variables=[dict(parameter_name=key)
                for key in ('contact_name', 'contract_code', 'pending_amount')])).insert()
        rule.outputs[0].template_reference = template.name
        self.approve(rule); rule.status = 'Enabled'; rule.save()
        condition = canonical_conditions(rule)[0]
        for scenario in ('ordinary', 'literal', 'missing'):
            with patch.object(events, 'rule_index', return_value={}):
                document = frappe.get_doc(dict(doctype='ToDo', description=scenario, status='Open')).insert(
                    set_name=self.prefix + '_' + scenario)
            add('ToDo', document.name, user=SERVICE_USER, read=1, notify=0)
            key = self.prefix + '-' + scenario
            with _automation_context():
                prepare(rule, document, 'on_update', condition_token(condition), key, utcnow(), condition=condition)
            name = frappe.db.get_value('Notification Queue', {'occurrence_key': key}, 'name')
            if scenario == 'missing':
                self.assertFalse(name)
                continue
            self.assertTrue(name, frappe.get_all('Process Execution Log', filters={'notification_key': key},
                fields=['execution_outcome', 'diagnostic_error_type', 'diagnostic_detail']))
            queue = frappe.get_doc('Notification Queue', name)
            expected_name = 'Juan {{contract_code}}' if scenario == 'literal' else 'Juan Perez'
            expected = 'Dear ' + expected_name + '\n\nContract SC-00000 has L 1,250.00 pending.\n\nThank you.'
            self.assertEqual(queue.prepared_text, expected)
            # Real state transitions and EMULR handoff stay inside the rollback-only fixture.
            with patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'):
                response = deliver(name, now=get_datetime(queue.scheduled_at) + timedelta(seconds=1))
                self.assertEqual(response['state'], 'Accepted')
                queue.reload()
                self.assertEqual(queue.delivery_status, 'Sent')
                with patch('messaging.emulator.provider.send', side_effect=AssertionError('Accepted cannot resend')):
                    self.assertEqual(deliver(name)['state'], 'Accepted')
        self.network_mock.assert_not_called()

    def test_uncertain_notification_reports_preserve_source_identity_and_never_resend(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.communication.doctype.notification_rule.events import prepare
        from messaging.communication.doctype.notification_queue.dispatch import deliver
        from messaging.communication.doctype.communication_channel_account.communication_channel_account import report_result, report_status
        from messaging.security.execution_context import _automation_context, _authenticated_adapter
        from messaging.common.primitives import utcnow
        from frappe.utils import get_datetime
        rule, source = self.notification('empty_notification')
        account = frappe.get_doc('Communication Channel Account', rule.account)
        account.enabled = 1
        next(row for row in account.parameters if row.parameter_key == 'transport_outcome').value = '"Unknown"'
        account.save()
        template = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix, language='es',  body='Synthetic notice.')).insert()
        synchronize(rule)
        rule.outputs[0].output_mode = 'Template'
        rule.outputs[0].template_reference = template.name
        self.approve(rule)
        rule.status = 'Enabled'; rule.save()
        condition = canonical_conditions(rule)[0]
        from frappe.share import add
        from messaging.security.service_identity import SERVICE_USER
        add('ToDo', source.name, user=SERVICE_USER, read=1, notify=0)
        key = self.prefix + '_uncertain'
        with _automation_context():
            prepare(rule, source, 'on_update', condition_token(condition), key, utcnow(), condition=condition)
        name = frappe.db.get_value('Notification Queue', {'occurrence_key': key}, 'name')
        self.assertTrue(name, frappe.get_all('Process Execution Log', filters={'notification_key': key},
            fields=['execution_outcome', 'diagnostic_error_type', 'diagnostic_detail']))
        queue = frappe.get_doc('Notification Queue', name)
        # Exercise the real EMULR handoff and state machine in this reversible transaction.
        with patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'):
            self.assertEqual(deliver(name, now=get_datetime(queue.scheduled_at) + timedelta(seconds=1))['state'], 'Unknown')
            queue.reload()
            reference = queue.transport_reference or 'synthetic-notification'
            with patch('messaging.communication.doctype.notification_queue.dispatch.handoff',
                side_effect=AssertionError('An uncertain handoff must never resend')):
                self.assertEqual(deliver(name)['state'], 'Unknown')
                with _authenticated_adapter('EMULR', account.name):
                    recovered = report_result('EMULR', account.name, name,
                        dict(outcome='Accepted', reference=reference), source_type='Notification Queue')
                    self.assertEqual(recovered['state'], 'Accepted')
                    report_status('EMULR', account.name, name, 'Read', reference, source_type='Notification Queue')
                    report_status('EMULR', account.name, name, 'Failed', reference, source_type='Notification Queue')
                    with self.assertRaises(frappe.ValidationError):
                        report_status('EMULR', account.name, name, 'Read', 'foreign-reference', source_type='Notification Queue')
                self.assertEqual(deliver(name)['state'], 'Accepted')
        queue.reload()
        self.assertEqual(queue.delivery_status, 'Read')
        self.assertEqual(frappe.db.count('Notification Queue', {'occurrence_key': key}), 1)
        self.network_mock.assert_not_called()


class TestRegisteredNotificationRules(ProcessFixtures, unittest.TestCase):
    """Automatic documentary synchronization retains Send and administrative safeguards."""
    def declaration(self):
        process_id = self.register('technical_skip', 'notification')
        return dict(key='messaging.' + self.prefix, name='Registered test rule', process=process_id,
            source_application='frappe', source_doctype='ToDo', events=[{'document_event': 'on_update'}],
            outcomes={'skip': {'mode': 'template', 'send': False}}, notification_delay_minutes=60)

    def test_automatic_install_is_idempotent_disabled_and_does_not_enqueue(self):
        from messaging.communication.doctype.notification_rule.notification_rule import save_rule
        declaration = self.declaration()
        queue = frappe.db.count('Notification Queue')
        with patch('messaging.communication.doctype.process_definition.execution.execute_process', side_effect=AssertionError('No execution')):
            result = save_rule(**declaration)
            self.assertEqual(result['status'], 'created')
            rule = frappe.get_doc('Notification Rule', result['name'])
            self.assertEqual(rule.status, 'Disabled')
            self.assertFalse(rule.outputs[0].send)
            self.assertFalse(rule.outputs[0].template_reference)
            self.assertEqual(rule.process_test_status, 'Untested')
            modified = rule.modified
            self.assertEqual(save_rule(**declaration)['status'], 'existing')
            self.assertEqual(frappe.get_doc('Notification Rule', rule.name).modified, modified)
        self.assertEqual(queue, frappe.db.count('Notification Queue'))

    def test_changed_defaults_preserve_manual_edits(self):
        from messaging.communication.doctype.notification_rule.notification_rule import save_rule
        declaration = self.declaration()
        save_rule(**declaration)
        rule = frappe.get_doc('Notification Rule', declaration['key'])
        template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix,
            channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), language='es', status='Inactive', body='Administrative decision')).insert()
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = template.name
        rule.save()
        result = save_rule(**{**declaration, 'name': 'Changed default'})
        self.assertEqual(result['status'], 'administrative_changes')
        self.assertTrue(frappe.get_doc('Notification Rule', rule.name).outputs[0].send)

    def test_changed_unedited_defaults_are_applied_without_activation(self):
        from messaging.communication.doctype.notification_rule.notification_rule import save_rule
        declaration = self.declaration()
        save_rule(**declaration)
        result = save_rule(**{**declaration, 'notification_delay_minutes': 90})
        self.assertEqual(result['status'], 'updated')
        rule = frappe.get_doc('Notification Rule', declaration['key'])
        self.assertEqual(rule.notification_delay_minutes, 90)
        self.assertEqual(rule.status, 'Disabled')

    def test_unknown_outcome_is_rejected_without_a_document(self):
        from messaging.communication.doctype.notification_rule.notification_rule import save_rule
        declaration = self.declaration()
        declaration['outcomes']['extra'] = {'mode': 'template', 'send': False}
        with self.assertRaises(frappe.ValidationError):
            save_rule(**declaration)
        self.assertFalse(frappe.db.exists('Notification Rule', declaration['key']))


def semantic_notification(*, params, outcomes):
    outcomes.register('notice_ready', fields={'recipient': 'string', 'detail': 'string'}, mode=['template'])


def semantic_notification_result(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    scenario = request.source.get_document().description
    if scenario == 'no_notice':
        return NotifyNoResponse('not_eligible')
    if scenario == 'technical_failure':
        raise RuntimeError('private-notification-diagnostic')
    return NotifyResponse('notice_ready', data={'recipient': '', 'detail': 'Recorded business fact'})


class TestNotificationOutcomeSafety(ProcessFixtures, unittest.TestCase):
    """Documentary requirements and notification completion share the real Runtime."""
    def configured_rule(self):
        from messaging.registry.processes import NotifyProcess
        from messaging.registry.processes import register_processes
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule, source = self.notification()
        identifier = 'messaging.' + self.prefix + '_semantic'
        self.definitions.append(NotifyProcess(id=identifier, label='Semantic notice', definition=semantic_notification, handler=semantic_notification_result))
        register_processes(app='messaging', processes=self.definitions)
        rule.process_definition = identifier
        synchronize(rule)
        account = frappe.get_doc('Communication Channel Account', rule.account)
        account.enabled = 1
        account.save()
        template = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix, language='es',
            body='Notice: {{detail}}', template_variables=[dict(parameter_name='detail')])).insert()
        rule.outputs[0].template_reference = template.name
        return rule, source

    def prepare(self, rule, source, scenario):
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.security.execution_context import _automation_context
        from messaging.security.service_identity import SERVICE_USER
        from messaging.common.primitives import utcnow
        frappe.db.set_value('ToDo', source.name, {'description': scenario, 'allocated_to': SERVICE_USER})
        source.reload()
        frappe.share.add('ToDo', source.name, user=SERVICE_USER, read=1, notify=0)
        condition = canonical_conditions(rule)[0]
        key = self.prefix + '_' + scenario
        with _automation_context():
            events.prepare(rule, source, 'on_update', condition_token(condition), key, utcnow(), condition=condition)
        return key

    def test_template_is_required_on_save_approval_and_enable_and_variables_are_checked(self):
        from messaging.communication.doctype.notification_rule.notification_rule import validate_process
        rule, _source = self.configured_rule()
        template = rule.outputs[0].template_reference
        rule.outputs[0].send = 0
        rule.outputs[0].template_reference = None
        rule.save()
        self.assertFalse(rule.outputs[0].template_reference)
        rule.outputs[0].send = 1
        rule.save()
        self.assertEqual(rule.status, 'Disabled')
        frappe.db.set_value('Notification Outcome', rule.outputs[0].name, 'send', 1)
        rule.reload()
        with self.assertRaisesRegex(frappe.ValidationError, 'notice_ready'):
            validate_process(rule.as_dict())
        invalid = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix + '_invalid', language='es',
            body='{{missing}}', template_variables=[dict(parameter_name='missing')])).insert()
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = invalid.name
        with self.assertRaises(frappe.ValidationError):
            rule.save()
        rule.reload()
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = template
        self.assertEqual(self.approve(rule)['status'], 'Passed')
        rule.status = 'Enabled'
        rule.save()

    def test_controlled_completion_and_real_failure_create_no_queue_and_distinct_logs(self):
        rule, source = self.configured_rule()
        self.assertEqual(self.approve(rule)['status'], 'Passed')
        rule.status = 'Enabled'
        rule.save()
        for scenario, expected in (('no_notice', 'No Result'), ('technical_failure', 'Internal Execution Error')):
            key = self.prepare(rule, source, scenario)
            self.assertFalse(frappe.db.exists('Notification Queue', {'occurrence_key': key}))
            logs = frappe.get_all('Process Execution Log', filters={'notification_key': key},
                fields=['execution_outcome', 'functional_status', 'diagnostic_detail', 'diagnostic_error_type', 'frappe_error_log'])
            self.assertEqual(len(logs), 1, logs)
            log = logs[0]
            self.assertEqual(log.execution_outcome, expected, log)
            self.assertFalse(log.functional_status)
            if scenario == 'no_notice':
                self.assertIn('not_eligible', log.diagnostic_detail)
                self.assertFalse(log.frappe_error_log)
            else:
                self.assertEqual(log.diagnostic_error_type, 'RuntimeError')
                self.assertTrue(log.frappe_error_log)
                trace = frappe.db.get_value('Error Log', log.frappe_error_log, 'error')
                self.assertIn('semantic_notification_result', trace)
                self.assertNotIn('private-notification-diagnostic', trace)
        self.network_mock.assert_not_called()

    def test_pending_notice_losing_eligibility_is_cancelled_without_handoff(self):
        rule, source = self.configured_rule()
        self.approve(rule)
        rule.status = 'Enabled'
        rule.save()
        key = self.prepare(rule, source, 'ready')
        name = frappe.db.get_value('Notification Queue', {'occurrence_key': key}, 'name')
        self.assertTrue(name)
        queue = frappe.get_doc('Notification Queue', name)
        frappe.db.set_value('ToDo', source.name, 'description', 'no_notice')
        with patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'), \
                patch.object(dispatch, 'handoff', side_effect=AssertionError('No eligible notice')):
            answer = dispatch.deliver(name, now=get_datetime(queue.scheduled_at) + timedelta(seconds=1))
        queue.reload()
        self.assertEqual(answer['state'], 'Cancelled')
        self.assertEqual(queue.diagnostic_code, 'event_no_longer_eligible')
        self.assertFalse(queue.handoff_at)

    def test_delivery_failure_after_enqueue_remains_on_the_existing_queue(self):
        from messaging.communication.doctype.communication_channel_account.communication_channel_account import report_status
        from messaging.security.execution_context import _authenticated_adapter
        rule, source = self.configured_rule()
        self.approve(rule)
        rule.status = 'Enabled'
        rule.save()
        key = self.prepare(rule, source, 'ready')
        name = frappe.db.get_value('Notification Queue', {'occurrence_key': key}, 'name')
        self.assertTrue(name)
        queue = frappe.get_doc('Notification Queue', name)
        with patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'):
            self.assertEqual(dispatch.deliver(name, now=get_datetime(queue.scheduled_at) + timedelta(seconds=1))['state'], 'Accepted')
            queue.reload()
            with _authenticated_adapter('EMULR', rule.account):
                report_status('EMULR', rule.account, name, 'Failed', queue.transport_reference,
                    source_type='Notification Queue')
            with patch.object(dispatch, 'handoff', side_effect=AssertionError('No resend after failure')):
                self.assertEqual(dispatch.deliver(name)['state'], 'Accepted')
        queue.reload()
        self.assertEqual(queue.delivery_status, 'Failed')
        self.assertEqual(frappe.db.count('Notification Queue', {'occurrence_key': key}), 1)
        self.network_mock.assert_not_called()


def enablement_notice_def(*, params, outcomes):
    outcomes.register('notice_ready', fields={'recipient': 'string', 'detail': 'string'}, mode=['template'])
    outcomes.register('skip_notice', fields={'recipient': 'string'}, mode=['template'])


def enablement_notice_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    scenario = request.source.get_document().description
    if scenario == 'skip_outcome':
        return NotifyResponse('skip_notice', data={'recipient': ''})
    if scenario == 'no_response':
        return NotifyNoResponse('notice_not_issued')
    return NotifyResponse('notice_ready', data={'recipient': '', 'detail': 'Synthetic fact'})


def skip_only_notice_def(*, params, outcomes):
    outcomes.register('skip', fields={'recipient': 'string'}, mode=['template'])


def skip_only_notice_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyNoResponse('notice_not_issued')


class TestNotificationRuleEnablementInvariants(ProcessFixtures, unittest.TestCase):
    """Notification Rule enablement requires outputs, send=1, and valid compatible templates."""

    def setup_multilang_rule(self, *, with_en=True):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule, source = self.notification()
        if with_en:
            settings = frappe.get_single('Bot Settings')
            if not any(r.language == 'en' for r in settings.chatbot_languages):
                settings.append('chatbot_languages', dict(language='en', enabled=1, is_default=0))
                for key in MESSAGE_KEYS:
                    settings.append('message_translations', dict(message_key=key, language='en', message='Synthetic en ' + key))
                settings.save()
        identifier = 'messaging.' + self.prefix + '_enablement'
        self.definitions.append(NotifyProcess(
            id=identifier, label='Enablement notice',
            definition=enablement_notice_def, handler=enablement_notice_handler,
        ))
        register_processes(app='messaging', processes=self.definitions)
        rule.process_definition = identifier
        synchronize(rule)
        account = frappe.get_doc('Communication Channel Account', rule.account)
        account.enabled = 1
        account.save()
        return rule, source

    def make_template(self, suffix='tpl', language='es', status='Active', body='Detail: {{detail}}', variables=None):
        if variables is None:
            variables = [dict(parameter_name='detail')] if '{{detail}}' in body else []
        return frappe.get_doc(dict(
            doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status=status,
            template_name=self.prefix + '_' + suffix, language=language,
            body=body, template_variables=variables,
        )).insert()

    def test_enablement_conditions_cumulative_across_save_and_api(self):
        import frappe.client
        from messaging.communication.doctype.notification_rule.notification_rule import set_active, validate_process

        rule, _source = self.setup_multilang_rule(with_en=True)
        tpl_es = self.make_template('shared', language='es')
        tpl_en = self.make_template('shared', language='en')
        tpl_skip_es = self.make_template('skip_only', language='es', body='Skip only', variables=[])
        tpl_skip_en = self.make_template('skip_only', language='en', body='Skip only EN', variables=[])

        # 6) Disabled incomplete draft (all send=0, no templates) can be saved
        for row in rule.outputs:
            row.send = 0
            row.template_reference = None
        rule.status = 'Disabled'
        rule.insert()
        self.assertEqual(rule.status, 'Disabled')

        # 1) Enabled rule without outputs -> rejected on insert, save, set_active, validate_process
        empty_rule = frappe.copy_doc(rule)
        empty_rule.rule_name = rule.rule_name + ' empty'
        empty_rule.status = 'Enabled'
        empty_rule.set('outputs', [])
        with self.assertRaises(frappe.ValidationError):
            empty_rule.insert()
        rule.reload()
        rule.status = 'Enabled'
        rule.set('outputs', [])
        with self.assertRaises(frappe.ValidationError):
            rule.save()
        rule.reload()

        # 2) Enabled rule with outputs, without template -> rejected
        rule.status = 'Enabled'
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = None
        with self.assertRaises(frappe.ValidationError):
            rule.save()
        rule.reload()

        # 3) Enabled rule with valid template, but ALL outputs send=0 -> rejected across all endpoints
        rule.outputs[0].send = 0
        rule.outputs[0].template_reference = tpl_es.name
        rule.outputs[1].send = 0
        rule.outputs[1].template_reference = None
        rule.save()
        rule.status = 'Enabled'
        with self.assertRaises(frappe.ValidationError):
            rule.save()
        rule.reload()
        with self.assertRaises(frappe.ValidationError):
            validate_process(rule.as_dict())
        with self.assertRaises(frappe.ValidationError):
            set_active(rule.as_dict(), 1)
        with self.assertRaises(frappe.ValidationError):
            frappe.client.set_value('Notification Rule', rule.name, 'status', 'Enabled')

        # 4) Template ONLY on a non-sending output (skip_notice send=0 has template, notice_ready send=0 or missing) -> rejected
        rule.reload()
        rule.outputs[0].send = 0
        rule.outputs[0].template_reference = None
        rule.outputs[1].send = 0
        rule.outputs[1].template_reference = tpl_skip_es.name
        rule.save()
        with self.assertRaises(frappe.ValidationError):
            validate_process(rule.as_dict())
        with self.assertRaises(frappe.ValidationError):
            set_active(rule.as_dict(), 1)
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = None
        with self.assertRaises(frappe.ValidationError):
            validate_process(rule.as_dict())
        rule.reload()

        # 5) Rule with send=1 output + valid templates for all enabled languages -> passes validate_process & activates
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = tpl_es.name
        rule.outputs[1].send = 0
        rule.outputs[1].template_reference = None
        rule.save()
        res = validate_process(rule.as_dict())
        self.assertEqual(res['status'], 'Passed')
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')

        # 7) Removing the last sendable output on an already-enabled rule -> save & db_update rejected, DB intact
        rule.outputs[0].send = 0
        with self.assertRaises(frappe.ValidationError):
            rule.save()
        rule.reload()
        rule.outputs[0].send = 0
        rule.outputs[0].template_reference = None
        with self.assertRaises(frappe.ValidationError):
            rule.db_update()
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')
        self.assertEqual(rule.outputs[0].send, 1)
        self.assertEqual(rule.outputs[0].template_reference, tpl_es.name)

        # Verify tpl_skip_en / tpl_en stay linked cleanly
        self.assertTrue(frappe.db.exists('Communication Template', tpl_en.name))
        self.assertTrue(frappe.db.exists('Communication Template', tpl_skip_en.name))

    def test_template_invalidation_missing_variant_and_skip_no_queue(self):
        from messaging.communication.doctype.communication_template.communication_template import set_active as set_tpl_active
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.communication.doctype.notification_rule.notification_rule import set_active, validate_process
        from messaging.security.execution_context import _automation_context
        from messaging.security.service_identity import SERVICE_USER
        from messaging.common.primitives import utcnow

        rule, source = self.setup_multilang_rule(with_en=True)
        tpl_es = self.make_template('lang', language='es')
        rule.outputs[0].send = 1
        rule.outputs[0].template_reference = tpl_es.name
        rule.outputs[1].send = 0
        rule.save()

        # A single Spanish template is sufficient with Spanish and English configured.
        self.assertEqual(validate_process(rule.as_dict())['status'], 'Passed')
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')
        tpl_en = self.make_template('lang', language='en')
        self.assertEqual(rule.test_state(), 'Passed')

        # 8b) Deactivation protection refuses deactivating template while referenced by enabled send=1 rule
        with self.assertRaisesRegex(frappe.ValidationError, rule.name):
            set_tpl_active(dict(name=tpl_es.name, modified=str(tpl_es.modified)), 0)

        # 8c) If template becomes invalid/inactive after disabling rule, reactivation is rejected
        set_active(rule.as_dict(), 0)
        rule.reload()
        tpl_es.reload()
        set_tpl_active(dict(name=tpl_es.name, modified=str(tpl_es.modified)), 0)
        rule.reload()
        self.assertEqual(rule.test_state(remember_invalid=True), 'Untested')
        with self.assertRaises(frappe.ValidationError):
            validate_process(rule.as_dict())
        with self.assertRaises(frappe.ValidationError):
            set_active(rule.as_dict(), 1)

        # Re-activate the selected template and enable rule to test skip / NotifyNoResponse runtime queue gate
        tpl_es.reload()
        set_tpl_active(dict(name=tpl_es.name, modified=str(tpl_es.modified)), 1)
        rule.reload()
        self.assertEqual(rule.test_state(remember_invalid=True), 'Passed')
        set_active(rule.as_dict(), 1)
        rule.reload()
        self.assertEqual(rule.status, 'Enabled')

        # 10) Process returning skip (send=0) or NotifyNoResponse creates ZERO Notification Queue rows
        nq_before = frappe.db.count('Notification Queue')
        frappe.share.add('ToDo', source.name, user=SERVICE_USER, read=1, notify=0)
        condition = canonical_conditions(rule)[0]
        for idx, scenario in enumerate(('skip_outcome', 'no_response')):
            frappe.db.set_value('ToDo', source.name, 'description', scenario)
            source.reload()
            key = f'{self.prefix}_skip_{idx}'
            with _automation_context():
                events.prepare(rule, source, 'on_update', condition_token(condition), key, utcnow(), condition=condition)
        self.assertEqual(frappe.db.count('Notification Queue'), nq_before)

        # And a skip-only Process (like NotifyNoResponse / send=0) cannot bypass enablement invariants
        skip_rule, _src2 = self.notification('empty_notification')
        skip_id = 'messaging.' + self.prefix + '_skip_only'
        self.definitions.append(NotifyProcess(
            id=skip_id, label='Skip only notice',
            definition=skip_only_notice_def, handler=skip_only_notice_handler,
        ))
        register_processes(app='messaging', processes=self.definitions)
        skip_rule.process_definition = skip_id
        from messaging.communication.doctype.notification_rule.contract import synchronize
        synchronize(skip_rule)
        skip_rule.outputs[0].send = 0
        skip_rule.outputs[0].template_reference = None
        skip_rule.status = 'Disabled'
        skip_rule.insert()
        with self.assertRaises(frappe.ValidationError):
            validate_process(skip_rule.as_dict())
        with self.assertRaises(frappe.ValidationError):
            set_active(skip_rule.as_dict(), 1)
        self.assertEqual(frappe.db.count('Notification Queue'), nq_before)
        self.network_mock.assert_not_called()


class TestSelectedNotificationChannel(ProcessFixtures, unittest.TestCase):
    """Exact channel ownership and selected content remain server-side requirements."""
    setup_multilang_rule = TestNotificationRuleEnablementInvariants.setup_multilang_rule
    make_template = TestNotificationRuleEnablementInvariants.make_template

    def test_selected_language_is_independent_of_bot_settings_and_contact_scope(self):
        from messaging.communication.doctype.notification_rule.contract import validate_notification_contract, prepared_data
        from messaging.communication.doctype.notification_rule.notification_rule import validate_process
        from messaging.chatbot.doctype.bot_settings.language import presentation_language
        rule, _source = self.setup_multilang_rule(with_en=True)
        template = self.make_template('only_english', language='en', body='Notice {{detail}}')
        for row in rule.outputs:
            row.send = int(row.status_code == 'notice_ready')
        rule.outputs[0].template_reference = template.name
        rule.save()
        with patch('messaging.chatbot.doctype.bot_settings.language.get_bot_settings', side_effect=AssertionError('Notifications cannot depend on Bot Settings')):
            validate_notification_contract(rule)
            self.assertEqual(validate_process(rule.as_dict())['status'], 'Passed')
            rule.reload()
            for language in ('es', 'en', None):
                with presentation_language(language):
                    result = prepared_data(dict(version=1, status='notice_ready', recipient='', data={'detail': 'Selected English'}, message=''), rule)
                    self.assertEqual(result['prepared_text'], 'Notice Selected English')
            self.assertEqual(rule.test_state(), 'Passed')

    def test_same_provider_other_channel_rejected_on_save_validation_and_render(self):
        from messaging.communication.doctype.notification_rule.contract import validate_notification_contract, prepared_data
        from messaging.communication.doctype.communication_template.communication_template import validate_template_fields
        from messaging.communication.doctype.communication_template.status import notification_template_gate
        rule, _source = self.setup_multilang_rule()
        template = self.make_template('same_channel')
        for row in rule.outputs:
            row.send = int(row.status_code == 'notice_ready')
        rule.outputs[0].template_reference = template.name
        rule.save()
        validate_notification_contract(rule)
        native_get = frappe.get_doc
        # An isolated model fixture preserves EMULR's single-channel declaration.
        other = frappe.copy_doc(template)
        other.name = template.name
        other.channel = 'synthetic-other-channel'
        def get(*args, **kwargs):
            if args[:2] == ('Communication Template', template.name):
                return other
            return native_get(*args, **kwargs)
        native_value = frappe.db.get_value
        def value(*args, **kwargs):
            if args[:3] == ('Communication Channel', other.channel, 'provider'):
                return template.provider
            return native_value(*args, **kwargs)
        with patch.object(frappe, 'get_doc', side_effect=get), patch.object(frappe.db, 'get_value', side_effect=value):
            self.assertEqual(other.provider, template.provider)
            with self.assertRaisesRegex(frappe.ValidationError, 'channel'):
                rule.save()
            with self.assertRaisesRegex(frappe.ValidationError, 'channel'):
                validate_notification_contract(rule)
            with self.assertRaises(frappe.ValidationError):
                validate_template_fields(template.name, {'detail': 'string'}, usage='Notification', account=rule.account)
            with self.assertRaises(frappe.ValidationError):
                prepared_data(dict(version=1, status='notice_ready', recipient='', data={'detail': 'Rejected'}, message=''), rule)
            self.assertEqual(notification_template_gate(template.name, rule.account), 'template_channel_mismatch')
        self.assertFalse(frappe.db.exists('Communication Channel', other.channel))

    def test_notification_message_variable_does_not_change_bot_flow_contract(self):
        from messaging.communication.doctype.communication_template.communication_template import validate_template_content
        template = self.make_template('business_message', body='{{message}}', variables=[dict(parameter_name='message')])
        validate_template_content(template)
        bot = frappe.get_doc(dict(doctype='Bot Flow Template', language='es', enabled=1, body='{{message}}', template_variables=[dict(parameter_name='message')]))
        with self.assertRaisesRegex(frappe.ValidationError, 'reserved'):
            validate_template_content(bot)
