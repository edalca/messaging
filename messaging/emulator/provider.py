"""Local transport implementing the same contract as an installed channel application."""

from datetime import datetime, timezone
import re
import time
import hmac
import json

from frappe.utils.verified_command import get_signed_params

import frappe
from frappe import _

from messaging.communication.doctype.communication_provider_value.communication_provider_value import transport_parameters
from messaging.emulator.doctype.emulator_contact import emulator_contact as contacts

# Emulator Contact owns these definitions. The adapter re-exports them so the
# installed EMULR declaration and existing callers keep one import path.
PROVIDER = contacts.PROVIDER
CHANNEL = contacts.CHANNEL


def require_manager():
	return contacts.require_manager()


def generate_emulator_account_id():
	return contacts.generate_emulator_account_id()


def resolve_account(binding):
	return bool(binding and binding.doctype == 'Communication Channel Account' and binding.channel == CHANNEL and binding.enabled)


def reference(outbound_id):
	return 'emu-' + outbound_id


def send(envelope):
	if envelope.get('source_type') == 'Notification Queue':
		if not validate_notification(envelope):
			return dict(outcome='Rejected')
		doc = frappe.get_doc('Notification Queue', envelope['notification'])
		if not doc.handoff_at:
			frappe.throw(_("Invalid emulator transport envelope."), frappe.PermissionError)
		from messaging.communication.doctype.communication_channel_account.communication_channel_account import resolve_binding
		_definition, binding = resolve_binding(PROVIDER, envelope['account'])

		outcome = transport_parameters(PROVIDER, binding.name)['Account']['transport_outcome']
		return dict(outcome=outcome, reference=reference(envelope['outbound_id']),
			**(dict(delivery_status='Sent') if outcome == 'Accepted' else {}))
	from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import require_dispatch_operator, envelope as canonical_envelope
	doc = frappe.get_doc('Bot Dispatch', envelope.get('dispatch'))
	require_dispatch_operator(doc)
	if envelope != canonical_envelope(doc.name) or doc.status != 'Sending':
		frappe.throw(_("Invalid emulator transport envelope."), frappe.PermissionError)
	from messaging.communication.doctype.communication_channel_account.communication_channel_account import resolve_binding
	_definition, binding = resolve_binding(PROVIDER, envelope['account'])
	if envelope['provider'] != PROVIDER or envelope['channel'] != CHANNEL or envelope['account_id'] != binding.account_id:
		frappe.throw(_("Invalid emulator account."), frappe.PermissionError)
	if not frappe.db.exists('Emulator Contact', {'sender_id': envelope['destination'], 'enabled': 1}):
		return dict(outcome='Rejected')
	outcome = transport_parameters(PROVIDER, binding.name)['Account']['transport_outcome']
	return dict(outcome=outcome, reference=reference(envelope['outbound_id']),
		**(dict(delivery_status='Sent') if outcome == 'Accepted' else {}))


def available():
	return True


def validate_notification(envelope):
	"""Validate structured emulator content; template identifiers have no executable meaning."""
	from messaging.communication.doctype.communication_provider.communication_provider import active_adapter
	from messaging.communication.doctype.notification_queue.dispatch import envelope as canonical_envelope
	if active_adapter.get() != (PROVIDER, envelope.get('account')):
		frappe.throw(_("Invalid emulator transport envelope."), frappe.PermissionError)
	doc = frappe.get_doc('Notification Queue', envelope.get('notification'))
	if doc.status != 'Processing' or envelope != canonical_envelope(doc.name) or doc.provider != PROVIDER or doc.channel != CHANNEL:
		frappe.throw(_("Invalid emulator transport envelope."), frappe.PermissionError)
	from messaging.emulator.doctype.emulator_contact.emulator_contact import validate_sender
	validate_sender(doc.effective_recipient)

	return bool(frappe.db.exists('Emulator Contact', {'name': doc.emulator_contact, 'sender_id': doc.effective_recipient, 'enabled': 1})
		and doc.content_type in ('Text', 'Message', 'Template')
		and doc.prepared_text)


def prepare_account_id(account):
	if account.channel != CHANNEL:
		frappe.throw(_("Invalid emulator account."), frappe.PermissionError)
	return contacts.generate_emulator_account_id()


def notification_destination(rule, account, recipient=None):
	"""All notifications use an explicitly configured fictitious rule destination."""
	from messaging.emulator.doctype.emulator_contact.emulator_contact import validate_sender
	row = next((r for r in rule.parameters if r.parameter_key == 'notification_contact'), None)
	name = json.loads(row.value) if row and row.value else None
	contact = frappe.db.get_value('Emulator Contact', name, ['name', 'sender_id', 'enabled'], as_dict=True) if name else None
	if account.channel != CHANNEL or not contact or not contact.enabled:
		frappe.throw(_("Configure an enabled emulated notification contact on this notification rule."))
	validate_sender(contact.sender_id)
	return dict(effective_recipient=contact.sender_id, emulator_contact=contact.name)


def validate_template(template, variables=None):
	"""EMULR uses native Header, Body, and Footer components and requires a non-empty Body."""
	from messaging.communication.doctype.communication_template.communication_template import (
		render_template_components,
		validate_template_structure,
	)
	if not isinstance(template.body, str) or not template.body.strip():
		frappe.throw(_("Template Body is required for Channel Emulator templates."))
	validate_template_structure(template)
	if variables is not None:
		render_template_components(template, variables)
	return True


def authenticate_ingress(binding, request, credentials):
	"""EMULR authenticates a native manager session, scoped account, contact and action.

	There is no external webhook or shared secret for this local adapter. External
	providers must verify their own cryptographic protocol in this registered step.
	"""
	require_manager()
	if binding.provider != PROVIDER or binding.channel != CHANNEL:
		frappe.throw(_("Invalid emulator account."), frappe.PermissionError)
	frappe.get_doc('Communication Channel Account', binding.account).check_permission('read')
	if not isinstance(request, dict):
		frappe.throw(_("Invalid channel event."))
	from messaging.emulator.doctype.emulator_contact.emulator_contact import validate_sender
	sender = request.get('sender')
	validate_sender(sender)
	if not frappe.db.exists('Emulator Contact', {'sender_id': sender, 'enabled': 1}):
		frappe.throw(_("Invalid or disabled emulator contact."), frappe.PermissionError)
	action = request.get('action')
	if action == 'receive':
		if set(request) != {'action', 'sender', 'client_id', 'text', 'dispatch', 'selection_id'}:
			frappe.throw(_("Invalid channel event."))
		client_id, text = request['client_id'], request['text']
		if not isinstance(client_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,100}', client_id):
			frappe.throw(_("A stable emulator event ID is required."))
		if not isinstance(text, str) or len(text) > 16000 or (not request['dispatch'] and not text.strip()):
			frappe.throw(_("Enter a text message."))
		event = dict(channel=CHANNEL, sender=sender, event_id='emu-' + client_id,
			event_at=datetime.now(timezone.utc).isoformat(timespec='microseconds'), message_type='text', text=text)
		if request['dispatch'] or request['selection_id']:
			if not request['dispatch'] or not request['selection_id']:
				frappe.throw(_("Invalid channel event."))
			_scoped_dispatch(binding, sender, request['dispatch'])
			event.update(message_type='interactive', interactive=dict(dispatch=request['dispatch'], selection_id=request['selection_id']))
		return dict(action='receive', payload=dict(event=event))
	if action == 'client_status':
		if set(request) != {'action', 'sender', 'name', 'source_type', 'value', 'receipt'} or request['value'] not in ('Delivered', 'Read'):
			frappe.throw(_("Invalid client evidence."), frappe.PermissionError)
		validate_client_receipt(binding.account, sender, request['source_type'], request['name'], request['receipt'], request['value'])
		action = 'report_status'
	elif action not in ('report_result', 'report_status') or set(request) != {'action', 'sender', 'name', 'source_type', 'value'}:
		frappe.throw(_("Invalid channel event."))
	elif action == 'report_status' and request['value'] == 'Read':
		frappe.throw(_("Reading requires visible client evidence."), frappe.PermissionError)
	source_type = request['source_type']
	doc, outbound = _scoped_outbound(binding, sender, source_type, request['name'])
	payload = dict(dispatch_name=doc.name, source_type=source_type)
	if action == 'report_status':
		if request['value'] not in ('Sent', 'Delivered', 'Read', 'Failed'):
			frappe.throw(_("Invalid delivery status."))
		payload.update(status=request['value'], reference=reference(outbound))
	else:
		if request['value'] not in ('Accepted', 'Rejected', 'Unknown'):
			frappe.throw(_("Invalid transport outcome."))
		payload['result'] = dict(outcome=request['value'], reference=reference(outbound))
	return dict(action=action, payload=payload)


def _scoped_dispatch(binding, sender, name):
	doc = frappe.get_doc('Bot Dispatch', name)
	if (doc.account, doc.channel) != (binding.account, CHANNEL) or frappe.db.get_value('Chat Contact', doc.contact, 'external_sender') != sender:
		frappe.throw(_("The dispatch does not match this emulator sender."), frappe.PermissionError)
	return doc


def _scoped_outbound(binding, sender, source_type, name):
	if source_type == 'Bot Dispatch':
		doc = _scoped_dispatch(binding, sender, name)
		return doc, doc.outbound_key
	if source_type == 'Notification Queue':
		doc = frappe.get_doc('Notification Queue', name)
		if (doc.account, doc.channel, doc.provider, doc.effective_recipient) != (binding.account, CHANNEL, PROVIDER, sender):
			frappe.throw(_("The notification does not match this emulator sender."), frappe.PermissionError)
		return doc, doc.occurrence_key
	frappe.throw(_("Invalid outbound source."))


def _sign_client_receipt(account, account_id, sender, source_type, name, transport_reference, stage='Delivered'):
	payload = dict(
		account=account,
		account_id=account_id,
		sender=sender,
		source_type=source_type,
		name=name,
		reference=transport_reference,
		user=frappe.session.user,
		stage=stage,
		issued_at=time.time(),
	)
	return get_signed_params(json.dumps(payload, sort_keys=True, separators=(',', ':')))


def client_receipt(account, sender, source_type, name, stage='Delivered'):
	"""Sign scoped client evidence without changing delivery or conversation state."""
	require_manager()
	doc, outbound = _scoped_outbound(frappe._dict(account=account), sender, source_type, name)
	if (doc.status != 'Accepted' or doc.delivery_status == 'Failed'
		or (source_type == 'Notification Queue' and not doc.handoff_at)
		or (stage == 'Read' and doc.delivery_status not in ('Delivered', 'Read'))
		or doc.transport_reference != reference(outbound)):
		frappe.throw(_("The message has no confirmed client delivery evidence."), frappe.PermissionError)
	account_id = frappe.db.get_value('Communication Channel Account', account, 'account_id')
	return _sign_client_receipt(account, account_id, sender, source_type, name, doc.transport_reference, stage=stage)


def client_receipts_bulk(binding, sender, source_type, names, stage='Delivered'):
	"""Bulk-sign scoped client evidence for pre-authorized history rows without N+1 queries."""
	require_manager()
	names = tuple(dict.fromkeys(name for name in (names or ()) if name))
	if not names:
		return {}
	receipts = {}
	if source_type == 'Bot Dispatch':
		rows = frappe.get_all(
			'Bot Dispatch',
			filters={'name': ('in', names), 'account': binding.name, 'channel': CHANNEL},
			fields=['name', 'contact', 'status', 'delivery_status', 'transport_reference', 'outbound_key'],
			limit_page_length=len(names),
		)
		contact_names = tuple({row.contact for row in rows if row.contact})
		contact_senders = dict(
			frappe.get_all(
				'Chat Contact',
				filters={'name': ('in', contact_names)},
				fields=['name', 'external_sender'],
				as_list=True,
				limit_page_length=len(contact_names),
			)
		) if contact_names else {}
		for row in rows:
			if (
				contact_senders.get(row.contact) == sender
				and row.status == 'Accepted'
				and row.delivery_status != 'Failed'
				and (stage != 'Read' or row.delivery_status in ('Delivered', 'Read'))
				and row.transport_reference == reference(row.outbound_key)
			):
				receipts[row.name] = _sign_client_receipt(
					binding.name, binding.account_id, sender, source_type, row.name, row.transport_reference, stage=stage
				)
		return receipts
	if source_type == 'Notification Queue':
		rows = frappe.get_all(
			'Notification Queue',
			filters={
				'name': ('in', names),
				'account': binding.name,
				'channel': CHANNEL,
				'provider': PROVIDER,
				'effective_recipient': sender,
			},
			fields=['name', 'status', 'delivery_status', 'handoff_at', 'transport_reference', 'occurrence_key'],
			limit_page_length=len(names),
		)
		for row in rows:
			if (
				row.status == 'Accepted'
				and row.delivery_status != 'Failed'
				and row.handoff_at
				and (stage != 'Read' or row.delivery_status in ('Delivered', 'Read'))
				and row.transport_reference == reference(row.occurrence_key)
			):
				receipts[row.name] = _sign_client_receipt(
					binding.name, binding.account_id, sender, source_type, row.name, row.transport_reference, stage=stage
				)
		return receipts
	frappe.throw(_("Invalid outbound source."))


def validate_client_receipt(account, sender, source_type, name, receipt, stage, *, max_age=3600):
	"""Revalidate signed identity, confirmed acceptance and server-side observation time."""
	try:
		if not isinstance(receipt, str) or len(receipt) > 4000:
			raise ValueError
		message, _signature = receipt.rsplit('&_signature=', 1)
		if not hmac.compare_digest(receipt, get_signed_params(message)):
			raise ValueError
		proof = json.loads(message)
		expected = json.loads(client_receipt(account, sender, source_type, name, stage).rsplit('&_signature=', 1)[0])
		issued_at = proof.pop('issued_at')
		expected.pop('issued_at')
		age = time.time() - issued_at
		maximum = 30 if stage == 'Read' else max_age
		if proof != expected or age < 0 or (maximum is not None and age > maximum) or (stage == 'Read' and age < .7):
			raise ValueError
	except (TypeError, ValueError, KeyError, AttributeError):
		frappe.throw(_("Invalid or expired client evidence."), frappe.PermissionError)
