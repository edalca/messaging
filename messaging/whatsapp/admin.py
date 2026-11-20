"""Explicit administrator operations for configured Meta accounts and channels."""

import secrets

import frappe
from frappe import _
from frappe.utils import cstr

from messaging.common.locking import document_savepoint
from messaging.communication.doctype.communication_channel_account.binding import resolve_ingress_binding
from messaging.security.permissions import require_system_manager
from messaging.whatsapp.api import Graph, GraphError, numeric_id
from messaging.whatsapp.configuration import (
	PROVIDER, VERIFY_TOKEN, graph_version, secret, valid_account, value, verify_token_row, webhook_token_write,
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
	"""Return the saved channel's callback URL for Desk."""
	from messaging.whatsapp.webhook import callback_url

	doc = _webhook_channel(channel, permission='read')
	return dict(url=callback_url(doc.name))


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


def _admin_client(account):
	require_system_manager()
	binding = frappe.get_doc('Communication Channel Account', account)
	binding.check_permission('write')
	channel = frappe.get_doc('Communication Channel', binding.channel)
	channel.check_permission('read')
	resolve_ingress_binding(PROVIDER, binding.name)
	if not valid_account(binding):
		frappe.throw(_('Invalid WhatsApp account configuration.'))
	try:
		return binding, Graph(binding.account_id, value(binding, 'waba_id'), secret(binding, 'access_token'),
			graph_version(channel))
	except GraphError:
		frappe.throw(_('Invalid WhatsApp account configuration.'))


@frappe.whitelist(methods=['POST'])
def account_action(account, action):
	_binding, graph = _admin_client(account)
	try:
		if action == 'check':
			return dict(connected=any(row.get('id') == graph.phone for row in graph.phone_info()))
		if action == 'subscription':
			return dict(apps=[row['whatsapp_business_api_data']['id'] for row in graph.subscriptions()
				if isinstance(row.get('whatsapp_business_api_data'), dict) and numeric_id(row['whatsapp_business_api_data'].get('id'))])
		if action in ('subscribe', 'unsubscribe'):
			return dict(success=graph.subscription('POST' if action == 'subscribe' else 'DELETE').get('success') is True)
	except GraphError:
		frappe.throw(_('Meta could not complete the WhatsApp operation.'))
	frappe.throw(_('Invalid WhatsApp operation.'))
