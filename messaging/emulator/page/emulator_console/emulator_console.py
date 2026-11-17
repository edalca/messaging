"""Authorized customer-side Desk operations over private neutral channel boundaries."""
import frappe
import json
from datetime import timezone
from zoneinfo import ZoneInfo
from frappe import _

from messaging.emulator.doctype.emulator_contact.emulator_contact import CHANNEL, PROVIDER, require_manager, validate_sender
from messaging.emulator.provider import client_receipt, client_receipts_bulk, validate_client_receipt
from messaging.communication.doctype.communication_provider.communication_provider import get_provider_enabled, require_provider_enabled
from messaging.communication.doctype.communication_channel_account.communication_channel_account import dispatch_ingress
from messaging.conversation.doctype.chat_message.chat_message import origin_label
from messaging.conversation.doctype.conversation_preference.conversation_preference import (
	clear_visible_history,
	get_preference,
	preference_map,
	set_archived as save_archived_preference,
	set_pinned as save_pinned_preference,
	utc_to_site_naive,
)


def account_scope(account, *, operating=False):
	require_manager()
	binding = frappe.get_doc('Communication Channel Account', account)
	binding.check_permission('read')
	if binding.channel != CHANNEL or frappe.db.get_value('Communication Channel', binding.channel, 'provider') != PROVIDER:
		frappe.throw(_("Select a Channel Emulator account."), frappe.PermissionError)
	if operating:
		if not (get_provider_enabled(PROVIDER) and binding.enabled and frappe.db.get_value('Communication Channel', CHANNEL, 'enabled')):
			frappe.throw(_("The channel emulator is disabled. Enable its configuration to use it."), frappe.PermissionError)
		require_provider_enabled(PROVIDER)
		from messaging.communication.doctype.communication_channel_account.communication_channel_account import resolve_binding
		resolve_binding(PROVIDER, binding.name)
	return binding, binding


def sender_identity(sender, *, operating=False):
	validate_sender(sender)
	contact = frappe.db.get_value('Emulator Contact', {'sender_id': sender}, ['name', 'enabled'], as_dict=True)
	if not contact or (operating and not contact.enabled):
		frappe.throw(_("Invalid or disabled emulator contact."), frappe.PermissionError)
	return sender


@frappe.whitelist()
def accounts():
	require_manager()
	bindings = frappe.get_list('Communication Channel Account', filters={'channel': CHANNEL}, fields=['name', 'account_id', 'account_name', 'enabled'], limit_page_length=500)
	if frappe.db.get_value('Communication Channel', CHANNEL, 'provider') != PROVIDER:
		bindings = []
	return dict(enabled=get_provider_enabled(PROVIDER) and bool(frappe.db.get_value('Communication Channel', CHANNEL, 'enabled')), accounts=bindings)


@frappe.whitelist()
def contacts(account=None):
	require_manager()
	catalog = frappe.get_all('Emulator Contact', filters={'enabled': 1},
		fields=['name', 'sender_id', 'display_name'], order_by='display_name, name', limit_page_length=500)
	if not account:
		return catalog
	_doc, binding = account_scope(account)
	return contact_activity(CHANNEL, binding.name, catalog)


@frappe.whitelist()
def conversations():
	"""Read the unified EMULR inbox without creating conversational documents."""
	config = accounts()
	catalog = frappe.get_all('Emulator Contact', fields=['name', 'sender_id', 'display_name', 'enabled'],
		order_by='display_name, name', limit_page_length=500)
	config['conversations'] = contact_activity(CHANNEL, [row.name for row in config['accounts']], catalog,
		conversations_only=True)
	return config


@frappe.whitelist()
def available_pairs():
	"""Availability follows persisted visible history across all episodes, per account."""
	config = accounts()
	catalog = contacts()
	account_names = tuple(row.name for row in config['accounts'])
	senders = tuple(row.sender_id for row in catalog)
	used = []
	if account_names and senders:
		used = frappe.db.sql("""SELECT DISTINCT c.account,c.external_sender AS sender_id
			FROM `tabChat Contact` c WHERE c.channel=%(channel)s AND c.account IN %(accounts)s
			AND c.external_sender IN %(senders)s AND EXISTS (SELECT 1 FROM `tabChat Message` m
				WHERE m.contact=c.name AND m.kind IN ('Incoming','Automatic','Human'))
			UNION SELECT DISTINCT n.account,n.effective_recipient AS sender_id FROM `tabNotification Queue` n
			WHERE n.channel=%(channel)s AND n.account IN %(accounts)s AND n.effective_recipient IN %(senders)s
			AND n.handoff_at IS NOT NULL AND n.status IN ('Processing','Accepted','Rejected','Unknown')""",
			dict(channel=CHANNEL, accounts=account_names, senders=senders), as_dict=True)
	return dict(**config, contacts=catalog, used=used)


@frappe.whitelist()
def history(account, sender, session=None, before=None, notification_before=None, message_before=None):
	_doc, binding = account_scope(account)
	validate_sender(sender)
	contact = frappe.db.get_value('Emulator Contact', {'sender_id': sender}, ['name', 'enabled'], as_dict=True)
	if not contact:
		frappe.throw(_("Invalid or disabled emulator contact."), frappe.PermissionError)
	result = message_history(CHANNEL, binding.name, sender, session, before, notification_before, message_before)
	for source_type, rows in [('Bot Dispatch', result['messages']), ('Notification Queue', result['notifications'])]:
		candidates = []
		for row in rows:
			transport = row['transport'] if source_type == 'Bot Dispatch' else row
			name = row.dispatch if source_type == 'Bot Dispatch' else row.name
			if transport and transport.status == 'Accepted' and transport.delivery_status not in ('Failed', 'Read'):
				candidates.append(name)
		receipt_map = client_receipts_bulk(binding, sender, source_type, candidates) if candidates else {}
		for row in rows:
			name = row.dispatch if source_type == 'Bot Dispatch' else row.name
			row['client_receipt'] = receipt_map.get(name)
	# Incoming content already exists before a worker allocates a session. Only
	# project persisted receipt states; HTTP acknowledgement is not completion.
	incoming = tuple(row.name for row in result['messages'] if row.kind == 'Incoming')
	if incoming:
		states = {row.name: row for row in frappe.db.sql("""SELECT m.name,t.inbox_state,t.status,t.inbox_diagnostic
			FROM `tabChat Message` m JOIN `tabChat Turn` t ON t.name=m.turn AND t.contact=m.contact
			JOIN `tabChat Contact` c ON c.name=m.contact WHERE m.name IN %(messages)s
			AND c.channel=%(channel)s AND c.account=%(account)s AND c.external_sender=%(sender)s""",
			dict(messages=incoming, channel=CHANNEL, account=binding.name, sender=sender), as_dict=True)}
		for row in result['messages']:
			if row.name in states:
				state = states[row.name]
				row['processing'] = dict(state=state.inbox_state, status=state.status, diagnostic=state.inbox_diagnostic)
	result['enabled'] = bool(get_provider_enabled(PROVIDER) and binding.enabled
		and frappe.db.get_value('Communication Channel', CHANNEL, 'enabled')
		and contact.enabled)
	return result


@frappe.whitelist(methods=['POST'])
def send_message(account, sender, client_id, text='', dispatch=None, selection_id=None):
	require_manager()
	_doc, binding = account_scope(account, operating=True)
	sender_identity(sender, operating=True)
	result = dispatch_ingress(PROVIDER, account, dict(action='receive', sender=sender,
		client_id=client_id, text=text, dispatch=dispatch, selection_id=selection_id))
	if isinstance(result, dict) and result.get('turn'):
		msg = frappe.db.get_value(
			'Chat Message',
			{'turn': result['turn'], 'kind': 'Incoming'},
			['name', 'session', 'turn', 'sequence', 'kind', 'content', 'agent', 'sent_at', 'dispatch', 'selection_id'],
			as_dict=True,
		)
		turn_state = frappe.db.get_value(
			'Chat Turn',
			result['turn'],
			['inbox_state', 'status', 'inbox_diagnostic'],
			as_dict=True,
		)
		if msg:
			msg['origin_label'] = origin_label(msg.kind)
			msg['timeline_at'] = _timeline_at(msg.sent_at)
			msg['options'] = []
			if turn_state:
				msg['processing'] = dict(
					state=turn_state.inbox_state,
					status=turn_state.status,
					diagnostic=turn_state.inbox_diagnostic,
				)
			result['message'] = msg
	return result


@frappe.whitelist(methods=['POST'])
def set_pinned(account, sender, pinned):
	"""Toggle per-user pinned state for one emulator conversation."""
	require_manager()
	_doc, binding = account_scope(account)
	sender_identity(sender)
	doc = save_pinned_preference(frappe.session.user, CHANNEL, binding.name, sender, pinned)
	frappe.db.commit()
	return _preference_payload(binding.name, sender, doc)


@frappe.whitelist(methods=['POST'])
def set_archived(account, sender, archived):
	"""Toggle per-user archived presentation state without altering messaging state."""
	require_manager()
	_doc, binding = account_scope(account)
	sender_identity(sender)
	doc = save_archived_preference(frappe.session.user, CHANNEL, binding.name, sender, archived)
	frappe.db.commit()
	return _preference_payload(binding.name, sender, doc)


@frappe.whitelist(methods=['POST'])
def clear_chat(account, sender):
	"""Advance per-user visible history cutoff without deleting source records."""
	require_manager()
	_doc, binding = account_scope(account)
	sender_identity(sender)
	doc = clear_visible_history(frappe.session.user, CHANNEL, binding.name, sender)
	frappe.db.commit()
	return _preference_payload(binding.name, sender, doc)


@frappe.whitelist(methods=['POST'])
def simulate_status(account, sender, dispatch, status):
	require_manager()
	return dispatch_ingress(PROVIDER, account, dict(action='report_status', sender=sender,
		name=dispatch, source_type='Bot Dispatch', value=status))


@frappe.whitelist(methods=['POST'])
def simulate_result(account, sender, dispatch, outcome):
	require_manager()
	return dispatch_ingress(PROVIDER, account, dict(action='report_result', sender=sender,
		name=dispatch, source_type='Bot Dispatch', value=outcome))


@frappe.whitelist(methods=['POST'])
def simulate_notification(account, sender, notification, status=None, outcome=None):
	require_manager()
	if bool(status) == bool(outcome):
		frappe.throw(_("Select exactly one transport outcome or delivery status."))
	return dispatch_ingress(PROVIDER, account, dict(action='report_status' if status else 'report_result',
		sender=sender, name=notification, source_type='Notification Queue', value=status or outcome))


@frappe.whitelist(methods=['POST'])
def client_status(account, sender, source_type, name, receipt, status):
	"""Client reports share the canonical lock and authenticated transport boundary."""
	_doc, binding = account_scope(account, operating=True)
	sender_identity(sender, operating=True)
	from messaging.common.locking import contact_lock
	from messaging.common.primitives import identity_key
	with contact_lock(identity_key(CHANNEL, binding.name, sender)):
		return dispatch_ingress(PROVIDER, binding.name, dict(action='client_status', sender=sender,
			name=name, source_type=source_type, value=status, receipt=receipt))


@frappe.whitelist(methods=['POST'])
def observe_message(account, sender, source_type, name, receipt):
	"""Start a bounded observation only after the client has confirmed receipt."""
	_doc, binding = account_scope(account, operating=True)
	sender_identity(sender, operating=True)
	from messaging.common.locking import contact_lock
	from messaging.common.primitives import identity_key
	with contact_lock(identity_key(CHANNEL, binding.name, sender)):
		# Loaded older pages may outlive their receipt token. A fresh Read observation
		# still requires current Delivered evidence and gets its own short deadline.
		validate_client_receipt(binding.name, sender, source_type, name, receipt, 'Delivered', max_age=None)
		return dict(receipt=client_receipt(binding.name, sender, source_type, name, 'Read'))

def _timeline_at(value, *, local=False, tz=None):
	stamp = frappe.utils.get_datetime(value).replace(tzinfo=(tz or ZoneInfo(frappe.utils.get_system_timezone())) if local else timezone.utc)
	return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _serialize_preference(pref):
	pref = pref or {}
	return dict(
		is_pinned=int(bool(pref.get('is_pinned'))),
		pinned_at=_timeline_at(pref['pinned_at']) if pref.get('pinned_at') else None,
		is_archived=int(bool(pref.get('is_archived'))),
		archived_at=_timeline_at(pref['archived_at']) if pref.get('archived_at') else None,
		cleared_before=_timeline_at(pref['cleared_before']) if pref.get('cleared_before') else None,
	)


def _preference_payload(account, sender, doc):
	return dict(account=account, sender=sender, **_serialize_preference(doc.as_dict()))


def contact_activity(channel, account, catalog, *, conversations_only=False, user=None):
	"""Project visible activity for one account or an authorized collection of accounts."""
	accounts = (account,) if isinstance(account, str) else tuple(account)
	params = dict(accounts=accounts, channel=channel)
	# The caller authorizes the account and supplies its visible sender catalog.
	# At most 500 latest rows per source, never the full message histories. A sender
	# and account pair in the combined top 500 is in one source's top 500.
	params['senders'] = tuple(row.sender_id for row in catalog)
	if not catalog or not accounts:
		return []
	prefs = preference_map(user or frappe.session.user, channel, accounts, params['senders'])
	messages = frappe.db.sql("""SELECT c.account,c.external_sender AS sender_id,m.name AS activity_name,
		m.sent_at AS activity_at,LEFT(m.content,250) AS preview,m.kind AS activity_kind
		FROM `tabChat Contact` c
		JOIN `tabChat Message` m ON m.name=(SELECT latest.name FROM `tabChat Message` latest
			WHERE latest.contact=c.name AND latest.kind IN ('Incoming','Automatic','Human')
			ORDER BY latest.sent_at DESC,latest.name DESC LIMIT 1)
		WHERE c.channel=%(channel)s AND c.account IN %(accounts)s AND c.external_sender IN %(senders)s
		ORDER BY m.sent_at DESC,m.name DESC LIMIT 500""", params, as_dict=True)
	notifications = frappe.db.sql("""SELECT n.account,n.effective_recipient AS sender_id,n.name AS activity_name,
		n.handoff_at AS activity_at,LEFT(COALESCE(NULLIF(n.rendered_content,''),NULLIF(n.prepared_text,''),n.template_reference),250) AS preview
		FROM `tabNotification Queue` n
		WHERE n.channel=%(channel)s AND n.account IN %(accounts)s AND n.effective_recipient IN %(senders)s
		AND n.name=(SELECT latest.name FROM `tabNotification Queue` latest
			WHERE latest.channel=%(channel)s AND latest.account=n.account AND latest.effective_recipient=n.effective_recipient
			AND latest.handoff_at IS NOT NULL AND latest.status IN ('Processing','Accepted','Rejected','Unknown')
			ORDER BY latest.handoff_at DESC,latest.name DESC LIMIT 1)
		ORDER BY n.handoff_at DESC,n.name DESC LIMIT 500""", params, as_dict=True)
	contacts = {row.sender_id: row for row in catalog}
	rows = {} if conversations_only else {
		(account_name, row.sender_id): dict(
			name=row.name,
			account=account_name,
			sender_id=row.sender_id,
			display_name=row.display_name,
			enabled=row.get('enabled', 1),
			last_activity=None,
			preview='',
			activity_kind=None,
		)
		for account_name in accounts for row in catalog
	}
	for (acc, sid), pref in prefs.items():
		if acc in accounts and sid in contacts and (acc, sid) not in rows and (pref.is_pinned or pref.is_archived or pref.cleared_before):
			contact = contacts[sid]
			rows[(acc, sid)] = dict(
				name=contact.name,
				account=acc,
				sender_id=sid,
				display_name=contact.display_name,
				enabled=contact.get('enabled', 1),
				last_activity=None,
				preview='',
				activity_kind=None,
			)
	site_tz = ZoneInfo(frappe.utils.get_system_timezone())
	for source, activities in [('message', messages), ('notification', notifications)]:
		for activity in activities:
			key = (activity.account, activity.sender_id)
			pref = prefs.get(key)
			cleared_iso = _timeline_at(pref.cleared_before) if pref and pref.cleared_before else None
			stamp = _timeline_at(activity.activity_at, local=source == 'notification', tz=site_tz)
			if key not in rows:
				contact = contacts[activity.sender_id]
				rows[key] = dict(
					name=contact.name,
					account=activity.account,
					sender_id=activity.sender_id,
					display_name=contact.display_name,
					enabled=contact.get('enabled', 1),
					last_activity=None,
					preview='',
					activity_kind=None,
				)
			if cleared_iso and stamp <= cleared_iso:
				continue
			activity_key = (stamp, source, activity.activity_name)
			previous = rows.get(key, {})
			if activity_key > previous.get('_activity_key', ('', '', '')):
				rows[key].update(
					last_activity=stamp,
					preview=activity.preview or '',
					activity_kind='Notification' if source == 'notification' else activity.activity_kind,
					_activity_key=activity_key,
				)
	for key, row in rows.items():
		row.update(_serialize_preference(prefs.get(key)))
	ordered = sorted(rows.values(), key=lambda row: (row['display_name'].casefold(), row['name'], row['account']))
	ordered.sort(key=lambda row: row.get('_activity_key', ('', '', '')), reverse=True)
	ordered.sort(key=lambda row: (1 if row['is_pinned'] else 0, row['pinned_at'] or ''), reverse=True)
	for row in ordered:
		row.pop('_activity_key', None)
	return ordered[:500]


def message_history(channel, account, sender, session=None, before=None, notification_before=None, message_before=None, *, user=None):
	"""Private read projection; the caller must authorize the account and sender."""
	pref = get_preference(user or frappe.session.user, channel, account, sender)
	cleared_before_utc = frappe.utils.get_datetime(pref.cleared_before) if pref.get('cleared_before') else None
	contact = frappe.db.get_value('Chat Contact', {'channel': channel, 'account': account, 'external_sender': sender}, 'name')
	sessions = frappe.get_all('Chat Session', filters={'contact': contact}, fields=['name', 'status', 'modified'],
		order_by='creation desc, name desc', limit_page_length=100) if contact else []
	if session and session not in [row.name for row in sessions]:
		frappe.throw(_("The conversation does not match this emulator sender."), frappe.PermissionError)
	selected = session or (sessions[0].name if sessions else None)
	message_params = dict(contact=contact)
	message_filter = ''
	if cleared_before_utc:
		message_filter += ' AND sent_at > %(cleared_before_utc)s'
		message_params['cleared_before_utc'] = cleared_before_utc
	if session or before:
		message_filter += ' AND session=%(session)s'
		message_params['session'] = selected
	if before:
		# A sequence cursor is local to the requested or latest episode.
		message_filter += ' AND sequence < %(before)s'
		message_params['before'] = frappe.utils.cint(before)
	if message_before:
		previous = frappe.db.get_value('Chat Message', {'name': message_before, 'contact': contact}, ['sent_at', 'session'], as_dict=True) if contact else None
		if not previous or (session and previous.session != session):
			frappe.throw(_("The message does not match this emulator sender."), frappe.PermissionError)
		message_params.update(before_at=previous.sent_at, before_name=message_before)
		message_filter += ' AND (sent_at < %(before_at)s OR (sent_at = %(before_at)s AND name < %(before_name)s))'
	messages = frappe.db.sql("""SELECT name,session,turn,sequence,kind,content,agent,sent_at,dispatch,selection_id
		FROM `tabChat Message` WHERE contact=%(contact)s AND kind IN ('Incoming','Automatic','Human')""" + message_filter +
		' ORDER BY sent_at DESC,name DESC LIMIT 61', message_params, as_dict=True) if contact else []
	more_messages = len(messages) > 60
	messages = messages[:60]
	dispatch_names = tuple({row.dispatch for row in messages if row.dispatch})
	dispatches = {
		d.name: d
		for d in frappe.get_all(
			'Bot Dispatch',
			filters={'name': ('in', dispatch_names)},
			fields=['name', 'status', 'delivery_status', 'transport_reference', 'diagnostic_code', 'menu_options'],
			limit_page_length=len(dispatch_names),
		)
	} if dispatch_names else {}
	menu_labels = None
	for row in messages:
		row['origin_label'] = origin_label(row.kind)
		row['timeline_at'] = _timeline_at(row.sent_at)
		dispatch_row = dispatches.get(row.dispatch) if row.dispatch else None
		row['transport'] = frappe._dict(
			name=dispatch_row.name,
			status=dispatch_row.status,
			delivery_status=dispatch_row.delivery_status,
			transport_reference=dispatch_row.transport_reference,
			diagnostic_code=dispatch_row.diagnostic_code,
		) if dispatch_row else None
		row['options'] = []
		if dispatch_row:
			options = json.loads(dispatch_row.menu_options or '{}')
			if options and dispatch_row.status == 'Accepted':
				if menu_labels is None:
					from messaging.chatbot.doctype.bot_settings.bot_settings import get_menu_flows
					menu_labels = {r['workflow_code']: r['label'] for r in get_menu_flows()}
				row['options'] = [dict(id=key, label=menu_labels[code]) for key, code in options.items() if code in menu_labels]
	# Notification history has its own bounded cursor; no synthetic Chat Message or Chat Turn.
	params = dict(account=account, channel=channel, effective_recipient=sender)
	cursor = ''
	if cleared_before_utc:
		cursor += ' AND handoff_at > %(cleared_before_site)s'
		params['cleared_before_site'] = utc_to_site_naive(cleared_before_utc)
	if notification_before:
		previous = frappe.db.get_value('Notification Queue', {'name': notification_before,
			'account': account, 'channel': channel, 'effective_recipient': sender}, ['handoff_at', 'name'], as_dict=True)
		if not previous or not previous.handoff_at:
			frappe.throw(_("The notification does not match this emulator sender."), frappe.PermissionError)
		params.update(before_at=previous.handoff_at, before_name=previous.name)
		cursor += ' AND (handoff_at < %(before_at)s OR (handoff_at = %(before_at)s AND name < %(before_name)s))'
	notifications = frappe.db.sql('''SELECT name,creation,handoff_at,account,recipient,effective_recipient,emulator_contact,content_type,template_reference,
		variables_json,prepared_text,rendered_content,status,delivery_status,transport_reference,diagnostic_code,last_attempt_at
		FROM `tabNotification Queue` WHERE account=%(account)s AND channel=%(channel)s AND effective_recipient=%(effective_recipient)s
		AND handoff_at IS NOT NULL AND status IN ('Processing','Accepted','Rejected','Unknown')''' + cursor +
		' ORDER BY handoff_at DESC, name DESC LIMIT 60', params, as_dict=True)
	site_tz = ZoneInfo(frappe.utils.get_system_timezone()) if notifications else None
	for row in notifications:
		row['timeline_at'] = _timeline_at(row.handoff_at, local=True, tz=site_tz)

	return dict(sessions=sessions, session=selected, preference=_serialize_preference(pref),
		more_messages=more_messages, messages=list(reversed(messages)), notifications=list(reversed(notifications)), more_notifications=len(notifications) == 60)
