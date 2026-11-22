"""Explicit administrator operations for configured Meta accounts and channels."""

import secrets

import frappe
from frappe import _
from frappe.query_builder import Order
from frappe.utils import convert_utc_to_system_timezone, cstr

from messaging.common.locking import document_savepoint
from messaging.communication.doctype.communication_channel import webhook_evidence
from messaging.communication.doctype.communication_provider.communication_provider import (
	is_available, provider_definition,
)
from messaging.communication.doctype.communication_provider_value.values import validate_values
from messaging.security.permissions import require_system_manager
from messaging.whatsapp import diagnostics
from messaging.whatsapp.api import Graph, GraphError, numeric_id, version_valid
from messaging.whatsapp.configuration import (
	PROVIDER, VERIFY_TOKEN, callback_url, graph_version, secret, value, verify_token_row, webhook_token_write,
)


def _webhook_channel(channel, *, for_update=False, permission='write'):
	require_system_manager()
	doc = frappe.get_doc('Communication Channel', channel, for_update=for_update)
	doc.check_permission(permission)
	if doc.provider != PROVIDER:
		frappe.throw(_('Select a WhatsApp channel.'))
	return doc


@frappe.whitelist(methods=['GET'])
def get_webhook_url(channel):
	"""Return the saved callback URL and private receipt summary for Desk."""
	doc = _webhook_channel(channel, permission='read')
	return dict(url=callback_url(doc.name), evidence=webhook_evidence.read(doc))


@frappe.whitelist(methods=['POST'])
def copy_webhook_token(channel):
	"""Reveal only the selected channel's verification token to its administrator."""
	token = secret(_webhook_channel(channel), VERIFY_TOKEN)
	if not token:
		frappe.throw(_('Generate a webhook verify token first.'))
	return dict(token=token)


@frappe.whitelist(methods=['POST'])
def generate_webhook_token(channel, expected_modified):
	"""Generate the first token for a saved channel."""
	_write_webhook_token(channel, expected_modified, replace=False)


@frappe.whitelist(methods=['POST'])
def regenerate_webhook_token(channel, expected_modified):
	"""Replace an existing token after the administrator confirms."""
	_write_webhook_token(channel, expected_modified, replace=True)


def _write_webhook_token(channel, expected_modified, *, replace):
	doc = _webhook_channel(channel, for_update=True)
	if cstr(doc.modified) != expected_modified:
		frappe.throw(_('The channel changed. Reload it before changing the webhook verify token.'), frappe.TimestampMismatchError)
	row = verify_token_row(doc)
	exists = bool(row and row.secret_value)
	if exists and not replace:
		frappe.throw(_('A webhook verify token already exists.'))
	if replace and not exists:
		frappe.throw(_('Generate a webhook verify token first.'))
	with document_savepoint(), webhook_token_write():
		if not row:
			row = doc.append('parameters', dict(parameter_key=VERIFY_TOKEN))
		row.secret_value = secrets.token_urlsafe(32)
		doc.save()


def _account(account, permission, *, for_update=False):
	require_system_manager()
	if not isinstance(account, str) or not account or len(account) > 140:
		frappe.throw(_('Invalid WhatsApp account configuration.'))
	doc = frappe.get_doc('Communication Channel Account', account, for_update=for_update)
	doc.check_permission(permission)
	return doc


def _provider_revision(*, for_update=False):
	return frappe.db.get_value('Communication Provider', PROVIDER,
		['creation', 'modified', 'status', 'definition_hash'], as_dict=True, for_update=for_update)


def _account_revision(doc):
	return (doc.name, doc.creation, doc.modified)


def _require_account_revision(doc, expected_modified):
	if cstr(doc.modified) != expected_modified:
		_configuration_changed()


def _configuration_changed():
	frappe.throw(_('The WhatsApp configuration changed. Reload the account and retry.'), frappe.TimestampMismatchError)


def _require_operational(channel, catalog):
	if channel.status != 'Enabled':
		frappe.throw(_('The communication channel is disabled.'), frappe.PermissionError)
	if not catalog or catalog.status != 'Enabled':
		frappe.throw(_('The channel provider is disabled.'), frappe.PermissionError)
	if not is_available(provider_definition(PROVIDER)):
		frappe.throw(_('The WhatsApp provider is unavailable.'))


def _admin_client(account, expected_modified):
	doc = _account(account, 'write')
	_require_account_revision(doc, expected_modified)
	channel = _webhook_channel(doc.channel, permission='read')
	catalog = _provider_revision()
	_require_operational(channel, catalog)
	validate_values(channel, operational=True)
	validate_values(doc, operational=True)
	try:
		graph = Graph(doc.account_id, value(doc, 'waba_id'), secret(doc, 'access_token'), graph_version(channel))
	except GraphError:
		frappe.throw(_('Invalid WhatsApp account configuration.'))
	return doc, channel, catalog, graph


def _record_diagnostic(doc, channel, catalog, operation, diagnostic, binding, *, enable):
	try:
		# Lock only after the Graph request. Read the latest evidence before merging.
		current = _account(doc.name, 'write', for_update=True)
		current_channel = _webhook_channel(current.channel, for_update=True, permission='read')
		current_catalog = _provider_revision(for_update=True)
		if (_account_revision(current) != _account_revision(doc)
			or _account_revision(current_channel) != _account_revision(channel) or current_catalog != catalog
			or diagnostics.configuration_key(current, current_channel, operation) != binding):
			_configuration_changed()
		_require_operational(current_channel, current_catalog)
		updates = {}
		if operation == 'connection' and diagnostic['result'] == 'confirmed':
			number = diagnostic['response']['data']['display_phone_number']
			if current.whatsapp_display_phone_number != number:
				updates['whatsapp_display_phone_number'] = number
			if enable and not current.enabled:
				updates['enabled'] = 1
		saved, stale = diagnostics.record(current, operation, diagnostic, binding, updates)
		return dict(diagnostics.present(saved, stale), enabled=bool(current.enabled))
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		_configuration_changed()


@frappe.whitelist(methods=['POST'])
def account_action(account, action, expected_modified):
	if action not in ('check', 'enable', 'subscription'):
		frappe.throw(_('Invalid WhatsApp operation.'))
	doc, channel, catalog, graph = _admin_client(account, expected_modified)
	operation = 'subscription' if action == 'subscription' else 'connection'
	binding = diagnostics.configuration_key(doc, channel, operation)
	diagnostic = diagnostics.graph_diagnostic(graph, channel, operation)
	saved = _record_diagnostic(doc, channel, catalog, operation, diagnostic, binding, enable=action == 'enable')
	return dict(saved, action=action)


@frappe.whitelist(methods=['POST'])
def disable_account(account, expected_modified):
	try:
		doc = _account(account, 'write', for_update=True)
		_webhook_channel(doc.channel, permission='read')
		_require_account_revision(doc, expected_modified)
		if doc.enabled:
			frappe.db.set_value(doc.doctype, doc.name, 'enabled', 0)
		return dict(enabled=False)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		_configuration_changed()


@frappe.whitelist(methods=['GET'])
def account_diagnostics(account):
	"""Read local configuration and receipt evidence for one permitted account."""
	doc = _account(account, 'read')
	channel = _webhook_channel(doc.channel, permission='read')
	catalog = _provider_revision()
	url = callback_url(channel.name)
	turn = frappe.qb.DocType('Chat Turn')
	contact = frappe.qb.DocType('Chat Contact')
	receipts = (frappe.qb.from_(turn).join(contact).on(turn.contact == contact.name)
		.select(turn.received_at).where((contact.account == doc.name) & (contact.channel == channel.name)
			& turn.received_at.notnull()).orderby(turn.received_at, order=Order.desc).limit(1).run())
	received_at = convert_utc_to_system_timezone(receipts[0][0]) if receipts else None
	return dict(channel=channel.name, url=url, channel_enabled=channel.status == 'Enabled',
		provider_enabled=bool(catalog and catalog.status == 'Enabled'),
		configured=bool(url and numeric_id(value(channel, 'app_id')) and version_valid(graph_version(channel))
			and secret(channel, 'app_secret') and secret(channel, VERIFY_TOKEN)),
		received_at=received_at, results=diagnostics.read(doc, channel, received_at))
