"""Bot Flow Template keeps internal content and does not depend on a provider."""

import unittest

import frappe

from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures


class TestBotFlowTemplate(ProcessFixtures, unittest.TestCase):
	def test_create_edit_and_preserve_content_without_provider_fields(self):
		from messaging.communication.doctype.communication_template.communication_template import resolve_output, template_revision
		meta = frappe.get_meta('Bot Flow Template')
		self.assertFalse({'provider', 'status', 'account', 'usage_type', 'provider_status'} & {field.fieldname for field in meta.fields})
		template = frappe.get_doc(dict(
			doctype='Bot Flow Template', template_name=self.prefix + ' Greeting', language='es', enabled=1,
			header='Hello', body='Contract {{contract}}', footer='End',
			template_variables=[dict(parameter_name='contract')],
		)).insert()
		original = template_revision(template)
		template.body = 'Contract {{contract}} updated'
		template.save()
		self.assertNotEqual(template_revision(template), original)
		template.reload()
		self.assertEqual(template.header, 'Hello')
		self.assertEqual(template.footer, 'End')
		self.assertEqual([row.parameter_name for row in template.template_variables], ['contract'])
		rendered = resolve_output('Template', template_reference=template.name, data={'contract': 'SC-1'})
		self.assertEqual(rendered['text'], 'Hello\n\nContract SC-1 updated\n\nEnd')
		template.enabled = 0
		template.save()
		with self.assertRaises(frappe.ValidationError):
			resolve_output('Template', template_reference=template.name, data={'contract': 'SC-1'})

	def test_language_is_immutable_and_identity_is_stable(self):
		template = frappe.get_doc(dict(
			doctype='Bot Flow Template', template_name=self.prefix + ' Áviso', language='es', enabled=1, body='Text',
		)).insert()
		self.assertTrue(template.name.endswith('_es'))
		template.language = 'en'
		with self.assertRaisesRegex(frappe.ValidationError, 'language is immutable'):
			template.save()
