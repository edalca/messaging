"""Outbound Meta payloads and customer service window policy."""
from datetime import timedelta
import json
import re

import frappe
from frappe import _
import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat

from messaging.common.primitives import utcnow
from messaging.whatsapp.api import GraphError
from messaging.whatsapp.configuration import PROVIDER, client
from messaging.whatsapp.templates import prepare_provider_template

DESTINATION = re.compile(r'[1-9][0-9]{0,14}')
E164_INPUT = re.compile(r'\+[0-9\s\-()]+')
E164_CANONICAL = re.compile(r'\+[1-9][0-9]{6,14}')
MESSAGE_ID = re.compile(r'wamid\.[a-zA-Z0-9_+/=.-]{1,134}')


def destination(value):
	return value.removeprefix('+') if isinstance(value, str) else ''


def normalize_e164_phone(value):
	"""Validate and normalize an explicit international E.164 phone number."""
	error = _('Enter a valid international phone number in E.164 format (for example, +50499999999).')
	if not isinstance(value, str):
		frappe.throw(error)
	text = value.strip()
	if not text or len(text) > 32 or not E164_INPUT.fullmatch(text):
		frappe.throw(error)
	if text.count('(') != text.count(')'):
		frappe.throw(error)
	try:
		parsed = phonenumbers.parse(text, None)
	except NumberParseException:
		frappe.throw(error)
	if (
		parsed.extension
		or not phonenumbers.is_possible_number(parsed)
		or not phonenumbers.is_valid_number(parsed)
	):
		frappe.throw(error)
	canonical = phonenumbers.format_number(parsed, PhoneNumberFormat.E164)
	if not E164_CANONICAL.fullmatch(canonical):
		frappe.throw(error)
	return canonical


def notification_destination(rule, account, recipient=None):
	"""Normalize the selected business or configured test phone number."""
	if frappe.db.get_value('Communication Channel', account.channel, 'provider') != PROVIDER:
		frappe.throw(_('The channel account does not belong to WhatsApp.'))
	if recipient is not None:
		# Accept the international digits also used by WhatsApp's inbound sender identity.
		if DESTINATION.fullmatch(recipient):
			return dict(effective_recipient=destination(normalize_e164_phone('+' + recipient)), emulator_contact=None)
		return dict(effective_recipient=normalize_e164_phone(recipient), emulator_contact=None)
	row = next((r for r in rule.parameters if r.parameter_key == 'notification_phone_number'), None)
	raw = json.loads(row.value) if row and row.value else None
	canonical = normalize_e164_phone(raw)
	encoded = json.dumps(canonical, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
	if row and row.value != encoded:
		row.value = encoded
	return dict(effective_recipient=canonical, emulator_contact=None)


def window_open(channel, account, sender):
	contact = frappe.db.get_value('Chat Contact', dict(channel=channel, account=account, external_sender=sender), 'name')
	if not contact:
		return False
	stamp = frappe.db.get_value('Chat Turn', {'contact': contact, 'event_at': ['<=', utcnow()]},
		'event_at', order_by='event_at desc')
	return bool(stamp and utcnow() - timedelta(hours=24) < stamp)


def _notification_template(envelope):
	if envelope.get('output_mode') != 'Provider Template':
		raise ValueError('Invalid WhatsApp notification content')
	to = destination(envelope.get('destination'))
	if not DESTINATION.fullmatch(to) or normalize_e164_phone('+' + to) != '+' + to:
		raise ValueError('Invalid WhatsApp notification recipient')
	template = prepare_provider_template(envelope)
	if 'provider_template_parameters' in envelope and envelope['provider_template_parameters'] != template:
		raise ValueError('WhatsApp notification parameters changed after preparation')
	return template


def validate_notification(envelope):
	try:
		_notification_template(envelope)
		return True
	except (KeyError, ValueError, frappe.ValidationError):
		return False


def conversation_payload(envelope):
	content, options = envelope['content'], envelope['interactive']
	if not content or len(content) > 4096:
		raise ValueError('Invalid WhatsApp text length')
	if not options or len(options) > 10 or len(content) > 1024:
		# The native menu already includes its numbered text.
		return dict(type='text', text=dict(body=content))
	if len(options) <= 3:
		action = dict(buttons=[dict(type='reply', reply=dict(id=row['id'], title=row['label'][:20])) for row in options])
		kind = 'button'
	else:
		action = dict(button=_('Options'), sections=[dict(rows=[
			dict(id=row['id'], title=row['label'][:24]) for row in options])])
		kind = 'list'
	titles = [row['label'][:20 if kind == 'button' else 24] for row in options]
	if not all(title.strip() for title in titles) or len(set(titles)) != len(titles):
		return dict(type='text', text=dict(body=content))
	return dict(type='interactive', interactive=dict(type=kind, body=dict(text=content), action=action))


def send(envelope):
	from messaging.communication.doctype.communication_provider.communication_provider import active_adapter
	account = envelope['account']
	if (active_adapter.get() != (PROVIDER, account)
		or frappe.db.get_value('Communication Channel Account', account, 'channel') != envelope['channel']):
		raise frappe.PermissionError
	to = destination(envelope['destination'])
	if not DESTINATION.fullmatch(to):
		return dict(outcome='Rejected')
	notification = envelope.get('source_type') == 'Notification Queue'
	try:
		if notification:
			# Recheck the exact persisted revision after the dispatch checkpoint.
			from messaging.communication.doctype.communication_template.status import notification_template_gate
			if notification_template_gate(envelope['template_reference'], account,
				expected_revision=envelope['template_revision']):
				return dict(outcome='Rejected')
			payload = dict(type='template', template=_notification_template(envelope))
		else:
			if not window_open(envelope['channel'], account, to):
				return dict(outcome='Rejected')
			payload = conversation_payload(envelope)
		payload.update(to=to, biz_opaque_callback_data=('n:' if notification else 'b:') + envelope['outbound_id'])
		response = client(account).send(payload)
	except GraphError as error:
		return dict(outcome='Rejected' if error.rejected else 'Unknown')
	except (KeyError, ValueError, frappe.ValidationError):
		return dict(outcome='Rejected')
	messages = response.get('messages')
	if isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], dict):
		reference = messages[0].get('id')
		if isinstance(reference, str) and MESSAGE_ID.fullmatch(reference):
			return dict(outcome='Accepted', reference=reference)
	return dict(outcome='Unknown')


def mark_incoming_read(payload):
	reference = payload['reference']
	if not MESSAGE_ID.fullmatch(reference):
		return
	try:
		client(payload['account']).send(dict(status='read', message_id=reference))
	except GraphError:
		frappe.logger('messaging').warning('WhatsApp read acknowledgement failed.')
