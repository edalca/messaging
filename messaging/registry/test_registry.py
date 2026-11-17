"""Functional registry boundaries and reusable real owner-hook fixtures."""
from contextlib import contextmanager, ExitStack
from importlib import import_module
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import frappe
from werkzeug.wrappers import Request
from messaging.registry import _catalog, register_registry


@contextmanager
def owner_application(source, *, prefix='preset_owner'):
    """Load an ephemeral owner package, mocking installation metadata only."""
    app = prefix + '_' + frappe.generate_hash(length=8).lower()
    with tempfile.TemporaryDirectory() as directory, ExitStack() as guards:
        root = Path(directory) / app
        root.mkdir()
        for name, content in {'__init__.py': '', 'hooks.py': '',
                'contribution.py': source + '\nfrom messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import provision_presets\n\ndef provision(declarations):\n    return provision_presets(app="APP", declarations=declarations)\n'}.items():
            (root / name).write_text(content.replace('APP', app))
        sys.path.insert(0, directory)
        installed = frappe.get_installed_apps
        app_path = frappe.get_app_path
        hooks = frappe.get_hooks
        installed_apps = list(installed()) + [app]
        guards.enter_context(patch('frappe.get_installed_apps', return_value=installed_apps))
        guards.enter_context(patch('messaging.security.permissions.installed_applications', return_value=installed_apps))
        guards.enter_context(patch('frappe.get_app_path', side_effect=lambda name, *parts, **kw:
            str(root.joinpath(*parts)) if name == app else app_path(name, *parts, **kw)))
        guards.enter_context(patch('frappe.get_hooks', side_effect=lambda hook=None, *args, **kw:
            getattr(import_module(app + '.hooks'), hook, []) if kw.get('app_name') == app else hooks(hook, *args, **kw)))
        try:
            yield app, import_module(app + '.contribution')
        finally:
            sys.path.remove(directory)
            for name in list(sys.modules):
                if name == app or name.startswith(app + '.'):
                    sys.modules.pop(name)


class TestRegistry(unittest.TestCase):
    def test_three_categories_and_direct_public_facade(self):
        from messaging import registry
        from messaging.registrations import processes, ai_providers, communication_providers
        from messaging.registrations.processes.contracts import BotProcess, NotifyProcess
        from messaging.registrations.ai_providers.contracts import AIProvider
        from messaging.registrations.communication_providers.contracts import CommunicationProvider
        self.assertIs(processes.BotProcess, BotProcess)
        self.assertIs(processes.NotifyProcess, NotifyProcess)
        self.assertFalse(hasattr(registry, 'Process'))
        self.assertFalse(hasattr(registry, 'ProcessRequest'))
        self.assertFalse(hasattr(registry, 'ProcessResult'))
        self.assertFalse(hasattr(registry, 'ProcessNoResult'))
        self.assertIs(ai_providers.AIProvider, AIProvider)
        self.assertIs(communication_providers.CommunicationProvider, CommunicationProvider)
        self.assertEqual(set(_catalog.declaration_types()), {'processes', 'ai_providers', 'communication_providers'})
        self.assertFalse(hasattr(registry, 'BotFlowDefinition'))
        self.assertFalse(hasattr(registry, 'EvaluationTemplateDefinition'))

    def test_read_queries_do_not_materialize_or_execute_business(self):
        with patch('frappe.model.document.Document.db_insert', side_effect=AssertionError('No writes')):
            declarations = _catalog.application_declarations('messaging')
        self.assertEqual({key: len(value) for key, value in declarations.items()},
            dict(processes=3, ai_providers=0, communication_providers=1))
        self.assertIsNone(_catalog._catalog.get())

    def test_registration_requires_authorized_hook_and_server_context(self):
        from messaging.presets.register import register
        with self.assertRaises(frappe.ValidationError):
            register_registry(app='messaging')
        with patch.object(frappe.local, 'request', Request.from_values(), create=True):
            with self.assertRaises(frappe.PermissionError):
                register()
            self.assertEqual(len(_catalog.application_declarations('messaging')['processes']), 3)

    def test_registry_rejects_document_configuration(self):
        with owner_application('''from messaging.registry import register_registry

def register():
    return register_registry(app='APP', definitions=({
        'key': 'messaging_greeting', 'name': 'Default Greeting', 'process': 'messaging.greeting',
    },))
''') as (app, module):
            with patch('frappe.get_hooks', wraps=frappe.get_hooks) as hooks:
                original = hooks._mock_wraps
                hooks.side_effect = lambda hook=None, *a, **kw: [app + '.contribution.register'] if hook == 'messaging_registry' and kw.get('app_name') == app else original(hook, *a, **kw)
                with self.assertRaisesRegex(frappe.ValidationError, 'Unknown Code-First'):
                    module.register()
        self.assertIsNone(_catalog._catalog.get())

    def test_failed_collection_restores_its_context(self):
        with patch.object(_catalog, '_hooks', side_effect=RuntimeError('Synthetic failure')):
            with self.assertRaises(RuntimeError):
                _catalog.application_declarations('messaging')
        self.assertIsNone(_catalog._catalog.get())
