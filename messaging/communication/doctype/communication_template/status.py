"""Persist provider template status without treating a failed reading as a disapproval."""

from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _
from frappe.utils import now_datetime

from messaging.registry.communication_providers import validate_template_status_result
from messaging.security.execution_context import automatic_job

_STATUS_WRITE = ContextVar('messaging_template_status_write', default=False)

NOT_APPLICABLE = 'not_applicable'
NOT_SYNCHRONIZED = 'not_synchronized'
MULTIPLE = 'multiple'
_AVAILABILITY = {True: 'Available', False: 'Unavailable', None: 'Unknown'}


def template_status_write_active():
	return bool(_STATUS_WRITE.get())


@contextmanager
def template_status_write():
	token = _STATUS_WRITE.set(True)
	try:
		yield
	finally:
		_STATUS_WRITE.reset(token)


def provider_status_summary(doc):
	"""Aggregate display. It is not the per-account send decision."""
	if not doc.provider:
		return NOT_APPLICABLE
	from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
	try:
		definition = provider_definition(doc.provider)
	except Exception:
		return NOT_APPLICABLE
	if 'template_status' not in definition.get('capabilities', []):
		return NOT_APPLICABLE
	reported = {row.provider_status for row in doc.get('statuses') or [] if row.provider_status}
	if not reported:
		return NOT_SYNCHRONIZED
	if len(reported) == 1:
		return next(iter(reported))
	return MULTIPLE


def protect_status_rows(doc):
	"""Ordinary saves cannot edit external status or change administrative status from it."""
	if template_status_write_active():
		return
	if doc.is_new():
		doc.set('statuses', [])
		return
	previous = doc.get_doc_before_save()
	doc.set('statuses', [row.as_dict() for row in (previous.get('statuses') or [] if previous else [])])


def _row_for(doc, scope_type, scope_key):
	for row in doc.statuses:
		if row.scope_type == scope_type and row.scope_key == scope_key:
			return row
	return None


def _reject_duplicate_scopes(doc):
	seen = set()
	for row in doc.statuses:
		identity = (row.scope_type, row.scope_key)
		if not row.scope_type or not row.scope_key or identity in seen:
			frappe.throw(_('Template status scopes must be unique.'))
		seen.add(identity)


def apply_template_status_result(doc, result, *, automatic=False):
	"""Store a normalized reading. Unknown and error keep the last valid status."""
	try:
		reading = validate_template_status_result(result)
	except frappe.ValidationError:
		reading = None
	if reading is None or reading['template'] != doc.name:
		_remember_failure(doc, doc.provider or 'provider', doc.provider or 'unknown', 'invalid_status_result')
	elif reading['outcome'] == 'reported':
		from messaging.communication.doctype.communication_template.communication_template import (
			template_content_revision,
		)
		row = _row_for(doc, reading['scope_type'], reading['scope_key'])
		if row is None:
			row = doc.append('statuses', {})
		row.scope_type = reading['scope_type']
		row.scope_key = reading['scope_key']
		row.provider_status = reading['provider_status']
		row.available = _AVAILABILITY[reading['available']]
		row.synced_at = now_datetime()
		row.sync_reference = reading['reference']
		row.template_revision = reading.get('template_revision') or template_content_revision(doc)
		row.last_error = None
		row.last_attempt_at = row.synced_at
	else:
		_remember_failure(doc, reading['scope_type'], reading['scope_key'], reading['error_code'])
	_reject_duplicate_scopes(doc)
	doc.provider_status = provider_status_summary(doc)
	with template_status_write():
		doc.save(ignore_permissions=automatic)
	return doc


def _remember_failure(doc, scope_type, scope_key, error_code):
	row = _row_for(doc, scope_type, scope_key)
	if row is None:
		row = doc.append('statuses', dict(scope_type=scope_type, scope_key=scope_key, available='Unknown'))
	row.last_error = error_code
	row.last_attempt_at = now_datetime()


def _definition_for(doc):
	from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
	if not doc.provider:
		frappe.throw(_('Select a communication provider before synchronizing template status.'))
	definition = provider_definition(doc.provider)
	if 'template_status' not in definition.get('capabilities', []) or 'query_template_status' not in definition.get('operations', {}):
		frappe.throw(_('This provider does not synchronize template status.'))
	return definition


def sync_template_status(doc, account=None, *, automatic=False):
	"""Ask the provider once and persist the normalized result. A failed call keeps the last valid status."""
	from messaging.communication.doctype.communication_provider.communication_provider import operation
	definition = _definition_for(doc)
	from .external import managed, refresh_template
	if managed(definition):
		if account and account != doc.account:
			frappe.throw(_('The template does not match the channel account.'))
		refresh_template(doc.name, automatic=automatic, channel=doc.channel, account=doc.account)
		return doc.reload()
	if account and doc.channel != frappe.db.get_value('Communication Channel Account', account, 'channel'):
		frappe.throw(_('The template channel does not match the channel account.'))
	try:
		result = operation(definition, 'query_template_status', doc, account)
	except Exception:
		frappe.log_error(title='template_status_query_failed', message=doc.name)
		result = dict(outcome='error', template=doc.name, scope_type='account' if account else 'provider',
			scope_key=account or doc.provider, error_code='sync_failed')
	return apply_template_status_result(doc, result, automatic=automatic)


def accept_template_status_update(provider, payload):
	"""Normalize an external provider event and persist it. No webhook route is registered here."""
	from messaging.communication.doctype.communication_provider.communication_provider import operation, provider_definition
	definition = provider_definition(provider)
	from .external import managed
	if managed(definition) or 'template_status' not in definition.get('capabilities', []) or 'apply_template_status_update' not in definition.get('operations', {}):
		frappe.throw(_('This provider does not accept template status updates.'))
	try:
		result = operation(definition, 'apply_template_status_update', payload)
		reading = validate_template_status_result(result)
	except Exception:
		frappe.throw(_('Invalid template status update.'))
	doc = frappe.get_doc('Communication Template', reading['template'])
	if doc.provider != provider:
		frappe.throw(_('The template provider does not match the channel account.'))
	return apply_template_status_result(doc, reading)


def template_send_block(template, account):
	"""Protect delivery for providers that require verified external evidence."""
	from .external import managed, provider_definition, published_block, requires_published

	try:
		definition = provider_definition(template.provider)
		if managed(definition):
			return published_block(template, definition, account)
		if requires_published(definition):
			return 'template_status_unknown'
	except Exception:
		return 'template_status_unknown'
	return None



def notification_template_gate(reference, account, variables=None, *, expected_revision=None, live=False):
	"""Re-check a queued notification template before handoff."""
	if not reference:
		return None
	if not frappe.db.exists('Communication Template', reference):
		return 'template_missing'
	template = frappe.get_doc('Communication Template', reference, for_update=live)
	if template.status != 'Active':
		return 'template_not_active'
	from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
	from messaging.communication.doctype.communication_provider_value.values import validate_values
	if template.channel != frappe.db.get_value('Communication Channel Account', account, 'channel'):
		return 'template_channel_mismatch'
	from .external import managed, published_block
	try:
		definition = provider_definition(template.provider)
		if managed(definition):
			return published_block(template, definition, account, expected_revision=expected_revision, live=live)
	except Exception:
		return 'template_status_unknown'
	try:
		validate_values(template, operational=True)
		if variables is not None and 'provider_templates' not in provider_definition(template.provider)['capabilities']:
			from messaging.communication.doctype.communication_template.communication_template import validate_template
			validate_template(template, account=account, variables=variables)
	except frappe.ValidationError:
		return 'template_configuration_invalid'
	return template_send_block(template, account)


@frappe.whitelist(methods=['POST'])
def synchronize_template_status(name, account=None):
	from messaging.security.permissions import require_system_manager
	require_system_manager()
	frappe.has_permission('Communication Template', 'write', throw=True)
	doc = frappe.get_doc('Communication Template', name)
	doc.check_permission('write')
	if account in ('', None):
		account = None
	elif not frappe.db.exists('Communication Channel Account', account):
		frappe.throw(_('Select an existing Communication Channel Account.'))
	if account:
		frappe.get_doc('Communication Channel Account', account).check_permission('read')
	from .external import managed
	if not managed(_definition_for(doc)):
		doc = frappe.get_doc('Communication Template', name, for_update=True)
	sync_template_status(doc, account)
	doc.reload()
	return dict(provider_status=doc.provider_status, statuses=[row.as_dict() for row in doc.statuses])


def schedule_template_refresh(definition, binding, payload):
	from messaging.security.execution_context import require_authenticated_adapter

	require_authenticated_adapter(definition['id'], binding.name)
	references = payload['templates']
	if ('template_status' not in definition['capabilities'] or not isinstance(references, list)
		or any(not isinstance(name, str) or not name or len(name) > 140 for name in references)):
		frappe.throw(_('Invalid template status update.'))
	for name in references:
		frappe.enqueue(refresh_account_template, enqueue_after_commit=True,
			name=name, provider=definition['id'], channel=binding.channel, account=binding.name)
	return len(references)


@automatic_job
def refresh_account_template(name, provider, channel, account):
	from messaging.communication.doctype.communication_channel_account.binding import resolve_binding

	definition, binding = resolve_binding(provider, account)
	from .external import managed, refresh_template
	if managed(definition):
		if frappe.db.exists('Communication Template', name):
			return refresh_template(name, automatic=True, provider=provider, channel=channel, account=account)
		return
	doc = frappe.get_doc('Communication Template', name, for_update=True)
	if doc.provider == provider and doc.channel == channel == binding.channel:
		sync_template_status(doc, account, automatic=True)
