"""Channel account binding resolution and provider derivation."""
import frappe
from frappe import _

from messaging.communication.doctype.communication_provider.communication_provider import (
	is_available,
	operation,
	provider_definition,
	require_provider_enabled,
)
from messaging.communication.doctype.communication_provider_value.values import validate_values
from messaging.security.execution_context import require_authenticated_adapter
from messaging.security.service_identity import require_messaging_service_user


def account_provider(account):
	channel = frappe.db.get_value('Communication Channel Account', account, 'channel')
	return frappe.db.get_value('Communication Channel', channel, 'provider') if channel else None


def resolve_ingress_binding(provider, account):
	"""Read-only pre-authentication configuration; no callbacks, identity change or secret access."""
	definition = provider_definition(provider)
	require_provider_enabled(provider)
	binding = frappe.get_doc('Communication Channel Account', account)
	channel = frappe.get_doc('Communication Channel', binding.channel)
	if channel.provider != provider or channel.status != 'Enabled' or not binding.enabled:
		frappe.throw(_("Invalid or disabled channel account."), frappe.PermissionError)
	# Validation may fill defaults in memory, but never saves documents.
	validate_values(channel, operational=True)
	validate_values(binding, operational=True)
	return definition, binding


def resolve_binding(provider, account, *, incoming=False, check_adapter=True):
	definition, binding = resolve_ingress_binding(provider, account)
	require_messaging_service_user()
	if incoming:
		require_authenticated_adapter(provider, account)
	usable = False
	try:
		usable = is_available(definition) and (not check_adapter or operation(definition, 'resolve_account', binding) is True)
	except Exception:
		pass
	if not usable:
		frappe.throw(_("The provider account is unavailable."))
	return definition, binding
