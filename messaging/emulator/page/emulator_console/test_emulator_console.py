"""Unified emulator projections over canonical, isolated account identities."""
import unittest
from unittest.mock import patch

import frappe

# Load the shared Chat Turn fixtures before their emulator specializations.
from messaging.conversation.doctype.chat_turn import test_chat_turn  # noqa: F401
from messaging.emulator.doctype.emulator_contact import test_emulator_contact as fixtures
from messaging.emulator.page.emulator_console import emulator_console as console


class TestEmulatorConsole(unittest.TestCase):
	def setUp(self):
		self.fixture = fixtures.TestEmulatorContact(methodName='runTest')
		self.addCleanup(self.fixture.doCleanups)
		self.fixture.setUp()
		self.addCleanup(self.fixture.tearDown)

	def counts(self):
		return {dt: frappe.db.count(dt) for dt in ('Chat Contact', 'Chat Session', 'Chat Turn', 'Chat Message')}

	def test_empty_catalog_and_opening_are_read_only(self):
		f = self.fixture
		before = self.counts()
		with patch.object(console, 'accounts', return_value=dict(enabled=False, accounts=[])):
			self.assertEqual(console.conversations()['conversations'], [])
			self.assertEqual(console.available_pairs()['accounts'], [])
		self.assertEqual([r for r in console.conversations()['conversations'] if r['account'] in (f.a, f.b) or r['sender_id'] == f.sender], [])
		self.assertEqual([r for r in console.available_pairs()['used'] if r.account in (f.a, f.b) or r.sender_id == f.sender], [])
		self.assertEqual(console.history(f.a, f.sender)['messages'], [])
		self.assertEqual(before, self.counts())

	def test_persisted_receipts_are_separate_and_retries_do_not_duplicate(self):
		f = self.fixture
		first = console.send_message(f.a, f.sender, 'unified-emulator-receipt-a', text='Account A pending')
		console.send_message(f.a, f.sender, 'unified-emulator-receipt-a', text='Account A pending')
		second = console.send_message(f.b, f.sender, 'unified-emulator-receipt-b', text='Account B pending')
		self.assertNotEqual(first['contact'], second['contact'])
		rows = [r for r in console.conversations()['conversations'] if r['sender_id'] == f.sender]
		self.assertEqual([(r['account'], r['preview']) for r in rows], [(f.b, 'Account B pending'), (f.a, 'Account A pending')])
		for account, content in [(f.a, 'Account A pending'), (f.b, 'Account B pending')]:
			history = console.history(account, f.sender)
			self.assertEqual([r.content for r in history['messages']], [content])
			self.assertEqual(history['messages'][0]['processing']['state'], 'Pending')
			self.assertEqual(history['sessions'], [])
		# An older page from another account cannot be used as a history cursor.
		with self.assertRaises(frappe.PermissionError):
			console.history(f.b, f.sender, message_before=console.history(f.a, f.sender)['messages'][0].name)

	def test_available_pairs_keep_global_contacts_and_closed_history(self):
		f = self.fixture
		first = f.message(text='Persisted conversation')
		frappe.db.set_value('Chat Session', first['session'], 'status', 'Closed')
		frappe.db.set_value('Chat Contact', first['contact'], 'active_session', None)
		frappe.db.commit()
		options = console.available_pairs()
		self.assertIn(f.sender, {c.sender_id for c in options['contacts']})
		self.assertIn((f.a, f.sender), {(p.account, p.sender_id) for p in options['used']})
		self.assertNotIn((f.b, f.sender), {(p.account, p.sender_id) for p in options['used']})
		before = self.counts()
		self.assertTrue(console.history(f.a, f.sender)['messages'])
		self.assertEqual(console.history(f.b, f.sender)['messages'], [])
		self.assertEqual(before, self.counts())

	def test_read_scope_and_provider_are_enforced(self):
		f = self.fixture
		console.send_message(f.a, f.sender, 'unified-emulator-scope-a', text='Visible A')
		console.send_message(f.b, f.sender, 'unified-emulator-scope-b', text='Hidden B')
		allowed = [row for row in console.accounts()['accounts'] if row.name == f.a]
		# Reuse Frappe's permission-filtered account catalog for every bulk query.
		original_list = frappe.get_list
		def scoped_list(doctype, *args, **kwargs):
			return allowed if doctype == 'Communication Channel Account' else original_list(doctype, *args, **kwargs)
		with patch.object(frappe, 'get_list', side_effect=scoped_list):
			self.assertEqual({r['account'] for r in console.conversations()['conversations']}, {f.a})
			self.assertEqual({r.account for r in console.available_pairs()['used']}, {f.a})
		with patch('frappe.model.document.Document.check_permission', side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError): console.history(f.b, f.sender)
			with self.assertRaises(frappe.PermissionError): console.send_message(f.b, f.sender, 'unified-emulator-denied', text='Denied')
		frappe.set_user('Guest')
		for method in (console.accounts, console.conversations, console.available_pairs):
			with self.assertRaises(frappe.PermissionError): method()
		frappe.set_user('Administrator')
		original = frappe.db.get_value
		def wrong_provider(doctype, name, fieldname=None, *args, **kwargs):
			if doctype == 'Communication Channel' and fieldname == 'provider': return 'OTHER'
			return original(doctype, name, fieldname, *args, **kwargs)
		with patch.object(frappe.db, 'get_value', side_effect=wrong_provider):
			self.assertEqual(console.accounts()['accounts'], [])
			with self.assertRaises(frappe.PermissionError): console.history(f.a, f.sender)
			with self.assertRaises(frappe.PermissionError): console.send_message(f.a, f.sender, 'unified-emulator-provider', text='Denied')

	def test_worker_progress_and_disabled_contact_preserve_history(self):
		f = self.fixture
		result = console.send_message(f.a, f.sender, 'unified-emulator-progress', text='Persisted before processing')
		from messaging.conversation.doctype.chat_turn.inbox import drain_contact
		drain_contact(result['contact'])
		history = console.history(f.a, f.sender)
		self.assertEqual(history['messages'][0]['processing']['state'], 'Done')
		contact = frappe.get_doc('Emulator Contact', f.contacts[0]); contact.enabled = 0; contact.save(); frappe.db.commit()
		self.assertFalse(console.history(f.a, f.sender)['enabled'])
		self.assertIn(f.sender, {r['sender_id'] for r in console.conversations()['conversations']})
		self.assertNotIn(f.sender, {r.sender_id for r in console.available_pairs()['contacts']})

	def test_notification_activity_uses_handoff_and_exact_account_without_sessions(self):
		f = self.fixture
		from frappe.utils import now_datetime
		from messaging.communication.doctype.notification_queue.notification_queue import internal_write
		# This uncommitted snapshot exercises read projections, not process evidence
		# or transport. No dispatcher or scheduler can see the synthetic log reference.
		queue = frappe.get_doc(dict(doctype='Notification Queue', status='Pending',
			occurrence_key=frappe.generate_hash(length=32), execution_log='Synthetic Page projection only',
			account=f.a, channel='emulator', provider='EMULR', effective_recipient=f.sender,
			delivery_mode='Required', delivery_fingerprint='synthetic', scheduled_at=now_datetime(),
			content_type='Text', prepared_text='Visible notification fixture'))
		with internal_write():
			queue.insert(ignore_permissions=True, ignore_links=True)
		self.assertEqual([r for r in console.conversations()['conversations'] if r['account'] in (f.a, f.b) or r['sender_id'] == f.sender], [])
		self.assertEqual([r for r in console.available_pairs()['used'] if r.account in (f.a, f.b) or r.sender_id == f.sender], [])
		with internal_write():
			queue.status = 'Processing'; queue.handoff_at = now_datetime()
			queue.flags.ignore_links = True; queue.save(ignore_permissions=True)
		rows = [r for r in console.conversations()['conversations'] if r['account'] in (f.a, f.b) or r['sender_id'] == f.sender]
		self.assertEqual([(r['account'], r['sender_id'], r['preview']) for r in rows],
			[(f.a, f.sender, 'Visible notification fixture')])
		self.assertEqual({(r.account, r.sender_id) for r in console.available_pairs()['used'] if r.account in (f.a, f.b) or r.sender_id == f.sender}, {(f.a, f.sender)})
		self.assertEqual(console.history(f.b, f.sender)['notifications'], [])
		self.assertEqual(console.history(f.a, f.sender)['notifications'][0].name, queue.name)
		self.assertEqual(frappe.db.count('Chat Session', {'account': f.a}), 0)

	def test_registered_notification_operations_preserve_destinations_outcomes_and_validation(self):
		from frappe.utils import now_datetime
		from messaging.communication.doctype.communication_provider.communication_provider import provider_definition, operation
		from messaging.communication.doctype.notification_queue.notification_queue import internal_write
		from messaging.communication.doctype.notification_queue.dispatch import envelope
		from messaging.emulator.provider import validate_notification
		import json
		f = self.fixture
		definition = provider_definition('EMULR')
		binding = frappe.get_doc('Communication Channel Account', f.a)
		rule = frappe._dict(parameters=[frappe._dict(parameter_key='notification_contact', value=json.dumps(f.contacts[0]))])
		self.assertEqual(operation(definition, 'notification_destination', rule, binding, 'business-destination'),
			dict(effective_recipient=f.sender, emulator_contact=f.contacts[0]))
		template = frappe._dict(body='Notice {{contact_name}}', header='', footer='',
			template_variables=[frappe._dict(parameter_name='contact_name')])
		self.assertTrue(operation(definition, 'validate_template', template, {'contact_name': 'Synthetic'}))
		template.body = ''
		with self.assertRaises(frappe.ValidationError): operation(definition, 'validate_template', template)
		# An uncommitted transport snapshot is invisible to workers and is rolled back
		# by fixture teardown. It grants no Process or dispatcher approval.
		queue = frappe.get_doc(dict(doctype='Notification Queue', status='Pending',
			occurrence_key=frappe.generate_hash(length=32), execution_log='Synthetic provider operation only',
			account=f.a, account_id=binding.account_id, channel='emulator', provider='EMULR',
			effective_recipient=f.sender, emulator_contact=f.contacts[0], delivery_mode='Required',
			delivery_fingerprint='synthetic', scheduled_at=now_datetime(), handoff_at=now_datetime(),
			content_type='Text', prepared_text='Synthetic provider notification'))
		with internal_write():
			queue.insert(ignore_permissions=True, ignore_links=True)
			queue.status = 'Processing'; queue.flags.ignore_links = True; queue.save(ignore_permissions=True)
		payload = envelope(queue.name)
		with self.assertRaises(frappe.PermissionError): validate_notification(payload)
		self.assertTrue(operation(definition, 'validate_notification', payload))
		with self.assertRaises(frappe.PermissionError):
			operation(definition, 'validate_notification', dict(payload, destination='other'))
		for outcome in ('Accepted', 'Rejected', 'Unknown'):
			next(row for row in binding.parameters if row.parameter_key == 'transport_outcome').value = json.dumps(outcome)
			binding.save()
			result = operation(definition, 'send', payload)
			self.assertEqual(result['outcome'], outcome)
			self.assertEqual(result.get('delivery_status'), 'Sent' if outcome == 'Accepted' else None)
		contact = frappe.get_doc('Emulator Contact', f.contacts[0]); contact.enabled = 0; contact.save()
		self.assertFalse(operation(definition, 'validate_notification', payload))
		with self.assertRaises(frappe.ValidationError):
			operation(definition, 'notification_destination', rule, binding, None)

	def _create_manager(self, tag):
		email = f'console-pref-{tag}-{frappe.generate_hash(length=6)}@example.com'
		doc = frappe.get_doc({
			'doctype': 'User',
			'email': email,
			'first_name': f'Console {tag}',
			'enabled': 1,
			'roles': [{'role': 'System Manager'}],
		}).insert(ignore_permissions=True)
		def cleanup():
			frappe.set_user('Administrator')
			frappe.db.delete('Conversation Preference', {'user': email})
			if frappe.db.exists('User', email):
				frappe.delete_doc('User', email, force=True, ignore_permissions=True)
			frappe.db.commit()
		self.addCleanup(cleanup)
		frappe.db.commit()
		return doc.name

	def test_send_message_returns_persisted_message_and_emits_realtime(self):
		f = self.fixture
		emitted = []
		with patch('frappe.publish_realtime', side_effect=lambda event, message=None, **kw: emitted.append((event, message, kw))):
			res = console.send_message(f.a, f.sender, 'realtime-and-msg-payload', text='Instant payload check')
		self.assertIn('message', res)
		msg = res['message']
		self.assertEqual(msg['kind'], 'Incoming')
		self.assertEqual(msg['content'], 'Instant payload check')
		self.assertEqual(msg['processing']['state'], 'Pending')
		self.assertTrue(msg['turn'])
		self.assertTrue(msg['timeline_at'])
		self.assertTrue(any(ev == 'messaging_conversation_update' and payload == {'channel': 'emulator', 'account': f.a} and kw.get('after_commit') for ev, payload, kw in emitted))

	def test_pin_unpin_ordering_persistence_and_isolation(self):
		f = self.fixture
		self.addCleanup(lambda: (frappe.db.delete('Conversation Preference', {'account': ['in', [f.a, f.b]]}), frappe.db.commit()))
		other = f.contact()
		sender_two = other.sender_id
		sender_three_doc = f.contact()
		sender_three = sender_three_doc.sender_id
		user_a = self._create_manager('pin-a')
		user_b = self._create_manager('pin-b')

		m1 = console.send_message(f.a, f.sender, 'pin-order-msg-0001', text='Older chat 1')['message']['name']
		m2 = console.send_message(f.a, sender_two, 'pin-order-msg-0002', text='Middle chat 2')['message']['name']
		m3 = console.send_message(f.a, sender_three, 'pin-order-msg-0003', text='Newest chat 3')['message']['name']
		console.send_message(f.b, f.sender, 'pin-order-msg-000b', text='Same sender other account')['message']['name']
		base = frappe.utils.get_datetime('2026-11-10 10:00:00')
		from datetime import timedelta
		frappe.db.set_value('Chat Message', m1, 'sent_at', base)
		frappe.db.set_value('Chat Message', m2, 'sent_at', base + timedelta(seconds=10))
		frappe.db.set_value('Chat Message', m3, 'sent_at', base + timedelta(seconds=20))
		frappe.db.commit()

		frappe.set_user(user_a)
		rows = [r for r in console.conversations()['conversations'] if r['account'] == f.a]
		self.assertEqual([r['sender_id'] for r in rows], [sender_three, sender_two, f.sender])

		# Pin oldest first, then pin middle: newest pinned stays on top in deterministic pinned_at order.
		res_pin_1 = console.set_pinned(f.a, f.sender, 1)
		self.assertEqual(res_pin_1['is_pinned'], 1)
		res_pin_2 = console.set_pinned(f.a, sender_two, 1)
		frappe.db.set_value('Conversation Preference', {'user': user_a, 'account': f.a, 'sender': f.sender}, 'pinned_at', base + timedelta(seconds=30))
		frappe.db.set_value('Conversation Preference', {'user': user_a, 'account': f.a, 'sender': sender_two}, 'pinned_at', base + timedelta(seconds=40))
		frappe.db.commit()

		# New activity on unpinned chat must NOT move unpinned chat above pinned chats.
		m4 = console.send_message(f.a, sender_three, 'pin-order-msg-0004', text='Fresh unpinned activity')['message']['name']
		frappe.db.set_value('Chat Message', m4, 'sent_at', base + timedelta(seconds=50))
		frappe.db.commit()

		rows_pinned = [r for r in console.conversations()['conversations'] if r['account'] == f.a]
		self.assertEqual([(r['sender_id'], r['is_pinned']) for r in rows_pinned], [(sender_two, 1), (f.sender, 1), (sender_three, 0)])

		# Account isolation: f.b with same f.sender stays unpinned.
		rows_b = [r for r in console.conversations()['conversations'] if r['account'] == f.b and r['sender_id'] == f.sender]
		self.assertEqual(rows_b[0]['is_pinned'], 0)

		# User isolation: user_b still sees ordinary activity order.
		frappe.set_user(user_b)
		rows_user_b = [r for r in console.conversations()['conversations'] if r['account'] == f.a]
		self.assertEqual([(r['sender_id'], r['is_pinned']) for r in rows_user_b], [(sender_three, 0), (sender_two, 0), (f.sender, 0)])

		# Unpin returns chat cleanly to its ordinary chronological position.
		frappe.set_user(user_a)
		res_unpin = console.set_pinned(f.a, f.sender, 0)
		self.assertEqual(res_unpin['is_pinned'], 0)
		rows_after_unpin = [r for r in console.conversations()['conversations'] if r['account'] == f.a]
		self.assertEqual([r['sender_id'] for r in rows_after_unpin], [sender_two, sender_three, f.sender])
		self.assertEqual(res_pin_2['account'], f.a)

	def test_archive_unarchive_preserves_state_and_receives_new_messages(self):
		f = self.fixture
		f.flow()
		self.addCleanup(lambda: (frappe.db.delete('Conversation Preference', {'account': ['in', [f.a, f.b]]}), frappe.db.commit()))
		sent = f.message(text='Session message before archive')
		session_before = frappe.get_doc('Chat Session', sent['session'])
		frappe.db.commit()

		archived = console.set_archived(f.a, f.sender, 1)
		self.assertEqual(archived['is_archived'], 1)
		session_after = frappe.get_doc('Chat Session', sent['session'])
		self.assertEqual(session_before.status, session_after.status)
		self.assertEqual(frappe.db.get_value('Emulator Contact', f.contacts[0], 'enabled'), 1)

		# New incoming message persists and renders while remaining explicitly archived.
		second = console.send_message(f.a, f.sender, 'archived-new-inbound-01', text='Message while archived')
		row = [r for r in console.conversations()['conversations'] if r['account'] == f.a and r['sender_id'] == f.sender][0]
		self.assertEqual(row['is_archived'], 1)
		self.assertEqual(row['preview'], 'Message while archived')
		hist = console.history(f.a, f.sender)
		self.assertEqual(len(hist['messages']), 3)
		self.assertEqual(hist['preference']['is_archived'], 1)
		self.assertEqual(second['session'], None)

		unarchived = console.set_archived(f.a, f.sender, 0)
		self.assertEqual(unarchived['is_archived'], 0)

	def test_clear_chat_hides_messages_and_notifications_without_deleting_sources(self):
		from datetime import timedelta
		from messaging.common.primitives import utcnow
		from messaging.communication.doctype.notification_queue.notification_queue import internal_write
		from messaging.conversation.doctype.conversation_preference.conversation_preference import utc_to_site_naive

		f = self.fixture
		created_notifs = []
		def cleanup_clear():
			frappe.db.delete('Conversation Preference', {'account': ['in', [f.a, f.b]]})
			if created_notifs:
				frappe.db.delete('Notification Queue', {'name': ['in', created_notifs]})
			frappe.db.commit()
		self.addCleanup(cleanup_clear)
		user_a = self._create_manager('clr-a')
		user_b = self._create_manager('clr-b')

		def make_notif(label, stamp_utc):
			q = frappe.get_doc(dict(
				doctype='Notification Queue', status='Pending', occurrence_key=frappe.generate_hash(length=32),
				execution_log='Synthetic clear test', account=f.a, channel='emulator', provider='EMULR',
				effective_recipient=f.sender, delivery_mode='Required', delivery_fingerprint='synthetic',
				scheduled_at=utc_to_site_naive(stamp_utc), content_type='Text', prepared_text=label,
			))
			with internal_write():
				q.insert(ignore_permissions=True, ignore_links=True)
				q.status = 'Processing'
				q.handoff_at = utc_to_site_naive(stamp_utc)
				q.flags.ignore_links = True
				q.save(ignore_permissions=True)
			created_notifs.append(q.name)
			return q

		t0 = utcnow().replace(microsecond=0) - timedelta(minutes=10)
		f.flow()
		first = f.message(text='Old bot exchange')
		old_msg = frappe.db.get_value('Chat Message', {'turn': first['turn']}, 'name')
		frappe.db.set_value('Chat Message', old_msg, 'sent_at', t0)
		frappe.db.set_value('Chat Message', {'session': first['session'], 'kind': 'Automatic'}, 'sent_at', t0 + timedelta(seconds=5))

		other_acct_msg = console.send_message(f.b, f.sender, 'clear-other-acct', text='Account B stays visible')['message']['name']

		old_notif = make_notif('Old notification', t0 + timedelta(seconds=8))
		frappe.db.commit()

		before_counts = {dt: frappe.db.count(dt) for dt in ('Chat Message', 'Chat Turn', 'Chat Session', 'Bot Dispatch', 'Notification Queue')}

		frappe.set_user(user_a)
		cleared = console.clear_chat(f.a, f.sender)
		self.assertTrue(cleared['cleared_before'])

		# Operational records remain intact.
		after_counts = {dt: frappe.db.count(dt) for dt in before_counts}
		self.assertEqual(before_counts, after_counts)

		# Cleared conversation shows empty history & empty preview for user_a, even under pagination cursors.
		hist_a = console.history(f.a, f.sender)
		self.assertEqual(hist_a['messages'], [])
		self.assertEqual(hist_a['notifications'], [])
		self.assertEqual(console.history(f.a, f.sender, message_before=old_msg)['messages'], [])
		self.assertEqual(console.history(f.a, f.sender, notification_before=old_notif.name)['notifications'], [])

		row_a = [r for r in console.conversations()['conversations'] if r['account'] == f.a and r['sender_id'] == f.sender][0]
		self.assertEqual(row_a['preview'], '')
		self.assertIsNone(row_a['last_activity'])

		# Account B and User B still see full history.
		self.assertEqual([m.name for m in console.history(f.b, f.sender)['messages']], [other_acct_msg])
		frappe.set_user(user_b)
		self.assertTrue(len(console.history(f.a, f.sender)['messages']) >= 2)
		self.assertEqual(len(console.history(f.a, f.sender)['notifications']), 1)

		# New message and notification strictly after cleared_before appear cleanly for user_a.
		frappe.set_user(user_a)
		future_utc = utcnow().replace(microsecond=0) + timedelta(minutes=2)
		new_res = console.send_message(f.a, f.sender, 'after-clear-msg-0001', text='After clear')
		frappe.db.set_value('Chat Message', new_res['message']['name'], 'sent_at', future_utc)
		new_notif = make_notif('New notification', future_utc + timedelta(seconds=5))
		frappe.db.commit()

		hist_after = console.history(f.a, f.sender)
		self.assertEqual([m.name for m in hist_after['messages']], [new_res['message']['name']])
		self.assertEqual([n.name for n in hist_after['notifications']], [new_notif.name])
		self.assertEqual(console.history(f.a, f.sender, message_before=new_res['message']['name'])['messages'], [])
		self.assertEqual(console.history(f.a, f.sender, notification_before=new_notif.name)['notifications'], [])
		row_after = [r for r in console.conversations()['conversations'] if r['account'] == f.a and r['sender_id'] == f.sender][0]
		self.assertEqual(row_after['preview'], 'New notification')

	def test_preference_endpoints_enforce_permissions(self):
		f = self.fixture
		self.addCleanup(lambda: (frappe.set_user('Administrator'), frappe.db.delete('Conversation Preference', {'account': ['in', [f.a, f.b]]}), frappe.db.commit()))
		frappe.set_user('Guest')
		for fn in (lambda: console.set_pinned(f.a, f.sender, 1), lambda: console.set_archived(f.a, f.sender, 1), lambda: console.clear_chat(f.a, f.sender)):
			with self.assertRaises(frappe.PermissionError):
				fn()
		frappe.set_user('Administrator')
		with patch('frappe.model.document.Document.check_permission', side_effect=frappe.PermissionError):
			for fn in (lambda: console.set_pinned(f.a, f.sender, 1), lambda: console.set_archived(f.a, f.sender, 1), lambda: console.clear_chat(f.a, f.sender)):
				with self.assertRaises(frappe.PermissionError):
					fn()

	def test_history_bulk_transport_menu_options_and_client_receipts(self):
		f = self.fixture
		f.flow()
		res1 = f.message(text='First reply check')
		res2 = f.message(text='Second reply check')
		session_name = res1['session']
		auto_msgs = frappe.get_all(
			'Chat Message',
			filters={'session': ['in', [session_name, res2['session']]], 'kind': 'Automatic'},
			fields=['name', 'dispatch'],
			order_by='creation asc',
		)
		self.assertTrue(len(auto_msgs) >= 2)
		# Verify Bot Dispatch starts Accepted and bulk history issues client receipts while attaching transport metadata.
		for m in auto_msgs:
			self.assertTrue(m.dispatch)
		hist = console.history(f.a, f.sender)
		by_name = {m['name']: m for m in hist['messages']}
		for m in auto_msgs:
			self.assertIn(m.name, by_name)
			self.assertEqual(by_name[m.name]['dispatch'], m.dispatch)
			self.assertEqual(by_name[m.name]['transport']['status'], 'Accepted')
			self.assertEqual(by_name[m.name]['transport']['delivery_status'], 'Sent')
			self.assertIn('diagnostic_code', by_name[m.name]['transport'])
			self.assertTrue(by_name[m.name]['client_receipt'])
		# Submitting bulk-issued Delivered receipts marks both dispatches as Delivered without errors.
		for m in auto_msgs:
			console.client_status(f.a, f.sender, 'Bot Dispatch', m.dispatch, by_name[m.name]['client_receipt'], 'Delivered')
		statuses = frappe.get_all(
			'Bot Dispatch',
			filters={'name': ['in', [m.dispatch for m in auto_msgs]]},
			fields=['delivery_status'],
		)
		self.assertTrue(all(s.delivery_status == 'Delivered' for s in statuses))
