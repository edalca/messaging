from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
"""Managed sender and generic emulator account regressions through neutral boundaries."""
import json
import unittest
from unittest.mock import patch
from pathlib import Path

import frappe

from messaging.emulator.page.emulator_console import emulator_console as console
from messaging.conversation.doctype.chat_turn import ingress as turn_ingress
from messaging.chatbot.doctype.bot_dispatch import delivery as bot_delivery
from messaging.emulator.doctype.emulator_contact.emulator_contact import CHANNEL, PROVIDER
from messaging.emulator.doctype.emulator_contact.fixtures import (
	bind_emulator,
	emulator_account,
	emulator_contact,
	emulator_message,
	release_emulator,
)
from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
from messaging.chatbot.doctype.bot_settings.interpretation import Decision
from messaging.conversation.doctype.chat_turn import test_chat_turn as native
from messaging.conversation.doctype.chat_session import inbox, human


class TestClientEvidenceTiming(unittest.TestCase):
	def test_read_proof_keeps_server_dwell_and_expiry_limits(self):
		from messaging.emulator.provider import validate_client_receipt
		from frappe.utils.verified_command import get_signed_params
		# Only the clock is exercised; no account, contact or conversation is created.
		proof = get_signed_params(json.dumps(dict(stage='Read', issued_at=1000)))
		with patch('messaging.emulator.provider.client_receipt', return_value=proof):
			for at in (1000.699, 1030.001):
				with self.subTest(at=at), patch('messaging.emulator.provider.time.time', return_value=at):
					with self.assertRaises(frappe.PermissionError):
						validate_client_receipt('account', 'sender', 'Bot Dispatch', 'outbound', proof, 'Read')
			for at in (1000.7, 1030):
				with self.subTest(at=at), patch('messaging.emulator.provider.time.time', return_value=at):
					validate_client_receipt('account', 'sender', 'Bot Dispatch', 'outbound', proof, 'Read')


def concurrent_receipt(site, sites_path, args, barrier, results):
	frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
	try:
		barrier.wait(timeout=15)
		results.put(console.client_status(**args))
	except Exception as exc:
		results.put(dict(error=type(exc).__name__))
	finally:
		frappe.db.rollback(); frappe.destroy()


class TestEmulatorContact(unittest.TestCase):
	configure=native.TestChatTurn.configure
	ensure_test_channel=native.TestChatTurn.ensure_test_channel
	flow=native.TestChatTurn.flow
	parameter=native.TestChatTurn.parameter

	def setUp(self):
		native.TestChatTurn.setUp(self)
		bind_emulator(self)

	def natural_mode(self):
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
		configuration = create_configuration(self.prefix + '_emulator_ai', api_key='synthetic-emulator-key')
		self.configure(interpretation_enabled=1, active_ai_configuration=configuration.name)
		def cleanup():
			frappe.set_user('Administrator')
			frappe.delete_doc('AI Configuration', configuration.name, delete_permanently=True)
			frappe.db.commit()
		self.addCleanup(cleanup)

	def account(self, outcome='Accepted'):
		return emulator_account(self, outcome)

	def contact(self, **values):
		return emulator_contact(self, **values)

	def message(self, account=None, sender=None, text='Hello', client_id=None, **kwargs):
		# Exercise the worker explicitly; the public ingress itself returns only its receipt.
		return emulator_message(self, account, sender, text, client_id, **kwargs)

	def tearDown(self):
		release_emulator(self, lambda: native.TestChatTurn.tearDown(self))

	def test_generic_accounts_fixed_channel_contacts_and_navigation(self):
		self.assertFalse(frappe.db.exists('DocType','Emulator Account'))
		self.assertEqual(frappe.db.count('Communication Channel',{'provider':PROVIDER}),1)
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(dict(doctype='Communication Channel',provider=PROVIDER,channel_name='Duplicate')).insert()
		self.assertEqual({r.name for r in console.accounts()['accounts']},{self.a,self.b} | set(self.existing_accounts))
		self.assertIn(self.sender, {row.sender_id for row in console.contacts()})
		with self.assertRaises((frappe.DuplicateEntryError,frappe.UniqueValidationError)):
			frappe.get_doc(dict(doctype='Emulator Contact',sender_id=self.sender,display_name='Duplicate')).insert()
		frappe.db.rollback()
		for sender in ['Administrator','Guest','../invalid','real@example.invalid','+50499999999','50499999999','111123456789012345']:
			with self.assertRaises(frappe.ValidationError):frappe.get_doc(dict(doctype='Emulator Contact',sender_id=sender,display_name='Invalid')).insert()
		root=Path(frappe.get_app_path('messaging'))
		sidebar=json.loads((root/'conversation/sidebar/conversation/conversation.json').read_text())
		items=sidebar['items'];section=next(i for i,r in enumerate(items) if r.get('label')=='Emulator')
		self.assertEqual([r['link_to'] for r in items[section+1:section+3]],['emulator-console','Emulator Contact'])
		dock=json.loads((root/'dock/messaging/messaging.json').read_text());self.assertEqual([r['link_to'] for r in dock['items']],['Conversation','Chatbot','Communication'])
		self.assertEqual(frappe.get_meta('Emulator Contact').module, 'Emulator')
		self.assertEqual(frappe.db.get_value('Page', 'emulator-console', 'module'), 'Emulator')
		self.assertTrue(frappe.db.exists('Module Def', {'name': 'Emulator', 'app_name': 'messaging'}))
		self.assertFalse((root/'channel_emulator').exists())
		self.assertFalse(frappe.db.exists('Workspace','Channel Emulator'));self.assertFalse(frappe.db.exists('Sidebar','Channel Emulator'))

	def test_global_contact_generated_invalid_number_and_optional_name(self):
		from messaging.emulator.doctype.emulator_contact.emulator_contact import new_number
		meta=frappe.get_meta('Emulator Contact')
		self.assertIsNone(meta.get_field('account'))
		self.assertEqual(meta.get_field('display_name').label,'Contact Name')
		self.assertFalse(meta.get_field('display_name').reqd)
		first=frappe.get_doc('Emulator Contact',self.contacts[0])
		self.assertRegex(first.sender_id,r'^000[0-9]{15}$')
		self.assertGreater(len(first.sender_id),15)
		self.assertTrue(first.display_name.strip())
		second=self.contact(display_name='   ')
		self.assertNotEqual(first.sender_id,second.sender_id)
		self.assertTrue(second.display_name.strip())
		second.display_name='Chosen contact name';second.save();second.reload()
		self.assertEqual(second.display_name,'Chosen contact name')
		second.display_name='';second.save();second.reload()
		self.assertTrue(second.display_name.strip())
		self.assertEqual(new_number()[:3],'000')
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):new_number()
		frappe.set_user('Administrator')

	def test_managed_sender_isolation_enablement_and_canonical_history(self):
		one=self.message(client_id='managed-contact-event-001');duplicate=self.message(client_id='managed-contact-event-001')
		two=self.message(account=self.b)
		self.assertEqual(one['turn'],duplicate['turn']);self.assertNotEqual(one['contact'],two['contact'])
		self.assertEqual(frappe.db.get_value('Chat Contact',one['contact'],'account'),self.a)
		with self.assertRaises(frappe.PermissionError):console.history(self.a,self.sender,session=two['session'])
		with self.assertRaises(frappe.PermissionError):self.message(sender='000999999999999999')
		contact=frappe.get_doc('Emulator Contact',self.contacts[0]);contact.enabled=0;contact.save();frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):self.message()
		with self.assertRaises(frappe.PermissionError):self.message(account=self.b)
		self.assertNotIn(self.sender, {row.sender_id for row in console.contacts()})
		self.assertEqual(console.history(self.a,self.sender)['session'],one['session'])
		with self.assertRaises(frappe.ValidationError):frappe.delete_doc('Emulator Contact',contact.name)
		contact.sender_id='changed'
		with self.assertRaises(frappe.ValidationError):contact.save()
		with self.assertRaises(frappe.ValidationError):frappe.delete_doc('Communication Channel Account',self.a)

	def test_channel_account_provider_and_user_authorization(self):
		for target in ['provider','channel','account']:
			if target=='provider':set_provider_enabled(PROVIDER,False)
			else:
				doc=frappe.get_doc('Communication Channel' if target=='channel' else 'Communication Channel Account',CHANNEL if target=='channel' else self.a)
				doc.enabled=0;doc.save()
			frappe.db.commit()
			try:
				with self.assertRaises(frappe.PermissionError):self.message()
			finally:
				if target=='provider':set_provider_enabled(PROVIDER,True)
				else:doc.reload();doc.enabled=1;doc.save()
				frappe.db.commit()
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):console.accounts()
		with self.assertRaises(frappe.PermissionError):self.message()

	def test_interactive_automatic_reply_and_accepted_rejected_unknown_statuses(self):
		self.natural_mode()
		flow=self.flow()
		first=self.message()
		menu=next(m for m in console.history(self.a,self.sender)['messages'] if m['options'])
		option=next(o for o in menu['options'] if o['label']==flow.workflow_name)
		chosen=self.message(text=option['label'],dispatch=menu['dispatch'],selection_id=option['id'])
		self.assertIn('Result hours.',chosen['response'])
		with self.assertRaises(frappe.PermissionError):self.message(account=self.b,dispatch=menu['dispatch'],selection_id=option['id'])
		for outcome in ['Rejected','Unknown']:
			account=self.account(outcome);result=self.message(account=account)
			self.assertEqual(frappe.db.get_value('Bot Dispatch',result['dispatch'],'status'),outcome)
			with patch('messaging.chatbot.doctype.bot_dispatch.delivery.handoff',side_effect=AssertionError('Unsafe resend')):
				self.assertEqual(bot_delivery.deliver(result['dispatch'])['state'],outcome)
			if outcome=='Unknown':self.assertEqual(console.simulate_result(account,self.sender,result['dispatch'],'Accepted')['state'],'Accepted')
		with self.assertRaises(frappe.PermissionError): console.simulate_status(self.a,self.sender,first['dispatch'],'Read')
		self.assertEqual(console.simulate_status(self.a,self.sender,first['dispatch'],'Delivered')['delivery'],'Delivered')
		self.assertEqual(console.simulate_status(self.a,self.sender,first['dispatch'],'Sent')['delivery'],'Delivered')

	def test_human_reply_pause_close_and_bot_return_keep_the_correct_sender(self):
		self.natural_mode()
		user=self.prefix+'@example.invalid'
		with patch('frappe.enqueue'):
			frappe.get_doc(dict(doctype='User',email=user,first_name='Synthetic agent',send_welcome_email=0,user_type='System User',roles=[dict(role='Messaging Agent')])).insert()
		self.users.append(user)
		frappe.get_doc(dict(doctype='Chat Agent',user=user,channel=CHANNEL,account=self.a,enabled=1,available=1,capacity=2)).insert();frappe.db.commit()
		human_flow=self.flow(method='controlled_human',show_in_menu=0,outputs=[dict(status_code='success',output_mode='Message')])
		self.configure(human_attention_enabled=1,
			human_flow=human_flow.name,human_transfer_states=[dict(state=state_name(human_flow.name, 'success'))])
		flow=self.flow()
		with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',return_value=Decision(outcome='recognized', human_attention_requested=True)):
			first=self.message(text='A person please')
		self.assertEqual(frappe.get_doc('Chat Session',first['session']).status,'Human Queued',first)
		self.assertIsNone(frappe.get_doc('Chat Session',first['session']).assigned_agent)
		self.availability.stop()
		self.message(text='Follow-up while Human owns the conversation')
		frappe.set_user(user);human.act(first['session'],'Take')
		reply=inbox.send(first['session'],'Synthetic human reply','consolidation-human-001')
		self.assertEqual(reply['state'],'Accepted')
		with self.assertRaises(frappe.ValidationError):human.act(first['session'],'Pause')
		with self.assertRaises(frappe.ValidationError):human.act(first['session'],'Resume')
		human.act(first['session'],'Close');frappe.set_user('Administrator')
		self.assertEqual([m['content'] for m in console.history(self.a,self.sender)['messages'] if m['kind']=='Human'],['Synthetic human active message.', 'Synthetic human reply'])
		self.assertEqual(console.history(self.b,self.sender)['messages'],[])
		self.availability.start()
		with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',return_value=Decision(outcome='recognized', flows=[flow.name])):
			self.assertIn('Result hours.',self.message(text=flow.name)['response'])

	def test_contact_activity_is_visible_scoped_and_independent_of_session_updates(self):
		from messaging.conversation.doctype.chat_message.chat_message import append_message
		self.configure(quotas_enabled=0)
		quiet = self.contact(display_name='A quiet contact')
		second = self.contact(display_name='B active contact')
		self.assertTrue(all(not row['last_activity'] for row in console.contacts(self.a)))
		self.assertFalse(frappe.db.count('Chat Session', {'account': self.a}))
		first = self.message()
		other = self.message(sender=second.sender_id)
		self.assertEqual(console.contacts(self.a)[0]['sender_id'], second.sender_id)
		session = frappe.get_doc('Chat Session', first['session'])
		for kind in ('Incoming', 'Automatic', 'Human'):
			append_message(session, frappe.generate_hash(length=32), kind, 'Visible '+kind)
			frappe.db.commit()
			row = console.contacts(self.a)[0]
			self.assertEqual((row['sender_id'], row['activity_kind'], row['preview']), (self.sender, kind, 'Visible '+kind))
		before = console.contacts(self.a)
		frappe.db.set_value('Chat Session', other['session'], 'last_activity', '2099-01-01')
		append_message(frappe.get_doc('Chat Session', other['session']), frappe.generate_hash(length=32), 'Event', 'Administrative event')
		frappe.db.commit()
		self.assertEqual(console.contacts(self.a), before)
		self.assertNotIn('Event', [row.kind for row in console.history(self.a, second.sender_id)['messages']])
		self.assertTrue(all(not row['last_activity'] for row in console.contacts(self.b)))
		self.assertEqual(console.history(self.a, quiet.sender_id)['messages'], [])
		with self.assertRaises(frappe.PermissionError):
			console.history(self.a, quiet.sender_id, message_before=console.history(self.a, self.sender)['messages'][0]['name'])
		self.assertFalse(frappe.db.count('Chat Contact', {'account': self.a, 'external_sender': quiet.sender_id}))

	def test_unified_history_crosses_episodes_and_uses_scoped_stable_message_cursor(self):
		from messaging.conversation.doctype.chat_message.chat_message import append_message
		self.configure(quotas_enabled=0)
		first = self.message()
		session = frappe.get_doc('Chat Session', first['session'])
		for index in range(61):
			append_message(session, frappe.generate_hash(length=32), 'Incoming', 'Page '+str(index))
		frappe.db.commit()
		session.status = "Closed"
		session.active_key = None
		session.save(ignore_permissions=True)
		frappe.db.commit()
		second = self.message()
		self.assertNotEqual(first['session'], second['session'])
		latest = console.history(self.a, self.sender)
		self.assertEqual(len(latest['messages']), 60)
		self.assertTrue(latest['more_messages'])
		earlier = console.history(self.a, self.sender, message_before=latest['messages'][0]['name'])
		combined = earlier['messages'] + latest['messages']
		self.assertEqual(len({row['name'] for row in combined}), len(combined))
		self.assertEqual({row['session'] for row in combined}, {first['session'], second['session']})
		keys = [(row['timeline_at'], row['name']) for row in combined]
		self.assertEqual(keys, sorted(keys))
		self.assertFalse(earlier['more_messages'])
		self.assertEqual(console.history(self.a, self.sender, session=first['session'])['session'], first['session'])
		with self.assertRaises(frappe.PermissionError):
			console.history(self.b, self.sender, message_before=latest['messages'][0]['name'])

	def test_client_evidence_delivery_read_and_idempotency(self):
		import time
		self.flow()
		result = self.message()
		dispatch = frappe.get_doc('Bot Dispatch', result['dispatch'])
		self.assertEqual((dispatch.status, dispatch.delivery_status), ('Accepted', 'Sent'))
		counts = {dt: frappe.db.count(dt) for dt in ('Chat Session', 'Chat Turn', 'Chat Message', 'Bot Dispatch')}
		accepted_at = dispatch.accepted_at
		row = next(m for m in console.history(self.a, self.sender)['messages'] if m.dispatch == dispatch.name)
		args = dict(account=self.a, sender=self.sender, source_type='Bot Dispatch', name=dispatch.name, receipt=row.client_receipt)
		self.assertEqual(frappe.db.get_value('Bot Dispatch', dispatch.name, 'delivery_status'), 'Sent')
		with self.assertRaises(frappe.PermissionError): console.observe_message(**args)
		console.client_status(**args, status='Delivered')
		delivered_at = frappe.db.get_value('Bot Dispatch', dispatch.name, 'delivered_at')
		console.client_status(**args, status='Delivered')
		self.assertEqual(frappe.db.get_value('Bot Dispatch', dispatch.name, 'delivered_at'), delivered_at)
		with patch('messaging.emulator.provider.time.time', return_value=time.time() + 3601):
			with self.assertRaises(frappe.PermissionError): console.client_status(**args, status='Delivered')
			self.assertTrue(console.observe_message(**args)['receipt'])
		proof = console.observe_message(**args)['receipt']
		read_args = dict(args, receipt=proof)
		with self.assertRaises(frappe.PermissionError): console.client_status(**read_args, status='Read')
		time.sleep(.72)
		console.client_status(**read_args, status='Read')
		read_at = frappe.db.get_value('Bot Dispatch', dispatch.name, 'read_at')
		console.client_status(**read_args, status='Read')
		console.client_status(**args, status='Delivered')
		dispatch.reload()
		self.assertEqual((dispatch.delivery_status, dispatch.accepted_at, dispatch.delivered_at, dispatch.read_at), ('Read', accepted_at, delivered_at, read_at))
		self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts)
		with self.assertRaises(frappe.PermissionError): console.client_status(**dict(args, account=self.b), status='Delivered')
		with self.assertRaises(frappe.PermissionError): console.client_status(**dict(args, receipt=proof+'invalid'), status='Read')
		with self.assertRaises(frappe.PermissionError): console.simulate_status(self.a, self.sender, dispatch.name, 'Read')

	def test_automatic_receipts_never_reconcile_uncertainty_or_override_failure(self):
		self.flow()
		for outcome in ('Unknown', 'Rejected'):
			account = self.account(outcome); result = self.message(account=account)
			row = next(m for m in console.history(account, self.sender)['messages'] if m.dispatch == result['dispatch'])
			self.assertIsNone(row.client_receipt)
			with self.assertRaises(frappe.PermissionError):
				console.client_status(account, self.sender, 'Bot Dispatch', result['dispatch'], 'forged', 'Delivered')
			self.assertEqual(frappe.db.get_value('Bot Dispatch', result['dispatch'], 'status'), outcome)
		result = self.message()
		row = next(m for m in console.history(self.a, self.sender)['messages'] if m.dispatch == result['dispatch'])
		console.simulate_status(self.a, self.sender, result['dispatch'], 'Failed')
		with self.assertRaises(frappe.PermissionError):
			console.client_status(self.a, self.sender, 'Bot Dispatch', result['dispatch'], row.client_receipt, 'Delivered')
		self.assertEqual(frappe.db.get_value('Bot Dispatch', result['dispatch'], 'delivery_status'), 'Failed')
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError): console.history(self.a, self.sender)

	def test_channel_console_does_not_query_ai_availability(self):
		self.availability.stop()
		with patch('messaging.chatbot.doctype.ai_configuration.ai_configuration.availability',
			side_effect=AssertionError('Console must not query AI availability')):
			self.assertNotIn('ai', console.accounts())
			result = self.message(text='Synthetic channel input')
			self.assertTrue(result['contact'])
			self.assertTrue(any(row.content == 'Synthetic channel input' for row in console.history(self.a, self.sender)['messages']))

	def test_assigned_string_capture_works_without_operational_ai_in_console(self):
		self.natural_mode()
		description = self.parameter('String', min_length=5)
		location = self.parameter('String', min_length=5)
		flow = self.flow(parameters=[dict(parameter=description.name, process_argument='value', required=1, capture_order=1),
			dict(parameter=location.name, process_argument='optional', required=1, capture_order=2)])
		with patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret',
			return_value=Decision(outcome='recognized', flows=[flow.name])):
			self.message(text=flow.name)
		self.availability.stop()
		self.interpreter.stop()
		with patch('messaging.chatbot.doctype.ai_configuration.ai_configuration.availability',
			return_value=dict(operational=False, status='unavailable', diagnostic='Synthetic unavailable AI.')), patch(
			'messaging.conversation.doctype.chat_turn.turn_decision.interpret', side_effect=AssertionError('No capture AI')) as interpreter:
			partial = self.message(text='Hay una tubería rota')
			self.assertIn('Continuation input.', partial['response'])
			answer = dict(text='Barrio de prueba, frente a la escuela', client_id=frappe.generate_hash(length=32))
			complete = self.message(**answer)
			interpreter.assert_not_called()
		self.assertEqual(native.CALLS, ['Hay una tubería rota'])
		for turn in (partial['turn'], complete['turn']):
			self.assertFalse(frappe.db.exists('AI Request Log', {'turn': turn}))
		history = console.history(self.a, self.sender)
		self.assertTrue(any(row.content == answer['text'] for row in history['messages']))

	def test_channel_without_configured_automation_retains_input_without_reply(self):
		self.interpreter.stop()
		self.availability.stop()
		self.configure(interpretation_enabled=0, registration_enabled=0, greeting_enabled=0, human_attention_enabled=0)
		with patch('messaging.conversation.doctype.chat_turn.chat_turn.mechanical_menu_flows', return_value=[]), patch(
			'messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('No channel AI')):
			result = self.message(text='Synthetic input without automation')
		self.assertEqual(result['state'], 'Complete')
		self.assertFalse(result['dispatch'])
		messages = console.history(self.a, self.sender)['messages']
		self.assertEqual([(row.kind, row.content) for row in messages], [('Incoming', 'Synthetic input without automation')])
		self.assertEqual(frappe.db.count('Chat Contact', {'channel': CHANNEL, 'account': self.a, 'external_sender': self.sender}), 1)

	def test_invalid_disabled_account_and_provider_reject_before_creating_conversations(self):
		first = self.message(text='Retained channel history')
		counts = {dt: frappe.db.count(dt) for dt in ('Chat Contact', 'Chat Session', 'Chat Message', 'Chat Turn')}
		account = frappe.get_doc('Communication Channel Account', self.a)
		account.enabled = 0; account.save(); frappe.db.commit()
		for selected in (self.a, 'missing-policy-account'):
			with self.assertRaises((frappe.PermissionError, frappe.DoesNotExistError)):
				self.message(account=selected)
		self.assertFalse(console.history(self.a, self.sender)['enabled'])
		self.assertTrue(console.history(self.a, self.sender)['messages'])
		account.enabled = 1; account.save(); frappe.db.commit()
		set_provider_enabled(PROVIDER, False)
		with self.assertRaises(frappe.PermissionError):
			self.message()
		self.assertEqual(counts, {dt: frappe.db.count(dt) for dt in counts})
		with patch.object(frappe, 'get_list', return_value=[]):
			self.assertEqual(console.accounts()['accounts'], [])
		self.assertTrue(console.history(self.a, self.sender)['messages'])


	def test_native_conversation_series_and_immutable_links(self):
		self.flow()
		result = self.message()
		for dt, field, prefix in [('Chat Contact', 'contact', 'CTC'), ('Chat Session', 'session', 'SES'), ('Chat Turn', 'turn', 'TRN')]:
			self.assertRegex(result[field], '^'+prefix+r'-[0-9]{6}$')
			self.assertEqual(frappe.get_meta(dt).autoname, prefix+'-.######')
			with self.assertRaises(frappe.ValidationError): frappe.rename_doc(dt, result[field], 'changed', force=True)
		messages = frappe.get_all('Chat Message', filters={'contact': result['contact']}, fields=['name', 'session', 'turn'])
		for message in messages:
			self.assertRegex(message.name, r'^MSG-[0-9]{6}$')
			self.assertEqual(message.session, result['session'])
		self.assertRegex(result['dispatch'], r'^DSP-[0-9]{6}$')
		self.assertEqual(frappe.get_meta('Bot Dispatch').autoname, 'DSP-.######')
		self.assertEqual(frappe.db.get_value('Chat Contact', result['contact'], 'external_sender'), self.sender)


	def test_concurrent_client_receipts_are_serialized_without_duplicate_effects(self):
		import multiprocessing
		self.flow()
		result = self.message()
		row = next(m for m in console.history(self.a, self.sender)['messages'] if m.dispatch == result['dispatch'])
		args = dict(account=self.a, sender=self.sender, source_type='Bot Dispatch', name=row.dispatch,
			receipt=row.client_receipt, status='Delivered')
		ctx = multiprocessing.get_context('spawn'); barrier = ctx.Barrier(2); results = ctx.Queue()
		children = [ctx.Process(target=concurrent_receipt, args=(frappe.local.site, str(frappe.local.sites_path), args, barrier, results)) for _ in range(2)]
		try:
			for child in children: child.start()
			outputs = [results.get(timeout=35) for _ in children]
			for child in children:
				child.join(timeout=5); self.assertEqual(child.exitcode, 0)
			self.assertEqual([value.get('delivery') for value in outputs], ['Delivered', 'Delivered'], outputs)
			frappe.db.rollback()
			self.assertEqual(frappe.db.count('Chat Turn', {'contact': result['contact']}), 1)
			self.assertEqual(frappe.db.count('Bot Dispatch', {'contact': result['contact']}), 1)
			self.assertEqual(frappe.db.count('Chat Message', {'contact': result['contact']}), 2)
		finally:
			for child in children:
				if child.is_alive(): child.terminate(); child.join()


class TestEmulrSetup(unittest.TestCase):
	def setUp(self):
		frappe.set_user('Administrator')

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user('Administrator')

	def test_official_sync_is_idempotent_and_creates_no_functional_records(self):
		from messaging import __version__
		before = {doctype: frappe.db.count(doctype) for doctype in (
			'Communication Channel', 'Communication Channel Account', 'Process Definition',
			'Communication Template', 'Notification Rule', 'Bot Flow', 'Notification Queue',
			'Chat Session', 'Chat Message')}
		enabled = frappe.db.get_value('Communication Provider', 'EMULR', 'enabled')
		sync_communication_providers()
		doc = frappe.get_doc('Communication Provider', 'EMULR')
		self.assertEqual(doc.provider_name, 'Channel Emulator')
		self.assertEqual(doc.application, 'messaging')
		self.assertEqual(doc.version, __version__)
		self.assertEqual(int(doc.single_channel), 1)
		self.assertEqual(doc.fixed_channel_id, 'emulator')
		parameters = {row.parameter_key: row for row in doc.parameters}
		self.assertEqual(parameters['notification_contact'].field_type, 'Link')
		self.assertEqual(parameters['notification_contact'].options, 'Emulator Contact')
		self.assertEqual(parameters['notification_contact'].scope, 'Notification Rule')
		self.assertEqual(parameters['transport_outcome'].field_type, 'Select')
		self.assertEqual(parameters['transport_outcome'].scope, 'Account')
		self.assertEqual(parameters['transport_outcome'].options, 'Accepted\nRejected\nUnknown')
		self.assertEqual(parameters['transport_outcome'].default_value, 'Accepted')
		self.assertEqual(frappe.db.get_value('Communication Provider', 'EMULR', 'enabled'), enabled if enabled is not None else 0)
		modified = doc.modified
		names = [row.name for row in doc.parameters]
		sync_communication_providers()
		again = frappe.get_doc('Communication Provider', 'EMULR')
		self.assertEqual(again.modified, modified)
		self.assertEqual([row.name for row in again.parameters], names)
		self.assertEqual(frappe.db.count('Communication Provider', {'provider_id': 'EMULR'}), 1)
		for doctype, count in before.items():
			self.assertEqual(frappe.db.count(doctype), count, doctype)
