"""Native human support policy and transitions under the existing contact connection lock."""
from messaging.chatbot.doctype.bot_settings.localization import text as settings_text

import json
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime
from messaging.security.execution_context import automatic_job

from messaging.common.primitives import utcnow
from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings, get_internal_error_message
from messaging.common.locking import contact_lock
from messaging.conversation.doctype.chat_contact.chat_contact import lock_contact
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_message.chat_message import append_message
from messaging.conversation.doctype.chat_session.notifications import notify_human_request_queued
from messaging.conversation.doctype.chat_session.permissions import check_session, is_manager, require_agent

HUMAN_OPEN = ('Human Queued', 'Human Active', 'Human Paused')
HUMAN_ASSIGNED = ('Human Active', 'Human Paused')
HUMAN_STATES = ('Human Requested', *HUMAN_OPEN)


def capacity(session, user=None):
	"""Verify explicit agent capacity under serialized grant locks.

	All explicit assignment actions acquire these same rows before mutating a session.
	Queued requests do not consume capacity. Paused conversations retain their assignment.
	Automatic agent selection without an explicit user is prohibited.
	"""
	if not user:
		return None
	# Start the capacity snapshot only after the serialized grants have been acquired.
	# The contact connection lock survives this durable checkpoint.
	frappe.db.commit()
	grants = frappe.db.sql('''SELECT name,user,capacity,available FROM `tabChat Agent`
		WHERE channel=%s AND account=%s AND enabled=1 ORDER BY name FOR UPDATE''',
		(session.channel, session.account), as_dict=True)
	occupied = frappe.db.sql('''SELECT name,assigned_agent FROM `tabChat Session`
		WHERE channel=%s AND account=%s AND status IN ('Human Active','Human Paused')
		AND IFNULL(assigned_agent,'')!='' ORDER BY name''', (session.channel,session.account), as_dict=True)
	for grant in grants:
		if grant.user != user:
			continue
		if (grant.available and frappe.db.get_value('User', grant.user, 'enabled')
			and 'Messaging Agent' in frappe.get_roles(grant.user)
			and sum(row.assigned_agent == grant.user and row.name != session.name for row in occupied) < grant.capacity):
			return grant.user
	return None


def event(session, text, *, agent=None):
	return append_message(session, identity_key(session.name, frappe.generate_hash()), 'Event', text, agent=agent)


def transfer(session, state, *, turn=None, flow=None, result=None, execution_id=None):
    """Accept one verified request, persisting only its selected output metadata."""
    from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration, selected_values
    from messaging.chatbot.doctype.bot_flow.execution import require_execution_result
    settings = get_bot_settings(for_update=True)
    if not settings.human_attention_enabled:
        return settings_text(settings, 'human_disabled_message')
    try:
        config = configuration(settings, 'human', operational=True)
        if not flow or config['flow'].name != flow.name:
            raise frappe.ValidationError(_('The human support Flow has changed.'))
        require_execution_result(config['flow'], result or {}, session=session.name, turn=turn, execution_id=execution_id)
        fields = selected_values(config, result['status'], result['data'])
    except (frappe.ValidationError, frappe.PermissionError):
        frappe.clear_last_message()
        return get_internal_error_message()
    requests = json.loads(session.human_requests or '[]')
    if any(item['execution_id'] == execution_id for item in requests):
        return result['text']
    if session.status in (*HUMAN_OPEN, 'Executing', 'Recovery Required', 'Closed', 'Expired'):
        return get_internal_error_message()
    pending = frappe.get_all('Bot Dispatch', filters={'session': session.name, 'message_type': 'Automatic',
        'status': ['in', ['Reserved', 'Prepared', 'Held', 'Sending', 'Unknown']]}, fields=['name', 'status', 'turn'])
    pending = [row for row in pending if row.turn != turn or row.status in ('Sending', 'Unknown')]
    if any(row.status in ('Sending', 'Unknown') for row in pending):
        return get_internal_error_message()
    for dispatch in pending:
        frappe.db.set_value('Bot Dispatch', dispatch.name, {'status': 'Rejected', 'resolved_at': utcnow()})
    stamp = utcnow()
    session.human_requested_at = session.opened_at = session.last_activity = stamp
    session.human_request_turn = turn
    session.assigned_agent = None
    session.assigned_at = None
    session.human_assignment_dispatch = None
    requests.append(dict(execution_id=execution_id, execution_log=result['execution_log'], turn=turn,
        flow=flow.name, status=result['status'], requested_at=stamp.isoformat(),
        fields=[dict(key=key, **value) for key, value in fields.items()]))
    session.human_requests = json.dumps(requests, ensure_ascii=False, allow_nan=False)
    session.status = 'Human Queued'
    session.active_flow = None
    state.clear()
    state.update(queue=[], values={}, greeted=True, completed_count=0)
    event(session, 'Human request queued.', agent=None)
    # This snapshot and the functional confirmation share the caller's transaction.
    session.save(ignore_permissions=True)
    frappe.publish_realtime(
        'messaging_conversation_update',
        dict(channel=session.channel, account=session.account, session=session.name),
        after_commit=True,
    )
    session_name = session.name
    contact_name = session.contact
    channel = session.channel
    account = session.account
    frappe.db.after_commit.add(
        lambda: notify_human_request_queued(
            session_name=session_name,
            contact_name=contact_name,
            channel=channel,
            account=account,
            turn=turn,
        )
    )
    if flow.process_method == 'messaging.presets.processes.human_availability.handle':
        from messaging.communication.doctype.process_definition.execution import _reset_human_request_count
        message = frappe.db.get_value('Chat Message',
            {'turn': turn, 'session': session.name, 'contact': session.contact, 'kind': 'Incoming'}, 'name')
        if message:
            contact = session.contact
            frappe.db.after_commit.add(lambda: _reset_human_request_count(contact, message))
    return result.get('text') or ''


def assignment_notice(session, contact):
    """Create one normal Human dispatch on the first effective assignment of this request."""
    if session.human_assignment_dispatch:
        return None
    content = _request_message(session, contact, 'human_active_message')
    if not content:
        raise frappe.ValidationError(_('Configure the Human Support Assigned Message.'))
    key = identity_key(session.name, session.human_request_turn, 'human_assignment')
    existing = frappe.db.get_value('Bot Dispatch', {'outbound_key': key}, 'name')
    notice = frappe.get_doc('Bot Dispatch', existing) if existing else frappe.get_doc(dict(
        doctype='Bot Dispatch', outbound_key=key, contact=contact.name, session=session.name,
        channel=contact.channel, account=contact.account, message_type='Human', quota_kind='Human',
        agent=session.assigned_agent, status='Prepared', content=content, reserved_at=utcnow())).insert(ignore_permissions=True)
    session.human_assignment_dispatch = notice.name
    return notice


def _request_message(session, contact, key):
    """Resolve deferred operational text using the original human request language."""
    settings = get_bot_settings()
    from messaging.chatbot.doctype.bot_settings.language import presentation_language
    # The request turn owns this deferred notice, regardless of later contact messages.
    request_turn = frappe.db.get_value('Chat Turn', session.human_request_turn,
        ['contact', 'session', 'input_summary'], as_dict=True) if session.human_request_turn else None
    if request_turn and (request_turn.contact != contact.name or request_turn.session != session.name):
        raise frappe.PermissionError(_('The turn does not belong to this contact.'))
    language = json.loads(request_turn.input_summary or '{}').get('language') if request_turn else None
    with presentation_language(language):
        return settings_text(settings, key)


def _completion_notice(session, contact):
    """Persist one automatic close notice in the closing transaction; never send here."""
    if session.close_reason not in ('Manual', 'Inactivity'):
        return
    content = _request_message(session, contact, 'human_completed_message')
    if not content:
        raise frappe.ValidationError(_('Configure the Human Support Completed Message.'))
    key = identity_key(session.name, session.human_request_turn, 'human_completion')
    if not frappe.db.exists('Bot Dispatch', {'outbound_key': key}):
        frappe.get_doc(dict(doctype='Bot Dispatch', outbound_key=key,
            contact=contact.name, session=session.name, turn=session.human_request_turn,
            channel=contact.channel, account=contact.account,
            message_type='Automatic', quota_kind='Human', status='Prepared',
            content=content, reserved_at=utcnow())).insert(ignore_permissions=True)
    from messaging.conversation.doctype.chat_contact.chat_contact import schedule_contact
    schedule_contact(contact.name)


def request_human(session, state, request, decision, settings, *, turn):
	"""Queue the ordinary configured Flow directly, using its own capture and decisions."""
	requested = bool(decision.get('human_requested') or decision.get('assigned_human'))
	if not requested and not state.get('human_attention'):
		return None
	if not settings.human_attention_enabled:
		session.status = 'Active'
		return settings_text(settings, "human_disabled_message")
	try:
		settings.validate_human_configuration()
	except (frappe.ValidationError, frappe.PermissionError):
		frappe.clear_last_message()
		return get_internal_error_message()
	if state.get('human_attention'):
		return None
	flow = settings.human_flow_configuration(operational=True)
	session.enqueue(state, [flow.name], settings, human_attention=True)
	session.active_flow = flow.name
	session.status = 'Active'
	return None


def _close(session, contact, *, reason):
	session.status = 'Closed'
	session.active_key = None
	session.closed_at = utcnow()
	session.closed_by = frappe.session.user
	session.close_reason = reason
	session.active_flow = None
	session.dialogue_state = json.dumps(dict(queue=[],values={},greeted=False,completed_count=0))
	contact.active_session = None
	event(session, 'Human conversation closed for inactivity.' if reason == 'Inactivity' else 'Human conversation closed.', agent=frappe.session.user)
	session.save(ignore_permissions=True)
	contact.save(ignore_permissions=True)
	_completion_notice(session, contact)


@frappe.whitelist(methods=['POST'])
def act(session_name, action, agent=None, expected_modified=None):
	"""Authenticated actions serialize with ingestion and dispatch on the contact connection lock."""
	require_agent()
	initial = frappe.get_doc('Chat Session', session_name)
	check_session(initial)
	key = frappe.db.get_value('Chat Contact', initial.contact, 'identity_key')
	with contact_lock(key):
		try:
			contact = lock_contact(initial.contact)
			session = frappe.get_doc('Chat Session', session_name)
			check_session(session)
			if action == 'Assign' and not is_manager():
				frappe.throw(_("Only System Manager can reassign conversations."), frappe.PermissionError)
			if action == 'Assign':
				if not expected_modified or get_datetime(expected_modified) != get_datetime(session.modified):
					frappe.throw(_("The conversation changed. Refresh before assigning."))
				if session.status not in HUMAN_OPEN or not agent or capacity(session, agent) != agent:
					frappe.throw(_("The agent is unavailable or has reached capacity."))
				session.assigned_agent = agent
				session.assigned_at = utcnow()
				session.read_sequence = 0
				if session.status == 'Human Queued':
					session.status = 'Human Active'
			elif action == 'Take':
				if session.status != 'Human Queued' or session.assigned_agent or capacity(session, frappe.session.user) != frappe.session.user:
					frappe.throw(_("This conversation cannot be taken by this agent."))
				session.assigned_agent = frappe.session.user
				session.assigned_at = utcnow()
				session.read_sequence = 0
				session.status = 'Human Active'
			elif action in ('Pause', 'Resume'):
				frappe.throw(_("Pausing or resuming human conversations is not permitted."))
			elif action == 'Close':
				if not is_manager() and session.assigned_agent != frappe.session.user:
					frappe.throw(_("The conversation must be assigned to you."), frappe.PermissionError)
				if session.status not in HUMAN_STATES:
					frappe.throw(_("This human conversation is already closed."))
				_close(session, contact, reason='Manual')
			else:
				frappe.throw(_("Invalid human conversation action."))
			notice = assignment_notice(session, contact) if action in ('Take', 'Assign') else None
			if action != 'Close':
				session.last_activity = utcnow()
				event(session, 'Human conversation action: ' + action + '.', agent=frappe.session.user)
				session.save(ignore_permissions=True)
			frappe.publish_realtime(
				'messaging_conversation_update',
				dict(channel=session.channel, account=session.account, session=session.name),
				after_commit=True,
			)
			frappe.db.commit()
			if notice:
				from messaging.chatbot.doctype.bot_dispatch.delivery import deliver_human_locked
				deliver_human_locked(notice)
			return dict(session=session.name,status=session.status,assigned_agent=session.assigned_agent)
		except Exception:
			frappe.db.rollback()
			raise


@automatic_job
def close_inactive(*, now=None):
	"""Close queued waits and assigned inactivity with the same policy and contact lock."""
	settings = get_bot_settings()
	from messaging.chatbot.doctype.bot_settings.bot_settings import validate_human_inactivity
	minutes = validate_human_inactivity(settings.human_inactivity_minutes) if settings.human_attention_enabled else max(1, int(settings.human_inactivity_minutes or 1))
	cutoff = (now or utcnow())-timedelta(minutes=minutes)
	closed = []
	candidates = frappe.get_all(
		'Chat Session',
		filters={
			'status': ['in', HUMAN_ASSIGNED],
			'assigned_agent': ['is', 'set'],
			'last_activity': ['<=', cutoff],
		},
		fields=['name', 'contact'],
	)
	candidates += frappe.get_all(
		'Chat Session',
		filters={
			'status': 'Human Queued',
			'assigned_agent': ['is', 'not set'],
			'human_requested_at': ['<=', cutoff],
		},
		fields=['name', 'contact'],
	)
	for row in candidates:
		key = frappe.db.get_value('Chat Contact',row.contact,'identity_key')
		with contact_lock(key):
			try:
				contact = lock_contact(row.contact)
				session = frappe.get_doc('Chat Session',row.name)
				# Recheck after Take, ingress or another maintenance run acquired the lock.
				queued_expired = (
					session.status == 'Human Queued'
					and not session.assigned_agent
					and session.human_requested_at
					and get_datetime(session.human_requested_at) <= cutoff
				)
				assigned_expired = (
					session.status in HUMAN_ASSIGNED
					and session.assigned_agent
					and get_datetime(session.last_activity) <= cutoff
				)
				if queued_expired or assigned_expired:
					_close(session,contact,reason='Inactivity')
					closed.append(session.name)
					frappe.publish_realtime(
						'messaging_conversation_update',
						dict(channel=session.channel, account=session.account, session=session.name),
						after_commit=True,
					)
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				raise
	return closed
