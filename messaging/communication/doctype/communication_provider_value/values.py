"""Shared validation, definitions, and fingerprinting for provider configuration values."""
import hashlib
import hmac
import json

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password, get_encryption_key, remove_encrypted_password

from messaging.common.configuration import normalize, prepare_secret, validate_value_owner
from messaging.communication.doctype.communication_provider.communication_provider import definition_hash

OWNER_SCOPES = {
	'Communication Channel': 'Channel',
	'Communication Channel Account': 'Account',
	'Notification Rule': 'Notification Rule',
	'Communication Template': 'Template',
}
OWNERS = tuple(OWNER_SCOPES)


def definitions(doc, *, for_update=False):
	if doc.doctype == 'Communication Template':
		provider = frappe.db.get_value('Communication Channel', doc.channel, 'provider') if doc.channel else None
		if not provider:
			return {}
	elif doc.doctype == 'Notification Rule':
		channel = frappe.db.get_value('Communication Channel Account', doc.account, 'channel')
		provider = frappe.db.get_value('Communication Channel', channel, 'provider')
	else:
		provider = doc.provider if doc.doctype == 'Communication Channel' else frappe.db.get_value('Communication Channel', doc.channel, 'provider')
	if not provider:
		frappe.throw(_("Invalid channel provider."))
	catalog = frappe.get_doc('Communication Provider', provider, for_update=for_update)
	scope = OWNER_SCOPES[doc.doctype]
	return {r.parameter_key: r.as_dict() for r in catalog.parameters if r.scope == scope}


def validate_values(doc, *, operational=False, check_required=None):
	parameters = definitions(doc, for_update=not operational)
	old = doc.get_doc_before_save()
	old_rows = {r.name: r for r in old.parameters} if old else {}
	seen = set()
	for row in doc.parameters:
		parameter = parameters.get(row.parameter_key)
		if not parameter or row.parameter_key in seen:
			frappe.throw(_("Invalid or duplicate provider parameter."))
		seen.add(row.parameter_key)
		stored = validate_value_owner(row, doc)
		if parameter['field_type'] == 'Password':
			prepare_secret(row, old_rows, stored)
			if row.secret_value and not row.is_dummy_password(row.secret_value):
				normalize(parameter, row.secret_value)
		else:
			if row.secret_value:
				frappe.throw(_("Invalid provider parameter value."))
			try:
				raw = json.loads(row.value) if row.value else None
			except (ValueError, TypeError):
				frappe.throw(_("Invalid provider parameter value."))
			value = normalize(parameter, raw)
			row.value = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
	# Defaults are supplied only for missing keys; explicit null never resets to default.
	for key, p in parameters.items():
		if key not in seen and p['default_value']:
			doc.append('parameters', dict(parameter_key=key, value=json.dumps(normalize(p, p['default_value']))))
			seen.add(key)
	if check_required is None:
		active_status = {'Communication Channel': 'Enabled', 'Communication Template': 'Active', 'Notification Rule': 'Enabled'}.get(doc.doctype)
		check_required = operational or (doc.status == active_status if active_status else doc.get('enabled'))
	if check_required:
		rows = {r.parameter_key: r for r in doc.parameters}
		for key, p in parameters.items():
			row = rows.get(key)
			present = bool(row and row.secret_value) if p['field_type'] == 'Password' else row is not None and json.loads(row.value) is not None
			if p['required'] and not present:
				frappe.throw(_("Complete required provider parameters before enabling or operating."))


def configuration_compatible(doc):
	"""Use the operational value contract without saving or replacing credentials."""
	try:
		validate_values(doc, operational=True)
		return True
	except (frappe.ValidationError, ValueError, TypeError):
		return False


def incompatible_configurations(provider):
	"""Report affected bindings while retaining their administrative enablement."""
	channels = frappe.get_all('Communication Channel', filters={'provider': provider}, pluck='name')
	invalid_channels = [name for name in channels
		if not configuration_compatible(frappe.get_doc('Communication Channel', name))]
	accounts = frappe.get_all('Communication Channel Account', filters={'channel': ['in', channels]}, fields=['name', 'channel']) if channels else []
	return dict(incompatible_channels=invalid_channels,
		incompatible_accounts=[row.name for row in accounts if row.channel in invalid_channels
			or not configuration_compatible(frappe.get_doc('Communication Channel Account', row.name))])


def cleanup_values(doc, *, deleting=False):
	old = doc.get_doc_before_save()
	previous = old.parameters if old else []
	keep = set() if deleting else {r.name for r in doc.parameters}
	for row in (doc.parameters if deleting else previous):
		if row.name not in keep:
			remove_encrypted_password('Communication Provider Value', row.name, 'secret_value')


def _authentication_parameters(definition, binding):
	"""Dispatcher-only read: exactly the registered verification keys for one enabled binding."""
	channel = frappe.get_doc('Communication Channel', binding.channel)
	result = {}
	for scope, doc in (('Channel', channel), ('Account', binding)):
		validate_values(doc, operational=True)
		keys = definition.get('ingress_parameters', {}).get(scope, [])
		parameters = definitions(doc)
		rows = {row.parameter_key: row for row in doc.parameters}
		values = {}
		for key in keys:
			row = rows.get(key)
			if not row:
				values[key] = None
			elif parameters[key]['field_type'] == 'Password':
				values[key] = get_decrypted_password(row.doctype, row.name, 'secret_value', raise_exception=False)
			else:
				values[key] = json.loads(row.value) if row.value else None
		result[scope] = values
	return result


def value_fingerprint(doc):
	"""Stable owner configuration hash; secrets use the existing keyed digest policy."""
	parameters = []
	for row in doc.get('parameters') or []:
		secret = row.secret_value
		if secret and row.is_dummy_password(secret):
			secret = get_decrypted_password(row.doctype, row.name, 'secret_value', raise_exception=False)
		parameters.append(dict(
			parameter_key=row.parameter_key,
			value=row.value,
			secret_hash=hmac.new(get_encryption_key().encode(), secret.encode(), hashlib.sha256).hexdigest() if secret else None,
		))
	return definition_hash(sorted(parameters, key=lambda row: row['parameter_key']))
