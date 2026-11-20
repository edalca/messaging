from messaging.registry.communication_providers import normalize_definitions
from messaging.communication.doctype.communication_provider import communication_provider as provider_catalog
"""Controlled synthetic declarations; no network, durable fixtures or external providers."""
from copy import deepcopy
import inspect
import json
import multiprocessing
from secrets import choice
from string import ascii_lowercase
import unittest
from unittest.mock import patch

import frappe

from messaging.communication.doctype.communication_provider import communication_provider as provider_catalog
from messaging.communication.doctype.notification_rule import events as notification_events
from messaging.security.execution_context import _authenticated_adapter
from messaging.communication.doctype.communication_provider.communication_provider import catalog_sync
from messaging.communication.doctype.communication_provider_value.communication_provider_value import parameter_schema, transport_parameters
from messaging.communication.doctype.communication_channel_account.communication_channel_account import resolve_binding, account_provider
from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled, get_provider_states


def resolve_account(binding):
	return binding.doctype == 'Communication Channel Account'


def validate_template(template, variables=None):
	return variables is None or isinstance(variables, dict)


def custom_template_limits(template):
	return {'header': 1, 'body': 2, 'footer': 0}


def invalid_template_limits(template):
	return {'header': -1, 'body': 'unlimited'}


def send(payload):
	return {'outcome': 'Accepted', 'reference': 'synthetic'}


def resolve_account_with_parameters(binding):
	return bool(transport_parameters('TSTPV', binding.name)['Channel']['app_secret'])


def parameter(key, kind='Data', scope='Channel', **kwargs):
	return dict(parameter_key=key, label=key, field_type=kind, scope=scope, **kwargs)


PROBE = dict(id='TSTPV', name='Synthetic provider', app='messaging', version='1.0', capabilities=['text'],
	operations=dict(resolve_account=__name__+'.resolve_account', send=__name__+'.send'), parameters=[
		parameter('app_id', required=1), parameter('app_secret', 'Password', required=1),
		parameter('number', 'Int'), parameter('ratio', 'Float'), parameter('choice', 'Select', options='one\ntwo'),
		parameter('flag', 'Check', scope='Account', default_value='0'), parameter('token', 'Password', scope='Account'),
	])


def declaration(raw):
	"""Build typed synthetic declarations from mutable test scenario data."""
	import sys
	from messaging.registry.communication_providers import CommunicationProvider, CommunicationProviderParameter, CommunicationOperations
	if type(raw) is CommunicationProvider:
		return raw
	try:
		values = dict(raw)
		operations = {}
		for name, function in values.pop('operations').items():
			if isinstance(function, str):
				module, _, symbol = function.rpartition('.')
				function = getattr(sys.modules.get(module), symbol, function)
			operations[name] = function
		values['operations'] = CommunicationOperations(**operations)
		values['parameters'] = tuple(CommunicationProviderParameter(**p) for p in values.get('parameters', ()))
		values['ingress_parameters'] = tuple(values.get('ingress_parameters', {}).items())
		return CommunicationProvider(**values)
	except (TypeError, ValueError, KeyError):
		raise frappe.ValidationError('Invalid synthetic provider declaration.') from None


def sync_communication_providers():
    """Synchronize the test's selected installed declarations through their owner."""
    from messaging.registry import _installed_declarations
    from messaging.registry.communication_providers import register_communication_providers
    return register_communication_providers(app='messaging',
        providers=_installed_declarations('messaging', 'communication_providers'))


class TestCommunicationProvider(unittest.TestCase):
	def setUp(self):
		from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers

		frappe.set_user('Administrator')
		self.declarations = [deepcopy(PROBE), dict(deepcopy(PROBE), id='TSTOT', parameters=[parameter('other_key')])]
		self.enterContext(registered_providers(self.declarations))
		sync_communication_providers()

	def tearDown(self):
		frappe.db.rollback(); frappe.set_user('Administrator')

	def channel(self, provider='TSTPV', enabled=0, parameters=None, *, channel_name=None):
		if channel_name is None:
			suffix = ''.join(choice(ascii_lowercase) for _ in range(10))
			channel_name = f'Synthetic channel {suffix}'
		channel = frappe.get_doc(dict(doctype='Communication Channel', provider=provider, channel_name=channel_name,
			parameters=parameters if parameters is not None else [dict(parameter_key='app_id', value='"synthetic-app"'), dict(parameter_key='app_secret', secret_value='synthetic-secret')])).insert()
		if enabled:
			set_channel_status(channel.name, 'Enabled')
			channel.reload()
		return channel

	def account(self, channel, account_id='fake-001', *, document_name=None, **kw):
		return frappe.get_doc(dict(doctype='Communication Channel Account', channel=channel.name, account_id=account_id,
			account_name='Synthetic account', **kw)).insert(set_name=document_name)

	def test_public_registration_preserves_caller_and_requires_installed_declarations(self):
		from messaging.registry.communication_providers import register_communication_providers
		before = frappe.get_doc('Communication Provider', 'TSTPV').as_dict()
		channels = frappe.db.count('Communication Channel')
		register_communication_providers(app='messaging', providers=tuple(declaration(row) for row in self.declarations))
		self.assertEqual(frappe.get_doc('Communication Provider', 'TSTPV').as_dict(), before)
		self.assertEqual(frappe.db.count('Communication Channel'), channels)
		with self.assertRaises(frappe.PermissionError):
			register_communication_providers(app='frappe', providers=[])
		for declarations in ([], [dict(self.declarations[0], version='uninstalled')], self.declarations * 2):
			with self.assertRaises(frappe.ValidationError):
				register_communication_providers(app='messaging', providers=tuple(declaration(row) for row in declarations))
		with patch.object(frappe.local, 'request', object(), create=True), self.assertRaises(frappe.PermissionError):
			register_communication_providers(app='messaging', providers=tuple(declaration(row) for row in self.declarations))
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):
			register_communication_providers(app='messaging', providers=tuple(declaration(row) for row in self.declarations))

	def test_public_registration_rolls_back_partial_catalogue(self):
		from messaging.registry.communication_providers import register_communication_providers
		from messaging.communication.doctype.communication_provider.communication_provider import CommunicationProvider
		before = frappe.get_doc('Communication Provider', 'TSTPV').as_dict()
		for row in self.declarations:
			row['version'] = '2.0'
		original = CommunicationProvider.validate
		def fail_second(doc):
			if doc.provider_id == 'TSTOT':
				raise frappe.ValidationError('Controlled catalogue failure')
			return original(doc)
		with patch.object(CommunicationProvider, 'validate', fail_second), self.assertRaises(frappe.ValidationError):
			register_communication_providers(app='messaging', providers=tuple(declaration(row) for row in self.declarations))
		self.assertEqual(frappe.get_doc('Communication Provider', 'TSTPV').as_dict(), before)

	def test_ordinary_document_and_rpc_mutations_are_rejected(self):
		import frappe.client
		for values in (dict(doctype='Communication Provider', provider_id='TSTNEW', provider_name='Forged', application='messaging'),):
			with self.assertRaises(frappe.PermissionError):
				frappe.client.insert(values)
		with self.assertRaises(frappe.PermissionError):
			frappe.client.set_value('Communication Provider', 'TSTPV', 'provider_name', 'Forged')
		with self.assertRaises(frappe.PermissionError):
			frappe.client.delete('Communication Provider', 'TSTPV')
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc('Communication Provider', 'TSTPV').delete(ignore_permissions=True)

	def test_catalog_idempotence_version_and_protected_identity(self):
		doc = frappe.get_doc('Communication Provider', 'TSTPV')
		self.assertFalse(get_provider_states().get('TSTPV', False))
		sync_communication_providers(); self.assertEqual(frappe.get_doc(doc.doctype, doc.name).modified, doc.modified)
		self.declarations[0]['version'] = '1.1'; self.declarations[0]['parameters'][0]['label'] = 'Application identifier'
		sync_communication_providers(); self.assertEqual(frappe.db.get_value(doc.doctype, doc.name, 'version'), '1.1')
		doc.reload(); doc.provider_name = 'Tampered'
		with self.assertRaises(frappe.PermissionError): doc.save(ignore_permissions=True)
		with catalog_sync():
			doc.provider_id = 'OTHER'
			with self.assertRaises(frappe.ValidationError): doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError): frappe.get_doc(dict(doctype='Communication Provider',provider_id='FRESH')).insert()

	def test_registration_rejects_invalid_ids_owners_routes_and_duplicates(self):
		for invalid in [dict(PROBE,id='bad_id'), dict(PROBE,id='ABCD'), dict(PROBE,id='ABCDEFGHIJK'), dict(PROBE,app='frappe'), dict(PROBE,operations=dict(PROBE['operations'],send='os.system'))]:
			self.declarations[:] = [invalid]
			with self.assertRaises(frappe.ValidationError): sync_communication_providers()
		self.declarations[:] = [PROBE, PROBE]
		with self.assertRaises(frappe.ValidationError): sync_communication_providers()
		with self.assertRaises(frappe.ValidationError):
			normalize_definitions('absent_app', [declaration(PROBE)])
		self.declarations.clear()
		with self.assertRaises(frappe.ValidationError): provider_catalog.provider_definition('TSTPV')

	def test_parameter_schema_rejects_unsafe_and_ambiguous_definitions(self):
		for parameters in [[parameter('same'),parameter('same',scope='Account')], [parameter('bad',scope='Global')],
			[parameter('bad','Code')], [parameter('bad','Table',options='User')], [parameter('bad','Password',default_value='unsafe')],
			[parameter('bad','Select',options='one\none')], [parameter('bad','Data',options='python')]]:
			self.declarations[:] = [dict(PROBE, parameters=parameters)]
			with self.assertRaises(frappe.ValidationError): sync_communication_providers()

	def test_schema_evolution_preserves_values_secrets_and_reports_incompatibility(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		for field, value in [('field_type', 'Float'), ('scope', 'Account')]:
			original=deepcopy(self.declarations[0]['parameters'])
			self.declarations[0]['parameters'][2][field]=value
			result = sync_communication_providers()
			self.assertFalse(result['configurations']['TSTPV']['incompatible_channels'])
			self.declarations[0]['parameters']=original
		sync_communication_providers()
		channel=self.channel(enabled=1); row=channel.parameters[1]; name=row.name
		self.declarations[0]['version']='2.0'; self.declarations[0]['parameters'].append(parameter('new_value',default_value='new'))
		sync_communication_providers(); channel.reload()
		self.assertEqual(channel.parameters[1].name,name)
		self.assertEqual(row.get_password('secret_value'),'synthetic-secret')
		self.assertIn('new_value',[p['parameter_key'] for p in parameter_schema('Communication Channel',provider='TSTPV')['parameters']])
		for change in ('type','scope','remove'):
			original=deepcopy(self.declarations[0]['parameters'])
			if change=='type':self.declarations[0]['parameters'][0]['field_type']='Int'
			elif change=='scope':self.declarations[0]['parameters'][0]['scope']='Account'
			else:self.declarations[0]['parameters']=self.declarations[0]['parameters'][1:]
			result = sync_communication_providers()
			self.assertEqual(result['configurations']['TSTPV']['incompatible_channels'], [channel.name])
			self.assertEqual(channel.reload().status, 'Enabled')
			self.assertEqual(row.get_password('secret_value'), 'synthetic-secret')
			self.declarations[0]['parameters']=original

	def test_required_scopes_types_and_canonical_values(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		incomplete=self.channel(parameters=[])
		with self.assertRaises(frappe.ValidationError):
			set_channel_status(incomplete.name, 'Enabled')
		self.declarations[0]['parameters'].append(parameter('multiline','Small Text'));sync_communication_providers()
		channel=self.channel();channel.append('parameters',dict(parameter_key='multiline',value=json.dumps('Hola {{name}}.\n¡Gracias! 🙂')));channel.save();channel.reload()
		self.assertEqual(json.loads(channel.parameters[-1].value),'Hola {{name}}.\n¡Gracias! 🙂')
		channel.append('parameters',dict(parameter_key='number',value='"12"'))
		channel.append('parameters',dict(parameter_key='ratio',value='1.25'));channel.append('parameters',dict(parameter_key='choice',value='"one"'));channel.save();channel.reload()
		self.assertEqual(json.loads(next(r.value for r in channel.parameters if r.parameter_key=='number')),12)
		account=self.account(channel);self.assertEqual(json.loads(account.parameters[0].value),False)
		self.assertEqual(parameter_schema('Notification Rule',account=account.name)['parameters'],[])
		for key,value in [('flag','true'),('other_key','"foreign"'),('number','1.5'),('ratio','"NaN"'),('choice','"invalid"')]:
			bad=self.channel();bad.append('parameters',dict(parameter_key=key,value=value))
			with self.assertRaises(frappe.ValidationError):bad.save()
		channel.append('parameters',dict(parameter_key='app_id',value='"duplicate"'))
		with self.assertRaises(frappe.ValidationError):channel.save()
		account.append('parameters',dict(parameter_key='app_id',value='"wrong-scope"'))
		with self.assertRaises(frappe.ValidationError):account.save()

	def test_effective_encryption_mask_preservation_replacement_and_cleanup(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		channel=self.channel(enabled=1);row=channel.parameters[1];name=row.name
		self.assertNotIn('synthetic-secret',channel.as_json())
		stored=frappe.db.get_value('Communication Provider Value',name,['value','secret_value'],as_dict=True)
		self.assertIsNone(stored.value);self.assertEqual(stored.secret_value,'********')
		auth=frappe.db.get_value('__Auth',dict(doctype='Communication Provider Value',name=name,fieldname='secret_value'),['password','encrypted'],as_dict=True,order_by='name')
		self.assertEqual(auth.encrypted,1);self.assertNotIn('synthetic-secret',auth.password)
		channel.reload();channel.parameters[1].secret_value='';channel.channel_name='New visible name';channel.save()
		self.assertEqual(channel.parameters[1].get_password('secret_value'),'synthetic-secret')
		channel.parameters[1].secret_value='replacement-secret';channel.save()
		self.assertEqual(channel.parameters[1].get_password('secret_value'),'replacement-secret')
		account=self.account(channel,enabled=1);set_provider_enabled('TSTPV',True)
		with _authenticated_adapter('TSTPV', account.name):
			self.assertEqual(transport_parameters('TSTPV',account.name)['Channel']['app_secret'],'replacement-secret')
		self.declarations[0]['operations']['resolve_account'] = __name__ + '.resolve_account_with_parameters'
		sync_communication_providers()
		resolve_binding('TSTPV',account.name)
		frappe.session.user='another-principal'
		try:
			with self.assertRaises(frappe.PermissionError):transport_parameters('TSTPV',account.name)
		finally:frappe.set_user('Administrator')
		self.assertNotIn('replacement-secret',frappe.get_doc(channel.doctype,channel.name).as_json())
		with self.assertRaises(frappe.ValidationError):frappe.rename_doc(channel.doctype,channel.name,'new-identity')
		set_channel_status(channel.name, 'Disabled')
		channel.reload();channel.set('parameters',[channel.parameters[0]]);channel.save()
		self.assertFalse(frappe.db.get_value('__Auth',dict(doctype='Communication Provider Value',name=name,fieldname='secret_value'),'name',order_by='name'))

	def test_value_ownership_cannot_be_stolen_and_no_direct_child_write(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		one=self.channel();two=self.channel();two.parameters[1].name=one.parameters[1].name
		with self.assertRaises(frappe.PermissionError):two.save()
		child=frappe.get_doc('Communication Provider Value',one.parameters[1].name)
		with self.assertRaises(frappe.PermissionError):child.save(ignore_permissions=True)
		fake=self.channel();fake.parameters[1].name=frappe.generate_hash();fake.parameters[1].secret_value='********'
		with self.assertRaises(frappe.ValidationError):fake.save()

	def test_multiple_channels_accounts_identity_and_enablement_layers(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		one=self.channel(enabled=1);two=self.channel(enabled=1)
		a=self.account(one,enabled=1)
		# Account IDs are globally unique, even across channels.
		a.reload()
		before = a.as_dict()
		counts = [frappe.db.count(dt) for dt in (a.doctype, 'Communication Provider Value', '__Auth')]
		with patch('messaging.emulator.doctype.emulator_contact.emulator_contact.generate_emulator_account_id') as generator:
			with self.assertRaises(frappe.DuplicateEntryError):
				self.account(two, parameters=[dict(parameter_key='token', secret_value='synthetic-collision-secret')])
			generator.assert_not_called()
		self.assertEqual([frappe.db.count(dt) for dt in (a.doctype, 'Communication Provider Value', '__Auth')], counts)
		self.assertEqual(frappe.get_doc(a.doctype, a.name).as_dict(), before)
		b=self.account(two, account_id='fake-002', enabled=1)
		self.assertEqual((a.name, b.name), (a.account_id, b.account_id))
		self.assertNotEqual(a.name,b.name);self.assertEqual(account_provider(a.name),'TSTPV')
		self.assertIsNone(frappe.get_meta('Emulator Contact').get_field('account'))
		with self.assertRaises((frappe.DuplicateEntryError,frappe.UniqueValidationError)):self.account(one)
		set_provider_enabled('TSTPV',True)
		with _authenticated_adapter('TSTPV', a.name):
			resolve_binding('TSTPV',a.name,incoming=True)
		set_channel_status(one.name, 'Disabled')
		one.reload()
		with self.assertRaises(frappe.PermissionError):resolve_binding('TSTPV',a.name)
		resolve_binding('TSTPV',b.name)
		a.channel=two.name
		with self.assertRaises(frappe.ValidationError):a.save()
		one.provider='TSTOT'
		with self.assertRaises(frappe.ValidationError):one.save()
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):parameter_schema('Communication Channel',provider='TSTPV')
		with self.assertRaises(frappe.PermissionError):self.channel()

	def test_single_channel_constraint_is_declared_and_persistent(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		self.declarations[1]['single_channel']=True;sync_communication_providers()
		self.channel('TSTOT',parameters=[])
		with self.assertRaises(frappe.ValidationError):self.channel('TSTOT',parameters=[])
		self.assertFalse(frappe.get_meta('Communication Channel Account').has_field('provider'))

	def test_stale_edits_do_not_overwrite_secrets_and_parent_deletion_cleans_auth(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		channel=self.channel();stale=frappe.get_doc(channel.doctype,channel.name)
		channel.parameters[1].secret_value='new-synthetic-secret';channel.save()
		stale.channel_name='Stale label'
		with self.assertRaises(frappe.TimestampMismatchError):stale.save()
		channel.reload();self.assertEqual(channel.parameters[1].get_password('secret_value'),'new-synthetic-secret')
		name=channel.parameters[1].name
		with self.assertRaises(frappe.PermissionError):frappe.delete_doc('Communication Provider Value',name,ignore_permissions=True)
		frappe.delete_doc(channel.doctype,channel.name)
		self.assertFalse(frappe.db.get_value('__Auth',{'doctype':'Communication Provider Value','name':name},'name',order_by='name'))



	def test_template_scope_contract_does_not_override_general_templates(self):
		set_provider_enabled('TSTPV', True)
		from messaging.communication.doctype.communication_template.communication_template import validate_template_structure
		self.declarations[0]['capabilities'].append('templates')
		self.declarations[0]['operations']['validate_template'] = __name__+'.validate_template'
		self.declarations[0]['parameters'].extend([parameter('external_name', scope='Template', required=1),
			parameter('template_secret', 'Password', scope='Template'), parameter('template_count', 'Int', scope='Template'),
			parameter('template_language', 'Link', scope='Template', options='Language')])
		sync_communication_providers()
		channel=self.channel()
		self.assertEqual({p['parameter_key'] for p in provider_catalog.provider_definition('TSTPV')['parameters'] if p['scope'] == 'Template'},
			{'external_name','template_secret','template_count','template_language'})
		account=self.account(channel)
		self.assertEqual(len(parameter_schema('Communication Template', channel=channel.name)['parameters']),4)
		doc=frappe.get_doc(dict(doctype='Bot Flow Template', template_name='General template', language='es', enabled=1, body='Static body'))
		validate_template_structure(doc)
		self.assertTrue(frappe.get_meta('Communication Template').get_field('parameters'))
		with self.assertRaises(frappe.ValidationError):
			parameter_schema('Communication Template')
		self.declarations[0]['capabilities'].remove('templates')
		with self.assertRaises(frappe.ValidationError):sync_communication_providers()

	def test_provider_custom_template_variable_limits_and_universal_invariants(self):
		from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings
		configure_test_settings()
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		# Provider cannot register template_variable_limits without 'templates' capability or with arbitrary expressions.
		self.declarations[0]['operations']['template_variable_limits'] = __name__ + '.custom_template_limits'
		with self.assertRaises(frappe.ValidationError):
			provider_catalog.provider_definitions()
		self.declarations[0]['capabilities'].append('templates')
		self.declarations[0]['operations']['validate_template'] = __name__ + '.validate_template'
		for bad_path in ['lambda doc: {"body": 1}', 'os.system', 'messaging.communication._private.limits']:
			self.declarations[0]['operations']['template_variable_limits'] = bad_path
			with self.assertRaises(frappe.ValidationError):
				provider_catalog.provider_definitions()

		self.declarations[0]['operations']['template_variable_limits'] = __name__ + '.custom_template_limits'
		sync_communication_providers()
		channel = self.channel(enabled=1)
		account = self.account(channel, enabled=1)

		def make_template(header='', body='', footer='', variables=()):
			doc = frappe.get_doc(dict(
				doctype='Communication Template', channel=channel.name, status='Inactive',
				template_name='Limits template '+frappe.generate_hash(length=10), language='es',
				header=header, body=body, footer=footer,
				template_variables=[dict(parameter_name=v) for v in variables],
			))
			from messaging.communication.doctype.communication_template.communication_template import validate_template_structure
			validate_template_structure(doc, definition=provider_catalog.provider_definition('TSTPV'))
			return doc

		# Valid under custom limits: header=1, body=2, footer=0 (literal footer allowed).
		valid = make_template(
			header='Estimado {{nombre_abonado}}',
			body='Contrato {{numero_contrato}} saldo {{monto_pendiente}}',
			footer='Pie informativo sin marcadores',
			variables=('nombre_abonado', 'numero_contrato', 'monto_pendiente'),
		).insert()
		self.assertEqual(len(valid.template_variables), 3)

		# Body exceeds custom limit of 2 variables.
		with self.assertRaisesRegex(frappe.ValidationError, 'at most 2'):
			make_template(
				body='{{uno}} {{dos}} {{tres}}',
				variables=('uno', 'dos', 'tres'),
			).insert()

		# Footer limit 0 forbids any variable in Footer.
		with self.assertRaisesRegex(frappe.ValidationError, 'cannot contain variables'):
			make_template(
				body='Contrato {{numero_contrato}}',
				footer='Pie {{pie_variable}}',
				variables=('numero_contrato', 'pie_variable'),
			).insert()

		# Universal rules remain strictly enforced regardless of custom provider limits.
		for bad_case in [
			dict(body='Hola {{NombreAbonado}}', variables=('NombreAbonado',)),
			dict(body='Hola {{contrato1}}', variables=('contrato1',)),
			dict(body='Hola {{no_declarada}}', variables=()),
			dict(body='Texto fijo', variables=('sin_usar',)),
			dict(body='Hola {{ espaciada }}', variables=('espaciada',)),
		]:
			with self.assertRaises(frappe.ValidationError):
				make_template(**bad_case).insert()

		# Malformed provider return payload is rejected safely.
		self.declarations[0]['operations']['template_variable_limits'] = __name__ + '.invalid_template_limits'
		sync_communication_providers()
		with self.assertRaisesRegex(frappe.ValidationError, 'Invalid provider template variable limits'):
			make_template(body='Texto fijo', variables=()).insert()

	def test_provider_enablement_is_protected_persisted_and_preserved_by_sync(self):
		from messaging.communication.doctype.communication_provider.communication_provider import get_provider_enabled
		self.assertFalse(get_provider_enabled('TSTPV'))
		revision = frappe.db.get_value('Communication Provider', 'TSTPV', 'modified')
		set_provider_enabled('TSTPV', True)
		self.assertTrue(get_provider_enabled('TSTPV'))
		self.assertFalse(get_provider_enabled('TSTOT'))
		sync_communication_providers()
		self.assertTrue(get_provider_enabled('TSTPV'))
		self.assertEqual(frappe.db.get_value('Communication Provider', 'TSTPV', 'modified'), revision)
		doc = frappe.get_doc('Communication Provider','TSTPV');doc.status='Disabled'
		with self.assertRaises(frappe.PermissionError):doc.save(ignore_permissions=True)
		self.assertTrue(get_provider_enabled('TSTPV'))
		for value in ('true', 'false', None, 2, [], {}):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):set_provider_enabled('TSTPV',value)
		self.assertEqual(frappe.get_attr(set_provider_enabled.__module__+'.set_provider_enabled'),set_provider_enabled)
		self.assertEqual(frappe.whitelisted.__contains__(set_provider_enabled),True)
		frappe.set_user('Guest')
		with self.assertRaises(frappe.PermissionError):set_provider_enabled('TSTPV',False)
		frappe.set_user('Administrator')
		with patch('messaging.communication.doctype.communication_provider.communication_provider.is_available',return_value=False):
			with self.assertRaises(frappe.ValidationError):set_provider_enabled('TSTPV',True)
			self.assertFalse(set_provider_enabled('TSTPV',False)['enabled'])

	def test_disabled_channel_consumes_single_slot_and_stays_editable(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		self.declarations[0]['single_channel'] = True
		sync_communication_providers()
		channel = self.channel(enabled=0)
		with self.assertRaises(frappe.ValidationError):
			self.channel(enabled=0)
		channel.channel_name = 'Edited disabled channel'
		channel.save()
		self.assertEqual(channel.status, 'Disabled')
		self.assertEqual(channel.channel_name, 'Edited disabled channel')
		self.assertEqual(frappe.db.get_value('Communication Channel', channel.name, 'single_provider_key'), 'TSTPV')

	def test_single_channel_policy_change_rejects_multiple_and_accepts_one(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		one = self.channel()
		two = self.channel()
		self.declarations[0]['single_channel'] = True
		with self.assertRaises(frappe.ValidationError):
			sync_communication_providers()
		frappe.delete_doc('Communication Channel', two.name)
		sync_communication_providers()
		self.assertEqual(frappe.db.get_value('Communication Provider', 'TSTPV', 'single_channel'), 1)
		self.assertEqual(frappe.db.get_value('Communication Channel', one.name, 'single_provider_key'), 'TSTPV')
		one.reload()
		one.channel_name = 'Still editable'
		one.save()
		self.assertEqual(one.channel_name, 'Still editable')
		self.declarations[0]['single_channel'] = False
		sync_communication_providers()
		self.assertFalse(frappe.db.get_value('Communication Channel', one.name, 'single_provider_key'))
		self.channel()
		self.assertEqual(frappe.db.count('Communication Channel', {'provider': 'TSTPV'}), 2)
		self.assertFalse(frappe.db.get_value('Communication Provider', 'TSTPV', 'fixed_channel_id'))

	def test_fixed_channel_id_is_independent_of_cardinality(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		from messaging.communication.doctype.communication_channel.communication_channel import CommunicationChannel
		from messaging.communication.doctype.communication_provider.communication_provider import _write_provider_catalog
		from messaging.registry.communication_providers import register_communication_providers
		source = inspect.getsource(CommunicationChannel) + inspect.getsource(register_communication_providers) + inspect.getsource(_write_provider_catalog)
		self.assertNotIn('EMULR', source)
		self.assertNotIn('emulator', source)
		self.declarations[0]['fixed_channel_id'] = 'fixed-one'
		self.declarations[0]['single_channel'] = False
		sync_communication_providers()
		self.assertFalse(frappe.db.exists('Communication Channel', {'provider': 'TSTPV'}))
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(dict(doctype='Communication Channel', provider='TSTPV', channel_name='Wrong identity')).insert(set_name='other-fixed')
		first = self.channel()
		second = self.channel()
		self.assertEqual(first.name, 'fixed-one')
		self.assertEqual(second.name, second.channel_name.lower().replace(' ', '_'))
		self.assertEqual(frappe.db.get_value('Communication Provider', 'TSTPV', 'single_channel'), 0)
		self.declarations[1]['single_channel'] = True
		sync_communication_providers()
		self.assertFalse(frappe.db.get_value('Communication Provider', 'TSTOT', 'fixed_channel_id'))
		only = self.channel('TSTOT', parameters=[])
		with self.assertRaises(frappe.ValidationError):
			self.channel('TSTOT', parameters=[])
		self.assertNotEqual(only.name, 'fixed-one')
		self.assertEqual(frappe.db.get_value('Communication Provider', 'TSTPV', 'fixed_channel_id'), 'fixed-one')

	def test_invalid_fixed_channel_id_and_unsynced_provider_are_rejected(self):
		self.declarations[0]['fixed_channel_id'] = 'bad id'
		with self.assertRaises(frappe.ValidationError):
			provider_catalog.provider_definitions()
		self.declarations[0].pop('fixed_channel_id')
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(dict(doctype='Communication Channel', provider='NOSUCH', channel_name='Missing')).insert()
		frappe.db.set_value('Communication Provider', 'TSTPV', 'definition_hash', '0' * 64, update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			self.channel()

	def test_concurrent_single_channel_inserts_allow_only_one(self):
		set_provider_enabled('TSTPV', True); set_provider_enabled('TSTOT', True)
		self.declarations[0]['single_channel'] = True
		sync_communication_providers()
		frappe.db.commit()
		try:
			context = multiprocessing.get_context('spawn')
			barrier = context.Barrier(2)
			results = context.Queue()
			children = [context.Process(target=concurrent_single_channel_insert, args=(frappe.local.site, str(frappe.local.sites_path), barrier, results)) for _ in range(2)]
			for child in children:
				child.start()
			for child in children:
				child.join(40)
				self.assertFalse(child.is_alive())
				self.assertEqual(child.exitcode, 0)
			outcomes = [results.get(timeout=5) for _ in range(2)]
			# commit() starts the next transaction before the children write.
			frappe.db.rollback()
			self.assertEqual(sum(1 for row in outcomes if row.get('name')), 1, outcomes)
			self.assertEqual(sum(1 for row in outcomes if row.get('error')), 1, outcomes)
			self.assertEqual(frappe.db.count('Communication Channel', {'provider': 'TSTPV'}), 1)
		finally:
			frappe.db.rollback()
			frappe.set_user('Administrator')
			for name in frappe.get_all('Communication Channel', filters={'provider': ['in', ('TSTPV', 'TSTOT')]}, pluck='name'):
				frappe.delete_doc('Communication Channel', name, force=1)
			with catalog_sync():
				for provider in ('TSTPV', 'TSTOT'):
					if frappe.db.exists('Communication Provider', provider):
						frappe.delete_doc('Communication Provider', provider, force=1, ignore_permissions=True)
			frappe.db.commit()


def concurrent_single_channel_insert(site, sites_path, barrier, results):
	from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import registered_providers

	frappe.init(site=site, sites_path=sites_path)
	frappe.connect()
	frappe.set_user('Administrator')

	try:
		with registered_providers([dict(PROBE, single_channel=True)]):
			barrier.wait(timeout=20)
			doc = frappe.get_doc(dict(doctype='Communication Channel', provider='TSTPV', channel_name='Concurrent channel')).insert()
			frappe.db.commit()
			results.put(dict(name=doc.name))
	except Exception as exc:
		frappe.db.rollback()
		results.put(dict(error=type(exc).__name__))
	finally:
		frappe.destroy()



def invalid_send_signature():
    return {'outcome': 'Accepted'}


class TestProviderDeclarations(unittest.TestCase):
    def setUp(self):
        frappe.set_user('Administrator')

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user('Administrator')

    def test_immutable_contracts_and_safe_function_references(self):
        from dataclasses import replace, FrozenInstanceError
        from messaging.presets.communication_providers.emulr import definition
        emulator = definition()
        with self.assertRaises(FrozenInstanceError):
            emulator.id = 'OTHER'
        for mutation in (
            lambda: replace(emulator, operations=replace(emulator.operations, send=invalid_send_signature)),
            lambda: replace(emulator, operations=replace(emulator.operations, send='os.system')),
            lambda: replace(emulator, operations=replace(emulator.operations, send=frappe.get_doc)),
            lambda: replace(emulator, capabilities=('text', 'text')),
            lambda: replace(emulator, parameters=emulator.parameters * 2),
            lambda: replace(emulator, app='frappe'),
        ):
            with self.subTest(mutation=mutation), self.assertRaises((frappe.ValidationError, ValueError)):
                normalize_definitions('messaging', (mutation(),))
        with self.assertRaises(frappe.ValidationError):
            normalize_definitions('messaging', ({'id': 'EMULR'},))

    def test_setup_preserves_generic_provider_catalog_without_functional_data(self):
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import install_synthetic_provider
        install_synthetic_provider(self)
        from messaging import hooks
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import sync_ai_providers
        before = {dt: frappe.db.count(dt) for dt in ('Communication Channel', 'Communication Channel Account',
            'Emulator Contact', 'Chat Contact', 'Bot Flow', 'Bot Parameter', 'AI Configuration', 'Process Definition')}
        self.assertFalse(hasattr(hooks, 'messaging_' + 'channel_providers'))
        sync_communication_providers()
        sync_ai_providers()
        before_documents = {dt: frappe.get_doc(dt, key).as_dict() for dt, key in
            (('Communication Provider', 'EMULR'), ('AI Provider', 'synthetic'))}
        sync_communication_providers()
        self.assertEqual(sync_ai_providers()['unchanged'], 1)
        for dt, key in (('Communication Provider', 'EMULR'), ('AI Provider', 'synthetic')):
            self.assertEqual(frappe.get_doc(dt, key).as_dict(), before_documents[dt])
        self.assertEqual({dt: frappe.db.count(dt) for dt in before}, before)
        self.assertIsNone(frappe.get_meta('Communication Channel').get_field('adapter'))
        self.assertNotIn('adapter', frappe.db.get_table_columns('Communication Channel'))
        definition = provider_catalog.provider_definition('EMULR')
        self.assertTrue(all(path.startswith('messaging.emulator.provider.') for path in definition['operations'].values()))


class TestTypedCapabilities(unittest.TestCase):
    def test_public_capabilities_preserve_serialization_and_reject_unknown_values(self):
        from dataclasses import replace
        from messaging.registry.communication_providers import CAPABILITIES, CommunicationCapability
        from messaging.presets.communication_providers.emulr import definition
        emulator = definition()
        self.assertEqual(len(CommunicationCapability), 11)
        self.assertIn(CommunicationCapability.TEMPLATE_STATUS, CommunicationCapability)
        self.assertEqual({capability.value for capability in CommunicationCapability}, CAPABILITIES)
        self.assertTrue(all(isinstance(capability, CommunicationCapability) for capability in emulator.capabilities))
        self.assertEqual(json.loads(frappe.as_json(emulator.capabilities)),
            ['text', 'interactive', 'templates', 'delivery_status', 'recipient_redirect', 'notification_redirect_required'])
        self.assertEqual(str(CommunicationCapability.TEXT), 'text')
        with self.assertRaises(ValueError):
            replace(emulator, capabilities=('text', 'unknown'))
        self.assertEqual(replace(emulator, capabilities=('text',)).capabilities, (CommunicationCapability.TEXT,))
        self.assertIs(emulator.operations.send, definition().operations.send)
