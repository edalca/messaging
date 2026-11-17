"""Bounded static source inspection and Process capability policy."""
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import frappe
from frappe import _
from messaging.communication.doctype.process_definition.process_definition import validate_process_method_path


POLICY_VERSION = 1
PROHIBITIONS = {
    'FRAPPE_THROW': dict(name='Desk exception', pattern=('frappe.throw',), category='presentation', operation='call',
        reason='Processes must return JSON results instead of Desk messages.', severity='error', action='reject'),
    'FRAPPE_MSGPRINT': dict(name='Desk message', pattern=('frappe.msgprint',), category='presentation', operation='call',
        reason='Processes must return JSON results instead of Desk messages.', severity='error', action='reject'),
    'FRAPPE_MESSAGE_LOG': dict(name='Message log mutation', pattern=('frappe.local.message_log', 'frappe.message_log'),
        category='presentation', operation='mutation', reason='Processes must not modify request messages.', severity='error', action='reject'),
    'FRAPPE_SERVER_MESSAGES': dict(name='HTTP messages mutation', pattern=('_server_messages',),
        category='presentation', operation='mutation', reason='Processes must not write Desk response messages.', severity='error', action='reject'),
    # These helpers call msgprint/throw in frappe.utils.messages in Frappe v16.
    'FRAPPE_UI_OUTPUT': dict(name='Desk presentation helper', pattern=('frappe.toast', 'frappe.throw_permission_error'),
        category='presentation', operation='call', reason='Processes must not produce Desk presentation effects.', severity='error', action='reject'),
}
MAX_FUNCTIONS = 256
MAX_MODULES = 64
NATIVE_PROCESS_CAPABILITIES = frozenset({
    'messaging.communication.doctype.process_definition.execution.record_human_request',
})
_CACHE = {}


def policy_fingerprint():
    return hashlib.sha256(json.dumps(dict(version=POLICY_VERSION, rules=PROHIBITIONS,
        max_functions=MAX_FUNCTIONS, max_modules=MAX_MODULES,
        native_capabilities=sorted(NATIVE_PROCESS_CAPABILITIES)), sort_keys=True).encode()).hexdigest()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def inspect_process(path, app_path=None):
    """Inspect one app's resolvable functions. Cache only while all source bytes match.

    A missing main function blocks approval. Unresolved helpers/dynamic calls are
    explicit partial coverage and do not imply a confirmed policy violation.
    """
    if app_path is None:
        import sys
        from importlib.machinery import PathFinder
        # Resolve only the top-level package location; do not import its __init__.
        spec = PathFinder.find_spec(path.split('.')[0], sys.path)
        if spec is None or not spec.origin:
            return dict(status='partial', inspectable=False, policy=policy_fingerprint(), sources={},
                functions=[], findings=[], limitations=['Process application source is unavailable'])
        app_path = Path(spec.origin).parent
    root = Path(app_path).resolve()
    key = (str(root), path, policy_fingerprint())
    cached = _CACHE.get(key)
    if cached:
        try:
            if all(_hash(Path(file).read_bytes()) == digest for file, digest in cached['sources'].items()):
                return deepcopy(cached)
        except OSError:
            pass
    result = _Inspector(path, root).run()
    if result['inspectable']:
        if len(_CACHE) >= 128:
            _CACHE.clear()
        _CACHE[key] = result
    return deepcopy(result)


class _Inspector:
    def __init__(self, path, root):
        self.path, self.root = path, root
        self.app = path.split('.')[0]
        self.modules, self.sources, self.seen = {}, {}, set()
        self.findings, self.limitations = [], set()

    def module(self, name):
        if name in self.modules:
            return self.modules[name]
        if not name.startswith(self.app + '.') and name != self.app:
            return None
        candidate = self.root.joinpath(*name.split('.')[1:])
        file = candidate.with_suffix('.py')
        if not file.is_file():
            file = candidate / '__init__.py'
        if len(self.modules) >= MAX_MODULES:
            self.limitations.add('Module inspection bound reached')
            return None
        try:
            file = file.resolve()
            file.relative_to(self.root)
            data = file.read_bytes()
            tree = ast.parse(data, filename=str(file))
        except (OSError, ValueError, SyntaxError):
            self.limitations.add('Source unavailable: ' + name)
            # Track absent candidates so a newly created helper invalidates the cache.
            self.sources[str(file)] = 'missing'
            return None
        self.sources[str(file)] = _hash(data)
        functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        aliases = {name: name for name in functions}
        aliases.update({name: name for name in ('setattr', 'delattr')})
        module = dict(name=name, file=file, functions=functions, aliases=aliases)
        self.modules[name] = module
        for node in tree.body:
            self.bind(node, module, aliases)
        return module

    def bind(self, node, module, aliases):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                package = module['name'].split('.') if module['file'].name == '__init__.py' else module['name'].split('.')[:-1]
                base = '.'.join(package[:len(package) - node.level + 1] + ([base] if base else []))
            for alias in node.names:
                aliases[alias.asname or alias.name] = base + '.' + alias.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = self.symbol(node.value, aliases)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = value or '?'

    def symbol(self, node, aliases):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.symbol(node.value, aliases)
            return base + '.' + node.attr if base else None
        if isinstance(node, ast.Subscript):
            base = self.symbol(node.value, aliases)
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                return (base or '') + '.' + node.slice.value
            return base
        return None

    def finding(self, rule, operation, node, module, function):
        self.findings.append(dict(rule=rule, operation=operation, file=str(module['file']),
            line=node.lineno, method=self.path, function=module['name'] + '.' + function))

    def mutation(self, symbol, node, module, function):
        if not symbol:
            return
        if any(symbol == prefix or symbol.startswith(prefix + '.') for prefix in PROHIBITIONS['FRAPPE_MESSAGE_LOG']['pattern']):
            self.finding('FRAPPE_MESSAGE_LOG', symbol, node, module, function)
        if '_server_messages' in symbol.split('.'):
            self.finding('FRAPPE_SERVER_MESSAGES', symbol, node, module, function)

    def follow(self, symbol, module):
        if not symbol:
            return
        if symbol in NATIVE_PROCESS_CAPABILITIES:
            # Runtime-owned authorization and fixed effects are a native boundary.
            # Keep its source in inspection evidence and the capability closure.
            self.module(symbol.rpartition('.')[0])
            return
        if symbol in module['functions']:
            return self.visit(module, symbol)
        if symbol.startswith(self.app + '.'):
            name, _, function = symbol.rpartition('.')
            target = self.module(name)
            if target and function in target['functions']:
                return self.visit(target, function)
            self.limitations.add('Unresolved own callable: ' + symbol)

    def visit(self, module, function):
        identity = (module['name'], function)
        if identity in self.seen:
            return
        if len(self.seen) >= MAX_FUNCTIONS:
            self.limitations.add('Function inspection bound reached')
            return
        self.seen.add(identity)
        node = module['functions'][function]
        aliases = dict(module['aliases'])
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            aliases[arg.arg] = '?'
        # A lexical walk avoids treating bodies of unrelated nested definitions as
        # immediately called. Resolvable nested helpers are inspected on reference.
        nested = {n.name: n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        local_module = dict(module, functions={**module['functions'], **nested})
        for name in nested:
            aliases[name] = name
        def walk(current):
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                return
            if isinstance(current, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                targets = current.targets if isinstance(current, (ast.Assign, ast.Delete)) else [current.target]
                for target in targets:
                    self.mutation(self.symbol(target, aliases), current, module, function)
            self.bind(current, module, aliases)
            if isinstance(current, ast.Call):
                symbol = self.symbol(current.func, aliases)
                canonical = symbol.replace('frappe.utils.messages.', 'frappe.') if symbol else None
                for rule, definition in PROHIBITIONS.items():
                    if definition.get('operation') == 'call' and canonical in definition['pattern']:
                        self.finding(rule, symbol + '()', current, module, function)
                # Mutating methods and setattr/delattr on the message containers.
                if isinstance(current.func, ast.Attribute) and current.func.attr in (
                    'append', 'extend', 'insert', 'clear', 'pop', 'remove', 'update', 'setdefault', '__setitem__', '__delitem__'):
                    self.mutation(self.symbol(current.func.value, aliases), current, module, function)
                    if any(k.arg == '_server_messages' for k in current.keywords):
                        self.finding('FRAPPE_SERVER_MESSAGES', symbol or 'update', current, module, function)
                    for arg in current.args:
                        if isinstance(arg, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == '_server_messages' for k in arg.keys):
                            self.finding('FRAPPE_SERVER_MESSAGES', symbol or 'update', current, module, function)
                if symbol in ('setattr', 'delattr', 'builtins.setattr', 'builtins.delattr') and len(current.args) >= 2:
                    key = current.args[1]
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        self.mutation((self.symbol(current.args[0], aliases) or '') + '.' + key.value, current, module, function)
                self.follow(symbol, local_module)
                # Callback functions passed to own helpers are part of the scope.
                for arg in [*current.args, *(k.value for k in current.keywords)]:
                    self.follow(self.symbol(arg, aliases), local_module)
                if symbol and symbol.startswith('frappe.'):
                    self.limitations.add('Framework internals are outside inspection scope')
                if not symbol or symbol.startswith('?') or ('.' in symbol
                    and not symbol.startswith(('frappe.', self.app + '.'))):
                    self.limitations.add('Dynamic or external call: ' + (symbol or ast.unparse(current.func))[:180])
            for child in ast.iter_child_nodes(current):
                walk(child)
        for statement in node.body:
            walk(statement)

    def run(self):
        module_name, _, function = self.path.rpartition('.')
        module = self.module(module_name)
        inspectable = bool(module and function in module['functions'])
        if inspectable:
            self.visit(module, function)
        else:
            self.limitations.add('Main Process source is unavailable')
        findings = [dict(items) for items in sorted({tuple(item.items()) for item in self.findings})]
        status = 'violations' if findings else 'partial' if self.limitations else 'clear'
        return dict(status=status, inspectable=inspectable, policy=policy_fingerprint(), sources=self.sources,
            functions=sorted('.'.join(item) for item in self.seen), findings=findings, limitations=sorted(self.limitations))


def require_compliant(path):
    result = inspect_process(validate_process_method_path(path))
    if not result['inspectable']:
        raise frappe.ValidationError(_('Process source could not be inspected. Validation is required before execution.'))
    if result['findings']:
        item = result['findings'][0]
        raise frappe.ValidationError(_('Process validation rejected: {0}. Operation: {1}. File: {2}. Line: {3}. Function: {4}. Processes must return JSON results instead of Desk messages.').format(
            item['rule'], item['operation'], item['file'], item['line'], item['function']))
    return result
