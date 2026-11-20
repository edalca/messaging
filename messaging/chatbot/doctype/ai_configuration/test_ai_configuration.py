"""Focused provider, wire, privacy, network and administrative integrity contracts."""
from messaging.chatbot.doctype.bot_settings.interpretation import build_request, normalize_response
import base64
import copy
import io
import json
import multiprocessing
import subprocess
import time
import unittest
from pathlib import Path
from contextlib import contextmanager
from socket import AF_INET, SOCK_STREAM
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from messaging.registry import ai_providers as wire
from messaging.chatbot.doctype.ai_configuration import execution as adapters, rest_transport
from messaging.registry.ai_providers.rest import DECISION_WIRE, EXTRACTION_WIRE
from messaging.chatbot.doctype.ai_configuration import ai_configuration as config
from messaging.chatbot.doctype.ai_provider import ai_provider as setup
from messaging.chatbot.doctype.ai_provider.test_ai_provider import sync_ai_providers, synthetic_wire_response
from messaging.chatbot.doctype.bot_settings.interpretation import Decision, InvalidInterpretation
from messaging.chatbot.doctype.bot_parameter.bot_parameter import recognize, captured_values


JSON_HEADERS = {'Content-Type': 'application/json'}


def rest_decision_body(request):
    options = [row['ref'] for row in request['available_flows']]
    options.extend(signal.value for signal in wire.DecisionSignal if request[signal.value + '_available'])
    return dict(wire_version=DECISION_WIRE, schema_version=request['schema_version'], request_id=request['request_id'],
        results=[dict(option_id=option, classification='absent') for option in options])


@contextmanager
def simulated_rest_http(reply, *, dns_error=None):
    """Run the production transport worker in process with deterministic DNS and HTTPS."""
    pool = MagicMock()

    def post(_method, _path, *, body, headers, **_kwargs):
        status, response_headers, content = reply(wire.strict_json(body), headers)
        return rest_transport.urllib3.response.HTTPResponse(body=io.BytesIO(content), headers=response_headers,
            status=status, preload_content=False)

    pool.urlopen.side_effect = post

    def child(command, *, input, **_settings):
        if command[1:] != ['-m', rest_transport.__name__]:
            raise AssertionError('Unexpected child process')
        streams = SimpleNamespace(stdin=SimpleNamespace(buffer=io.BytesIO(input)),
            stdout=SimpleNamespace(buffer=io.BytesIO()))
        with patch.object(rest_transport, 'sys', streams):
            rest_transport._worker()
        return SimpleNamespace(stdout=streams.stdout.buffer.getvalue())

    address = [(AF_INET, SOCK_STREAM, 6, '', ('93.184.215.14', 443))]
    with (
        patch.object(rest_transport.subprocess, 'run', side_effect=child),
        patch.object(rest_transport.socket, 'getaddrinfo', return_value=address, side_effect=dns_error),
        patch.object(rest_transport.urllib3, 'HTTPSConnectionPool', return_value=pool) as connect,
    ):
        yield connect, pool


def successful_probe(provider, values, request, *, policy=None):
    if provider.provider_type == 'Internal':
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import synthetic_interpret
        return synthetic_interpret(request, values)
    return synthetic_wire_response(request, Decision(outcome='empty_catalog'), .99)


def create_configuration(name, provider='synthetic', **values):
    doc = frappe.get_doc(dict(doctype='AI Configuration',configuration_name=name,provider=provider))
    for key, value in values.items():
        doc.append('parameters', dict(parameter_key=key, **({'secret_value':value} if key in ('api_key','auth_secret') else {'value':json.dumps(value)})))
    with patch.object(config, 'run', side_effect=successful_probe), patch(
            'messaging.chatbot.doctype.bot_settings.language.resolve_process_language', return_value='es'):
        doc.insert()
        config.check_connection(doc)
    # Callers of this fixture supply a separate approval double.
    adapters.set_profile_status(doc, 'Enabled')
    return doc


def activation_worker(site, sites_path, name, barrier, results):
    frappe.init(site=site,sites_path=sites_path);frappe.connect();frappe.set_user('Administrator')
    try:
        doc=frappe.get_single('Bot Settings')
        barrier.wait(timeout=15)
        doc.active_ai_configuration=name
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import SYNTHETIC
        with patch.object(adapters, 'internal_provider', return_value=SYNTHETIC), patch('messaging.chatbot.doctype.ai_evaluation_run.runtime.approval', return_value={'approved': True}):
            doc.save()
        frappe.db.commit()
        results.put('selected')
    except Exception as exc:
        frappe.db.rollback();results.put(type(exc).__name__)
    finally:
        frappe.destroy()


class TestAIConfiguration(unittest.TestCase):
    def setUp(self):
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import install_synthetic_provider
        frappe.set_user("Administrator")
        install_synthetic_provider(self)
        frappe.db.commit()
        self.test_mode = patch.object(frappe, 'in_test', True)
        self.test_mode.start()
        self.addCleanup(self.test_mode.stop)
        # These tests isolate connectivity. Functional approval has its own real gate suite.
        self.functional = patch('messaging.chatbot.doctype.ai_evaluation_run.runtime.approval', return_value={'approved': True})
        self.functional.start()
        self.addCleanup(self.functional.stop)
        frappe.set_user('Administrator')
        self.prefix='ai_test_'+frappe.generate_hash(length=10)
        self.original=frappe.get_single('Bot Settings')
        if not self.original.chatbot_languages:
            from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings, restore_bot_settings
            self.addCleanup(restore_bot_settings, self.original.as_dict())
            configure_test_settings()
        frappe.db.set_single_value('Bot Settings', {'interpretation_enabled':1, 'registration_enabled':0, 'greeting_enabled':0}, update_modified=False)
        self.logs=set(frappe.get_all('AI Request Log',pluck='name'))

    def tearDown(self):
        frappe.db.rollback();frappe.set_user('Administrator')
        # Concurrency and engine calls own commits; remove only this test's named fixtures.
        for key in ('active_ai_configuration','interpretation_enabled','registration_enabled','greeting_enabled'):
            frappe.db.set_single_value('Bot Settings',key,self.original.get(key),update_modified=False)
        frappe.db.commit()
        names=frappe.get_all('AI Configuration',filters={'name':['like',self.prefix+'%']},pluck='name')
        for name in names:
            frappe.delete_doc('AI Configuration',name,force=True)
        for name in frappe.get_all('AI Provider',filters={'name':['like',self.prefix+'%']},pluck='name'):
            frappe.delete_doc('AI Provider',name,force=True)
        for name in set(frappe.get_all('AI Request Log',pluck='name'))-self.logs:
            log=frappe.get_doc('AI Request Log',name)
            if log.error_log:frappe.db.delete('Error Log',{'name':log.error_log})
            frappe.db.delete('AI Request Log',{'name':name})
        frappe.db.commit()

    def provider(self, profile_type='Decision', *, suffix='p'):
        doc = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix+'_'+suffix, provider_name='Synthetic classifier',
            provider_type='External', decision=int(profile_type=='Decision'), parameter_extraction=int(profile_type=='Parameter Extraction'), schema_version='1')).insert()
        setup.set_provider_enabled(doc.name, True)
        return doc.reload()

    def test_profile_purposes_preserve_existing_configuration_and_secrets(self):
        doc = create_configuration(self.prefix+'_purpose', api_key='synthetic-purpose')
        self.assertEqual(doc.profile_type, 'Decision')
        doc.reload()
        before = doc.as_dict()
        frappe.db.set_value(doc.doctype, doc.name, 'profile_type', '', update_modified=False)
        setup_hook = config.on_doctype_update
        with patch.object(config, 'run') as provider:
            setup_hook(); setup_hook()
            provider.assert_not_called()
        doc.reload()
        self.assertEqual(doc.as_dict(), before)
        row = next(r for r in doc.parameters if r.parameter_key == 'api_key')
        self.assertEqual(get_decrypted_password(row.doctype, row.name, 'secret_value'), 'synthetic-purpose')
        extraction_provider = self.provider('Parameter Extraction')
        extraction = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix+'_extract',
            provider=extraction_provider.name, profile_type='Parameter Extraction', enabled=1))
        with patch.object(config, 'run') as provider:
            extraction.insert()
            extraction.reload()
            self.assertEqual(extraction.profile_type, 'Parameter Extraction')
            self.assertEqual(config.test_connection(extraction.name)['status'], 'extraction_inactive')
            extraction.onload()
            listed = next(r for r in config.dashboard()['configurations'] if r.name == extraction.name)
            self.assertFalse(listed.operational)
            provider.assert_not_called()
        with self.assertRaises(adapters.AIServiceError):
            config.resolve_configuration(extraction.name)
        settings = frappe.get_single('Bot Settings')
        settings.active_ai_configuration = extraction.name
        with self.assertRaises(frappe.ValidationError):
            settings.save()
        settings.reload(); settings.active_ai_configuration = doc.name; settings.save()
        doc.profile_type = 'Parameter Extraction'
        with self.assertRaises(frappe.ValidationError):
            doc.save()

    def test_administrative_catalog_definitions_and_closed_internal_registry(self):
        with self.assertRaises(frappe.PermissionError):
            frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix+'_forged', provider_name='Forged',
                provider_type='Internal', adapter='synthetic', profile_type='Decision', enabled=1, schema_version='1')).insert()
        provider=self.provider()
        self.assertIn('endpoint',{r.parameter_key for r in provider.parameters})
        self.assertEqual(provider.provider_type, 'External')
        schema=__import__('messaging.chatbot.doctype.ai_provider.ai_provider',fromlist=['parameter_schema']).parameter_schema(provider.name)
        self.assertNotIn('secret_value',json.dumps(schema))
        provider.application='messaging'
        with self.assertRaises(frappe.PermissionError):provider.save()
        provider.reload();provider.parameters[0].parameter_key='bad.key'
        with self.assertRaises(frappe.ValidationError):provider.save()
        provider.reload();next(row for row in provider.parameters if row.parameter_key == 'timeout').default_value='9999'
        with self.assertRaises(frappe.ValidationError):provider.save()
        provider.reload();provider.parameters.append(provider.parameters[0])
        with self.assertRaises(frappe.ValidationError):provider.save()

    def test_encrypted_values_secret_ownership_and_forged_diagnostics(self):
        doc=create_configuration(self.prefix+'_one',api_key='synthetic-one')
        other=create_configuration(self.prefix+'_two',api_key='synthetic-two')
        row=next(r for r in doc.parameters if r.parameter_key=='api_key')
        self.assertEqual(row.secret_value,'********')
        self.assertNotIn('synthetic-one',doc.as_json())
        self.assertEqual(get_decrypted_password(row.doctype,row.name,'secret_value'),'synthetic-one')
        doc.validated_hash='forged';doc.validation_status='authentication_error';doc.save()
        self.assertEqual(doc.validation_status,'operational')
        stolen=next(r for r in other.parameters if r.parameter_key=='api_key')
        row.name=stolen.name
        with self.assertRaises(frappe.PermissionError):doc.save()
        other.reload();next(r for r in other.parameters if r.parameter_key=='timeout').value='61'
        with self.assertRaises(frappe.ValidationError):other.save()
        other.reload();next(r for r in other.parameters if r.parameter_key=='api_key').value='"plaintext"'
        with self.assertRaises(frappe.ValidationError):other.save()

    def test_draft_activation_validation_change_and_live_failure(self):
        doc=frappe.get_doc(dict(doctype='AI Configuration',configuration_name=self.prefix+'_draft',provider='synthetic',enabled=0)).insert()
        self.assertEqual(doc.validation_status,'configuration_incomplete')
        settings=frappe.get_single('Bot Settings');settings.active_ai_configuration=doc.name
        with self.assertRaises(frappe.ValidationError):settings.save()
        doc=create_configuration(self.prefix+'_valid',api_key='synthetic-key')
        settings=frappe.get_single('Bot Settings');settings.active_ai_configuration=doc.name;settings.save()
        self.assertEqual(frappe.get_single('Bot Settings').active_ai_configuration,doc.name)
        self.assertTrue(config.availability()['operational'])
        with patch.object(frappe.db, 'count', return_value=0):self.assertEqual(config.availability()['status'],'no_provider')
        with patch.object(config,'run',side_effect=adapters.AIServiceError('authentication_error')) as call:
            row=next(r for r in doc.parameters if r.parameter_key=='api_key');row.secret_value='changed-key';doc.save()
            self.assertEqual(call.call_count,0)
            config.test_connection(doc.name)
            self.assertEqual(call.call_count,1)
            doc.reload();doc.save();self.assertEqual(call.call_count,1)
        self.assertEqual(doc.validation_status,'authentication_error')
        self.assertEqual(frappe.get_single('Bot Settings').active_ai_configuration,doc.name)
        with self.assertRaises(adapters.AIServiceError):config.resolve_configuration(doc.name)
        self.assertNotIn('changed-key',doc.diagnostic)
        self.assertFalse(config.availability()['operational'])

    def test_native_decision_policy_is_separate_and_invalidates_material_changes(self):
        doc = create_configuration(self.prefix + '_policy', api_key='synthetic-key')
        self.assertEqual((doc.minimum_confidence, doc.intent_probability, doc.negation_probability), (.6, .75, .65))
        self.assertNotIn('minimum_confidence', adapters.configuration_values(doc, config.provider_for(doc)))
        generation = doc.evaluation_generation
        with patch.object(adapters, 'run') as network, patch.object(frappe, 'enqueue') as enqueue:
            doc.onload()
            doc.intent_probability = .8
            doc.save()
            network.assert_not_called()
            enqueue.assert_not_called()
        self.assertEqual(doc.status, 'Unavailable')
        self.assertNotEqual(doc.evaluation_generation, generation)
        doc.negation_probability = .4
        with self.assertRaises(frappe.ValidationError):
            doc.save()
        doc.reload()
        doc.append('parameters', dict(parameter_key='minimum_confidence', value='0.9'))
        with self.assertRaises(frappe.ValidationError):
            doc.save()

    def test_probe_error_categories_and_no_reads_call_remote_services(self):
        doc=create_configuration(self.prefix+'_valid',api_key='synthetic-key')
        for code in ('authentication_error','timeout','rate_limited','unavailable','contract_invalid'):
            with self.subTest(code=code),patch.object(config,'run',side_effect=adapters.AIServiceError(code)):
                result=config.test_connection(doc.name)
                self.assertEqual(result['status'],code)
        with patch.object(config,'run',side_effect=AssertionError('Unexpected network')):
            frappe.get_doc('AI Configuration',doc.name)
            config.dashboard()
        with patch.object(config,'run',side_effect=successful_probe):
            self.assertTrue(config.test_connection(doc.name)['operational'])
        provider=frappe.get_doc('AI Provider','synthetic');provider.description='Administrative description'
        with self.assertRaises(frappe.PermissionError):provider.save()
        self.assertEqual(doc.reload().status, 'Unavailable')
        with self.assertRaises(adapters.AIServiceError):config.resolve_configuration(doc.name)
        external=self.provider();external_doc=create_configuration(self.prefix+'_fingerprint',provider=external.name,endpoint='https://example.com/interpret')
        external.description='Administrative description';external.save()
        self.assertEqual(config.resolve_configuration(external_doc.name)[0].name,external_doc.name)
        external.reload();external.parameters[0].description='Changed runtime definition';external.save()
        self.assertEqual(config.resolve_configuration(external_doc.name)[0].name, external_doc.name)

    def test_connection_probe_offers_a_signal_to_require_provider_interpretation(self):
        doc = create_configuration(self.prefix+'_signal', api_key='synthetic-key')
        def inspect_probe(provider, values, request, **kwargs):
            self.assertTrue(request.greeting_available)
            self.assertFalse(request.human_attention_available)
            self.assertEqual(request.available_flows, ())
            self.assertEqual(request.message.text, 'Connection check.')
            return successful_probe(provider, values, request)
        with patch.object(config, 'run', side_effect=inspect_probe) as probe:
            self.assertTrue(config.test_connection(doc.name)['operational'])
        self.assertEqual(probe.call_count, 1)

    def test_password_replacement_preservation_and_persisted_probe(self):
        doc = create_configuration(self.prefix+'_password', api_key='synthetic-first')
        previous_hash = doc.validated_hash
        doc.reload()
        row = next(r for r in doc.parameters if r.parameter_key == 'api_key')
        row.secret_value = 'synthetic-replacement'
        def inspect_saved(provider, values, request, **kwargs):
            self.assertEqual(values['api_key'], 'synthetic-replacement')
            self.assertEqual(get_decrypted_password(row.doctype, row.name, 'secret_value'), values['api_key'])
            raise adapters.AIServiceError('authentication_error')
        with patch.object(config, 'run', side_effect=inspect_saved) as probe:
            doc.save()
            self.assertEqual(probe.call_count, 0)
            config.test_connection(doc.name)
            doc.reload()
            self.assertEqual(probe.call_count, 1)
            self.assertIsNone(doc.validated_hash)
            self.assertNotEqual(doc.attempted_hash, previous_hash)
            doc.reload()
            row = next(r for r in doc.parameters if r.parameter_key == 'api_key')
            self.assertEqual(row.secret_value, '********')
            # Native masks and an omitted Password preserve the existing encrypted credential.
            for secret in ('********', ''):
                row.secret_value = secret
                doc.save()
                self.assertEqual(get_decrypted_password(row.doctype, row.name, 'secret_value'), 'synthetic-replacement')
            self.assertEqual(probe.call_count, 1)
            result = config.test_connection(doc.name)
            self.assertEqual(probe.call_count, 2)
            self.assertEqual(result['status'], 'authentication_error')
            self.assertIn('API key', result['message'])
        self.assertNotIn('synthetic-replacement', doc.as_json())

    def test_provider_selection_is_enabled_and_immutable_after_insert(self):
        provider = self.provider()
        draft = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix+'_identity',
            provider=provider.name, enabled=0))
        draft.provider = 'synthetic'
        draft.insert()
        self.assertEqual(draft.provider, 'synthetic')
        draft.provider = provider.name
        with self.assertRaises(frappe.ValidationError):
            draft.save()
        self.assertEqual(frappe.db.get_value('AI Configuration', draft.name, 'provider'), 'synthetic')
        setup.set_provider_enabled(provider.name, False)
        with self.assertRaises(frappe.ValidationError):
            frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix+'_disabled',
                provider=provider.name, enabled=0)).insert()

    def test_unexpected_probe_error_is_safe_visible_and_persisted(self):
        doc = create_configuration(self.prefix+'_unexpected', api_key='synthetic-key')
        with patch.object(config, 'run', side_effect=RuntimeError('synthetic-secret-and-remote-body')):
            result = config.test_connection(doc.name)
        self.assertFalse(result['operational'])
        self.assertEqual(result['status'], 'internal_error')
        self.assertIn('server error log', result['message'])
        doc.reload()
        self.assertEqual(doc.validation_status, result['status'])
        self.assertEqual(str(doc.last_checked), str(result['last_checked']))
        log = frappe.get_last_doc('Error Log', filters={'reference_doctype':doc.doctype, 'reference_name':doc.name})
        details = json.loads(log.error)
        self.assertEqual(details['exception_type'], 'RuntimeError')
        self.assertTrue(details['frames'])
        self.assertNotIn('synthetic-secret-and-remote-body', log.as_json())
        self.assertNotIn('synthetic-key', log.as_json())

    def test_catalog_setup_is_idempotent_without_provisioning_profiles(self):
        before = frappe.db.count('AI Configuration')
        with patch.object(config, 'run', side_effect=AssertionError('No network during setup')):
            sync_ai_providers()
            self.assertEqual(sync_ai_providers()['unchanged'], 1)
        self.assertEqual(frappe.db.count('AI Configuration'), before)

    def test_real_database_concurrent_selection_is_exclusive(self):
        one=create_configuration(self.prefix+'_one',api_key='synthetic-one')
        two=create_configuration(self.prefix+'_two',api_key='synthetic-two')
        frappe.db.commit()
        ctx=multiprocessing.get_context('spawn');barrier=ctx.Barrier(2);results=ctx.Queue()
        processes=[ctx.Process(target=activation_worker,args=(frappe.local.site,str(Path(frappe.get_site_path()).parent),name,barrier,results)) for name in (one.name,two.name)]
        for process in processes:process.start()
        for process in processes:process.join(25);self.assertFalse(process.is_alive())
        states=[results.get(timeout=5) for _ in processes]
        self.assertEqual(states.count('selected'),1,states)
        self.assertIn('TimestampMismatchError',states)
        frappe.db.rollback()
        self.assertIn(frappe.get_single('Bot Settings').active_ai_configuration,(one.name,two.name))

    def test_administrative_permissions_and_provider_disabled(self):
        doc=create_configuration(self.prefix+'_valid',api_key='synthetic-key')
        provider=frappe.get_doc('AI Provider','synthetic');provider.status='Unavailable'
        with self.assertRaises(frappe.PermissionError):provider.save()
        self.assertEqual(frappe.db.get_value('AI Provider','synthetic','status'),'Enabled')
        external=self.provider()
        disabled=create_configuration(self.prefix+'_disabled_provider',provider=external.name,endpoint='https://example.com/interpret')
        setup.set_provider_enabled(external.name, False)
        with self.assertRaises(adapters.AIServiceError) as error:config.resolve_configuration(disabled.name)
        self.assertEqual(error.exception.code,'provider_disabled')
        self.assertEqual(disabled.reload().status, 'Unavailable')
        modified = str(doc.modified)
        for user in ('Guest',):
            frappe.set_user(user)
            with self.assertRaises(frappe.PermissionError):config.test_connection(doc.name)
            with self.assertRaises(frappe.PermissionError):config.dashboard()
            with self.assertRaises(frappe.PermissionError):config.set_active(doc.name, False, modified)
        frappe.set_user('Administrator')
        doc.status = 'Disabled'
        with self.assertRaises(frappe.PermissionError):doc.save()
        with self.assertRaises(frappe.PermissionError):doc.db_set('status','Disabled')

    def test_external_https_contract_and_http_failures(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import interpret

        provider = self.provider()
        secret = 'synthetic-rest-secret'
        doc = self._profile(self.prefix + '_remote', provider, 'https://decision.example.test:9443/interpret',
            auth_type='Bearer', auth_secret=secret)
        seen = []
        mode = 'valid'

        def reply(body, headers):
            seen.append((body, headers))
            if mode == 'timeout':
                raise rest_transport.urllib3.exceptions.ReadTimeoutError(None, '/interpret', 'private')
            if mode == 'tls':
                raise rest_transport.urllib3.exceptions.SSLError('certificate verification failed')
            if mode.isdecimal():
                return int(mode), {'Location': 'https://blocked.example.test/'}, secret.encode()
            response = rest_decision_body(body)
            if response['results'] and body['available_flows']:
                response['results'][0].update(classification='request', confidence=.98, probabilities={'request': .98})
            if mode == 'version':
                response['wire_version'] = 'decision-v1'
            if mode == 'malformed':
                return 200, JSON_HEADERS, b'not-json ' + secret.encode()
            if mode == 'oversized':
                return 200, JSON_HEADERS, b' ' * (wire.MAX_RESPONSE_BYTES + 1)
            if mode == 'content_type':
                return 200, {'Content-Type': 'text/html'}, secret.encode()
            if mode == 'encoded':
                return 200, dict(JSON_HEADERS, **{'Content-Encoding': 'gzip'}), b'compressed'
            return 200, JSON_HEADERS, wire.serialize(response)

        with simulated_rest_http(reply) as (connect, pool):
            self.assertTrue(config.test_connection(doc.name)['operational'])
            self.assertEqual(doc.reload().status, 'Untested')
            adapters.set_profile_status(doc, 'Enabled')  # Approval double is scoped to this transport test.
            settings = frappe.get_single('Bot Settings')
            settings.active_ai_configuration = doc.name
            settings.save()
            parameter = frappe._dict(name='private_parameter', label='Número de Contrato', parameter_type='Expression',
                expression='#####', enabled=1)
            flow = dict(name='Consultar saldo', description='Saldo público', parameters={parameter.name: parameter},
                multiple_parameters=[parameter.name])
            decision = interpret('Debía 54321 de mi contrato 12345.', {}, {'private_flow': flow}, {}, settings)
            self.assertEqual(decision.flows, ['private_flow'])
            self.assertEqual(decision.parameters, {parameter.name: ['54321', '12345']})
            body, headers = seen[-1]
            self.assertEqual(body['wire_version'], DECISION_WIRE)
            self.assertEqual(body['message']['text'], 'Debía [numero_de_contrato] de mi contrato [numero_de_contrato].')
            self.assertEqual(headers['Authorization'], 'Bearer ' + secret)
            self.assertEqual(headers['Host'], 'decision.example.test:9443')
            for private in (secret, '54321', '12345', 'private_parameter', 'private_flow', 'expression'):
                self.assertNotIn(private, json.dumps(body))
            connect.assert_called_with('93.184.215.14', port=9443, server_hostname='decision.example.test',
                assert_hostname='decision.example.test', cert_reqs='CERT_REQUIRED',
                ca_certs=rest_transport.requests.certs.where(), maxsize=1)
            args, kwargs = pool.urlopen.call_args
            self.assertEqual(args, ('POST', '/interpret'))
            for flag in ('redirect', 'retries', 'preload_content'):
                self.assertIs(kwargs[flag], False)
            pool.close.assert_called()
            for mode, expected in (
                ('401', 'authentication_error'), ('403', 'authentication_error'), ('429', 'rate_limited'),
                ('503', 'unavailable'), ('302', 'contract_invalid'), ('version', 'contract_invalid'),
                ('malformed', 'contract_invalid'), ('oversized', 'contract_invalid'), ('content_type', 'contract_invalid'),
                ('encoded', 'contract_invalid'), ('timeout', 'timeout'), ('tls', 'unavailable'),
            ):
                with self.subTest(mode=mode):
                    before = len(seen)
                    self.assertEqual(config.test_connection(doc.name)['status'], expected)
                    self.assertEqual(len(seen), before + 1)
        for row in frappe.get_all('AI Request Log', filters={'configuration': doc.name}, pluck='name'):
            self.assertNotIn(secret, frappe.get_doc('AI Request Log', row).as_json())

    def test_external_pretransmission_failure_has_prepared_evidence(self):
        provider = self.provider()
        doc = self._profile(self.prefix + '_prepared', provider, 'https://decision.example.test/interpret')
        values = config.configuration_values(doc, provider)
        request, _refs = build_request('Consulta [numero_de_contrato].\n  Texto sintético.', {}, {}, 'es')
        from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope
        with evaluation_scope(None, configuration=doc.name), simulated_rest_http(
            None, dns_error=OSError('private DNS error')
        ) as (connect, _pool):
            with self.assertRaisesRegex(adapters.AIServiceError, 'unavailable'):
                adapters.run(provider, values, request)
            connect.assert_not_called()
        log = frappe.get_doc('AI Request Log', {'configuration': doc.name})
        self.assertEqual((log.status, log.transmission_state, log.request_text),
            ('Failed', 'Prepared', request.message.text))
        self.assertNotIn('private DNS error', log.as_json())

    def test_external_provider_parameters_are_nested_typed_and_isolated(self):
        provider = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix + '_dual',
            provider_name='Synthetic dual capability', decision=1, parameter_extraction=1, parameters=[
                setup.parameter('model', 'Model', required=1, send_to_rest=1),
                setup.parameter('temperature', 'Temperature', 'Float', minimum=0, maximum=1, send_to_rest=1),
                setup.parameter('limit', 'Limit', 'Int', send_to_rest=1),
                setup.parameter('enabled_flag', 'Enabled Flag', 'Check', send_to_rest=1),
                setup.parameter('local_note', 'Local Note'),
            ])).insert()
        self.assertEqual(provider.status, 'Disabled')
        setup.set_provider_enabled(provider.name, True)
        provider.reload()
        endpoint, secret = 'https://decision.example.test/score', 'synthetic-profile-secret'
        first = self._profile(self.prefix + '_a', provider, endpoint, model='classifier-a', temperature=.2,
            local_note='private-note', auth_type='Basic', username='example-user', auth_secret=secret)
        second = self._profile(self.prefix + '_b', provider, 'https://extraction.example.test/observe',
            profile_type='Parameter Extraction', model='extractor', limit=0, enabled_flag=False)
        self.assertEqual(second.validation_status, 'extraction_inactive')
        self.assertEqual((first.minimum_confidence, first.intent_probability, first.negation_probability), (.6, .75, .65))
        self.assertFalse(second.minimum_confidence)
        self.assertNotEqual(first.evaluation_generation, second.evaluation_generation)
        definition = next(row for row in setup.parameter_schema(provider.name)['parameters']
            if row['parameter_key'] == 'endpoint')
        self.assertEqual(definition['label'], 'REST Service URL')
        secret_row = next(row for row in first.parameters if row.parameter_key == 'auth_secret')
        self.assertEqual(get_decrypted_password(secret_row.doctype, secret_row.name, 'secret_value'), secret)
        self.assertNotIn(secret, first.as_json())

        seen = []

        def reply(body, headers):
            seen.append((body, headers))
            return 200, JSON_HEADERS, wire.serialize(rest_decision_body(body))

        decision, _ = build_request('Hola', {}, {}, 'es', greeting_available=True)
        extraction = wire.ParameterExtractionRequest('observe', wire.Message('0'), 'es',
            (wire.ExtractionParameter('number', 'Number', wire.ParameterType.INT),))
        with simulated_rest_http(reply) as (_connect, pool):
            values = config.configuration_values(first, provider, required=True)
            adapters.run(provider, values, decision, policy=config.profile_policy(first, provider))
            self.assertEqual(seen[0][1]['Authorization'], 'Basic ' + base64.b64encode(
                b'example-user:synthetic-profile-secret').decode())
            body = seen[0][0]
            self.assertEqual(body, dict(wire.to_json(decision), wire_version=DECISION_WIRE,
                provider_parameters={'model': 'classifier-a', 'temperature': .2}))
            extracted = config.configuration_values(second, provider, required=True)
            self.assertNotEqual(values['endpoint'], extracted['endpoint'])
            self.assertEqual(adapters.outbound_request(provider, extracted, extraction), dict(wire.to_json(extraction),
                wire_version=EXTRACTION_WIRE, provider_parameters={'enabled_flag': False, 'limit': 0, 'model': 'extractor'}))
            plain = self.provider(suffix='plain')
            plain_profile = self._profile(self.prefix + '_plain', plain, endpoint)
            adapters.run(plain, config.configuration_values(plain_profile, plain), decision)
            self.assertNotIn('Authorization', seen[-1][1])
            self.assertNotIn('provider_parameters', seen[-1][0])
            draft = self._profile(self.prefix + '_draft_rest', provider, None, model='draft')
            self.assertEqual(draft.validation_status, 'configuration_incomplete')
            self.assertEqual(config.test_connection(draft.name)['status'], 'configuration_incomplete')
            for invalid in (dict(values, unregistered='no'), dict(values, model=secret)):
                with self.assertRaisesRegex(adapters.AIServiceError, 'configuration_incomplete'):
                    adapters.run(provider, invalid, decision)
            self.assertEqual(pool.urlopen.call_count, 2)
        for key in ('minimum_confidence', 'intent_probability', 'negation_probability', 'wire_version', 'results',
            'human_attention_available', 'message', 'parameters'):
            with self.subTest(key=key), self.assertRaisesRegex(frappe.ValidationError, 'reserved contract name'):
                frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix + '_reserved', provider_name='REST',
                    decision=1, parameters=[setup.parameter(key, 'Reserved', send_to_rest=1)])).insert()
        with self.assertRaisesRegex(frappe.ValidationError, 'Additional secret parameters'):
            frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix + '_password', provider_name='REST',
                decision=1, parameters=[setup.parameter('extra_secret', 'Extra', 'Password', send_to_rest=1)])).insert()
        provider.reload()
        endpoint_definition = next(row for row in provider.parameters if row.parameter_key == 'endpoint')
        endpoint_definition.send_to_rest = 1
        with self.assertRaisesRegex(frappe.ValidationError, 'Connection parameters'):
            provider.save()
        provider.reload()
        endpoint_definition = next(row for row in provider.parameters if row.parameter_key == 'endpoint')
        endpoint_definition.default_value = endpoint
        with self.assertRaisesRegex(frappe.ValidationError, 'on each AI profile'):
            provider.save()

    def _profile(self, name, provider, endpoint, *, profile_type='Decision', auth_secret=None, **values):
        doc = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=name, provider=provider.name,
            profile_type=profile_type))
        for key, value in dict(endpoint=endpoint, **values).items():
            if value is not None:
                doc.append('parameters', dict(parameter_key=key, value=json.dumps(value)))
        if auth_secret:
            doc.append('parameters', dict(parameter_key='auth_secret', secret_value=auth_secret))
        return doc.insert()


class TestRESTTransport(unittest.TestCase):
    def test_request_byte_limit_is_enforced_before_network(self):
        with patch.object(adapters, 'bounded_exchange') as transport:
            with self.assertRaisesRegex(wire.InvalidInterpretation, 'Oversized interpretation request'):
                adapters.rest_interpret({'message': {'text': 'é' * 60000}},
                    {'endpoint': 'https://example.test/', 'auth_type': 'None', 'timeout': 5})
            transport.assert_not_called()

    def test_endpoint_url_and_address_policy(self):
        for url in ('http://example.test', 'https://example.test:0/path', 'https://example.test:',
            'https://example.test:65536', 'https://u:p@example.test', 'https://@example.test',
            'https://example.test/?', 'https://example.test/#', 'https://example.test\t/path'):
            with self.subTest(url=url), self.assertRaisesRegex(wire.AIServiceError, 'configuration_incomplete'):
                rest_transport.endpoint_target(url)
        self.assertEqual(rest_transport.endpoint_target('https://[2606:4700::1111]:8443/ai'),
            ('2606:4700::1111', 8443, '/ai'))
        for ip, allowed, expected in (
            ('93.184.215.14', [], True), ('10.1.2.3', [], False), ('10.1.2.3', ['example.test'], True),
            ('10.1.2.3', ['other.example.test'], False), ('224.0.0.1', ['example.test'], False),
            ('0.0.0.0', ['example.test'], False), ('255.255.255.255', ['example.test'], False),
            ('169.254.169.254', ['example.test'], False), ('ff02::1', ['example.test'], False),
            ('::ffff:224.0.0.1', ['example.test'], False), ('::', ['example.test'], False),
        ):
            with self.subTest(ip=ip), patch.object(rest_transport.socket, 'getaddrinfo',
                return_value=[(None, None, None, '', (ip, 443, 0, 0))]):
                if expected:
                    self.assertEqual(rest_transport._resolve('example.test', 443, allowed), ip)
                else:
                    with self.assertRaisesRegex(wire.AIServiceError, 'destination_blocked'):
                        rest_transport._resolve('example.test', 443, allowed)
        with patch.object(rest_transport.socket, 'getaddrinfo', return_value=[
            (None, None, None, '', ('93.184.215.14', 443)), (None, None, None, '', ('10.1.2.3', 443)),
        ]), self.assertRaisesRegex(wire.AIServiceError, 'destination_blocked'):
            rest_transport._resolve('example.test', 443, [])

    def test_subprocess_deadline_covers_dns_and_body_reads(self):
        launch = subprocess.run
        controlled_worker = """
import sys
import time
from types import SimpleNamespace
from messaging.chatbot.doctype.ai_configuration import rest_transport as transport
def block(*args, **kwargs):
    time.sleep(8)
if sys.argv[1] == 'dns':
    transport.socket.getaddrinfo = block
else:
    transport.socket.getaddrinfo = lambda *args, **kwargs: [(2, 1, 6, '', ('93.184.215.14', 443))]
    response = SimpleNamespace(status=200, headers={'Content-Type': 'application/json'},
        read1=block, close=lambda: None)
    transport.urllib3.HTTPSConnectionPool = lambda *args, **kwargs: SimpleNamespace(
        urlopen=lambda *args, **kwargs: response, close=lambda: None)
transport._worker()
"""
        for phase in ('dns', 'body'):
            def controlled(command, **kwargs):
                self.assertEqual(command[1:], ['-m', rest_transport.__name__])
                self.assertNotIn('synthetic-token', ' '.join(command))
                self.assertIn(b'synthetic-token', kwargs['input'])
                return launch([command[0], '-c', controlled_worker, phase], **kwargs)

            with self.subTest(phase=phase), patch.object(subprocess, 'run', side_effect=controlled):
                started = time.monotonic()
                with self.assertRaisesRegex(wire.AIServiceError, 'timeout'):
                    rest_transport.bounded_exchange(('example.test', 443, '/'), b'{}',
                        {'Authorization': 'Bearer synthetic-token'}, 1, [])
                self.assertLess(time.monotonic() - started, 3)


class TestRecognitionContract(unittest.TestCase):

    def test_pending_capture_context_contains_only_public_definitions(self):
        parameter = self.parameter(name='private_parameter', label='Problem location')
        parameter.parameter_type = 'String'
        request, _refs = build_request('Synthetic answer', {'pending_parameters': ['private_parameter']},
            {'private_flow': dict(name='Public report', description='Public meaning', parameters={parameter.name: parameter})}, 'es')
        self.assertEqual(wire.to_json(request)['context'], {'previously_recognized_flows': []})
        for private in ('private_parameter', 'private_flow', 'expression', 'validated_values'):
            self.assertNotIn(private, json.dumps(wire.to_json(request)))

    def parameter(self,name='technical_contract',label='Número de Contrato',expression='#####'):
        return frappe._dict(name=name,label=label,parameter_type='Expression',expression=expression,enabled=1)

    def test_required_masking_examples_and_local_positions(self):
        parameter=self.parameter();parameters={parameter.name:parameter}
        with self.assertRaises(InvalidInterpretation):recognize('x'*16001,parameters)
        cases=[('Mi contrato es 12345.','Mi contrato es [numero_de_contrato].',['12345']),
            ('Me dijeron que debo 1000 lempiras, contrato 12345.','Me dijeron que debo 1000 lempiras, contrato [numero_de_contrato].',['12345']),
            ('Debía 54321 de mi contrato 12345.','Debía [numero_de_contrato] de mi contrato [numero_de_contrato].',['54321','12345']),
            ('Hola, quiero información.','Hola, quiero información.',[]),
            ('12345, 12345 y 00123','[numero_de_contrato], [numero_de_contrato] y [numero_de_contrato]',['12345','12345','00123'])]
        for original,expected,values in cases:
            with self.subTest(text=original):
                transformed,occurrences=recognize(original,parameters)
                self.assertEqual(transformed,expected)
                self.assertEqual([item.value for item in occurrences],values)
                for item in occurrences:self.assertEqual(original[item.start:item.end],item.value)
                request,refs=build_request(transformed,{}, {'private_balance_route':dict(name='Saldo',description='Consultar saldo')},'es')
                serialized=wire.serialize(request).decode()
                for value in values:self.assertNotIn(value,serialized)
                for private in ('technical_contract','private_balance_route','expression','parameter_type','parameters'):
                    self.assertNotIn(private,serialized)
                self.assertTrue(all(ref.startswith('flow_') for ref in refs))

    def test_public_labels_collisions_and_overlap_precedence(self):
        first=self.parameter('a','NÚMERO de Contráto');second=self.parameter('b','Numero de Contrato','SC-#####')
        transformed,occurrences=recognize('SC-12345 67890',{'b':second,'a':first})
        self.assertEqual(transformed,'[numero_de_contrato_2] [numero_de_contrato_1]')
        third=self.parameter('c','Numero de Contrato 1','YY####')
        transformed,_=recognize('YY1234 SC-12345 67890',{'a':first,'b':second,'c':third})
        self.assertEqual(transformed,'[numero_de_contrato_1] [numero_de_contrato_3] [numero_de_contrato_2]')
        overlap=self.parameter('z','Other','#####')
        transformed,occurrences=recognize('12345',{'z':overlap,'a':first})
        self.assertEqual(occurrences[0].parameter,'a')
        self.assertEqual(transformed,'[numero_de_contrato]')
        values, ambiguous = captured_values(occurrences, {'z':overlap}, set())
        self.assertEqual(values, {'z':'12345'});self.assertFalse(ambiguous)

    def test_local_list_order_deduplication_and_singular_ambiguity(self):
        p=self.parameter(expression='#####\nSC-#####')
        transformed,occurrences=recognize('00123 SC-12345 00123',{p.name:p})
        values,ambiguous=captured_values(occurrences,{p.name:p},{p.name})
        self.assertEqual(values,{p.name:['00123','SC-12345']});self.assertFalse(ambiguous)
        values,ambiguous=captured_values(occurrences,{p.name:p},set())
        self.assertTrue(ambiguous);self.assertEqual(values,{})

    def test_strict_wire_decisions_negation_clarification_and_unauthorized_actions(self):
        request,refs=build_request('Consulta [numero_de_contrato]',{'pending_parameters':['private']}, {'internal':dict(name='Public',description='Description')},'es')
        ref=next(iter(refs))
        for decision in (Decision(flows=[ref],outcome='recognized'),Decision(excluded_flows=[ref],outcome='recognized'),
            Decision(ambiguous=True,outcome='ambiguous'),Decision(flows=[],outcome='unrecognized')):
            normalized=normalize_response(synthetic_wire_response(request,decision,.9),request,refs,minimum_confidence=.6)
            self.assertEqual(normalized.outcome,decision.outcome)
        good=synthetic_wire_response(request,Decision(flows=[ref],outcome='recognized'),.9)
        for changes in ({'flows':['internal']},{'flows':[ref,ref]},{'parameters':{'secret':'12345'}},{'process_method':'os.system'},
            {'confidence':float('nan')},{'confidence':True},{'human_requested':1},{'excluded_flows':[ref]},{'outcome':'explicit'},{'ambiguous':True}):
            proposal=wire.to_json(good);proposal['decision'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(InvalidInterpretation):normalize_response(wire.deserialize_response(proposal,request),request,refs,minimum_confidence=.6)
        for field,value in [('schema_version','2'),('request_id','wrong')]:
            proposal=wire.to_json(good);proposal[field]=value
            with self.assertRaises(InvalidInterpretation):normalize_response(wire.deserialize_response(proposal,request),request,refs,minimum_confidence=.6)
        self.assertTrue(normalize_response(good,request,refs,minimum_confidence=.99).ambiguous)
        for body in ('{"a":1,"a":2}','{"a":NaN}','[invalid]'):
            with self.assertRaises(InvalidInterpretation):wire.strict_json(body)
