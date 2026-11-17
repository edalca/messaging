"""Bot Dispatch handoff, interactive menu evidence and authenticated reports."""
import json
from contextlib import nullcontext
import frappe
from frappe import _
from messaging.communication.doctype.communication_provider.communication_provider import handoff, validate_result, advances_delivery, POSITIVE_DELIVERY
from messaging.communication.doctype.communication_channel_account.binding import resolve_binding, account_provider
from messaging.security.permissions import is_system_manager
from messaging.security.execution_context import _automation_context, is_automation
from messaging.common.locking import contact_lock
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_contact.chat_contact import require_operator


def valid_menu(request, contact, session):
	doc = frappe.get_doc('Bot Dispatch', request['menu_dispatch'])
	return (doc.channel == contact.channel and doc.account == contact.account and doc.contact == contact.name
		and doc.session == session.name and doc.status == 'Accepted' and session.status == 'Active'
		and json.loads(doc.menu_options or '{}').get(request.get('selection_id')) == request['menu_selection']
		and frappe.db.get_value('Bot Flow', request['menu_selection'], ['status', 'show_in_menu']) == ('Enabled', 1))


def _issue_menu(doc, definition):
	from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings, get_menu_flows
	if doc.message_type != 'Automatic' or doc.quota_kind == 'Human' or 'interactive' not in definition['capabilities']:
		return
	settings = get_bot_settings()
	# Mechanical menus are plain text and carry their correspondence in dialogue_state.
	decisions = json.loads(frappe.db.get_value("Chat Turn", doc.turn, "decisions") or "[]") if doc.turn else []
	if any(d.get("mode") == "Mechanical" for d in decisions):
		return
	if not any(d.get("kind") == "Main Menu" for d in decisions):
		return
	doc.menu_options = json.dumps({identity_key(doc.outbound_key, row['workflow_code']): row['workflow_code'] for row in get_menu_flows(settings)})


def deliver(dispatch_name):
	"""One durable handoff shared by automatic and human replies. Never resend Unknown."""
	from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import claim, envelope, reconcile, require_dispatch_operator
	doc = frappe.get_doc('Bot Dispatch', dispatch_name)
	require_dispatch_operator(doc)
	provider = account_provider(doc.account)
	definition, binding = resolve_binding(provider, doc.account)
	if doc.channel != binding.channel:
		frappe.throw(_("Invalid channel account."))
	if doc.message_type != 'Human' and not is_automation() and not is_system_manager():
		frappe.throw(_("Messaging operator permission is required."), frappe.PermissionError)
	with _automation_context() if doc.message_type != 'Human' else nullcontext():
		key = frappe.db.get_value('Chat Contact', doc.contact, 'identity_key')
		with contact_lock(key):
			# Recheck enabled binding after acquiring the contact lock.
			definition, binding = resolve_binding(provider, doc.account)
			doc.reload()
			if doc.status != 'Prepared':
				return envelope(doc.name)
			_issue_menu(doc, definition)
			doc.save(ignore_permissions=True)
			frappe.publish_realtime('messaging_conversation_update', dict(channel=doc.channel, account=doc.account, session=doc.session), after_commit=True)
			frappe.db.commit()
			payload = claim(doc.name)
			if payload is None:
				return envelope(doc.name)
			result = handoff(definition, payload)
			return reconcile(doc.name, result['outcome'], reference=result.get('reference'), delivery_status=result.get('delivery_status'))


def deliver_human_locked(doc):
	return deliver(doc.name)


def _dispatch(provider, account, dispatch_name):
	require_operator()
	definition, binding = resolve_binding(provider, account, incoming=True)
	doc = frappe.get_doc('Bot Dispatch', dispatch_name)
	if (doc.account, doc.channel) != (binding.name, binding.channel):
		frappe.throw(_("The dispatch does not match the channel account."), frappe.PermissionError)
	return definition, doc


def report_result(provider, account, dispatch_name, result):
	"""Explicit recovery of a durable Sending/Unknown handoff; never calls send."""
	_definition, doc = _dispatch(provider, account, dispatch_name)
	try:
		validate_result(result)
	except ValueError:
		frappe.throw(_("Invalid transport outcome."))
	from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import reconcile
	key = frappe.db.get_value('Chat Contact', doc.contact, 'identity_key')
	with contact_lock(key):
		_definition, doc = _dispatch(provider, account, dispatch_name)
		if doc.transport_reference and result.get('reference') and doc.transport_reference != result['reference']:
			frappe.throw(_("The transport reference does not match this dispatch."))
		return reconcile(dispatch_name, result['outcome'], reference=result.get('reference'), transport=True, delivery_status=result.get('delivery_status'))


def report_status(provider, account, dispatch_name, status, reference):
	"""Delivery does not reverse acceptance or refund quotas; progress is monotonic."""
	from messaging.common.primitives import utcnow
	from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import reconcile
	definition, doc = _dispatch(provider, account, dispatch_name)
	if 'delivery_status' not in definition['capabilities'] or status not in ('Sent', 'Delivered', 'Read', 'Failed') or not isinstance(reference, str) or not reference.strip() or len(reference) > 140:
		frappe.throw(_("Invalid delivery status."))
	key = frappe.db.get_value('Chat Contact', doc.contact, 'identity_key')
	with contact_lock(key):
		_dispatch(provider, account, dispatch_name)
		doc.reload()
		if doc.transport_reference and doc.transport_reference != reference:
			frappe.throw(_("The transport reference does not match this dispatch."))
		if doc.status in ('Sending', 'Unknown') and status in POSITIVE_DELIVERY:
			reconcile(doc.name, 'Accepted', reference=reference, transport=True)
			doc.reload()
		if doc.status == 'Rejected':
			return dict(condition='conflicting_definitive_result')
		if doc.status not in ('Sending', 'Unknown', 'Accepted'):
			frappe.throw(_("The message has not been claimed for dispatch."))
		if advances_delivery(doc.delivery_status, status):
			doc.transport_reference = reference
			doc.delivery_status = status
			if status == 'Delivered': doc.delivered_at = utcnow()
			if status == 'Read': doc.read_at = utcnow()
			if status == 'Failed': doc.diagnostic_code = 'provider_delivery_failed'
			doc.save(ignore_permissions=True)
			frappe.publish_realtime('messaging_conversation_update', dict(channel=doc.channel, account=doc.account, session=doc.session), after_commit=True)
			frappe.db.commit()
		return dict(dispatch=doc.name, acceptance=doc.status, delivery=doc.delivery_status)
