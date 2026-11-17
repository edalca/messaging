"""Verified, transient declaration discovery and category-driven lifecycle dispatch."""
from collections.abc import Collection
from contextvars import ContextVar
import importlib
import inspect
from pathlib import Path

import frappe
from frappe import _

_catalog = ContextVar('messaging_registry', default=None)
_DECLARATION_TYPES = {}
_DECLARATION_VALIDATORS = {}
_DECLARATION_SYNCHRONIZERS = {}
_DECLARATION_RETIRERS = {}
KINDS = ('processes', 'ai_providers', 'communication_providers')


def register_declaration_type(kind, cls, *, validate=None, synchronize=None, retire=None):
    if kind not in KINDS:
        invalid('Invalid declaration kind.')
    _DECLARATION_TYPES[kind] = cls
    if validate is not None:
        _DECLARATION_VALIDATORS[kind] = validate
    if synchronize is not None:
        _DECLARATION_SYNCHRONIZERS[kind] = synchronize
    if retire is not None:
        _DECLARATION_RETIRERS[kind] = retire


def invalid(detail):
    raise frappe.ValidationError(_('Invalid registry declaration: {0}').format(_(detail)))


def _accepted(expected):
    return expected if isinstance(expected, tuple) else (expected,)


def _matches(expected, declaration):
    return type(declaration) in _accepted(expected)


def declaration_types():
    return {kind: _DECLARATION_TYPES[kind] for kind in KINDS}


def _key(kind, declaration):
    if kind in ('processes', 'communication_providers'):
        return declaration.id
    return declaration.provider_id


def _hooks(app):
    from messaging.security.permissions import application_hooks
    return application_hooks(app, 'messaging_registry')


def _owner_root(file):
    from messaging.security.permissions import installed_applications
    path = Path(file).resolve()
    for app in installed_applications():
        root = Path(frappe.get_app_path(app)).resolve()
        if path.is_relative_to(root) and path.is_file() and path.suffix == '.py':
            scope = _catalog.get()
            if scope is not None and scope['app'] != app:
                invalid('Registry registration must belong to its installed application.')
            return app, root
    invalid('Registry registration must belong to its installed application.')


def load_declarations(register_file, kind):
    """Read conventional owner modules; declaration factories never execute operations."""
    return tuple(row for row, source in _load_declarations(register_file, kind))


def _load_declarations(register_file, kind):
    """Isolate invalid modules only in a lifecycle or availability read scope."""
    if kind not in declaration_types():
        invalid('Invalid declaration kind.')
    app, root = _owner_root(register_file)
    result = []
    files = sorted({* (root / 'presets' / kind).glob('*.py'),
        * (root / 'providers' / kind).glob('*.py')})
    for file in files:
        if file.name == '__init__.py':
            continue
        try:
            result.extend(_load_declaration(app, root, file, kind))
        except Exception:
            scope = _catalog.get()
            if scope is None or not scope['tolerate_errors']:
                raise
            scope['failures'][kind].add(str(file.relative_to(root)))
    return tuple(result)


def _load_declaration(app, root, file, kind):
    expected = declaration_types().get(kind)
    if expected is None:
        invalid('Invalid declaration kind.')
    if not file.resolve().is_relative_to(root) or not file.stem.isidentifier():
        invalid('Preset files must remain inside their application.')
    module_name = app + '.' + '.'.join(file.relative_to(root).with_suffix('').parts)
    module = importlib.import_module(module_name)
    from messaging.security.permissions import verify_loaded_method
    if kind == 'processes':
        declaration = getattr(module, 'process', None)
        for function in (getattr(declaration, 'definition', None), getattr(declaration, 'handler', None)):
            if (not inspect.isfunction(function) or function.__module__ != module_name
                or function.__qualname__ != function.__name__
                or Path(inspect.getsourcefile(function) or '').resolve() != file.resolve()):
                invalid('Process modules must define their own definition and handler.')
            verify_loaded_method(function)
        if not _matches(expected, declaration):
            invalid(_('Unexpected declaration type in {0}').format(file.relative_to(root)))
    else:
        factory = getattr(module, 'definition', None)
        if (Path(module.__file__).resolve() != file.resolve() or not inspect.isfunction(factory)
            or factory.__module__ != module_name or factory.__qualname__ != 'definition'
            or Path(inspect.getsourcefile(factory)).resolve() != file.resolve()):
            invalid('Declaration modules must define their own definition() function.')
        verify_loaded_method(factory)
        declaration = factory()
        if not _matches(expected, declaration):
            invalid(_('Unexpected declaration type in {0}').format(file.relative_to(root)))
    return ((declaration, str(file.relative_to(root))),)


def _collect(scope, *, discover, definitions, caller):
    """Both public input forms converge before validation or owner persistence."""
    if type(discover) is not bool:
        invalid('discover must be a boolean.')
    if not isinstance(definitions, Collection) or isinstance(definitions, (str, bytes, dict)):
        invalid('definitions must be a finite collection of typed declarations.')
    types = declaration_types()
    root = Path(frappe.get_app_path(scope['app'])).resolve()
    rows = []
    if discover:
        for kind in types:
            if scope.get('kinds') is not None and kind not in scope['kinds']:
                continue
            for declaration, source in _load_declarations(caller.f_code.co_filename, kind):
                rows.append((kind, declaration, source))
    source = str(Path(caller.f_code.co_filename).resolve().relative_to(root))
    for declaration in definitions:
        kind = next((kind for kind, expected in types.items() if _matches(expected, declaration)), None)
        if kind is None:
            invalid('Unknown Code-First declaration type.')
        if scope.get('kinds') is None or kind in scope['kinds']:
            rows.append((kind, declaration, source))
    for kind, declaration, source in rows:
        key = _key(kind, declaration)
        identity = (kind, key)
        if identity in scope['sources']:
            invalid(_('Duplicate preset key: {0}').format(key))
        scope['sources'][identity] = (scope['app'], source)
        try:
            declaration.__post_init__()
            _DECLARATION_VALIDATORS[kind](scope['app'], (declaration,))
        except Exception:
            if not scope['tolerate_errors']:
                raise
            scope['failures'][kind].add(source)
            continue
        scope['declarations'][kind].append(declaration)


def _finalize(scope):
    """Validate typed relations and owner contracts without writing or executing callbacks."""
    app = scope['app']
    groups = scope['declarations']
    for kind in groups:
        groups[kind] = tuple(sorted(groups[kind], key=lambda row: _key(kind, row)))
        _DECLARATION_VALIDATORS[kind](app, groups[kind])
    scope['collecting'] = False


def _scope(app, *, kinds=None, tolerate_errors=False):
    if kinds is not None and (not isinstance(kinds, (tuple, frozenset)) or not set(kinds) <= declaration_types().keys()):
        invalid('Invalid declaration category selection.')
    return dict(app=app, kinds=kinds, sources={}, collecting=True, hooks=set(),
        tolerate_errors=tolerate_errors, failures={kind: set() for kind in declaration_types()},
        declarations={kind: [] for kind in declaration_types()})


def application_declarations(app, *, kinds=None, tolerate_errors=False):
    """Obtain the complete verified owner hook declarations with no materialization."""
    outer = _catalog.get()
    if outer is not None and outer['app'] == app:
        if outer['collecting']:
            invalid('Registry hooks cannot recursively query their own declarations.')
        return outer['declarations']
    return _read_application(app, kinds=kinds, tolerate_errors=tolerate_errors)['declarations']


def _read_application(app, *, kinds=None, tolerate_errors=False):
    scope = _scope(app, kinds=kinds, tolerate_errors=tolerate_errors)
    token = _catalog.set(scope)
    try:
        try:
            hooks = _hooks(app)
        except Exception:
            if not tolerate_errors:
                raise
            hooks = {}
            for kind in scope['failures']:
                scope['failures'][kind].add('*')
        for path, function in hooks.items():
            before = {kind: list(rows) for kind, rows in scope['declarations'].items()}
            sources = dict(scope['sources'])
            try:
                function()
                if path not in scope['hooks']:
                    invalid('Registry hooks must declare through register_registry.')
            except Exception:
                if not tolerate_errors:
                    raise
                scope['declarations'] = before
                scope['sources'] = sources
                # Explicit declarations have no recoverable identity if their hook
                # cannot import. Protect only that owner's unresolved catalogue.
                for kind in scope['failures']:
                    scope['failures'][kind].add('*')
        _finalize(scope)
        return scope
    finally:
        _catalog.reset(token)


def register_application(*, app, discover, definitions, caller):
    """Authorized hooks declare in a private read scope; direct lifecycle calls synchronize."""
    from messaging.security.permissions import require_registration_owner
    outer = _catalog.get()
    reading = outer is not None and outer['app'] == app and outer['collecting']
    require_registration_owner(app, caller, administrative=not reading)
    hooks = _hooks(app)
    active = next((path for path, function in hooks.items() if function.__code__ is caller.f_code
        and function.__module__ == caller.f_globals.get('__name__')
        and Path(inspect.getsourcefile(function)).resolve() == Path(caller.f_code.co_filename).resolve()), None)
    if active is None:
        invalid('Registry registration must use the installed owner hook function.')
    if outer is not None and not reading:
        invalid('Registry registration must remain inside its owner declaration scope.')
    if reading:
        if active in outer['hooks']:
            invalid('Registry hooks must register their complete definitions once.')
        outer['hooks'].add(active)
        outer['caller'] = caller
        _collect(outer, discover=discover, definitions=definitions, caller=caller)
        return
    scope = _scope(app)
    token = _catalog.set(scope)
    try:
        scope['hooks'].add(active)
        _collect(scope, discover=discover, definitions=definitions, caller=caller)
        for path, function in hooks.items():
            if path != active:
                function()
                if path not in scope['hooks']:
                    invalid('Registry hooks must declare through register_registry.')
        _finalize(scope)
        from messaging.common.locking import document_savepoint
        groups = scope['declarations']
        with document_savepoint():
            result = {}
            for kind, synchronize in _DECLARATION_SYNCHRONIZERS.items():
                result[kind] = synchronize(app=app, definitions=groups[kind], caller=caller)
            for kind, retire in _DECLARATION_RETIRERS.items():
                result[kind]['unavailable'] = retire(app, {_key(kind, row) for row in groups[kind]})
            return result
    finally:
        _catalog.reset(token)


def declaration_source(kind, key):
    scope = _catalog.get()
    return scope['sources'].get((kind, key), ('', ''))[1] if scope else ''


def reject_declaration(kind, key):
    """Discovery can reject one Process without losing other owner declarations."""
    scope = _catalog.get()
    if scope is None or not scope['tolerate_errors']:
        return False
    scope['failures'][kind].add(declaration_source(kind, key))
    scope['declarations'][kind] = tuple(row for row in scope['declarations'][kind] if _key(kind, row) != key)
    return True


def tolerates_errors():
    scope = _catalog.get()
    return bool(scope and scope['tolerate_errors'])


def synchronize_application(app):
    """Revalidate current contracts and persist healthy declarations and failures."""
    from messaging.common.locking import document_savepoint
    scope = _read_application(app, tolerate_errors=True)
    token = _catalog.set(scope)
    try:
        with document_savepoint():
            result = {}
            caller = scope.get('caller')
            for kind, synchronize in _DECLARATION_SYNCHRONIZERS.items():
                definitions = scope['declarations'][kind]
                result[kind] = synchronize(app=app, definitions=definitions, caller=caller) if caller else {}
            result['incompatible_declarations'] = {kind: sorted(paths) for kind, paths in scope['failures'].items() if paths}
            for kind, paths in result['incompatible_declarations'].items():
                for source in paths:
                    diagnostic = f'{app}: {kind}: {source}: incompatible_or_unavailable'
                    if not frappe.db.exists('Error Log', {'method': 'Messaging Registry', 'error': diagnostic}):
                        frappe.get_doc(dict(doctype='Error Log', method='Messaging Registry',
                            error=diagnostic, metadata='{}')).insert(ignore_permissions=True)
            for kind, retire in _DECLARATION_RETIRERS.items():
                result[kind]['unavailable'] = retire(app, {_key(kind, row) for row in scope['declarations'][kind]})
            return result
    finally:
        _catalog.reset(token)
