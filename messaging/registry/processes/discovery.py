"""Owner registration, Code-First discovery and verified source policy."""

import ast
import frappe
import hashlib
import inspect
import json
import subprocess
import sys
import threading
from copy import deepcopy
from frappe import _
from messaging.common.primitives import _freeze
from messaging.registry.processes.contracts import PROCESS_CONTRACT_VERSION, BotProcess, NotifyProcess, validate_definition_signature, validate_handler_signature, validate_process_schema_v1
from messaging.communication.doctype.process_definition.process_definition import (
    PROCESS_TIMEOUT_SECONDS,
    _discovery_deadline as execution_deadline,
    _registry_write,
    _testing,
    controlled_reads,
    process_messages,
    read_boundary,
    resolve_definition,
    registered_contract,
    registered_snapshot,
    retire_definitions,
    validate_process_method_path,
)
from .context import _ProcessContext
from messaging.security.permissions import require_registration_owner as _require_owner, verify_loaded_method
from pathlib import Path
from .inspection import inspect_process, require_compliant, policy_fingerprint



def function_reference(method, app, *, process_type, role):
    """Resolve an actual owner-defined function, including its loaded source."""
    if not inspect.isfunction(method):
        raise frappe.ValidationError(_('Process capabilities require function references.'))
    path = validate_process_method_path(method.__module__ + '.' + method.__name__)
    if path.split('.')[0] != app or frappe.get_attr(path) is not method:
        raise frappe.ValidationError(_('Process function must belong to its owning application.'))
    _method(path, frappe._dict(application=app, process_type=process_type), role=role)
    return path


def validate_registered_method(path, app, *, process_type, role):
    method = frappe.get_attr(validate_process_method_path(path))
    function_reference(method, app, process_type=process_type, role=role)
    return method


def _register_processes(*, app, processes, caller):
    """Reconcile one application catalogue. Discovery never executes business callbacks."""
    _require_owner(app, caller)
    if not isinstance(processes, (list, tuple)):
        raise frappe.ValidationError(_('Process definitions must be a list.'))
    normalized = {}
    for definition in processes:
        if type(definition) not in (BotProcess, NotifyProcess):
            raise frappe.ValidationError(_('Invalid Process declaration.'))
        definition.__post_init__()
        definition_path = function_reference(definition.definition, app, process_type=definition.type, role='definition')
        handler_path = function_reference(definition.handler, app, process_type=definition.type, role='handler')
        identifier, label = definition.id, definition.label
        if identifier in normalized:
            raise frappe.ValidationError(_('Invalid or duplicate Process ID.'))
        normalized[identifier] = dict(process_id=identifier, process_name=label,
            process_type=definition.type, application=app, definition_method=definition_path,
            method=handler_path, status='Enabled', contract_version=PROCESS_CONTRACT_VERSION)
        try:
            # Discovery validates the complete Code-First contract before any
            # catalogue mutation, including updates that would otherwise be no-ops.
            revision = _uncached_capability_revision(frappe._dict(normalized[identifier]))
        except Exception as exc:
            from messaging.registry._catalog import reject_declaration
            if reject_declaration('processes', identifier):
                normalized.pop(identifier)
                continue
            raise frappe.ValidationError(_('Process {0} registration rejected: {1}').format(
                identifier, str(exc))) from exc
        values = normalized[identifier]
        values.update(registration_revisions(frappe._dict(values), revision))
        values['registered_contract'] = canonical_json(revision['declaration']['contract'])
        values['registered_snapshot'] = canonical_json(revision)
    # One durable catalogue lock serializes ownership claims, including new IDs.
    frappe.db.get_value('DocType', 'Process Definition', 'name', for_update=True)
    existing = {d.name: d for d in frappe.db.sql('SELECT * FROM `tabProcess Definition` FOR UPDATE', as_dict=True)}
    for identifier, values in list(normalized.items()):
        old = existing.get(identifier)
        if old and (old.application != app or old.process_type != values['process_type']):
            from messaging.registry._catalog import reject_declaration
            if reject_declaration('processes', identifier):
                normalized.pop(identifier)
                continue
            if old.application != app:
                raise frappe.PermissionError(_('Process ownership is immutable.'))
            raise frappe.ValidationError(_('Process type is immutable; register a new Process ID.'))
    summary = dict(created=0, updated=0, unchanged=0, retired=0)
    savepoint = 'registry_' + frappe.generate_hash(length=12)
    frappe.db.savepoint(savepoint)
    try:
        with _registry_write():
            for identifier, values in normalized.items():
                old = existing.get(identifier)
                if old and all(old.get(key) == value for key, value in values.items()):
                    summary['unchanged'] += 1
                    continue
                doc = frappe.get_doc('Process Definition', identifier) if old else frappe.new_doc('Process Definition')
                previous_method = old.method if old else None
                doc.update(values)
                doc.save()
                if previous_method and previous_method != values['method']:
                    for doctype in ('Bot Flow', 'Notification Rule'):
                        frappe.db.sql(f"""update `tab{doctype}` set process_method=%s
                            where process_definition=%s and process_method=%s""",
                            (values['method'], identifier, previous_method))
                summary['updated' if old else 'created'] += 1
            summary['retired'] = sum(old.application == app and identifier not in normalized
                and old.status != 'Unavailable' for identifier, old in existing.items())
            retire_definitions(app, set(normalized))
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return summary


def discover_contract(definition, *, language=None):
    """Run only the owner handler. No source document or business callback is read."""
    contract, callback, _response = _discover_contract(definition, language=language)
    return contract, callback


def _discover_contract(definition, *, language=None):
    """Keep discovery bounded on web threads without running business callbacks."""
    if threading.current_thread() is not threading.main_thread():
        # Signals cannot interrupt a web thread. A fresh interpreter owns its
        # signal timer and the parent also enforces a hard subprocess deadline.
        _method(definition.definition_method, definition, role='definition')
        _method(definition.method, definition, role='handler')
        request = dict(site=frappe.local.site, sites_path=str(Path(frappe.local.sites_path).resolve()),
            user=frappe.session.user, language=language, lang=frappe.local.lang,
            definition={key: definition.get(key) for key in (
                'application', 'process_id', 'process_type', 'definition_method', 'method')})
        try:
            result = subprocess.run([sys.executable, '-c',
                'from messaging.registry.processes.discovery import _discovery_worker; _discovery_worker()'],
                input=json.dumps(request), text=True, capture_output=True,
                timeout=PROCESS_TIMEOUT_SECONDS, check=True)
            payload = json.loads(result.stdout)
            if set(payload) == {'error'} and isinstance(payload['error'], str):
                raise frappe.ValidationError(payload['error'])
            if set(payload) != {'contract', 'callback', 'response'}:
                raise ValueError('Invalid discovery response')
            callback = _method(payload['callback'], definition, role='handler')
            response = validate_process_schema_v1(payload['response'])
            if response != dict(version=1, status='test', recipient='', data={}, message=''):
                raise ValueError('Invalid discovery response')
            return payload['contract'], callback, response
        except (subprocess.SubprocessError, ValueError, TypeError, KeyError, OSError) as exc:
            raise frappe.ValidationError(_('The Process contract could not be verified. Review its discovery before validating or activating the Flow.')) from exc
    handler = _method(definition.definition_method, definition, role='definition')
    callback = _method(definition.method, definition, role='handler')
    context = _ProcessContext(definition.process_id, definition.process_type,
        language or '', True, _freeze({}))
    from frappe.translate import get_all_translations
    if context.language:
        get_all_translations(context.language)
    get_all_translations(frappe.local.lang)
    token = _testing.set(True)
    try:
        with process_messages(), execution_deadline(), controlled_reads([]), read_boundary():
            context.declare(handler)
            result = context.response()
        validate_process_schema_v1(result)
        if result != dict(version=1, status='test', recipient='', data={}, message=''):
            raise frappe.ValidationError(_('Invalid discovery response'))
        contract = context.contract()
        if context._state['callback'] is not None:
            raise frappe.ValidationError(_('Process discovery must not bind a business handler.'))
        return contract, callback, result
    finally:
        _testing.reset(token)


def _discovery_worker():
    """Private discovery subprocess: same site and actor, no persisted writes."""
    request = json.loads(sys.stdin.read())
    frappe.init(site=request['site'], sites_path=request['sites_path'])
    frappe.connect()
    try:
        frappe.set_user(request['user'])
        frappe.local.lang = request['lang']
        try:
            contract, callback, response = _discover_contract(frappe._dict(request['definition']), language=request['language'])
        except (frappe.ValidationError, frappe.PermissionError) as exc:
            sys.stdout.write(json.dumps(dict(error=str(exc))))
        else:
            sys.stdout.write(json.dumps(dict(contract=contract,
                callback=callback.__module__ + '.' + callback.__name__, response=response)))
    finally:
        frappe.db.rollback()
        frappe.destroy()


def declaration(definition):
    """Discover the contract; native permissions authorize business document access."""
    contract, callback = discover_contract(definition)
    required = getattr(frappe.get_module(definition.application + '.hooks'), 'required_apps', [])
    dependencies = sorted(set(required) - {'messaging', definition.application})
    if any(app not in frappe.get_installed_apps() for app in dependencies):
        raise frappe.PermissionError(_('Process dependencies must be installed required applications.'))
    return dict(functions={'definition': definition.definition_method,
        'handler': callback.__module__ + '.' + callback.__name__},
        dependencies=dependencies, contract=contract)


def document_modules(definition, spec):
    """Include native controllers of the owner's installed domain dependencies."""
    from frappe.model.base_document import get_controller
    from frappe.modules.utils import get_module_app
    applications = {definition.application, *spec['dependencies']}
    return {get_controller(row.name).__module__ for row in frappe.get_all('DocType', fields=['name', 'module'])
        if get_module_app(row.module) in applications}


def _source_digest(module, source):
    """Keep app release metadata out of Process revisions while verifying executable initializers."""
    if module == 'messaging':
        tree = ast.parse(source)
        if len(tree.body) == 1 and isinstance(assignment := tree.body[0], ast.Assign):
            if (len(assignment.targets) == 1 and isinstance(target := assignment.targets[0], ast.Name)
                and target.id == '__version__' and isinstance(assignment.value, ast.Constant)
                and isinstance(assignment.value.value, str)):
                source = b'__version__ = ""\n'
    return hashlib.sha256(source).hexdigest()


def source_closure(definition, spec, *, platform_modules=()):
    """Fingerprint the full static source closure in declared application roots."""
    roots = {app: Path(frappe.get_app_path(app)).resolve()
        for app in (definition.application, *spec.get('dependencies', []))}
    queue = [path.rpartition('.')[0] for path in spec.get('functions', {}).values()]
    if not platform_modules:
        queue.extend(document_modules(definition, spec))
        queue.extend(app + ".hooks" for app in roots)
    result = {}
    while queue:
        module = queue.pop()
        if (
            module in result or module.split('.')[0] not in roots
            or module in platform_modules
            or (platform_modules and module.startswith(
                ('messaging.common', 'messaging.security', 'messaging.registry')
            ))
        ):
            continue
        root = roots[module.split('.')[0]]
        file = root.joinpath(*module.split('.')[1:]).with_suffix('.py')
        if not file.is_file():
            file = root.joinpath(*module.split('.')[1:]) / '__init__.py'
        if not file.is_file():
            continue
        if not file.resolve().is_relative_to(root) or len(result) >= 512:
            raise frappe.ValidationError(_('Process capability source closure is invalid or too large.'))
        source = file.read_bytes()
        result[module] = dict(path=str(file), digest=_source_digest(module, source))
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                package = module.split('.') if file.name == '__init__.py' else module.split('.')[:-1]
                base = '.'.join(package[:len(package) - node.level + 1] + ([node.module] if node.module else [])) if node.level else node.module or ''
                queue.append(base)
                queue.extend(base + '.' + alias.name for alias in node.names)
        # Package initialization is also part of Python's import boundary.
        queue.extend('.'.join(module.split('.')[:i]) for i in range(1, len(module.split('.'))))
    return result


from messaging.communication.doctype.process_definition.revisions import registration_revisions, canonical_json


def _uncached_capability_revision(definition):
    from messaging.communication.doctype.process_definition.revisions import runtime_revision, runtime_sources
    spec = declaration(definition)
    platform = runtime_sources()
    sources = source_closure(definition, spec)
    # The deployment manifest still protects shared runtime code at invocation.
    # Approval revisions identify the registered implementation and its contract.
    implementation = (
        source_closure(definition, spec, platform_modules={*platform, 'messaging.hooks'})
        if definition.application == 'messaging' else sources
    )
    implementation_sources = sorted(implementation)
    sources.update(platform)
    verify_source_closure(sources)
    return dict(declaration=spec, sources=sources, implementation_sources=implementation_sources,
        runtime=runtime_revision(), policy=policy_fingerprint())


def verify_registered_revision(definition):
    """Execution and explicit validation reject deployments absent from the catalogue.

    Compare the registered source manifest and loaded functions without invoking
    the declaration, walking the application tree, or building another contract.
    """
    from messaging.communication.doctype.process_definition.revisions import runtime_revision
    snapshot = registered_snapshot(definition)
    from messaging.security.permissions import installed_applications
    if any(app not in installed_applications() for app in snapshot['declaration']['dependencies']):
        raise frappe.PermissionError(_('Process dependencies must be installed required applications.'))
    if ('implementation_sources' not in snapshot or snapshot['runtime'] != runtime_revision() or snapshot['policy'] != policy_fingerprint()
        or registration_revisions(definition, snapshot)['capability_revision'] != definition.capability_revision):
        raise frappe.ValidationError(_('Process code changed. Synchronize its owning application.'))
    for module, source in snapshot['sources'].items():
        path = Path(frappe.get_app_path(module.split('.')[0])).joinpath(*module.split('.')[1:]).with_suffix('.py')
        if not path.is_file():
            path = Path(frappe.get_app_path(module.split('.')[0])).joinpath(*module.split('.')[1:]) / '__init__.py'
        if not path.is_file() or _source_digest(module, path.read_bytes()) != source['digest']:
            raise frappe.ValidationError(_('Process code changed. Synchronize its owning application.'))
    verify_source_closure(snapshot['sources'])
    return snapshot


def _method(path, definition, *, role):
    method = frappe.get_attr(path)
    if (not inspect.isfunction(method) or method.__module__ != path.rpartition('.')[0]
        or method.__closure__ or method.__name__ != path.rpartition('.')[2]
        or not Path(inspect.getfile(method)).resolve().is_relative_to(Path(frappe.get_app_path(definition.application)).resolve())):
        raise frappe.ValidationError(_('Process capability must be an owner-defined function with a verified signature.'))
    if role == 'definition':
        validate_definition_signature(method)
    elif role == 'handler':
        validate_handler_signature(method, definition.process_type)
    else:
        raise frappe.ValidationError(_('Process capability must be an owner-defined function with a verified signature.'))
    verify_loaded_method(method)
    return method


def verify_source_closure(closure):
    """Verify loaded functions, source-defined class members and literal constants."""
    for module, source in closure.items():
        loaded = vars(frappe.get_module(module))
        tree = ast.parse(Path(source['path']).read_bytes())
        targets = list(loaded.values())
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                value = loaded.get(node.name)
                if inspect.isclass(value) and value.__module__ == module:
                    targets.extend(vars(value).get(item.name) for item in node.body if isinstance(item, ast.FunctionDef))
                    annotations = {item.target.id for item in node.body if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)}
                    if hasattr(value, '__annotate__'):
                        from annotationlib import get_annotations, Format
                        actual = get_annotations(value, format=Format.STRING)
                    else:
                        actual = getattr(value, '__annotations__', {})
                    from typing import is_typeddict
                    if is_typeddict(value):
                        for base in node.bases:
                            if isinstance(base, ast.Name):
                                annotations.update(getattr(loaded.get(base.id), '__annotations__', {}))
                    if annotations and annotations != set(actual):
                        raise frappe.ValidationError(_('Process capability code changed. Reload the application process before testing.'))
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id.isupper() and not node.targets[0].id.startswith('_')):
                try:
                    literal = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                if loaded.get(node.targets[0].id) != literal:
                    raise frappe.ValidationError(_('Process capability code changed. Reload the application process before testing.'))
        for value in targets:
            if isinstance(value, (classmethod, staticmethod)):
                value = value.__func__
            if inspect.isfunction(value) and value.__module__ == module:
                verify_loaded_method(value)


def consumer_contract(process_id, *, app, expected_type):
    """Read an available registered Process owned by the declaring application."""
    process = resolve_definition(process_id, expected_type=expected_type)
    process.check_permission('read')
    if process.application != app:
        raise frappe.ValidationError(_('The Process must belong to the preset application.'))
    return registered_contract(process)
