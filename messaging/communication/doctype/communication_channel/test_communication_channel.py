"""Channel creation uses enabled providers and the catalog's exact cardinality."""
import unittest
from unittest.mock import patch

import frappe
from frappe.client import set_value
from frappe.utils.password import get_decrypted_password
from frappe.website.serve import get_response
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from messaging.communication.doctype.communication_channel import public_information as public
from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.communication.doctype.communication_channel_account.binding import resolve_ingress_binding
from messaging.communication.doctype.communication_provider import test_communication_provider as fixtures
from messaging.communication.doctype.communication_provider.communication_provider import channel_providers, set_provider_enabled
from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers


class TestCommunicationChannel(unittest.TestCase):
	def setUp(self):
		self.catalog = fixtures.TestCommunicationProvider(methodName='runTest')
		self.addCleanup(self.catalog.doCleanups)
		self.catalog.setUp()
		self.addCleanup(self.catalog.tearDown)

	def test_name_uses_channel_name_and_edit_preserves_account_and_password(self):
		set_provider_enabled('TSTPV', True)
		channel = self.catalog.channel(channel_name='  Atención   Ñandú Ünico  ')
		self.assertEqual(channel.name, 'atencion_nandu_unico')
		self.assertEqual(channel.channel_name, 'Atención Ñandú Ünico')
		account = self.catalog.account(channel)
		password_row = next(row for row in channel.parameters if row.parameter_key == 'app_secret')
		channel.channel_name = '  Updated   description '
		channel.save()
		self.assertEqual(channel.reload().name, 'atencion_nandu_unico')
		self.assertEqual(channel.channel_name, 'Updated description')
		self.assertEqual(account.reload().channel, channel.name)
		self.assertEqual(get_decrypted_password(password_row.doctype, password_row.name, 'secret_value'), 'synthetic-secret')
		with self.assertRaisesRegex(frappe.ValidationError, 'Channel identity is immutable'):
			frappe.rename_doc(channel.doctype, channel.name, 'Another channel', force=True)

	def test_initial_identity_rejects_blank_duplicate_and_explicit_override(self):
		set_provider_enabled('TSTPV', True)
		set_provider_enabled('TSTOT', True)
		for name in ('', '   ', 'Channel 2', 'Channel_name', 'Channel-name', 'Channel!', 'Channel 📞', 'Two\twords'):
			with self.assertRaises(frappe.ValidationError):
				self.catalog.channel(channel_name=name)
		channel = self.catalog.channel(channel_name='A\u0301rbol Peña')
		self.assertEqual((channel.channel_name, channel.name), ('Árbol Peña', 'arbol_pena'))
		with self.assertRaisesRegex(frappe.ValidationError, 'A channel with this ID already exists'):
			self.catalog.channel('TSTOT', channel_name=' arbol   PENA ', parameters=[])
		channel.channel_name = 'Label 2'
		with self.assertRaisesRegex(frappe.ValidationError, 'may contain only letters and spaces'):
			channel.save()
		with self.assertRaisesRegex(frappe.ValidationError, 'The channel identity must match Channel Name'):
			frappe.get_doc(dict(doctype='Communication Channel', provider='TSTPV',
				channel_name='Selected name')).insert(set_name='Other name')

	def test_fixed_id_is_reserved_before_its_channel_exists(self):
		set_provider_enabled('TSTPV', True)
		set_provider_enabled('TSTOT', True)
		self.catalog.declarations[1]['fixed_channel_id'] = 'reserved_test_id'
		sync_communication_providers()
		with self.assertRaisesRegex(frappe.ValidationError, 'The fixed channel ID belongs to another provider'):
			self.catalog.channel(channel_name='RESERVED TEST ID')
		self.catalog.declarations[0]['fixed_channel_id'] = 'RESERVED_TEST_ID'
		with self.assertRaisesRegex(frappe.ValidationError, 'The fixed channel ID belongs to another provider'):
			sync_communication_providers()
		del self.catalog.declarations[0]['fixed_channel_id']
		first = self.catalog.channel('TSTOT', channel_name='Fixed provider label', parameters=[])
		self.assertEqual(first.name, 'reserved_test_id')

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

	def test_status_actions_require_permission_and_validate_enablement(self):
		self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[set_channel_status], ['POST'])
		set_provider_enabled('TSTPV', True)
		channel = self.catalog.channel()
		self.assertEqual(channel.status, 'Disabled')
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc(dict(doctype=channel.doctype, provider=channel.provider,
				channel_name='Forged status', status='Enabled')).insert()
		with self.assertRaises(frappe.PermissionError):
			set_value(channel.doctype, channel.name, 'status', 'Enabled')
		with self.assertRaises(frappe.ValidationError):
			set_channel_status(channel.name, 'Available')
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):
			set_channel_status(channel.name, 'Enabled')
		frappe.set_user('Administrator')
		with patch.object(channel.__class__, 'check_permission', side_effect=frappe.PermissionError):
			with self.assertRaises(frappe.PermissionError):
				set_channel_status(channel.name, 'Enabled')
		with patch('messaging.communication.doctype.communication_provider.communication_provider.is_available', return_value=False):
			with self.assertRaises(frappe.ValidationError):
				set_channel_status(channel.name, 'Enabled')
		self.assertEqual(set_channel_status(channel.name, 'Enabled')['status'], 'Enabled')
		binding = self.catalog.account(channel, enabled=1)
		resolve_ingress_binding('TSTPV', binding.name)
		set_provider_enabled('TSTPV', False)
		self.assertEqual(channel.reload().status, 'Enabled')
		with self.assertRaises(frappe.PermissionError):
			resolve_ingress_binding('TSTPV', binding.name)
		set_channel_status(channel.name, 'Disabled')
		with self.assertRaises(frappe.PermissionError):
			set_channel_status(channel.name, 'Enabled')
		self.assertEqual(channel.reload().status, 'Disabled')
		set_provider_enabled('TSTPV', True)
		set_channel_status(channel.name, 'Enabled')
		resolve_ingress_binding('TSTPV', binding.name)


	def test_public_documents_keep_reviewed_content_and_channel_scope(self):
		set_provider_enabled('TSTPV', True)
		channel = self.catalog.channel(channel_name='Public document fixture')
		other = self.catalog.channel(channel_name='Other public document fixture')
		for doc in (channel, other):
			doc.public_organization = 'Synthetic & Co'
			doc.public_service = 'Synthetic Service'
			doc.public_email = 'legal@example.invalid'
			doc.public_website = 'https://example.invalid'
			doc.public_contact = '<b>Public street</b>'
			doc.public_description = 'Consultas de prueba'
			doc.save()

		def set_publication(doc, key, action='publish'):
			public.set_publication(doc.name, key, action, str(doc.modified))
			doc.reload()

		def visit(doc, key, *, query=None, name=None):
			origin = 'http://untrusted-host.invalid'
			request = Request(EnvironBuilder(base_url=origin, path=f'/messaging/{name or doc.name}/{key}',
				query_string=query).get_environ())
			with patch.object(frappe.local, 'request', request, create=True):
				frappe.set_user('Guest')
				try:
					return get_response()
				finally:
					frappe.set_user('Administrator')

		self.assertEqual(channel.status, 'Disabled')
		with patch.dict(frappe.conf, host_name='https://public.example.com'):
			for key, (field, _title) in public.DOCUMENTS.items():
				with self.subTest(key=key):
					self.assertEqual(public.publication_status(channel.name)[key], dict(published=False, url=None))
					draft = public.standard_draft(channel.name, key)
					self.assertIn('<h2>', draft)
					channel.set(field, draft)
					channel.save()
					self.assertEqual(visit(channel, key).status_code, 404)
					if key != 'data-deletion':
						with self.assertRaises(frappe.ValidationError):
							set_publication(channel, key)
					channel.set(field, '<h2>Reviewed body</h2><p>Visible text</p>'
						'<script>evilScript</script><img src=x onerror=evilHandler>'
						'<a href="java&#x73;cript:evilLink">Test</a><svg><script>evilSvg</script></svg>')
					channel.save()
					preview = public.preview_draft(channel.name, key)
					self.assertIn('<h2>Reviewed body</h2>', preview)
					self.assertNotIn('evil', preview)
					set_publication(channel, key)
					result = visit(channel, key)
					self.assertEqual(result.status_code, 200)
					self.assertEqual(result.headers['Cache-Control'], 'no-store')
					html = result.get_data(as_text=True)
					self.assertIn('Synthetic &amp; Co', html)
					self.assertIn('&lt;b&gt;Public street&lt;/b&gt;', html)
					for forbidden in ('evil', 'synthetic-secret', 'app_secret', 'parameters'):
						self.assertNotIn(forbidden, html)
					self.assertEqual(public.publication_status(channel.name)[key], dict(published=True,
						url=f'https://public.example.com/messaging/{channel.name}/{key}'))

			set_channel_status(channel.name, 'Enabled')
			set_channel_status(channel.name, 'Disabled')
			channel.reload()
			for key in public.DOCUMENTS:
				self.assertEqual(visit(channel, key).status_code, 200)

			key = 'privacy-policy'
			other.public_privacy_draft = '<p>Other channel</p>'
			other.save()
			set_publication(other, key)
			channel.public_privacy_draft = '<p>Unpublished revision</p>'
			channel.public_email = 'revised@example.invalid'
			channel.channel_name = 'Renamed channel'
			for row in channel.parameters:
				if row.parameter_key == 'app_secret':
					row.secret_value = 'updated-synthetic-secret'
			channel.save()

			html = visit(channel, key, query={'channel': other.name, 'document': 'data-deletion'}).get_data(as_text=True)
			self.assertIn('/' + channel.name + '/data-deletion', html)
			self.assertIn('/' + channel.name + '/terms-of-service', html)
			self.assertIn('legal@example.invalid', html)
			for forbidden in ('Unpublished revision', 'revised@example.invalid', 'Other channel'):
				self.assertNotIn(forbidden, html)
			self.assertIn('Other channel', visit(other, key).get_data(as_text=True))
			self.assertEqual(visit(channel, key, name=channel.name.upper()).status_code, 404)
			with patch.dict(frappe.conf, host_name='', hostname=None):
				self.assertIsNone(public.publication_status(channel.name)[key]['url'])
				self.assertEqual(visit(channel, key).status_code, 200)
			set_publication(channel, key, 'withdraw')
			self.assertEqual(visit(channel, key).status_code, 404)
			self.assertNotIn('/privacy-policy', visit(channel, 'data-deletion').get_data(as_text=True))
			self.assertIn('Unpublished revision', public.preview_draft(channel.name, key))
			set_publication(channel, key)
			self.assertIn('Unpublished revision', visit(channel, key).get_data(as_text=True))

	def test_public_document_actions_protect_permissions_and_revision(self):
		set_provider_enabled('TSTPV', True)
		channel = self.catalog.channel()
		revision = str(channel.modified)
		channel.public_organization = 'Synthetic organization'
		channel.save()
		with self.assertRaises(frappe.TimestampMismatchError):
			public.set_publication(channel.name, 'privacy-policy', 'publish', revision)
		with self.assertRaises(frappe.ValidationError):
			public.set_publication(channel.name, 'unknown-document', 'publish', str(channel.modified))
		with self.assertRaises(frappe.PermissionError):
			set_value(channel.doctype, channel.name, public.SNAPSHOTS, '{"privacy-policy": {}}')

		frappe.set_user('Guest')
		try:
			for method, args in (
				(public.preview_draft, ('privacy-policy',)),
				(public.publication_status, ()),
				(public.standard_draft, ('privacy-policy',)),
				(public.set_publication, ('privacy-policy', 'publish', str(channel.modified))),
				(public.set_publication, ('privacy-policy', 'withdraw', str(channel.modified))),
			):
				with self.subTest(method=method.__name__, args=args), self.assertRaises(frappe.PermissionError):
					method(channel.name, *args)
		finally:
			frappe.set_user('Administrator')
