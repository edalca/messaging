"""Generic request-log immutability and native metadata."""
import unittest
import frappe
from messaging.chatbot.doctype.ai_request_log.ai_request_log import AIRequestLog

class TestAIRequestLog(unittest.TestCase):
    def test_log_retention_is_independent_of_provider(self):
        with self.assertRaises(frappe.PermissionError):
            AIRequestLog({'doctype': 'AI Request Log'}).on_trash()
        field = frappe.get_meta('AI Request Log').get_field('request_text')
        self.assertEqual((field.fieldtype, field.permlevel, field.read_only), ('Code', 1, 1))


class TestProviderDiagnostics(unittest.TestCase):
    def setUp(self):
        from messaging.registry import ai_providers as wire
        from messaging.chatbot.doctype.ai_request_log import ai_request_log as logs
        self.wire, self.logs = wire, logs
        frappe.set_user('Administrator')
        self.request = wire.DecisionRequest('diagnostic_test', wire.Message('Masked [parameter]'), 'es',
            (wire.FlowReference('balance', 'Balance'),), greeting_available=True, human_attention_available=True)
        self.provider = frappe._dict(name='diagnostic_fixture', parameters=[
            frappe._dict(parameter_key='key', field_type='Password')])

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user('Administrator')

    def test_provider_structure_scores_and_normalized_decision_are_independent(self):
        import json
        from unittest.mock import patch
        body = {'model': 'fixture-v1', 'results': {'question_7': {
            'choice': 'request', 'confidence': .92, 'probabilities': {'request': .92, 'absent': .08}},
            'greeting': .84, 'human': .88}, 'usage': {'input_tokens': 7}}
        response = self.wire.DecisionResponse(self.request.request_id, self.wire.DecisionOutcome.RECOGNIZED,
            ('balance',), confidence=.84, greeting_detected=True, human_attention_requested=True)
        context = {'question_7': {'flow_ref': 'balance', 'instructions': 'Private person Alice'}}
        with patch.object(self.logs, 'record_call', wraps=self.logs.record_call) as writer:
            with self.logs.evaluation_scope(None):
                with self.logs.provider_call(self.provider, {'key': 'secret-fixture'}, self.request) as call:
                    self.wire.capture_response(json.dumps(body), context=context,
                        public_values=('fixture-v1', 'request', 'absent'))
                    self.wire.record_call(None, body, {'question_count': 3}, 12, None, provider=self.provider.name)
                    self.assertFalse(frappe.db.exists('AI Request Log', {'provider': self.provider.name}))
                    call.update(normalized=self.wire.validate_response(response, self.request), contract_validation='Passed')
            self.assertEqual(writer.call_count, 2)  # Collection and the sole final writer.
        docs = frappe.get_all('AI Request Log', filters={'provider': self.provider.name}, fields=['*'])
        self.assertEqual(len(docs), 1)
        evidence = json.loads(docs[0].result_summary)
        self.assertEqual(evidence['provider_response'], body)
        self.assertEqual(evidence['provider_context']['question_7']['flow_ref'], 'balance')
        self.assertEqual(evidence['provider_context']['question_7']['instructions'], '[redacted]')
        self.assertEqual(evidence['normalized_response'], self.wire.to_json(response))
        self.assertEqual(docs[0].input_tokens, 7)
        self.assertEqual(evidence['contract_validation'], 'Passed')
        self.assertNotIn('results', self.wire.to_json(response))

    def test_secret_reflections_personal_text_and_arbitrary_formats(self):
        import json
        body = {'access_token': 'credential', 'headers': {'Authorization': 'Bearer credential'},
            'nested': [{'password': 'credential', 'answer': 'Alice lives in a private address',
                'score': .78, 'email': 'alice@example.test', 'values': ['12345'], 'input_tokens': 9,
                'phone_number': 55512345, 'Alice Smith': {'private_number': 12345}}],
            'technical': 'credential', 'credential-key': 1}
        protected, status = self.logs.protected_json(body, secrets=['credential'], public_values=['credential'])
        self.assertIsNone(protected)  # A credential reflected in a key cannot be retained safely.
        self.assertEqual(status, 'omitted_invalid_json')
        body.pop('credential-key')
        protected, status = self.logs.protected_json(body, secrets=['credential'], public_values=['credential'])
        self.assertEqual(status, 'redacted')
        self.assertNotIn('credential', json.dumps(protected))
        self.assertNotIn('Alice', json.dumps(protected))
        self.assertEqual(protected['nested'][0]['score'], .78)
        self.assertEqual(protected['nested'][0]['input_tokens'], 9)
        self.assertEqual(protected['nested'][0]['phone_number'], '[redacted]')
        self.assertEqual(protected['nested'][0]['values'], '[redacted]')
        self.assertEqual(self.logs.protected_json([1, {'score': .3}])[0], [1, {'score': .3}])

    def test_size_invalid_json_and_failed_validation_keep_safe_evidence(self):
        import json
        self.assertEqual(self.logs.protected_json('x' * 250001)[1], 'omitted_size_limit')
        for body in ('{"x": NaN}', '{"x":1,"x":2}', b'\xff', {'score': float('inf')}):
            self.assertEqual(self.logs.protected_json(body)[1], 'omitted_invalid_json')
        from unittest.mock import patch
        with patch.object(self.logs, 'record_call') as writer:
            with self.logs.provider_call(self.provider, {'key': 'fixture-secret'}, self.request) as call:
                self.logs.capture_response({'score': .7, 'unexpected': 'private'})
                call.update(error='contract_invalid', contract_validation='Failed', exception_type='InvalidInterpretation')
            evidence = writer.call_args.kwargs['summary']
            self.assertEqual(evidence['provider_response']['score'], .7)
            self.assertEqual(evidence['provider_response']['unexpected'], '[redacted]')
            self.assertIsNone(evidence['normalized_response'])
            self.assertEqual(writer.call_args.kwargs['error'], 'contract_invalid')
        with patch.object(self.logs, 'record_call') as writer:
            with self.logs.provider_call(self.provider, {}, self.request) as call:
                call.update(contract_validation='Passed', normalized=self.wire.DecisionResponse(
                    self.request.request_id, self.wire.DecisionOutcome.UNRECOGNIZED))
            self.assertEqual(writer.call_args.kwargs['summary']['response_status'], 'not_available')

    def test_diagnostics_permissions_and_historical_records(self):
        import json
        self.logs.record_call(provider=self.provider.name, summary={'question_count': 7})
        doc = frappe.get_doc('AI Request Log', {'provider': self.provider.name})
        self.assertNotIn('provider_response', json.loads(doc.result_summary))
        self.assertTrue(self.logs.has_permission(doc, user='Administrator'))
        self.assertFalse(self.logs.has_permission(doc, user='Guest'))
        self.assertEqual(self.logs.get_permission_query_conditions('Guest'), '1=0')
        for field in ('result_summary', 'diagnostics_html', 'request_text'):
            self.assertEqual(frappe.get_meta('AI Request Log').get_field(field).permlevel, 1)
        frappe.set_user('Guest')
        from frappe.client import get
        with self.assertRaises(frappe.PermissionError):
            get('AI Request Log', doc.name)

    def test_rest_boundary_preserves_evidence_and_redacts_contract_failures(self):
        import json
        from dataclasses import replace
        from messaging.chatbot.doctype.ai_configuration import execution
        from messaging.chatbot.doctype.ai_configuration.test_ai_configuration import JSON_HEADERS, simulated_rest_http
        from messaging.registry.ai_providers.rest import DECISION_WIRE

        wire = self.wire
        request = replace(self.request, greeting_available=False, human_attention_available=False)
        provider = frappe._dict(name=self.provider.name, provider_type='External', status='Enabled',
            decision=1, schema_version='1', parameters=[])
        values = dict(endpoint='https://provider.example.test', auth_type='None', timeout=5)
        response = wire.DecisionEvidence(request.request_id, (
            wire.DecisionClassificationResult('balance', wire.DecisionClassification.REQUEST, .92),
        ))
        body = dict(wire.to_json(response), wire_version=DECISION_WIRE)

        def reply(_request, _headers):
            return 200, JSON_HEADERS, wire.serialize(body)

        with simulated_rest_http(reply):
            self.assertEqual(execution.run(provider, values, request), response)
        log = frappe.get_doc('AI Request Log', {'provider': provider.name})
        evidence = json.loads(log.result_summary)
        self.assertEqual(evidence['provider_response'], body)
        self.assertEqual(evidence['normalized_response'], wire.to_json(response))
        self.assertEqual(evidence['http_status'], 200)
        frappe.db.rollback()
        body['results'][0]['option_id'] = 'not_offered'
        with simulated_rest_http(reply), self.assertRaises(wire.InvalidInterpretation):
            execution.run(provider, values, request)
        log = frappe.get_doc('AI Request Log', {'provider': provider.name})
        evidence = json.loads(log.result_summary)
        self.assertEqual(log.status, 'Failed')
        self.assertEqual(log.error_code, 'contract_invalid')
        self.assertEqual(evidence['contract_validation'], 'Failed')
        self.assertIsNone(evidence['normalized_response'])
        self.assertEqual(evidence['provider_response']['results'][0]['option_id'], '[redacted]')

    def test_secret_reflections_in_model_and_request_metadata_are_protected(self):
        from unittest.mock import patch
        with patch.object(self.logs, 'record_call', wraps=self.logs.record_call):
            with self.logs.provider_call(self.provider, {'key': 'private-fixture'}, self.request) as call:
                self.wire.record_call(None, {'model': 'jev-private-fixture'}, {}, 1, None,
                    provider=self.provider.name, actual_model='jev-private-fixture', requested_model='jev-private-fixture',
                    request_text='Masked [parameter] private-fixture')
                call.update(contract_validation='Passed', normalized=self.wire.DecisionResponse(
                    self.request.request_id, self.wire.DecisionOutcome.UNRECOGNIZED))
        doc = frappe.get_doc('AI Request Log', {'provider': self.provider.name})
        self.assertNotIn('private-fixture', str(doc.as_dict()))
        self.assertIn('[parameter]', doc.request_text)
        self.assertIn('[redacted]', doc.request_text)
        self.assertRegex(doc.name, r'^AIR-[0-9]{6}$')
        self.assertTrue(doc.request_key)
        legacy = frappe.get_all('AI Request Log', filters={'name': ['not like', 'AIR-%']}, pluck='name', limit=1)
        if legacy:
            self.assertTrue(frappe.get_doc('AI Request Log', legacy[0]).request_key)
