"""Administrative AI provider catalog with independent typed capabilities.

Internal definitions are written only by the owning application's synchronization.
Desk and ordinary RPC can create External providers and cannot appoint an owner.
"""
import math
import re
from contextlib import nullcontext

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.configuration import normalize
from messaging.common.write_guards import InternalWriteGuard
from messaging.security.permissions import require_system_manager

provider_sync = InternalWriteGuard('messaging_ai_provider_sync')
from .parameters import TYPES, PROVIDER_ID, validate_scalar, validate_parameter_schema
ORIGINS = ('Internal', 'External')
REST_TRANSPORT_KEYS = frozenset(('endpoint', 'auth_type', 'username', 'auth_secret', 'timeout', 'minimum_confidence'))
RESERVED_REQUEST_KEYS = frozenset(('schema_version', 'request_id', 'message', 'language', 'context',
    'available_flows', 'allowed_outcomes', 'greeting_available', 'provider_parameters', 'decision', 'parameters'))
DEFINITION_FIELDS = ('parameter_key', 'label', 'description', 'field_type', 'required', 'default_value',
    'options', 'minimum', 'maximum', 'max_length', 'sort_order')


def parameter(key, label, kind='Data', default=None, required=0, **kwargs):
    return dict(parameter_key=key, label=label, field_type=kind, default_value=default,
        required=required, max_length=4000, **kwargs)


def adapter_parameters(kind="rest"):
    common = [parameter('timeout', 'Timeout (seconds)', 'Int', '10', 1, minimum=1, maximum=60),
        parameter('minimum_confidence', 'Minimum Confidence', 'Float', '0.60', 1, minimum=0.500001, maximum=1)]
    if kind == 'rest':
        return common + [parameter('endpoint', 'REST Endpoint', required=1),
            parameter('auth_type', 'Authentication', 'Select', 'None', 1, options='None\nBearer\nBasic'),
            parameter('username', 'Username'), parameter('auth_secret', 'Credential', 'Password')]
    frappe.throw(_('Invalid REST provider parameter schema.'))


def _blank(value):
    return value in (None, '')


def _reject_ordinary_internal(name):
    if name and frappe.db.get_value('AI Provider', name, 'provider_type') == 'Internal':
        frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)


class AIProvider(Document):
    def before_validate(self):
        require_system_manager()
        if provider_sync.active():
            return
        if self.is_new():
            # An ordinary document cannot claim internal origin or application ownership.
            if self.provider_type not in (None, '', 'External') or not _blank(self.application):
                frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
            self.provider_type = 'External'
            self.application = None
            if not self.parameters:
                for row in adapter_parameters('rest'):
                    self.append('parameters', row)
            return
        persisted = frappe.db.get_value(self.doctype, self.name,
            ['provider_type', 'application', 'decision', 'parameter_extraction'], as_dict=True, for_update=True)
        if persisted.provider_type == 'Internal':
            frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
        if self.provider_type not in (None, '', 'External'):
            frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
        if not _blank(self.application):
            frappe.throw(_('An external AI provider cannot claim an owning application.'), frappe.PermissionError)
        # Missing origin is historical and ambiguous: classify it External, never Internal.
        self.provider_type = 'External'
        self.application = None
        if (bool(persisted.decision), bool(persisted.parameter_extraction)) != (bool(self.decision), bool(self.parameter_extraction)):
            frappe.throw(_('AI Provider capabilities are immutable.'))

    def validate(self):
        if not PROVIDER_ID.fullmatch(self.provider_id or ''):
            frappe.throw(_('Invalid AI Provider identifier.'))
        if self.schema_version != '1':
            frappe.throw(_('Unsupported AI provider contract.'))
        if self.decision not in (0, 1) or self.parameter_extraction not in (0, 1) or not (self.decision or self.parameter_extraction):
            frappe.throw(_('Select at least one AI provider capability.'))
        if self.provider_type not in ORIGINS:
            frappe.throw(_('Invalid AI Provider origin.'))
        if provider_sync.active():
            if (self.provider_type != 'Internal' or _blank(self.application)
                or not isinstance(self.application, str)):
                frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
        elif self.provider_type != 'External' or not _blank(self.application):
            frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
        old = self.get_doc_before_save()
        if old:
            frappe.db.get_value(self.doctype, self.name, 'name', for_update=True)
            if old.provider_id != self.provider_id:
                frappe.throw(_('AI Provider identity and implementation are immutable.'))
            if old.provider_type == 'Internal' and not provider_sync.active():
                frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
            if old.provider_type == 'External' and self.provider_type != 'External':
                frappe.throw(_('An external AI provider cannot be replaced by an internal definition.'))
            if old.provider_type and old.provider_type != self.provider_type and not (
                _blank(old.provider_type) or (provider_sync.active() and old.provider_type == 'Internal')):
                frappe.throw(_('AI Provider identity and implementation are immutable.'))
            if old.application and old.application != self.application:
                frappe.throw(_('AI Provider identity and implementation are immutable.'))
            if not provider_sync.active() and (bool(old.decision), bool(old.parameter_extraction)) != (bool(self.decision), bool(self.parameter_extraction)):
                frappe.throw(_('AI Provider capabilities are immutable.'))
        elif self.provider_type == 'Internal' and not provider_sync.active():
            frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
        self._validate_parameters()

    def _validate_parameters(self, required=None):
        definitions = []
        for row in self.parameters:
            values = frappe._dict(row.as_dict())
            for key in ('options', 'description'):
                if values[key] is None:
                    values[key] = ''
            values.max_length = int(values.max_length or 4000)
            values.sort_order = values.sort_order or 0
            definitions.append(values)
        validate_parameter_schema(definitions)
        for row in self.parameters:
            row.is_secret = int(row.field_type == 'Password')
        if required is None:
            required = adapter_parameters('rest') if self.provider_type == 'External' else []
        actual = {row.parameter_key: row for row in self.parameters}
        for expected in required:
            row = actual.get(expected['parameter_key'])
            if not row or row.field_type != expected['field_type'] or (expected['required'] and not row.required):
                frappe.throw(_('Adapter parameter definitions are incomplete or incompatible.'))
        if self.provider_type == 'External':
            for row in self.parameters:
                if row.parameter_key in REST_TRANSPORT_KEYS:
                    continue
                if row.field_type == 'Password':
                    frappe.throw(_('Additional secret parameters cannot be sent by the generic REST executor. Use the standard credential.'))
                if row.parameter_key in RESERVED_REQUEST_KEYS:
                    frappe.throw(_('Provider parameters cannot use a reserved contract name.'))
        old = self.get_doc_before_save()
        if old and not provider_sync.active():
            previous = {r.parameter_key: r for r in old.parameters}
            for key, row in previous.items():
                if key not in actual or actual[key].field_type != row.field_type:
                    frappe.throw(_('Existing provider parameter keys and types are immutable.'))

    def before_rename(self, *args, **kwargs):
        frappe.throw(_('AI Provider identity and implementation are immutable.'))

    def on_trash(self):
        require_system_manager()
        origin = frappe.db.get_value(self.doctype, self.name, 'provider_type')
        if origin == 'Internal' or provider_sync.active():
            frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
        if frappe.db.exists('AI Configuration', {'provider': self.name}):
            frappe.throw(_('An AI Provider with configurations cannot be deleted.'))


def _number(value):
    """Canonical Float storage. Frappe persists this field as decimal(21,9) NOT NULL, so an omitted bound is 0."""
    if value in (None, ''):
        return 0.0
    return round(float(value), 6)


def _parameter_snapshot(rows):
    snapshot = []
    for row in rows:
        data = row.as_dict() if hasattr(row, 'as_dict') else row
        snapshot.append((data.get('parameter_key'), data.get('label') or '', data.get('description') or '',
            data.get('field_type'), int(data.get('required') or 0), data.get('default_value') or None,
            data.get('options') or '', _number(data.get('minimum')), _number(data.get('maximum')),
            int(data.get('max_length') or 4000), int(data.get('sort_order') or 0)))
    return tuple(sorted(snapshot))


def _snapshot(doc):
    return (doc.provider_name, doc.description or '', int(doc.enabled or 0), doc.schema_version, int(doc.decision), int(doc.parameter_extraction),
        doc.application or None, doc.provider_type, _parameter_snapshot(doc.parameters))


def _sync_ai_providers(*, app, definitions):
    """Reconcile one application's internal AI providers inside the caller's transaction.

    Registration supplies normalized storage records after verifying its owner.
    An identical declaration does not save. External rows and ambiguous origins are
    never adopted, and this function does not delete providers.
    """
    if not isinstance(definitions, dict):
        frappe.throw(_('Invalid AI Provider storage records.'))
    frappe.db.get_value('DocType', 'AI Provider', 'name', for_update=True)
    summary = dict(created=0, updated=0, unchanged=0)
    savepoint = 'ai_provider_' + frappe.generate_hash(length=12)
    frappe.db.savepoint(savepoint)
    try:
        with provider_sync():
            for provider_id, values in definitions.items():
                exists = frappe.db.exists('AI Provider', provider_id)
                doc = frappe.get_doc('AI Provider', provider_id, for_update=True) if exists else frappe.new_doc('AI Provider')
                if exists and doc.provider_type == 'External':
                    frappe.throw(_('An external AI provider cannot be replaced by an internal definition.'))
                if exists and doc.provider_type != 'Internal':
                    frappe.throw(_('Ambiguous AI provider origin cannot be adopted by synchronization.'))
                if exists and doc.application != app:
                    frappe.throw(_('AI Provider identity and implementation are immutable.'))
                desired = frappe._dict(values)
                if exists:
                    # The declaration supplies the initial state only.
                    desired.enabled = doc.enabled
                if exists and _snapshot(doc) == _snapshot(desired):
                    summary['unchanged'] += 1
                    continue
                if not exists:
                    doc.provider_id = provider_id
                previous = {row.parameter_key: row.name for row in doc.parameters}
                doc.update({key: desired[key] if key != 'parameters' else [] for key in (
                    'provider_name', 'description', 'enabled', 'schema_version', 'decision', 'parameter_extraction', 'application',
                    'provider_type', 'parameters')})
                for item in values['parameters']:
                    row = doc.append('parameters', item)
                    if item['parameter_key'] in previous:
                        row.name = previous[item['parameter_key']]
                doc.save(ignore_permissions=True)
                summary['updated' if exists else 'created'] += 1
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return summary


@frappe.whitelist(methods=['POST'])
def set_provider_enabled(provider, enabled):
    """Administrative enablement is independent of profile credentials and approval."""
    require_system_manager()
    doc = frappe.get_doc('AI Provider', provider, for_update=True)
    doc.check_permission('write')
    if type(enabled) not in (bool, int, str) or enabled not in (True, False, '0', '1'):
        frappe.throw(_('Provider enabled must be a boolean value.'))
    value = int(enabled in (True, '1'))
    if value != doc.enabled:
        with provider_sync() if doc.provider_type == 'Internal' else nullcontext():
            doc.enabled = value
            doc.save()
    return dict(provider=doc.name, enabled=bool(value))


@frappe.whitelist()
def parameter_schema(provider):
    require_system_manager()
    doc = frappe.get_doc('AI Provider', provider)
    doc.check_permission('read')
    return dict(parameters=[{key: row.get(key) for key in DEFINITION_FIELDS} for row in doc.parameters])
