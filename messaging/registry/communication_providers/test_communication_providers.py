"""Channel provider declaration schema and administrative independence."""
from dataclasses import replace
import unittest
from unittest.mock import patch
import frappe
from messaging.registrations.communication_providers import CommunicationProviderParameter, CommunicationCapability, normalize_definitions
from messaging.presets.communication_providers.emulr import definition
from messaging.communication.doctype.communication_provider.communication_provider import validate_parameters


class TestCommunicationProviders(unittest.TestCase):
    def test_native_provider_and_unknown_capabilities(self):
        provider = definition()
        self.assertEqual(normalize_definitions('messaging', (provider,))['EMULR']['id'], 'EMULR')
        self.assertIn(CommunicationCapability.TEXT, provider.capabilities)
        with self.assertRaises(ValueError):
            replace(provider, capabilities=('unsupported',))
        with self.assertRaises(ValueError):
            replace(provider, registration_schema_version='2')

    def test_document_parameter_validation_uses_its_own_schema(self):
        with patch('messaging.registrations.communication_providers.normalize_definitions', side_effect=AssertionError('No registry discovery')):
            self.assertEqual(validate_parameters([dict(parameter_key='flag', label='Flag', field_type='Check', scope='Account', required=False)])[0]['scope'], 'Account')
        with self.assertRaises(frappe.ValidationError):
            CommunicationProviderParameter('flag', 'Flag', 'Data', 'Unsupported')
