"""Meta webhook verification and normalization.

Adapted from frappe/whatsapp. Copyright (c) 2026, Frappe Technologies Pvt. Ltd.
and contributors. See LICENSE.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
import json
import re
from urllib.parse import quote, urlencode, urlsplit

import frappe
from werkzeug.wrappers import Response

from messaging.communication.doctype.communication_channel_account.communication_channel_account import dispatch_ingress
from messaging.communication.doctype.communication_provider.communication_provider import require_provider_enabled
from messaging.whatsapp.api import numeric_id
from messaging.whatsapp.configuration import PROVIDER, secret, value
from messaging.whatsapp.provider import DESTINATION, MESSAGE_ID, destination

MAX_WEBHOOK = 3 * 1024 * 1024
WEBHOOK_PATH = '/api/method/messaging.whatsapp.webhook.handler'
DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?')
LOCAL_SUFFIXES = ('.localhost', '.local', '.internal', '.lan', '.home.arpa', '.test', '.invalid', '.example')
STAGES = {'sent': 'Sent', 'delivered': 'Delivered', 'read': 'Read', 'failed': 'Failed'}


@dataclass(frozen=True)
class Event:
	raw: bytes
	signature: str
	channel: str
	business: str
	phone: str
	kind: str
	data: dict


def callback_url(channel):
	"""Build the Meta callback from the site's configured public origin and the saved channel ID."""
	host_name = frappe.conf.host_name or frappe.conf.hostname or ''
	try:
		origin = urlsplit(host_name.strip())
		domain = (origin.hostname or '').rstrip('.').encode('idna').decode('ascii')
		port = origin.port
	except (ValueError, UnicodeError):
		return None
	if (origin.scheme != 'https' or not DOMAIN.fullmatch(domain) or domain.endswith(LOCAL_SUFFIXES)
		or origin.username is not None or origin.path not in ('', '/') or origin.query or origin.fragment):
		return None
	authority = f'{domain}:{port}' if port is not None else domain
	query = urlencode({'channel': channel}, quote_via=quote)
	return f'https://{authority}{WEBHOOK_PATH}?{query}'


def signed(raw, signature, app_secret):
	return bool(app_secret and re.fullmatch(r'sha256=[a-f0-9]{64}', signature or '')
		and hmac.compare_digest(signature, 'sha256=' + hmac.digest(app_secret.encode(), raw, 'sha256').hex()))


def events(raw, signature, channel):
	payload = json.loads(raw)
	if not isinstance(payload, dict) or payload.get('object') != 'whatsapp_business_account':
		raise ValueError
	for entry in objects(payload, 'entry', required=True):
		business = entry.get('id')
		if not numeric_id(business):
			raise ValueError
		for change in objects(entry, 'changes', required=True):
			field = change.get('field')
			if field not in ('messages', 'message_template_status_update'):
				continue
			data = change.get('value')
			if not isinstance(data, dict):
				raise ValueError
			if field == 'message_template_status_update':
				yield Event(raw, signature, channel, business, '', 'template', data)
				continue
			metadata = data.get('metadata')
			phone = metadata.get('phone_number_id') if isinstance(metadata, dict) else None
			if not numeric_id(phone):
				raise ValueError
			messages, statuses = objects(data, 'messages'), objects(data, 'statuses')
			if not messages and not statuses:
				yield Event(raw, signature, channel, business, phone, 'ignore', {})
			for key, rows in (('messages', messages), ('statuses', statuses)):
				for item in rows:
					yield Event(raw, signature, channel, business, phone, key, item)


def objects(parent, key, *, required=False):
	rows = parent.get(key, None if required else [])
	if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
		raise ValueError
	return rows


def matches(pattern, value):
	return isinstance(value, str) and pattern.fullmatch(value) is not None


def find_dispatch(binding, sender, reference):
	contact = frappe.db.get_value('Chat Contact',
		dict(account=binding.account, channel=binding.channel, external_sender=sender), 'name')
	if not contact or not matches(MESSAGE_ID, reference):
		return None
	return frappe.db.get_value('Bot Dispatch',
		dict(account=binding.account, channel=binding.channel, contact=contact,
			status='Accepted', transport_reference=reference), 'name')


def incoming(binding, item):
	sender, reference = item.get('from'), item.get('id')
	if not matches(DESTINATION, sender) or not matches(MESSAGE_ID, reference):
		raise ValueError
	stamp = datetime.fromtimestamp(int(item['timestamp']), timezone.utc)
	event = dict(channel=binding.channel, sender=sender, event_id=reference,
		event_at=stamp.isoformat(), message_type='unsupported', text='')
	context = item.get('context')
	reply = context.get('id') if isinstance(context, dict) else None
	if matches(MESSAGE_ID, reply):
		event['reply_reference'] = reply
	if item.get('type') == 'text':
		body = item['text']['body']
		if not isinstance(body, str) or not body.strip() or len(body) > 4096:
			raise ValueError
		event.update(message_type='text', text=body)
	elif item.get('type') == 'interactive':
		interactive = item.get('interactive')
		kind = interactive.get('type') if isinstance(interactive, dict) else None
		if kind in ('button_reply', 'list_reply'):
			selection = interactive.get(kind)
			key = selection.get('id') if isinstance(selection, dict) else None
			dispatch = find_dispatch(binding, sender, reply)
			if dispatch and isinstance(key, str) and re.fullmatch(r'[a-f0-9]{64}', key):
				event.update(message_type='interactive', interactive=dict(dispatch=dispatch, selection_id=key))
	return dict(action='receive', payload=dict(event=event))


def status_report(binding, item):
	stage = STAGES.get(item.get('status'))
	reference, sender = item.get('id'), item.get('recipient_id')
	if not stage or not matches(MESSAGE_ID, reference) or not matches(DESTINATION, sender):
		return dict(action='ignore', payload={})
	opaque = item.get('biz_opaque_callback_data')
	match = re.fullmatch(r'([bn]):([a-f0-9]{64})', opaque) if isinstance(opaque, str) else None
	for source, key, prefix in (('Bot Dispatch', 'outbound_key', 'b'), ('Notification Queue', 'occurrence_key', 'n')):
		if match and match[1] != prefix:
			continue
		filters = dict(account=binding.account, channel=binding.channel)
		filters.update({key: match[2]} if match else dict(transport_reference=reference))
		fields = ['name', 'status', 'transport_reference', 'contact' if prefix == 'b' else 'effective_recipient']
		doc = frappe.db.get_value(source, filters, fields, as_dict=True)
		if not doc or (doc.transport_reference and doc.transport_reference != reference):
			continue
		target = (frappe.db.get_value('Chat Contact', doc.contact, 'external_sender')
			if prefix == 'b' else doc.effective_recipient)
		if destination(target) != sender or doc.status not in ('Accepted', 'Sending', 'Processing', 'Unknown', 'Rejected'):
			continue
		return dict(action='report_status', payload=dict(dispatch_name=doc.name,
			source_type=source, status=stage, reference=reference))
	return dict(action='ignore', payload={})


def authenticate_ingress(binding, request, credentials):
	if (not isinstance(request, Event) or request.channel != binding.channel
		or request.business != credentials['Account']['waba_id']
		or (request.kind != 'template' and request.phone != binding.account_id)
		or not signed(request.raw, request.signature, credentials['Channel']['app_secret'])):
		raise ValueError
	if request.kind == 'messages':
		return incoming(binding, request.data)
	if request.kind == 'statuses':
		return status_report(binding, request.data)
	if request.kind == 'template':
		name, language = request.data.get('message_template_name'), request.data.get('message_template_language')
		if isinstance(name, str) and isinstance(language, str) and len(name) <= 512 and len(language) <= 12:
			from messaging.whatsapp.templates import affected_templates
			return dict(action='refresh_template_status', payload=dict(templates=affected_templates(binding.channel, binding.name, request.business, name, language)))
	return dict(action='ignore', payload={})


@frappe.whitelist(allow_guest=True, methods=['GET', 'POST'])
def handler():
	request = frappe.request
	names = request.args.getlist('channel')
	if len(names) != 1 or not names[0] or len(names[0]) > 140:
		return Response(status=403)
	channel_name = names[0]
	try:
		channel = frappe.get_doc('Communication Channel', channel_name)
	except frappe.DoesNotExistError:
		return Response(status=403)
	if channel.name != channel_name or channel.provider != PROVIDER or channel.status != 'Enabled':
		return Response(status=403)
	require_provider_enabled(PROVIDER)
	if request.method == 'GET':
		token, challenge = request.args.get('hub.verify_token'), request.args.get('hub.challenge')
		configured = secret(channel, 'verify_token')
		if (request.args.get('hub.mode') != 'subscribe' or not token or not configured
			or not hmac.compare_digest(token.encode(), configured.encode())
			or not challenge or len(challenge) > 256):
			return Response(status=403)
		return Response(challenge, mimetype='text/plain')
	if request.content_length is not None and request.content_length > MAX_WEBHOOK:
		return Response(status=413)
	raw = request.get_data()
	if len(raw) > MAX_WEBHOOK:
		return Response(status=413)
	signature = request.headers.get('X-Hub-Signature-256', '')
	if not signed(raw, signature, secret(channel, 'app_secret')):
		return Response(status=403)
	try:
		batch = list(events(raw, signature, channel.name))
		if len(batch) > 500:
			raise ValueError
	except (ValueError, RecursionError):
		return Response(status=400)
	accounts = {}
	routes = {}
	verified = []
	for event in batch:
		key = (event.business, event.phone)
		if key not in routes:
			if event.phone:
				name = frappe.db.get_value('Communication Channel Account',
					dict(channel=channel.name, account_id=event.phone, enabled=1), 'name')
				candidates = [frappe.get_doc('Communication Channel Account', name)] if name else []
			else:
				if not accounts:
					accounts = {name: frappe.get_doc('Communication Channel Account', name) for name in frappe.get_all(
						'Communication Channel Account', filters=dict(channel=channel.name, enabled=1), pluck='name')}
				candidates = accounts.values()
			routes[key] = [account.name for account in candidates if value(account, 'waba_id') == event.business]
		if not routes[key]:
			return Response(status=403)
		if event.kind != 'ignore':
			verified.append((event, routes[key]))
	# Receipt and delivery owners can restart the transaction under their locks.
	# Schedule template refreshes last so those restarts cannot erase enqueue callbacks.
	template_refreshes = set()
	for event, names in sorted(verified, key=lambda route: route[0].kind == 'template'):
		if event.kind != 'template':
			for name in names:
				dispatch_ingress(PROVIDER, name, event)
			continue
		key = (event.business, str(event.data.get('message_template_name')), str(event.data.get('message_template_language')))
		if key in template_refreshes:
			continue
		for name in names:
			if dispatch_ingress(PROVIDER, name, event):
				template_refreshes.add(key)
				break
	return Response('ok', mimetype='text/plain')
