"""Shared administrative subject checks; domain permissions remain with their owners."""
import importlib
import inspect
from functools import lru_cache
from pathlib import Path
from types import CodeType

import frappe
from frappe import _


@lru_cache(maxsize=64)
def _compiled_functions(source, filename):
	"""Compile without executing; bytes form the cache key, never timestamps."""
	def codes(code):
		for value in code.co_consts:
			if isinstance(value, CodeType):
				yield value
				yield from codes(value)
	return tuple(codes(compile(source, filename, 'exec', dont_inherit=True)))


def verify_loaded_method(method, *, inspection=None):
	"""Refuse stale functions, including already loaded resolvable own helpers."""
	import inspect
	import sys
	targets = [method]
	if inspection:
		for symbol in inspection['functions']:
			module, separator, name = symbol.rpartition('.')
			loaded = sys.modules.get(module)
			target = vars(loaded).get(name) if loaded else None
			if inspect.isfunction(target):
				targets.append(target)
	for target in targets:
		target = inspect.unwrap(target)
		if not inspect.isfunction(target):
			raise frappe.ValidationError(_('Process must be a Python function with verifiable source code.'))
		source = Path(inspect.getsourcefile(target))
		if target.__code__ not in _compiled_functions(source.read_bytes(), target.__code__.co_filename):
			raise frappe.ValidationError(_('Process code changed. Reload the application process before testing.'))


def is_system_manager(user=None):
	user = user or frappe.session.user
	return bool(user and user != "Guest" and "System Manager" in frappe.get_roles(user))


def require_system_manager(message=None):
	if not is_system_manager():
		frappe.throw(message or _("System Manager permission is required."), frappe.PermissionError)


def require_registration_owner(app, caller, *, administrative=True):
    """Require a lifecycle/Bench caller whose module and source belong to the owner."""
    if administrative:
        require_system_manager()
    if administrative and getattr(frappe.local, 'request', None) is not None:
        raise frappe.PermissionError(_('Registration requires a server lifecycle or Bench call.'))
    if not isinstance(app, str) or app not in frappe.get_installed_apps() or app not in installed_applications():
        raise frappe.ValidationError(_('Registration owner must be an installed application.'))
    root = Path(frappe.get_app_path(app)).resolve()
    module = caller.f_globals.get('__name__', '')
    file = Path(caller.f_code.co_filename).resolve()
    if not (module == app or module.startswith(app + '.')) or not file.is_relative_to(root):
        raise frappe.PermissionError(_('Definitions must originate in their owning application.'))


def require_provider_function(function, app, *args, **kwargs):
    """Verify an installed owner's public function and its operation signature.

    Provider operations perform I/O and cannot use the Process AST policy. They
    share its loaded-source verifier, while retaining their own call signatures.
    """
    import inspect
    import re
    import sys
    if (not inspect.isfunction(function) or inspect.iscoroutinefunction(function)
        or inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function)
        or app not in frappe.get_installed_apps() or app not in installed_applications()):
        frappe.throw(_('Provider operations require installed owner function references.'))
    module, name = function.__module__, function.__name__
    source = inspect.getsourcefile(function)
    if (not module.startswith(app + '.') or not re.fullmatch(r'[A-Za-z]\w*(?:\.[A-Za-z]\w*)*', module + '.' + name) or '<locals>' in function.__qualname__
        or not source or not Path(source).resolve().is_relative_to(Path(frappe.get_app_path(app)).resolve())
        or getattr(sys.modules.get(module), name, None) is not function):
        frappe.throw(_('Provider operation must belong to its owning application.'))
    try:
        inspect.signature(function).bind(*args, **kwargs)
    except TypeError:
        frappe.throw(_('Invalid provider operation signature.'))
    verify_loaded_method(function)
    return module + '.' + name


def _invalid_hook(detail):
    raise frappe.ValidationError(_(detail))


def application_hooks(app, hook):
    """Resolve only real per-application hooks and functions inside the installed package."""
    if app not in frappe.get_installed_apps() or app not in installed_applications():
        _invalid_hook('Registration owner must be an installed application.')
    hooks = frappe.get_hooks(hook, app_name=app)

    def paths(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [path for item in value for path in paths(item)]
        _invalid_hook('Invalid application preset hooks.')

    hooks = paths(hooks)
    result = {}
    root = Path(frappe.get_app_path(app)).resolve()
    for path in hooks:
        if not isinstance(path, str) or not path.startswith(app + '.') or path in result:
            _invalid_hook('Preset hooks must belong to their installed application.')
        module_name, _, name = path.rpartition('.')
        module = importlib.import_module(module_name)
        function = getattr(module, name, None)
        source = getattr(module, '__file__', None)
        expected = root.joinpath(*module_name.split('.')[1:])
        if (not source or Path(source).resolve() not in (expected.with_suffix('.py'), expected / '__init__.py')
            or not inspect.isfunction(function) or function.__module__ != module_name
            or function.__name__ != name or function.__qualname__ != name
            or Path(inspect.getsourcefile(function)).resolve() != Path(source).resolve()):
            _invalid_hook('Preset registration must use the installed owner hook function.')
        if require_provider_function(function, app) != path:
            _invalid_hook('Preset registration must use the installed owner hook function.')
        result[path] = function
    return result


def require_hook_caller(caller, hook):
    """Verify the exact loaded owner hook before accepting documentary contributions."""
    app = caller.f_globals.get('__name__', '').split('.')[0]
    require_registration_owner(app, caller)
    for path, function in application_hooks(app, hook).items():
        if function.__code__ is caller.f_code and function.__module__ == caller.f_globals.get('__name__'):
            return app, path
    raise frappe.PermissionError(_('Registration must originate in its authorized application hook.'))


def installed_applications():
    """Read site ownership without request or defaults caches retained by workers."""
    return frappe.parse_json(frappe.db.get_value('DefaultValue',
        {'parent': '__global', 'defkey': 'installed_apps'}, 'defvalue') or '[]')
