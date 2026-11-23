"""Durable channel inbox and native RQ coordination owned by Chat Turn.

Redis is a wake-up mechanism. MariaDB receipt order, connection locks and the
engine's invocation receipts remain authoritative, including after worker death.
"""
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar

import frappe

from messaging.common.locking import contact_lock
from messaging.common.primitives import identity_key, utcnow
from messaging.security.execution_context import automatic_job
from messaging.conversation.doctype.chat_contact.chat_contact import (
	JOB_TIMEOUT, _draining, _enqueue, get_contact, require_operator, resume_pending_inputs, schedule_contact,
)
from messaging.conversation.doctype.chat_turn.chat_turn import capture_pending

METHOD = __name__ + '.drain_contact'
TERMINAL = ('Complete', 'Ignored', 'Human Paused', 'Quota Blocked')


def accept_message(request):
	"""Persist one authenticated receipt and sole content copy before acknowledging it."""
	from messaging.conversation.doctype.chat_turn.chat_turn import validate_request, turn_result
	from messaging.conversation.doctype.chat_message.chat_message import _writing
	require_operator()
	request = validate_request(request)
	key = identity_key(request['channel'], request['account'], request['sender'])
	# This short lock never waits on business execution. It also serializes first contact creation.
	with contact_lock(identity_key(key, 'receipt')):
		name = frappe.db.get_value('Chat Contact', {'identity_key': key}, 'name')
		contact = frappe.get_doc('Chat Contact', name) if name else get_contact(
			request['channel'], request['account'], request['sender'])
		event_key = identity_key(key, request['event_id'])
		existing = frappe.db.get_value('Chat Turn', {'event_key': event_key}, 'name')
		if existing:
			turn = frappe.get_doc('Chat Turn', existing)
		else:
			last = frappe.db.sql('SELECT COALESCE(MAX(receipt_order),0) FROM `tabChat Turn` WHERE contact=%s', (contact.name,))[0][0]
			turn = frappe.get_doc(dict(doctype='Chat Turn', event_key=event_key, contact=contact.name,
				status='Pending', inbox_state='Pending', receipt_order=last + 1, received_at=utcnow(),
				event_at=request['event_at'], executions='[]', decisions='[]',
				request_context=json.dumps(dict(event_id=request['event_id'], interpret_natural=True,
					request={k: v for k, v in request.items() if k not in
						('channel', 'account', 'sender', 'event_id', 'event_at', 'text')})))).insert(ignore_permissions=True)
			with _writing():
				frappe.get_doc(dict(doctype='Chat Message', message_key=event_key, contact=contact.name,
					kind='Incoming', content=request.get('text', ''), turn=turn.name, sent_at=turn.received_at,
					event_at=request['event_at'], external_id=request.get('external_reference'),
					source_type=request.get('source_type'), reply_reference=request.get('reply_reference'),
					selection_id=request.get('selection_id'))).insert(ignore_permissions=True)
		schedule_contact(contact.name)
		frappe.publish_realtime('messaging_conversation_update',
			dict(channel=request['channel'], account=request['account']), after_commit=True)
		# Receipt boundaries own their transaction, like the existing channel boundaries.
		frappe.db.commit()
		return turn_result(turn)


@contextmanager
def _fenced_connection(key):
	"""Fail closed if the database connection or its non-expiring contact lock is lost."""
	database = frappe.db
	connection = database._conn
	auto_reconnect = connection.auto_reconnect
	connection.auto_reconnect = False
	original = database.sql
	lock_name = 'messaging:' + key[:54]
	def sql(*args, **kwargs):
		if database._conn is not connection:
			raise RuntimeError('Conversation database connection changed')
		if original('SELECT IS_USED_LOCK(%s)=CONNECTION_ID()', (lock_name,))[0][0] != 1:
			raise RuntimeError('Conversation exclusivity was lost')
		return original(*args, **kwargs)
	database.sql = sql
	try:
		yield
	finally:
		database.sql = original
		connection.auto_reconnect = auto_reconnect


@contextmanager
def _worker_transaction():
	"""Avoid snapshot conflicts with simultaneous native naming-series allocations.

	Only this worker connection uses READ COMMITTED; document locks, atomic Process
	savepoints and the non-expiring conversation lock retain their own semantics.
	"""
	frappe.db.rollback()
	previous = frappe.db.sql('SELECT @@tx_isolation')[0][0]
	frappe.db.sql('SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED')
	try:
		yield
	finally:
		frappe.db.rollback()
		# Values come exclusively from the database, never from job arguments.
		levels = {'READ-UNCOMMITTED': 'READ UNCOMMITTED', 'READ-COMMITTED': 'READ COMMITTED',
			'REPEATABLE-READ': 'REPEATABLE READ', 'SERIALIZABLE': 'SERIALIZABLE'}
		frappe.db.sql('SET SESSION TRANSACTION ISOLATION LEVEL ' + levels[previous])


def _recover_turn(turn):
	"""A lost claim is replayable only before any durable reservation/invocation."""
	if turn.status in TERMINAL:
		turn.inbox_state = 'Done'
	elif turn.dispatch or json.loads(turn.executions or '[]'):
		turn.inbox_state, turn.inbox_diagnostic = 'Blocked', 'execution_reconciliation_required'
		if turn.session:
			session = frappe.get_doc('Chat Session', turn.session)
			from messaging.conversation.doctype.chat_session.human import HUMAN_STATES
			if session.status not in (*HUMAN_STATES, 'Closed', 'Expired'):
				session.status = 'Recovery Required'
				session.save(ignore_permissions=True)
		turn.status = 'Recovery Required'
	else:
		turn.inbox_state, turn.status = 'Pending', 'Pending'
	turn.save(ignore_permissions=True)
	frappe.db.commit()


def _deliver_prepared(contact):
	"""Recover output preparation independently; Sending/Unknown is never resent."""
	from messaging.chatbot.doctype.bot_dispatch.delivery import deliver
	from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import prepare
	from messaging.conversation.doctype.chat_message.chat_message import dispatch_content
	active = frappe.db.get_value('Chat Contact', contact, 'active_session')
	if active:
		session = frappe.get_doc('Chat Session', active)
		session.settle_technical_failure()
		failure = session.read_state('dialogue_state').get('technical_failure')
		if failure and session.status == 'Recovery Required':
			owner = frappe.get_doc('Chat Turn', failure['turn'])
			if owner.dispatch:
				dispatch = frappe.get_doc('Bot Dispatch', owner.dispatch)
				if dispatch.status == 'Reserved':
					from messaging.chatbot.doctype.bot_flow.bot_flow import get_flow_effective_internal_error_message
					text = dispatch_content(dispatch) if failure.get('notice_stored') else '\n\n'.join(filter(None,
						[dispatch_content(dispatch), get_flow_effective_internal_error_message(session.active_flow)]))
					prepare(dispatch, text, functional=bool(dispatch.functional_result))
				if dispatch.status == 'Held':
					dispatch.status, dispatch.resolved_at = 'Rejected', utcnow()
					dispatch.diagnostic_code = 'technical_response_exceeds_limit'
					dispatch.save(ignore_permissions=True)
					session.settle_technical_failure()
		frappe.db.commit()
	for row in frappe.get_all('Bot Dispatch', filters=dict(contact=contact, message_type='Automatic',
		status=['in', ['Prepared', 'Sending', 'Unknown', 'Held']]), fields=['name', 'status'], order_by='creation, name'):
		if row.status != 'Prepared':
			return False
		deliver(row.name)
		if frappe.db.get_value('Bot Dispatch', row.name, 'status') not in ('Accepted', 'Rejected'):
			return False
	return True


@automatic_job
def drain_contact(contact_name):
	"""Bounded work on one conversation, independent of worker threading model."""
	from messaging.conversation.doctype.chat_turn.chat_turn import pending_input_request, process_event
	require_operator()
	key = frappe.db.get_value('Chat Contact', contact_name, 'identity_key')
	if not key:
		return
	with _worker_transaction(), contact_lock(key, timeout=0) as acquired:
		if not acquired:
			return dict(busy=True)
		with _fenced_connection(key):
			token = _draining.set(True)
			try:
				started = time.monotonic()
				for _index in range(50):
					# Each iteration refreshes snapshots; arrivals commit independently of this lock.
					frappe.db.commit()
					if not _deliver_prepared(contact_name):
						return
					active = frappe.db.get_value('Chat Contact', contact_name, 'active_session')
					if active:
						session = frappe.get_doc('Chat Session', active)
						state = session.read_state('dialogue_state')
						if session.status == 'Recovery Required' and state.get('execution_reconciled'):
							state.pop('execution_reconciled')
							session.write_state('dialogue_state', state)
							session.status = 'Active'
							session.save(ignore_permissions=True)
							frappe.db.commit()
					rows = frappe.get_all('Chat Turn', filters=dict(contact=contact_name,
						inbox_state=['in', ['Pending', 'Running', 'Blocked']]), fields=['name'],
						order_by='receipt_order, name', limit_page_length=1)
					if not rows:
						return
					turn = frappe.get_doc('Chat Turn', rows[0].name)
					if turn.inbox_state != 'Pending':
						_recover_turn(turn)
						if turn.inbox_state == 'Blocked':
							return
						if turn.inbox_state == 'Done':
							continue
					if active and frappe.db.get_value('Chat Session', active, 'status') in ('Executing', 'Recovery Required'):
						return
					from messaging.communication.doctype.communication_channel_account.binding import account_provider, resolve_binding
					account = frappe.db.get_value('Chat Contact', contact_name, 'account')
					try:
						resolve_binding(account_provider(account), account)
					except (frappe.PermissionError, frappe.ValidationError, frappe.DoesNotExistError):
						turn.inbox_diagnostic = 'binding_unavailable'
						turn.save(ignore_permissions=True)
						frappe.db.commit()
						return
					turn.inbox_state, turn.claimed_at = 'Running', utcnow()
					turn.inbox_diagnostic = None
					turn.save(ignore_permissions=True)
					frappe.db.commit()
					request = pending_input_request(turn)
					try:
						process_event(request, interpret_natural=True, _resume_turn=turn.name)
					except Exception:
						frappe.db.rollback()
						turn.reload()
						_recover_turn(turn)
						raise
					turn.reload()
					turn.inbox_state = 'Done' if turn.status in TERMINAL else 'Blocked'
					turn.save(ignore_permissions=True)
					frappe.publish_realtime('messaging_conversation_update',
						dict(channel=frappe.db.get_value('Chat Contact', contact_name, 'channel'), account=account),
						after_commit=True)
					frappe.db.commit()
					if not _deliver_prepared(contact_name):
						return
					turn.reload()
					if turn.status in TERMINAL and turn.inbox_state != 'Done':
						turn.inbox_state = 'Done'
						turn.save(ignore_permissions=True)
						frappe.publish_realtime('messaging_conversation_update',
							dict(channel=frappe.db.get_value('Chat Contact', contact_name, 'channel'), account=account),
							after_commit=True)
						frappe.db.commit()
					if turn.inbox_state == 'Blocked':
						return
					if time.monotonic() - started >= 60:
						break
			finally:
				_draining.reset(token)
	# No permanent worker per contact. A wake-up lost to RQ deduplication is recovered by scheduler.
	schedule_contact(contact_name)
	frappe.db.commit()


@automatic_job
def recover_pending():
	"""Periodic durable scan, including failed enqueue, lost jobs and prepared responses."""
	# The cursor only distributes scan work. Losing it restarts a full traversal;
	# durable receipts and claim safety never depend on Redis metadata.
	cursor_key = 'messaging_inbox_recovery_cursor'
	after = frappe.cache.get_value(cursor_key) or ''
	contacts = frappe.db.sql('''SELECT contact FROM `tabChat Turn`
		WHERE inbox_state IN ('Pending','Running','Blocked') AND contact>%s
		UNION SELECT contact FROM `tabBot Dispatch`
		WHERE message_type='Automatic' AND status='Prepared' AND contact>%s
		ORDER BY contact LIMIT 200''', (after, after), pluck=True)
	for contact in contacts:
		schedule_contact(contact)
	frappe.db.commit()
	frappe.cache.set_value(cursor_key, contacts[-1] if len(contacts) == 200 else '')
