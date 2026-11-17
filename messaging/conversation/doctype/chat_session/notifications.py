"""Native Frappe Desk notifications for unassigned queued human attention requests."""
from collections import defaultdict
from urllib.parse import quote

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import escape_html

from messaging.conversation.doctype.chat_session.inbox import agent_capacity_status, get_account_public_id


def eligible_agent_recipients(channel, account):
	"""Return deduplicated enabled, available agents with Messaging Agent role and free capacity."""
	if not channel or not account:
		return []
	grants = frappe.get_all(
		'Chat Agent',
		filters={'channel': channel, 'account': account, 'enabled': 1, 'available': 1},
		fields=['user'],
		order_by='name asc',
	)
	seen_users = set()
	seen_emails = set()
	recipients = []
	for grant in grants:
		user = grant.user
		if not user or user in seen_users:
			continue
		seen_users.add(user)
		status = agent_capacity_status(channel, account, user)
		if not (
			status['has_role']
			and status['has_grant']
			and status['available']
			and status['occupied'] < (status['capacity'] or 1)
		):
			continue
		user_info = frappe.db.get_value(
			'User', user, ['enabled', 'email', 'language'], as_dict=True
		)
		if not user_info or not user_info.enabled or not user_info.email:
			continue
		email = user_info.email.strip()
		if not email or email in seen_emails:
			continue
		seen_emails.add(email)
		recipients.append({
			'user': user,
			'email': email,
			'language': user_info.language or getattr(frappe.local, 'lang', None) or 'en',
		})
	return recipients


def _safe_request_metadata(contact_name, channel, account):
	"""Read non-sensitive display labels for a queued human attention request."""
	contact_label = contact_name or ''
	if contact_name:
		contact_data = frappe.db.get_value(
			'Chat Contact', contact_name, ['external_sender', 'parameter_values'], as_dict=True
		)
		if contact_data:
			try:
				params = frappe.parse_json(contact_data.parameter_values or '{}') or {}
			except Exception:
				params = {}
			if not isinstance(params, dict):
				params = {}
			contact_label = (
				params.get('name')
				or params.get('nombre')
				or contact_data.external_sender
				or contact_name
			)

	channel_label = (
		frappe.db.get_value('Communication Channel', channel, 'channel_name')
		if channel
		else None
	) or channel or ''

	account_label = (
		frappe.db.get_value('Communication Channel Account', account, 'account_name')
		if account
		else None
	) or get_account_public_id(account) or account or ''

	return str(contact_label), str(channel_label), str(account_label)


def _localized_notification_content(contact_label, channel_label, account_label, *, lang):
	"""Build escaped, translated Desk notification title and description."""
	heading = _('New human support request', lang=lang)
	summary = _('{0} requests to speak with an agent.', lang=lang).format(contact_label)
	details = _(
		'Channel: {0}. Account: {1}. Status: Pending assignment.',
		lang=lang,
	).format(channel_label, account_label)
	channel_line = _('Channel: {0}.', lang=lang).format(channel_label)
	account_line = _('Account: {0}.', lang=lang).format(account_label)
	status_line = _('Status: Pending assignment.', lang=lang)

	title = (
		f'<b class="subject-title">{escape_html(heading)}</b>'
		f'<br>{escape_html(summary)}'
		f'<br>{escape_html(details)}'
	)
	description = (
		f'<p><strong>{escape_html(heading)}</strong></p>'
		f'<p>{escape_html(summary)}</p>'
		f'<p>{escape_html(channel_line)}<br>{escape_html(account_line)}<br>{escape_html(status_line)}</p>'
	)
	return title, description


def notify_human_request_queued(*, session_name, contact_name, channel, account, turn=None):
	"""Create native Frappe Desk notifications once a request enters Human Queued.

	Runs after commit so rolled-back transactions never notify, and swallows technical
	notification errors so an accepted queued session is never lost or reverted.
	"""
	try:
		if not session_name:
			return
		row = frappe.db.get_value(
			'Chat Session', session_name, ['status', 'assigned_agent'], as_dict=True
		)
		if not row or row.status != 'Human Queued' or row.assigned_agent:
			return

		recipients = eligible_agent_recipients(channel, account)
		if not recipients:
			return

		contact_label, channel_label, account_label = _safe_request_metadata(
			contact_name, channel, account
		)
		link = f'/desk/conversation-inbox?session={quote(session_name, safe="")}'
		session_user = getattr(getattr(frappe, 'session', None), 'user', None)
		from_user = session_user if session_user and session_user != 'Guest' else 'Administrator'

		grouped = defaultdict(list)
		for recipient in recipients:
			grouped[recipient['language']].append(recipient['email'])

		for lang, emails in grouped.items():
			title, description = _localized_notification_content(
				contact_label, channel_label, account_label, lang=lang
			)
			doc = {
				'type': 'Alert',
				'document_type': 'Chat Session',
				'document_name': session_name,
				'from_user': from_user,
				'link': link,
				'title': title,
				'description': description,
			}
			dedupe_on = ['document_type', 'document_name', 'type']
			if turn:
				doc['source_doctype'] = 'Chat Turn'
				doc['source_name'] = turn
				dedupe_on.extend(['source_doctype', 'source_name'])
			enqueue_create_notification(emails, doc, dedupe_on=dedupe_on)
		if frappe.in_test:
			frappe.db.commit()
	except Exception as exc:
		frappe.log_error(
			title='Human request notification failed',
			message=f'Session {session_name}: {type(exc).__name__}: {exc}',
		)
