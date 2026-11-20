"""AI capability contracts and installed owner resolution."""
from dataclasses import replace
import unittest
from unittest.mock import patch
import frappe
from messaging.registry import _catalog
from messaging.registry import ai_providers as wire
from messaging.registry.ai_providers import internal_provider


class TestAIProviders(unittest.TestCase):
    def test_installed_provider_identity_and_contract_version(self):
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import SYNTHETIC
        original = _catalog.application_declarations
        def declared(app, **kwargs):
            if app == SYNTHETIC.app:
                return dict(original(app, **kwargs), ai_providers=(SYNTHETIC,))
            return original(app, **kwargs)
        with patch.object(_catalog, 'application_declarations', side_effect=declared):
            definitions = _catalog.application_declarations(SYNTHETIC.app)['ai_providers']
            self.assertEqual(len(definitions), 1)
            definition = definitions[0]
            self.assertEqual(internal_provider(definition.provider_id, definition.app), definition)
            with self.assertRaises(ValueError):
                replace(definition, registration_schema_version='invalid')
            self.assertEqual(definition.schema_version, '1')

    def test_external_administration_does_not_require_registry_during_validation(self):
        from messaging.chatbot.doctype.ai_provider.ai_provider import adapter_parameters
        with patch('messaging.registry.ai_providers.internal_provider', side_effect=AssertionError('Document schema is independent')):
            self.assertTrue(adapter_parameters())

    def test_closed_wire_rejects_duplicates(self):
        with self.assertRaises(wire.InvalidInterpretation):
            wire.strict_json('{"schema_version":"1","schema_version":"1"}')
