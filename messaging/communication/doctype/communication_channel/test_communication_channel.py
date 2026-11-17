"""Channel creation uses enabled providers and the catalog's exact cardinality."""
import unittest
from unittest.mock import patch

import frappe

from messaging.communication.doctype.communication_provider import test_communication_provider as fixtures
from messaging.communication.doctype.communication_provider.communication_provider import channel_providers, set_provider_enabled
from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers


class TestCommunicationChannel(unittest.TestCase):
	def setUp(self):
		self.catalog = fixtures.TestCommunicationProvider(methodName='runTest')
		self.addCleanup(self.catalog.doCleanups)
		self.catalog.setUp()
		self.addCleanup(self.catalog.tearDown)

	def test_selector_uses_enablement_and_single_or_multiple_channel_slots(self):
		f = self.catalog
		selected = lambda: {p['id'] for p in channel_providers()} & {'TSTPV', 'TSTOT'}
		self.assertNotIn('TSTPV', {p['id'] for p in channel_providers()})
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		f.declarations[1]['single_channel'] = True; sync_communication_providers()
		self.assertEqual(selected(), {'TSTPV', 'TSTOT'})
		f.channel('TSTOT', parameters=[])
		self.assertEqual(selected(), {'TSTPV'})
		with self.assertRaises(frappe.ValidationError): f.channel('TSTOT', parameters=[])
		f.channel(); f.channel()
		self.assertEqual(frappe.db.count('Communication Channel', {'provider': 'TSTPV'}), 2)
		self.assertEqual(selected(), {'TSTPV'})
		# Technical availability must not replace administrative enablement in the selector.
		with patch('messaging.communication.doctype.communication_provider.communication_provider.is_available', return_value=False):
			self.assertEqual(selected(), {'TSTPV'})
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError): channel_providers()

	def test_server_rejects_disabled_provider_creation_and_allows_existing_edits(self):
		f = self.catalog
		with self.assertRaises(frappe.PermissionError): f.channel()
		set_provider_enabled('TSTPV', True)
		channel = f.channel()
		set_provider_enabled('TSTPV', False)
		with self.assertRaises(frappe.PermissionError): f.channel()
		channel.channel_name = 'Edited while provider disabled'; channel.save()
		self.assertEqual(channel.channel_name, 'Edited while provider disabled')
		self.assertNotIn('TSTPV', {p['id'] for p in channel_providers()})
