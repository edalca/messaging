"""Private scoped execution; no HTTP endpoints or event-controlled identities."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import frappe
from frappe import _
from messaging.security.permissions import is_system_manager
from messaging.security.service_identity import SERVICE_USER, require_messaging_service_user

_automatic = ContextVar("messaging_automatic_operation", default=False)
_ingress = ContextVar("messaging_authenticated_adapter", default=None)
_process = ContextVar("messaging_authorized_process", default=None)


def is_automation():
	return _automatic.get() and frappe.session.user == SERVICE_USER


def automatic_operation(method):
	"""Enter automatic engine work only from an authenticated adapter or manager."""
	@wraps(method)
	def run(*args, **kwargs):
		if not is_automation() and not is_system_manager():
			frappe.throw(_("Messaging operator permission is required."), frappe.PermissionError)
		with _automation_context():
			return method(*args, **kwargs)
	return run


@contextmanager
def _automation_context():
	"""Only trusted internal call sites enter; preserve Frappe's request-local state."""
	identity = require_messaging_service_user()
	attributes = ("session", "cache", "form_dict", "jenv_restricted", "jenv_unrestricted",
		"role_permissions", "new_doc_templates", "user_perms", "lang")
	missing = object()
	previous = {key: getattr(frappe.local, key, missing) for key in attributes}
	# set_user mutates session in place, including sid/data. Keep the caller's
	# original object intact, including its HTTP authentication and CSRF state.
	frappe.local.session = frappe._dict(previous["session"])
	token = _automatic.set(True)
	try:
		frappe.set_user(identity)
		yield identity
	finally:
		for key, value in previous.items():
			if value is missing:
				if hasattr(frappe.local, key):
					delattr(frappe.local, key)
			else:
				setattr(frappe.local, key, value)
		_automatic.reset(token)


@contextmanager
def _authenticated_adapter(provider, account):
	"""Dispatcher-only scope, entered after registered protocol verification."""
	token = _ingress.set((provider, account))
	try:
		with _automation_context():
			yield
	finally:
		_ingress.reset(token)


def require_authenticated_adapter(provider, account):
	if not is_automation() or _ingress.get() != (provider, account):
		frappe.throw(_("An authenticated channel adapter context is required."), frappe.PermissionError)
	require_messaging_service_user()


@contextmanager
def authorized_process(path, *, source_type=None, source_name=None, session=None, turn=None, execution_id=None):
	"""Executor-only proof: enter after configured source, signature and mode checks."""
	if is_automation():
		require_messaging_service_user()
	token = _process.set(dict(path=path, source_type=source_type, source_name=source_name,
		session=session, turn=turn, execution_id=execution_id))
	try:
		yield
	finally:
		_process.reset(token)


def require_automation_process(path):
	context = _process.get()
	if not is_automation() or not context or context['path'] != path:
		frappe.throw(_("An authorized automatic Process context is required."), frappe.PermissionError)
	return dict(context)


def automatic_job(method):
	"""Restrict scheduler work to its exact native job or an explicit manager run."""
	path = method.__module__ + "." + method.__name__
	@wraps(method)
	def run(*args, **kwargs):
		job = getattr(frappe.local, "job", None)
		if not is_automation() and not is_system_manager():
			if not job or job.method != path:
				frappe.throw(_("A trusted Messaging job is required."), frappe.PermissionError)
		with _automation_context():
			return method(*args, **kwargs)
	return run
