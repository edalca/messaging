"""Canonical visible history. Turn diagnostics and evaluation logs never store this text."""
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.naming import make_autoname
from messaging.common.write_guards import InternalWriteGuard

_writing = InternalWriteGuard("messaging_chat_message_write")


def origin_label(kind):
	"""Administrative provenance only; never compose or alter delivered content."""
	return {
		'Incoming': _('Subscriber'),
		'Automatic': _('Bot'),
		'Human': _('Agent'),
		'Event': _('System'),
	}.get(kind, '')


class ChatMessage(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("MSG-.######")

	def before_validate(self):
		if not _writing.active():
			frappe.throw(_("Conversation messages can only be written by Messaging."), frappe.PermissionError)

	def validate(self):
		if not self.session and self.kind == 'Incoming' and self.turn and frappe.db.get_value(
			'Chat Turn', self.turn, ['contact', 'inbox_state']) == (self.contact, 'Pending'):
			return
		if frappe.db.get_value('Chat Session', self.session, 'contact') != self.contact:
			frappe.throw(_("The message does not belong to this contact."))

	def on_trash(self):
		frappe.throw(_("Conversation messages can only be written by Messaging."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Conversation messages can only be written by Messaging."), frappe.PermissionError)


def append_message(session, key, kind, content, *, agent=None, turn=None, dispatch=None, event_at=None, external_id=None, source_type=None, reply_reference=None, selection_id=None):
	"""Caller holds the contact lock. Sequence is the local, stable receipt order in UTC."""
	from messaging.common.primitives import utcnow

	name = frappe.db.get_value('Chat Message', {'message_key': key}, 'name')
	if name:
		return frappe.get_doc('Chat Message', name)
	session.message_sequence = max(session.message_sequence or 0, frappe.db.get_value('Chat Session', session.name, 'message_sequence') or 0) + 1
	if kind == 'Incoming':
		session.last_incoming_sequence = session.message_sequence
	doc = frappe.get_doc(dict(doctype='Chat Message', message_key=key, session=session.name,
		contact=session.contact, sequence=session.message_sequence, kind=kind, content=content,
		agent=agent, turn=turn, dispatch=dispatch, sent_at=utcnow(), event_at=event_at,
		external_id=external_id, source_type=source_type, reply_reference=reply_reference, selection_id=selection_id))
	with _writing():
		doc.insert(ignore_permissions=True)
	frappe.db.set_value('Chat Session', session.name, dict(message_sequence=session.message_sequence,
		last_incoming_sequence=session.last_incoming_sequence or 0), update_modified=False)
	return doc


def attach_incoming(message, session):
	"""Assign a persisted inbox message to its episode once, under the contact lock."""
	if message.session:
		return message
	session.message_sequence = max(session.message_sequence or 0,
		frappe.db.get_value('Chat Session', session.name, 'message_sequence') or 0) + 1
	session.last_incoming_sequence = session.message_sequence
	message.session, message.sequence = session.name, session.message_sequence
	with _writing():
		message.save(ignore_permissions=True)
	frappe.db.set_value('Chat Session', session.name, dict(message_sequence=session.message_sequence,
		last_incoming_sequence=session.last_incoming_sequence), update_modified=False)
	return message


def dispatch_content(dispatch):
	if dispatch.content:
		return dispatch.content  # Prepared dispatches retain content until durable message persistence.
	return frappe.db.get_value('Chat Message', dispatch.message, 'content') if dispatch.message else None


def persist_dispatch_content(dispatch):
	"""Move new outbound content once, preserving the existing transport and quota identity."""
	if not dispatch.content:
		return
	session = frappe.get_doc('Chat Session', dispatch.session)
	if dispatch.message:
		frappe.db.set_value('Chat Message', dispatch.message, 'content', dispatch.content)
	else:
		msg = append_message(session, dispatch.outbound_key, dispatch.message_type, dispatch.content,
			agent=dispatch.agent, turn=dispatch.turn, dispatch=dispatch.name)
		dispatch.message = msg.name
	dispatch.content = None


def on_doctype_update():
	frappe.db.add_index("Chat Message", ["contact", "kind", "event_at"], "source_window")
	frappe.db.add_unique('Chat Message', ['session', 'sequence'], 'session_message_sequence')
	frappe.db.add_index("Chat Message", ["contact", "sent_at", "name"], "contact_visible_history")
