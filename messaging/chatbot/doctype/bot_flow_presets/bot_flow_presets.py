"""Persisted Bot Flow defaults, with independently owned localized contributions."""
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import inspect
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.locking import document_savepoint, lock_document_types
from messaging.common.write_guards import InternalWriteGuard
from messaging.security.permissions import require_registration_owner, require_provider_function, require_system_manager
from messaging.security import permissions as ownership
from messaging.chatbot.doctype.bot_flow.configuration import (
    combine_preset_configuration, validate_preset_configuration,
)

_preset_write = InternalWriteGuard('messaging_flow_preset_write')
_synchronization = ContextVar('messaging_flow_preset_synchronization', default=None)
_MUTATION_PERMISSIONS = frozenset(('create', 'write', 'delete', 'import', 'submit', 'cancel', 'amend'))


def _deny_manual_write(*args, **kwargs):
    raise frappe.PermissionError(_('Bot Flow presets are managed by their owning application.'))


def _require_preset_write(doc):
    """Allow only the exact parent and child instances being persisted by synchronization."""
    scope = _synchronization.get()
    parent = scope.get('document') if scope is not None else None
    if (not _preset_write.active() or scope is None or scope.get('phase') != 'persist'
            or scope['active'] is not None or parent is None
            or getattr(frappe.local, 'request', None) is not None):
        _deny_manual_write()
    require_system_manager()
    if doc is parent:
        return
    if (doc.doctype != 'Bot Flow Preset Language' or doc.parenttype != parent.doctype
            or doc.parent != parent.name or doc.parentfield != 'languages'
            or not any(row is doc for row in parent.languages)):
        _deny_manual_write()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def configuration_hash(value):
    return sha256(canonical(value).encode()).hexdigest()


class BotFlowPresets(Document):
    def has_permission(self, permtype='read', **kwargs):
        if permtype in _MUTATION_PERMISSIONS:
            return False
        return super().has_permission(permtype, **kwargs)

    def check_permission(self, permtype='read', *args, **kwargs):
        if permtype in ('create', 'write'):
            _require_preset_write(self)
            return
        if permtype in _MUTATION_PERMISSIONS:
            _deny_manual_write()
        return super().check_permission(permtype, *args, **kwargs)

    def db_insert(self, *args, **kwargs):
        self.before_validate()
        return super().db_insert(*args, **kwargs)

    def db_update(self, *args, **kwargs):
        self.before_validate()
        return super().db_update(*args, **kwargs)

    def before_validate(self):
        _require_preset_write(self)

    def update_child_table(self, *args, **kwargs):
        _require_preset_write(self)
        return super().update_child_table(*args, **kwargs)

    db_set = _deny_manual_write
    before_change = _deny_manual_write
    delete = _deny_manual_write

    def validate(self):
        self.before_validate()
        if not re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*', self.preset_key or ''):
            frappe.throw(_('Invalid preset identifier.'))
        if len(self.preset_key.replace('.', '_')) > 64:
            frappe.throw(_('Preset identifier is too long.'))
        previous = self.get_doc_before_save()
        if previous and (self.preset_key, self.application) != (previous.preset_key, previous.application):
            raise frappe.PermissionError(_('Preset identity and ownership are immutable.'))
        common = json.loads(self.configuration)
        # Retired declarations remain historical records even if their Process is absent.
        if self.available:
            validate_preset_configuration(self.process_definition, common)
        if self.configuration_hash != configuration_hash(dict(name=self.preset_name, process=self.process_definition, data=common)):
            frappe.throw(_('Preset configuration hash does not match its content.'))
        seen = set()
        old_languages = {r.language: r for r in previous.languages} if previous else {}
        for row in self.languages:
            if row.language in seen:
                frappe.throw(_('Each preset language must have a single owner.'))
            seen.add(row.language)
            old = old_languages.get(row.language)
            if old and row.application != old.application:
                raise frappe.PermissionError(_('Preset language ownership is immutable.'))
            data = json.loads(row.configuration)
            if self.available and row.available:
                validate_preset_configuration(self.process_definition, common, language=row.language, localized=data)
            if row.configuration_hash != configuration_hash(data):
                frappe.throw(_('Preset language hash does not match its content.'))

    def on_trash(self):
        raise frappe.PermissionError(_('Retire preset declarations without deleting their history.'))

    def before_rename(self, *args, **kwargs):
        raise frappe.PermissionError(_('Preset identity is immutable.'))


def _provenance(caller):
    scope = _synchronization.get()
    app = caller.f_globals.get('__name__', '').split('.')[0]
    require_registration_owner(app, caller)
    function = scope.get('function') if scope else None
    if function is None or function.__code__ is not caller.f_code:
        raise frappe.PermissionError(_('Preset contributions must use the active authorized declaration.'))
    method = require_provider_function(function, app)
    if scope is None or scope.get('phase') != 'collect' or scope['active'] != (app, method):
        raise frappe.PermissionError(_('Preset contributions must use the active authorized declaration.'))
    return app, method


def common_configuration(*, inputs=None, outcomes=None, priority=0, show_in_menu=False, menu_order=0):
    """Build the persisted common preset document from explicit registration arguments."""
    return dict(
        inputs={} if inputs is None else inputs,
        outcomes={} if outcomes is None else outcomes,
        options=dict(priority=priority, show_in_menu=show_in_menu, menu_order=menu_order),
    )


def save_preset(*, key, name, process, inputs=None, outcomes=None, priority=0, show_in_menu=False, menu_order=0):
    """Record one main preset. Synchronization persists it with insert() or save()."""
    frame = inspect.currentframe()
    try:
        app, method = _provenance(frame.f_back)
    finally:
        del frame
    if not isinstance(name, str) or not name.strip():
        frappe.throw(_('Preset Name is required.'))
    data = common_configuration(inputs=inputs, outcomes=outcomes, priority=priority,
        show_in_menu=show_in_menu, menu_order=menu_order)
    operation = dict(key=key, name=name, process=process, data=deepcopy(data), app=app, method=method)
    scope = _synchronization.get()
    if key in scope['main']:
        frappe.throw(_('Duplicate main preset declaration: {0}').format(key))
    scope['main'][key] = operation
    return key


def record_language(*, preset, language, data, app, method):
    """Accept one staged language contribution from its owning hook."""
    operation = dict(preset=preset, language=language, data=deepcopy(data), app=app, method=method)
    scope = _synchronization.get()
    identity = (preset, language)
    if identity in scope['languages']:
        frappe.throw(_('Duplicate preset language contribution: {0} / {1}').format(*identity))
    scope['languages'][identity] = operation
    return preset


def _persist(doc, before):
    """Write one complete preset through the Document controller."""
    scope = _synchronization.get()
    if (scope is None or scope.get('phase') != 'persist' or scope['active'] is not None
            or scope.get('document') is not None):
        _deny_manual_write()
    if before is not None and before == doc.as_dict():
        return doc.name
    scope['document'] = doc
    try:
        with _preset_write():
            # A new catalogue row uses insert(); an existing row uses save().
            doc.insert() if doc.is_new() else doc.save()
    finally:
        scope['document'] = None
    return doc.name


def _save_main(*, key, name, process, data, app, method, validate_existing=True):
    exists = frappe.db.exists('Bot Flow Presets', key)
    if exists:
        doc = frappe.get_doc('Bot Flow Presets', key, for_update=True)
        if doc.application != app:
            raise frappe.PermissionError(_('The preset belongs to another application.'))
        if doc.process_definition != process:
            frappe.throw(_('Preset Process identity is immutable.'))
        before = doc.as_dict()
        doc.update(dict(preset_key=key, preset_name=name, process_definition=process, application=app,
            source_method=method, configuration=canonical(data),
            configuration_hash=configuration_hash(dict(name=name, process=process, data=data)), available=1))
    else:
        before = None
        doc = frappe.get_doc(dict(doctype='Bot Flow Presets', __islocal=1, preset_key=key, preset_name=name,
            process_definition=process, application=app, source_method=method, configuration=canonical(data),
            configuration_hash=configuration_hash(dict(name=name, process=process, data=data)), available=1))
    # Synchronization applies all contributions before the single parent insert or save.
    if not validate_existing:
        return doc, before
    return _persist(doc, before)


def _save_language(*, preset, language, data, app, method, documents=None):
    from messaging.chatbot.doctype.bot_flow_preset_language.bot_flow_preset_language import apply_language
    return apply_language(preset=preset, language=language, data=data, app=app, method=method, documents=documents)


def provision_presets(*, app, declarations):
    """Provision explicitly selected owner defaults during installation or a patch."""
    require_system_manager()
    frame = inspect.currentframe()
    try:
        require_registration_owner(app, frame.f_back)
    finally:
        del frame
    if getattr(frappe.local, 'request', None) is not None or _synchronization.get() is not None:
        raise frappe.PermissionError(_('Preset synchronization requires a server lifecycle or Bench call.'))
    if not isinstance(declarations, (list, tuple)):
        raise frappe.ValidationError(_('Preset declarations must be a finite collection of owner functions.'))
    functions = [(require_provider_function(function, app), function) for function in declarations]
    scope = dict(active=None, function=None, main={}, languages={}, phase='collect', document=None)
    token = _synchronization.set(scope)
    try:
        with document_savepoint():
            lock_document_types('Bot Flow Presets')
            for method, function in functions:
                scope['active'] = (app, method)
                scope['function'] = function
                function()
            scope['active'] = None
            scope['function'] = None
            scope['phase'] = 'persist'
            documents = {}
            for key, operation in sorted(scope['main'].items()):
                documents[key] = _save_main(**operation, validate_existing=False)
            for identity, operation in sorted(scope['languages'].items()):
                preset = operation['preset']
                if preset not in documents:
                    doc = frappe.get_doc('Bot Flow Presets', preset, for_update=True)
                    documents[preset] = doc, doc.as_dict()
                _save_language(**operation, documents=documents)
            for key, (doc, before) in documents.items():
                _persist(doc, before)
            return dict(presets=len(documents), languages=len(scope['languages']))
    except Exception:
        frappe.clear_document_cache('Bot Flow Presets')
        raise
    finally:
        _synchronization.reset(token)


def configuration(doc, *, languages):
    """Read persisted available contributions, never invoke a declaration hook."""
    installed = ownership.installed_applications()
    if not doc.available or doc.application not in installed:
        frappe.throw(_('The main preset declaration is unavailable: {0}').format(doc.name))
    localized = {r.language: json.loads(r.configuration) for r in doc.languages
        if r.available and r.application in installed and r.language in languages}
    return combine_preset_configuration(key=doc.name, name=doc.preset_name, process=doc.process_definition,
        data=json.loads(doc.configuration), languages=localized)


def declaration_versions(doc):
    """Optimistic tokens bind administrative confirmation to the displayed declarations."""
    installed = ownership.installed_applications()
    return dict(common=dict(hash=doc.configuration_hash, available=bool(doc.available and doc.application in installed)),
        languages={row.language: dict(hash=row.configuration_hash,
            available=bool(row.available and row.application in installed), application=row.application)
            for row in doc.languages})


@frappe.whitelist()
def list_presets():
    """Read the persisted catalogue for Bot Settings; no code discovery or synchronization."""
    from messaging.chatbot.doctype.bot_settings.language import configured_languages
    from messaging.chatbot.doctype.bot_flow.configuration import _available_languages
    from messaging.chatbot.doctype.bot_flow.preset_application import (
        installation_state, installed_flow_languages)
    from messaging.chatbot.doctype.bot_flow.contract import default_contract
    from messaging.chatbot.doctype.bot_flow.bot_flow import existing_default
    require_system_manager()
    frappe.get_single('Bot Settings').check_permission('read')
    frappe.has_permission('Bot Flow Presets', 'read', throw=True)
    frappe.has_permission('Bot Flow', 'read', throw=True)
    active, default = configured_languages()
    result = []
    for name in frappe.get_all('Bot Flow Presets', pluck='name', order_by='application, preset_key'):
        doc = frappe.get_doc('Bot Flow Presets', name)
        item = dict(key=doc.name, name=doc.preset_name, app=doc.application, process=doc.process_definition,
            version=declaration_versions(doc),
            declared_languages=sorted(r.language for r in doc.languages if r.available and r.application in ownership.installed_applications()),
            selectable=False, existing=None, installed_languages=[], pending_languages=[], changed_languages=[],
            unavailable_languages=[], available=[], missing_content=[], common_changed=False,
            manual_changes=[], requires_reset=False, status='Unavailable', diagnostic='')
        try:
            existing = existing_default(dict(key=doc.name, process=doc.process_definition), app=doc.application)
            if existing:
                existing.check_permission('read')
                versions = json.loads(existing.preset_versions or '{}')
                item['existing'] = existing.name
                item['installed_languages'] = installed_flow_languages(existing)
                item['unavailable_languages'] = sorted(code for code in versions.get('languages', {})
                    if code not in set(item['declared_languages']))
            definition = configuration(doc, languages=active)
            default_contract(definition, doc.application)
            item.update(installation_state(doc, definition, active))
            item['missing_content'] = [language for language in active if language not in item['available']]
            if item['missing_content']:
                frappe.throw(_('Every enabled language requires complete compatible preset content.'))
            item['selectable'] = bool(not item['existing'] or item['pending_languages'] or item['requires_reset'])
            item['status'] = ('Restoration available' if item['requires_reset'] else 'Pending languages'
                if item['existing'] and item['pending_languages'] else 'Complete configuration'
                if item['existing'] else 'Pending creation')
        except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError) as error:
            item['diagnostic'] = str(error)
        result.append(item)
    return dict(languages=active, default_language=default, presets=result)
