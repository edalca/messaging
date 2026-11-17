from messaging.chatbot.doctype.bot_settings.localization import text as settings_text
"""Integrated human operations with real documents, transactions and independent MariaDB connections."""
import json
import multiprocessing
import time
import unittest
from datetime import timedelta
from unittest.mock import patch, MagicMock

import frappe

from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import simulate_dispatch
from messaging.common.primitives import utcnow, identity_key
from messaging.conversation.doctype.chat_message.chat_message import _writing, dispatch_content
from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
from messaging.conversation.doctype.chat_turn import test_chat_turn as native
from messaging.conversation.doctype.chat_turn import test_chat_turn as interpretation_tests
from messaging.conversation.doctype.chat_turn.chat_turn import process_event, process_message
from messaging.conversation.doctype.chat_session import human, inbox
from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers, bind
from messaging.presets.communication_providers.emulr import definition as emulator_definition


def concurrent_action(site, sites_path, user, task, args, barrier, results, contracts):
	frappe.init(site=site,sites_path=sites_path);frappe.connect();frappe.set_user(user)
	try:
		from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import PROBE
		from messaging.presets.communication_providers.emulr import definition as emulator_definition
		native._dialogue_contracts.update(contracts)
		with registered_providers([emulator_definition(), PROBE]), \
			patch('requests.sessions.Session.request', side_effect=AssertionError('Network is forbidden')), \
			patch('messaging.chatbot.doctype.ai_evaluation_run.runtime.approval', return_value={'approved': True}):
			barrier.wait(timeout=20)
			function = {'act':human.act,'event':process_event,'send':inbox.send,'inactive':human.close_inactive}[task]
			result = function(**args)
			results.put(dict(ok=True,result=result))
	except Exception as exc:
		results.put(dict(ok=False,error=type(exc).__name__, detail=str(exc)))
	finally:
		frappe.db.rollback();frappe.destroy()


def concurrent_supervision_reassign(site, sites_path, user, args, barrier, results):
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.set_user(user)
	try:
		from messaging.conversation.doctype.chat_session import supervision
		barrier.wait(timeout=20)
		result = supervision.reassign(**args)
		results.put(dict(ok=True, result=result))
	except Exception as exc:
		results.put(dict(ok=False, error=type(exc).__name__, detail=str(exc)))
	finally:
		frappe.db.rollback()
		frappe.destroy()



class TestRequestCompletion(unittest.TestCase):
    def test_resolved_request_retires_only_request_state_and_is_idempotent(self):
        from copy import deepcopy
        from messaging.conversation.doctype.chat_session.chat_session import complete_request
        state = dict(queue=[], completed_count=2, mechanical_menu={'options': {}}, menu_pending=True,
            active_request='request', prompt_turn='turn', greeted=True,
            values={'parameter': 'captured'}, interaction_mode='Mechanical', registration_completed=True)
        self.assertIsNone(complete_request(state))
        self.assertEqual(state, dict(queue=[], completed_count=0, greeted=False, values={},
            interaction_mode='Mechanical', registration_completed=True))
        completed = deepcopy(state)
        self.assertIsNone(complete_request(state))
        self.assertEqual(state, completed)

    def test_pending_request_is_preserved(self):
        from copy import deepcopy
        from messaging.conversation.doctype.chat_session.chat_session import complete_request
        state = dict(queue=[{'flow': 'pending'}], completed_count=1, values={'parameter': 'captured'},
            active_request='request', prompt_turn='turn', greeted=True)
        pending = deepcopy(state)
        self.assertIsNone(complete_request(state))
        self.assertEqual(state, pending)

    def test_retry_for_initial_absence_does_not_spend_invalid_attempt_or_duplicate_prompt(self):
        from messaging.conversation.doctype.chat_session.chat_session import apply_flow_result
        from types import SimpleNamespace
        flow=frappe._dict(name='business',parameters=[frappe._dict(parameter='number')])
        settings=frappe._dict(greeting_flow='greeting',max_invalid_attempts=3)
        item=dict(flow='business',values={},invalid_attempts=0)
        state=dict(values={})
        session=SimpleNamespace(status='Executing')
        turn=SimpleNamespace(name='first')
        result=dict(flow_action='Retry Parameter',functional=True,retry_parameter='number',text='Enter your contract number.')
        parts,menu,waiting=apply_flow_result(result,flow,item,session,state,turn,settings,[])
        self.assertEqual(item['invalid_attempts'],0)
        self.assertEqual(parts,[])
        self.assertTrue(waiting)
        item['values']={'number':'invalid'}
        turn.name='second'
        apply_flow_result(result,flow,item,session,state,turn,settings,[])
        self.assertEqual(item['invalid_attempts'],1)


class TestHuman(unittest.TestCase):
	configure = native.TestChatTurn.configure
	ensure_test_channel = native.TestChatTurn.ensure_test_channel


	event = native.TestChatTurn.event


	flow = native.TestChatTurn.flow


	parameter = native.TestChatTurn.parameter


	state = native.TestChatTurn.state


	proposal = interpretation_tests.InterpretationFixtures.proposal


	def setUp(self):
		network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network is forbidden'))
		network.start(); self.addCleanup(network.stop)
		native.TestChatTurn.setUp(self)
		from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import PROBE
		from messaging.communication.doctype.communication_provider.communication_provider import get_provider_states, set_provider_enabled
		from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
		self.provider_states_before = get_provider_states()
		self.registry = registered_providers([emulator_definition()] + [PROBE])
		self.registry.__enter__()
		self.addCleanup(self.registry.__exit__, None, None, None)
		sync_communication_providers()
		set_provider_enabled('CTRPRO', True)
		self.probe_channel = frappe.get_doc(dict(doctype='Communication Channel', provider='CTRPRO', channel_name='Synthetic human contract channel', enabled=1)).insert(set_name='test')
		self.binding = bind(self.prefix, name=self.prefix)
		frappe.db.commit()
		self.error_logs_before = set(frappe.get_all("Error Log", pluck="name"))
		self.users = []
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
		self.ai_configuration = create_configuration(self.prefix + "_ai", api_key="synthetic-human-key")
		flow = self.flow(method='controlled_human', show_in_menu=0, outputs=[dict(status_code='success', output_mode='Message')])
		self.configure(human_attention_enabled=1,
			human_inactivity_minutes=60,interpretation_enabled=1, active_ai_configuration=self.ai_configuration.name,
			human_flow=flow.name, human_transfer_states=[dict(state=state_name(flow.name, 'success'))])


	def tearDown(self):
		frappe.db.rollback();frappe.set_user('Administrator')
		contacts = frappe.get_all('Chat Contact',filters={'account':['in',self.accounts]},pluck='name')
		sessions = frappe.get_all('Chat Session',filters={'contact':['in',contacts]},pluck='name') if contacts else []
		if sessions:
			logs = frappe.get_all('Process Execution Log',filters={'session':['in',sessions]},fields=['name','frappe_error_log'])
			for log in logs:
				if log.frappe_error_log:frappe.db.delete('Error Log',{'name':log.frappe_error_log})
			frappe.db.delete('Process Execution Log',{'session':['in',sessions]})
		for user in self.users:
			frappe.db.delete('Chat Agent',{'user':user})
		frappe.db.commit()
		# Restore the installed provider hook before native fixture registration.
		self.registry.__exit__(None, None, None)
		native.TestChatTurn.tearDown(self)
		frappe.delete_doc("AI Configuration", self.ai_configuration.name, delete_permanently=True)
		frappe.delete_doc("Communication Channel Account", self.binding.name, ignore_permissions=True)
		from messaging.communication.doctype.communication_provider.communication_provider import _lock_provider_states, _write_provider_states
		from messaging.communication.doctype.communication_provider.communication_provider import catalog_sync
		_lock_provider_states(); _write_provider_states(self.provider_states_before)
		frappe.delete_doc('Communication Channel', self.probe_channel.name)
		with catalog_sync(): frappe.delete_doc('Communication Provider', 'CTRPRO')
		for user in self.users:
			frappe.delete_doc('User',user,ignore_permissions=True,force=True)
		for row in frappe.get_all('Error Log', fields=['name','method','error']):
			if (row.name not in self.error_logs_before and self.prefix in (row.error or '')
				and row.method in ('frappe.core.doctype.user.user.create_contact','Failed to send Notification')):
				frappe.db.delete('Error Log', {'name':row.name})
		frappe.db.commit()


	def agent(self, *, capacity=1, available=1, account=None):
		user = self.prefix+str(len(self.users))+'@example.invalid'
		# Synthetic Messaging agents do not need Frappe's deferred business Contact job.
		with patch('frappe.enqueue'):
			frappe.get_doc(dict(doctype='User',email=user,first_name='Controlled Agent',send_welcome_email=0,
				user_type='System User',roles=[dict(role='Messaging Agent')])).insert(ignore_permissions=True)
		self.users.append(user)
		frappe.get_doc(dict(doctype='Chat Agent',user=user,channel='test',account=account or self.prefix,
			enabled=1,available=available,capacity=capacity)).insert(ignore_permissions=True)
		frappe.db.commit()
		return user


	def open(self,sender='external-user'):
		return process_event(self.event(sender=sender,text='Please connect me.',human_requested=True))


	def race(self, specs):
		frappe.db.commit()
		ctx=multiprocessing.get_context('spawn');barrier=ctx.Barrier(len(specs));results=ctx.Queue()
		contracts = {definition.id: native._dialogue_contracts[definition.id] for definition in self.fixture_definitions}
		children=[ctx.Process(target=concurrent_action,args=(frappe.local.site,str(frappe.local.sites_path),user,task,args,barrier,results,contracts)) for user,task,args in specs]
		for child in children:child.start()
		out=[results.get(timeout=45) for _ in children]
		for child in children:
			child.join(timeout=10)
			if child.is_alive():child.terminate();child.join();self.fail('Concurrent operation exceeded deadline')
		frappe.db.rollback()
		return out


	def test_human_flow_assigned_message_follows_accepted_functional_result(self):
		user=self.agent()
		opened=self.open()
		self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'status'), 'Human Queued')
		self.assertFalse(frappe.db.get_value('Chat Session', opened['session'], 'assigned_agent'))
		self.assertEqual(opened['response'], 'Human decision: success.')
		self.assertEqual(opened['response'].count('Human decision:'), 1)
		frappe.set_user(user)
		human.act(opened['session'], 'Take')
		self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'status'), 'Human Active')
		self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'assigned_agent'), user)
		notice = frappe.get_doc('Bot Dispatch', {'session': opened['session'], 'message_type': 'Human'})
		self.assertEqual(dispatch_content(notice), settings_text(frappe.get_single('Bot Settings'), 'human_active_message'))

	def test_human_flow_keeps_assignment_permissions_and_inactivity(self):
		user = self.agent(available=0)
		opened = self.open()
		session = opened['session']
		self.assertEqual(frappe.db.get_value('Chat Session', session, 'status'), 'Human Queued')
		frappe.db.set_value('Chat Agent', {'user':user}, 'available', 1);frappe.db.commit()
		frappe.set_user(user);human.act(session, 'Take')
		self.assertEqual(frappe.db.get_value('Chat Session', session, 'assigned_agent'), user)
		with self.assertRaises(frappe.ValidationError):human.act(session, 'Pause')
		with self.assertRaises(frappe.ValidationError):human.act(session, 'Resume')
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):inbox.history(session)
		frappe.set_user('Administrator')
		frappe.db.set_value('Chat Session', session, 'last_activity', utcnow()-timedelta(minutes=61));frappe.db.commit()
		self.assertIn(session, human.close_inactive())
		self.assertEqual(frappe.db.get_value('Chat Session', session, 'status'), 'Closed')

	def test_permissions_queue_reassignment_read_cursor_and_inactivity(self):
		one=self.agent(available=0);two=self.agent(available=0);other=self.agent(account=self.prefix+'-other')
		opened=self.open();session=opened['session']
		self.assertEqual(frappe.get_doc('Chat Session',session).status,'Human Queued')
		frappe.db.set_value('Chat Agent',{'user':one},'available',1);frappe.db.commit()
		frappe.set_user(one);human.act(session,'Take');inbox.mark_read(session,1)
		frappe.set_user(two)
		for fn,args in [(inbox.history,(session,)),(inbox.send,(session,'Unauthorized reply','unauthorized-id')),(human.act,(session,'Assign',one))]:
			with self.assertRaises(frappe.PermissionError):fn(*args)
		self.assertEqual(inbox.conversations(),[])
		self.assertFalse(frappe.has_permission('Chat Session','read',doc=frappe.get_doc('Chat Session',session)))
		self.assertEqual(frappe.get_list('Chat Message',filters={'session':session}),[])
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):inbox.contact_profile(opened['contact'])
		self.assertEqual(inbox.contacts(),[])
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):inbox.conversations()
		frappe.set_user('Administrator');frappe.db.set_value('Chat Agent',{'user':two},'available',1);frappe.db.commit()
		revision=frappe.get_doc('Chat Session',session).modified
		human.act(session,'Assign',two,revision)
		with self.assertRaises(frappe.ValidationError):human.act(session,'Assign',one,revision)
		frappe.set_user(two)
		self.assertGreater(inbox.history(session)['session']['unread'],0)
		frappe.set_user('Administrator')
		frappe.db.set_value('Chat Session',session,'last_activity',utcnow()-timedelta(minutes=61));frappe.db.commit()
		self.assertIn(session,human.close_inactive())
		self.assertEqual(frappe.get_doc('Chat Session',session).close_reason,'Inactivity')


	def test_concurrent_take_duplicate_receive_reassign_close_and_transfer(self):
		one=self.agent(available=0,capacity=3);two=self.agent(available=0,capacity=3)
		opened=self.open();session=opened['session']
		for user in (one,two):frappe.db.set_value('Chat Agent',{'user':user},'available',1)
		frappe.db.commit()
		out=self.race([(u,'act',dict(session_name=session,action='Take')) for u in (one,two)])
		self.assertEqual(sum(r['ok'] for r in out),1,out)
		incoming=self.event(text='Concurrent incoming event.')
		out=self.race([('Administrator','event',dict(request=incoming))]*2)
		self.assertTrue(all(r['ok'] for r in out),out)
		self.assertEqual(out[0]['result']['turn'],out[1]['result']['turn'])
		self.assertEqual(frappe.db.count('Chat Message',dict(session=session,content='Concurrent incoming event.')),1)
		out=self.race([('Administrator','event',dict(request=self.event(text='Distinct concurrent '+str(i)))) for i in range(2)])
		self.assertTrue(all(r['ok'] for r in out),out)
		self.assertEqual(len({r['result']['turn'] for r in out}),2)
		self.assertEqual(frappe.db.count('Chat Message',dict(session=session,content=['like','Distinct concurrent %'])),2)
		revision=frappe.get_doc('Chat Session',session).modified
		out=self.race([('Administrator','act',dict(session_name=session,action='Assign',agent=u,expected_modified=str(revision))) for u in (one,two)])
		self.assertEqual(sum(r['ok'] for r in out),1,out)
		out=self.race([('Administrator','act',dict(session_name=session,action='Close')),('Administrator','event',dict(request=self.event(text='Concurrent close incoming.')))])
		self.assertTrue(all(r['ok'] for r in out),out)
		self.assertEqual(frappe.db.count('Chat Message',dict(contact=opened['contact'],content='Concurrent close incoming.')),1)
		# Transfer and an automatic Process compete on the same durable contact lock.
		flow=self.flow();fresh=process_event(self.event(sender='transfer-race',text='Start.'))
		simulate_dispatch(fresh['dispatch'])
		transfer_request = self.event(sender='transfer-race', human_requested=True)
		automatic_request = self.event(sender='transfer-race', flows=[flow.name], event_at=transfer_request['event_at'])
		out=self.race([('Administrator','event',dict(request=transfer_request)),
			('Administrator','event',dict(request=automatic_request))])
		self.assertTrue(all(r['ok'] for r in out),out)
		transferred = frappe.get_doc('Chat Session', fresh['session'])
		self.assertIn(transferred.status, human.HUMAN_OPEN)
		confirmation = frappe.db.get_value('Chat Turn', transferred.human_request_turn, 'dispatch')
		self.assertTrue(confirmation)
		self.assertEqual(frappe.get_all('Bot Dispatch', filters=dict(session=fresh['session'],message_type='Automatic',
			status=['in',['Reserved','Prepared','Held','Sending']]), pluck='name'), [confirmation])
		self.assertEqual(frappe.db.get_value('Bot Dispatch', confirmation, 'functional_result'), 1)


	def test_human_unknown_is_durable_and_capacity_queues(self):
		user=self.agent(capacity=1)
		first=self.open();second=self.open('second')
		self.assertEqual(frappe.get_doc('Chat Session',first['session']).status,'Human Queued')
		self.assertEqual(frappe.get_doc('Chat Session',second['session']).status,'Human Queued')
		frappe.set_user(user)
		human.act(first['session'],'Take')
		with self.assertRaises(frappe.ValidationError):
			human.act(second['session'],'Take')
		self.assertEqual(frappe.get_doc('Chat Session',second['session']).status,'Human Queued')
		with patch('messaging.communication.doctype.communication_provider.communication_provider.operation',side_effect=TimeoutError('private transport')) as transport:
			result=inbox.send(first['session'],'Uncertain reply.','uncertain-id-001')
			self.assertEqual(result['state'],'Unknown')
			self.assertEqual(inbox.send(first['session'],'Uncertain reply.','uncertain-id-001')['state'],'Unknown')
			self.assertEqual(transport.call_count,1)
		with patch('messaging.communication.doctype.communication_provider.communication_provider.operation',return_value=dict(outcome='Rejected',reference='simulated')):
			self.assertEqual(inbox.send(first['session'],'Rejected reply.','rejected-id-001')['state'],'Rejected')
		self.assertEqual(frappe.db.count('Bot Dispatch',dict(contact=first['contact'],quota_kind='Human')),3)
		self.assertFalse(frappe.db.exists('Bot Dispatch',dict(contact=first['contact'],quota_kind='Registration')))


	def test_distinct_contact_capacity_race_and_history_pagination(self):
		user=self.agent(capacity=1)
		out=self.race([('Administrator','event',dict(request=self.event(sender=sender,human_requested=True,text='Queue request.'))) for sender in ('capacity-one','capacity-two')])
		self.assertTrue(all(r['ok'] for r in out),out)
		statuses=[frappe.get_doc('Chat Session',r['result']['session']).status for r in out]
		self.assertEqual(statuses,['Human Queued','Human Queued'])
		take_out=self.race([(user,'act',dict(session_name=r['result']['session'],action='Take')) for r in out])
		self.assertEqual(sum(r['ok'] for r in take_out),1,take_out)
		taken_session=next(r['result']['session'] for r in take_out if r['ok'])
		active=next(r['result'] for r in out if r['result']['session']==taken_session)
		contact=frappe.get_doc('Chat Contact',active['contact'])
		for i in range(4):process_event(self.event(sender=contact.external_sender,text='History item '+str(i)))
		frappe.set_user(user)
		latest=inbox.history(active['session'],limit=2)['messages']
		earlier=inbox.history(active['session'],before=latest[0]['sequence'],limit=2)['messages']
		self.assertEqual(len(latest),2);self.assertEqual(len(earlier),2)
		self.assertLess(earlier[-1]['sequence'],latest[0]['sequence'])
		self.assertEqual(len(inbox.conversations(limit=1)),1)
		self.assertEqual(len(inbox.contacts(limit=1)),1)


	def test_pending_registration_yields_to_existing_human_attention(self):
		self.agent()
		parameter = self.parameter(mask='ID-#####')
		registration_flow = self.flow(parameters=[dict(parameter=parameter.name, process_argument='value', required=1, capture_order=1)],
			outputs=[dict(status_code='success', output_mode='Message')], output_fields={'success': {'result': 'string'}})
		first = process_event(self.event(human_requested=True))
		opened = process_event(self.event(human_requested=True))
		self.assertEqual(frappe.db.get_value('Chat Session',opened['session'],'status'),'Human Queued')
		self.configure(registration_enabled=1, registration_flow=registration_flow.name,
			registration_states=[dict(state=state_name(registration_flow.name, 'success'))],
			registration_fields=[dict(field_key='result', label='Result', keep=1)])
		frappe.db.set_value('Chat Contact', opened['contact'], 'registration_status', 'Pending')
		with patch('messaging.chatbot.doctype.ai_configuration.execution.run',side_effect=AssertionError('Human must suspend registration and AI')):
			received = process_message(self.event(text='ID-12345'))
		self.assertEqual(received['state'],'Human Paused')
		self.assertIsNone(received['dispatch'])
		contact = frappe.get_doc('Chat Contact',received['contact'])
		self.assertEqual(contact.registration_status,'Pending')
		self.assertEqual(json.loads(contact.parameter_values),{})
		self.assertEqual(first['session'],received['session'])



class TestHumanFlowRuntime(unittest.TestCase):
	configure = native.TestChatTurn.configure
	ensure_test_channel = native.TestChatTurn.ensure_test_channel
	event = native.TestChatTurn.event
	flow = native.TestChatTurn.flow
	parameter = native.TestChatTurn.parameter
	phrase = native.TestChatTurn.phrase
	state = native.TestChatTurn.state

	def setUp(self):
		native.TestChatTurn.setUp(self)

	def tearDown(self):
		native.TestChatTurn.tearDown(self)

	def human_flow(self, *, parameter=False, authorized=('success',)):
		value = self.parameter('Int', min_value=0, max_value=9) if parameter else None
		rows = [dict(parameter=value.name, process_argument='value', required=1, capture_order=1)] if value else []
		outputs = [dict(status_code=status, output_mode='Message', flow_action=action,
			**(dict(retry_parameter=value.name) if action == 'Retry Parameter' else {}))
			for status, action in [('success','Complete Flow'),('unlisted','Complete Flow'),('denied','Cancel Flow')]
			+ ([('retry','Retry Parameter')] if value else [])]
		flow = self.flow(method='controlled_human', parameters=rows, outputs=outputs, show_in_menu=0)
		self.configure(human_attention_enabled=1,
			human_flow=flow.name, human_transfer_states=[dict(state=state_name(flow.name, status)) for status in authorized])
		return flow, value

	def send(self, *, human_requested=False, text='', sender='external-user'):
		return process_event(self.event(human_requested=human_requested, text=text, sender=sender))

	def test_allowed_complete_flow_transfers_once_through_real_executor(self):
		flow, _ = self.human_flow()
		request = self.event(human_requested=True)
		first = process_event(request)
		self.assertEqual(frappe.db.get_value('Chat Session', first['session'], 'status'), 'Human Queued')
		self.assertIn('Human decision: success.', first['response'])
		self.assertEqual(first['response'], 'Human decision: success.')
		self.assertEqual(process_event(request), first)
		self.assertEqual(frappe.db.count('Process Execution Log', dict(session=first['session'], source_type='Bot Flow',
			source_name=flow.name, execution_outcome='Functional Status')), 1)
		self.assertFalse(self.send(text='Again', human_requested=True)['dispatch'])
		from messaging.conversation.doctype.chat_session.chat_session import mechanical_menu_flows
		self.assertNotIn(flow.name, [row['workflow_code'] for row in mechanical_menu_flows(frappe.get_single('Bot Settings'))])

	def test_capture_retry_cancel_and_unlisted_complete_keep_ordinary_semantics(self):
		flow, parameter = self.human_flow(parameter=True)
		first = self.send(human_requested=True)
		self.assertEqual(frappe.db.get_value('Chat Session', first['session'], 'status'), 'Capturing')
		self.assertEqual(frappe.db.count('Process Execution Log', {'session':first['session']}), 0)
		with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('Capture cannot call AI')):
			retry = self.send(text='0')
			self.assertIn('Human decision: retry.', retry['response'])
			self.assertEqual(frappe.db.get_value('Chat Session', retry['session'], 'status'), 'Capturing')
			self.assertTrue(self.state(retry)['human_attention'])
			complete = self.send(text='1')
		self.assertEqual(frappe.db.get_value('Chat Session', complete['session'], 'status'), 'Human Queued')
		for sender, value, message in [('denied','2','denied'), ('not-listed','3','unlisted')]:
			self.send(human_requested=True, sender=sender)
			result = self.send(text=value, sender=sender)
			self.assertEqual(frappe.db.get_value('Chat Session', result['session'], 'status'), 'Active')
			self.assertIn('Human decision: ' + message, result['response'])
			self.assertFalse(self.state(result).get('human_attention'))


	def test_inactive_stale_or_technical_failure_never_transfers(self):
		flow, _ = self.human_flow(parameter=True)
		self.send(human_requested=True)
		failure = self.send(text='4')
		self.assertNotIn(frappe.db.get_value('Chat Session',failure['session'],'status'), human.HUMAN_OPEN)
		self.assertFalse(frappe.db.exists('Chat Session', {'name':failure['session'],'assigned_agent':['is','set']}))
		from messaging.chatbot.doctype.bot_flow.bot_flow import set_active
		set_active(flow.as_dict(), 0);frappe.db.commit()
		result = self.send(human_requested=True, sender='inactive')
		self.assertNotIn(frappe.db.get_value('Chat Session', result['session'], 'status'), human.HUMAN_OPEN)
		self.assertIn(settings_text(frappe.get_single('Bot Settings'), 'default_internal_error_message'), result['response'])

	def test_multiple_complete_states_can_explicitly_authorize_transfer(self):
		self.human_flow(parameter=True, authorized=('success','unlisted'))
		self.send(human_requested=True)
		result = self.send(text='3')
		self.assertEqual(frappe.db.get_value('Chat Session', result['session'], 'status'), 'Human Queued')

	def test_live_configuration_change_prevents_transfer_and_capacity_requires_user(self):
		self.human_flow()
		agent = self.ensure_agent() if hasattr(self, 'ensure_agent') else None
		_ = agent
		with patch.object(human, 'capacity') as cap_check:
			self.configure(human_attention_enabled=0, human_transfer_states=[])
			result = self.send(human_requested=True)
		cap_check.assert_not_called()
		self.assertNotIn(frappe.db.get_value('Chat Session', result['session'], 'status'), human.HUMAN_OPEN)
		self.assertIsNone(human.capacity(frappe.get_doc('Chat Session', result['session'])))

	def test_foreign_execution_receipt_cannot_authorize_transfer(self):
		flow, _ = self.human_flow(parameter=True)
		self.send(human_requested=True, sender='first')
		first = self.send(text='1', sender='first')
		second = self.send(human_requested=True, sender='second')
		log = frappe.get_doc('Process Execution Log', {'session':first['session'], 'execution_outcome':'Functional Status'})
		session = frappe.get_doc('Chat Session', second['session'])
		state = self.state(second)
		human.transfer(session, state, turn=second['turn'], flow=flow, execution_id=log.execution_id,
			result=dict(functional=True,status='success',flow_action='Complete Flow',execution_log=log.name))
		self.assertNotIn(session.status, human.HUMAN_OPEN)

	def test_ordinary_use_of_selected_flow_never_transfers(self):
		flow, _ = self.human_flow()
		result = process_event(self.event(flows=[flow.name]))
		self.assertEqual(frappe.db.get_value('Chat Session', result['session'], 'status'), 'Active')
		self.assertIn('Human decision: success.', result['response'])

	def test_technical_human_flow_failure_uses_global_error_instead_of_flow_custom_error(self):
		flow, _ = self.human_flow(parameter=True)
		self.configure(default_internal_error_message='Global safe message.')
		flow.use_custom_internal_error_message = 1
		flow.translations[0].internal_error_message = 'Flow-specific safe message.'
		flow.save();frappe.db.commit()
		self.send(human_requested=True)
		result=self.send(text='4')
		self.assertEqual(result['response'], 'Global safe message.')
		self.assertNotIn('Flow-specific', result['response'])
		self.assertNotIn(frappe.db.get_value('Chat Session', result['session'], 'status'), human.HUMAN_OPEN)


class TestAssignmentNotice(unittest.TestCase):
    setUp = TestHuman.setUp
    tearDown = TestHuman.tearDown
    configure = TestHuman.configure
    ensure_test_channel = TestHuman.ensure_test_channel
    event = TestHuman.event
    flow = TestHuman.flow
    parameter = TestHuman.parameter
    state = TestHuman.state
    agent = TestHuman.agent
    open = TestHuman.open

    def test_take_and_assign_share_one_effective_assignment_notice(self):
        from messaging.conversation.doctype.chat_message.chat_message import dispatch_content
        agent = self.agent(available=0)
        opened = self.open()
        settings = frappe.get_single('Bot Settings')
        assigned_text = settings_text(settings, 'human_active_message')
        self.assertNotIn(assigned_text, opened['response'])
        self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'status'), 'Human Queued')
        frappe.db.set_value('Chat Agent', {'user': agent}, 'available', 1)
        frappe.db.commit()
        frappe.set_user(agent)
        human.act(opened['session'], 'Take')
        notices = frappe.get_all('Bot Dispatch', filters={'session': opened['session'], 'message_type': 'Human'}, pluck='name')
        self.assertEqual(len(notices), 1)
        self.assertEqual(dispatch_content(frappe.get_doc('Bot Dispatch', notices[0])), assigned_text)
        frappe.set_user('Administrator')
        session = frappe.get_doc('Chat Session', opened['session'])
        human.act(session.name, 'Assign', agent, session.modified)
        self.assertEqual(frappe.db.count('Bot Dispatch', {'session': session.name, 'message_type': 'Human'}), 1)
        human.act(session.name, 'Close')
        second = self.open()
        self.assertNotEqual(second['session'], opened['session'])
        self.assertEqual(len(json.loads(frappe.db.get_value('Chat Session', opened['session'], 'human_requests'))), 1)
        self.assertEqual(len(json.loads(frappe.db.get_value('Chat Session', second['session'], 'human_requests'))), 1)


class TestCompletionNotice(unittest.TestCase):
    setUp = TestHuman.setUp
    tearDown = TestHuman.tearDown
    configure = TestHuman.configure
    ensure_test_channel = TestHuman.ensure_test_channel
    event = TestHuman.event
    flow = TestHuman.flow
    parameter = TestHuman.parameter
    state = TestHuman.state
    agent = TestHuman.agent
    open = TestHuman.open
    race = TestHuman.race

    def opened(self):
        user = self.agent()
        opened = self.open()
        simulate_dispatch(opened['dispatch'])
        frappe.set_user(user)
        human.act(opened['session'], 'Take')
        return opened

    def notice(self, opened):
        key = identity_key(opened['session'], opened['turn'], 'human_completion')
        self.assertEqual(frappe.db.count('Bot Dispatch', {'outbound_key': key}), 1)
        return frappe.get_doc('Bot Dispatch', {'outbound_key': key})

    def test_manual_close_language_worker_and_repeated_delivery(self):
        from messaging.chatbot.doctype.bot_settings.test_bot_settings import set_localized_text
        from messaging.conversation.doctype.chat_turn.inbox import drain_contact
        opened = self.opened()
        frappe.set_user('Administrator')
        settings = frappe.get_single('Bot Settings')
        settings.append('chatbot_languages', dict(language='en', enabled=1))
        set_localized_text(settings, 'human_completed_message', 'Custom English completion.', language='en')
        settings.save()
        turn = frappe.get_doc('Chat Turn', opened['turn'])
        summary = json.loads(turn.input_summary or '{}')
        summary['language'] = 'en'
        turn.input_summary = json.dumps(summary)
        turn.save(ignore_permissions=True)
        frappe.db.commit()
        human.act(opened['session'], 'Close')
        notice = self.notice(opened)
        self.assertEqual((notice.message_type, notice.quota_kind, notice.status), ('Automatic', 'Human', 'Prepared'))
        self.assertFalse(notice.agent)
        self.assertEqual(dispatch_content(notice), 'Custom English completion.')
        message = frappe.get_doc('Chat Message', notice.message)
        self.assertEqual((message.kind, message.session, message.dispatch), ('Automatic', opened['session'], notice.name))
        with self.assertRaises(frappe.ValidationError):
            human.act(opened['session'], 'Close')
        drain_contact(opened['contact'])
        notice.reload()
        self.assertEqual(notice.status, 'Accepted')
        self.assertTrue(notice.transport_reference)
        from messaging.chatbot.doctype.bot_dispatch.delivery import deliver
        deliver(notice.name)
        self.assertEqual(self.notice(opened).name, notice.name)
        session = frappe.get_doc('Chat Session', opened['session'])
        self.assertEqual((session.status, session.close_reason), ('Closed', 'Manual'))
        self.assertEqual(frappe.db.count('Chat Message', {'dispatch': notice.name}), 1)

    def test_inactivity_queue_deadline_default_fallback_and_repeat(self):
        opened = self.opened()
        frappe.set_user('Administrator')
        queued = self.open(sender='completion-queued')
        old = utcnow() - timedelta(minutes=61)
        frappe.db.set_value('Chat Session', {'name': ['in', [opened['session'], queued['session']]]}, 'last_activity', old)
        turn = frappe.get_doc('Chat Turn', opened['turn'])
        summary = json.loads(turn.input_summary or '{}'); summary['language'] = 'en'
        turn.input_summary = json.dumps(summary); turn.save(ignore_permissions=True)
        frappe.db.commit()
        get_all = frappe.get_all
        def isolated(doctype, *args, **kwargs):
            if doctype == 'Chat Session' and kwargs.get('fields') == ['name', 'contact']:
                kwargs['filters'] = dict(kwargs['filters'], name=['in', [opened['session'], queued['session']]])
            return get_all(doctype, *args, **kwargs)
        with patch.object(frappe, 'get_all', side_effect=isolated):
            self.assertIn(opened['session'], human.close_inactive())
            self.assertEqual(human.close_inactive(), [])
        self.assertEqual(frappe.db.get_value('Chat Session', queued['session'], 'status'), 'Human Queued')
        deadline = frappe.get_doc('Chat Session', queued['session']).human_requested_at + timedelta(minutes=60)
        self.assertIn(queued['session'], human.close_inactive(now=deadline))
        self.assertEqual(human.close_inactive(now=deadline), [])
        with self.assertRaises(frappe.ValidationError):
            human.act(queued['session'], 'Take')
        self.notice(queued)
        self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'close_reason'), 'Inactivity')
        self.assertEqual(dispatch_content(self.notice(opened)), settings_text(frappe.get_single('Bot Settings'), 'human_completed_message'))

    def test_queue_messages_preserve_deadline_and_history(self):
        opened = self.open()
        session = frappe.get_doc('Chat Session', opened['session'])
        start = session.human_requested_at
        incoming = process_event(self.event(text='Still waiting.'))
        self.assertEqual(incoming['session'], session.name)
        session.reload()
        self.assertEqual(session.human_requested_at, start)
        count = frappe.db.count('Chat Message', {'session': session.name})
        self.assertEqual(human.close_inactive(now=start + timedelta(minutes=59)), [])
        self.assertIn(session.name, human.close_inactive(now=start + timedelta(minutes=60)))
        session.reload()
        self.assertEqual((session.status, session.close_reason, session.active_key), ('Closed', 'Inactivity', None))
        self.assertFalse(frappe.db.get_value('Chat Contact', session.contact, 'active_session'))
        self.assertGreaterEqual(frappe.db.count('Chat Message', {'session': session.name}), count)
        self.assertTrue(inbox.history(session.name)['messages'])
        self.notice(opened)

    def test_take_retires_queue_deadline(self):
        user = self.agent()
        opened = self.open()
        session = frappe.get_doc('Chat Session', opened['session'])
        start = utcnow() - timedelta(minutes=59)
        frappe.db.set_value('Chat Session', session.name, 'human_requested_at', start)
        frappe.db.commit()
        frappe.set_user(user)
        human.act(session.name, 'Take')
        frappe.set_user('Administrator')
        self.assertEqual(human.close_inactive(now=start + timedelta(minutes=61)), [])
        session.reload()
        self.assertEqual(session.status, 'Human Active')

    def test_queue_expiry_races_take_and_duplicate_maintenance(self):
        user = self.agent(capacity=3)
        opened = self.open()
        deadline = frappe.get_doc('Chat Session', opened['session']).human_requested_at + timedelta(minutes=60)
        results = self.race([(user, 'act', dict(session_name=opened['session'], action='Take')),
            ('Administrator', 'inactive', dict(now=deadline))])
        session = frappe.get_doc('Chat Session', opened['session'])
        self.assertIn(session.status, ('Closed', 'Human Active'), results)
        if session.status == 'Closed':
            self.assertFalse(results[0]['ok'] and results[1]['ok'], results)
            self.notice(opened)
        else:
            self.assertFalse(frappe.db.exists('Bot Dispatch', {'outbound_key': identity_key(session.name, opened['turn'], 'human_completion')}))
        queued = self.open(sender='duplicate-maintenance')
        deadline = frappe.get_doc('Chat Session', queued['session']).human_requested_at + timedelta(minutes=60)
        results = self.race([('Administrator', 'inactive', dict(now=deadline))] * 2)
        self.assertTrue(all(row['ok'] for row in results), results)
        self.assertEqual(sum(queued['session'] in row['result'] for row in results), 1, results)
        self.notice(queued)

    def test_concurrent_close_creates_one_notice(self):
        opened = self.opened()
        frappe.set_user('Administrator')
        results = self.race([('Administrator', 'act', dict(session_name=opened['session'], action='Close'))] * 2)
        self.assertEqual(sum(row['ok'] for row in results), 1, results)
        self.notice(opened)

    def test_unauthorized_and_rollback_never_publish_notice(self):
        opened = self.opened()
        other = self.agent()
        frappe.set_user(other)
        with self.assertRaises(frappe.PermissionError):
            human.act(opened['session'], 'Close')
        frappe.set_user('Administrator')
        with patch.object(frappe.db, 'commit', side_effect=RuntimeError('Synthetic commit failure')), \
            patch('messaging.conversation.doctype.chat_contact.chat_contact._enqueue') as enqueue:
            with self.assertRaises(RuntimeError):
                human.act(opened['session'], 'Close')
            enqueue.assert_not_called()
        self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'status'), 'Human Active')
        self.assertFalse(frappe.db.exists('Bot Dispatch', {'outbound_key': identity_key(opened['session'], opened['turn'], 'human_completion')}))

    def test_transport_uncertainty_rejection_and_exception_preserve_close(self):
        from messaging.chatbot.doctype.bot_dispatch.delivery import deliver
        from messaging.communication.doctype.communication_provider import communication_provider as providers
        for index, outcome in enumerate(('Rejected', 'Unknown', 'exception')):
            with self.subTest(outcome=outcome):
                frappe.set_user('Administrator')
                opened = self.open(sender='completion-failure-' + str(index))
                simulate_dispatch(opened['dispatch'])
                human.act(opened['session'], 'Close')
                notice = self.notice(opened)
                operation = providers.operation
                sent = []
                def provider_operation(definition, name, *args, **kwargs):
                    if name != 'send':
                        return operation(definition, name, *args, **kwargs)
                    sent.append(args[0])
                    if outcome == 'exception':
                        raise RuntimeError('Synthetic provider error')
                    return dict(outcome=outcome)
                with patch.object(providers, 'operation', side_effect=provider_operation):
                    deliver(notice.name)
                    deliver(notice.name)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(sent[0]['content'], dispatch_content(notice))
                    self.assertEqual(sent[0]['message_type'], 'Automatic')
                notice.reload()
                self.assertEqual(notice.status, 'Unknown' if outcome == 'exception' else outcome)
                self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], ['status', 'close_reason']), ('Closed', 'Manual'))
                self.notice(opened)

    def test_ordinary_closed_dispatch_and_technical_error_cannot_send_completion(self):
        from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import claim
        opened = self.opened()
        frappe.set_user('Administrator')
        session = frappe.get_doc('Chat Session', opened['session'])
        contact = frappe.get_doc('Chat Contact', opened['contact'])
        human._close(session, contact, reason='Technical Error')
        frappe.db.commit()
        self.assertFalse(frappe.db.exists('Bot Dispatch', {'outbound_key': identity_key(session.name, opened['turn'], 'human_completion')}))
        # A separate ordinary prepared response never gains the close-notice exception.
        ordinary = frappe.get_doc(dict(doctype='Bot Dispatch', outbound_key=identity_key(session.name, 'ordinary'),
            contact=contact.name, session=session.name, turn=opened['turn'], channel=contact.channel,
            account=contact.account, content='Ordinary output.', status='Prepared')).insert(ignore_permissions=True)
        frappe.db.commit()
        self.assertIsNone(claim(ordinary.name))
        ordinary.reload()
        self.assertEqual((ordinary.status, ordinary.diagnostic_code), ('Rejected', 'episode_closed_before_handoff'))


class TestConversationInbox(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		from messaging.communication.doctype.communication_provider.communication_provider import (
			get_provider_states,
			set_provider_enabled,
		)
		from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
		self.provider_states_before = get_provider_states()
		sync_communication_providers()
		set_provider_enabled("EMULR", True)
		self.channel = "emulator"
		self.created_channel = False
		if not frappe.db.exists("Communication Channel", self.channel):
			frappe.get_doc({
				"doctype": "Communication Channel",
				"provider": "EMULR",
				"channel_name": "Synthetic Emulator Channel",
				"enabled": 1,
			}).insert(ignore_permissions=True)
			self.created_channel = True
		self.channel_name = frappe.db.get_value("Communication Channel", self.channel, "channel_name")
		self.account_name = "Synthetic Inbox Account"
		self.account_doc = frappe.get_doc({
			"doctype": "Communication Channel Account",
			"channel": self.channel,
			"account_id": "inbox-" + frappe.generate_hash(length=10),
			"account_name": self.account_name,
			"enabled": 1,
		}).insert(ignore_permissions=True)
		self.account = self.account_doc.name
		self.sender_q = "sender_q_" + frappe.generate_hash(length=8)
		self.sender_r = "sender_r_" + frappe.generate_hash(length=8)
		self.sender_a = "sender_a_" + frappe.generate_hash(length=8)
		self.sender_c = "sender_c_" + frappe.generate_hash(length=8)
		self.stamp = utcnow()

		self.agent_doc = frappe.get_doc({
			"doctype": "Chat Agent",
			"user": "Administrator",
			"channel": self.channel,
			"account": self.account,
			"enabled": 1,
			"available": 1,
			"capacity": 5
		}).insert(ignore_permissions=True)

		self.contact_q = frappe.get_doc({
			"doctype": "Chat Contact",
			"channel": self.channel,
			"account": self.account,
			"external_sender": self.sender_q,
			"identity_key": identity_key(self.channel, self.account, self.sender_q),
			"registration_status": "Complete",
			"parameter_values": '{"name": "Queued Subscriber"}'
		}).insert(ignore_permissions=True)

		self.contact_r = frappe.get_doc({
			"doctype": "Chat Contact",
			"channel": self.channel,
			"account": self.account,
			"external_sender": self.sender_r,
			"identity_key": identity_key(self.channel, self.account, self.sender_r),
			"registration_status": "Complete",
			"parameter_values": '{"name": "Pending Subscriber"}'
		}).insert(ignore_permissions=True)

		self.contact_a = frappe.get_doc({
			"doctype": "Chat Contact",
			"channel": self.channel,
			"account": self.account,
			"external_sender": self.sender_a,
			"identity_key": identity_key(self.channel, self.account, self.sender_a),
			"registration_status": "Complete",
			"parameter_values": '{"name": "Active Subscriber"}'
		}).insert(ignore_permissions=True)

		self.contact_c = frappe.get_doc({
			"doctype": "Chat Contact",
			"channel": self.channel,
			"account": self.account,
			"external_sender": self.sender_c,
			"identity_key": identity_key(self.channel, self.account, self.sender_c),
			"registration_status": "Complete",
			"parameter_values": '{"name": "Closed Subscriber"}'
		}).insert(ignore_permissions=True)

		self.session_queued = frappe.get_doc({
			"doctype": "Chat Session",
			"contact": self.contact_q.name,
			"channel": self.channel,
			"account": self.account,
			"status": "Human Queued",
			"human_requested_at": self.stamp - timedelta(minutes=15),
			"last_activity": self.stamp - timedelta(minutes=10)
		}).insert(ignore_permissions=True)

		with _writing():
			self.msg = frappe.get_doc({
				"doctype": "Chat Message",
				"message_key": identity_key(self.session_queued.name, "1"),
				"session": self.session_queued.name,
				"contact": self.contact_q.name,
				"sequence": 1,
				"kind": "Incoming",
				"content": "Need assistance with billing problem",
				"sent_at": self.stamp - timedelta(minutes=10)
			}).insert(ignore_permissions=True)
		self.session_queued.message_sequence = 1
		self.session_queued.save(ignore_permissions=True)

		self.session_requested = frappe.get_doc({
			"doctype": "Chat Session",
			"contact": self.contact_r.name,
			"channel": self.channel,
			"account": self.account,
			"status": "Human Requested",
			"human_requested_at": self.stamp - timedelta(minutes=5),
			"last_activity": self.stamp - timedelta(minutes=5)
		}).insert(ignore_permissions=True)

		self.session_active = frappe.get_doc({
			"doctype": "Chat Session",
			"contact": self.contact_a.name,
			"channel": self.channel,
			"account": self.account,
			"status": "Human Active",
			"assigned_agent": "Administrator",
			"human_requested_at": self.stamp - timedelta(minutes=30),
			"last_activity": self.stamp - timedelta(minutes=2)
		}).insert(ignore_permissions=True)

		self.session_closed = frappe.get_doc({
			"doctype": "Chat Session",
			"contact": self.contact_c.name,
			"channel": self.channel,
			"account": self.account,
			"status": "Closed",
			"human_requested_at": self.stamp - timedelta(minutes=60),
			"last_activity": self.stamp - timedelta(minutes=45)
		}).insert(ignore_permissions=True)

		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for s in (self.session_queued, self.session_requested, self.session_active, self.session_closed):
			frappe.db.delete("Bot Dispatch", {"session": s.name})
			frappe.db.delete("Chat Turn", {"session": s.name})
			frappe.db.delete("Chat Message", {"session": s.name})
			frappe.db.delete("Chat Session", {"name": s.name})
		for c in (self.contact_q, self.contact_r, self.contact_a, self.contact_c):
			frappe.db.delete("Bot Dispatch", {"contact": c.name})
			frappe.db.delete("Chat Contact", {"name": c.name})
		if getattr(self, "account", None):
			frappe.db.delete("Bot Dispatch", {"account": self.account})
		if getattr(self, "agent_doc", None) and frappe.db.exists("Chat Agent", self.agent_doc.name):
			frappe.delete_doc("Chat Agent", self.agent_doc.name, ignore_permissions=True)
		if getattr(self, "account_doc", None) and frappe.db.exists("Communication Channel Account", self.account_doc.name):
			frappe.delete_doc("Communication Channel Account", self.account_doc.name, ignore_permissions=True)
		if getattr(self, "created_channel", False) and frappe.db.exists("Communication Channel", self.channel):
			if not frappe.db.exists("Communication Channel Account", {"channel": self.channel}):
				frappe.delete_doc("Communication Channel", self.channel, ignore_permissions=True)
		from messaging.communication.doctype.communication_provider.communication_provider import _lock_provider_states, _write_provider_states
		_lock_provider_states()
		_write_provider_states(self.provider_states_before)
		frappe.db.commit()

	def test_inbox_summary(self):
		summary = inbox.inbox_summary()
		self.assertGreaterEqual(summary["waiting"], 1)
		self.assertGreaterEqual(summary["pending"], 1)
		self.assertEqual(summary["total"], summary["waiting"] + summary["pending"])

		summary_chan = inbox.inbox_summary(channel=self.channel)
		self.assertGreaterEqual(summary_chan["total"], 2)

	def test_unassigned_conversations_excludes_active_and_closed(self):
		rows = inbox.conversations(unassigned_only=1, limit=50)
		session_names = [r["name"] for r in rows]
		self.assertIn(self.session_queued.name, session_names)
		self.assertIn(self.session_requested.name, session_names)
		self.assertNotIn(self.session_active.name, session_names)
		self.assertNotIn(self.session_closed.name, session_names)

	def test_filter_waiting_and_pending(self):
		waiting_rows = inbox.conversations(status="Waiting", unassigned_only=1)
		waiting_names = [r["name"] for r in waiting_rows]
		self.assertIn(self.session_queued.name, waiting_names)
		self.assertNotIn(self.session_requested.name, waiting_names)

		pending_rows = inbox.conversations(status="Pending", unassigned_only=1)
		pending_names = [r["name"] for r in pending_rows]
		self.assertIn(self.session_requested.name, pending_names)
		self.assertNotIn(self.session_queued.name, pending_names)

	def test_filter_by_channel_and_account(self):
		rows = inbox.conversations(channel=self.channel, account=self.account, unassigned_only=1)
		self.assertTrue(all(r["channel"] == self.channel for r in rows))
		self.assertTrue(all(r["account"] == self.account for r in rows))

	def test_search_by_contact_name_and_sender(self):
		rows = inbox.conversations(search="Queued Subscriber", unassigned_only=1)
		self.assertIn(self.session_queued.name, [r["name"] for r in rows])

		rows_sender = inbox.conversations(search=self.sender_q, unassigned_only=1)
		self.assertIn(self.session_queued.name, [r["name"] for r in rows_sender])

	def test_public_session_metadata(self):
		rows = inbox.conversations(search=self.session_queued.name, unassigned_only=1)
		self.assertTrue(len(rows) >= 1)
		item = [r for r in rows if r["name"] == self.session_queued.name][0]
		self.assertEqual(item["contact_name"], "Queued Subscriber")
		self.assertEqual(item["channel_name"], self.channel_name)
		self.assertEqual(item["account_name"], self.account_name)
		self.assertEqual(item["last_message"], "Need assistance with billing problem")
		self.assertEqual(item["last_message_kind"], "Incoming")
		self.assertGreater(item["wait_time_seconds"], 0)

	def test_history_provenance(self):
		data = inbox.history(self.session_queued.name)
		self.assertIn("provenance", data)
		self.assertEqual(data["provenance"]["channel"]["channel_name"], self.channel_name)
		self.assertEqual(data["provenance"]["account"]["account_name"], self.account_name)
		self.assertEqual(data["contact"]["display_name"], "Queued Subscriber")
		self.assertEqual(len(data["messages"]), 1)
		self.assertEqual(data["messages"][0]["content"], "Need assistance with billing problem")

	def test_take_conversation_removes_from_inbox(self):
		rows_before = inbox.conversations(search=self.session_queued.name, unassigned_only=1)
		self.assertEqual(len(rows_before), 1)

		result = human.act(self.session_queued.name, "Take")
		self.assertEqual(result["status"], "Human Active")
		self.assertEqual(result["assigned_agent"], "Administrator")

		rows_after = inbox.conversations(search=self.session_queued.name, unassigned_only=1)
		self.assertEqual(len(rows_after), 0)

		with self.assertRaises(frappe.ValidationError):
			human.act(self.session_queued.name, "Take")


def concurrent_inbox_take(site, sites_path, user, session_name, barrier, results):
	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.set_user(user)
	try:
		with patch('requests.sessions.Session.request', side_effect=AssertionError('Network is forbidden')):
			barrier.wait(timeout=20)
			result = human.act(session_name=session_name, action='Take')
			results.put(dict(ok=True, result=result))
	except Exception as exc:
		results.put(dict(ok=False, error=type(exc).__name__, detail=str(exc)))
	finally:
		frappe.db.rollback()
		frappe.destroy()


class TestInboxEndToEnd(unittest.TestCase):
	configure = native.TestChatTurn.configure
	ensure_test_channel = native.TestChatTurn.ensure_test_channel
	event = native.TestChatTurn.event
	flow = native.TestChatTurn.flow
	parameter = native.TestChatTurn.parameter
	phrase = native.TestChatTurn.phrase
	state = native.TestChatTurn.state

	def setUp(self):
		network = patch('requests.sessions.Session.request', side_effect=AssertionError('Network is forbidden'))
		network.start()
		self.addCleanup(network.stop)
		native.TestChatTurn.setUp(self)
		from messaging.communication.doctype.communication_provider.communication_provider import (
			get_provider_states,
			set_provider_enabled,
		)
		self.provider_states_before = get_provider_states()
		set_provider_enabled('EMULR', True)
		self.ensure_test_channel('emulator')
		self.binding = frappe.get_doc(dict(
			doctype='Communication Channel Account',
			channel='emulator',
			account_id=self.prefix,
			account_name='Synthetic Inbox Account',
			enabled=1,
		)).insert(set_name=self.prefix)
		self.users = []
		from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import create_configuration
		self.ai_configuration = create_configuration(self.prefix + '_ai', api_key='synthetic-inbox-key')
		flow = self.flow(method='controlled_human', show_in_menu=0, outputs=[dict(status_code='success', output_mode='Message')])
		self.configure(
			human_attention_enabled=1,
			human_inactivity_minutes=60,
			interpretation_enabled=1,
			active_ai_configuration=self.ai_configuration.name,
			human_flow=flow.name,
			human_transfer_states=[dict(state=state_name(flow.name, 'success'))],
		)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user('Administrator')
		contacts = frappe.get_all('Chat Contact', filters={'account': ['in', self.accounts]}, pluck='name')
		sessions = frappe.get_all('Chat Session', filters={'contact': ['in', contacts]}, pluck='name') if contacts else []
		if sessions:
			frappe.db.delete('Process Execution Log', {'session': ['in', sessions]})
			frappe.db.delete('Chat Message', {'session': ['in', sessions]})
			frappe.db.delete('Bot Dispatch', {'session': ['in', sessions]})
			frappe.db.delete('Chat Turn', {'session': ['in', sessions]})
			frappe.db.delete('Chat Session', {'name': ['in', sessions]})
		if contacts:
			frappe.db.delete('Chat Contact', {'name': ['in', contacts]})
		for user in getattr(self, 'users', []):
			frappe.db.delete('Chat Agent', {'user': user})
		frappe.db.commit()
		native.TestChatTurn.tearDown(self)
		frappe.delete_doc('AI Configuration', self.ai_configuration.name, delete_permanently=True)
		frappe.delete_doc('Communication Channel Account', self.binding.name, ignore_permissions=True)
		from messaging.communication.doctype.communication_provider.communication_provider import (
			_lock_provider_states,
			_write_provider_states,
		)
		_lock_provider_states()
		_write_provider_states(self.provider_states_before)
		for user in getattr(self, 'users', []):
			frappe.delete_doc('User', user, ignore_permissions=True, force=True)
		frappe.db.commit()

	def agent(self, *, capacity=1, available=1, account=None):
		user = self.prefix + str(len(self.users)) + '@example.invalid'
		with patch('frappe.enqueue'):
			frappe.get_doc(dict(
				doctype='User',
				email=user,
				first_name='Controlled Agent',
				send_welcome_email=0,
				user_type='System User',
				roles=[dict(role='Messaging Agent')],
			)).insert(ignore_permissions=True)
		self.users.append(user)
		frappe.get_doc(dict(
			doctype='Chat Agent',
			user=user,
			channel='emulator',
			account=account or self.prefix,
			enabled=1,
			available=available,
			capacity=capacity,
		)).insert(ignore_permissions=True)
		frappe.db.commit()
		return user

	def open(self, sender='external-user'):
		return process_event(self.event(channel='emulator', sender=sender, text='Please connect me.', human_requested=True))

	def test_manual_take_moves_queued_request_to_mine_and_leaves_queue(self):
		agent_user = self.agent(available=1, capacity=2)
		opened = self.open(sender='auto-sub')
		session_name = opened['session']
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Queued')
		self.assertFalse(frappe.db.get_value('Chat Session', session_name, 'assigned_agent'))

		frappe.set_user(agent_user)
		queue_summary = inbox.inbox_summary(channel='emulator', account=self.prefix)
		self.assertEqual(queue_summary['waiting'], 1)
		self.assertEqual(queue_summary['mine'], 0)
		queue_rows = inbox.conversations(unassigned_only=1, channel='emulator', account=self.prefix)
		self.assertEqual([r['name'] for r in queue_rows], [session_name])
		self.assertTrue(queue_rows[0]['can_take'])

		human.act(session_name, 'Take')
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Active')
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'assigned_agent'), agent_user)

		summary = inbox.inbox_summary(channel='emulator', account=self.prefix)
		self.assertEqual(summary['total'], 0)
		self.assertEqual(summary['waiting'], 0)
		self.assertEqual(summary['pending'], 0)
		self.assertEqual(summary['mine'], 1)
		self.assertEqual(summary['mine_active'], 1)
		self.assertEqual(summary['mine_paused'], 0)
		self.assertFalse(summary['is_manager'])

		mine_rows = inbox.conversations(mine=1, channel='emulator', account=self.prefix)
		self.assertEqual([r['name'] for r in mine_rows], [session_name])
		self.assertTrue(mine_rows[0]['can_reply'])
		self.assertFalse(mine_rows[0]['can_pause'])
		self.assertTrue(mine_rows[0]['can_close'])
		self.assertFalse(mine_rows[0]['can_take'])

		queue_rows = inbox.conversations(unassigned_only=1, channel='emulator', account=self.prefix)
		self.assertEqual(queue_rows, [])

	def test_queue_messages_take_reply_mark_read_pause_resume_close_and_bot_resumption(self):
		agent_user = self.agent(available=0, capacity=2)
		sender_id = '000' + str(abs(hash(self.prefix)) % 10**15).zfill(15)
		emu_contact = frappe.get_doc(dict(
			doctype='Emulator Contact',
			sender_id=sender_id,
			contact_name='Queued Test Sub',
			enabled=1,
		)).insert(ignore_permissions=True)
		frappe.db.commit()
		try:
			opened = self.open(sender=sender_id)
			session_name = opened['session']
			self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Queued')

			# Additional subscriber message arriving while still queued must remain visible and queued.
			queued_msg = process_event(self.event(channel='emulator', sender=sender_id, text='Still waiting here'))
			self.assertEqual(queued_msg['session'], session_name)
			self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Queued')

			frappe.db.set_value('Chat Agent', {'user': agent_user}, 'available', 1)
			frappe.db.commit()

			frappe.set_user(agent_user)
			before_hist = inbox.history(session_name)
			self.assertTrue(before_hist['session']['can_take'])
			self.assertFalse(before_hist['session']['can_reply'])
			self.assertIn('Still waiting here', [m['content'] for m in before_hist['messages']])
			self.assertEqual(before_hist['agent_status']['remaining'], 2)

			res_take = human.act(session_name, 'Take')
			self.assertEqual(res_take['status'], 'Human Active')
			self.assertEqual(res_take['assigned_agent'], agent_user)

			sent = inbox.send(session_name, 'Hello from human agent', 'cid-' + frappe.generate_hash(length=12))
			self.assertEqual(sent['message_type'], 'Human')
			self.assertEqual(sent['state'], 'Accepted')

			active_hist = inbox.history(session_name)
			self.assertTrue(active_hist['session']['can_reply'])
			self.assertTrue(active_hist['can_read'])
			human_msgs = [m for m in active_hist['messages'] if m.get('content') == 'Hello from human agent']
			self.assertEqual(len(human_msgs), 1)
			self.assertEqual(human_msgs[0]['transport']['status'], 'Accepted')

			read_cursor = inbox.mark_read(session_name, active_hist['session']['message_sequence'])
			self.assertEqual(read_cursor, active_hist['session']['message_sequence'])
			mine_rows = inbox.conversations(mine=1, channel='emulator', account=self.prefix)
			self.assertEqual(mine_rows[0]['unread'], 0)

			# Pause and Resume are rejected cleanly without mutating session state.
			with self.assertRaises(frappe.ValidationError):
				human.act(session_name, 'Pause')
			with self.assertRaises(frappe.ValidationError):
				human.act(session_name, 'Resume')
			self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Active')
			second_send = inbox.send(session_name, 'Continued active reply', 'cid-' + frappe.generate_hash(length=12))
			self.assertEqual(second_send['state'], 'Accepted')

			# Close finalizes human intervention, excludes from operational summary, and restarts bot on next message.
			human.act(session_name, 'Close')
			summary_closed = inbox.inbox_summary(channel='emulator', account=self.prefix)
			self.assertEqual(summary_closed['mine'], 0)
			closed_rows = inbox.conversations(mine=1, status='Closed', channel='emulator', account=self.prefix)
			self.assertEqual([r['name'] for r in closed_rows], [session_name])
			with self.assertRaises(frappe.PermissionError):
				inbox.send(session_name, 'Blocked after close', 'cid-' + frappe.generate_hash(length=12))

			frappe.set_user('Administrator')
			next_in = process_event(self.event(channel='emulator', sender=sender_id, text='Hi again'))
			self.assertNotEqual(next_in['session'], session_name)
			self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Closed')
			self.assertEqual(frappe.db.get_value('Chat Session', next_in['session'], 'status'), 'Active')
		finally:
			frappe.set_user('Administrator')
			frappe.db.delete('Emulator Contact', {'name': emu_contact.name})
			frappe.db.commit()

	def test_concurrent_take_race_and_realtime_session_payload(self):
		agent_a = self.agent(available=0)
		opened = self.open(sender='race-sub')
		session_name = opened['session']
		agent_b = self.agent(available=1)
		frappe.db.set_value('Chat Agent', {'user': agent_a}, 'available', 1)
		frappe.db.commit()

		ctx = multiprocessing.get_context('spawn')
		barrier = ctx.Barrier(2)
		results_q = ctx.Queue()
		children = [
			ctx.Process(
				target=concurrent_inbox_take,
				args=(frappe.local.site, str(frappe.local.sites_path), user, session_name, barrier, results_q),
			)
			for user in (agent_a, agent_b)
		]
		for c in children:
			c.start()
		results = [results_q.get(timeout=45) for _ in children]
		for c in children:
			c.join(timeout=10)
			if c.is_alive():
				c.terminate()
				c.join()
				self.fail('Concurrent Take exceeded deadline')
		frappe.db.rollback()

		self.assertEqual(sum(1 for r in results if r['ok']), 1, results)
		self.assertEqual(sum(1 for r in results if not r['ok']), 1, results)

		winner = frappe.db.get_value('Chat Session', session_name, 'assigned_agent')
		self.assertIn(winner, (agent_a, agent_b))
		loser = agent_b if winner == agent_a else agent_a

		frappe.set_user(loser)
		with self.assertRaises(frappe.PermissionError):
			inbox.send(session_name, 'Unauthorized reply', 'cid-' + frappe.generate_hash(length=12))

		with patch('messaging.conversation.doctype.chat_session.human.frappe.publish_realtime') as pub:
			frappe.set_user(winner)
			human.act(session_name, 'Close')
			matched = [
				call_args for call_args in pub.call_args_list
				if call_args.args and call_args.args[0] == 'messaging_conversation_update'
				and isinstance(call_args.args[1], dict) and call_args.args[1].get('session') == session_name
			]
			self.assertTrue(len(matched) >= 1)

	def test_permissions_account_isolation_other_agents_and_disabled_binding(self):
		agent1 = self.agent(available=1)
		agent2 = self.agent(available=1)
		opened = self.open(sender='iso-sub')
		session_name = opened['session']
		frappe.set_user(agent1)
		human.act(session_name, 'Take')
		owner = agent1
		other = agent2

		# Regular agent cannot query other_agents supervision list.
		frappe.set_user(other)
		with self.assertRaises(frappe.PermissionError):
			inbox.conversations(other_agents=1, channel='emulator', account=self.prefix)

		# System Manager sees session in other_agents supervision and summary['others'] == 1.
		frappe.set_user('Administrator')
		mgr_summary = inbox.inbox_summary(channel='emulator', account=self.prefix)
		self.assertTrue(mgr_summary['is_manager'])
		self.assertEqual(mgr_summary['others'], 1)
		others_rows = inbox.conversations(other_agents=1, channel='emulator', account=self.prefix)
		self.assertEqual([r['name'] for r in others_rows], [session_name])

		# Disabled provider/binding refuses send BEFORE creating any Prepared Bot Dispatch.
		frappe.set_user(owner)
		before_dispatches = frappe.db.count('Bot Dispatch', {'session': session_name})
		with patch('messaging.conversation.doctype.chat_session.inbox.resolve_binding', side_effect=frappe.PermissionError('Provider disabled')):
			with self.assertRaises(frappe.PermissionError):
				inbox.send(session_name, 'Should not orphan', 'cid-' + frappe.generate_hash(length=12))
		self.assertEqual(frappe.db.count('Bot Dispatch', {'session': session_name}), before_dispatches)

	def test_inbox_summary_counts_more_than_twenty_sessions(self):
		agent_user = self.agent(available=1)
		base_time = utcnow()
		created = []
		created_contacts = []
		for idx in range(22):
			sender_i = f'count-sub-{idx}'
			ct = frappe.get_doc({
				'doctype': 'Chat Contact',
				'channel': 'emulator',
				'account': self.prefix,
				'external_sender': sender_i,
				'identity_key': identity_key('emulator', self.prefix, sender_i),
				'registration_status': 'Complete',
			}).insert(ignore_permissions=True)
			created_contacts.append(ct.name)
			doc = frappe.get_doc({
				'doctype': 'Chat Session',
				'contact': ct.name,
				'channel': 'emulator',
				'account': self.prefix,
				'status': 'Human Queued',
				'human_requested_at': base_time - timedelta(minutes=idx + 1),
				'last_activity': base_time - timedelta(minutes=idx + 1),
			}).insert(ignore_permissions=True)
			created.append(doc.name)
		frappe.db.commit()
		try:
			frappe.set_user(agent_user)
			summary = inbox.inbox_summary(channel='emulator', account=self.prefix)
			self.assertEqual(summary['waiting'], 22)
			self.assertEqual(summary['total'], 22)
		finally:
			frappe.set_user('Administrator')
			frappe.db.delete('Chat Session', {'name': ['in', created]})
			frappe.db.delete('Chat Contact', {'name': ['in', created_contacts]})
			frappe.db.commit()




class TestManualHumanQueueAndNotifications(TestInboxEndToEnd):
	def setUp(self):
		super().setUp()
		self.extra_grants = []
		self.extra_error_logs = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user('Administrator')
		contacts = frappe.get_all('Chat Contact', filters={'account': ['in', self.accounts]}, pluck='name')
		sessions = frappe.get_all('Chat Session', filters={'contact': ['in', contacts]}, pluck='name') if contacts else []
		if sessions:
			frappe.db.delete('Notification Log', {'document_type': 'Chat Session', 'document_name': ['in', sessions]})
			for sess_name in sessions:
				frappe.db.delete('Error Log', {'method': 'Human request notification failed', 'error': ['like', f'%{sess_name}%']})
		for grant_name in self.extra_grants:
			if frappe.db.exists('Chat Agent', grant_name):
				frappe.delete_doc('Chat Agent', grant_name, ignore_permissions=True)
		frappe.db.commit()
		super().tearDown()

	def _create_user(self, *, roles=('Messaging Agent',), enabled=1, language='es'):
		email = self.prefix + str(len(self.users)) + '@example.invalid'
		with patch('frappe.enqueue'):
			frappe.get_doc(dict(
				doctype='User',
				email=email,
				first_name='Desk Agent',
				enabled=enabled,
				language=language,
				send_welcome_email=0,
				user_type='System User',
				roles=[dict(role=r) for r in roles],
			)).insert(ignore_permissions=True)
		self.users.append(email)
		frappe.db.commit()
		return email

	def _grant(self, user, *, channel='emulator', account=None, enabled=1, available=1, capacity=1):
		doc = frappe.get_doc(dict(
			doctype='Chat Agent',
			user=user,
			channel=channel,
			account=account or self.prefix,
			enabled=enabled,
			available=available,
			capacity=capacity,
		)).insert(ignore_permissions=True)
		self.extra_grants.append(doc.name)
		frappe.db.commit()
		return doc.name

	def test_single_and_multiple_available_agents_still_queue_manually_and_notify_desk_without_email(self):
		agent_one = self._create_user(language='es')
		agent_two = self._create_user(language='es')
		self._grant(agent_one, available=1, capacity=3)
		self._grant(agent_two, available=1, capacity=3)

		sender = 'irene-' + frappe.generate_hash(length=6)
		contact = frappe.get_doc(dict(
			doctype='Chat Contact',
			channel='emulator',
			account=self.prefix,
			external_sender=sender,
			identity_key=identity_key('emulator', self.prefix, sender),
			registration_status='Complete',
			parameter_values=json.dumps({'name': 'Irene Selva', 'secret_contract': 'SECRET-999'}),
		)).insert(ignore_permissions=True)
		frappe.db.commit()

		with patch('frappe.sendmail', side_effect=AssertionError('Desk bell alert must not send email')):
			opened = self.open(sender=sender)
			session_name = opened['session']

		session = frappe.get_doc('Chat Session', session_name)
		self.assertEqual(session.status, 'Human Queued')
		self.assertFalse(session.assigned_agent)
		self.assertFalse(session.assigned_at)
		self.assertEqual(opened['response'], 'Human decision: success.')
		self.assertNotIn(settings_text(frappe.get_single('Bot Settings'), 'human_active_message'), opened['response'])
		self.assertEqual(frappe.db.count('Bot Dispatch', {'session': session_name, 'message_type': 'Human'}), 0)

		logs = frappe.get_all(
			'Notification Log',
			filters={'document_type': 'Chat Session', 'document_name': session_name},
			fields=['name', 'for_user', 'type', 'link', 'title', 'description', 'read'],
		)
		self.assertEqual({row.for_user for row in logs}, {agent_one, agent_two})
		for row in logs:
			self.assertEqual(row.type, 'Alert')
			self.assertEqual(row.link, f'/desk/conversation-inbox?session={session_name}')
			self.assertEqual(row.read, 0)
			self.assertIn('Irene Selva', row.title)
			self.assertIn('Synthetic Inbox Account', row.title)
			self.assertIn('Nueva solicitud de atención humana', row.title)
			self.assertNotIn('SECRET-999', row.title)
			self.assertNotIn('SECRET-999', row.description or '')

		# Follow-up message in Human Queued must neither auto-assign nor duplicate notifications.
		with patch('frappe.sendmail', side_effect=AssertionError('Follow-up message must not notify')):
			follow = process_event(self.event(channel='emulator', sender=sender, text='Any update?'))
		self.assertEqual(follow['session'], session_name)
		session.reload()
		self.assertEqual(session.status, 'Human Queued')
		self.assertFalse(session.assigned_agent)
		self.assertEqual(
			frappe.db.count('Notification Log', {'document_type': 'Chat Session', 'document_name': session_name}),
			2,
		)

		# Explicit Take by agent_one transitions to Human Active and dispatches assignment notice once.
		frappe.set_user(agent_one)
		res = human.act(session_name, 'Take')
		self.assertEqual(res['status'], 'Human Active')
		self.assertEqual(res['assigned_agent'], agent_one)
		self.assertEqual(frappe.db.count('Bot Dispatch', {'session': session_name, 'message_type': 'Human'}), 1)

	def test_recipient_filtering_capacity_exhaustion_dedup_and_zero_recipients(self):
		# 0 eligible recipients still queues cleanly and never falls back to Administrator.
		no_agent_open = self.open(sender='no-agent-' + frappe.generate_hash(length=6))
		self.assertEqual(frappe.db.get_value('Chat Session', no_agent_open['session'], 'status'), 'Human Queued')
		self.assertFalse(frappe.db.get_value('Chat Session', no_agent_open['session'], 'assigned_agent'))
		self.assertEqual(
			frappe.db.count('Notification Log', {'document_type': 'Chat Session', 'document_name': no_agent_open['session']}),
			0,
		)

		eligible = self._create_user(language='es')
		disabled_user = self._create_user(enabled=1)
		no_role = self._create_user(roles=('Messaging Agent', 'Desk User'))
		unavailable = self._create_user()
		disabled_grant = self._create_user()
		full_agent = self._create_user()
		other_account_agent = self._create_user()

		self._grant(eligible, available=1, capacity=2)
		self._grant(disabled_user, available=1, capacity=2)
		self._grant(no_role, available=1, capacity=2)
		self._grant(unavailable, available=0, capacity=2)
		self._grant(disabled_grant, enabled=0, available=1, capacity=2)
		self._grant(full_agent, available=1, capacity=1)
		self._grant(other_account_agent, account='other-account-' + self.prefix, available=1, capacity=2)

		frappe.db.set_value('User', disabled_user, 'enabled', 0)
		no_role_doc = frappe.get_doc('User', no_role)
		no_role_doc.roles = [r for r in no_role_doc.roles if r.role != 'Messaging Agent']
		no_role_doc.save(ignore_permissions=True)
		frappe.db.commit()

		# Occupy full_agent capacity=1 with an existing active session on this account.
		occ_sender = 'occ-' + frappe.generate_hash(length=6)
		occ_contact = frappe.get_doc(dict(
			doctype='Chat Contact',
			channel='emulator',
			account=self.prefix,
			external_sender=occ_sender,
			identity_key=identity_key('emulator', self.prefix, occ_sender),
			registration_status='Complete',
		)).insert(ignore_permissions=True)
		frappe.get_doc(dict(
			doctype='Chat Session',
			contact=occ_contact.name,
			channel='emulator',
			account=self.prefix,
			status='Human Active',
			assigned_agent=full_agent,
			human_requested_at=utcnow(),
			last_activity=utcnow(),
		)).insert(ignore_permissions=True)
		frappe.db.commit()

		from messaging.conversation.doctype.chat_session.notifications import eligible_agent_recipients
		recipients = eligible_agent_recipients('emulator', self.prefix)
		self.assertEqual([r['user'] for r in recipients], [eligible])

		opened = self.open(sender='filtered-' + frappe.generate_hash(length=6))
		notified = frappe.get_all(
			'Notification Log',
			filters={'document_type': 'Chat Session', 'document_name': opened['session']},
			pluck='for_user',
		)
		self.assertEqual(notified, [eligible])

	def test_rollback_never_notifies_and_notification_failure_preserves_queued_session(self):
		agent_user = self._create_user()
		self._grant(agent_user, available=1, capacity=2)

		# Case F: Rolled back transaction must emit zero notifications.
		rb_sender = 'rollback-' + frappe.generate_hash(length=6)
		contact = frappe.get_doc(dict(
			doctype='Chat Contact',
			channel='emulator',
			account=self.prefix,
			external_sender=rb_sender,
			identity_key=identity_key('emulator', self.prefix, rb_sender),
			registration_status='Complete',
		)).insert(ignore_permissions=True)
		session = frappe.get_doc(dict(
			doctype='Chat Session',
			contact=contact.name,
			channel='emulator',
			account=self.prefix,
			status='Active',
		)).insert(ignore_permissions=True)
		frappe.db.commit()

		turn = frappe.get_doc(dict(
			doctype='Chat Turn',
			contact=contact.name,
			session=session.name,
			channel='emulator',
			account=self.prefix,
			event_key=identity_key(session.name, 'rollback-turn'),
			input_summary=json.dumps({'language': 'es'}),
		)).insert(ignore_permissions=True)
		# Commit setup so session & turn exist, then trigger notify callback and rollback.
		from messaging.conversation.doctype.chat_session.notifications import notify_human_request_queued
		frappe.db.commit()
		session.status = 'Human Queued'
		session.save(ignore_permissions=True)
		frappe.db.after_commit.add(lambda: notify_human_request_queued(
			session_name=session.name,
			contact_name=contact.name,
			channel='emulator',
			account=self.prefix,
			turn=turn.name,
		))
		frappe.db.rollback()
		self.assertEqual(
			frappe.db.count('Notification Log', {'document_type': 'Chat Session', 'document_name': session.name}),
			0,
		)

		# Case G: Notification failure after commit must never roll back or hide Human Queued session.
		with patch(
			'messaging.conversation.doctype.chat_session.notifications.enqueue_create_notification',
			side_effect=RuntimeError('Synthetic notification backend failure'),
		):
			opened = self.open(sender='fail-safe-' + frappe.generate_hash(length=6))
		self.assertEqual(frappe.db.get_value('Chat Session', opened['session'], 'status'), 'Human Queued')
		self.assertFalse(frappe.db.get_value('Chat Session', opened['session'], 'assigned_agent'))
		self.assertTrue(
			frappe.db.exists(
				'Error Log',
				{'method': 'Human request notification failed', 'error': ['like', f"%{opened['session']}%"]},
			)
		)

	def test_inactivity_closes_queued_and_assigned_but_not_requested(self):
		agent_user = self._create_user()
		self._grant(agent_user, available=1, capacity=5)
		old_time = utcnow() - timedelta(minutes=120)

		def make_session(sender_tag, status, assigned=None):
			ct = frappe.get_doc(dict(
				doctype='Chat Contact',
				channel='emulator',
				account=self.prefix,
				external_sender=sender_tag,
				identity_key=identity_key('emulator', self.prefix, sender_tag),
				registration_status='Complete',
			)).insert(ignore_permissions=True)
			doc = frappe.get_doc(dict(
				doctype='Chat Session',
				contact=ct.name,
				channel='emulator',
				account=self.prefix,
				status=status,
				assigned_agent=assigned,
				human_requested_at=old_time if status != 'Human Requested' else None,
				assigned_at=old_time if assigned else None,
				last_activity=old_time,
			)).insert(ignore_permissions=True)
			ct.active_session = doc.name
			ct.save(ignore_permissions=True)
			return doc.name

		queued_id = make_session('inact-q-' + frappe.generate_hash(length=5), 'Human Queued')
		requested_id = make_session('inact-r-' + frappe.generate_hash(length=5), 'Human Requested')
		active_id = make_session('inact-a-' + frappe.generate_hash(length=5), 'Human Active', assigned=agent_user)
		paused_id = make_session('inact-p-' + frappe.generate_hash(length=5), 'Human Paused', assigned=agent_user)
		frappe.db.commit()

		closed = human.close_inactive()
		self.assertIn(queued_id, closed)
		self.assertNotIn(requested_id, closed)
		self.assertIn(active_id, closed)
		self.assertIn(paused_id, closed)

		self.assertEqual(frappe.db.get_value('Chat Session', queued_id, 'status'), 'Closed')
		self.assertEqual(frappe.db.get_value('Chat Session', requested_id, 'status'), 'Human Requested')
		self.assertEqual(frappe.db.get_value('Chat Session', active_id, 'status'), 'Closed')
		self.assertEqual(frappe.db.get_value('Chat Session', active_id, 'close_reason'), 'Inactivity')
		self.assertEqual(frappe.db.get_value('Chat Session', paused_id, 'status'), 'Closed')
		self.assertEqual(frappe.db.get_value('Chat Session', paused_id, 'close_reason'), 'Inactivity')
		prev_lang = getattr(frappe.local, 'lang', 'en')
		frappe.local.lang = 'es'
		try:
			inact_hist = inbox.history(active_id)
		finally:
			frappe.local.lang = prev_lang
		self.assertEqual(inact_hist['session']['close_reason_label'], 'Finalizada por inactividad')

	def test_my_conversations_mine_only_and_spanish_labels(self):
		agent_one = self._create_user()
		agent_two = self._create_user()
		self._grant(agent_one, available=1, capacity=5)
		self._grant(agent_two, available=1, capacity=5)

		opened = self.open(sender='mc-' + frappe.generate_hash(length=6))
		session_name = opened['session']
		contact_name = frappe.db.get_value('Chat Session', session_name, 'contact')
		frappe.db.set_value(
			'Chat Contact',
			contact_name,
			{
				'parameter_values': json.dumps({'name': 'Carlos Ruiz'}),
				'profile_labels': json.dumps({'name': 'Name'}),
			},
		)
		frappe.db.commit()

		# Queued session must not appear in mine=1 and must fail mine_only=1 even for Administrator.
		self.assertFalse(any(r['name'] == session_name for r in inbox.conversations(mine=1, status='')))
		with self.assertRaises(frappe.PermissionError):
			inbox.history(session_name, mine_only=1)

		frappe.set_user(agent_one)
		human.act(session_name, 'Take')
		mine_rows = inbox.conversations(mine=1, status='')
		self.assertTrue(any(r['name'] == session_name and r['status'] == 'Human Active' for r in mine_rows))

		prev_lang = getattr(frappe.local, 'lang', 'en')
		frappe.local.lang = 'es'
		try:
			hist = inbox.history(session_name, mine_only=1)
		finally:
			frappe.local.lang = prev_lang

		profile_labels = [f.get('label') for f in hist['contact'].get('profile_fields', [])]
		self.assertIn('Nombre', profile_labels)
		event_texts = [m.get('content') for m in hist['messages'] if m.get('kind') == 'Event']
		self.assertTrue(any('toma de conversación' in t for t in event_texts))

		# Other user/agent cannot read another agent's session through mine_only=1.
		frappe.set_user(agent_two)
		self.assertFalse(any(r['name'] == session_name for r in inbox.conversations(mine=1, status='')))
		with self.assertRaises(frappe.PermissionError):
			inbox.history(session_name, mine_only=1)

		# Full lifecycle by owner agent: reply, Pause/Resume rejection (agent + manager), close reason labels.
		frappe.set_user(agent_one)
		self.assertFalse(hist['session']['can_pause'])
		self.assertFalse(hist['session']['can_resume'])
		self.assertTrue(hist['session']['can_close'])
		sent = inbox.send(session_name, 'Hola Carlos, ya reviso tu caso.', client_id='mc-test-' + frappe.generate_hash(length=8))
		self.assertTrue(sent.get('dispatch'))

		with self.assertRaises(frappe.ValidationError):
			human.act(session_name, 'Pause')
		with self.assertRaises(frappe.ValidationError):
			human.act(session_name, 'Resume')
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Active')

		frappe.set_user('Administrator')
		with self.assertRaises(frappe.ValidationError):
			human.act(session_name, 'Pause')
		with self.assertRaises(frappe.ValidationError):
			human.act(session_name, 'Resume')
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Human Active')

		frappe.set_user(agent_one)
		human.act(session_name, 'Close')
		remaining = inbox.conversations(mine=1, status='')
		self.assertFalse(any(r['name'] == session_name for r in remaining))

		frappe.local.lang = 'es'
		try:
			closed_hist = inbox.history(session_name, mine_only=1)
		finally:
			frappe.local.lang = prev_lang
		self.assertEqual(closed_hist['session']['status'], 'Closed')
		self.assertEqual(closed_hist['session']['close_reason'], 'Manual')
		self.assertEqual(closed_hist['session']['close_reason_label'], 'Finalizada manualmente')

	def test_historical_human_paused_read_compat_and_close(self):
		agent_user = self._create_user()
		self._grant(agent_user, available=1, capacity=5)
		opened = self.open(sender='hist-p-' + frappe.generate_hash(length=6))
		session_name = opened['session']

		frappe.set_user(agent_user)
		human.act(session_name, 'Take')
		frappe.set_user('Administrator')
		frappe.db.set_value('Chat Session', session_name, 'status', 'Human Paused', update_modified=False)
		frappe.db.commit()

		frappe.set_user(agent_user)
		rows = inbox.conversations(mine=1, status='Human Paused')
		self.assertTrue(any(r['name'] == session_name for r in rows))
		hist = inbox.history(session_name, mine_only=1)
		self.assertEqual(hist['session']['status'], 'Human Paused')
		self.assertFalse(hist['session']['can_reply'])
		self.assertFalse(hist['session']['can_pause'])
		self.assertFalse(hist['session']['can_resume'])
		self.assertTrue(hist['session']['can_close'])
		with self.assertRaises(frappe.ValidationError):
			human.act(session_name, 'Resume')
		human.act(session_name, 'Close')
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'status'), 'Closed')
		self.assertEqual(frappe.db.get_value('Chat Session', session_name, 'close_reason'), 'Manual')


class TestConversationHistory(unittest.TestCase):
	configure = TestManualHumanQueueAndNotifications.configure
	ensure_test_channel = TestManualHumanQueueAndNotifications.ensure_test_channel
	event = TestManualHumanQueueAndNotifications.event
	flow = TestManualHumanQueueAndNotifications.flow
	parameter = TestManualHumanQueueAndNotifications.parameter
	phrase = TestManualHumanQueueAndNotifications.phrase
	state = TestManualHumanQueueAndNotifications.state
	agent = TestManualHumanQueueAndNotifications.agent
	_create_user = TestManualHumanQueueAndNotifications._create_user
	_grant = TestManualHumanQueueAndNotifications._grant

	def setUp(self):
		TestInboxEndToEnd.setUp(self)
		self.extra_grants = []
		self.extra_error_logs = []
		self.other_account = self.prefix + '-other'
		self.other_binding = frappe.get_doc(dict(
			doctype='Communication Channel Account',
			channel='emulator',
			account_id=self.other_account,
			account_name='Synthetic Other History Account',
			enabled=1,
		)).insert(set_name=self.other_account)
		self.accounts.append(self.other_account)
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user('Administrator')
		other_name = getattr(self, 'other_binding', None) and self.other_binding.name
		for grant_name in getattr(self, 'extra_grants', []):
			if frappe.db.exists('Chat Agent', grant_name):
				frappe.delete_doc('Chat Agent', grant_name, ignore_permissions=True)
		frappe.db.commit()
		TestInboxEndToEnd.tearDown(self)
		if other_name and frappe.db.exists('Communication Channel Account', other_name):
			frappe.delete_doc('Communication Channel Account', other_name, ignore_permissions=True)
			frappe.db.commit()

	def _create_contact(self, sender, *, name=None, account=None):
		acc = account or self.prefix
		params = {'name': name} if name else {}
		return frappe.get_doc(dict(
			doctype='Chat Contact',
			channel='emulator',
			account=acc,
			external_sender=sender,
			identity_key=identity_key('emulator', acc, sender),
			registration_status='Complete',
			parameter_values=json.dumps(params),
			profile_labels=json.dumps({'name': 'Name'}) if name else '{}',
		)).insert(ignore_permissions=True)

	def _create_session(
		self,
		contact,
		*,
		status,
		close_reason='',
		human_requested_at=None,
		assigned_agent=None,
		assigned_at=None,
		opened_at=None,
		closed_at=None,
		closed_by=None,
		last_activity=None,
		expires_at=None,
	):
		return frappe.get_doc(dict(
			doctype='Chat Session',
			contact=contact.name,
			channel=contact.channel,
			account=contact.account,
			status=status,
			close_reason=close_reason,
			human_requested_at=human_requested_at,
			assigned_agent=assigned_agent,
			assigned_at=assigned_at,
			opened_at=opened_at,
			closed_at=closed_at,
			closed_by=closed_by,
			last_activity=last_activity or utcnow(),
			expires_at=expires_at,
		)).insert(ignore_permissions=True)

	def _append_msg(self, session, seq, kind, content, *, sent_at=None, agent=None, dispatch=None):
		with _writing():
			msg = frappe.get_doc(dict(
				doctype='Chat Message',
				message_key=identity_key(session.name, str(seq), kind),
				session=session.name,
				contact=session.contact,
				sequence=seq,
				kind=kind,
				content=content,
				agent=agent,
				dispatch=dispatch,
				sent_at=sent_at or utcnow(),
			)).insert(ignore_permissions=True)
		frappe.db.set_value(
			'Chat Session',
			session.name,
			{
				'message_sequence': seq,
				'last_incoming_sequence': seq if kind == 'Incoming' else (session.last_incoming_sequence or 0),
			},
			update_modified=False,
		)
		session.message_sequence = seq
		if kind == 'Incoming':
			session.last_incoming_sequence = seq
		return msg

	def _create_turn(self, session):
		return frappe.get_doc(dict(
			doctype='Chat Turn',
			contact=session.contact,
			session=session.name,
			channel=session.channel,
			account=session.account,
			event_key=identity_key(session.name, 'turn', frappe.generate_hash(length=10)),
			status='Complete',
		)).insert(ignore_permissions=True)

	def _create_dispatch(self, session, *, message_type='Automatic', status='Accepted', delivery_status='', diagnostic_code='', agent=None, turn=None):
		return frappe.get_doc(dict(
			doctype='Bot Dispatch',
			outbound_key=identity_key(session.name, frappe.generate_hash(length=12)),
			contact=session.contact,
			session=session.name,
			turn=turn.name if turn else None,
			channel=session.channel,
			account=session.account,
			message_type=message_type,
			quota_kind='Human' if message_type == 'Human' else 'Standard',
			agent=agent,
			status=status,
			accepted_at=utcnow() if status == 'Accepted' else None,
			delivery_status=delivery_status,
			diagnostic_code=diagnostic_code,
			reserved_at=utcnow(),
		)).insert(ignore_permissions=True)

	def test_listing_filters_search_multi_session_and_pagination(self):
		agent_one = self._create_user()
		agent_two = self._create_user()
		self._grant(agent_one, account=self.prefix, available=1, capacity=5)
		self._grant(agent_two, account=self.prefix, available=1, capacity=5)

		base = utcnow() - timedelta(days=1)
		day_old = utcnow() - timedelta(days=5)

		c_multi = self._create_contact('000' + frappe.generate_hash(length=15), name='Diego Historial')
		c_tech = self._create_contact('000' + frappe.generate_hash(length=15), name='Elena Error')
		c_active = self._create_contact('000' + frappe.generate_hash(length=15), name='Mario Activo')
		c_other = self._create_contact('000' + frappe.generate_hash(length=15), name='Carlos Otra Cuenta', account=self.other_account)

		# Episode 1 for Diego: Automatic Expired (older, tie-break timestamp test with s_tech)
		s_multi_exp = self._create_session(
			c_multi,
			status='Expired',
			last_activity=day_old,
			expires_at=day_old + timedelta(minutes=15),
		)
		self._append_msg(s_multi_exp, 1, 'Incoming', 'Consulta de saldo', sent_at=day_old - timedelta(minutes=2))
		self._append_msg(s_multi_exp, 2, 'Automatic', 'Tu saldo es 0', sent_at=day_old)

		# Episode 2 for Diego: Human Closed Manual by agent_one
		s_multi_closed = self._create_session(
			c_multi,
			status='Closed',
			close_reason='Manual',
			human_requested_at=base - timedelta(minutes=30),
			assigned_agent=agent_one,
			assigned_at=base - timedelta(minutes=25),
			closed_at=base,
			closed_by=agent_one,
			last_activity=base,
		)
		self._append_msg(s_multi_closed, 1, 'Incoming', 'Necesito un agente', sent_at=base - timedelta(minutes=32))
		self._append_msg(s_multi_closed, 2, 'Human', 'Caso resuelto manualmente', sent_at=base - timedelta(minutes=1), agent=agent_one)

		# Episode 3 for Diego: Active (must be excluded by default)
		s_multi_active = self._create_session(c_multi, status='Active', last_activity=utcnow())
		c_multi.active_session = s_multi_active.name
		c_multi.save(ignore_permissions=True)

		# Technical Error automatic session sharing exact day_old last_activity for tie-breaking
		s_tech = self._create_session(
			c_tech,
			status='Closed',
			close_reason='Technical Error',
			closed_at=day_old + timedelta(seconds=10),
			last_activity=day_old,
		)
		self._append_msg(s_tech, 1, 'Incoming', 'Fallo tecnico', sent_at=day_old - timedelta(seconds=5))

		# Inactivity Closed session by agent_two
		s_inact = self._create_session(
			c_tech,
			status='Closed',
			close_reason='Inactivity',
			human_requested_at=base - timedelta(hours=2),
			assigned_agent=agent_two,
			assigned_at=base - timedelta(hours=1, minutes=50),
			closed_at=base - timedelta(minutes=10),
			last_activity=base - timedelta(minutes=10),
		)
		self._append_msg(s_inact, 1, 'Incoming', 'Hola soporte', sent_at=base - timedelta(hours=2))

		# Active human queued and human active must be excluded (distinct contacts to satisfy unique active_key)
		s_queued = self._create_session(c_tech, status='Human Queued', human_requested_at=utcnow(), last_activity=utcnow())
		s_human_act = self._create_session(c_active, status='Human Active', human_requested_at=utcnow(), assigned_agent=agent_one, last_activity=utcnow())

		# Other account closed session
		s_other_acc = self._create_session(
			c_other,
			status='Closed',
			close_reason='Manual',
			human_requested_at=base - timedelta(minutes=50),
			assigned_agent=agent_two,
			assigned_at=base - timedelta(minutes=45),
			closed_at=base - timedelta(minutes=20),
			last_activity=base - timedelta(minutes=20),
		)
		frappe.db.commit()

		# Administrator listing on synthetic accounts
		all_a = inbox.historical_conversations(account=self.prefix, limit=100)
		names_a = [r['name'] for r in all_a]
		self.assertEqual(set(names_a), {s_multi_closed.name, s_inact.name, s_tech.name, s_multi_exp.name})
		self.assertNotIn(s_multi_active.name, names_a)
		self.assertNotIn(s_queued.name, names_a)
		self.assertNotIn(s_human_act.name, names_a)
		self.assertNotIn(s_other_acc.name, names_a)

		# Multiple sessions of the same contact remain independent episodes with exact timings
		by_name = {r['name']: r for r in all_a}
		self.assertEqual(by_name[s_multi_exp.name]['status'], 'Expired')
		self.assertEqual(by_name[s_multi_exp.name]['attention_type'], 'Automatic')
		self.assertIsNone(by_name[s_multi_exp.name]['human_wait_seconds'])
		self.assertIsNone(by_name[s_multi_exp.name]['human_attention_seconds'])
		self.assertEqual(by_name[s_multi_closed.name]['status'], 'Closed')
		self.assertEqual(by_name[s_multi_closed.name]['close_reason'], 'Manual')
		self.assertEqual(by_name[s_multi_closed.name]['attention_type'], 'Human')
		self.assertEqual(by_name[s_multi_closed.name]['human_wait_seconds'], 300)
		self.assertEqual(by_name[s_multi_closed.name]['human_attention_seconds'], 1500)

		# Tie-breaking on equal last_activity (s_tech > s_multi_exp by name desc) and non-overlapping pagination
		page1 = inbox.historical_conversations(account=self.prefix, start=0, limit=2)
		page2 = inbox.historical_conversations(account=self.prefix, start=2, limit=2)
		self.assertEqual(len(page1), 2)
		self.assertEqual(len(page2), 2)
		combined = [r['name'] for r in page1 + page2]
		self.assertEqual(len(combined), len(set(combined)))
		self.assertEqual(combined, [s_multi_closed.name, s_inact.name, s_tech.name, s_multi_exp.name])

		# Filters: status, close_reason, attention_type, agent, mine, date range, search
		self.assertEqual([r['name'] for r in inbox.historical_conversations(account=self.prefix, status='Expired')], [s_multi_exp.name])
		self.assertEqual([r['name'] for r in inbox.historical_conversations(account=self.prefix, close_reason='Technical Error')], [s_tech.name])
		self.assertEqual([r['name'] for r in inbox.historical_conversations(account=self.prefix, close_reason='Inactivity')], [s_inact.name])
		self.assertEqual(
			{r['name'] for r in inbox.historical_conversations(account=self.prefix, attention_type='Automatic')},
			{s_tech.name, s_multi_exp.name},
		)
		self.assertEqual([r['name'] for r in inbox.historical_conversations(account=self.prefix, agent=agent_one)], [s_multi_closed.name])

		# Date range filtering isolates recent vs 5-day-old sessions
		recent_date = base.date().isoformat()
		recent_rows = inbox.historical_conversations(account=self.prefix, date_from=recent_date, date_to=recent_date)
		self.assertEqual({r['name'] for r in recent_rows}, {s_multi_closed.name, s_inact.name})

		with self.assertRaises(frappe.ValidationError):
			inbox.historical_conversations(status='Active')
		with self.assertRaises(frappe.ValidationError):
			inbox.historical_conversations(close_reason='Other')
		with self.assertRaises(frappe.ValidationError):
			inbox.historical_conversations(attention_type='Bot')
		with self.assertRaises(frappe.ValidationError):
			inbox.historical_conversations(date_from='2026-11-15', date_to='2026-11-10')

		# Search by contact name (like & =), sender, contact ID, and session ID
		self.assertEqual(
			{r['name'] for r in inbox.historical_conversations(account=self.prefix, search='Diego Historial')},
			{s_multi_closed.name, s_multi_exp.name},
		)
		self.assertEqual(
			{r['name'] for r in inbox.historical_conversations(account=self.prefix, search='Diego Historial', search_condition='=')},
			{s_multi_closed.name, s_multi_exp.name},
		)
		self.assertEqual(
			{r['name'] for r in inbox.historical_conversations(account=self.prefix, search=c_tech.external_sender)},
			{s_inact.name, s_tech.name},
		)
		self.assertEqual(
			[r['name'] for r in inbox.historical_conversations(account=self.prefix, search=s_tech.name)],
			[s_tech.name],
		)

		meta = inbox.channels_and_accounts()
		self.assertTrue(meta['is_manager'])
		self.assertTrue(any(a['user'] == agent_one for a in meta['agents']))

	def test_messages_chronology_origin_transport_multiline_and_no_fabrication(self):
		agent_one = self._create_user()
		self._grant(agent_one, account=self.prefix, available=1, capacity=5)
		contact = self._create_contact('000' + frappe.generate_hash(length=15), name='Lucia Mensajes')
		t0 = utcnow() - timedelta(minutes=20)
		session = self._create_session(
			contact,
			status='Closed',
			close_reason='Manual',
			human_requested_at=t0 + timedelta(minutes=1),
			assigned_agent=agent_one,
			assigned_at=t0 + timedelta(minutes=2),
			closed_at=t0 + timedelta(minutes=10),
			closed_by=agent_one,
			last_activity=t0 + timedelta(minutes=10),
		)
		turn_bot = self._create_turn(session)
		turn_close = self._create_turn(session)

		d_bot = self._create_dispatch(session, message_type='Automatic', status='Accepted', delivery_status='Read', turn=turn_bot)
		d_human = self._create_dispatch(session, message_type='Human', status='Accepted', delivery_status='Delivered', agent=agent_one)
		d_close = self._create_dispatch(session, message_type='Automatic', status='Rejected', diagnostic_code='provider_rejected', turn=turn_close)

		self._append_msg(session, 1, 'Incoming', 'Hola linea 1\nlinea 2', sent_at=t0)
		self._append_msg(session, 2, 'Event', 'Human request queued.', sent_at=t0 + timedelta(minutes=1))
		self._append_msg(session, 3, 'Automatic', 'Respuesta bot', sent_at=t0 + timedelta(minutes=1, seconds=5), dispatch=d_bot.name)
		self._append_msg(session, 4, 'Human', 'Respuesta agente\ncon salto', sent_at=t0 + timedelta(minutes=3), agent='Administrator', dispatch=d_human.name)
		self._append_msg(session, 5, 'Automatic', 'Atencion finalizada', sent_at=t0 + timedelta(minutes=10), dispatch=d_close.name)

		# Expired automatic session with only incoming message must NOT fabricate completion message
		s_exp = self._create_session(contact, status='Expired', last_activity=t0, expires_at=t0 + timedelta(minutes=15))
		self._append_msg(s_exp, 1, 'Incoming', 'Sin respuesta', sent_at=t0)
		frappe.db.commit()

		prev_lang = getattr(frappe.local, 'lang', 'en')
		frappe.local.lang = 'es'
		try:
			page_latest = inbox.history(session.name, limit=3, historical=1)
			self.assertTrue(page_latest['has_more'])
			self.assertEqual([m['sequence'] for m in page_latest['messages']], [3, 4, 5])

			page_earlier = inbox.history(session.name, before=page_latest['messages'][0]['sequence'], limit=3, historical=1)
			self.assertFalse(page_earlier['has_more'])
			self.assertEqual([m['sequence'] for m in page_earlier['messages']], [1, 2])
		finally:
			frappe.local.lang = prev_lang

		all_msgs = page_earlier['messages'] + page_latest['messages']
		self.assertEqual(all_msgs[0]['content'], 'Hola linea 1\nlinea 2')
		self.assertEqual(all_msgs[0]['origin_label'], 'Abonado')
		self.assertEqual(all_msgs[1]['origin_label'], 'Sistema')
		self.assertEqual(all_msgs[2]['origin_label'], 'Bot')
		self.assertEqual(all_msgs[2]['transport']['delivery_status'], 'Read')
		self.assertEqual(all_msgs[3]['origin_label'], 'Agente')
		self.assertNotIn('Administrator', all_msgs[3]['origin_label'])
		self.assertEqual(all_msgs[3]['content'], 'Respuesta agente\ncon salto')
		self.assertEqual(all_msgs[3]['transport']['delivery_status'], 'Delivered')
		self.assertEqual(all_msgs[4]['transport']['status'], 'Rejected')
		self.assertEqual(all_msgs[4]['transport']['diagnostic_code'], 'provider_rejected')

		exp_hist = inbox.history(s_exp.name, historical=1)
		self.assertEqual(len(exp_hist['messages']), 1)
		self.assertEqual(exp_hist['messages'][0]['content'], 'Sin respuesta')
		self.assertEqual(exp_hist['session']['status'], 'Expired')
		self.assertEqual(exp_hist['session']['close_reason'], '')
		self.assertFalse(exp_hist['session']['can_reply'])
		self.assertFalse(exp_hist['session']['can_take'])
		self.assertFalse(exp_hist['session']['can_close'])

	def test_permissions_isolation_and_strict_readonly(self):
		agent_one = self._create_user()
		agent_two = self._create_user()
		no_grant_agent = self._create_user()
		self._grant(agent_one, account=self.prefix, available=1, capacity=5)
		self._grant(agent_two, account=self.prefix, available=1, capacity=5)

		t0 = utcnow() - timedelta(minutes=30)
		c_a = self._create_contact('000' + frappe.generate_hash(length=15), name='Contacto Cuenta A')
		c_b = self._create_contact('000' + frappe.generate_hash(length=15), name='Contacto Cuenta B', account=self.other_account)

		s_one = self._create_session(
			c_a,
			status='Closed',
			close_reason='Manual',
			human_requested_at=t0,
			assigned_agent=agent_one,
			assigned_at=t0 + timedelta(minutes=1),
			closed_at=t0 + timedelta(minutes=5),
			last_activity=t0 + timedelta(minutes=5),
		)
		self._append_msg(s_one, 1, 'Incoming', 'Mensaje de mi sesion', sent_at=t0)

		s_two = self._create_session(
			c_a,
			status='Closed',
			close_reason='Inactivity',
			human_requested_at=t0,
			assigned_agent=agent_two,
			assigned_at=t0 + timedelta(minutes=1),
			closed_at=t0 + timedelta(minutes=5),
			last_activity=t0 + timedelta(minutes=5),
		)
		s_auto_exp = self._create_session(c_a, status='Expired', last_activity=t0, expires_at=t0 + timedelta(minutes=15))
		s_unassigned_closed = self._create_session(c_a, status='Closed', close_reason='Manual', human_requested_at=t0, closed_at=t0, last_activity=t0)
		s_other_acc = self._create_session(
			c_b,
			status='Closed',
			close_reason='Manual',
			human_requested_at=t0,
			assigned_agent=agent_one,
			assigned_at=t0 + timedelta(minutes=1),
			closed_at=t0 + timedelta(minutes=5),
			last_activity=t0 + timedelta(minutes=5),
		)
		s_active = self._create_session(c_a, status='Active', last_activity=utcnow())
		frappe.db.commit()

		# Snapshot before read operations to verify zero side effects
		snapshot_before = frappe.db.get_value(
			'Chat Session',
			s_one.name,
			['status', 'message_sequence', 'last_incoming_sequence', 'read_sequence', 'last_activity', 'modified', 'assigned_agent'],
			as_dict=True,
		)
		dispatch_count_before = frappe.db.count('Bot Dispatch', {'session': s_one.name})
		notif_count_before = frappe.db.count('Notification Log', {'document_name': s_one.name})

		# Guest must be rejected everywhere
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):
			inbox.historical_conversations()
		with self.assertRaises(frappe.PermissionError):
			inbox.history(s_one.name, historical=1)
		with self.assertRaises(frappe.PermissionError):
			inbox.channels_and_accounts()

		# Agent without any Chat Agent grant sees nothing and cannot bypass via parameters
		frappe.set_user(no_grant_agent)
		self.assertEqual(inbox.historical_conversations(), [])
		no_grant_meta = inbox.channels_and_accounts()
		self.assertEqual(no_grant_meta['accounts'], [])
		self.assertEqual(no_grant_meta['agents'], [])
		with self.assertRaises(frappe.PermissionError):
			inbox.historical_conversations(account=self.prefix)
		with self.assertRaises(frappe.PermissionError):
			inbox.history(s_one.name, historical=1)

		# Agent one sees only s_one, never s_two, s_auto_exp, s_unassigned_closed, or s_other_acc
		frappe.set_user(agent_one)
		rows_one = inbox.historical_conversations(account=self.prefix, limit=100)
		self.assertEqual([r['name'] for r in rows_one], [s_one.name])
		self.assertEqual(inbox.historical_conversations(mine=1, account=self.prefix), rows_one)
		self.assertEqual(inbox.historical_conversations(attention_type='Automatic'), [])

		# Cannot filter by unauthorized account or other agent
		with self.assertRaises(frappe.PermissionError):
			inbox.historical_conversations(account=self.other_account)
		with self.assertRaises(frappe.PermissionError):
			inbox.historical_conversations(agent=agent_two)

		# Direct session access: own historical allowed, others/automatic/unassigned/other-account/nonexistent/active rejected identically
		own_hist = inbox.history(s_one.name, historical=1)
		self.assertEqual(own_hist['session']['name'], s_one.name)
		self.assertFalse(own_hist['can_read'])
		for forbidden_id in (s_two.name, s_auto_exp.name, s_unassigned_closed.name, s_other_acc.name, s_active.name, 'SES-999999999'):
			with self.assertRaises(frappe.PermissionError):
				inbox.history(forbidden_id, historical=1)

		# Even Administrator cannot open an active session through historical=1
		frappe.set_user('Administrator')
		with self.assertRaises(frappe.PermissionError):
			inbox.history(s_active.name, historical=1)

		# Verify strict read-only invariant after all queries
		snapshot_after = frappe.db.get_value(
			'Chat Session',
			s_one.name,
			['status', 'message_sequence', 'last_incoming_sequence', 'read_sequence', 'last_activity', 'modified', 'assigned_agent'],
			as_dict=True,
		)
		self.assertEqual(snapshot_before, snapshot_after)
		self.assertEqual(frappe.db.count('Bot Dispatch', {'session': s_one.name}), dispatch_count_before)
		self.assertEqual(frappe.db.count('Notification Log', {'document_name': s_one.name}), notif_count_before)


class TestConversationSupervision(unittest.TestCase):
	setUp = TestConversationHistory.setUp
	tearDown = TestConversationHistory.tearDown
	configure = TestConversationHistory.configure
	ensure_test_channel = TestConversationHistory.ensure_test_channel
	event = TestConversationHistory.event
	flow = TestConversationHistory.flow
	parameter = TestConversationHistory.parameter
	phrase = TestConversationHistory.phrase
	state = TestConversationHistory.state
	_grant = TestConversationHistory._grant
	_create_contact = TestConversationHistory._create_contact
	_create_session = TestConversationHistory._create_session
	_append_msg = TestConversationHistory._append_msg
	_create_turn = TestConversationHistory._create_turn
	_create_dispatch = TestConversationHistory._create_dispatch

	def _create_role_user(self, roles, *, first_name='Role User', enabled=1):
		email = f'{self.prefix}-{len(self.users)}@example.invalid'
		with patch('frappe.enqueue'):
			user = frappe.get_doc(dict(
				doctype='User',
				email=email,
				first_name=first_name,
				enabled=enabled,
				send_welcome_email=0,
				user_type='System User',
				roles=[dict(role=r) for r in roles],
			)).insert(ignore_permissions=True)
		self.users.append(user.name)
		frappe.db.commit()
		return user.name

	def test_supervision_summary_and_agent_workload(self):
		from messaging.conversation.doctype.chat_session import supervision

		agent_full = self._create_role_user(('Messaging Agent',), first_name='Full Agent')
		agent_multi = self._create_role_user(('Messaging Agent',), first_name='Multi Agent')
		agent_unavail = self._create_role_user(('Messaging Agent',), first_name='Unavail Agent')
		agent_disabled = self._create_role_user(('Messaging Agent',), first_name='Disabled Agent')

		# agent_full: capacity 1 on self.prefix -> 1/1 (full)
		self._grant(agent_full, account=self.prefix, available=1, capacity=1, enabled=1)
		# agent_multi: full 1/1 on self.prefix, free 0/2 on self.other_account -> counted ONCE in global scope, 0 on self.prefix
		self._grant(agent_multi, account=self.prefix, available=1, capacity=1, enabled=1)
		self._grant(agent_multi, account=self.other_account, available=1, capacity=2, enabled=1)
		# agent_unavail: available=0 on self.prefix -> NOT counted as available
		self._grant(agent_unavail, account=self.prefix, available=0, capacity=3, enabled=1)
		# agent_disabled: enabled=0, available=1 -> NOT counted as available
		self._grant(agent_disabled, account=self.prefix, available=1, capacity=3, enabled=0)

		now = utcnow()
		c1 = self._create_contact('000' + frappe.generate_hash(length=15), name='Ana Activa')
		c2 = self._create_contact('000' + frappe.generate_hash(length=15), name='Luis Activo')
		c3 = self._create_contact('000' + frappe.generate_hash(length=15), name='Sofia Cola 1')
		c4 = self._create_contact('000' + frappe.generate_hash(length=15), name='Pedro Cola 2')
		c5 = self._create_contact('000' + frappe.generate_hash(length=15), name='Bot Auto')
		c6 = self._create_contact('000' + frappe.generate_hash(length=15), name='Cerrada Hist')

		# 2 Human Active on self.prefix (occupying agent_full and agent_multi on self.prefix)
		self._create_session(c1, status='Human Active', human_requested_at=now - timedelta(minutes=20), assigned_agent=agent_full, assigned_at=now - timedelta(minutes=15), last_activity=now - timedelta(minutes=2))
		self._create_session(c2, status='Human Active', human_requested_at=now - timedelta(minutes=12), assigned_agent=agent_multi, assigned_at=now - timedelta(minutes=10), last_activity=now - timedelta(minutes=1))
		# 2 Human Queued on self.prefix (waits ~60s and ~180s -> avg ~120s)
		self._create_session(c3, status='Human Queued', human_requested_at=now - timedelta(seconds=60), last_activity=now - timedelta(seconds=60))
		self._create_session(c4, status='Human Queued', human_requested_at=now - timedelta(seconds=180), last_activity=now - timedelta(seconds=180))
		# Automatic Active and Terminal Closed must NOT be counted
		self._create_session(c5, status='Active', last_activity=now)
		self._create_session(c6, status='Closed', close_reason='Manual', human_requested_at=now - timedelta(hours=1), assigned_agent=agent_full, closed_at=now - timedelta(minutes=30), last_activity=now - timedelta(minutes=30))
		frappe.db.commit()

		# Scoped to self.prefix: 2 in_attention, 2 waiting, 0 available_agents (agent_full=full, agent_multi=full on prefix, others unavail/disabled)
		sum_prefix = supervision.summary(channel='emulator', account=self.prefix)
		self.assertEqual(sum_prefix['in_attention'], 2)
		self.assertEqual(sum_prefix['waiting'], 2)
		self.assertEqual(sum_prefix['available_agents'], 0)
		self.assertIsNotNone(sum_prefix['avg_wait_seconds'])
		self.assertGreaterEqual(sum_prefix['avg_wait_seconds'], 115)
		self.assertLessEqual(sum_prefix['avg_wait_seconds'], 135)

		# Scoped to self.other_account: 0 in_attention, 0 waiting, avg_wait_seconds=None, 1 available agent (agent_multi)
		sum_other = supervision.summary(channel='emulator', account=self.other_account)
		self.assertEqual(sum_other['in_attention'], 0)
		self.assertEqual(sum_other['waiting'], 0)
		self.assertIsNone(sum_other['avg_wait_seconds'])
		self.assertEqual(sum_other['available_agents'], 1)

		# Verify agent workload preserves per-(user, channel, account) grants without summing incompatible capacities
		workload_multi = supervision.agent_workload(agent=agent_multi)
		self.assertEqual(len(workload_multi), 2)
		by_acc = {row['account']: row for row in workload_multi}
		self.assertEqual((by_acc[self.prefix]['occupied'], by_acc[self.prefix]['capacity'], by_acc[self.prefix]['status_code']), (1, 1, 'full'))
		self.assertEqual((by_acc[self.other_account]['occupied'], by_acc[self.other_account]['capacity'], by_acc[self.other_account]['status_code']), (0, 2, 'available'))

		# Even with another free grant on other_account, distinct available_agents across both accounts for agent_multi is 1
		self.assertEqual(supervision.summary(agent=agent_multi)['available_agents'], 1)

		# Disabled grant is marked disabled and not counted available
		workload_dis = supervision.agent_workload(account=self.prefix, agent=agent_disabled)
		self.assertEqual(len(workload_dis), 1)
		self.assertEqual(workload_dis[0]['status_code'], 'disabled')
		self.assertFalse(workload_dis[0]['can_receive'])

	def test_supervision_list_tabs_filters_search_sort_and_pagination(self):
		from messaging.conversation.doctype.chat_session import supervision

		agent_one = self._create_role_user(('Messaging Agent',), first_name='Carla Uno')
		agent_two = self._create_role_user(('Messaging Agent',), first_name='Bruno Dos')
		self._grant(agent_one, account=self.prefix, available=1, capacity=5)
		self._grant(agent_two, account=self.prefix, available=0, capacity=5)
		self._grant(agent_one, account=self.other_account, available=1, capacity=5)

		now = utcnow()
		c_a1 = self._create_contact('000' + frappe.generate_hash(length=15), name='Julio Diez')
		c_a2 = self._create_contact('000' + frappe.generate_hash(length=15), name='Elena Centro')
		c_q1 = self._create_contact('000' + frappe.generate_hash(length=15), name='Alicia Acosta')
		c_q2 = self._create_contact('000' + frappe.generate_hash(length=15), name='Roberto Garcia')
		c_other = self._create_contact('000' + frappe.generate_hash(length=15), name='Otro Canal', account=self.other_account)
		c_closed = self._create_contact('000' + frappe.generate_hash(length=15), name='Cerrado')

		s_a1 = self._create_session(c_a1, status='Human Active', human_requested_at=now - timedelta(minutes=30), assigned_agent=agent_one, assigned_at=now - timedelta(minutes=25), last_activity=now - timedelta(minutes=5))
		s_a2 = self._create_session(c_a2, status='Human Active', human_requested_at=now - timedelta(minutes=15), assigned_agent=agent_two, assigned_at=now - timedelta(minutes=10), last_activity=now - timedelta(minutes=1))
		s_q1 = self._create_session(c_q1, status='Human Queued', human_requested_at=now - timedelta(minutes=20), last_activity=now - timedelta(minutes=4))
		s_q2 = self._create_session(c_q2, status='Human Queued', human_requested_at=now - timedelta(minutes=8), last_activity=now - timedelta(minutes=2))
		s_other = self._create_session(c_other, status='Human Active', human_requested_at=now - timedelta(minutes=10), assigned_agent=agent_one, assigned_at=now - timedelta(minutes=9), last_activity=now)
		self._create_session(c_closed, status='Closed', close_reason='Manual', human_requested_at=now - timedelta(hours=1), assigned_agent=agent_one, closed_at=now - timedelta(minutes=30), last_activity=now - timedelta(minutes=30))
		frappe.db.commit()

		# Tabs: active, queued, all (union without duplicates, no closed/other)
		active_res = supervision.conversations(tab='active', account=self.prefix)
		self.assertEqual({r['name'] for r in active_res['items']}, {s_a1.name, s_a2.name})

		queued_res = supervision.conversations(tab='queued', account=self.prefix)
		self.assertEqual({r['name'] for r in queued_res['items']}, {s_q1.name, s_q2.name})

		all_res = supervision.conversations(tab='all', account=self.prefix)
		all_names = [r['name'] for r in all_res['items']]
		self.assertEqual(len(all_names), 4)
		self.assertEqual(set(all_names), {s_a1.name, s_a2.name, s_q1.name, s_q2.name})
		self.assertNotIn(s_other.name, all_names)

		# Ordering & state restrictions
		self.assertEqual(
			[r['name'] for r in supervision.conversations(tab='queued', account=self.prefix, order_by='longest_wait')['items']],
			[s_q1.name, s_q2.name],
		)
		self.assertEqual(
			[r['name'] for r in supervision.conversations(tab='active', account=self.prefix, order_by='oldest_attention')['items']],
			[s_a1.name, s_a2.name],
		)
		with self.assertRaises(frappe.ValidationError):
			supervision.conversations(tab='active', account=self.prefix, order_by='longest_wait')
		with self.assertRaises(frappe.ValidationError):
			supervision.conversations(tab='queued', account=self.prefix, order_by='oldest_attention')

		# Filters: channel, account, agent, availability, status
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, agent=agent_one)['items']], [s_a1.name])
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, availability='available')['items']], [s_a1.name])
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, availability='unavailable')['items']], [s_a2.name])
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.other_account)['items']], [s_other.name])

		# Search by contact name, session ID, assigned agent full_name, and external_sender
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, search='Julio Diez')['items']], [s_a1.name])
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, search=s_q2.name, search_condition='=')['items']], [s_q2.name])
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, search='Bruno Dos')['items']], [s_a2.name])
		self.assertEqual([r['name'] for r in supervision.conversations(tab='all', account=self.prefix, search=c_q1.external_sender)['items']], [s_q1.name])

		# Server pagination: 2 pages of 2 without overlap
		p1 = supervision.conversations(tab='all', account=self.prefix, start=0, limit=2)
		p2 = supervision.conversations(tab='all', account=self.prefix, start=2, limit=2)
		self.assertEqual(p1['total'], 4)
		self.assertTrue(p1['has_more'])
		self.assertFalse(p2['has_more'])
		combined = [r['name'] for r in p1['items'] + p2['items']]
		self.assertEqual(len(combined), 4)
		self.assertEqual(len(set(combined)), 4)

		# Closing an active session removes it from active supervision list
		human.act(s_a2.name, 'Close')
		after_close = supervision.conversations(tab='active', account=self.prefix)
		self.assertEqual([r['name'] for r in after_close['items']], [s_a1.name])

	def test_supervision_readonly_detail_messages_and_zero_mutations(self):
		from messaging.conversation.doctype.chat_session import supervision

		agent_one = self._create_role_user(('Messaging Agent',), first_name='Agente Lectura')
		self._grant(agent_one, account=self.prefix, available=1, capacity=3)
		contact = self._create_contact('000' + frappe.generate_hash(length=15), name='Lucia Supervision')
		t0 = utcnow() - timedelta(minutes=15)
		s_act = self._create_session(
			contact,
			status='Human Active',
			human_requested_at=t0 + timedelta(minutes=1),
			assigned_agent=agent_one,
			assigned_at=t0 + timedelta(minutes=2),
			last_activity=t0 + timedelta(minutes=5),
		)
		turn_bot = self._create_turn(s_act)
		d_bot = self._create_dispatch(s_act, message_type='Automatic', status='Accepted', delivery_status='Read', turn=turn_bot)
		d_human = self._create_dispatch(s_act, message_type='Human', status='Accepted', delivery_status='Delivered', agent=agent_one)
		self._append_msg(s_act, 1, 'Incoming', 'Hola soporte', sent_at=t0)
		self._append_msg(s_act, 2, 'Event', 'Human request queued.', sent_at=t0 + timedelta(minutes=1))
		self._append_msg(s_act, 3, 'Automatic', 'Transfiriendo...', sent_at=t0 + timedelta(minutes=1, seconds=5), dispatch=d_bot.name)
		self._append_msg(s_act, 4, 'Human', 'En que puedo ayudarle?', sent_at=t0 + timedelta(minutes=3), agent='Administrator', dispatch=d_human.name)
		frappe.db.commit()

		snap_before = frappe.db.get_value(
			'Chat Session',
			s_act.name,
			['status', 'message_sequence', 'last_incoming_sequence', 'read_sequence', 'last_activity', 'modified', 'assigned_agent'],
			as_dict=True,
		)
		disp_before = frappe.db.count('Bot Dispatch', {'session': s_act.name})
		msg_before = frappe.db.count('Chat Message', {'session': s_act.name})

		prev_lang = getattr(frappe.local, 'lang', 'en')
		frappe.local.lang = 'es'
		try:
			supervision.overview(channel='emulator', account=self.prefix)
			supervision.conversations(tab='all', account=self.prefix)
			detail = supervision.detail(s_act.name)
		finally:
			frappe.local.lang = prev_lang

		self.assertFalse(detail['can_read'])
		self.assertTrue(detail['session']['can_reassign'])
		self.assertEqual(len(detail['messages']), 4)
		self.assertEqual([m['sequence'] for m in detail['messages']], [1, 2, 3, 4])
		self.assertEqual([m['origin_label'] for m in detail['messages']], ['Abonado', 'Sistema', 'Bot', 'Agente'])
		self.assertNotIn('Administrator', detail['messages'][3]['origin_label'])
		self.assertEqual(detail['messages'][2]['transport']['delivery_status'], 'Read')
		self.assertEqual(detail['messages'][3]['transport']['delivery_status'], 'Delivered')
		self.assertEqual(detail['assigned_agent_info']['user'], agent_one)
		self.assertEqual(detail['assigned_agent_info']['occupied'], 1)
		self.assertEqual(detail['assigned_agent_info']['capacity'], 3)

		snap_after = frappe.db.get_value(
			'Chat Session',
			s_act.name,
			['status', 'message_sequence', 'last_incoming_sequence', 'read_sequence', 'last_activity', 'modified', 'assigned_agent'],
			as_dict=True,
		)
		self.assertEqual(snap_before, snap_after)
		self.assertEqual(frappe.db.count('Bot Dispatch', {'session': s_act.name}), disp_before)
		self.assertEqual(frappe.db.count('Chat Message', {'session': s_act.name}), msg_before)

	def test_supervision_reassignment_validations_and_concurrency(self):
		from messaging.conversation.doctype.chat_session import supervision

		agent_one = self._create_role_user(('Messaging Agent',), first_name='Agente Uno')
		agent_two = self._create_role_user(('Messaging Agent',), first_name='Agente Dos')
		agent_unavail = self._create_role_user(('Messaging Agent',), first_name='Agente NoDisp')
		agent_full = self._create_role_user(('Messaging Agent',), first_name='Agente Lleno')
		agent_dis_grant = self._create_role_user(('Messaging Agent',), first_name='Agente GrantDes')
		agent_other_acc = self._create_role_user(('Messaging Agent',), first_name='Agente OtraCuenta')

		self._grant(agent_one, account=self.prefix, available=1, capacity=2, enabled=1)
		self._grant(agent_two, account=self.prefix, available=1, capacity=2, enabled=1)
		self._grant(agent_unavail, account=self.prefix, available=0, capacity=2, enabled=1)
		self._grant(agent_full, account=self.prefix, available=1, capacity=1, enabled=1)
		self._grant(agent_dis_grant, account=self.prefix, available=1, capacity=2, enabled=0)
		self._grant(agent_other_acc, account=self.other_account, available=1, capacity=2, enabled=1)

		c_main = self._create_contact('000' + frappe.generate_hash(length=15), name='Cliente Reasignar')
		c_full = self._create_contact('000' + frappe.generate_hash(length=15), name='Cliente Ocupando')
		c_queued = self._create_contact('000' + frappe.generate_hash(length=15), name='Cliente En Cola')
		t0 = utcnow() - timedelta(minutes=10)

		s_main = self._create_session(c_main, status='Human Queued', human_requested_at=t0, last_activity=t0)
		self._append_msg(s_main, 1, 'Incoming', 'Necesito ayuda urgente', sent_at=t0)
		# Occupy agent_full (1/1)
		self._create_session(c_full, status='Human Active', human_requested_at=t0, assigned_agent=agent_full, assigned_at=t0, last_activity=t0)
		# Queued session (must not be reassigned via supervision)
		s_queued = self._create_session(c_queued, status='Human Queued', human_requested_at=t0, last_activity=t0)
		frappe.db.commit()

		# agent_one takes s_main via official contract
		frappe.set_user(agent_one)
		human.act(s_main.name, 'Take')
		frappe.set_user('Administrator')
		s_main.reload()
		rev = str(s_main.modified)
		dispatches_before = frappe.db.count('Bot Dispatch', {'session': s_main.name})

		# Candidates exclude current agent, disabled grants, and other accounts
		cand_data = supervision.reassignment_candidates(s_main.name)
		cand_users = {c['user']: c for c in cand_data['candidates']}
		self.assertNotIn(agent_one, cand_users)
		self.assertNotIn(agent_dis_grant, cand_users)
		self.assertNotIn(agent_other_acc, cand_users)
		self.assertTrue(cand_users[agent_two]['eligible'])
		self.assertFalse(cand_users[agent_unavail]['eligible'])
		self.assertFalse(cand_users[agent_full]['eligible'])

		# Rejections
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_one, rev)
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_unavail, rev)
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_full, rev)
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_dis_grant, rev)
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_other_acc, rev)
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_two, '2020-01-01 00:00:00')
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_queued.name, agent_two, str(s_queued.modified))

		# Valid reassignment to agent_two
		res = supervision.reassign(s_main.name, agent_two, rev)
		self.assertEqual(res['assigned_agent'], agent_two)
		self.assertEqual(res['status'], 'Human Active')

		# Stale revision now rejected
		with self.assertRaises(frappe.ValidationError):
			supervision.reassign(s_main.name, agent_one, rev)

		# Verify no duplicate dispatches, preserved history, and My Conversations isolation
		self.assertEqual(frappe.db.count('Bot Dispatch', {'session': s_main.name}), dispatches_before)
		self.assertGreaterEqual(frappe.db.count('Chat Message', {'session': s_main.name, 'kind': 'Event'}), 1)
		frappe.set_user(agent_one)
		self.assertEqual([r['name'] for r in inbox.conversations(mine=1, account=self.prefix)], [])
		frappe.set_user(agent_two)
		self.assertEqual([r['name'] for r in inbox.conversations(mine=1, account=self.prefix)], [s_main.name])

		# Capacity updated on both agents
		frappe.set_user('Administrator')
		w_one = supervision.agent_workload(account=self.prefix, agent=agent_one)[0]
		w_two = supervision.agent_workload(account=self.prefix, agent=agent_two)[0]
		self.assertEqual(w_one['occupied'], 0)
		self.assertEqual(w_two['occupied'], 1)

		# Concurrency race: two supervisors attempt to reassign simultaneously with same expected_modified
		s_main.reload()
		race_rev = str(s_main.modified)
		frappe.db.commit()
		ctx = multiprocessing.get_context('spawn')
		barrier = ctx.Barrier(2)
		results = ctx.Queue()
		sup_two = self._create_role_user(('System Manager',), first_name='Sup Race')
		specs = [
			('Administrator', dict(session_name=s_main.name, new_agent=agent_one, expected_modified=race_rev)),
			(sup_two, dict(session_name=s_main.name, new_agent=agent_one, expected_modified=race_rev)),
		]
		procs = [
			ctx.Process(
				target=concurrent_supervision_reassign,
				args=(frappe.local.site, str(frappe.local.sites_path), u, a, barrier, results),
			)
			for u, a in specs
		]
		for p in procs:
			p.start()
		out = [results.get(timeout=45) for _ in procs]
		for p in procs:
			p.join(timeout=10)
			if p.is_alive():
				p.terminate()
				p.join()
				self.fail('Concurrent supervision reassignment exceeded deadline')
		frappe.db.rollback()
		self.assertEqual(sum(1 for r in out if r['ok']), 1, out)

	def test_supervision_security_access_control(self):
		from messaging.conversation.doctype.chat_session import supervision

		agent_user = self._create_role_user(('Messaging Agent',), first_name='Only Agent')
		self._grant(agent_user, account=self.prefix, available=1, capacity=2)
		sys_mgr = self._create_role_user(('System Manager',), first_name='Supervisor User')

		contact = self._create_contact('000' + frappe.generate_hash(length=15), name='Seguridad Contacto')
		contact_auto = self._create_contact('000' + frappe.generate_hash(length=15), name='Seguridad Auto')
		s_human = self._create_session(contact, status='Human Active', human_requested_at=utcnow(), assigned_agent=agent_user, assigned_at=utcnow(), last_activity=utcnow())
		s_auto = self._create_session(contact_auto, status='Active', last_activity=utcnow())
		frappe.db.commit()

		# System Manager succeeds
		frappe.set_user(sys_mgr)
		self.assertIn('summary', supervision.overview(account=self.prefix))
		self.assertEqual(supervision.detail(s_human.name)['session']['name'], s_human.name)
		# Direct ID enumeration of non-human session or nonexistent ID rejected identically
		for forbidden in (s_auto.name, 'SES-999999999'):
			with self.assertRaises(frappe.PermissionError):
				supervision.detail(forbidden)
			with self.assertRaises(frappe.PermissionError):
				supervision.reassignment_candidates(forbidden)

		# Arbitrary invalid filters rejected cleanly
		for bad_kwargs in (
			dict(tab='invalid'),
			dict(status='Closed'),
			dict(order_by='drop_table'),
			dict(availability='online'),
			dict(search_condition='regex'),
		):
			with self.assertRaises(frappe.ValidationError):
				supervision.conversations(**bad_kwargs)

		# Messaging Agent and Guest denied on every supervision endpoint
		for unauthorized in (agent_user, 'Guest'):
			frappe.set_user(unauthorized)
			for fn, args in (
				(supervision.overview, ()),
				(supervision.summary, ()),
				(supervision.agent_workload, ()),
				(supervision.conversations, ()),
				(supervision.detail, (s_human.name,)),
				(supervision.reassignment_candidates, (s_human.name,)),
				(supervision.reassign, (s_human.name, agent_user, str(s_human.modified))),
			):
				with self.assertRaises(frappe.PermissionError):
					fn(*args)

	def test_supervision_consolidated_users_and_availability_reasons(self):
		from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import (
			PROBE,
			registered_providers,
		)
		from messaging.communication.doctype.communication_provider.communication_provider import (
			_lock_provider_states,
			_write_provider_states,
			catalog_sync,
			get_provider_states,
			set_provider_enabled,
		)
		from messaging.communication.doctype.communication_provider.test_communication_provider import (
			sync_communication_providers,
		)
		from messaging.conversation.doctype.chat_session import supervision
		from messaging.presets.communication_providers.emulr import definition as emulator_definition

		frappe.set_user('Administrator')
		user_mixed = self._create_role_user(('Messaging Agent',), first_name='Maria Mixta')
		user_same_1 = self._create_role_user(('Messaging Agent',), first_name='Carlos Igual')
		user_same_2 = self._create_role_user(('Messaging Agent',), first_name='Carlos Igual')
		user_no_role = self._create_role_user(('Messaging Agent',), first_name='Sin Rol')
		user_disabled = self._create_role_user(('Messaging Agent',), first_name='Usuario Apagado')
		user_only_retired = self._create_role_user(('Messaging Agent',), first_name='Solo Retirada')
		user_acc_disabled = self._create_role_user(('Messaging Agent',), first_name='Cuenta Apagada')

		disabled_account = self.prefix + '-disabled-acc'
		disabled_acc_doc = frappe.get_doc(dict(
			doctype='Communication Channel Account',
			channel='emulator',
			account_id=disabled_account,
			account_name='Disabled Synthetic Account',
			enabled=0,
		)).insert(set_name=disabled_account)
		self.addCleanup(
			lambda: frappe.db.exists('Communication Channel Account', disabled_account)
			and (frappe.delete_doc('Communication Channel Account', disabled_account, ignore_permissions=True), frappe.db.commit())
		)

		# user_mixed: 1 available (self.prefix), 1 full (self.other_account), 1 retired/missing account
		self._grant(user_mixed, account=self.prefix, available=1, capacity=4, enabled=1)
		self._grant(user_mixed, account=self.other_account, available=1, capacity=1, enabled=1)
		self._grant(user_mixed, account=self.prefix + '-missing-acc', available=1, capacity=5, enabled=1)

		# Two distinct users sharing identical full_name ("Carlos Igual")
		self._grant(user_same_1, account=self.prefix, available=1, capacity=3, enabled=1)
		self._grant(user_same_2, account=self.prefix, available=0, capacity=3, enabled=1)

		# Historical grants where user later loses role or becomes disabled
		self._grant(user_no_role, account=self.prefix, available=1, capacity=2, enabled=1)
		self._grant(user_disabled, account=self.prefix, available=1, capacity=2, enabled=1)
		self._grant(user_only_retired, account=self.prefix + '-retired-only', available=1, capacity=5, enabled=1)
		self._grant(user_acc_disabled, account=disabled_account, available=1, capacity=3, enabled=1)
		frappe.db.commit()

		with patch('frappe.enqueue'):
			u_nr = frappe.get_doc('User', user_no_role)
			u_nr.roles = []
			u_nr.save(ignore_permissions=True)
			u_dis = frappe.get_doc('User', user_disabled)
			u_dis.enabled = 0
			u_dis.save(ignore_permissions=True)
		frappe.db.commit()

		now = utcnow()
		# 2 unique active sessions for user_mixed (1 on self.prefix, 1 on self.other_account -> full on other_account)
		c1 = self._create_contact('000' + frappe.generate_hash(length=15), name='Mix 1', account=self.prefix)
		c2 = self._create_contact('000' + frappe.generate_hash(length=15), name='Mix 2', account=self.other_account)
		c_other_agent = self._create_contact('000' + frappe.generate_hash(length=15), name='Other Agent Sess', account=self.prefix)
		c_queued = self._create_contact('000' + frappe.generate_hash(length=15), name='Queued Unassigned', account=self.prefix)
		s_mix_1 = self._create_session(c1, status='Human Active', human_requested_at=now - timedelta(minutes=8), assigned_agent=user_mixed, assigned_at=now - timedelta(minutes=5), last_activity=now)
		s_mix_2 = self._create_session(c2, status='Human Active', human_requested_at=now - timedelta(minutes=6), assigned_agent=user_mixed, assigned_at=now - timedelta(minutes=4), last_activity=now)
		self._create_session(c_other_agent, status='Human Active', human_requested_at=now - timedelta(minutes=7), assigned_agent=user_same_1, assigned_at=now - timedelta(minutes=3), last_activity=now)
		self._create_session(c_queued, status='Human Queued', human_requested_at=now - timedelta(minutes=5), last_activity=now)
		frappe.db.commit()

		# Agent filter uses real User ID, excludes other agents and unassigned Human Queued sessions
		mix_convs = supervision.conversations(tab='all', agent=user_mixed)
		self.assertEqual({r['name'] for r in mix_convs['items']}, {s_mix_1.name, s_mix_2.name})
		mix_prefix_convs = supervision.conversations(tab='all', channel='emulator', account=self.prefix, agent=user_mixed)
		self.assertEqual([r['name'] for r in mix_prefix_convs['items']], [s_mix_1.name])
		empty_agent_convs = supervision.conversations(tab='all', channel='emulator', account=self.prefix, agent=user_same_2)
		self.assertEqual(empty_agent_convs['items'], [])
		self.assertEqual(empty_agent_convs['total'], 0)

		# Overview across all accounts in prefix/other/etc. for our synthetic users
		ov_prefix = supervision.overview(channel='emulator', account=self.prefix)
		prefix_users = {u['user']: u for u in ov_prefix['agent_users']}
		# Only users with grants on self.prefix appear (user_only_retired and user_acc_disabled excluded)
		self.assertIn(user_mixed, prefix_users)
		self.assertIn(user_same_1, prefix_users)
		self.assertIn(user_same_2, prefix_users)
		self.assertIn(user_no_role, prefix_users)
		self.assertIn(user_disabled, prefix_users)
		self.assertNotIn(user_only_retired, prefix_users)
		self.assertNotIn(user_acc_disabled, prefix_users)

		# Distinct users with identical display name are NOT merged
		self.assertEqual(prefix_users[user_same_1]['full_name'], prefix_users[user_same_2]['full_name'])
		self.assertEqual(prefix_users[user_same_1]['status_code'], 'available')
		self.assertEqual(prefix_users[user_same_2]['status_code'], 'unavailable')

		# Role-removed and disabled users show exact reason_code
		self.assertEqual(prefix_users[user_no_role]['status_code'], 'not_enabled')
		self.assertEqual(prefix_users[user_no_role]['reason_code'], 'missing_role')
		self.assertEqual(prefix_users[user_disabled]['status_code'], 'not_enabled')
		self.assertEqual(prefix_users[user_disabled]['reason_code'], 'user_disabled')

		# KPI available_agents equals exact count of consolidated user cards with can_receive
		self.assertEqual(
			ov_prefix['summary']['available_agents'],
			sum(1 for u in ov_prefix['agent_users'] if u['can_receive']),
		)
		# Only user_mixed and user_same_1 are available on self.prefix (plus Administrator if Administrator has a grant on self.prefix -> none)
		self.assertGreaterEqual(ov_prefix['summary']['available_agents'], 2)

		# Global overview (no account filter): user_mixed appears ONCE with 3 grants, 2 active sessions, 2 operational, 1 available
		ov_all = supervision.overview()
		all_users = {u['user']: u for u in ov_all['agent_users']}
		self.assertEqual(len(ov_all['agent_users']), len(all_users))
		mixed = all_users[user_mixed]
		self.assertEqual(mixed['account_count'], 3)
		self.assertEqual(mixed['operational_account_count'], 2)
		self.assertEqual(mixed['available_account_count'], 1)
		self.assertEqual(mixed['full_account_count'], 1)
		self.assertEqual(mixed['non_operational_account_count'], 1)
		self.assertEqual(mixed['active_conversations'], 2)
		self.assertEqual(mixed['status_code'], 'available')
		self.assertTrue(mixed['can_receive'])
		grants_by_acc = {g['account']: g for g in mixed['grants']}
		self.assertEqual(grants_by_acc[self.prefix]['reason_code'], 'available')
		self.assertEqual((grants_by_acc[self.prefix]['occupied'], grants_by_acc[self.prefix]['capacity']), (1, 4))
		self.assertEqual(grants_by_acc[self.other_account]['reason_code'], 'full')
		self.assertEqual((grants_by_acc[self.other_account]['occupied'], grants_by_acc[self.other_account]['capacity']), (1, 1))
		self.assertEqual(grants_by_acc[self.prefix + '-missing-acc']['reason_code'], 'account_missing')
		self.assertFalse(grants_by_acc[self.prefix + '-missing-acc']['account_exists'])

		# KPI counts user_mixed ONCE across multiple accounts and matches cards
		self.assertEqual(
			ov_all['summary']['available_agents'],
			sum(1 for u in ov_all['agent_users'] if u['can_receive']),
		)
		# Filtering specifically to user_mixed on self.other_account shows full (0 available)
		sum_mixed_other = supervision.summary(channel='emulator', account=self.other_account, agent=user_mixed)
		self.assertEqual(sum_mixed_other['available_agents'], 0)
		mixed_other = supervision.overview(channel='emulator', account=self.other_account)['agent_users']
		mixed_other_map = {u['user']: u for u in mixed_other}
		self.assertEqual(mixed_other_map[user_mixed]['status_code'], 'full')
		self.assertEqual(mixed_other_map[user_mixed]['active_conversations'], 1)

		# User with only retired or disabled account has status no_operational_accounts
		self.assertEqual(all_users[user_only_retired]['status_code'], 'no_operational_accounts')
		self.assertEqual(all_users[user_only_retired]['grants'][0]['reason_code'], 'account_missing')
		self.assertEqual(all_users[user_acc_disabled]['status_code'], 'no_operational_accounts')
		self.assertEqual(all_users[user_acc_disabled]['grants'][0]['reason_code'], 'account_disabled')

		# Channel disabled test using an isolated synthetic provider + channel
		prev_states = get_provider_states()
		ch_name = self.prefix + '-dis-ch'
		acc_name = self.prefix + '-dis-ch-acc'
		try:
			with registered_providers([emulator_definition(), PROBE]):
				sync_communication_providers()
				set_provider_enabled('CTRPRO', True)
				ch_doc = frappe.get_doc(dict(
					doctype='Communication Channel',
					provider='CTRPRO',
					channel_name=ch_name,
					enabled=0,
				)).insert(set_name=ch_name, ignore_permissions=True)
				acc_doc = frappe.get_doc(dict(
					doctype='Communication Channel Account',
					channel=ch_doc.name,
					account_id=acc_name,
					account_name='Disabled Channel Acc',
					enabled=1,
				)).insert(set_name=acc_name, ignore_permissions=True)
				self._grant(user_same_1, channel=ch_doc.name, account=acc_doc.name, available=1, capacity=2, enabled=1)
				frappe.db.commit()

				ov_ch = supervision.overview(channel=ch_doc.name)
				self.assertEqual(ov_ch['summary']['available_agents'], 0)
				self.assertEqual(len(ov_ch['agent_users']), 1)
				u_ch = ov_ch['agent_users'][0]
				self.assertEqual(u_ch['status_code'], 'no_operational_accounts')
				self.assertEqual(u_ch['grants'][0]['reason_code'], 'channel_disabled')
		finally:
			frappe.db.delete('Chat Agent', {'channel': ch_name})
			if frappe.db.exists('Communication Channel Account', acc_name):
				frappe.delete_doc('Communication Channel Account', acc_name, ignore_permissions=True)
			if frappe.db.exists('Communication Channel', ch_name):
				frappe.delete_doc('Communication Channel', ch_name, ignore_permissions=True)
			_lock_provider_states()
			_write_provider_states(prev_states)
			with catalog_sync():
				if frappe.db.exists('Communication Provider', 'CTRPRO') and not frappe.db.exists('Communication Channel', {'provider': 'CTRPRO'}):
					frappe.delete_doc('Communication Provider', 'CTRPRO')
			frappe.db.commit()

	def test_supervision_administrator_implicit_role_read_only(self):
		from messaging.conversation.doctype.chat_session import supervision

		frappe.set_user('Administrator')
		# Administrator resolves Messaging Agent via frappe.get_roles without requiring a Has Role row.
		self.assertIn('Messaging Agent', frappe.get_roles('Administrator'))
		ov = supervision.overview()
		self.assertEqual(
			ov['summary']['available_agents'],
			sum(1 for u in ov['agent_users'] if u['can_receive']),
		)
		admin_cards = [u for u in ov['agent_users'] if u['user'] == 'Administrator']
		if admin_cards:
			self.assertEqual(len(admin_cards), 1)
			admin_card = admin_cards[0]
			self.assertTrue(admin_card['has_role'])
			for grant in admin_card['grants']:
				if grant['account_exists'] and grant['channel_exists'] and grant['channel_enabled'] and grant['account_enabled'] and grant['grant_enabled']:
					self.assertTrue(grant['effective_enabled'])
				elif not grant['account_exists']:
					self.assertEqual(grant['reason_code'], 'account_missing')
					self.assertFalse(grant['can_receive'])
