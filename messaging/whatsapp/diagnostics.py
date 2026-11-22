"""Account-scoped evidence from Meta lookups and authenticated webhook receipts."""

import hmac
import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from messaging.whatsapp.api import PAGE_SIZE, PHONE_FIELDS, GraphError, numeric_id
from messaging.whatsapp.configuration import VERIFY_TOKEN, callback_url, graph_version, secret, value

OPERATIONS = ('connection', 'subscription', 'webhook')
MAX_STORED_APPS = 100


def configuration_key(account, channel, operation):
	"""Identify the saved configuration without storing credentials or exposing them to Desk."""
	key = frappe.conf.get('encryption_key')
	if not key:
		return None
	binding = [account.name, str(account.creation), account.account_id, value(account, 'waba_id'),
		channel.name, str(channel.creation), value(channel, 'app_id')]
	if operation == 'webhook':
		binding.extend([callback_url(channel.name), secret(channel, 'app_secret'), secret(channel, VERIFY_TOKEN)])
	else:
		binding.extend([graph_version(channel), secret(account, 'access_token')])
	return hmac.digest(key.encode(), json.dumps([operation, binding]).encode(), 'sha256').hex()


def _stored(account):
	evidence = account.last_diagnostics
	if isinstance(evidence, str):
		evidence = json.loads(evidence)
	return evidence if evidence and evidence.get('version') == 1 else dict(version=1, results={}, bindings={})


def record(account, operation, diagnostic, binding, updates=None):
	"""Merge one normalized result while the account row is locked."""
	evidence = _stored(account)
	previous = evidence['results'].get(operation)
	if not previous or previous['checked_at'] <= diagnostic['checked_at']:
		evidence['results'][operation] = diagnostic
		evidence['bindings'][operation] = binding
		changes = {'last_diagnostics': json.dumps(evidence), **(updates or {})}
		frappe.db.set_value(account.doctype, account.name, changes, update_modified=bool(updates))
		account.update(changes)
	return evidence['results'][operation], evidence['bindings'][operation] != binding


def record_webhook_receipts(channel, receipts):
	"""Record routed signed messages against their original configuration."""
	if not receipts:
		return
	accounts = [frappe.get_doc('Communication Channel Account', name, for_update=True) for name in sorted(receipts)]
	current_channel = frappe.get_doc(channel.doctype, channel.name, for_update=True)
	if (current_channel.status != 'Enabled' or current_channel.provider != channel.provider
		or (current_channel.creation, current_channel.modified) != (channel.creation, channel.modified)):
		return
	for account in accounts:
		checked_at, binding, original_revision = receipts[account.name]
		if (account.channel != channel.name or not account.enabled
			or (account.creation, account.modified) != original_revision
			or configuration_key(account, current_channel, 'webhook') != binding):
			continue
		result = dict(result='webhook_received', checked_at=checked_at, interpretation='signed_receipt',
			request=dict(operation='webhook', method='POST', endpoint=callback_url(channel.name),
				parameters={'channel': channel.name}, technical_id=account.account_id),
			response=dict(http_status=200, data={}, meta_code=None, meta_subcode=None, trace_id=None))
		record(account, 'webhook', result, binding)


def read(account, channel, received_at=None):
	evidence = _stored(account)
	results = {}
	for operation in OPERATIONS:
		if operation in evidence['results']:
			binding = evidence['bindings'].get(operation)
			results[operation] = present(evidence['results'][operation],
				not binding or binding != configuration_key(account, channel, operation))
	if received_at and 'webhook' not in results:
		# Existing Chat Turns attest to receipt, but do not bind it to today's configuration.
		results['webhook'] = present(dict(result='webhook_received', checked_at=str(received_at),
			interpretation='signed_receipt', request=dict(operation='webhook', method='POST'),
			response=dict(data={})), stale=True)
	return results


def present(diagnostic, stale=False):
	request = diagnostic['request']
	operation = request['operation']
	label = {
		'connection': _('WhatsApp phone number lookup'),
		'subscription': _('WhatsApp Business subscriptions lookup'),
		'webhook': _('Signed webhook receipt'),
	}[operation]
	return {**diagnostic, 'stale': stale, 'request': {**request, 'operation': label},
		'interpretation': _interpretation(diagnostic, label)}


def _interpretation(diagnostic, operation):
	kind = diagnostic['interpretation']
	response = diagnostic['response']
	if kind == 'phone_confirmed':
		return _('Meta confirmed access to the configured WhatsApp phone number.')
	if kind == 'app_subscribed':
		return _('Meta listed the configured app among the subscriptions for this Messaging Account.')
	if kind == 'app_absent':
		return _('Meta returned the complete subscription list without the configured app.')
	if kind == 'app_unconfigured':
		return _('Set the Meta App ID on the channel to determine its subscription status.')
	if kind == 'signed_receipt':
		return _('A previous signed webhook was routed to this account.')
	if kind == 'transport_error':
		return _('Could not reach Meta while running {0}.').format(operation)
	if kind == 'identity_mismatch':
		return _('Meta returned a different Phone Number ID.')
	# Meta can echo request values in free-form errors. Use local descriptions.
	code, subcode = response['meta_code'], response['meta_subcode']
	if code is None:
		if response['http_status'] in (401, 403):
			return _('Meta denied access to this operation.')
		return _('Meta returned an unexpected response while running {0}.').format(operation)
	if code == 100 and subcode == 33:
		detail = _('The resource is unavailable, the operation is unsupported, or the token lacks access.')
	elif code == 100:
		detail = _('Meta did not accept a parameter or the requested resource does not support this operation.')
	elif code in (102, 190):
		detail = _('Meta did not accept the access token. It may be invalid, expired, or revoked.')
	elif code == 10 or 200 <= code < 300:
		detail = _('The token does not have the permissions required for this operation.')
	else:
		detail = _('Meta reported an error for this operation.')
	if subcode is not None:
		return _('{0} failed (Meta code {1}, subcode {2}). {3}').format(operation, code, subcode, detail)
	return _('{0} failed (Meta code {1}). {2}').format(operation, code, detail)


def graph_diagnostic(graph, channel, operation):
	"""Execute an authorized GET and allowlist diagnostics for persistence."""
	business = operation == 'subscription'
	node = graph.business if business else graph.phone
	parameters = {'limit': PAGE_SIZE} if business else {'fields': PHONE_FIELDS}
	request = dict(operation=operation, method='GET',
		endpoint=graph.endpoint(node, 'subscribed_apps' if business else ''),
		parameters=parameters, technical_id=node, graph_version=graph.version)
	error_code = error_subcode = None
	try:
		if business:
			app_id = value(channel, 'app_id')
			apps = graph.subscription_app_ids()
			data = dict(app_ids=apps[:MAX_STORED_APPS], app_count=len(apps))
			if not numeric_id(app_id):
				result, interpretation = 'inconclusive', 'app_unconfigured'
			elif app_id in apps:
				result, interpretation = 'confirmed', 'app_subscribed'
			else:
				result, interpretation = 'not_subscribed', 'app_absent'
		else:
			data = graph.phone_info()
			result, interpretation = 'confirmed', 'phone_confirmed'
	except GraphError as error:
		data = {}
		error_code, error_subcode = error.code, error.subcode
		interpretation = 'identity_mismatch' if error.identity_mismatch else 'graph_error'
		if error.transport:
			result = 'inconclusive' if business else 'network_error'
			interpretation = 'transport_error'
		elif graph.http_status in (400, 401, 403, 404) or error.rejected or (not business and error.code is not None):
			result = 'meta_rejected'
		elif graph.http_status == 200 and not business:
			result = 'failed'
		else:
			result = 'inconclusive'
	return dict(result=result, interpretation=interpretation, checked_at=str(now_datetime()), request=request,
		response=dict(http_status=graph.http_status, data=data, meta_code=error_code,
			meta_subcode=error_subcode, trace_id=graph.trace_id))
