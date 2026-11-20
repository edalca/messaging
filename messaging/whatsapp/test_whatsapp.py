"""Focused WhatsApp protocol and owner integration tests with synthetic Meta data."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hmac
import json
import re
import unittest
from unittest.mock import Mock, patch

import frappe
import requests
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from messaging.common.primitives import utcnow
from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.communication.doctype.communication_provider.communication_provider import (
	handoff,
	provider_definition,
	set_provider_enabled,
)
from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema
from messaging.presets.communication_providers.whatsapp import definition
from messaging.registry.communication_providers import register_communication_providers
from messaging.whatsapp import admin, provider, templates, webhook
from messaging.whatsapp.api import DEFAULT_VERSION, SUPPORTED_VERSIONS, Graph, GraphError
from messaging.whatsapp.configuration import PROVIDER, graph_version, verify_token_row


class TestGraph(unittest.TestCase):
	def setUp(self):
		self.graph = Graph('101', '201', 'synthetic', 'v25.0')

	def test_graph_send_uses_bounded_https_and_no_redirects(self):
		with patch('messaging.whatsapp.api.requests.request') as request:
			response = request.return_value.__enter__.return_value
			response.status_code = 200
			response.iter_content.return_value = [b'{"messages":[{"id":"wamid.test"}]}']
			self.assertEqual(self.graph.send({'type': 'text'})['messages'][0]['id'], 'wamid.test')
			args, options = request.call_args
			self.assertEqual(args, ('POST', 'https://graph.facebook.com/v25.0/101/messages'))
			self.assertEqual(options['headers'], {'Authorization': 'Bearer synthetic'})
			self.assertTrue(options['verify'])
			self.assertFalse(options['allow_redirects'])
			self.assertEqual(options['timeout'], (3.05, 10))
			response.status_code = 400
			response.iter_content.return_value = [b'{"error":{"code":131030}}']
			with self.assertRaises(GraphError) as rejected:
				self.graph.send({})
			self.assertTrue(rejected.exception.rejected)
			response.status_code = 502
			with self.assertRaises(GraphError) as uncertain:
				self.graph.send({})
			self.assertFalse(uncertain.exception.rejected)

	def test_timeout_remains_uncertain(self):
		with patch('messaging.whatsapp.api.requests.request', side_effect=requests.ReadTimeout) as request:
			with self.assertRaises(GraphError) as failure:
				self.graph.send({})
			self.assertFalse(failure.exception.rejected)
			request.assert_called_once()

	def test_pagination_uses_cursor_and_ignores_next_destination(self):
		with patch.object(self.graph, '_request', side_effect=[
			{'data': [{'id': '1'}], 'paging': {'next': 'https://example.invalid', 'cursors': {'after': 'cursor'}}},
			{'data': [{'id': '2'}]},
		]) as request:
			self.assertEqual(self.graph.templates('name'), [{'id': '1'}, {'id': '2'}])
			self.assertEqual(request.call_args.args, ('GET', '201', 'message_templates'))
			self.assertEqual(request.call_args.kwargs['params']['after'], 'cursor')

	def test_unlisted_versions_are_rejected_before_request(self):
		with patch('messaging.whatsapp.api.requests.request') as request:
			for version in ('v24.0', 'v28.0', 'v27.1', ' v27.0', None, 27):
				with self.subTest(version=version), self.assertRaises(GraphError) as failure:
					Graph('101', '201', 'synthetic', version)
				self.assertTrue(failure.exception.rejected)
			request.assert_not_called()

	def test_supported_versions_share_graph_operations(self):
		self.assertEqual(SUPPORTED_VERSIONS, ('v27.0', 'v26.0', 'v25.0'))
		self.assertEqual(DEFAULT_VERSION, 'v27.0')
		messages = [
			provider.conversation_payload(dict(content='Hello', interactive=[])),
			provider.conversation_payload(dict(content='Pick', interactive=[dict(id='1', label='One')])),
			provider.conversation_payload(dict(content='Pick', interactive=[
				dict(id=str(index), label=f'Option {index}') for index in range(4)])),
			{'type': 'template', 'template': {'name': 'notice', 'language': {'code': 'en_US'}}},
			{'status': 'read', 'message_id': 'wamid.test'},
		]
		create = dict(name='notice', language='en_US', category='UTILITY', components=[])
		update = dict(category='UTILITY', components=[])
		for version in SUPPORTED_VERSIONS:
			with self.subTest(version=version), patch('messaging.whatsapp.api.requests.request') as request:
				graph = Graph('101', '201', 'synthetic', version)
				response = request.return_value.__enter__.return_value
				response.status_code = 200
				response.iter_content.return_value = [b'{"messages":[{"id":"wamid.test"}]}']
				for payload in messages:
					graph.send(payload)
				response.iter_content.return_value = [b'{"data":[]}']
				self.assertEqual(graph.phone_info(), [])
				self.assertEqual(graph.templates('notice'), [])
				self.assertEqual(graph.subscriptions(), [])
				response.iter_content.return_value = [b'{"success":true}']
				for method in ('POST', 'DELETE'):
					self.assertTrue(graph.subscription(method)['success'])
				graph.save_template(create)
				graph.save_template(update, '301')

				root = f'https://graph.facebook.com/{version}'
				self.assertEqual([exchange.args for exchange in request.call_args_list],
					[('POST', f'{root}/101/messages')] * len(messages) + [
						('GET', f'{root}/201/phone_numbers'), ('GET', f'{root}/201/message_templates'),
						('GET', f'{root}/201/subscribed_apps'), ('POST', f'{root}/201/subscribed_apps'),
						('DELETE', f'{root}/201/subscribed_apps'), ('POST', f'{root}/201/message_templates'),
						('POST', f'{root}/301'),
					])
				self.assertEqual([exchange.kwargs['json'] for exchange in request.call_args_list[:len(messages)]],
					[dict(messaging_product='whatsapp', **payload) for payload in messages])
				self.assertEqual([exchange.kwargs['json'] for exchange in request.call_args_list[-2:]], [create, update])


class TestWhatsApp(unittest.TestCase):
	def setUp(self):
		frappe.set_user('Administrator')
		self.addCleanup(frappe.set_user, 'Administrator')
		self.addCleanup(frappe.db.rollback)
		register_communication_providers(app='messaging', providers=frappe.get_attr(
			'messaging.registry._installed_declarations')('messaging', 'communication_providers'))
		set_provider_enabled(PROVIDER, True)
		self.definition = provider_definition(PROVIDER)
		self.first = self.account('123450001', '201', 'first')
		self.second = self.account('123450002', '202', 'second')
		self.third = self.account('123450003', '201', 'first', self.first.channel)

	def account(self, phone, business, key, channel=None):
		if channel is None:
			doc = frappe.get_doc(dict(doctype='Communication Channel', provider=PROVIDER,
				channel_name=f'WhatsApp protocol fixture {key}', parameters=[
					dict(parameter_key='app_id', value=json.dumps(phone)),
					dict(parameter_key='app_secret', secret_value=key + '-secret'),
				])).insert()
			channel = doc.name
			with patch.object(admin.secrets, 'token_urlsafe', return_value=key + '-verify'):
				admin.generate_webhook_token(channel, str(doc.modified))
			set_channel_status(channel, 'Enabled')
		return frappe.get_doc(dict(doctype='Communication Channel Account', channel=channel,
			account_id=phone, account_name='WhatsApp protocol fixture', enabled=1, parameters=[
				dict(parameter_key='access_token', secret_value=phone + '-synthetic'),
				dict(parameter_key='waba_id', value=json.dumps(business)),
			])).insert()

	def request(self, account, *, query=None, data=None, signature=''):
		params = {'channel': account.channel, **(query or {})}
		req = Request(EnvironBuilder(method='POST' if data is not None else 'GET',
			query_string=params, data=data, content_type='application/json',
			headers={'X-Hub-Signature-256': signature}).get_environ())
		with patch.object(frappe.local, 'request', req, create=True):
			return webhook.handler()

	def payload(self, phone=None, business='201', **fields):
		return json.dumps(dict(object='whatsapp_business_account', entry=[dict(id=business, changes=[
			dict(field='messages', value=dict(metadata=dict(phone_number_id=phone or self.first.name), **fields)),
		])]), ensure_ascii=False).encode()

	@staticmethod
	def sign(raw, key='first'):
		return 'sha256=' + hmac.digest((key + '-secret').encode(), raw, 'sha256').hex()

	def test_registration_and_guest_permissions(self):
		self.assertEqual(definition().id, self.definition['id'])
		self.assertIn('send', provider_definition('EMULR')['operations'])
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):
			admin.account_action(self.first.name, 'subscribe')

	def test_graph_version_schema_and_saved_channel_selection(self):
		schema = parameter_schema('Communication Channel', provider=PROVIDER)
		parameter = next(row for row in schema['parameters'] if row['parameter_key'] == 'graph_version')
		self.assertEqual(parameter['field_type'], 'Select')
		self.assertEqual(parameter['options'].splitlines(), list(SUPPORTED_VERSIONS))
		self.assertEqual(parameter['default_value'], DEFAULT_VERSION)
		channel = frappe.get_doc('Communication Channel', self.first.channel)
		self.assertEqual(graph_version(channel), DEFAULT_VERSION)
		version_row = next(row for row in channel.parameters if row.parameter_key == 'graph_version')
		row_name = version_row.name
		version_row.value = json.dumps('v25.0')
		channel.save()
		channel.reload()
		channel.channel_name = 'WhatsApp protocol fixture renamed'
		channel.save()
		self.assertEqual(frappe.db.get_value('Communication Provider Value', row_name, 'value'), '"v25.0"')
		self.assertEqual(channel.status, 'Enabled')
		self.assertEqual(admin._admin_client(self.first.name)[1].version, 'v25.0')
		self.assertEqual(admin._admin_client(self.second.name)[1].version, DEFAULT_VERSION)
		version_row = next(row for row in channel.parameters if row.name == row_name)
		version_row.value = json.dumps('v28.0')
		with self.assertRaises(frappe.ValidationError):
			channel.save()
		channel.reload()
		self.assertEqual(graph_version(channel), 'v25.0')
		self.assertEqual(channel.status, 'Enabled')

	def test_get_challenge_is_plain_text_and_scoped(self):
		frappe.set_user('Guest')
		query = {'hub.mode': 'subscribe', 'hub.verify_token': 'first-verify', 'hub.challenge': '1234'}
		reply = self.request(self.first, query=query)
		self.assertEqual((reply.status_code, reply.mimetype, reply.get_data()), (200, 'text/plain', b'1234'))
		self.assertEqual(self.request(self.third, query=query).get_data(), b'1234')
		self.assertEqual(self.request(self.second, query=query).status_code, 403)

	def test_callback_url_uses_saved_channel_identity_and_public_site_origin(self):
		channel = frappe.get_doc('Communication Channel', self.first.channel)
		channel.channel_name = 'Renamed label'
		channel.save()
		with patch.dict(frappe.conf, host_name='https://wa.example.com', hostname=None):
			self.assertEqual(admin.get_webhook_url(channel.name), {
				'url': 'https://wa.example.com' + webhook.WEBHOOK_PATH +
					'?channel=whatsapp_protocol_fixture_first',
			})
			self.assertNotEqual(admin.get_webhook_url(channel.name), admin.get_webhook_url(self.second.channel))
			self.assertEqual(webhook.callback_url('synthetich'),
				'https://wa.example.com' + webhook.WEBHOOK_PATH + '?channel=synthetich')
			self.assertTrue(webhook.callback_url('A+B & C#D').endswith('?channel=A%2BB%20%26%20C%23D'))
			with self.assertRaises(frappe.ValidationError):
				admin.get_webhook_url('emulator')
		for host in ('', 'http://wa.example.com', 'https://qota.localhost', 'https://127.0.0.1',
			'https://user:password@wa.example.com', 'wa.example.com', 'https://wa.example.com/?token=ignored'):
			with self.subTest(host=host), patch.dict(frappe.conf, host_name=host, hostname=None):
				self.assertEqual(admin.get_webhook_url(channel.name), {'url': None})
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):
			admin.get_webhook_url(channel.name)

	def test_native_channel_rename_preserves_webhook_credentials_and_accounts(self):
		from frappe.model.rename_doc import rename_doc, validate_rename
		from messaging.whatsapp.configuration import secret

		old = self.first.channel
		channel = frappe.get_doc('Communication Channel', old, for_update=True)
		new = validate_rename(channel.doctype, old, 'renamed_whatsapp_fixture', channel.meta,
			merge=False, force=True, old_doc=channel)
		rows = [(row.name, row.parameter_key) for row in channel.parameters]
		rename_doc(channel.doctype, old, new, validate=False, rebuild_search=False)
		self.assertFalse(frappe.db.exists(channel.doctype, old))
		renamed = frappe.get_doc(channel.doctype, new)
		self.assertEqual(renamed.channel_name, channel.channel_name)
		self.assertEqual([(row.name, row.parameter_key) for row in renamed.parameters], rows)
		self.assertEqual(secret(renamed, 'app_secret'), 'first-secret')
		self.assertEqual(secret(renamed, 'verify_token'), 'first-verify')
		for account in (self.first, self.third):
			self.assertEqual(account.reload().channel, new)
			self.assertEqual(secret(account, 'access_token'), account.account_id + '-synthetic')
		with patch.dict(frappe.conf, host_name='https://wa.example.com', hostname=None):
			self.assertTrue(admin.get_webhook_url(new)['url'].endswith('?channel=renamed_whatsapp_fixture'))
		frappe.set_user('Guest')
		query = {'hub.mode': 'subscribe', 'hub.verify_token': 'first-verify', 'hub.challenge': '000123'}
		self.assertEqual(self.request(self.first, query=query).get_data(), b'000123')
		self.assertEqual(self.request(self.first, query={**query, 'channel': old}).status_code, 403)

	def test_meta_app_id_is_unique_and_required_to_enable(self):
		from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema
		from messaging.whatsapp.configuration import value

		doc = frappe.get_doc(dict(doctype='Communication Channel', provider=PROVIDER,
			channel_name='Meta App ID fixture', parameters=[
				dict(parameter_key='app_secret', secret_value='synthetic'),
			])).insert()
		fields = parameter_schema(doc.doctype, provider=PROVIDER)['parameters']
		self.assertEqual([(field['label'], field['field_type']) for field in fields if field['parameter_key'] == 'app_id'],
			[('Meta App ID', 'Data')])
		admin.generate_webhook_token(doc.name, str(doc.modified))
		with self.assertRaisesRegex(frappe.ValidationError, 'Meta App ID before enabling'):
			set_channel_status(doc.name, 'Enabled')
		doc.reload()
		for app_id, error in (('invalid', 'numeric Meta App ID'), (self.first.name, 'already configured')):
			doc.append('parameters', dict(parameter_key='app_id', value=json.dumps(app_id)))
			with self.assertRaisesRegex(frappe.ValidationError, error):
				doc.save()
			doc.reload()
		doc.append('parameters', dict(parameter_key='app_id', value='"300000000000000001"'))
		doc.save()
		set_channel_status(doc.name, 'Enabled')
		self.assertEqual(value(doc.reload(), 'app_id'), '300000000000000001')
		doc.remove(next(row for row in doc.parameters if row.parameter_key == 'app_id'))
		with self.assertRaisesRegex(frappe.ValidationError, 'Meta App ID before enabling'):
			doc.save()

	def test_manual_token_uses_encrypted_storage_and_survives_edits_and_sync(self):
		from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema

		channel = frappe.get_doc(dict(doctype='Communication Channel', provider=PROVIDER,
			channel_name='Manual token fixture')).insert()
		self.assertIsNone(verify_token_row(channel))
		channel.channel_name = 'Saved without token'
		channel.save()
		self.assertIsNone(verify_token_row(channel.reload()))
		register_communication_providers(app='messaging', providers=frappe.get_attr(
			'messaging.registry._installed_declarations')('messaging', 'communication_providers'))
		self.assertIsNone(verify_token_row(channel.reload()))
		parameters = parameter_schema(channel.doctype, provider=PROVIDER)['parameters']
		self.assertTrue(next(p for p in parameters if p['parameter_key'] == 'verify_token')['read_only'])

		channel.append('parameters', dict(parameter_key='app_secret', secret_value='synthetic'))
		channel.append('parameters', dict(parameter_key='app_id', value='"123450010"'))
		channel.save()
		with self.assertRaises(frappe.ValidationError):
			set_channel_status(channel.name, 'Enabled')
		version = str(channel.modified)
		with self.assertRaises(frappe.ValidationError):
			admin.regenerate_webhook_token(channel.name, version)
		with self.assertRaises(frappe.ValidationError):
			admin.copy_webhook_token(channel.name)
		self.assertIsNone(admin.generate_webhook_token(channel.name, version))
		with self.assertRaises(frappe.TimestampMismatchError):
			admin.generate_webhook_token(channel.name, version)
		channel.reload()
		with self.assertRaises(frappe.ValidationError):
			admin.generate_webhook_token(channel.name, str(channel.modified))

		token = admin.copy_webhook_token(channel.name)['token']
		self.assertTrue(re.fullmatch(r'[A-Za-z0-9_-]{43}', token))
		self.assertNotIn(token, channel.as_json())
		row = verify_token_row(channel)
		self.assertEqual(row.secret_value, '********')
		auth = frappe.db.get_value('__Auth', dict(doctype=row.doctype, name=row.name, fieldname='secret_value'),
			['encrypted', 'password'], as_dict=True, order_by='name')
		self.assertEqual(auth.encrypted, 1)
		self.assertNotEqual(auth.password, token)
		channel.remove(next(row for row in channel.parameters if row.parameter_key == 'app_secret'))
		channel.save()
		with self.assertRaises(frappe.ValidationError):
			set_channel_status(channel.name, 'Enabled')
		channel.append('parameters', dict(parameter_key='app_secret', secret_value='synthetic'))
		channel.save()
		set_channel_status(channel.name, 'Enabled')
		channel.reload()
		channel.channel_name = 'Edited token fixture'
		channel.save()
		register_communication_providers(app='messaging', providers=frappe.get_attr(
			'messaging.registry._installed_declarations')('messaging', 'communication_providers'))
		self.assertEqual(admin.copy_webhook_token(channel.name), {'token': token})

	def test_hidden_parameter_cannot_set_replace_or_remove_token(self):
		from frappe.client import set_value

		channel = frappe.get_doc(dict(doctype='Communication Channel', provider=PROVIDER,
			channel_name='Direct edit fixture', parameters=[
				dict(parameter_key='verify_token', secret_value='synthetic'),
			]))
		with self.assertRaises(frappe.PermissionError):
			channel.insert()

		set_channel_status(self.first.channel, 'Disabled')
		doc = frappe.get_doc('Communication Channel', self.first.channel)
		row = verify_token_row(doc)
		with self.assertRaises(frappe.PermissionError):
			set_value(row.doctype, row.name, 'secret_value', 'synthetic')
		doc.remove(row)
		with self.assertRaises(frappe.PermissionError):
			doc.save()
		self.assertEqual(admin.copy_webhook_token(doc.name), {'token': 'first-verify'})

	def test_verify_token_rotation_is_scoped_and_preserves_post_signature(self):
		self.assertEqual(admin.copy_webhook_token(self.first.channel), {'token': 'first-verify'})
		version = str(frappe.db.get_value('Communication Channel', self.first.channel, 'modified'))
		self.assertIsNone(admin.regenerate_webhook_token(self.first.channel, version))
		with self.assertRaises(frappe.TimestampMismatchError):
			admin.regenerate_webhook_token(self.first.channel, version)
		token = admin.copy_webhook_token(self.first.channel)['token']
		self.assertTrue(token != 'first-verify')
		self.assertEqual(admin.copy_webhook_token(self.second.channel), {'token': 'second-verify'})
		query = {'hub.mode': 'subscribe', 'hub.verify_token': token, 'hub.challenge': '2048'}
		frappe.set_user('Guest')
		self.assertEqual(self.request(self.first, query=query).get_data(), b'2048')
		query['hub.verify_token'] = 'first-verify'
		self.assertEqual(self.request(self.first, query=query).status_code, 403)
		raw = self.payload()
		self.assertEqual(self.request(self.first, data=raw, signature=self.sign(raw)).status_code, 200)
		actions = ((admin.copy_webhook_token, (self.first.channel,)),
			(admin.generate_webhook_token, (self.first.channel, version)),
			(admin.regenerate_webhook_token, (self.first.channel, version)))
		for method, args in actions:
			self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[method], ['POST'])
			with self.assertRaises(frappe.PermissionError):
				method(*args)
		frappe.set_user('Administrator')
		with patch.object(frappe.get_doc('Communication Channel', self.first.channel).__class__,
			'check_permission', side_effect=frappe.PermissionError):
			for method, args in actions:
				with self.assertRaises(frappe.PermissionError):
					method(*args)

	def test_text_ingress_deduplicates_and_routes_by_waba_phone_and_channel(self):
		frappe.set_user('Guest')
		raw = self.payload(messages=[dict(id='wamid.inbound', **{'from': '15550000001'},
			timestamp=str(int(utcnow().timestamp())), type='text', text=dict(body='Hola café'))])
		with (patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'),
			patch('messaging.conversation.doctype.chat_turn.inbox.schedule_contact')):
			self.assertEqual(self.request(self.first, data=raw).status_code, 403)
			self.assertEqual(self.request(self.second, data=raw, signature=self.sign(raw)).status_code, 403)
			for data in (raw, raw):
				self.assertEqual(self.request(self.first, data=data, signature=self.sign(data)).status_code, 200)
			rows = frappe.get_all('Chat Message', filters={'external_id': 'wamid.inbound'}, fields=['content', 'contact'])
			self.assertEqual(len(rows), 1)
			self.assertEqual(rows[0].content, 'Hola café')
			self.assertEqual(frappe.db.get_value('Chat Contact', rows[0].contact, 'account'), self.first.name)

	def test_signed_batch_rejects_other_channels_before_dispatch(self):
		first = self.payload(statuses=[{}])
		second = self.payload(phone=self.second.name, business='202', statuses=[{}])
		batch = json.loads(first)
		batch['entry'].extend(json.loads(second)['entry'])
		mixed = json.dumps(batch).encode()
		frappe.set_user('Guest')
		with patch.object(webhook, 'dispatch_ingress') as dispatch:
			for raw in (second, mixed, self.payload(business='202'), self.payload(phone='123450004')):
				self.assertEqual(self.request(self.first, data=raw, signature=self.sign(raw)).status_code, 403)
			dispatch.assert_not_called()
			self.assertEqual(self.request(self.second, data=second, signature=self.sign(second, 'second')).status_code, 200)
			dispatch.assert_called_once()
			self.assertEqual(dispatch.call_args.args[:2], (PROVIDER, self.second.name))
			self.assertEqual(dispatch.call_args.args[2].channel, self.second.channel)

	def test_outbound_is_account_scoped_with_window_enforcement(self):
		base = dict(account=self.first.name, channel=self.first.channel, destination='15550000001',
			content='Hello', interactive=[], outbound_id='a' * 64)
		with patch.object(Graph, 'send', return_value={'messages': [{'id': 'wamid.outbound'}]}) as send:
			self.assertEqual(handoff(self.definition, base), {'outcome': 'Rejected'})
			send.assert_not_called()
			from messaging.conversation.doctype.chat_turn.inbox import accept_message
			with (patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'),
			patch('messaging.conversation.doctype.chat_turn.inbox.schedule_contact')):
				accept_message(dict(account=self.first.name, channel=self.first.channel,
					sender=base['destination'], event_id='wamid.start', event_at=utcnow().isoformat() + 'Z', text='Hello'))
			self.assertEqual(handoff(self.definition, {**base, 'account': self.third.name}), {'outcome': 'Rejected'})
			self.assertEqual(handoff(self.definition, base), {'outcome': 'Accepted', 'reference': 'wamid.outbound'})
			self.assertEqual(send.call_args.args[0]['biz_opaque_callback_data'], 'b:' + base['outbound_id'])
			send.side_effect = GraphError()
			self.assertEqual(handoff(self.definition, base), {'outcome': 'Unknown'})
			with patch.object(provider, 'utcnow', return_value=utcnow() + timedelta(hours=24)):
				self.assertEqual(handoff(self.definition, base), {'outcome': 'Rejected'})

	def test_interactive_and_delivery_correlate_exact_dispatch(self):
		binding = frappe._dict(channel=self.first.channel, account=self.first.name)
		id_ = 'b' * 64
		item = {'from': '15550000001', 'id': 'wamid.reply', 'timestamp': '1767225600', 'type': 'interactive',
			'context': {'id': 'wamid.outbound'}, 'interactive': {'type': 'list_reply', 'list_reply': {'id': id_}}}
		with patch.object(webhook, 'find_dispatch', return_value='dispatch') as find:
			result = webhook.incoming(binding, item)['payload']['event']
			self.assertEqual(result['interactive'], {'dispatch': 'dispatch', 'selection_id': id_})
			find.assert_called_once_with(binding, item['from'], 'wamid.outbound')
		item['type'] = 'image'
		self.assertEqual(webhook.incoming(binding, item)['payload']['event']['message_type'], 'unsupported')
		options = [dict(id=str(index), label=f'Option {index}') for index in range(4)]
		self.assertEqual(provider.conversation_payload(dict(content='Pick', interactive=options))['interactive']['type'], 'list')
		doc = frappe._dict(name='dispatch', status='Unknown', transport_reference=None, contact='contact')
		with patch.object(frappe.db, 'get_value', side_effect=[doc, item['from']]) as lookup:
			report = webhook.status_report(binding, dict(status='read', id='wamid.outbound',
				recipient_id=item['from'], biz_opaque_callback_data='b:' + id_))
			self.assertEqual(report['payload']['status'], 'Read')
			self.assertEqual(lookup.call_args_list[0].args[1]['account'], binding.account)

	def new_template(self, account=None, name='Position notice', *, channel=None):
		return frappe.get_doc(dict(doctype='Communication Template', channel=channel or self.first.channel,
			account=account.name if account else None, template_name=name, language='en', provider_language='en', status='Inactive',
			body='Your reference is {{reference}}.', template_variables=[dict(parameter_name='reference')]))

	def remote(self, name='position_notice', body='Reference {{2}} comes before {{1}} with {{3}} and {{4}}.',
			status='APPROVED', reference='2021', **more):
		return dict(dict(id=reference, name=name, language='en', status=status, category='UTILITY',
			parameter_format='POSITIONAL', components=[dict(type='BODY', text=body)]), **more)

	def named_remote(self, body='Hello {{customer}}, your {{contract}} is ready: {{contract}}.', name='named_notice', **options):
		return self.remote(name=name, language='es_HN', parameter_format='NAMED', body=body, **options)

	def test_named_import_preserves_exact_names_and_reuses_parameters(self):
		external = self.keep_fixture_transaction()
		remote = self.named_remote('Hello {{name}}, contract {{contract}} with {{message}}. Confirm {{contract}}.')
		remote['components'].insert(0, dict(type='HEADER', format='TEXT', text='Notice {{name}}',
			example={'header_text_named_params': [dict(param_name='name', example='Ana')]}))
		remote['components'][1]['example'] = {'body_text_named_params': [
			dict(param_name=name, example=name) for name in ('contract', 'message', 'name')
		]}
		incompatible_reserved = self.named_remote('Hello {{status}}.', name='reserved_named', reference='2024')
		incompatible_digits = self.named_remote('Invoice {{invoice_2}}.', name='digits_named', reference='2025')
		positional = self.remote(reference='2022')
		sync_result = self.sync(self.first, [remote, positional, incompatible_reserved, incompatible_digits])
		self.assertEqual(sync_result['created'], 2)
		self.assertEqual(
			[(row['name'], row['reason']) for row in sync_result['skipped']],
			[('reserved_named', 'unsupported_named_parameters'), ('digits_named', 'unsupported_named_parameters')],
		)
		name = f'{self.first.channel}_{self.first.name}_named_notice_es_hn'
		doc = frappe.get_doc('Communication Template', name)
		snapshot = external.stored(doc, 'published_snapshot')
		delivery = snapshot['delivery']
		names = delivery['names']
		self.assertEqual(names, dict(
			header={'name': 'name'},
			body={'contract': 'contract', 'message': 'message', 'name': 'name'},
		))
		self.assertEqual(doc.body.count('{{contract}}'), 2)
		self.assertEqual([row.parameter_name for row in doc.template_variables], ['name', 'contract', 'message'])
		self.assertEqual({row.parameter_key for row in doc.parameters}, {'parameter_format'})
		self.assertEqual((snapshot['reference'], snapshot['scope'], doc.provider_language, delivery['parameter_format']),
			(remote['id'], '201', 'es_HN', 'NAMED'))
		self.assertEqual(external.stored(doc, 'externally_observed')['category'], 'UTILITY')
		self.assertEqual(templates.describe_template(doc)['revision'], templates._reading(remote)['revision'])
		# Reordering rows in NAMED must not alter revision, identity, or sync_state.
		original_revision = doc.revision
		doc.set('template_variables', list(reversed(doc.template_variables)))
		doc.language = 'en'
		doc.save()
		self.assertEqual((doc.revision, doc.sync_state), (original_revision, 'Synchronized'))
		self.assertEqual(templates.describe_template(doc)['revision'], templates._reading(remote)['revision'])
		stamp = str(doc.modified)
		self.assertEqual(self.sync(self.first, [remote, positional])['unchanged'], 2)
		self.assertEqual((doc.reload().language, doc.language_mapping, str(doc.modified)), ('en', 'Manual', stamp))
		self.assertEqual(self.sync(self.third, [remote, positional])['created'], 2)
		other = self.named_remote('Different WABA.', reference='2023', category='MARKETING')
		self.assertEqual(self.sync(self.second, [other])['created'], 1)
		self.assertEqual(doc.reload().published_snapshot, external.encode(snapshot))
		self.assertEqual(frappe.db.count('Communication Template', {'external_key': doc.external_key}), 3)
		with self.assertRaises(frappe.ValidationError):
			external.published_contract(doc.name, self.third.name, available=False)
		with patch.object(Graph, 'templates', return_value=[remote]) as lookup:
			self.assertFalse(external.operation(self.definition, 'verify_template_revision',
				dict(account=self.second.name, channel=self.second.channel), snapshot))
			lookup.assert_not_called()
			self.assertEqual(external.operation(self.definition, 'verify_template_revision',
				dict(account=self.first.name, channel=self.first.channel), snapshot), 'APPROVED')

	def test_named_validates_format_components_and_examples(self):
		self.keep_fixture_transaction()
		doc = self.new_template(self.first)
		format_row = doc.append('parameters', dict(parameter_key='parameter_format', value='"OTHER"'))
		with self.assertRaises(frappe.ValidationError):
			doc.insert()
		format_row.value = '"NAMED"'
		for removed_key, removed_value in (
			('named_parameters', json.dumps('{"reference":"other"}')),
			('position_order', json.dumps('{"body":["reference"]}')),
			('meta_name', '"custom_meta_name"'),
		):
			with self.subTest(removed_key=removed_key):
				extra = doc.append('parameters', dict(parameter_key=removed_key, value=removed_value))
				with self.assertRaisesRegex(frappe.ValidationError, 'Invalid or duplicate provider parameter'):
					doc.insert()
				doc.remove(extra)
		remote = self.named_remote()
		duplicate = dict(param_name='customer', example='Example')
		remote['components'][0]['example'] = {'body_text_named_params': [
			duplicate, dict(duplicate, example='Contradictory'), dict(param_name='contract', example='001'),
		]}
		self.assertEqual(templates._reading(remote)['issue'], 'invalid_named_parameter_examples')
		remote['components'][0]['example']['body_text_named_params'][1] = duplicate
		self.assertIsNone(templates._reading(remote)['issue'])
		for incompatible in ('status', 'invoice_2', 'a__b'):
			with self.subTest(incompatible=incompatible):
				self.assertEqual(
					templates._reading(self.named_remote(f'Check {{{{{incompatible}}}}}.'))['issue'],
					'unsupported_named_parameters',
				)
		with self.assertRaises((KeyError, ValueError)):
			templates.message_template(
				dict(name='named_notice', language='es_HN', parameter_format='NAMED',
					names=dict(body=dict(name='name'))),
				dict(nombre_cliente='Ana'),
			)
		for part, text in [('FOOTER', 'Footer {{customer}}'), ('HEADER', '{{customer}} and {{contract}}'),
			('BODY', 'Mixed {{customer}} and {{1}}'), ('BODY', 'Invalid {{Uppercase}}')]:
			with self.subTest(part=part, text=text):
				component = dict(type=part, text=text)
				if part == 'HEADER':
					component['format'] = 'TEXT'
				invalid = [component] if part == 'BODY' else [*remote['components'], component]
				self.assertEqual(templates._reading(dict(remote, components=invalid))['issue'],
					'unsupported_parameter_names')

	def test_named_publication_create_and_update_wait_for_meta_evidence(self):
		from messaging.communication.doctype.communication_template.status import synchronize_template_status

		external = self.keep_fixture_transaction()
		doc = self.new_template(self.first, 'Named notice')
		doc.provider_language = 'es_HN'
		doc.header = 'Hi {{reference}}'
		doc.body = 'Your reference is {{reference}}. Keep {{reference}}.'
		doc.append('parameters', dict(parameter_key='parameter_format', value='"NAMED"'))
		graph = Mock()
		remote = []
		graph.templates.side_effect = lambda *_: remote
		options = dict(category='UTILITY', examples={'reference': '001'})
		create_components = [
			dict(type='HEADER', format='TEXT', text='Hi {{reference}}',
				example={'header_text_named_params': [dict(param_name='reference', example='001')]}),
			dict(type='BODY', text='Your reference is {{reference}}. Keep {{reference}}.',
				example={'body_text_named_params': [dict(param_name='reference', example='001')]}),
		]

		def create(payload, reference):
			self.assertIsNone(reference)
			self.assertTrue(external.stored(doc.reload(), 'publication_request')['pending'])
			self.assertEqual(payload, dict(name='named_notice', language='es_HN', category='UTILITY',
				parameter_format='NAMED', components=create_components))
			remote.append(dict(payload, status='PENDING', id='2021'))
			return dict(id='2021', status='PENDING')

		with patch.object(templates, 'client', return_value=graph), patch.object(frappe.db, 'commit'):
			doc.insert()
			graph.save_template.assert_not_called()
			with self.assertRaisesRegex(frappe.ValidationError, 'example for each'):
				external.publish_template(doc.name, str(doc.modified), dict(options, examples={}))
			graph.save_template.assert_not_called()
			graph.save_template.side_effect = create
			self.assertEqual(external.publish_template(doc.name, str(doc.modified), options)['state'], 'Publishing')
			self.assertTrue(external.published_block(doc.reload(), self.definition, self.first.name))
			remote[0]['status'] = 'APPROVED'
			synchronize_template_status(doc.name)
			self.assertEqual(doc.reload().sync_state, 'Synchronized')
			first_revision = doc.published_revision
			doc.body += ' Thank you.'
			doc.save()
			self.assertEqual(graph.save_template.call_count, 1)
			updated_components = deepcopy(create_components)
			updated_components[1]['text'] += ' Thank you.'

			def update(payload, reference):
				self.assertEqual(reference, '2021')
				self.assertEqual(payload, dict(category='UTILITY', parameter_format='NAMED', components=updated_components))
				remote[0] = dict(remote[0], components=payload['components'], status='PENDING')
				return dict(success=True)

			graph.save_template.side_effect = update
			self.assertEqual(external.publish_template(doc.name, str(doc.modified), options)['state'], 'Publishing')
			self.assertEqual(doc.reload().published_revision, first_revision)
			remote[0]['status'] = 'APPROVED'
			synchronize_template_status(doc.name)
			self.assertNotEqual(doc.reload().published_revision, first_revision)
			self.assertEqual(doc.sync_state, 'Synchronized')
			self.assertFalse(external.stored(doc, 'publication_request')['pending'])

	def test_whatsapp_template_schema_exposes_only_parameter_format_select(self):
		schema = parameter_schema('Communication Template', channel=self.first.channel)
		keys = [row['parameter_key'] for row in schema['parameters']]
		self.assertEqual(keys, ['parameter_format'])
		field = schema['parameters'][0]
		self.assertEqual(field['field_type'], 'Select')
		self.assertEqual(field['options'].splitlines(), ['POSITIONAL', 'NAMED'])
		for removed in ('meta_name', 'position_order', 'named_parameters'):
			self.assertNotIn(removed, keys)

	def test_positional_row_order_out_of_order_and_independent_components(self):
		from messaging.communication.doctype.communication_template.communication_template import set_active
		from messaging.communication.doctype.communication_template.status import synchronize_template_status

		external = self.keep_fixture_transaction()
		doc = frappe.get_doc(dict(
			doctype='Communication Template',
			channel=self.first.channel,
			account=self.first.name,
			template_name='Payment reminder',
			language='es',
			provider_language='es_HN',
			status='Inactive',
			header='Aviso {{nombre_header}}',
			body='Hola {{nombre_cliente}}, su pago de {{monto}} corresponde al contrato {{numero_contrato}}.',
			# Place header variable between body variables to prove per-component independent numbering.
			template_variables=[
				dict(parameter_name='nombre_cliente'),
				dict(parameter_name='nombre_header'),
				dict(parameter_name='numero_contrato'),
				dict(parameter_name='monto'),
			],
		)).insert()
		self.assertEqual(doc.parameters, [])
		graph = Mock()
		remote = []
		graph.templates.side_effect = lambda *_: remote
		options = dict(
			category='UTILITY',
			examples=dict(nombre_header='Ana', nombre_cliente='Ana Lopez', numero_contrato='C-100', monto='L 500.00'),
		)

		def create(payload, reference):
			self.assertIsNone(reference)
			self.assertEqual(payload, dict(
				name='payment_reminder',
				language='es_HN',
				category='UTILITY',
				parameter_format='POSITIONAL',
				components=[
					dict(type='HEADER', format='TEXT', text='Aviso {{1}}', example=dict(header_text=['Ana'])),
					dict(
						type='BODY',
						text='Hola {{1}}, su pago de {{3}} corresponde al contrato {{2}}.',
						example=dict(body_text=[['Ana Lopez', 'C-100', 'L 500.00']]),
					),
				],
			))
			remote.append(dict(payload, status='APPROVED', id='4001'))
			return dict(id='4001', status='APPROVED')

		graph.save_template.side_effect = create
		with patch.object(templates, 'client', return_value=graph), patch.object(frappe.db, 'commit'):
			self.assertEqual(external.publish_template(doc.name, str(doc.modified), options)['state'], 'Synchronized')
		doc.reload()
		set_active(dict(name=doc.name, modified=str(doc.modified)), 1)
		doc.reload()
		approved_revision = doc.published_revision
		approved_snapshot = doc.published_snapshot
		delivery = external.stored(doc, 'published_snapshot')['delivery']
		self.assertEqual(delivery['positions'], dict(
			header=['nombre_header'],
			body=['nombre_cliente', 'numero_contrato', 'monto'],
		))
		# Reordering body rows updates local draft and marks Pending without altering published contract.
		doc.set('template_variables', [
			doc.template_variables[0],  # nombre_cliente
			doc.template_variables[1],  # nombre_header
			doc.template_variables[3],  # monto -> now body position 2
			doc.template_variables[2],  # numero_contrato -> now body position 3
		])
		doc.save()
		self.assertEqual(doc.sync_state, 'Pending')
		self.assertEqual((doc.published_revision, doc.published_snapshot), (approved_revision, approved_snapshot))
		# Delivery still uses the published revision order (1=nombre_cliente, 2=numero_contrato, 3=monto).
		payload = dict(
			account=self.first.name, channel=self.first.channel, destination='+14155552671',
			source_type='Notification Queue', output_mode='Provider Template', template_reference=doc.name,
			template_revision=approved_revision, outbound_id='d' * 64,
			variables=dict(nombre_header='Ana', nombre_cliente='Ana Lopez', numero_contrato='C-100', monto='L 500.00'),
		)
		with patch.object(Graph, 'send', return_value={'messages': [{'id': 'wamid.pos'}]}) as send:
			self.assertEqual(handoff(self.definition, payload)['outcome'], 'Accepted')
			self.assertEqual(send.call_args.args[0]['template']['components'], [
				dict(type='header', parameters=[dict(type='text', text='Ana')]),
				dict(type='body', parameters=[
					dict(type='text', text='Ana Lopez'),
					dict(type='text', text='C-100'),
					dict(type='text', text='L 500.00'),
				]),
			])
		# Subsequent sync while draft is Pending preserves reordered draft and published snapshot.
		self.assertEqual(self.sync(self.first, remote)['unchanged'], 1)
		self.assertEqual([row.parameter_name for row in doc.reload().template_variables],
			['nombre_cliente', 'nombre_header', 'monto', 'numero_contrato'])
		self.assertEqual(doc.sync_state, 'Pending')
		# Invalid positional remote markers (missing/repeated) are rejected.
		for bad_body in ('Hola {{1}} y {{3}}.', 'Hola {{1}} y {{1}}.'):
			self.assertEqual(templates._reading(self.remote(body=bad_body))['issue'], 'unsupported_parameter_positions')

	def test_qota_five_whatsapp_templates_compatibility_and_idempotency(self):
		from messaging.communication.doctype.communication_template.communication_template import set_active
		from messaging.whatsapp.provider import validate_notification
		from messaging.whatsapp.templates import prepare_provider_template

		external = self.keep_fixture_transaction()
		catalog = [
			dict(id='5001', name='hello_world', language='en_US', status='APPROVED', category='UTILITY',
				parameter_format='POSITIONAL', components=[
					dict(type='HEADER', format='TEXT', text='Hello World'),
					dict(type='BODY', text='Welcome and congratulations!!'),
					dict(type='FOOTER', text='WhatsApp Business Platform sample message'),
				]),
			dict(id='5002', name='jaspers_market_plain_text_v1', language='en_US', status='APPROVED',
				category='MARKETING', parameter_format='POSITIONAL',
				components=[dict(type='BODY', text='Welcome to Jasper Market!')]),
			dict(id='5003', name='qota_service_reconnection_test', language='es_HN', status='APPROVED',
				category='UTILITY', parameter_format='NAMED', components=[
					dict(type='HEADER', format='TEXT', text='Aviso de Reconexion del Servicio'),
					dict(type='BODY', text='Hola *{{name}}*. Aviso *{{reconexion}}* contrato *{{contract}}*.\n{{message}}'),
					dict(type='FOOTER', text='Junta Administradora de Agua · Notificaciones'),
				]),
			dict(id='5004', name='qota_payment_received_test', language='es_HN', status='APPROVED',
				category='MARKETING', parameter_format='NAMED', components=[
					dict(type='HEADER', format='TEXT', text='Confirmación de Pago Recibido'),
					dict(type='BODY', text='Hola *{{name}}*. Recibo *{{receipt}}*, contrato *{{contract}}*, fecha *{{date}}*.\n{{detail}}\nTotal: {{amount}}'),
					dict(type='FOOTER', text='Junta Administradora de Agua · Notificaciones'),
				]),
			dict(id='5005', name='qota_service_suspension_test', language='es_HN', status='APPROVED',
				category='UTILITY', parameter_format='NAMED', components=[
					dict(type='HEADER', format='TEXT', text='Aviso de Suspensión del Servicio'),
					dict(type='BODY', text='Hola *{{name}}*. Suspensión *{{suspension}}*, contrato *{{contract}}*.\n{{message}}'),
					dict(type='FOOTER', text='Junta Administradora de Agua · Notificaciones'),
				]),
		]
		first = self.sync(self.first, catalog)
		self.assertEqual((first['created'], first['skipped']), (5, []))
		expected_vars = {
			'qota_service_reconnection_test': ['name', 'reconexion', 'contract', 'message'],
			'qota_payment_received_test': ['name', 'receipt', 'contract', 'date', 'detail', 'amount'],
			'qota_service_suspension_test': ['name', 'suspension', 'contract', 'message'],
		}
		stamps = {}
		for template_name, variables in expected_vars.items():
			doc = frappe.get_doc('Communication Template',
				f'{self.first.channel}_{self.first.name}_{template_name}_es_hn')
			self.assertEqual([row.parameter_name for row in doc.template_variables], variables)
			self.assertEqual({row.parameter_key for row in doc.parameters}, {'parameter_format'})
			self.assertEqual(external.stored(doc, 'published_snapshot')['delivery']['names']['body'],
				{key: key for key in variables})
			if template_name == 'qota_service_reconnection_test':
				stamps[doc.name] = (str(doc.modified), doc.published_revision)
				continue
			if not doc.language:
				doc.language = 'es'
				doc.save()
			set_active(dict(name=doc.name, modified=str(doc.modified)), 1)
			doc.reload()
			stamps[doc.name] = (str(doc.modified), doc.published_revision)
			values = {key: 'Texto válido' for key in variables}
			if 'detail' in values:
				values['detail'] = 'Cuota enero:\tL     160.00\nCuota febrero: L 160.00'
			envelope = dict(account=self.first.name, output_mode='Provider Template',
				destination='+50488887777', template_reference=doc.name,
				template_revision=doc.published_revision, variables=values)
			mapped = prepare_provider_template(envelope)
			params = {row['parameter_name']: row['text'] for row in mapped['components'][0]['parameters']}
			self.assertEqual(params, values | ({
				'detail': 'Cuota enero: L 160.00; Cuota febrero: L 160.00'
			} if 'detail' in values else {}))
			self.assertTrue(validate_notification(envelope | {'provider_template_parameters': mapped}))
			self.assertFalse(validate_notification(envelope | {'provider_template_parameters': {'name': 'changed'}}))
			for invalid in (' \n\t ', 'Texto\x01inválido', 'a' * 16001, '\n'.join('a' * 8000)):
				with self.subTest(invalid_length=len(invalid)):
					self.assertFalse(validate_notification(envelope | {'variables': values | {'name': invalid}}))
		second = self.sync(self.first, catalog)
		self.assertEqual((second['created'], second['updated'], second['unchanged']), (0, 0, 5))
		for doc_name, (stamp, revision) in stamps.items():
			doc = frappe.get_doc('Communication Template', doc_name)
			self.assertEqual((str(doc.modified), doc.published_revision), (stamp, revision))

	def test_external_format_change_requires_explicit_adoption(self):
		external = self.keep_fixture_transaction()
		remote = self.named_remote('No parameters.')
		self.assertEqual(self.sync(self.first, [remote])['created'], 1)
		doc = frappe.get_doc('Communication Template', {'external_key': templates._reading(remote)['key']})
		saved = doc.published_snapshot
		format_row = next(row for row in doc.parameters if row.parameter_key == 'parameter_format')
		format_row.value = '"POSITIONAL"'
		with self.assertRaisesRegex(frappe.ValidationError, 'format is immutable'):
			doc.save()
		changed = dict(remote, parameter_format='POSITIONAL')
		self.assertEqual(self.sync(self.first, [changed])['skipped'][0]['reason'], 'external_parameter_format_changed')
		self.assertEqual((doc.reload().sync_state, doc.published_snapshot), ('Conflict', saved))
		with patch.object(Graph, 'templates', return_value=[changed]):
			external.adopt_external_template(doc.name, str(doc.modified))
		self.assertEqual(templates._format(doc.reload()), 'POSITIONAL')
		self.assertEqual(doc.sync_state, 'Synchronized')

	def test_catalog_matches_explicit_and_conventional_codes(self):
		from messaging.communication.doctype.communication_template.languages import resolve_language
		from messaging.communication.doctype.communication_template.external import template_language_preview
		from messaging.registry.communication_providers import TemplateLanguage, normalize_definitions

		declared = replace(definition(), template_languages=(TemplateLanguage('english:canada', 'Canadian English', 'en'),
			TemplateLanguage('en_GB', 'British English')))
		probe = normalize_definitions('messaging', (declared,))[PROVIDER]
		frappe.db.set_value('Language', 'en-GB', 'enabled', 1)
		self.assertEqual(resolve_language(probe, 'english:canada'), ('en', 'Automatic'))
		self.assertEqual(template_language_preview(self.first.channel, 'en_GB'),
			dict(language='en-GB', origin='Automatic'))
		self.assertIn('es_HN', self.definition['template_languages'])
		with self.assertRaises(frappe.ValidationError):
			replace(declared, template_languages=declared.template_languages * 2)
		doc = self.new_template(self.first)
		doc.provider_language = 'en-GB'
		with self.assertRaises(frappe.ValidationError):
			doc.insert()

	def test_regional_languages_preserve_mapping_identity_and_published_delivery(self):
		from messaging.chatbot.doctype.bot_settings import language as chatbot_languages
		from messaging.communication.doctype.communication_template.communication_template import set_active
		from messaging.communication.doctype.notification_rule.contract import prepared_data

		self.keep_fixture_transaction()
		frappe.db.set_value('Language', 'en-GB', 'enabled', 0)
		default = frappe._dict(language='es', enabled=1, is_default=1)
		settings = frappe._dict(chatbot_languages=[default])
		rows = [self.remote(body='Ready.', language=code, reference=str(index))
			for index, code in enumerate(('en_AE', 'en_GB'), 2001)]
		# Frappe can prefill Link fields using a user or site default.
		defaults = frappe.new_doc('Communication Template', as_dict=True)
		defaults['language'] = 'en'
		with patch.object(chatbot_languages, 'get_bot_settings', return_value=settings), patch.dict(
			frappe.local.new_doc_templates, {'Communication Template': defaults}
		):
			result = self.sync(self.first, rows)
			self.assertEqual(result['created'], 2)
			self.assertEqual([row['language'] for row in result['language_warnings']], ['es', 'es'])
			names = [f'{self.first.channel}_{self.first.name}_position_notice_{code}' for code in ('en_ae', 'en_gb')]
			doc, sibling = [frappe.get_doc('Communication Template', name) for name in names]
			self.assertEqual((doc.language, doc.language_mapping, doc.provider_language), ('es', 'Provisional', 'en_AE'))
			self.assertEqual((sibling.language, sibling.language_mapping), ('es', 'Provisional'))
			set_active(dict(name=doc.name, modified=str(doc.modified)), 1)
			doc.reload()
			evidence = (doc.name, doc.revision, doc.published_revision, doc.published_snapshot, doc.external_key)
			rule = frappe.get_doc(dict(doctype='Notification Rule', account=self.first.name,
				outputs=[dict(status_code='ready', send=1, template_reference=doc.name)]))
			response = dict(version=1, status='ready', recipient='+15550000001', data={}, message='')
			queued = prepared_data(response, rule)
			self.assertEqual(rule.process_language(), 'es')
			with patch.object(Graph, 'save_template') as publish:
				doc.language = 'en'
				doc.language_mapping = 'Automatic'
				doc.save()
			publish.assert_not_called()
			self.assertEqual((doc.name, doc.revision, doc.published_revision, doc.published_snapshot, doc.external_key), evidence)
			self.assertEqual((doc.language_mapping, doc.provider_language, doc.sync_state), ('Manual', 'en_AE', 'Synchronized'))
			self.assertEqual(rule.process_language(), 'en')
			self.assertEqual(prepared_data(response, rule), queued)
			delivery = json.loads(queued['template_contract_json'])['delivery']
			self.assertEqual(templates.message_template(delivery, {})['language']['code'], 'en_AE')
			default.language = 'en'
			result = self.sync(self.first, rows)
			self.assertEqual(result['unchanged'], 2)
			self.assertEqual([row['provider_language'] for row in result['language_warnings']], ['en_GB'])
			self.assertEqual((doc.reload().language, doc.language_mapping), ('en', 'Manual'))
			self.assertEqual((sibling.reload().language, sibling.language_mapping), ('es', 'Provisional'))
			doc.provider_language = 'en_GB'
			with self.assertRaisesRegex(frappe.ValidationError, 'immutable'):
				doc.save()

	def test_import_without_valid_default_preserves_provider_metadata(self):
		from messaging.chatbot.doctype.bot_settings import language as chatbot_languages
		from messaging.communication.doctype.communication_template.communication_template import set_active

		self.keep_fixture_transaction()
		settings = frappe._dict(chatbot_languages=[])
		with patch.object(chatbot_languages, 'get_bot_settings', return_value=settings):
			draft = self.new_template(self.first, 'Missing language')
			draft.update(dict(provider_language='en_AE', language=None))
			draft.insert()
			self.assertEqual((draft.language, draft.language_mapping), (None, 'Unassigned'))
			settings.chatbot_languages = [frappe._dict(language=code, enabled=1, is_default=1) for code in ('es', 'en')]
			result = self.sync(self.first, [self.remote(language='es_HN')])
			self.assertEqual(result['created'], 1)
			self.assertEqual(result['language_warnings'][0]['reason'], 'unassigned_template_language')
			doc = frappe.get_doc('Communication Template', f'{self.first.channel}_{self.first.name}_position_notice_es_hn')
			self.assertTrue(doc.published_revision)
			with self.assertRaisesRegex(frappe.ValidationError, 'enabled native Language'):
				set_active(dict(name=doc.name, modified=str(doc.modified)), 1)
			settings.chatbot_languages.pop()
			self.assertEqual(self.sync(self.first, [self.remote(language='es_HN')])['unchanged'], 1)
			self.assertFalse(doc.reload().language)
			doc.language = 'es'
			doc.save()
			self.assertEqual(doc.language_mapping, 'Manual')

	def test_catalog_diagnostics_separate_language_and_components(self):
		from messaging.chatbot.doctype.bot_settings import language as chatbot_languages

		self.keep_fixture_transaction()
		settings = frappe._dict(chatbot_languages=[frappe._dict(language='es', enabled=1, is_default=1)])
		rows = [self.remote(name='positional', language='en_AE'), self.remote(name='named', reference='2022',
			language='es_HN', parameter_format='NAMED'), self.remote(name='image', reference='2023', category='MARKETING')]
		rows[-1]['components'].insert(0, dict(type='HEADER', format='IMAGE'))
		with patch.object(chatbot_languages, 'get_bot_settings', return_value=settings):
			before = list(frappe.local.message_log)
			result = self.sync(self.first, rows)
			self.assertEqual(frappe.local.message_log, before)
		self.assertEqual(result['created'], 1)
		self.assertEqual(result['language_warnings'][0]['name'], 'positional')
		self.assertEqual(result['skipped'], [
			dict(reference='2022', name='named', provider_language='es_HN', category='UTILITY', reason='unsupported_parameter_names'),
			dict(reference='2023', name='image', provider_language='en', category='MARKETING', reason='unsupported_media_header'),
		])

	def sync(self, binding, rows):
		from messaging.communication.doctype.communication_template.external import synchronize_templates
		with patch.object(Graph, 'templates') as remote:
			if isinstance(rows, Exception):
				remote.side_effect = rows
			else:
				remote.return_value = rows
			result = synchronize_templates(PROVIDER, binding.channel, binding.name)
			remote.assert_called_once_with()
		return result

	def keep_fixture_transaction(self):
		# Production holds a MariaDB scope lock, restarts the transaction at entry
		# and checkpoints before POST. These fixtures roll back all test rows.
		from messaging.communication.doctype.communication_template import external
		self.enterContext(patch.object(external, 'scope_lock', return_value=nullcontext()))
		return external

	def test_account_identity_and_scope_are_server_validated(self):
		with self.assertRaises(frappe.ValidationError):
			self.new_template().insert()
		with self.assertRaises(frappe.ValidationError):
			self.new_template(self.second).insert()
		one = self.new_template(self.first).insert()
		three = self.new_template(self.third).insert()
		self.assertEqual(one.name, f'{self.first.channel}_{self.first.name}_position_notice_en')
		self.assertNotEqual(one.name, three.name)
		with self.assertRaises(frappe.ValidationError):
			self.new_template(self.first, 'Position--Notice').insert()
		with self.assertRaises(frappe.ValidationError):
			self.new_template(self.first, 'a' * 130).insert()
		one.account = self.third.name
		with self.assertRaises(frappe.ValidationError):
			one.save()

	def test_published_four_parameters_survive_a_five_parameter_draft_and_revocation(self):
		from messaging.communication.doctype.communication_template.communication_template import (
			resolve_output, set_active, validate_template_fields,
		)
		from messaging.communication.doctype.communication_template.status import notification_template_gate
		from messaging.communication.doctype.notification_queue.dispatch import revalidate
		from messaging.communication.doctype.notification_rule.contract import prepared_data

		external = self.keep_fixture_transaction()
		remote = self.remote()
		self.assertEqual(self.sync(self.first, [remote])['created'], 1)
		name = f'{self.first.channel}_{self.first.name}_position_notice_en'
		template = frappe.get_doc('Communication Template', name)
		self.assertEqual((template.status, template.sync_state, template.provider_status),
			('Inactive', 'Synchronized', 'APPROVED'))
		self.assertEqual(template.body, 'Reference {{body_b}} comes before {{body_a}} with {{body_c}} and {{body_d}}.')
		set_active(dict(name=name, modified=str(template.modified)), 1)
		template.reload()
		approved = template.published_revision
		data = {f'body_{key}': str(index) for index, key in enumerate('abcde', 1)}
		with patch.object(Graph, 'save_template') as publish:
			template.body += ' Plus {{body_e}}.'
			template.append('template_variables', dict(parameter_name='body_e'))
			template.save()
		publish.assert_not_called()
		self.assertEqual(template.sync_state, 'Pending')
		self.assertEqual(external.published_revision(template), approved)
		self.assertEqual(self.sync(self.first, [remote])['unchanged'], 1)
		output = resolve_output('Template', data=data, template_reference=name, usage='Notification', account=self.first.name)
		self.assertEqual(output['revision'], approved)
		self.assertEqual(output['variables'], {key: data[key] for key in list(data)[:4]})
		self.assertEqual(output['external_contract']['delivery']['positions']['body'],
			['body_a', 'body_b', 'body_c', 'body_d'])
		self.assertEqual(output['external_contract']['reference'], remote['id'])
		rule = frappe.get_doc(dict(doctype='Notification Rule', account=self.first.name,
			outputs=[dict(status_code='ready', send=1, template_reference=name)]))
		notice = prepared_data(dict(version=1, status='ready', recipient='+15550000001', data=data, message=''), rule)
		self.assertEqual(json.loads(notice['template_contract_json']), output['external_contract'])
		self.assertEqual(notice['template_revision'], approved)
		validate_template_fields(name, {key: 'string' for key in output['variables']}, usage='Notification', account=self.first.name)
		payload = dict(account=self.first.name, channel=self.first.channel, destination='+15550000001',
			source_type='Notification Queue', output_mode='Provider Template', template_reference=name,
			template_revision=approved, variables=output['variables'], outbound_id='c' * 64)
		with patch.object(Graph, 'send', return_value={'messages': [{'id': 'wamid.notice'}]}) as send:
			self.assertEqual(handoff(self.definition, payload)['outcome'], 'Accepted')
			self.assertEqual(send.call_args.args[0]['template']['components'],
				[dict(type='body', parameters=[dict(type='text', text=str(i)) for i in range(1, 5)])])
		with patch.object(Graph, 'templates', return_value=[dict(remote, status='PAUSED')]):
			self.assertEqual(notification_template_gate(name, self.first.name, expected_revision=approved, live=True), 'template_unavailable')
		self.sync(self.first, [dict(remote, status='PAUSED')])
		queued = frappe._dict(template_reference=name, account=self.first.name, content_type='Provider Template', **notice)
		self.assertEqual(revalidate(queued), 'template_unavailable')
		with self.assertRaises(frappe.ValidationError):
			resolve_output('Template', data=data, template_reference=name, usage='Notification', account=self.first.name)
		next_revision = self.remote(body=remote['components'][0]['text'] + ' Plus {{5}}.')
		self.sync(self.first, [next_revision])
		self.assertNotEqual(template.reload().published_revision, approved)
		self.assertEqual(revalidate(queued), 'template_unavailable')
		with self.assertRaisesRegex(frappe.ValidationError, 'body_e'):
			validate_template_fields(name, {key: 'string' for key in output['variables']},
				usage='Notification', account=self.first.name)

	def test_declared_policy_allows_multiple_states_and_denies_unknown(self):
		external = self.keep_fixture_transaction()
		self.sync(self.first, [self.remote()])
		template = frappe.get_doc('Communication Template', f'{self.first.channel}_{self.first.name}_position_notice_en')
		policy = {'APPROVED': {'can_send': True}, 'REVIEW_AVAILABLE': {'can_send': True}, 'PENDING': {'can_send': False}}
		self.assertEqual(dict(replace(definition(), template_statuses=policy).template_statuses), policy)
		contract = dict(self.definition, template_statuses=policy)
		for state, allowed in [('APPROVED', True), ('REVIEW_AVAILABLE', True), ('PENDING', False), ('UNKNOWN', False)]:
			observed = external.stored(template, 'externally_observed')
			template.externally_observed = external.encode(dict(observed, status=state))
			self.assertEqual(external.published_block(template, contract, self.first.name) is None, allowed)

	def test_scoped_import_preserves_drafts_and_ignores_partial_reads(self):
		external = self.keep_fixture_transaction()
		remote = self.remote()
		self.assertEqual(self.sync(self.first, [remote])['created'], 1)
		self.assertEqual(self.sync(self.third, [remote])['created'], 1)
		other = self.remote(reference='9090', body='Other WABA')
		self.assertEqual(self.sync(self.second, [other])['created'], 1)
		names = [f'{item.channel}_{item.name}_position_notice_en' for item in (self.first, self.third, self.second)]
		first, sibling, separate = [frappe.get_doc('Communication Template', name) for name in names]
		first.body += ' Draft.'
		first.save()
		multimedia = self.remote(name='with_buttons', reference='2022')
		multimedia['components'].append({'type': 'BUTTONS', 'buttons': []})
		changed = deepcopy(remote)
		changed['components'][0]['text'] = 'Changed {{1}} and {{2}} with {{3}} and {{4}}.'
		result = self.sync(self.first, [changed, multimedia])
		self.assertEqual(result['skipped'], [dict(reference='2022', name='with_buttons',
			provider_language='en', category='UTILITY', reason='unsupported_buttons')])
		first.reload()
		sibling.reload()
		separate.reload()
		self.assertEqual(first.sync_state, 'Conflict')
		self.assertTrue(first.body.endswith(' Draft.'))
		self.assertEqual(sibling.sync_state, 'Synchronized')
		self.assertTrue(sibling.body.startswith('Changed'))
		self.assertEqual(separate.body, 'Other WABA')
		snapshot = first.published_snapshot
		self.assertFalse(self.sync(self.first, GraphError())['success'])
		first.reload()
		self.assertEqual(first.published_snapshot, snapshot)
		self.assertEqual(first.provider_status, 'APPROVED')
		self.assertEqual(first.sync_state, 'Error')
		self.assertEqual(separate.reload().sync_state, 'Synchronized')
		with patch.object(Graph, 'templates', return_value=[changed]):
			external.adopt_external_template(first.name, str(first.modified))
		self.assertEqual(first.reload().sync_state, 'Synchronized')
		self.assertFalse(first.body.endswith(' Draft.'))

	def test_explicit_publication_records_claim_and_waits_for_approval(self):
		from messaging.communication.doctype.communication_template.status import synchronize_template_status

		external = self.keep_fixture_transaction()
		doc = self.new_template(self.first).insert()
		peer = self.new_template(self.third).insert()
		remote = self.remote(body='Your reference is {{1}}.', status='PENDING')
		initial = []
		graph = Mock()
		graph.templates.side_effect = lambda *args: initial
		options = dict(category='UTILITY', examples={'reference': '42'})

		def save(payload, reference):
			claimed = frappe.get_doc('Communication Template', doc.name)
			self.assertEqual(claimed.sync_state, 'Publishing')
			self.assertTrue(external.stored(claimed, 'publication_request')['pending'])
			self.assertFalse(claimed.published_revision)
			self.assertIsNone(reference)
			self.assertEqual(payload['components'][0]['example'], {'body_text': [['42']]})
			initial.append(remote)
			return {'id': remote['id'], 'status': 'PENDING'}

		graph.save_template.side_effect = save
		with patch.object(templates, 'client', return_value=graph), patch.object(frappe.db, 'commit') as checkpoint:
			result = external.publish_template(doc.name, str(doc.modified), options)
			self.assertEqual(result['state'], 'Publishing')
			checkpoint.assert_called_once()
			graph.save_template.assert_called_once()
			with self.assertRaisesRegex(frappe.ValidationError, 'pending confirmation'):
				external.publish_template(peer.name, str(peer.reload().modified), options)
			graph.save_template.assert_called_once()
			initial[0] = dict(remote, status='APPROVED')
			synchronize_template_status(doc.name)
		self.assertEqual(doc.reload().sync_state, 'Synchronized')
		self.assertTrue(doc.published_revision)
		self.assertEqual(doc.status, 'Inactive')
		self.assertFalse(external.stored(doc, 'publication_request')['pending'])
		self.assertEqual(peer.reload().provider_status, 'APPROVED')

	def test_uncertain_publication_is_not_repeated(self):
		external = self.keep_fixture_transaction()
		doc = self.new_template(self.first).insert()
		options = dict(category='UTILITY', examples={'reference': '42'})
		with patch.object(Graph, 'templates', return_value=[]), patch.object(Graph, 'save_template', side_effect=GraphError()) as publish, patch.object(frappe.db, 'commit'):
			self.assertEqual(external.publish_template(doc.name, str(doc.modified), options)['diagnostic'], 'publication_result_unknown')
			with self.assertRaisesRegex(frappe.ValidationError, 'pending confirmation'):
				external.publish_template(doc.name, str(doc.reload().modified), options)
			publish.assert_called_once()

	def test_signed_status_events_trigger_one_fresh_read_per_waba_template(self):
		external = self.keep_fixture_transaction()
		remote = self.remote()
		self.sync(self.first, [remote])
		self.sync(self.third, [remote])
		name = f'{self.first.channel}_{self.first.name}_position_notice_en'
		entry = dict(field='message_template_status_update', value=dict(
			message_template_name='position_notice', message_template_language='en', event='APPROVED'))
		raw = json.dumps(dict(object='whatsapp_business_account', entry=[dict(id='201', changes=[entry, entry])])).encode()
		frappe.set_user('Guest')
		with patch.object(frappe, 'enqueue') as queue:
			self.assertEqual(self.request(self.first, data=raw, signature=self.sign(raw)).status_code, 200)
			queue.assert_called_once()
			self.assertTrue(queue.call_args.kwargs['enqueue_after_commit'])
		frappe.set_user('Administrator')
		with patch.object(Graph, 'templates', return_value=[dict(remote, status='PAUSED')]) as read:
			external.refresh_template(name)
			read.assert_called_once()
		for account in (self.first, self.third):
			current = f'{account.channel}_{account.name}_position_notice_en'
			self.assertEqual(frappe.db.get_value('Communication Template', current, 'provider_status'), 'PAUSED')

	def test_template_actions_require_management_permissions(self):
		from messaging.communication.doctype.communication_template.external import (
			publish_template, synchronize_templates, template_capabilities,
		)
		doc = self.new_template(self.first).insert()
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):
			template_capabilities()
		with self.assertRaises(frappe.PermissionError):
			synchronize_templates(PROVIDER, self.first.channel, self.first.name)
		with self.assertRaises(frappe.PermissionError):
			publish_template(doc.name, str(doc.modified))
