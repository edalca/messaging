"""Persistent catalogue exclusively maintained by the public Process Registry."""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import keyword
import re
import signal
import sys
import threading
import time

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.primitives import _freeze
from messaging.security import permissions as registration_permissions

from messaging.common.write_guards import InternalWriteGuard

_registry_write = InternalWriteGuard('messaging_process_registry')


class ProcessUnavailableError(frappe.ValidationError):
    """The persisted Process is unavailable to consumers."""


class ProcessDefinition(Document):
    def onload(self):
        # Desk displays effective site availability, including missed uninstall hooks.
        self.status = self.lifecycle_status

    @property
    def lifecycle_status(self):
        return 'Enabled' if _is_available(self) else 'Unavailable'

    def check_permission(self, permtype='read', *args, **kwargs):
        if _registry_write.active() and permtype in ('create', 'write', 'delete'):
            from messaging.security.permissions import require_system_manager
            require_system_manager()
            return
        return super().check_permission(permtype, *args, **kwargs)

    def before_validate(self):
        if not _registry_write.active():
            raise frappe.PermissionError(_('Process definitions are managed by their owning application.'))

    def validate(self):
        self.before_validate()
        previous = self.get_doc_before_save()
        if previous and (self.application, self.process_id, self.process_type) != (
            previous.application, previous.process_id, previous.process_type):
            raise frappe.PermissionError(_('Process identity, type and ownership are immutable.'))

        if not self.get('registered_contract') or not self.get('registered_snapshot'):
            raise frappe.ValidationError(_('Register the complete Process contract before using this definition.'))
        if self.status == 'Enabled' and not self.definition_method:
            raise frappe.ValidationError(_('Register the Process definition before enabling it.'))
        contract = json.loads(self.registered_contract)
        snapshot = json.loads(self.registered_snapshot)
        from messaging.communication.doctype.process_definition.revisions import registration_revisions
        revisions = registration_revisions(self, snapshot)
        if (snapshot['declaration']['contract'] != contract
            or self.contract_revision != revisions['contract_revision']
            or self.capability_revision != revisions['capability_revision']):
            raise frappe.ValidationError(_('The persisted Process metadata does not match its registered revision.'))

    def on_trash(self):
        self.before_validate()
        from messaging.security.permissions import require_system_manager
        require_system_manager()
        if getattr(frappe.local, 'request', None):
            raise frappe.PermissionError(_('Process definitions must be retired through the Registry.'))

    def before_rename(self, *args, **kwargs):
        raise frappe.PermissionError(_('Process identity is immutable.'))


def _is_available(definition):
    return bool(definition.status == 'Enabled' and definition.application in registration_permissions.installed_applications()
        and definition.contract_version == 1
        and (definition.method or '').split('.')[0] == definition.application
        and (definition.definition_method or '').split('.')[0] == definition.application)


def retire_definitions(app, identifiers):
    """Keep unavailable declarations and their consumers; never execute old contracts."""
    from messaging.security.permissions import require_system_manager
    require_system_manager()
    frappe.db.get_value('DocType', 'Process Definition', 'name', for_update=True)
    retired = []
    with _registry_write():
        for name in frappe.get_all('Process Definition', filters={'application': app}, pluck='name'):
            if name in identifiers:
                continue
            retired.append(name)
            doc = frappe.get_doc('Process Definition', name, for_update=True)
            if doc.status != 'Unavailable':
                # An incompatible declaration must be withdrawable even when its
                # stored contract no longer passes the current Enabled validator.
                doc.load_doc_before_save()
                doc.status = 'Unavailable'
                frappe.db.set_value('Process Definition', name, 'status', 'Unavailable')
                doc.run_method('on_update')
    return retired


def after_app_uninstall(app_name):
    """Retire only the absent owner's technical availability, preserving consumers."""
    if app_name in registration_permissions.installed_applications():
        return
    from messaging.security.permissions import require_system_manager
    require_system_manager()
    if getattr(frappe.local, 'request', None) is not None:
        raise frappe.PermissionError(_('Registration requires a server lifecycle or Bench call.'))
    # Use the Registry's catalogue lock to serialize retirement with registration.
    frappe.db.get_value('DocType', 'Process Definition', 'name', for_update=True)
    if app_name in registration_permissions.installed_applications():
        return
    names = frappe.get_all('Process Definition',
        filters={'application': app_name, 'status': 'Enabled'}, pluck='name')
    retire_definitions(app_name, set())
    if names:
        # Frappe 16 remove_app commits before this hook; its CLI then destroys
        # the connection without a final commit. Persist this bounded retirement.
        frappe.db.commit()


def resolve_definition(process_id, *, expected_type, allow_retired=False):
    """Fresh resolution is mandatory at approval and invocation boundaries."""
    if not process_id or not frappe.db.exists('Process Definition', process_id):
        raise ProcessUnavailableError(_('Select an available registered Process.'))
    doc = frappe.get_doc('Process Definition', process_id)
    if doc.process_type != expected_type or (not allow_retired and not _is_available(doc)):
        raise ProcessUnavailableError(_('The Process is unavailable or has an incompatible type.'))
    return doc


def registered_contract(definition):
    """Read the authoritative registered declaration without loading owner code."""
    if not definition.get('registered_contract'):
        raise frappe.ValidationError(_('Synchronize the Process owning application before configuring consumers.'))
    return json.loads(definition.registered_contract)


def registered_snapshot(definition):
    if not definition.get('registered_snapshot'):
        raise frappe.ValidationError(_('Synchronize the Process owning application before execution.'))
    return json.loads(definition.registered_snapshot)


def bind_definition(doc, *, allow_retired=False):
    """Derive technical fields; browser-supplied paths never authorize execution."""
    expected = {'Bot Flow': 'bot_flow', 'Notification Rule': 'notification'}[doc.doctype]
    previous = doc.get_doc_before_save() if allow_retired else None
    definition = resolve_definition(doc.get('process_definition'), expected_type=expected,
        allow_retired=bool(previous and previous.process_definition == doc.process_definition))
    doc.process_method = definition.method
    doc.process_invocation_mode = 'Internal Call (frappe.call)'
    return definition


def definition_revision(definition):
    """Labels are presentation; availability, identity and contract govern approval."""
    keys = ['process_id', 'application', 'process_type', 'method', 'capability_revision', 'status', 'contract_version']
    if definition.get('definition_method'):
        keys.insert(3, 'definition_method')
    values = {key: definition.get(key) for key in keys}
    values['creation'] = str(definition.get('creation') or '')
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


DOTTED_METHOD_PATH_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*){2,10}$")

FORBIDDEN_PROCESS_ROOT_MODULES = frozenset(
	{
		"builtins",
		"__builtin__",
		"os",
		"sys",
		"subprocess",
		"shutil",
		"pathlib",
		"importlib",
		"pickle",
		"marshal",
		"socket",
		"ctypes",
		"multiprocessing",
		"threading",
		"code",
		"pty",
		"commands",
		"inspect",
		"eval",
		"exec",
	}
)

FORBIDDEN_PROCESS_PREFIXES = (
	"frappe.db.",
	"frappe.client.",
	"frappe.core.",
	"frappe.desk.",
	"frappe.handler.",
	"frappe.model.",
	"frappe.utils.safe_exec.",
)


def validate_process_method_path(method_path: str | None) -> str:
	"""Validate that a Process method path is a safe dotted Python path without importing or executing it."""
	if not isinstance(method_path, str) or not method_path.strip():
		frappe.throw(_("Process Method Path is required."), frappe.ValidationError)

	cleaned = method_path.strip()
	if not DOTTED_METHOD_PATH_PATTERN.match(cleaned):
		frappe.throw(
			_(
				"Process Method Path '{0}' must be a valid dotted Python path '<app>.<module>.<function>' without spaces, expressions, or private '_' segments."
			).format(cleaned),
			frappe.ValidationError,
		)

	segments = cleaned.split(".")
	for seg in segments:
		if seg.startswith("_") or keyword.iskeyword(seg):
			frappe.throw(
				_(
					"Process Method Path '{0}' contains a reserved or private segment '{1}'."
				).format(cleaned, seg),
				frappe.ValidationError,
			)

	if segments[0] in FORBIDDEN_PROCESS_ROOT_MODULES or any(
		seg in FORBIDDEN_PROCESS_ROOT_MODULES for seg in segments
	):
		frappe.throw(
			_("Process Method Path '{0}' references a restricted module.").format(cleaned),
			frappe.ValidationError,
		)

	if any(cleaned.startswith(prefix) for prefix in FORBIDDEN_PROCESS_PREFIXES):
		frappe.throw(
			_("Process Method Path '{0}' references a restricted framework namespace.").format(cleaned),
			frappe.ValidationError,
		)

	return cleaned



_testing = ContextVar("messaging_process_test", default=False)
PROCESS_TIMEOUT_SECONDS = 30


def is_process_test():
	return _testing.get()


@contextmanager
def _discovery_deadline():
	"""Interrupt synchronous discovery calls on main-thread workers."""
	if threading.current_thread() is not threading.main_thread():
		raise RuntimeError("Process execution requires a worker with deadline support")
	previous_handler = signal.getsignal(signal.SIGALRM)
	previous_timer = signal.getitimer(signal.ITIMER_REAL)
	started = time.monotonic()
	def expired(signum, frame):
		raise TimeoutError("Process execution deadline exceeded")
	signal.signal(signal.SIGALRM, expired)
	limit = min(PROCESS_TIMEOUT_SECONDS, previous_timer[0]) if previous_timer[0] else PROCESS_TIMEOUT_SECONDS
	signal.setitimer(signal.ITIMER_REAL, limit)
	try:
		yield
	finally:
		signal.setitimer(signal.ITIMER_REAL, 0)
		signal.signal(signal.SIGALRM, previous_handler)
		if previous_timer[0]:
			signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0]-(time.monotonic()-started)), previous_timer[1])


@contextmanager
def process_messages():
	"""Isolate request presentation during the business call, preserving outer Desk messages."""
	previous_log = frappe.local.message_log
	previous_mute = frappe.flags.mute_messages
	response = frappe.local.response
	sentinel = object()
	previous_server_messages = response.get('_server_messages', sentinel)
	frappe.local.message_log = []
	frappe.flags.mute_messages = True
	try:
		yield
	finally:
		frappe.local.message_log = previous_log
		frappe.flags.mute_messages = previous_mute
		response = frappe.local.response
		if previous_server_messages is sentinel:
			response.pop('_server_messages', None)
		else:
			response['_server_messages'] = previous_server_messages


_scope = ContextVar('messaging_read_scope', default=None)
_internal = ContextVar('messaging_read_internal', default=False)
MAX_ROWS = 5000


@contextmanager
def _native():
    token = _internal.set(True)
    try:
        yield
    finally:
        _internal.reset(token)


def _reject():
    raise frappe.PermissionError(_('The read is outside the declared Process capability.'))


def _doctype(doctype):
    scope = _scope.get()
    if scope is None:
        return
    if doctype not in scope:
        _reject()


# Only native permission/metadata machinery may use these tables while mediated.
_TECHNICAL_READS = frozenset(('DocType', 'DocField', 'DocPerm', 'Custom DocPerm',
    'Custom Field', 'Property Setter', 'User Permission', 'DocShare', 'Permission Type',
    'Role', 'Has Role', 'User', 'Module Def', 'Singles', 'DefaultValue', 'Notification Rule'))


def verify_read_query(query):
    """Check every native query table, including joins made by trusted helpers."""
    tables = re.findall(r'(?i)\b(?:FROM|JOIN)\s+(?:`tab([^`]+)`|tab([a-zA-Z][a-zA-Z0-9_]*))', query)
    if not tables:
        _reject()
    for quoted, plain in tables:
        name = quoted or plain
        if name not in (_scope.get() or {}) and name not in _TECHNICAL_READS:
            _reject()


def _fields(doctype, fields, *, parenttype=None):
    meta = frappe.get_meta(doctype)
    parent = _scope.get().get(doctype) if _scope.get() is not None else parenttype
    parents = parent if isinstance(parent, tuple) else (parent,)
    levels = set.intersection(*(set(meta.get_permlevel_access(permission_type='read', parenttype=p)) for p in parents))
    for name in fields:
        if name in ('name', 'creation', 'modified', 'modified_by', 'owner', 'docstatus', 'idx',
                    'parent', 'parenttype', 'parentfield'):
            continue
        field = meta.get_field(name)
        if not field or field.permlevel not in levels or field.fieldtype in ('Table', 'Table MultiSelect'):
            _reject()


def _check_document(doc):
    _doctype(doc.doctype)
    parent = _scope.get().get(doc.doctype) if _scope.get() is not None else doc.get('parenttype')
    if doc.meta.istable:
        if not parent or doc.parenttype not in (parent if isinstance(parent, tuple) else (parent,)):
            _reject()
        owner = frappe.get_doc(doc.parenttype, doc.parent)
        field = owner.meta.get_field(doc.parentfield)
        if not field or field.options != doc.doctype or field.permlevel not in owner.get_permlevel_access('read'):
            _reject()
        owner.check_permission('read')
    else:
        doc.check_permission('read')
    from frappe.permissions import has_controller_permissions
    if not has_controller_permissions(doc, 'read'):
        _reject()
    doc.apply_fieldlevel_read_permissions()
    for field in doc.meta.get_table_fields():
        if field.permlevel not in doc.get_permlevel_access('read'):
            continue
        for child in doc.get(field.fieldname) or []:
            _check_document(child)
    return doc


def _document(original, *args, **kwargs):
    if len(args) == 1 and isinstance(args[0], dict) and not kwargs:
        values = args[0]
        _doctype(values.get('doctype'))
        _require_write_execution()
        with _native():
            doc = original(*args)
            doc.check_permission('create')
            return doc
    if not args or not isinstance(args[0], str) or len(args) > 2 or kwargs:
        _reject()
    _doctype(args[0])
    with _native():
        return _check_document(original(*args))


def _filter_fields(filters):
    if not filters:
        return []
    if isinstance(filters, dict):
        return list(filters)
    if isinstance(filters, (list, tuple)):
        names = []
        for item in filters:
            if not isinstance(item, (list, tuple)) or len(item) not in (2, 3):
                _reject()
            names.append(item[0])
        return names
    _reject()


def _rows(original, doctype, *args, **kwargs):
    _doctype(doctype)
    if args or set(kwargs) - {'filters', 'or_filters', 'fields', 'order_by', 'limit_page_length',
                             'limit_start', 'limit', 'pluck'}:
        _reject()
    fields = kwargs.get('fields') or [kwargs.get('pluck') or 'name']
    if isinstance(fields, str):
        fields = [fields]
    if not isinstance(fields, (list, tuple)) or not fields or any(not isinstance(f, str) for f in fields):
        _reject()
    pluck = kwargs.pop('pluck', None)
    if pluck and fields != [pluck]:
        _reject()
    with _native():
        if fields == ['*']:
            fields = ['name'] + [f.fieldname for f in frappe.get_meta(doctype).fields
                if f.fieldtype not in ('Table', 'Table MultiSelect', 'Section Break', 'Column Break', 'Tab Break', 'HTML', 'Button', 'Heading')]
        _fields(doctype, [*fields, *_filter_fields(kwargs.get('filters')), *_filter_fields(kwargs.get('or_filters'))])
        order = kwargs.get('order_by') or 'name asc'
        for part in order.split(','):
            if not re.fullmatch(r'\s*[a-zA-Z][a-zA-Z0-9_]*(?:\s+(?:asc|desc))?\s*', part, re.I):
                _reject()
            _fields(doctype, [part.strip().split()[0]])
        limit = kwargs.pop('limit', kwargs.pop('limit_page_length', MAX_ROWS + 1))
        if type(limit) is not int or not 1 <= limit <= MAX_ROWS + 1:
            _reject()
        start = kwargs.get('limit_start', 0)
        if type(start) is not int or not 0 <= start <= MAX_ROWS:
            _reject()
        # Include identity and child relationship for per-record checks.
        query_fields = list(dict.fromkeys(['name', *fields]))
        if frappe.get_meta(doctype).istable:
            query_fields = list(dict.fromkeys([*query_fields, 'parent', 'parenttype', 'parentfield']))
        result = original(doctype, **dict(kwargs, fields=query_fields, order_by=order, limit_page_length=limit))
        if len(result) > MAX_ROWS:
            raise frappe.ValidationError(_('Business read exceeds the record limit.'))
        for row in result:
            _check_document(frappe.get_doc(doctype, row.name))
        return [row[pluck] for row in result] if pluck else [frappe._dict({f: row.get(f) for f in fields}) for row in result]


def _value(original, doctype, filters=None, fieldname='name', *args, **kwargs):
    if args or set(kwargs) - {'as_dict', 'cache', 'for_update'}:
        _reject()
    _doctype(doctype)
    fields = [fieldname] if isinstance(fieldname, str) else list(fieldname)
    with _native():
        _fields(doctype, fields)
    if isinstance(filters, str) or filters is None:
        with _native():
            # Existence reveals no business fields; permissions are checked first.
            parent = (_scope.get() or {}).get(doctype)
            if kwargs.get('for_update'):
                _require_write_execution()
                original(doctype, filters, 'name', for_update=True)
            if filters is not None and not frappe.db.exists(doctype, filters):
                return None
            doc = _check_document(frappe.get_doc(doctype, filters) if filters is not None else frappe.get_doc(doctype))
            values = frappe._dict({f: doc.get(f) for f in fields})
    else:
        result = _rows(frappe.get_all, doctype, filters=filters, fields=fields, limit_page_length=1)
        if not result:
            return None
        values = result[0]
    return values if kwargs.get('as_dict') else values[fields[0]] if len(fields) == 1 else tuple(values[f] for f in fields)


def install_read_access():
    for name in ('get_doc', 'get_cached_doc', 'get_single', 'get_all', 'get_list', 'has_permission'):
        original = getattr(frappe, name)
        if getattr(original, '__messaging_read_access__', None) is True:
            continue
        @wraps(original)
        def controlled(*args, __original=original, __name=name, **kwargs):
            if _scope.get() is None or _internal.get():
                return __original(*args, **kwargs)
            if __name == 'has_permission':
                target = args[0] if args else kwargs.get('doctype') or kwargs.get('doc')
                _doctype(target if isinstance(target, str) else getattr(target, 'doctype', None))
                # Native Single permission checks load their document lazily.
                # Keep that read under the same verified SQL and document scope.
                with _native():
                    return __original(*args, **kwargs)
            return (_rows if __name in ('get_all', 'get_list') else _document)(__original, *args, **kwargs)
        controlled.__messaging_read_access__ = True
        setattr(frappe, name, controlled)


@contextmanager
def controlled_reads(documents=(), *, applications=None):
    install_read_access()
    scope = {}
    if applications is not None:
        from frappe.modules.utils import get_module_app
        documents = [row.name for row in frappe.get_all('DocType', fields=['name', 'module', 'istable'])
            if not row.istable and get_module_app(row.module) in applications]
    def include(name, parent=None):
        if name in scope:
            if applications is not None and parent:
                previous = scope[name] if isinstance(scope[name], tuple) else (scope[name],)
                scope[name] = tuple(dict.fromkeys((*previous, parent)))
            elif scope[name] != parent:
                _reject()
            return
        scope[name] = parent
        meta = frappe.get_meta(name)
        # Native query metadata must be ready before restricting business SQL.
        if not meta.issingle and not meta.is_virtual:
            frappe.db.get_table_columns(name)
        from frappe.permissions import get_rights
        get_rights(name)
        if not meta.istable and applications is None:
            frappe.has_permission(name, 'read', throw=True)
        for field in meta.get_table_fields():
            include(field.options, name)
    for name in documents:
        include(name)
    # All metadata and roles are loaded before entering the restricted database boundary.
    frappe.get_roles()
    originals = {name: getattr(frappe.db, name) for name in ('get_value', 'exists', 'get_single_value')}
    def value(*args, **kwargs):
        if _internal.get():
            return originals['get_value'](*args, **kwargs)
        return _value(originals['get_value'], *args, **kwargs)
    def exists(doctype, filters=None, **kwargs):
        if _internal.get():
            return originals['exists'](doctype, filters, **kwargs)
        return _value(originals['get_value'], doctype, filters, 'name')
    def single(doctype, fieldname, **kwargs):
        if _internal.get():
            return originals['get_single_value'](doctype, fieldname, **kwargs)
        return _value(originals['get_value'], doctype, None, fieldname)
    frappe.db.get_value, frappe.db.exists, frappe.db.get_single_value = value, exists, single
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)
        for name, original in originals.items():
            setattr(frappe.db, name, original)


def decimal_rows(doctype, names, fields):
    """Exact decimal strings for already authorized records; no caller SQL."""
    if not names:
        return []
    _doctype(doctype)
    with _native():
        if len(names) > MAX_ROWS:
            _reject()
        documents = [_check_document(frappe.get_doc(doctype, name)) for name in names]
        for doc in documents:
            _fields(doctype, fields, parenttype=doc.get('parenttype'))
        meta = frappe.get_meta(doctype)
        if any(meta.get_field(f).fieldtype not in ('Currency', 'Float', 'Percent') for f in fields):
            _reject()
        return _decimal_values(doctype, names, fields)


def _decimal_values(doctype, names, fields):
    # Callers have already checked native field names and record read permissions.
    columns = ', '.join(f'CAST(`{f}` AS CHAR) AS `{f}`' for f in fields)
    return frappe.db.sql(f'SELECT name, {columns} FROM `tab{doctype}` WHERE name IN %s',
        (tuple(names),), as_dict=True)


def process_profile_value(name, key):
    """Read one profile value of the active Process contact without granting catalogue access."""
    execution = _effect.get()
    if not execution or not execution.get('session') or type(key) is not str:
        raise frappe.PermissionError(_('An authorized operational execution is required.'))
    token = _scope.set({**(_scope.get() or {}), 'Chat Session': None, 'Chat Contact': None})
    try:
        with _native():
            if frappe.db.get_value('Chat Session', execution['session'], 'contact') != name:
                raise frappe.PermissionError(_('The contact does not belong to the Process session.'))
            doc = frappe.get_doc('Chat Contact', name)
            doc.check_permission('read')
            doc.apply_fieldlevel_read_permissions()
            if not doc.has_permlevel_access_to('parameter_values'):
                raise frappe.PermissionError(_('Contact profile read permission is required.'))
            return _freeze(frappe.parse_json(doc.parameter_values or '{}').get(key))
    finally:
        _scope.reset(token)


_effect = ContextVar('messaging_effect_operations', default=None)
_native_write = ContextVar('messaging_effect_native_write', default=None)
_document_operation = ContextVar('messaging_effect_document_operation', default=None)
_document_guards_installed = False


def _require_write_execution():
    """The executor proof permits requesting native authorization; it grants no CRUD."""
    scope = _effect.get()
    from messaging.security.execution_context import require_automation_process
    if not scope or require_automation_process(scope['path']) != scope or (
        scope['source_type'] != 'Bot Flow'
        or any(not scope.get(key) for key in ('source_name', 'session', 'turn', 'execution_id'))):
        raise frappe.PermissionError(_('An authorized operational execution is required.'))


def _install():
    global _document_guards_installed
    if _document_guards_installed:
        return
    for name, operation in (('insert', 'create'), ('db_insert', 'create'), ('save', 'update'),
                            ('db_update', 'update'), ('submit', 'submit'), ('cancel', 'cancel'), ('delete', 'delete')):
        original = getattr(Document, name)
        @wraps(original)
        def guarded(doc, *args, __original=original, __operation=operation, __name=name, **kwargs):
            if _effect.get() is None:
                return __original(doc, *args, **kwargs)
            if doc.flags.get('ignore_permissions') or kwargs.get('ignore_permissions'):
                raise frappe.PermissionError(_('Native document permissions are required.'))
            inherited = _document_operation.get()
            current = (inherited[1] if inherited and inherited[0] is doc
                and __operation == 'update' and inherited[1] in ('submit', 'cancel') else __operation)
            if current == 'update' and doc.is_new():
                current = 'create'
            if current == 'create' and doc.get('amended_from'):
                doc.check_permission('amend')
            _require_write_execution()
            doc.check_permission('write' if current == 'update' else current)
            # Only the native persistence primitive can open a SQL write scope.
            # Controller hooks remain unable to issue direct SQL, even on target.
            physical = 'create' if __name == 'db_insert' else 'update' if __name == 'db_update' else 'delete'
            token = _native_write.set((doc.doctype, physical, current)) if __name in ('db_insert', 'db_update', 'delete') else None
            action = _document_operation.set((doc, current))
            had_notify = 'notify_update' in doc.flags
            previous_notify = doc.flags.get('notify_update')
            doc.flags.notify_update = False
            try:
                with _native():
                    return __original(doc, *args, **kwargs)
            finally:
                if had_notify:
                    doc.flags.notify_update = previous_notify
                else:
                    doc.flags.pop('notify_update', None)
                _document_operation.reset(action)
                if token is not None:
                    _native_write.reset(token)
        setattr(Document, name, guarded)
    _document_guards_installed = True


@contextmanager
def effect_boundary(execution):
    if _reading.get() or not isinstance(execution, dict) or set(execution) - {
        'path', 'source_type', 'source_name', 'session', 'turn', 'execution_id'} or 'path' not in execution:
        _denied()
    _install_guard()
    _install()
    original = frappe.db.sql
    def sql(query, *args, **kwargs):
        text = str(query).strip()
        if ';' in text.rstrip(';') or re.search(r'(?i)\b(INTO\s+OUTFILE|GET_LOCK|RELEASE_LOCK|SLEEP|BENCHMARK|LOAD_FILE)\b', text):
            _denied()
        if re.match(r'(?is)^(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN)\b', text):
            if not _internal.get():
                _denied()
            verify_read_query(text)
            return original(query, *args, **kwargs)
        native = _native_write.get()
        match = re.match(r'(?is)^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`tab([^`]+)`', text)
        operation = {'INSERT INTO': 'create', 'UPDATE': 'update', 'DELETE FROM': 'delete'}
        if not native or not match or ';' in text.rstrip(';'):
            _denied()
        verb = ' '.join(match[1].upper().split())
        if native[:2] != (match[2], operation[verb]):
            _denied()
        # Accept only the native single-target statement forms.
        suffix = text[match.end():].lstrip()
        expected = r'\(' if verb == 'INSERT INTO' else r'SET\b' if verb == 'UPDATE' else r'WHERE\b'
        if not re.match(expected, suffix, re.I) or re.search(r'(?i)\b(JOIN|UNION|SELECT)\b', suffix):
            _denied()
        _require_write_execution()
        return original(query, *args, **kwargs)
    callbacks = {key: getattr(frappe.db, key) for key in ('before_commit', 'after_commit', 'before_rollback', 'after_rollback')}
    class TechnicalCallbacks:
        def __init__(self, name):
            self.name = name

        def add(self, callback):
            # Frappe invalidates this document's Redis cache after either outcome.
            # No business callback, job or realtime publication is delegated.
            if (self.name not in ('after_commit', 'after_rollback') or not inspect.isfunction(callback)
                or callback.__module__ != 'frappe.model.document'
                or callback.__qualname__ != 'clear_document_cache.<locals>.clear_in_redis'):
                _denied()
            from messaging.security.permissions import verify_loaded_method
            verify_loaded_method(callback)
            captured = inspect.getclosurevars(callback).nonlocals
            active = _document_operation.get()
            if (not active or captured.get('doctype') != active[0].doctype
                or captured.get('name') != active[0].name):
                _denied()
            callbacks[self.name].add(callback)
    token = _effect.set(execution)
    external = _reading.set(True)
    previous_control = frappe.db._disable_transaction_control
    frappe.db._disable_transaction_control = True
    frappe.db.sql = sql
    for key in callbacks:
        setattr(frappe.db, key, TechnicalCallbacks(key))
    try:
        yield
    finally:
        frappe.db.sql = original
        frappe.db._disable_transaction_control = previous_control
        for key, value in callbacks.items():
            setattr(frappe.db, key, value)
        _reading.reset(external)
        _effect.reset(token)


_reading = ContextVar('messaging_business_read', default=False)
_guard_installed = False


def _denied():
    raise frappe.PermissionError(_('Business read capabilities cannot perform effects.'))


def _audit(event, args):
    if not _reading.get():
        return
    if (event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.connect', 'socket.bind', 'socket.sendto', 'subprocess.Popen', 'os.system',
        'os.remove', 'os.rename', 'os.mkdir', 'os.rmdir', 'os.putenv')
        or event.startswith(('os.exec', 'os.spawn', 'ctypes.'))):
        _denied()
    if event == 'open':
        mode, flags = args[1:3]
        if (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (isinstance(flags, int) and flags & 0x643):
            _denied()


def _install_guard():
    global _guard_installed
    if not _guard_installed:
        sys.addaudithook(_audit)
    from requests.sessions import Session
    targets = [(frappe, name) for name in ('enqueue', 'sendmail', 'publish_realtime', 'set_user')]
    targets.append((Session, 'request'))
    for owner, name in targets:
        original = getattr(owner, name)
        if getattr(original, '__messaging_effect_guard__', None) is True:
            continue
        @wraps(original)
        def protected(*args, __original=original, **kwargs):
            if _reading.get():
                _denied()
            return __original(*args, **kwargs)
        protected.__messaging_effect_guard__ = True
        setattr(owner, name, protected)
    _guard_installed = True


@contextmanager
def read_boundary():
    """Deny database writes and external effects before they occur, not by rollback."""
    _install_guard()
    original = frappe.db.sql
    def sql(query, *args, **kwargs):
        text = str(query).strip()
        text = re.sub(r'/\*.*?\*/|--[^\n]*', '', text, flags=re.S).strip()
        if _scope.get() is not None:
            if not _internal.get():
                _denied()
            verify_read_query(text)
        if (not re.match(r'(?is)^(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN)\b', text)
            or ';' in text.rstrip(';')
            or re.search(r'(?i)\b(INTO|GET_LOCK|RELEASE_LOCK|SLEEP|BENCHMARK|LOAD_FILE)\b', text)):
            _denied()
        return original(query, *args, **kwargs)
    managers = {key: getattr(frappe.db, key) for key in ('before_commit', 'after_commit', 'before_rollback', 'after_rollback')}
    class NoCallbacks:
        def add(self, *args, **kwargs):
            _denied()
    previous_control = frappe.db._disable_transaction_control
    frappe.db._disable_transaction_control = True
    frappe.db.sql = sql
    for key in managers:
        setattr(frappe.db, key, NoCallbacks())
    @contextmanager
    def suspend():
        """Runtime delegates only the registered callback outside the handler guard."""
        frappe.db.sql = original
        frappe.db._disable_transaction_control = previous_control
        for key, value in managers.items():
            setattr(frappe.db, key, value)
        token = _reading.set(False)
        try:
            yield
        finally:
            _reading.reset(token)
            frappe.db.sql = sql
            frappe.db._disable_transaction_control = True
            for key in managers:
                setattr(frappe.db, key, NoCallbacks())
    token = _reading.set(True)
    try:
        yield suspend
    finally:
        _reading.reset(token)
        frappe.db.sql = original
        frappe.db._disable_transaction_control = previous_control
        for key, value in managers.items():
            setattr(frappe.db, key, value)
