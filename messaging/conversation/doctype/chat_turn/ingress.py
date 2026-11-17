"""Private authenticated channel input owned by the conversational turn inbox."""
import json
import frappe
from frappe import _
from messaging.communication.doctype.communication_channel_account.binding import resolve_binding
from messaging.conversation.doctype.chat_contact.chat_contact import require_operator


def receive(provider, account, event):
	"""Persist an authenticated input; workers own all conversational execution."""
	require_operator()
	definition, binding = resolve_binding(provider, account, incoming=True)
	from messaging.conversation.doctype.chat_turn.inbox import accept_message
	return accept_message(normalize_event(definition, binding, event))


def normalize_event(definition, binding, event):
	"""Normalize without changing identity or documents; authorization belongs to the caller."""
	if not isinstance(event, dict) or set(event) - {'channel', 'sender', 'event_id', 'event_at', 'message_type', 'text', 'interactive', 'reply_reference'}:
		frappe.throw(_("Invalid channel event."))
	kind = event.get('message_type')
	if event.get('channel') != binding.channel or kind not in ('text', 'interactive', 'unsupported') or (kind == 'interactive' and 'interactive' not in definition['capabilities']):
		frappe.throw(_("Invalid channel event."))
	request = dict(channel=binding.channel, account=binding.name, sender=event.get('sender'),
		event_id=event.get('event_id'), event_at=event.get('event_at'), text=event.get('text', ''),
		source_type=kind, external_reference=event.get('event_id'),
		reply_reference=event.get('reply_reference', ''), unsupported=kind == 'unsupported' and not bool(event.get('text', '').strip()))
	if kind == 'interactive':
		selection = event.get('interactive')
		if not isinstance(selection, dict) or set(selection) != {'dispatch', 'selection_id'} or any(not isinstance(v, str) or not v or len(v) > 140 for v in selection.values()):
			frappe.throw(_("Invalid channel event."))
		doc = frappe.get_doc('Bot Dispatch', selection['dispatch'])
		options = json.loads(doc.menu_options or '{}')
		request.update(menu_dispatch=doc.name, menu_selection=options.get(selection['selection_id'], ''),
			selection_id=selection['selection_id'], unsupported=selection['selection_id'] not in options)
	elif event.get('interactive') is not None:
		frappe.throw(_("Invalid channel event."))
	return request
