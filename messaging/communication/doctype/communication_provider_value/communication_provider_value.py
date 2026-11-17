"""Typed JSON scalars and encrypted child secrets, owned by a channel, account, template or notification rule."""
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.password import get_decrypted_password

from messaging.common.configuration import EncryptedConfigurationValue, MASK, normalize, prepare_secret, validate_value_owner
from messaging.communication.doctype.communication_channel_account.binding import resolve_binding
from messaging.communication.doctype.communication_provider.communication_provider import active_adapter, provider_definition
from messaging.communication.doctype.communication_provider_value.values import (
	OWNERS,
	OWNER_SCOPES,
	_authentication_parameters,
	cleanup_values,
	definitions,
	validate_values,
	value_fingerprint,
)


class CommunicationProviderValue(EncryptedConfigurationValue, Document):
	def validate(self):
		frappe.throw(_("Save provider values through their owning configuration."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Save provider values through their owning configuration."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Provider value ownership is immutable."), frappe.PermissionError)


@frappe.whitelist()
def parameter_schema(doctype, provider=None, channel=None, account=None):
	if doctype not in OWNERS:
		frappe.throw(_("Invalid provider value owner."))
	frappe.has_permission(doctype, 'read', throw=True)
	if doctype == 'Notification Rule':
		account_doc = frappe.get_doc('Communication Channel Account', account)
		account_doc.check_permission('read')
		channel = account_doc.channel
	if doctype in ('Communication Template', 'Communication Channel Account', 'Notification Rule'):
		channel_doc = frappe.get_doc('Communication Channel', channel)
		channel_doc.check_permission('read')
		provider = channel_doc.provider
	catalog = frappe.get_doc('Communication Provider', provider)
	catalog.check_permission('read')
	scope = OWNER_SCOPES[doctype]
	# No executable paths, values or decrypted secrets are exposed.
	definition = provider_definition(provider)
	parameters = [{k: r.get(k) for k in ('parameter_key', 'label', 'field_type', 'required', 'options', 'default_value', 'sort_order', 'description')} for r in catalog.parameters if r.scope == scope]
	for parameter in parameters:
		if scope == 'Notification Rule' and parameter['field_type'] == 'Link' and frappe.get_meta(parameter['options']).has_field('enabled'):
			parameter['filters'] = {'enabled': 1}
	return dict(revision=catalog.definition_hash, automatic_account_id='prepare_account_id' in definition['operations'], parameters=parameters)


def transport_parameters(provider, account, *, template=None):
	"""Private adapter API. Only authenticated ingress or registered callbacks decrypt values."""
	active = active_adapter.get() == (provider, account)
	_definition, binding = resolve_binding(provider, account, incoming=not active, check_adapter=False)
	channel = frappe.get_doc('Communication Channel', binding.channel)
	result = {}
	documents = [('Channel', channel), ('Account', binding)]
	if template is not None:
		if not active:
			frappe.throw(_("Template parameters require a registered adapter operation."), frappe.PermissionError)
		doc = frappe.get_doc('Communication Template', template)
		if doc.status != 'Active' or doc.channel != binding.channel:
			frappe.throw(_("The template channel does not match the channel account."), frappe.PermissionError)
		validate_values(doc, operational=True)
		documents.append(('Template', doc))
	for scope, doc in documents:
		validate_values(doc, operational=True)
		parameters = definitions(doc)
		values = {}
		for row in doc.parameters:
			values[row.parameter_key] = get_decrypted_password(row.doctype, row.name, 'secret_value', raise_exception=False) if parameters[row.parameter_key]['field_type'] == 'Password' else json.loads(row.value)
		result[scope] = values
	return result
