"""Public message content stays independent of administrative provenance."""
import unittest
from unittest.mock import patch

import frappe

from messaging.conversation.doctype.chat_message.chat_message import origin_label


class TestMessageContent(unittest.TestCase):
	def test_administrative_origin_uses_kind_only(self):
		with patch('messaging.conversation.doctype.chat_message.chat_message._', side_effect=lambda value: value):
			self.assertEqual([origin_label(kind) for kind in ('Incoming', 'Automatic', 'Human', 'Event', None)],
				['Subscriber', 'Bot', 'Agent', 'System', ''])

	def test_human_and_automatic_envelopes_do_not_compose_internal_metadata(self):
		from messaging.chatbot.doctype.bot_dispatch import bot_dispatch
		content = 'Hola, ¿en qué puedo ayudarte?'
		dispatch = frappe._dict(name='dispatch', outbound_key='outbound', contact='contact', account='account',
			channel='channel', content=content, menu_options='{}', agent='Administrator', status='Sending')
		def value(doctype, *args, **kwargs):
			return {'Communication Channel Account': dict(channel='channel', account_id='external-account'),
				'Communication Channel': 'EMULR', 'Chat Contact': 'subscriber'}[doctype]
		with patch.object(bot_dispatch, 'require_dispatch_operator'), \
			patch.object(frappe, 'get_doc', return_value=dispatch), patch.object(frappe.db, 'get_value', side_effect=value):
			for kind in ('Automatic', 'Human'):
				dispatch.message_type = kind
				payload = bot_dispatch.envelope(dispatch.name)
				self.assertEqual(payload['content'], content)
				self.assertEqual(payload['content_type'], 'text')
				self.assertEqual(payload['interactive'], [])
				self.assertTrue({'agent', 'assigned_agent', 'origin_label', 'process', 'header', 'footer'}.isdisjoint(payload))
				self.assertNotIn('Administrator', payload.values())

	def test_explicit_template_components_are_rendered_once(self):
		from messaging.communication.doctype.communication_template.communication_template import render_template_components
		body = 'Hola, ¿en qué podemos ayudarte?'
		template = frappe._dict(doctype='Bot Flow Template', header='', body=body, footer='', template_variables=[])
		self.assertEqual(render_template_components(template, {}), body)
		template.update(header='🤖 Bot', footer='ℹ️ Junta Administradora de Agua')
		self.assertEqual(render_template_components(template, {}), f'🤖 Bot\n\n{body}\n\nℹ️ Junta Administradora de Agua')
		template.update(body='Hello {{name}}', template_variables=[frappe._dict(parameter_name='name')])
		self.assertEqual(render_template_components(template, {'name': '{{literal}}'}),
			'🤖 Bot\n\nHello {{literal}}\n\nℹ️ Junta Administradora de Agua')

	def test_emulator_handoff_preserves_content_and_transport_outcomes(self):
		from messaging.emulator import provider
		from messaging.chatbot.doctype.bot_dispatch import bot_dispatch
		from messaging.communication.doctype.communication_channel_account import communication_channel_account as accounts
		payload = dict(dispatch='dispatch', provider='EMULR', channel='emulator', account='account',
			account_id='external-account', destination='subscriber', outbound_id='outbound', content='Public body')
		dispatch = frappe._dict(name='dispatch', status='Sending')
		with patch.object(frappe, 'get_doc', return_value=dispatch), \
			patch.object(bot_dispatch, 'require_dispatch_operator'), patch.object(bot_dispatch, 'envelope', return_value=dict(payload)), \
			patch.object(accounts, 'resolve_binding', return_value=({}, frappe._dict(name='account', account_id='external-account'))), \
			patch.object(frappe.db, 'exists', return_value=True), patch.object(provider, 'transport_parameters') as parameters:
			for outcome in ('Accepted', 'Rejected', 'Unknown'):
				parameters.return_value = {'Account': {'transport_outcome': outcome}}
				result = provider.send(payload)
				self.assertEqual(result['outcome'], outcome)
				self.assertEqual(result.get('delivery_status'), 'Sent' if outcome == 'Accepted' else None)
				self.assertEqual(payload['content'], 'Public body')
				self.assertNotIn('header', payload)
