"""Code-owned provider catalog and explicit lifecycle synchronization."""
from contextvars import ContextVar
from hashlib import sha256
import json

import frappe
from frappe import _
from frappe.model.document import Document

from .parameters import PARAMETER_FIELDS, validate_parameter_schema
from messaging.common.write_guards import InternalWriteGuard
from messaging.security.permissions import require_provider_function, require_registration_owner, installed_applications

catalog_sync = InternalWriteGuard('messaging_provider_sync')


def validate_parameters(parameters):
    """Add site-dependent Link checks to the authoritative declaration schema."""
    result = validate_parameter_schema(parameters)
    from messaging.common.configuration import normalize
    for parameter in result:
        if parameter['field_type'] == 'Link':
            if not frappe.db.exists('DocType', parameter['options']):
                frappe.throw(_("Invalid provider parameter options."))
            if parameter['default_value']:
                normalize(parameter, parameter['default_value'])
    return result


def definition_hash(definition):
	return sha256(json.dumps(definition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class CommunicationProvider(Document):
	def before_validate(self):
		if not catalog_sync.active():
			frappe.throw(_("Provider definitions are managed by the owning application."), frappe.PermissionError)

	def validate(self):
		old = self.get_doc_before_save()
		if old and (self.provider_id != old.provider_id or self.application != old.application):
			frappe.throw(_("Provider identity is immutable."))
		if old and old.fixed_channel_id and self.fixed_channel_id != old.fixed_channel_id:
			frappe.throw(_("Configured provider channels require an explicit compatible migration."))
		provider_name = self.name or self.provider_id
		if provider_name and int(self.single_channel or 0) and frappe.db.count('Communication Channel', {'provider': provider_name}) > 1:
			frappe.throw(_("Configured provider channels require an explicit compatible migration."))
		validate_parameters([{k: row.get(k) for k in PARAMETER_FIELDS} for row in self.parameters])

	def on_trash(self):
		if not catalog_sync.active():
			frappe.throw(_("Provider definitions are managed by the owning application."), frappe.PermissionError)
		if frappe.db.exists('Communication Channel', {'provider': self.name}):
			frappe.throw(_("A provider with configured channels cannot be deleted."))

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Provider identity is immutable."))


def _sync_provider_catalog(definitions, *, tolerate_errors=False):
	"""Single persistence path; lock and rollback protect each complete catalogue."""
	savepoint = 'providers_' + frappe.generate_hash(length=12)
	frappe.db.savepoint(savepoint)
	try:
		from messaging.common.locking import document_savepoint
		result = dict(created=0, updated=0, unchanged=0, rejected=[])
		for name, definition in definitions.items():
			try:
				with document_savepoint():
					action = _write_provider_catalog({name: definition})
			except (frappe.ValidationError, frappe.PermissionError):
				if not tolerate_errors:
					raise
				result['rejected'].append(name)
				continue
			result[action] += 1
		return result
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


def _write_provider_catalog(definitions):
	for definition in definitions.values():
		validate_parameters(definition['parameters'])
	frappe.db.get_value('DocType', 'Communication Provider', 'name', for_update=True)
	with catalog_sync():
		for definition in definitions.values():
			name = definition['id']
			doc = frappe.get_doc('Communication Provider', name, for_update=True) if frappe.db.exists('Communication Provider', name) else frappe.new_doc('Communication Provider')
			if not doc.is_new() and doc.application != definition['app']:
				frappe.throw(_("Provider identity is immutable."))
			if definition['single_channel'] and frappe.db.count('Communication Channel', {'provider': name}) > 1:
				frappe.throw(_("Configured provider channels require an explicit compatible migration."))
			fixed = definition.get('fixed_channel_id')
			if not doc.is_new() and doc.fixed_channel_id and doc.fixed_channel_id != fixed:
				frappe.throw(_("Configured provider channels require an explicit compatible migration."))
			if fixed:
				owner = frappe.db.get_value('Communication Channel', fixed, 'provider')
				if owner and owner != name:
					frappe.throw(_("The fixed channel ID belongs to another provider."))
			fingerprint = definition_hash(definition)
			action = 'created' if doc.is_new() else 'updated' if doc.definition_hash != fingerprint else 'unchanged'
			if doc.definition_hash != fingerprint:
				doc.update(dict(provider_id=name, provider_name=definition['name'], application=definition['app'], version=definition['version'],
					single_channel=int(definition['single_channel']), fixed_channel_id=fixed, definition_hash=fingerprint))
				old_rows = {r.parameter_key: r for r in doc.parameters}
				doc.set('parameters', [])
				for p in definition['parameters']:
					row = doc.append('parameters', p)
					if p['parameter_key'] in old_rows:
						row.name = old_rows[p['parameter_key']].name
				doc.save(ignore_permissions=True)
			_align_channel_cardinality(name, definition['single_channel'])
	if action != 'unchanged':
		frappe.clear_cache(doctype='Communication Provider')
	return action


def retire_providers(app, identifiers):
	"""Availability is derived from current declarations; administrative states survive."""
	from messaging.security.permissions import require_system_manager
	require_system_manager()
	names = [name for name in frappe.get_all('Communication Provider', filters={'application': app}, pluck='name')
		if name not in identifiers]
	frappe.db.get_value('DocType', 'Communication Provider', 'name', for_update=True)
	for name in names:
		if frappe.db.get_value('Communication Provider', name, 'definition_hash'):
			frappe.db.set_value('Communication Provider', name, 'definition_hash', None)
	return names


def _align_channel_cardinality(provider, single_channel):
	"""Keep the unique single-channel key aligned without creating or deleting channels."""
	if single_channel:
		rows = frappe.get_all('Communication Channel', filters={'provider': provider}, pluck='name')
		if len(rows) == 1 and frappe.db.get_value('Communication Channel', rows[0], 'single_provider_key') != provider:
			frappe.db.set_value('Communication Channel', rows[0], 'single_provider_key', provider, update_modified=False)
		return
	if frappe.db.exists('Communication Channel', {'provider': provider, 'single_provider_key': ['is', 'set']}):
		frappe.db.sql('update `tabCommunication Channel` set single_provider_key=null where provider=%s and single_provider_key is not null', provider)



def _lock_provider_states():
	"""Serialize administrative enablement with catalog synchronization."""
	frappe.db.get_value('DocType', 'Communication Provider', 'name', for_update=True)


def get_provider_states(*, for_update=False):
	"""Provider states are uncached, disabled by default and independent of availability."""
	if for_update:
		_lock_provider_states()
		rows = frappe.db.sql('select name, enabled from `tabCommunication Provider` for update', as_dict=True)
	else:
		rows = frappe.get_all('Communication Provider', fields=['name', 'enabled'])
	return {row.name: bool(row.enabled) for row in rows}


def _write_provider_states(states):
	# Internal caller owns the catalog lock and the transaction.
	for name in frappe.get_all('Communication Provider', pluck='name'):
		frappe.db.set_value('Communication Provider', name, 'enabled', int(states.get(name, False)), update_modified=False)


@frappe.whitelist(methods=['POST'])
def set_provider_enabled(provider, enabled):
	"""Assign one explicit state; concurrent requests serialize or fail safely for a fresh retry."""
	from messaging.security.permissions import require_system_manager
	require_system_manager()
	if not frappe.db.exists('Communication Provider', provider):
		frappe.throw(_("Invalid channel provider."))
	frappe.get_doc('Communication Provider', provider).check_permission('read')
	if type(enabled) not in (bool, int, str) or enabled not in (True, False, '0', '1'):
		frappe.throw(_("Provider enabled must be a boolean value."))
	enabled = enabled in (True, '1')
	try:
		_lock_provider_states()
		if not frappe.db.exists('Communication Provider', provider):
			frappe.throw(_("Invalid channel provider."))
		if enabled:
			definition = provider_definition(provider)
			try:
				ready = all(callable(frappe.get_attr(definition['operations'][name])) for name in ('resolve_account', 'send'))
				ready = ready and is_available(definition)
			except Exception:
				ready = False
			if not ready:
				frappe.throw(_("The channel provider is unavailable and cannot be enabled."))
		states = get_provider_states(for_update=True)
		previous = states.get(provider, False)
		frappe.db.set_value('Communication Provider', provider, 'enabled', int(enabled), update_modified=False)
		return dict(provider=provider, enabled=enabled, previous_enabled=previous)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		frappe.throw(_("Provider enablement changed concurrently. Reload the provider list and retry."), frappe.TimestampMismatchError)


@frappe.whitelist()
def channel_providers():
	"""Enabled providers with an available channel slot, respecting Desk permissions."""
	frappe.has_permission('Communication Channel', 'read', throw=True)
	providers = frappe.get_list('Communication Provider', filters={'enabled': 1},
		fields=['name', 'provider_name', 'single_channel'], order_by='provider_name, name', limit_page_length=0)
	occupied = set(frappe.get_all('Communication Channel',
		filters={'provider': ['in', [row.name for row in providers]]}, pluck='provider')) if providers else set()
	return [dict(id=row.name, name=row.provider_name, single_channel=bool(row.single_channel))
		for row in providers if not row.single_channel or row.name not in occupied]


def provider_definitions():
	result, conflicts = {}, set()
	for app in installed_applications():
		from messaging.registry._catalog import application_declarations
		from messaging.registrations.communication_providers import normalize_definitions
		rows = normalize_definitions(app, application_declarations(app, kinds=('communication_providers',), tolerate_errors=True)['communication_providers'])
		for key, row in rows.items():
			owner = frappe.db.get_value('Communication Provider', key, 'application')
			if owner and owner != app:
				continue
			if key in result or key in conflicts:
				conflicts.add(key)
				result.pop(key, None)
			else:
				result[key] = row
	return result


def provider_registry():
	definitions = provider_definitions()
	result = []
	for catalog in frappe.get_all('Communication Provider', fields=['name', 'provider_name', 'application', 'version', 'single_channel']):
		definition = definitions.get(catalog.name)
		available = False
		if definition:
			try:
				provider_definition(catalog.name)
				available = is_available(definition)
			except Exception:
				pass
		result.append(dict(id=catalog.name, name=catalog.provider_name, app=catalog.application, version=catalog.version,
			single_channel=bool(catalog.single_channel), available=available, status='Available' if available else 'Unavailable',
			capabilities=definition['capabilities'] if definition else [], operations=sorted(definition['operations']) if definition else []))
	return result


active_adapter = ContextVar('messaging_active_adapter', default=None)


def provider_definition(provider):
	if not isinstance(provider, str) or provider not in (rows := provider_definitions()):
		frappe.throw(_("Invalid channel provider."))
	definition = rows[provider]
	catalog = frappe.db.get_value('Communication Provider', provider, ['application', 'definition_hash'], as_dict=True)
	if not catalog or catalog.application != definition['app'] or catalog.definition_hash != definition_hash(definition):
		frappe.throw(_("Provider catalog synchronization is required."))
	return definition


def operation(definition, name, *args):
	"""Only installed Code-First declarations, never client paths, select executable operations."""
	if name == 'authenticate_ingress':
		frappe.throw(_("Use the registered ingress dispatcher for channel authentication."), frappe.PermissionError)
	account = (args[0].get('account') if name in ('send', 'validate_notification', 'validate_template', 'template_variable_limits', 'resolve_provider_template', 'prepare_provider_template')
		else getattr(args[1], 'name', None) if name == 'notification_destination'
		else getattr(args[0], 'name', None) if name == 'resolve_account' else None)
	token = active_adapter.set((definition['id'], account))
	try:
		return frappe.get_attr(definition['operations'][name])(*args)
	finally:
		active_adapter.reset(token)


def is_available(definition):
	return all(callable(frappe.get_attr(path)) for path in definition['operations'].values()) and (
		'available' not in definition['operations'] or operation(definition, 'available') is True)


def get_provider_enabled(provider):
	"""Administrative authorization is independent of technical availability."""
	provider_definition(provider)
	return get_provider_states().get(provider, False)


def require_provider_enabled(provider):
	if not get_provider_enabled(provider):
		frappe.throw(_("The channel provider is disabled."), frappe.PermissionError)


DELIVERY_RANK = {'Pending': 0, 'Sent': 1, 'Failed': 2, 'Delivered': 3, 'Read': 4}
POSITIVE_DELIVERY = frozenset(('Sent', 'Delivered', 'Read'))


def advances_delivery(current, proposed):
	return proposed in DELIVERY_RANK and DELIVERY_RANK[proposed] > DELIVERY_RANK.get(current, -1)


def handoff(definition, payload):
	"""Shared adapter call after the source has durably recorded its handoff marker."""
	try:
		result = operation(definition, 'send', payload)
		validate_result(result)
		return result
	except Exception:
		frappe.db.rollback()
		return dict(outcome='Unknown')


def validate_result(result):
	if not isinstance(result, dict) or set(result) - {'outcome', 'reference', 'delivery_status'} or result.get('outcome') not in ('Accepted', 'Rejected', 'Unknown'):
		raise ValueError('Invalid transport result')
	ref = result.get('reference')
	if (ref is not None and (not isinstance(ref, str) or not ref.strip() or len(ref) > 140)) or (result['outcome'] == 'Accepted' and not ref):
		raise ValueError('Invalid transport reference')
	if 'delivery_status' in result and (result['outcome'] != 'Accepted' or result['delivery_status'] != 'Sent'):
		raise ValueError('Invalid handoff delivery evidence')
