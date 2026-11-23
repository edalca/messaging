"""Persistent catalogue exclusively maintained by the public Process Registry."""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
import keyword
import math
import os
import re
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

_DEADLINE_EXCEEDED = "Process execution deadline exceeded"
_DEADLINE_REARM_SECONDS = 0.05
_DB_TIMEOUT_ERRNOS = frozenset({1969, 3024})
_DB_LOCK_WAIT_ERRNO = 1205

_active_deadline = ContextVar("messaging_execution_deadline", default=None)
_in_db_call = ContextVar("messaging_deadline_in_db", default=False)
_guard_lock = threading.Lock()
_deadline_lock = threading.Lock()
_deadline_condition = threading.Condition(_deadline_lock)
_deadline_entries = {}
_deadline_expired = set()
_deadline_monitoring_active = False
_deadline_tool_id = None
_deadline_watcher = None
_deadline_token_seq = 0
_deadline_internal_codes = set()
_sleep_guard_installed = False
_original_sleep = time.sleep


def _get_deadline_tool_id():
	global _deadline_tool_id
	if _deadline_tool_id is not None:
		return _deadline_tool_id
	monitoring = getattr(sys, "monitoring", None)
	if monitoring is None:
		return None
	for candidate in (3, 4):
		if monitoring.get_tool(candidate) is None:
			try:
				monitoring.use_tool_id(candidate, "messaging_execution_deadline")
			except ValueError:
				continue
			monitoring.register_callback(candidate, monitoring.events.INSTRUCTION, _on_deadline_event)
			_deadline_tool_id = candidate
			return _deadline_tool_id
	return None


def _set_deadline_monitoring(enabled):
	global _deadline_monitoring_active
	if enabled == _deadline_monitoring_active:
		return
	tool_id = _get_deadline_tool_id()
	if tool_id is None:
		return
	sys.monitoring.set_events(tool_id, sys.monitoring.events.INSTRUCTION if enabled else 0)
	_deadline_monitoring_active = enabled


def _disarm_current_thread(tid=None):
	if tid is None:
		tid = threading.get_ident()
	with _deadline_lock:
		_deadline_expired.discard(tid)
		rearm_at = time.monotonic() + _DEADLINE_REARM_SECONDS
		for entry in _deadline_entries.values():
			if entry[2] == tid and entry[3] < rearm_at:
				entry[3] = rearm_at
		if not _deadline_expired:
			_set_deadline_monitoring(False)


def _on_deadline_event(code, _offset):
	tid = threading.get_ident()
	if tid not in _deadline_expired or code in _deadline_internal_codes or _in_db_call.get():
		return
	deadline = _active_deadline.get()
	if deadline is None or time.monotonic() < deadline:
		return
	with _deadline_lock:
		if tid not in _deadline_expired:
			return
		_deadline_expired.discard(tid)
		rearm_at = time.monotonic() + _DEADLINE_REARM_SECONDS
		for entry in _deadline_entries.values():
			if entry[2] == tid and entry[3] < rearm_at:
				entry[3] = rearm_at
		if not _deadline_expired:
			_set_deadline_monitoring(False)
	raise TimeoutError(_DEADLINE_EXCEEDED)


def _deadline_watcher_loop():
	while True:
		with _deadline_condition:
			while not _deadline_entries:
				_deadline_condition.wait()
			now = time.monotonic()
			next_wake = None
			has_expired = False
			dead_keys = []
			for key, entry in _deadline_entries.items():
				if not entry[4].is_alive():
					dead_keys.append((key, entry[2]))
					continue
				if entry[3] <= now:
					_deadline_expired.add(entry[2])
					entry[3] = now + _DEADLINE_REARM_SECONDS
					has_expired = True
				if next_wake is None or entry[3] < next_wake:
					next_wake = entry[3]
			for key, dead_tid in dead_keys:
				_deadline_entries.pop(key, None)
				if not any(e[2] == dead_tid for e in _deadline_entries.values()):
					_deadline_expired.discard(dead_tid)
			if not _deadline_expired:
				_set_deadline_monitoring(False)
			elif has_expired:
				_set_deadline_monitoring(True)
			wait_for = max(0.001, next_wake - time.monotonic()) if next_wake is not None else None
			_deadline_condition.wait(timeout=wait_for)


def _ensure_deadline_watcher():
	global _deadline_watcher
	if _deadline_watcher is None or not _deadline_watcher.is_alive():
		_deadline_watcher = threading.Thread(
			target=_deadline_watcher_loop,
			name="messaging-deadline-watcher",
			daemon=True,
		)
		_deadline_watcher.start()


def _install_sleep_guard():
	global _sleep_guard_installed
	with _guard_lock:
		if _sleep_guard_installed and getattr(time.sleep, "__messaging_deadline_guard__", False):
			return
		original = _original_sleep

		@wraps(original)
		def _bounded_sleep(seconds):
			deadline = _active_deadline.get()
			if deadline is None:
				return original(seconds)
			if isinstance(seconds, (int, float)) and seconds < 0:
				return original(seconds)
			remaining = deadline - time.monotonic()
			if remaining <= 0:
				_disarm_current_thread()
				raise TimeoutError(_DEADLINE_EXCEEDED)
			if seconds > remaining:
				original(remaining)
				_disarm_current_thread()
				raise TimeoutError(_DEADLINE_EXCEEDED)
			result = original(seconds)
			if time.monotonic() >= deadline:
				_disarm_current_thread()
				raise TimeoutError(_DEADLINE_EXCEEDED)
			return result

		_bounded_sleep.__messaging_deadline_guard__ = True
		_deadline_internal_codes.add(_bounded_sleep.__code__)
		time.sleep = _bounded_sleep
		_sleep_guard_installed = True


def _is_db_timeout_error(exc, deadline):
	current = exc
	seen = set()
	while current is not None and id(current) not in seen:
		seen.add(id(current))
		args = getattr(current, "args", None)
		errno = args[0] if args else None
		if errno in _DB_TIMEOUT_ERRNOS:
			return True
		if errno == _DB_LOCK_WAIT_ERRNO and time.monotonic() >= deadline:
			return True
		current = current.__cause__ or current.__context__
	return False


def _before_deadline_fork():
	_deadline_lock.acquire()


def _after_deadline_fork_parent():
	_deadline_lock.release()


def _after_deadline_fork_child():
	global _deadline_lock, _deadline_condition, _deadline_watcher, _deadline_monitoring_active
	_deadline_lock = threading.Lock()
	_deadline_condition = threading.Condition(_deadline_lock)
	_deadline_entries.clear()
	_deadline_expired.clear()
	_deadline_watcher = None
	if _deadline_tool_id is not None and _deadline_monitoring_active:
		sys.monitoring.set_events(_deadline_tool_id, 0)
	_deadline_monitoring_active = False


if hasattr(os, "register_at_fork"):
	os.register_at_fork(
		before=_before_deadline_fork,
		after_in_parent=_after_deadline_fork_parent,
		after_in_child=_after_deadline_fork_child,
	)


class _ExecutionDeadline:
	"""Thread-isolated deadline over Python bytecode, time.sleep, and MariaDB queries.

	Avoids process-global SIGALRM/ITIMER_REAL mutation so main threads (sync, RQ)
	and secondary threads (Gunicorn gthread) execute concurrently without interfering
	with each other or with enclosing worker timers.
	"""

	__slots__ = (
		"_timeout",
		"_deadline",
		"_ctx_token",
		"_tid",
		"_entry_token",
		"_use_trace_fallback",
		"_previous_trace",
		"_caller_frame",
		"_previous_caller_trace",
		"_db",
		"_original_sql",
		"_previous_db_limits",
		"_configured_stmt_limit",
	)

	def __init__(self, timeout):
		self._timeout = float(timeout)
		self._deadline = None
		self._ctx_token = None
		self._tid = None
		self._entry_token = None
		self._use_trace_fallback = False
		self._previous_trace = None
		self._caller_frame = None
		self._previous_caller_trace = None
		self._db = None
		self._original_sql = None
		self._previous_db_limits = None
		self._configured_stmt_limit = None

	def _apply_db_limits(self, remaining):
		db = self._db
		if db is None or getattr(db, "_conn", None) is None:
			return
		db_token = _in_db_call.set(True)
		try:
			if self._previous_db_limits is None:
				row = self._original_sql(
					"SELECT @@SESSION.max_statement_time, @@SESSION.innodb_lock_wait_timeout"
				)[0]
				self._previous_db_limits = (float(row[0] or 0.0), int(row[1] or 50))
			prev_stmt, prev_lock = self._previous_db_limits
			stmt_limit = max(0.001, round(remaining, 3))
			if prev_stmt > 0:
				stmt_limit = min(stmt_limit, prev_stmt)
			lock_limit = max(1, min(prev_lock, math.ceil(remaining)))
			self._original_sql(
				"SET SESSION max_statement_time = %s, innodb_lock_wait_timeout = %s",
				(stmt_limit, lock_limit),
			)
			self._configured_stmt_limit = stmt_limit
		finally:
			_in_db_call.reset(db_token)

	def _enter_db(self, remaining):
		db = getattr(frappe.local, "db", None)
		if db is None or getattr(db, "db_type", None) != "mariadb" or not callable(getattr(db, "sql", None)):
			return
		self._db = db
		self._original_sql = db.sql
		if getattr(db, "_conn", None) is not None:
			self._apply_db_limits(remaining)

		def _bounded_sql(*args, **kwargs):
			left = self._deadline - time.monotonic()
			if left <= 0:
				_disarm_current_thread()
				raise TimeoutError(_DEADLINE_EXCEEDED)
			if getattr(db, "_conn", None) is not None:
				if (
					self._configured_stmt_limit is None
					or left < self._configured_stmt_limit - 0.1
					or (self._configured_stmt_limit > 1.0 and left < self._configured_stmt_limit * 0.5)
				):
					self._apply_db_limits(left)
			db_token = _in_db_call.set(True)
			try:
				result = self._original_sql(*args, **kwargs)
			except Exception as exc:
				if _is_db_timeout_error(exc, self._deadline):
					_disarm_current_thread()
					raise TimeoutError(_DEADLINE_EXCEEDED) from exc
				raise
			finally:
				_in_db_call.reset(db_token)
			if time.monotonic() >= self._deadline:
				_disarm_current_thread()
				raise TimeoutError(_DEADLINE_EXCEEDED)
			return result

		db.sql = _bounded_sql

	def _exit_db(self):
		db = self._db
		if db is None:
			return
		if self._original_sql is not None:
			db.sql = self._original_sql
		if self._previous_db_limits is not None and getattr(db, "_conn", None) is not None:
			db_token = _in_db_call.set(True)
			try:
				self._original_sql(
					"SET SESSION max_statement_time = %s, innodb_lock_wait_timeout = %s",
					self._previous_db_limits,
				)
			except Exception:
				pass
			finally:
				_in_db_call.reset(db_token)
		self._db = None
		self._original_sql = None
		self._previous_db_limits = None
		self._configured_stmt_limit = None

	def __enter__(self):
		global _deadline_token_seq
		_install_sleep_guard()
		now = time.monotonic()
		if self._timeout <= 0:
			raise TimeoutError(_DEADLINE_EXCEEDED)
		outer = _active_deadline.get()
		deadline = min(outer, now + self._timeout) if outer is not None else (now + self._timeout)
		remaining = deadline - now
		if remaining <= 0:
			raise TimeoutError(_DEADLINE_EXCEEDED)
		self._deadline = deadline
		self._tid = threading.get_ident()
		self._enter_db(remaining)
		try:
			with _deadline_condition:
				tool_id = _get_deadline_tool_id()
				if tool_id is not None:
					_ensure_deadline_watcher()
					_deadline_token_seq += 1
					self._entry_token = _deadline_token_seq
					_deadline_entries[(self._tid, self._entry_token)] = [
						deadline,
						self._entry_token,
						self._tid,
						deadline,
						threading.current_thread(),
					]
					_deadline_condition.notify()
				else:
					self._use_trace_fallback = True
			if self._use_trace_fallback:
				self._previous_trace = sys.gettrace()
				fired = False

				def _trace(frame, event, arg):
					nonlocal fired
					if not fired and frame.f_code not in _deadline_internal_codes and not _in_db_call.get() and time.monotonic() >= deadline:
						fired = True
						raise TimeoutError(_DEADLINE_EXCEEDED)
					if self._previous_trace is not None:
						self._previous_trace(frame, event, arg)
					return _trace

				caller = sys._getframe(1)
				self._caller_frame = caller
				self._previous_caller_trace = caller.f_trace
				caller.f_trace = _trace
				sys.settrace(_trace)
			self._ctx_token = _active_deadline.set(deadline)
		except Exception:
			self.__exit__(None, None, None)
			raise
		return self

	def __exit__(self, exc_type, exc, tb):
		if self._ctx_token is not None:
			_active_deadline.reset(self._ctx_token)
			self._ctx_token = None
		if self._use_trace_fallback:
			sys.settrace(self._previous_trace)
			if self._caller_frame is not None:
				self._caller_frame.f_trace = self._previous_caller_trace
				self._caller_frame = None
		elif self._entry_token is not None:
			with _deadline_condition:
				_deadline_entries.pop((self._tid, self._entry_token), None)
				now = time.monotonic()
				still_expired = False
				for entry in _deadline_entries.values():
					if entry[2] == self._tid and entry[0] <= now:
						still_expired = True
						break
				if not still_expired:
					_deadline_expired.discard(self._tid)
					if not _deadline_expired:
						_set_deadline_monitoring(False)
			self._entry_token = None
		self._exit_db()
		return False


_deadline_internal_codes.update(
	{
		_ExecutionDeadline.__enter__.__code__,
		_ExecutionDeadline.__exit__.__code__,
		_ExecutionDeadline._apply_db_limits.__code__,
		_ExecutionDeadline._enter_db.__code__,
		_ExecutionDeadline._exit_db.__code__,
		_disarm_current_thread.__code__,
		_on_deadline_event.__code__,
		_set_deadline_monitoring.__code__,
		_get_deadline_tool_id.__code__,
		_ensure_deadline_watcher.__code__,
		_install_sleep_guard.__code__,
		_is_db_timeout_error.__code__,
	}
)
_install_sleep_guard()


def deadline_scope(timeout=None):
	"""Return a thread-isolated deadline context manager for Process operations."""
	return _ExecutionDeadline(PROCESS_TIMEOUT_SECONDS if timeout is None else timeout)


def is_process_test():
	return _testing.get()


def _discovery_deadline():
	"""Bound synchronous discovery calls on any worker thread."""
	return deadline_scope(PROCESS_TIMEOUT_SECONDS)


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


_discovering = ContextVar('messaging_process_discovering', default=False)


@contextmanager
def discovery_reads():
    """Protect declaration discovery from document data, including cached reads."""
    with _guard_lock:
        for name in ('get_doc', 'get_cached_doc', 'get_single', 'get_all', 'get_list', 'has_permission'):
            original = getattr(frappe, name)
            if getattr(original, '__messaging_discovery_guard__', None) is True:
                continue

            @wraps(original)
            def guarded(*args, __original=original, **kwargs):
                if _discovering.get():
                    _denied()
                return __original(*args, **kwargs)

            guarded.__messaging_discovery_guard__ = True
            setattr(frappe, name, guarded)

    token = _discovering.set(True)
    try:
        yield
    finally:
        _discovering.reset(token)


def decimal_rows(doctype, names, fields):
    """Read exact decimal text for a handler-selected set of document names."""
    if not names or not fields:
        return []
    if len(names) > 5000:
        raise frappe.ValidationError(_('Business read exceeds the record limit.'))
    meta = frappe.get_meta(doctype)
    for name in fields:
        field = meta.get_field(name)
        if not field or field.fieldtype not in ('Currency', 'Float', 'Percent'):
            raise frappe.ValidationError(_('Business decimal reads require numeric fields.'))
    return _decimal_values(meta.name, names, fields)


def _decimal_values(doctype, names, fields):
    # Callers validate field metadata. Document names remain bound values.
    def identifier(value):
        return '`' + value.replace('`', '``') + '`'

    columns = ', '.join(f'CAST({identifier(field)} AS CHAR) AS {identifier(field)}' for field in fields)
    return frappe.db.sql(f'SELECT name, {columns} FROM {identifier("tab" + doctype)} WHERE name IN %s',
        (tuple(names),), as_dict=True)


def process_profile_value(name, key):
    """Read one profile value of the active Process contact without granting catalogue access."""
    execution = _effect.get()
    if not execution or not execution.get('session') or type(key) is not str:
        raise frappe.PermissionError(_('An authorized operational execution is required.'))
    if frappe.db.get_value('Chat Session', execution['session'], 'contact') != name:
        raise frappe.PermissionError(_('The contact does not belong to the Process session.'))
    doc = frappe.get_doc('Chat Contact', name)
    doc.check_permission('read')
    doc.apply_fieldlevel_read_permissions()
    if not doc.has_permlevel_access_to('parameter_values'):
        raise frappe.PermissionError(_('Contact profile read permission is required.'))
    return _freeze(frappe.parse_json(doc.parameter_values or '{}').get(key))


_effect = ContextVar('messaging_effect_operations', default=None)
_native_write = ContextVar('messaging_effect_native_write', default=None)
_document_operation = ContextVar('messaging_effect_document_operation', default=None)
_document_guards_installed = False


def _require_write_execution():
    """Require the exact operational proof before opening a write scope."""
    scope = _effect.get()
    from messaging.security.execution_context import require_automation_process
    if not scope or require_automation_process(scope['path']) != scope or (
        scope['source_type'] != 'Bot Flow'
        or any(not scope.get(key) for key in ('source_name', 'session', 'turn', 'execution_id'))):
        raise frappe.PermissionError(_('An authorized operational execution is required.'))
    return scope


def _install_document_guards():
    global _document_guards_installed
    with _guard_lock:
        if _document_guards_installed:
            return
        physical_operations = {'db_insert': 'create', 'db_update': 'update', 'delete': 'delete'}
        for name in ('insert', 'db_insert', 'save', 'db_update', 'submit', 'cancel', 'delete'):
            original = getattr(Document, name)

            @wraps(original)
            def guarded(doc, *args, __original=original, __physical=physical_operations.get(name), **kwargs):
                if _effect.get() is None:
                    return __original(doc, *args, **kwargs)
                _require_write_execution()
                # Only native persistence opens a SQL write scope. Frappe owns
                # the permission behavior of the selected document operation.
                token = _native_write.set((doc.doctype, __physical)) if __physical else None
                action = _document_operation.set(doc)
                had_notify = 'notify_update' in doc.flags
                previous_notify = doc.flags.get('notify_update')
                doc.flags.notify_update = False
                try:
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
    _install_document_guards()
    original = frappe.db.sql

    def sql(query, *args, **kwargs):
        text = str(query).strip()
        caller = sys._getframe(1)
        # Preserve the native APIs that generate queries without mediating their
        # arguments, results, or permissions. Raw SQL entry points stay blocked.
        native_query = (
            caller.f_globals.get('__name__', '').startswith('frappe.')
            and caller.f_code.co_name not in ('sql_list', 'sql_ddl', 'multisql')
        )
        if (
            ';' in text.rstrip(';')
            or re.search(r'/\*(?:!|M!)', text, re.I)
            or re.search(r'\b(GET_LOCK|RELEASE_LOCK|SLEEP|BENCHMARK|LOAD_FILE)\b', text, re.I)
        ):
            _denied()
        if re.match(r'(?is)^(SELECT|SHOW|DESC|DESCRIBE|EXPLAIN)\b', text):
            if (
                (not native_query and caller.f_code is not _decimal_values.__code__)
                or re.search(r'\bINTO\s+(OUTFILE|DUMPFILE)\b', text, re.I)
            ):
                _denied()
            if re.search(r'(?i)\b(FOR\s+UPDATE|LOCK\s+IN\s+SHARE\s+MODE)\b', text):
                _require_write_execution()
            return original(query, *args, **kwargs)
        native = _native_write.get()
        match = re.match(r'(?is)^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+`tab([^`]+)`', text)
        operation = {'INSERT INTO': 'create', 'UPDATE': 'update', 'DELETE FROM': 'delete'}
        if not native_query or not native or not match:
            _denied()
        verb = ' '.join(match[1].upper().split())
        if native != (match[2], operation[verb]):
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
            if (not active or captured.get('doctype') != active.doctype
                or captured.get('name') != active.name):
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
    with _guard_lock:
        if _guard_installed:
            return
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
        if _discovering.get():
            _denied()
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
