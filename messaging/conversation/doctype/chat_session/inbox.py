"""Desk inbox APIs with bounded pagination and server-side assignment/account checks."""
import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, getdate

from messaging.chatbot.doctype.bot_dispatch.delivery import deliver_human_locked
from messaging.communication.doctype.communication_channel_account.binding import account_provider, resolve_binding
from messaging.common.primitives import utcnow
from messaging.common.locking import contact_lock
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_contact.chat_contact import lock_contact
from messaging.conversation.doctype.chat_message.chat_message import dispatch_content, origin_label
from messaging.conversation.doctype.chat_session.permissions import check_session, has_scope, is_manager, require_agent

SESSION_FIELDS = [
	'name', 'contact', 'channel', 'account', 'status', 'assigned_agent', 'last_activity',
	'human_requested_at', 'assigned_at', 'paused_at', 'closed_at', 'closed_by', 'close_reason',
	'opened_at', 'expires_at', 'creation',
	'message_sequence', 'last_incoming_sequence', 'read_sequence', 'modified'
]
TERMINAL_STATES = ('Closed', 'Expired')
CLOSE_REASONS = ('Manual', 'Inactivity', 'Technical Error')
ATTENTION_TYPES = ('Automatic', 'Human')
PUBLIC_ACCOUNT_KEYS = ('phone_number', 'display_number', 'public_identifier', 'public_id', 'phone')
ALLOWED_HISTORY_ORDERS = {
	'last_activity desc, name desc',
	'closed_at desc, name desc',
	'creation desc, name desc',
}


def close_reason_label(reason):
	"""Return translated label for canonical Chat Session close_reason values."""
	if reason == 'Manual':
		return _('Closed manually')
	if reason == 'Inactivity':
		return _('Closed due to inactivity')
	if reason == 'Technical Error':
		return _('Technical error')
	return _(reason) if reason else ''


def attention_type_label(attention_type):
	"""Return translated label for session attention classification."""
	if attention_type == 'Human':
		return _('Human attention')
	if attention_type == 'Automatic':
		return _('Automatic attention')
	return _(attention_type) if attention_type else ''


def _to_utc_naive(value):
	if not value:
		return None
	dt = get_datetime(value)
	if dt.tzinfo is not None:
		return dt.astimezone(timezone.utc).replace(tzinfo=None)
	return dt


def _creation_to_utc(value):
	"""Convert Frappe system-timezone creation stamp to UTC naive datetime."""
	if not value:
		return None
	dt = get_datetime(value)
	if dt.tzinfo is not None:
		return dt.astimezone(timezone.utc).replace(tzinfo=None)
	tz_name = frappe.utils.get_system_timezone() or 'UTC'
	try:
		zone = ZoneInfo(tz_name)
	except (ZoneInfoNotFoundError, KeyError, ValueError):
		return dt
	return dt.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)


def _iso_utc(dt):
	return dt.isoformat() if dt else None


def _compute_session_timings(row, first_msg_sent_at=None):
	"""Derive consistent UTC timestamps and factual durations without fabrication."""
	attention_type = 'Human' if (row.get('human_requested_at') or row.get('assigned_agent')) else 'Automatic'
	status = row.get('status') or ''
	start_candidates = [
		d for d in (
			_to_utc_naive(first_msg_sent_at),
			_creation_to_utc(row.get('creation')),
			_to_utc_naive(row.get('opened_at')),
			_to_utc_naive(row.get('human_requested_at')),
		)
		if d is not None
	]
	started_dt = min(start_candidates) if start_candidates else None

	if status == 'Closed':
		ended_dt = _to_utc_naive(row.get('closed_at'))
	elif status == 'Expired':
		ended_dt = _to_utc_naive(row.get('closed_at')) or _to_utc_naive(row.get('expires_at'))
	else:
		ended_dt = None

	activity_dt = (
		_to_utc_naive(row.get('closed_at'))
		or _to_utc_naive(row.get('last_activity'))
		or _to_utc_naive(row.get('expires_at'))
	)

	total_duration_seconds = (
		int((ended_dt - started_dt).total_seconds())
		if (started_dt and ended_dt and ended_dt >= started_dt)
		else None
	)

	req_dt = _to_utc_naive(row.get('human_requested_at'))
	assigned_dt = _to_utc_naive(row.get('assigned_at'))
	closed_dt = _to_utc_naive(row.get('closed_at'))

	human_wait_seconds = (
		int((assigned_dt - req_dt).total_seconds())
		if (attention_type == 'Human' and req_dt and assigned_dt and assigned_dt >= req_dt)
		else None
	)
	human_attention_seconds = (
		int((closed_dt - assigned_dt).total_seconds())
		if (attention_type == 'Human' and row.get('assigned_agent') and assigned_dt and closed_dt and closed_dt >= assigned_dt)
		else None
	)

	return {
		'attention_type': attention_type,
		'attention_type_label': attention_type_label(attention_type),
		'status_label': _(status) if status else '',
		'started_at': _iso_utc(started_dt),
		'ended_at': _iso_utc(ended_dt),
		'activity_at': _iso_utc(activity_dt),
		'total_duration_seconds': total_duration_seconds,
		'human_wait_seconds': human_wait_seconds,
		'human_attention_seconds': human_attention_seconds,
	}


def _extract_public_id_from_values(vals, fallback_account_id):
	for v in vals or ():
		if v.get('parameter_key') in PUBLIC_ACCOUNT_KEYS:
			val = v.get('value')
			if val and not str(val).startswith('__Auth:'):
				return val
	return fallback_account_id


def _escape_like(term):
	return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def page_size(value):
	return max(1, min(cint(value) or 40, 100))


def get_account_public_id(account_name):
	"""Obtain public identifier of channel account without exposing credentials or secrets."""
	if not account_name:
		return ''
	vals = frappe.get_all(
		'Communication Provider Value',
		filters={'parent': account_name, 'parenttype': 'Communication Channel Account'},
		fields=['parameter_key', 'value']
	)
	fallback = frappe.db.get_value('Communication Channel Account', account_name, 'account_id') or account_name
	return _extract_public_id_from_values(vals, fallback)


def agent_capacity_status(channel, account, user, *, exclude_session=None):
	"""Read current agent role, grant, availability, and occupied capacity for an account."""
	user_enabled = bool(frappe.db.get_value('User', user, 'enabled'))
	has_role = user_enabled and 'Messaging Agent' in frappe.get_roles(user)
	grant = frappe.db.get_value(
		'Chat Agent',
		{'channel': channel, 'account': account, 'user': user, 'enabled': 1},
		['available', 'capacity'],
		as_dict=True
	)
	filters = {
		'channel': channel,
		'account': account,
		'status': ['in', ['Human Active', 'Human Paused']],
		'assigned_agent': user,
	}
	if exclude_session:
		filters['name'] = ['!=', exclude_session]
	occupied = frappe.db.count('Chat Session', filters)
	cap = cint(grant.capacity or 1) if grant else 0
	return {
		'has_role': bool(has_role),
		'has_grant': bool(grant),
		'available': bool(grant and grant.available),
		'capacity': cap,
		'occupied': occupied,
		'remaining': max(0, cap - occupied) if grant else 0,
	}


def check_can_take(session, user):
	"""Check if user has valid capacity and assignment eligibility without acquiring row locks."""
	if session.status != 'Human Queued' or session.assigned_agent:
		return False
	status = agent_capacity_status(session.channel, session.account, user, exclude_session=session.name)
	return bool(status['has_role'] and status['has_grant'] and status['available'] and status['occupied'] < (status['capacity'] or 1))


def _build_public_session(
	row,
	*,
	contact_data=None,
	channel_info=None,
	account_info=None,
	account_public_id=None,
	last_msg=None,
	first_msg_sent_at=None,
	user_names=None,
	unread=None,
	historical=False,
):
	row = dict(row)
	user = frappe.session.user
	manager = is_manager(user)
	contact_probe = frappe.new_doc('Chat Contact')
	can_view_sender = user == 'Administrator' or contact_probe.has_permlevel_access_to('external_sender')
	can_view_profile = user == 'Administrator' or contact_probe.has_permlevel_access_to('parameter_values')

	if contact_data is None and row.get('contact'):
		contact_data = frappe.db.get_value(
			'Chat Contact', row.get('contact'), ['external_sender', 'parameter_values'], as_dict=True
		)

	if contact_data:
		ext = contact_data.get('external_sender') if can_view_sender else ''
		row['external_sender'] = ext or ''
		params = frappe.parse_json(contact_data.get('parameter_values') or '{}') if can_view_profile else {}
		row['contact_name'] = params.get('name') or params.get('nombre') or row['external_sender'] or row['contact']
	else:
		row['external_sender'] = ''
		row['contact_name'] = row.get('contact', '')

	if channel_info is None and row.get('channel'):
		channel_info = frappe.db.get_value(
			'Communication Channel', row.get('channel'), ['channel_name', 'provider'], as_dict=True
		)
	row['channel_name'] = channel_info.get('channel_name') if channel_info else row.get('channel', '')
	row['provider'] = channel_info.get('provider') if channel_info else ''

	if account_info is None and row.get('account'):
		account_info = frappe.db.get_value(
			'Communication Channel Account', row.get('account'), ['account_name', 'account_id'], as_dict=True
		)
	row['account_name'] = account_info.get('account_name') if account_info else row.get('account', '')
	row['account_id'] = account_info.get('account_id') if account_info else row.get('account', '')
	if account_public_id is None:
		account_public_id = get_account_public_id(row.get('account'))
	row['account_public_id'] = account_public_id

	if last_msg is None and not historical:
		last_msg = frappe.db.get_value(
			'Chat Message', {'session': row['name']}, ['kind', 'content', 'sent_at'],
			order_by='sequence desc', as_dict=True
		)
	last_content = (last_msg.get('content') or '') if last_msg else ''
	if last_msg and last_msg.get('kind') == 'Event' and last_content:
		last_content = _(last_content)
	row['last_message'] = last_content[:140]
	row['last_message_kind'] = last_msg.get('kind') if last_msg else ''
	last_sent = last_msg.get('sent_at') if last_msg else None
	row['last_message_time'] = last_sent.isoformat() if last_sent else None

	if row.get('human_requested_at'):
		req_dt = get_datetime(row['human_requested_at'])
		now_dt = get_datetime(utcnow())
		row['wait_time_seconds'] = max(0, int((now_dt - req_dt).total_seconds()))
	else:
		row['wait_time_seconds'] = 0

	status = row.get('status')
	is_terminal = status in TERMINAL_STATES
	if unread is not None:
		row['unread'] = unread
	elif historical or is_terminal:
		row['unread'] = 0
	else:
		row['unread'] = frappe.db.count('Chat Message', dict(
			session=row['name'], kind='Incoming', sequence=['>', row.get('read_sequence') or 0]
		))

	scoped = bool(has_scope(row.get('channel'), row.get('account'), user))
	assigned = row.get('assigned_agent')
	closed_by = row.get('closed_by')

	if user_names is not None:
		row['assigned_agent_name'] = (user_names.get(assigned) or assigned) if assigned else ''
		row['closed_by_name'] = (user_names.get(closed_by) or closed_by) if closed_by else ''
	else:
		row['assigned_agent_name'] = (
			frappe.db.get_value('User', assigned, 'full_name') or assigned
		) if assigned else ''
		row['closed_by_name'] = (
			frappe.db.get_value('User', closed_by, 'full_name') or closed_by
		) if closed_by else ''

	row['is_mine'] = bool(assigned and assigned == user)
	row['has_scope'] = scoped
	row['close_reason_label'] = close_reason_label(row.get('close_reason'))

	timings = _compute_session_timings(row, first_msg_sent_at=first_msg_sent_at)
	row.update(timings)

	if historical or is_terminal:
		row['can_take'] = False
		row['can_reply'] = False
		row['can_pause'] = False
		row['can_resume'] = False
		row['can_close'] = False
		row['can_assign'] = False
	else:
		row['can_take'] = bool(
			status == 'Human Queued'
			and not assigned
			and scoped
		)
		row['can_reply'] = bool(status == 'Human Active' and row['is_mine'] and scoped)
		row['can_pause'] = False
		row['can_resume'] = False
		row['can_close'] = bool(
			status in ('Human Requested', 'Human Queued', 'Human Active', 'Human Paused')
			and (row['is_mine'] or manager)
			and scoped
		)
		row['can_assign'] = bool(manager and status in ('Human Queued', 'Human Active', 'Human Paused'))

	return row


def public_session(row):
	return _build_public_session(row)


def _enrich_historical_sessions(rows):
	"""Batch-load related contact, provenance, user, and boundary message metadata without N+1 queries."""
	if not rows:
		return []

	session_names = [r['name'] for r in rows]
	contact_names = list({r['contact'] for r in rows if r.get('contact')})
	channel_names = list({r['channel'] for r in rows if r.get('channel')})
	account_names = list({r['account'] for r in rows if r.get('account')})
	user_ids = list({u for r in rows for u in (r.get('assigned_agent'), r.get('closed_by')) if u})

	contacts_map = {
		c['name']: c
		for c in frappe.get_all(
			'Chat Contact',
			filters={'name': ['in', contact_names]},
			fields=['name', 'external_sender', 'parameter_values'],
		)
	} if contact_names else {}

	channels_map = {
		ch['name']: ch
		for ch in frappe.get_all(
			'Communication Channel',
			filters={'name': ['in', channel_names]},
			fields=['name', 'channel_name', 'provider'],
		)
	} if channel_names else {}

	accounts_map = {
		acc['name']: acc
		for acc in frappe.get_all(
			'Communication Channel Account',
			filters={'name': ['in', account_names]},
			fields=['name', 'account_id', 'account_name'],
		)
	} if account_names else {}

	provider_vals_by_account = {}
	if account_names:
		for val in frappe.get_all(
			'Communication Provider Value',
			filters={
				'parent': ['in', account_names],
				'parenttype': 'Communication Channel Account',
				'parameter_key': ['in', list(PUBLIC_ACCOUNT_KEYS)],
			},
			fields=['parent', 'parameter_key', 'value'],
		):
			provider_vals_by_account.setdefault(val['parent'], []).append(val)

	public_id_map = {
		acc_name: _extract_public_id_from_values(
			provider_vals_by_account.get(acc_name),
			(accounts_map.get(acc_name) or {}).get('account_id') or acc_name,
		)
		for acc_name in account_names
	}

	user_names = {
		u['name']: u['full_name'] or u['name']
		for u in frappe.get_all(
			'User',
			filters={'name': ['in', user_ids]},
			fields=['name', 'full_name'],
		)
	} if user_ids else {}

	first_msg_map = {}
	last_msg_map = {}
	edge_rows = frappe.db.sql(
		"""
		SELECT m.session, m.sequence, m.kind, m.content, m.sent_at,
		       b.min_seq, b.max_seq
		FROM (
			SELECT session, MIN(sequence) AS min_seq, MAX(sequence) AS max_seq
			FROM `tabChat Message`
			WHERE session IN %s
			GROUP BY session
		) b
		INNER JOIN `tabChat Message` m
			ON m.session = b.session AND m.sequence IN (b.min_seq, b.max_seq)
		""",
		(tuple(session_names),),
		as_dict=True,
	)
	for msg in edge_rows:
		if msg['sequence'] == msg['min_seq']:
			first_msg_map[msg['session']] = msg['sent_at']
		if msg['sequence'] == msg['max_seq']:
			last_msg_map[msg['session']] = msg

	return [
		_build_public_session(
			row,
			contact_data=contacts_map.get(row.get('contact')),
			channel_info=channels_map.get(row.get('channel')),
			account_info=accounts_map.get(row.get('account')),
			account_public_id=public_id_map.get(row.get('account'), ''),
			last_msg=last_msg_map.get(row['name']),
			first_msg_sent_at=first_msg_map.get(row['name']),
			user_names=user_names,
			unread=0,
			historical=True,
		)
		for row in rows
	]


def _count_sessions(filters):
	res = frappe.get_list(
		'Chat Session',
		filters=filters,
		fields=[{'COUNT': '*', 'as': 'total'}],
		limit_page_length=0,
	)
	if not res:
		return 0
	row = res[0]
	return cint(row.get('total') if isinstance(row, dict) else row[0])


@frappe.whitelist()
def conversations(status=None, mine=0, start=0, limit=40, channel=None, account=None, search=None, search_condition=None, unassigned_only=0, order_by=None, other_agents=0):
	require_agent()
	if cint(mine) and cint(other_agents):
		frappe.throw(_("Invalid conversation filter."))
	filters = {'human_requested_at': ['is', 'set']}
	extra_filters = []

	if cint(other_agents):
		if not is_manager():
			frappe.throw(_("Only System Manager can view conversations assigned to other agents."), frappe.PermissionError)
		extra_filters = [
			['assigned_agent', 'is', 'set'],
			['assigned_agent', '!=', ''],
			['assigned_agent', '!=', frappe.session.user],
		]
		if status in ('Human Active', 'Active', 'active'):
			filters['status'] = 'Human Active'
		elif status in ('Human Paused', 'Paused', 'paused'):
			filters['status'] = 'Human Paused'
		elif status == 'Closed':
			filters['status'] = 'Closed'
		elif status in (None, '', 'All', 'all'):
			filters['status'] = ['in', ['Human Active', 'Human Paused']]
		else:
			frappe.throw(_("Invalid conversation filter."))
		effective_order = order_by or 'last_activity desc, name desc'
	elif cint(mine):
		filters['assigned_agent'] = frappe.session.user
		if status in ('Human Active', 'Active', 'active'):
			filters['status'] = 'Human Active'
		elif status in ('Human Paused', 'Paused', 'paused'):
			filters['status'] = 'Human Paused'
		elif status == 'Closed':
			filters['status'] = 'Closed'
		elif status in (None, '', 'All', 'all'):
			filters['status'] = ['in', ['Human Active', 'Human Paused']]
		else:
			frappe.throw(_("Invalid conversation filter."))
		effective_order = order_by or 'last_activity desc, name desc'
	elif cint(unassigned_only):
		filters['assigned_agent'] = ['in', ['', None]]
		if status:
			if status in ('Human Queued', 'Waiting', 'waiting'):
				filters['status'] = 'Human Queued'
			elif status in ('Human Requested', 'Pending', 'pending'):
				filters['status'] = 'Human Requested'
			elif status in ('All', 'all', ''):
				filters['status'] = ['in', ['Human Queued', 'Human Requested']]
			elif status in ('Human Active', 'Human Paused', 'Closed'):
				filters['status'] = status
			else:
				frappe.throw(_("Invalid conversation filter."))
		else:
			filters['status'] = ['in', ['Human Queued', 'Human Requested']]
		effective_order = order_by or 'human_requested_at asc, last_activity asc, name desc'
	else:
		if status:
			if status in ('Waiting', 'waiting'):
				filters['status'] = 'Human Queued'
			elif status in ('Pending', 'pending'):
				filters['status'] = 'Human Requested'
			elif status in ('Active', 'active'):
				filters['status'] = 'Human Active'
			elif status in ('Paused', 'paused'):
				filters['status'] = 'Human Paused'
			elif status in ('All', 'all', ''):
				filters['status'] = ['in', ['Human Requested', 'Human Queued', 'Human Active', 'Human Paused']]
			elif status in ('Human Requested', 'Human Queued', 'Human Active', 'Human Paused', 'Closed'):
				filters['status'] = status
			else:
				frappe.throw(_("Invalid conversation filter."))
		else:
			filters['status'] = ['in', ['Human Requested', 'Human Queued', 'Human Active', 'Human Paused']]
		effective_order = order_by or 'last_activity desc, name desc'

	if channel:
		filters['channel'] = channel

	if account:
		filters['account'] = account

	or_filters = None
	if search and isinstance(search, str) and search.strip():
		term = search.strip()
		exact = search_condition == '='
		if exact:
			contact_or = [['external_sender', '=', term]]
			if not any(char in term for char in '%_\\"\''):
				contact_or.append(['parameter_values', 'like', f'%"{term}"%'])
			matching_contacts = frappe.get_list(
				'Chat Contact',
				or_filters=contact_or,
				pluck='name',
				limit_page_length=500,
			)
			or_filters = {'name': term}
			if matching_contacts:
				or_filters['contact'] = ['in', matching_contacts]
		else:
			matching_contacts = frappe.get_list(
				'Chat Contact',
				or_filters={'parameter_values': ['like', f'%{term}%'], 'external_sender': ['like', f'%{term}%']},
				pluck='name'
			)
			if matching_contacts:
				or_filters = {
					'contact': ['in', matching_contacts],
					'name': ['like', f'%{term}%']
				}
			else:
				filters['name'] = ['like', f'%{term}%']

	filter_list = []
	for key, val in filters.items():
		if isinstance(val, (list, tuple)) and len(val) == 2:
			filter_list.append([key, val[0], val[1]])
		else:
			filter_list.append([key, '=', val])
	filter_list.extend(extra_filters)

	rows = frappe.get_list(
		'Chat Session',
		filters=filter_list,
		or_filters=or_filters,
		fields=SESSION_FIELDS,
		order_by=effective_order,
		start=max(0, cint(start)),
		page_length=page_size(limit)
	)
	return [public_session(row) for row in rows]


@frappe.whitelist()
def inbox_summary(channel=None, account=None):
	"""Operational aggregate counts for pending triage and active attentions respecting scope."""
	require_agent()
	user = frappe.session.user
	manager = is_manager(user)
	scope_filters = {'human_requested_at': ['is', 'set']}
	if channel:
		scope_filters['channel'] = channel
	if account:
		scope_filters['account'] = account

	unassigned_filters = dict(scope_filters, assigned_agent=['in', ['', None]])
	waiting = _count_sessions(dict(unassigned_filters, status='Human Queued'))
	pending = _count_sessions(dict(unassigned_filters, status='Human Requested'))

	mine_filters = dict(scope_filters, assigned_agent=user)
	mine_active = _count_sessions(dict(mine_filters, status='Human Active'))
	mine_paused = _count_sessions(dict(mine_filters, status='Human Paused'))

	others = 0
	if manager:
		others_filters = [
			['human_requested_at', 'is', 'set'],
			['assigned_agent', 'is', 'set'],
			['assigned_agent', '!=', ''],
			['assigned_agent', '!=', user],
			['status', 'in', ['Human Active', 'Human Paused']],
		]
		if channel:
			others_filters.append(['channel', '=', channel])
		if account:
			others_filters.append(['account', '=', account])
		others = _count_sessions(others_filters)

	return {
		'total': waiting + pending,
		'waiting': waiting,
		'pending': pending,
		'mine': mine_active + mine_paused,
		'mine_active': mine_active,
		'mine_paused': mine_paused,
		'others': others,
		'is_manager': bool(manager),
	}


def _parse_date_boundary(value, *, end_of_day=False):
	if not value:
		return None
	try:
		parsed = getdate(value)
	except Exception:
		frappe.throw(_("Invalid date filter."))
	if not parsed:
		frappe.throw(_("Invalid date filter."))
	tz_names = ['UTC']
	user_tz = (
		frappe.db.get_value('User', frappe.session.user, 'time_zone')
		if frappe.session.user not in ('Guest', None)
		else None
	)
	for candidate in (user_tz, frappe.utils.get_system_timezone()):
		if candidate and candidate not in tz_names:
			tz_names.append(candidate)
	boundary_time = time(23, 59, 59, 999999) if end_of_day else time(0, 0, 0, 0)
	candidates_utc = []
	for tz_name in tz_names:
		try:
			zone = ZoneInfo(tz_name)
		except (ZoneInfoNotFoundError, KeyError, ValueError):
			continue
		local_dt = datetime.combine(parsed, boundary_time).replace(tzinfo=zone)
		candidates_utc.append(local_dt.astimezone(timezone.utc).replace(tzinfo=None))
	if not candidates_utc:
		return datetime.combine(parsed, boundary_time)
	return max(candidates_utc) if end_of_day else min(candidates_utc)


@frappe.whitelist()
def channels_and_accounts(channel=None):
	"""Available communication channels, accounts, and agents filtered by permission scope."""
	require_agent()
	user = frappe.session.user
	manager = is_manager(user)

	if manager:
		channels = frappe.get_all(
			'Communication Channel',
			filters={'status': 'Enabled'},
			fields=['name', 'channel_name', 'provider'],
			order_by='channel_name asc'
		)
		acc_filters = {'enabled': 1}
		if channel:
			acc_filters['channel'] = channel
		accounts = frappe.get_all(
			'Communication Channel Account',
			filters=acc_filters,
			fields=['name', 'channel', 'account_id', 'account_name'],
			order_by='account_name asc'
		)
		agent_users = set(frappe.get_all('Chat Agent', pluck='user', limit_page_length=500))
		agent_users.update(
			u for u in frappe.get_all(
				'Chat Session',
				filters={'status': ['in', list(TERMINAL_STATES)], 'assigned_agent': ['is', 'set']},
				pluck='assigned_agent',
				limit_page_length=500,
			) if u
		)
		agent_list = sorted(u for u in agent_users if u)
		full_names = {
			row.name: row.full_name or row.name
			for row in frappe.get_all(
				'User',
				filters={'name': ['in', agent_list]},
				fields=['name', 'full_name'],
			)
		} if agent_list else {}
		agents_out = [
			{'user': u, 'full_name': full_names.get(u, u)}
			for u in agent_list
		]
		agents_out.sort(key=lambda item: (item['full_name'].lower(), item['user']))
	else:
		grants = frappe.get_all('Chat Agent', filters={'user': user, 'enabled': 1}, fields=['channel', 'account'])
		channel_names = list({g['channel'] for g in grants})
		channels = frappe.get_all(
			'Communication Channel',
			filters={'name': ['in', channel_names], 'status': 'Enabled'},
			fields=['name', 'channel_name', 'provider'],
			order_by='channel_name asc'
		) if channel_names else []
		acc_names = [g['account'] for g in grants]
		acc_filters = {'name': ['in', acc_names], 'enabled': 1}
		if channel:
			acc_filters['channel'] = channel
		accounts = frappe.get_all(
			'Communication Channel Account',
			filters=acc_filters,
			fields=['name', 'channel', 'account_id', 'account_name'],
			order_by='account_name asc'
		) if acc_names else []
		if grants:
			full_name = frappe.db.get_value('User', user, 'full_name') or user
			agents_out = [{'user': user, 'full_name': full_name}]
		else:
			agents_out = []

	for acc in accounts:
		acc['public_id'] = get_account_public_id(acc['name'])

	return {
		'channels': channels,
		'accounts': accounts,
		'agents': agents_out,
		'is_manager': bool(manager),
	}


@frappe.whitelist()
def historical_conversations(
	status=None,
	close_reason=None,
	attention_type=None,
	channel=None,
	account=None,
	agent=None,
	mine=0,
	date_from=None,
	date_to=None,
	search=None,
	search_condition=None,
	start=0,
	limit=40,
	order_by=None,
):
	"""Read-only, paginated query of terminal (Closed/Expired) sessions respecting strict agent scope."""
	require_agent()
	user = frappe.session.user
	manager = is_manager(user)

	if status in (None, '', 'All', 'all'):
		status_filter = ['in', list(TERMINAL_STATES)]
	elif status in TERMINAL_STATES:
		status_filter = status
	else:
		frappe.throw(_("Invalid conversation filter."))

	if close_reason in (None, '', 'All', 'all'):
		reason_filter = None
	elif close_reason in CLOSE_REASONS:
		reason_filter = close_reason
	else:
		frappe.throw(_("Invalid conversation filter."))

	if attention_type in (None, '', 'All', 'all'):
		att_type = None
	elif attention_type in ATTENTION_TYPES:
		att_type = attention_type
	else:
		frappe.throw(_("Invalid conversation filter."))

	if order_by and order_by not in ALLOWED_HISTORY_ORDERS:
		frappe.throw(_("Invalid conversation filter."))
	effective_order = order_by or 'last_activity desc, name desc'

	start_dt = _parse_date_boundary(date_from, end_of_day=False) if date_from else None
	end_dt = _parse_date_boundary(date_to, end_of_day=True) if date_to else None
	if date_from and date_to and getdate(date_from) > getdate(date_to):
		frappe.throw(_("Start date cannot be after end date."))

	if cint(mine) and agent and agent != user:
		frappe.throw(_("Invalid conversation filter."))

	filter_list = [
		['status', status_filter[0], status_filter[1]]
		if isinstance(status_filter, list)
		else ['status', '=', status_filter]
	]

	if not manager:
		grants = frappe.get_all('Chat Agent', filters={'user': user, 'enabled': 1}, fields=['channel', 'account'])
		allowed_channels = {g['channel'] for g in grants}
		allowed_accounts = {g['account'] for g in grants}
		if channel and channel not in allowed_channels:
			frappe.throw(_("This channel is outside your account scope."), frappe.PermissionError)
		if account and account not in allowed_accounts:
			frappe.throw(_("This account is outside your account scope."), frappe.PermissionError)
		if agent and agent != user:
			frappe.throw(_("This conversation is outside your assignment or account scope."), frappe.PermissionError)
		if not grants or att_type == 'Automatic':
			return []
		filter_list.append(['assigned_agent', '=', user])
		filter_list.append(['human_requested_at', 'is', 'set'])
		filter_list.append(['account', 'in', sorted(allowed_accounts)])

	if reason_filter:
		filter_list.append(['close_reason', '=', reason_filter])

	if att_type == 'Human':
		filter_list.append(['human_requested_at', 'is', 'set'])
	elif att_type == 'Automatic':
		filter_list.append(['human_requested_at', 'is', 'not set'])
		filter_list.append(['assigned_agent', 'in', ['', None]])

	if cint(mine):
		filter_list.append(['assigned_agent', '=', user])
		filter_list.append(['human_requested_at', 'is', 'set'])
	elif agent and manager:
		filter_list.append(['assigned_agent', '=', agent])

	if channel:
		filter_list.append(['channel', '=', channel])
	if account:
		filter_list.append(['account', '=', account])
	if start_dt:
		filter_list.append(['last_activity', '>=', start_dt])
	if end_dt:
		filter_list.append(['last_activity', '<=', end_dt])

	or_filters = None
	if search is not None and search != '':
		if not isinstance(search, str):
			frappe.throw(_("Invalid conversation filter."))
		term = search.strip()
		if term:
			contact_probe = frappe.new_doc('Chat Contact')
			can_search_sender = user == 'Administrator' or contact_probe.has_permlevel_access_to('external_sender')
			can_search_profile = user == 'Administrator' or contact_probe.has_permlevel_access_to('parameter_values')
			exact = search_condition == '='
			if exact:
				contact_or = [['name', '=', term]]
				if can_search_sender:
					contact_or.append(['external_sender', '=', term])
				if can_search_profile and not any(ch in term for ch in '%_\\"\''):
					contact_or.append(['parameter_values', 'like', f'%"{term}"%'])
				matching_contacts = frappe.get_list(
					'Chat Contact',
					or_filters=contact_or,
					pluck='name',
					limit_page_length=500,
				)
				or_filters = {'name': term}
				if matching_contacts:
					or_filters['contact'] = ['in', matching_contacts]
			else:
				escaped = _escape_like(term)
				contact_or = [['name', 'like', f'%{escaped}%']]
				if can_search_sender:
					contact_or.append(['external_sender', 'like', f'%{escaped}%'])
				if can_search_profile:
					contact_or.append(['parameter_values', 'like', f'%{escaped}%'])
				matching_contacts = frappe.get_list(
					'Chat Contact',
					or_filters=contact_or,
					pluck='name',
					limit_page_length=500,
				)
				if matching_contacts:
					or_filters = {
						'contact': ['in', matching_contacts],
						'name': ['like', f'%{escaped}%'],
					}
				else:
					filter_list.append(['name', 'like', f'%{escaped}%'])

	rows = frappe.get_list(
		'Chat Session',
		filters=filter_list,
		or_filters=or_filters,
		fields=SESSION_FIELDS,
		order_by=effective_order,
		start=max(0, cint(start)),
		page_length=page_size(limit),
	)
	return _enrich_historical_sessions(rows)


@frappe.whitelist()
def history(session_name, before=None, limit=60, mine_only=0, historical=0):
	is_historical = bool(cint(historical))
	if is_historical:
		require_agent()
		if not session_name or not frappe.db.exists('Chat Session', session_name):
			frappe.throw(_("This conversation is outside your assignment or account scope."), frappe.PermissionError)
		session = frappe.get_doc('Chat Session', session_name)
		check_session(session)
		if session.status not in TERMINAL_STATES:
			frappe.throw(_("This conversation is not in the historical archive."), frappe.PermissionError)
	else:
		session = frappe.get_doc('Chat Session', session_name)
		check_session(session)
	if cint(mine_only) and session.assigned_agent != frappe.session.user:
		frappe.throw(_("This conversation is outside your assignment or account scope."), frappe.PermissionError)
	filters = {'session': session.name}
	if before:
		filters['sequence'] = ['<', cint(before)]
	limit_n = page_size(limit)
	rows = frappe.get_all(
		'Chat Message',
		filters=filters,
		fields=[
			'name', 'sequence', 'kind', 'content', 'agent', 'sent_at', 'event_at',
			'dispatch', 'external_id', 'source_type', 'reply_reference', 'selection_id'
		],
		order_by='sequence desc',
		page_length=limit_n + 1
	)
	has_more = len(rows) > limit_n
	rows = rows[:limit_n]
	dispatch_names = list({row.dispatch for row in rows if row.dispatch})
	dispatch_map = {
		d['name']: {
			'status': d['status'],
			'transport_reference': d['transport_reference'],
			'delivery_status': d['delivery_status'],
			'diagnostic_code': d['diagnostic_code'],
		}
		for d in frappe.get_all(
			'Bot Dispatch',
			filters={'name': ['in', dispatch_names]},
			fields=['name', 'status', 'transport_reference', 'delivery_status', 'diagnostic_code'],
		)
	} if dispatch_names else {}

	for row in rows:
		row['origin_label'] = origin_label(row.kind)
		if row.get('kind') == 'Event' and row.get('content'):
			row['content'] = _(row['content'])
		row['transport'] = dispatch_map.get(row.dispatch) if row.dispatch else None
		if row.get('sent_at'):
			row['sent_at'] = row['sent_at'].isoformat()
		if row.get('event_at'):
			row['event_at'] = row['event_at'].isoformat()

	contact = frappe.get_doc('Chat Contact', session.contact)
	first_msg_sent_at = frappe.db.get_value(
		'Chat Message', {'session': session.name}, 'sent_at', order_by='sequence asc'
	)
	session_data = _build_public_session(
		{f: session.get(f) for f in SESSION_FIELDS},
		first_msg_sent_at=first_msg_sent_at,
		historical=is_historical,
	)
	if not is_historical:
		session_data['can_take'] = check_can_take(session, frappe.session.user)

	can_view_account = frappe.has_permission('Communication Channel Account', 'read')
	can_view_channel = frappe.has_permission('Communication Channel', 'read')

	account_doc = frappe.db.get_value(
		'Communication Channel Account', session.account,
		['name', 'account_id', 'account_name', 'channel', 'enabled'],
		as_dict=True
	) or {}
	account_doc['public_id'] = get_account_public_id(session.account)

	channel_doc = frappe.db.get_value(
		'Communication Channel', session.channel,
		['name', 'channel_name', 'provider', 'status'],
		as_dict=True
	) or {}
	if channel_doc:
		channel_doc['enabled'] = int(channel_doc.pop('status') == 'Enabled')

	can_view_sender = frappe.session.user == 'Administrator' or contact.has_permlevel_access_to('external_sender')
	can_view_profile = frappe.session.user == 'Administrator' or contact.has_permlevel_access_to('parameter_values')
	contact_params = frappe.parse_json(contact.parameter_values or '{}') if can_view_profile else {}
	contact_sender = contact.external_sender if can_view_sender else ''
	contact_display_name = (
		contact_params.get('name')
		or contact_params.get('nombre')
		or contact_sender
		or contact.name
	)

	human_requests = json.loads(session.human_requests or '[]')
	for req in human_requests:
		if isinstance(req, dict):
			for field in req.get('fields') or []:
				if isinstance(field, dict) and field.get('label'):
					field['label'] = _(field['label'])

	return dict(
		session=session_data,
		human_requests=human_requests,
		contact=dict(
			name=contact.name,
			channel=contact.channel,
			account=contact.account,
			display_name=contact_display_name,
			external_sender=contact_sender,
			registration_status=contact.registration_status,
			registration=contact_params,
			profile_fields=contact.profile_fields() if can_view_profile else []
		),
		provenance=dict(
			channel=channel_doc,
			account=account_doc,
			can_view_account=can_view_account,
			can_view_channel=can_view_channel
		),
		agent_status=agent_capacity_status(session.channel, session.account, frappe.session.user),
		messages=list(reversed(rows)),
		has_more=has_more,
		is_manager=is_manager(),
		can_read=False if is_historical else (session.assigned_agent == frappe.session.user)
	)


@frappe.whitelist(methods=['POST'])
def mark_read(session_name, through, visible=None):
	initial = frappe.get_doc('Chat Session', session_name)
	check_session(initial)
	key = frappe.db.get_value('Chat Contact', initial.contact, 'identity_key')
	with contact_lock(key):
		try:
			lock_contact(initial.contact)
			session = frappe.get_doc('Chat Session', session_name)
			check_session(session)
			if session.assigned_agent != frappe.session.user:
				frappe.throw(_("Only the assigned agent can mark this conversation as read."), frappe.PermissionError)
			cursor = max(0, cint(through))
			if cursor > (session.message_sequence or 0):
				frappe.throw(_("Invalid read cursor."))
			previous = session.read_sequence or 0
			session.read_sequence = max(previous, cursor)
			session.save(ignore_permissions=True)
			if visible:
				if isinstance(visible, str):
					visible = frappe.parse_json(visible)
				if (not isinstance(visible, list) or len(visible) > 100
					or any(not isinstance(name, str) or len(name) > 140 for name in visible)):
					frappe.throw(_("Invalid read cursor."))
				from messaging.conversation.doctype.chat_message.read_receipts import schedule_read_receipts
				receipts = frappe.get_all('Chat Message', filters={
					'name': ['in', visible], 'session': session.name, 'kind': 'Incoming',
					'sequence': ['<=', cursor],
				}, pluck='external_id')
				schedule_read_receipts(session.account, receipts)
			frappe.db.commit()
			return session.read_sequence
		except Exception:
			frappe.db.rollback()
			raise


@frappe.whitelist(methods=['POST'])
def send(session_name, content, client_id):
	if not isinstance(content, str) or not content.strip() or len(content) > 16000 or not isinstance(client_id, str) or not 8 <= len(client_id) <= 140:
		frappe.throw(_("A message and an idempotent client identity are required."))
	initial = frappe.get_doc('Chat Session', session_name)
	check_session(initial)
	key = frappe.db.get_value('Chat Contact', initial.contact, 'identity_key')
	with contact_lock(key):
		try:
			contact = lock_contact(initial.contact)
			session = frappe.get_doc('Chat Session', session_name)
			check_session(session)
			outbound = identity_key(session.name, frappe.session.user, client_id, 'human')
			existing = frappe.db.get_value('Bot Dispatch', {'outbound_key': outbound}, 'name')
			if existing:
				from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import envelope
				doc = frappe.get_doc('Bot Dispatch', existing)
				if dispatch_content(doc) != content:
					frappe.throw(_("The message identity was already used for different content."))
				result = envelope(doc.name)
				frappe.db.commit()
				return result
			check_session(session, responding=True)
			resolve_binding(account_provider(contact.account), contact.account)
			doc = frappe.get_doc(dict(
				doctype='Bot Dispatch', outbound_key=outbound,
				contact=contact.name, session=session.name, channel=contact.channel, account=contact.account,
				message_type='Human', quota_kind='Human', agent=frappe.session.user, status='Prepared',
				content=content, reserved_at=utcnow()
			)).insert(ignore_permissions=True)
			session.last_activity = contact.last_activity = utcnow()
			session.save(ignore_permissions=True)
			contact.save(ignore_permissions=True)
			frappe.db.commit()
			return deliver_human_locked(doc)
		except Exception:
			frappe.db.rollback()
			raise


@frappe.whitelist()
def contacts(start=0, limit=40):
	require_agent()
	return frappe.get_list(
		'Chat Contact',
		fields=['name', 'channel', 'account', 'external_sender', 'registration_status', 'last_activity'],
		order_by='last_activity desc, name desc',
		start=max(0, cint(start)),
		page_length=page_size(limit)
	)


@frappe.whitelist()
def contact_profile(contact_name):
	require_agent()
	contact = frappe.get_doc('Chat Contact', contact_name)
	if not has_scope(contact.channel, contact.account):
		frappe.throw(_("This contact is outside your account scope."), frappe.PermissionError)
	conversations_raw = frappe.get_list(
		'Chat Session',
		filters={'contact': contact.name, 'human_requested_at': ['is', 'set']},
		fields=SESSION_FIELDS,
		order_by='last_activity desc',
		page_length=100
	)
	enriched = [public_session(row) for row in conversations_raw]
	contact_params = frappe.parse_json(contact.parameter_values or '{}')
	contact_display_name = (
		contact_params.get('name')
		or contact_params.get('nombre')
		or contact.external_sender
		or contact.name
	)
	return dict(
		name=contact.name,
		channel=contact.channel,
		account=contact.account,
		display_name=contact_display_name,
		external_sender=contact.external_sender,
		registration_status=contact.registration_status,
		registration=contact_params,
		profile_fields=contact.profile_fields(),
		conversations=enriched
	)


@frappe.whitelist()
def agents(session_name):
	session = frappe.get_doc('Chat Session', session_name)
	check_session(session)
	if not is_manager():
		frappe.throw(_("Only System Manager can open or reassign conversations."), frappe.PermissionError)
	return frappe.get_all(
		'Chat Agent',
		filters={'channel': session.channel, 'account': session.account, 'enabled': 1},
		fields=['user', 'available', 'capacity'],
		order_by='user',
		page_length=100
	)
