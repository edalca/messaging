"""Focused provider, wire, privacy, network and administrative integrity contracts."""
from messaging.chatbot.doctype.bot_settings.interpretation import build_request, normalize_response
import copy
import json
import multiprocessing
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password, set_encrypted_password

from messaging.registrations import ai_providers as wire
from messaging.chatbot.doctype.ai_configuration import execution as adapters
from messaging.chatbot.doctype.ai_configuration import ai_configuration as config
from messaging.chatbot.doctype.ai_provider import ai_provider as setup
from messaging.chatbot.doctype.ai_provider.test_ai_provider import sync_ai_providers, synthetic_wire_response
from messaging.chatbot.doctype.bot_settings.interpretation import Decision, InvalidInterpretation
from messaging.chatbot.doctype.bot_parameter.bot_parameter import recognize, captured_values


def successful_probe(provider, values, request):
    return synthetic_wire_response(request, Decision(outcome='empty_catalog'), .99)


def create_configuration(name, provider='synthetic', **values):
    doc = frappe.get_doc(dict(doctype='AI Configuration',configuration_name=name,provider=provider,enabled=1))
    for key, value in values.items():
        doc.append('parameters', dict(parameter_key=key, **({'secret_value':value} if key in ('api_key','auth_secret') else {'value':json.dumps(value)})))
    with patch.object(config, 'run', side_effect=successful_probe):
        doc.insert()
        config.check_connection(doc)
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
        return frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix+'_'+suffix, provider_name='Synthetic classifier',
            provider_type='External', decision=int(profile_type=='Decision'), parameter_extraction=int(profile_type=='Parameter Extraction'), enabled=1, schema_version='1')).insert()

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
        provider.reload();provider.parameters[0].default_value='9999'
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
        self.assertEqual(config.resolve_configuration(doc.name)[0].name,doc.name)
        external=self.provider();external_doc=create_configuration(self.prefix+'_fingerprint',provider=external.name,endpoint='https://example.com/interpret')
        external.description='Administrative description';external.save()
        self.assertEqual(config.resolve_configuration(external_doc.name)[0].name,external_doc.name)
        external.reload();external.parameters[0].description='Changed runtime definition';external.save()
        self.assertEqual(config.resolve_configuration(external_doc.name)[0].name, external_doc.name)

    def test_connection_probe_offers_a_signal_to_require_provider_interpretation(self):
        doc = create_configuration(self.prefix+'_signal', api_key='synthetic-key')
        def inspect_probe(provider, values, request):
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
        def inspect_saved(provider, values, request):
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
                doc.enabled = not doc.enabled
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
        provider.enabled = 0
        provider.save()
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
        provider=frappe.get_doc('AI Provider','synthetic');provider.enabled=0
        with self.assertRaises(frappe.PermissionError):provider.save()
        self.assertEqual(frappe.db.get_value('AI Provider','synthetic','enabled'),1)
        external=self.provider()
        disabled=create_configuration(self.prefix+'_disabled_provider',provider=external.name,endpoint='https://example.com/interpret')
        external.enabled=0;external.save()
        with self.assertRaises(adapters.AIServiceError) as error:config.resolve_configuration(disabled.name)
        self.assertEqual(error.exception.code,'provider_disabled')
        for user in ('Guest',):
            frappe.set_user(user)
            with self.assertRaises(frappe.PermissionError):config.test_connection(doc.name)
            with self.assertRaises(frappe.PermissionError):config.dashboard()
        frappe.set_user('Administrator')

    def test_external_https_server_contract_and_http_failures(self):
        from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings, restore_bot_settings
        self.addCleanup(restore_bot_settings, self.original.as_dict())
        frappe.db.set_single_value('Bot Settings', 'interpretation_enabled', 0, update_modified=False)
        configure_test_settings()
        observed=[]
        class Handler(BaseHTTPRequestHandler):
            mode='valid'
            def log_message(self,*args):pass
            def do_POST(self):
                request=wire.strict_json(self.rfile.read(int(self.headers['Content-Length'])))
                observed.append((request,self.headers.get('Authorization')))
                mode=type(self).mode
                if mode=='timeout':time.sleep(1.2)
                status=int(mode) if mode.isdigit() else 200
                self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers()
                if 'parameters' in request:
                    result=wire.ParameterExtractionResponse(request['request_id'],tuple(
                        wire.ExtractedParameter(row['ref'],wire.ExtractionStatus.EXTRACTED,
                            ('123',) if mode=='extraction_invalid' else (123,)) for row in request['parameters']))
                else:
                    decision=Decision(outcome='recognized',flows=[request['available_flows'][0]['ref']]) if request['available_flows'] else Decision(outcome='empty_catalog')
                    result=synthetic_wire_response(request,decision,.98)
                result=wire.to_json(result)
                if mode=='version':result['schema_version']='unsupported'
                body=b'not-json' if mode=='malformed' else wire.serialize(result)
                if mode=='oversized':body=b' '*(wire.MAX_RESPONSE_BYTES+1)
                try:self.wfile.write(body)
                except OSError:pass
        with tempfile.TemporaryDirectory() as folder:
            cert,key=Path(folder)/'cert.pem',Path(folder)/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1',
                '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key)
            server.socket=tls.wrap_socket(server.socket,server_side=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                provider=self.provider()
                with patch.dict(frappe.conf,{'messaging_ai_allowed_hosts':['localhost']}),patch.object(adapters.requests.certs,'where',return_value=str(cert)):
                    doc=frappe.get_doc(dict(doctype='AI Configuration',configuration_name=self.prefix+'_remote',provider=provider.name,enabled=1))
                    for keyname,value in dict(endpoint=f'https://localhost:{server.server_port}/interpret',auth_type='Bearer',timeout=1).items():
                        doc.append('parameters',dict(parameter_key=keyname,value=json.dumps(value)))
                    doc.append('parameters',dict(parameter_key='auth_secret',secret_value='synthetic-rest-secret'));doc.insert()
                    config.test_connection(doc.name);doc.reload()
                    self.assertEqual(doc.validation_status,'operational')
                    values=config.configuration_values(doc,provider)
                    request,refs=build_request('Saldo [numero_de_contrato]',{}, {'technical_flow':dict(name='Consultar saldo',description='Saldo público')},'es')
                    result=adapters.run(provider,values,request)
                    self.assertEqual(normalize_response(result,request,refs,minimum_confidence=.6).flows,['technical_flow'])
                    self.assertEqual(observed[-1][0],wire.to_json(request))
                    log = frappe.get_last_doc('AI Request Log', filters={'provider': provider.name})
                    self.assertEqual(log.request_text, observed[-1][0]['message']['text'])
                    self.assertEqual(log.transmission_state, 'Responded')
                    self.assertEqual(observed[-1][1],'Bearer synthetic-rest-secret')
                    self.assertNotIn('synthetic-rest-secret',json.dumps(observed[-1][0]))
                    from messaging.chatbot.doctype.bot_settings.interpretation import interpret
                    settings=frappe.get_single('Bot Settings');settings.interpretation_enabled=1;settings.active_ai_configuration=doc.name;settings.save()
                    parameter=frappe._dict(name='private_parameter',label='Número de Contrato',parameter_type='Expression',expression='#####',enabled=1)
                    flow=dict(name='Consultar saldo',description='Saldo público',parameters={parameter.name:parameter},multiple_parameters=[parameter.name])
                    decision=interpret('Debía 54321 de mi contrato 12345.',{}, {'private_flow':flow},{},settings)
                    self.assertEqual(decision.flows,['private_flow'])
                    self.assertEqual(decision.parameters,{parameter.name:['54321','12345']})
                    received=observed[-1][0]
                    self.assertEqual(received['message']['text'],'Debía [numero_de_contrato] de mi contrato [numero_de_contrato].')
                    log = frappe.get_last_doc('AI Request Log', filters={'configuration': doc.name})
                    self.assertEqual(log.request_text, received['message']['text'])
                    self.assertNotIn('synthetic-rest-secret', log.as_json())
                    for private in ('54321','12345','private_parameter','private_flow','expression'):
                        self.assertNotIn(private,json.dumps(received))
                    for mode,expected in [('401','authentication_error'),('403','authentication_error'),('429','rate_limited'),('503','unavailable'),('302','contract_invalid'),('version','contract_invalid'),('malformed','contract_invalid'),('oversized','contract_invalid'),('timeout','timeout')]:
                        Handler.mode=mode
                        with self.subTest(mode=mode):self.assertEqual(config.test_connection(doc.name)['status'],expected)
                    Handler.mode='valid';self.assertTrue(config.test_connection(doc.name)['operational'])
                    extraction=self.provider('Parameter Extraction',suffix='extract_wire')
                    profile=frappe.get_doc(dict(doctype='AI Configuration',configuration_name=self.prefix+'_extract_wire',
                        provider=extraction.name,profile_type='Parameter Extraction',enabled=1))
                    profile.append('parameters',dict(parameter_key='endpoint',value=json.dumps(f'https://localhost:{server.server_port}/extract')))
                    profile.insert()
                    request=wire.ParameterExtractionRequest('extract-wire',wire.Message('123'),'es',
                        (wire.ExtractionParameter('number','Number',wire.ParameterType.INT),))
                    values=config.configuration_values(profile,extraction,required=True)
                    from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope
                    with evaluation_scope(None,configuration=profile.name):
                        result=adapters.run(extraction,values,request)
                    self.assertEqual(result.parameters[0].values,(123,))
                    log=frappe.get_last_doc('AI Request Log',filters={'configuration':profile.name})
                    self.assertEqual(log.operation,'Parameter Extraction')
                    self.assertEqual(profile.validation_status,'extraction_inactive')
                    Handler.mode='extraction_invalid'
                    with self.assertRaises(wire.InvalidInterpretation):adapters.run(extraction,values,request)
                    Handler.mode='valid'

                with self.assertRaises(adapters.AIServiceError):adapters.endpoint_target(f'https://localhost:{server.server_port}/interpret')
            finally:
                server.shutdown();server.server_close();thread.join(3)

        for endpoint in ('http://example.com','https://u:p@example.com','https://example.com/#secret','https://example.com/?secret=x'):
            with self.subTest(endpoint=endpoint),self.assertRaises(adapters.AIServiceError):adapters.endpoint_target(endpoint)

    def test_external_pretransmission_failure_has_prepared_evidence(self):
        provider = self.provider()
        doc = create_configuration(self.prefix+'_prepared', provider=provider.name,
            endpoint='https://example.com/interpret', auth_type='Bearer', auth_secret='synthetic-rest-secret')
        values = config.configuration_values(doc, provider)
        request, _refs = build_request('Consulta [numero_de_contrato].\n  Texto sintético.', {}, {}, 'es')
        from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope
        with evaluation_scope(None, configuration=doc.name), patch.object(adapters.socket, 'getaddrinfo', side_effect=OSError('private DNS error')), patch.object(adapters.urllib3.HTTPSConnectionPool, 'urlopen') as transport:
            with self.assertRaises(adapters.AIServiceError):
                adapters.run(provider, values, request)
            transport.assert_not_called()
        log = frappe.get_doc('AI Request Log', {'configuration': doc.name})
        self.assertEqual((log.status, log.transmission_state, log.request_text),
            ('Failed', 'Prepared', request.message.text))
        self.assertNotIn('synthetic-rest-secret', log.as_json())

    def test_external_provider_parameters_are_nested_typed_and_isolated(self):
        provider = frappe.get_doc('AI Provider', 'synthetic')
        probe, _refs = build_request('Hola', {}, {}, 'es')
        self.assertEqual(adapters.outbound_request(provider, {}, probe), wire.to_json(probe))
        self.assertNotIn('provider_parameters', wire.to_json(probe))
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = wire.strict_json(self.rfile.read(int(self.headers['Content-Length'])))
                observed.append(body)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                flows = [body['available_flows'][0]['ref']] if body['available_flows'] else []
                decision = Decision(outcome='recognized' if flows else 'empty_catalog', flows=flows)
                self.wfile.write(wire.serialize(synthetic_wire_response(body, decision, .99)))
        secret = 'synthetic-profile-secret'
        with tempfile.TemporaryDirectory() as folder:
            cert, key = Path(folder)/'cert.pem', Path(folder)/'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
                '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(cert, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                provider = self.provider(suffix='extra')
                for row in (setup.parameter('model', 'Model', required=1),
                    setup.parameter('temperature', 'Temperature', 'Float', minimum=0, maximum=1),
                    setup.parameter('retries', 'Retries', 'Int', minimum=0, maximum=5),
                    setup.parameter('enabled_flag', 'Enabled Flag', 'Check'),
                    setup.parameter('customer_id', 'Customer')):
                    provider.append('parameters', row)
                provider.save()
                endpoint = f'https://localhost:{server.server_port}/decision'
                with patch.dict(frappe.conf, {'messaging_ai_allowed_hosts': ['localhost']}), patch.object(adapters.requests.certs, 'where', return_value=str(cert)):
                    first = self._profile(self.prefix+'_a', provider, endpoint, secret, model='classifier-a', temperature=0.2, customer_id='customer-a')
                    second = self._profile(self.prefix+'_b', provider, endpoint, secret, model='classifier-b', retries=0, enabled_flag=False)
                    request, refs = build_request('Saldo [numero_de_contrato]', {}, {'balance': dict(name='Consultar saldo', description='Saldo público')}, 'es')
                    result = adapters.run(provider, config.configuration_values(first, provider), request)
                    self.assertEqual(normalize_response(result, request, refs, minimum_confidence=.6).flows, ['balance'])
                    adapters.run(provider, config.configuration_values(second, provider), request)
                    plain = self.provider(suffix='plain')
                    plain_profile = self._profile(self.prefix+'_plain', plain, endpoint, secret)
                    adapters.run(plain, config.configuration_values(plain_profile, plain), request)
                    contract = {'schema_version', 'request_id', 'message', 'language', 'greeting_available', 'human_attention_available', 'context', 'available_flows', 'allowed_outcomes'}
                    left, right, bare = observed
                    self.assertEqual(set(left) - {'provider_parameters'}, contract)
                    self.assertEqual(set(right) - {'provider_parameters'}, contract)
                    self.assertEqual(set(bare), contract)
                    self.assertNotIn('provider_parameters', bare)
                    self.assertEqual(left['schema_version'], '1')
                    self.assertEqual(left['provider_parameters'], {'customer_id': 'customer-a', 'model': 'classifier-a', 'temperature': 0.2})
                    self.assertIs(type(left['provider_parameters']['temperature']), float)
                    self.assertEqual(right['provider_parameters'], {'enabled_flag': False, 'model': 'classifier-b', 'retries': 0})
                    self.assertIs(right['provider_parameters']['enabled_flag'], False)
                    self.assertIs(type(right['provider_parameters']['retries']), int)
                    for body in (left, right, bare):
                        self.assertNotIn(secret, json.dumps(body))
                        for transport in adapters.REST_TRANSPORT_KEYS:
                            self.assertNotIn(transport, body.get('provider_parameters', {}))
                    self.assertNotIn('customer-a', json.dumps(right))
                    self.assertNotIn('classifier-a', json.dumps(right))
                    missing = self._profile(self.prefix+'_missing', provider, endpoint, secret, temperature=0.2)
                    before = len(observed)
                    with self.assertRaises(adapters.AIServiceError) as error:
                        adapters.run(provider, config.configuration_values(missing, provider), request)
                    self.assertEqual(error.exception.code, 'configuration_incomplete')
                    self.assertEqual(len(observed), before)
                    extraction = self.provider('Parameter Extraction', suffix='extract')
                    extraction.append('parameters', setup.parameter('model', 'Model', required=1))
                    extraction.save()
                    nested = adapters.outbound_request(extraction, {'model': 'extract-model'}, request)
                    self.assertEqual(nested['provider_parameters'], {'model': 'extract-model'})
                    self.assertNotIn('parameters', nested)
                    self.assertNotIn('provider_parameters', wire.to_json(request))
                    with self.assertRaises(adapters.AIServiceError) as error:
                        adapters.run(extraction, {'model': 'extract-model', 'timeout': 5, 'minimum_confidence': .6,
                            'endpoint': endpoint, 'auth_type': 'None'}, request)
                    self.assertEqual(error.exception.code, 'capability_unavailable')
                    self.assertEqual(len(observed), before)
                for suffix, rows, text in (
                    ('secret', setup.adapter_parameters('rest') + [setup.parameter('extra_secret', 'Extra', 'Password', required=1)], 'Additional secret parameters'),
                    ('reserved', setup.adapter_parameters('rest') + [setup.parameter('message', 'Message')], 'reserved contract name')):
                    doc = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix+'_'+suffix,
                        provider_name='Rejected classifier', provider_type='External', profile_type='Decision', enabled=1,
                        schema_version='1', parameters=rows))
                    with self.subTest(text=text), self.assertRaises(frappe.ValidationError) as error:
                        doc.insert()
                    self.assertIn(text, str(error.exception))
                rejected = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix+'_hot', provider=provider.name, enabled=1))
                rejected.append('parameters', dict(parameter_key='temperature', value=json.dumps('hot')))
                with self.assertRaises(frappe.ValidationError):
                    rejected.insert()
                logs = frappe.get_all('AI Request Log', filters={'provider': ['in', [provider.name, plain.name]]}, fields=['name', 'request_text'])
                self.assertTrue(logs)
                for log in logs:
                    self.assertEqual(log.request_text, request.message.text)
                    self.assertNotIn(secret, frappe.get_doc('AI Request Log', log.name).as_json())
            finally:
                server.shutdown(); server.server_close(); thread.join(3)

    def _profile(self, name, provider, endpoint, secret, **values):
        doc = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=name, provider=provider.name, enabled=1))
        values = dict(endpoint=endpoint, auth_type='Bearer', timeout=5, **values)
        for key, value in values.items():
            doc.append('parameters', dict(parameter_key=key, value=json.dumps(value)))
        doc.append('parameters', dict(parameter_key='auth_secret', secret_value=secret))
        doc.insert()
        return doc


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
