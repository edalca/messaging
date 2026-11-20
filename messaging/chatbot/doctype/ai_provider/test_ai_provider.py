"""Typed capability, ownership, local execution and JSON contract regressions."""
from messaging.chatbot.doctype.bot_settings.interpretation import build_request, normalize_response
import copy
import json
import unittest
from unittest.mock import patch

import frappe
from messaging.chatbot.doctype.ai_provider import ai_provider as provider_catalog

from dataclasses import replace
from messaging.registry.ai_providers import *
from messaging.registry import ai_providers as wire
from messaging.registry.ai_providers.rest import (
    DECISION_WIRE, EXTRACTION_WIRE, deserialize_rest_response, serialize_rest_request,
)
from messaging.chatbot.doctype.ai_configuration import execution as adapters
from messaging import registry as registrations


def synthetic_interpret(request, values):
    assert not {'minimum_confidence', 'intent_probability', 'negation_probability'}.intersection(values)
    refs = [row.ref for row in request.available_flows]
    if request.greeting_available:
        refs.append(DecisionSignal.GREETING.value)
    if request.human_attention_available:
        refs.append(DecisionSignal.HUMAN_ATTENTION.value)
    return DecisionEvidence(request.request_id, tuple(
        DecisionClassificationResult(ref, DecisionClassification.ABSENT) for ref in refs
    ))


def synthetic_validate(values):
    return None


SYNTHETIC = AIProvider(provider_id='synthetic', provider_name='Synthetic', app='messaging',
    decision=DecisionCapability(synthetic_interpret), validate_configuration=synthetic_validate,
    parameters=(AIProviderParameter('timeout','Timeout','Int',required=True,default_value='10',minimum=1,maximum=60),
        AIProviderParameter('model','Model',required=True,default_value='synthetic-latest'),
        AIProviderParameter('api_key','API Key','Password',required=True)))


def install_synthetic_provider(testcase):
    from messaging import registry as registrations
    from messaging.registry import _catalog
    original = _catalog.application_declarations
    def installed(app, **kwargs):
        groups = dict(original(app, **kwargs))
        if app == 'messaging' and not any(d.provider_id == SYNTHETIC.provider_id for d in groups['ai_providers']):
            groups['ai_providers'] = (*groups['ai_providers'], SYNTHETIC)
        return groups
    guard = patch.object(_catalog, 'application_declarations', side_effect=installed)
    guard.start()
    testcase.addCleanup(guard.stop)
    existed = frappe.db.exists('AI Provider', SYNTHETIC.provider_id)
    def cleanup():
        if not existed and frappe.db.exists('AI Provider', SYNTHETIC.provider_id):
            if frappe.db.exists('AI Configuration', {'provider': SYNTHETIC.provider_id}):
                raise AssertionError('A synthetic profile escaped its fixture cleanup.')
            frappe.db.delete('AI Provider Parameter', {'parent': SYNTHETIC.provider_id, 'parenttype': 'AI Provider'})
            frappe.db.delete('AI Provider', {'name': SYNTHETIC.provider_id})
            frappe.db.commit()
    testcase.addCleanup(cleanup)
    sync_ai_providers()


from messaging.chatbot.doctype.ai_configuration import ai_configuration as config
from messaging.chatbot.doctype.ai_provider.ai_provider import adapter_parameters
from messaging.registry.ai_providers import register_ai_providers, _declared
from messaging.chatbot.doctype.bot_settings.interpretation import Decision, InvalidInterpretation, validate_decision
from messaging.chatbot.doctype.bot_flow.execution import process_arguments
from messaging.chatbot.doctype.bot_parameter.bot_parameter import validate_input_value


def declaration(provider_id, **overrides):
    values = dict(provider_id=provider_id, provider_name='Synthetic internal')
    values.update(overrides)
    return replace(SYNTHETIC, **values)


def sync_ai_providers():
    from messaging import registry as registrations
    return register_ai_providers(app='messaging', providers=registrations._installed_declarations('messaging', 'ai_providers'))



def synthetic_extraction(request, values):
    return ParameterExtractionResponse(request.request_id,
        tuple(ExtractedParameter(row.ref, ExtractionStatus.NOT_FOUND) for row in request.parameters))


def synthetic_private_evidence(request, values):
    assert values['api_key'] == 'private-test-token'
    assert values['api_key'] not in serialize(request).decode()
    try:
        values['api_key'] = 'overwritten'
    except TypeError:
        return synthetic_interpret(request, values)
    raise AssertionError('Provider options must be read-only')


def synthetic_mixed_evidence(request, options):
    rows = []
    for flow in request.available_flows:
        if flow.label == 'Service status':
            row = DecisionClassificationResult(flow.ref, DecisionClassification.REQUEST, .84,
                DecisionProbabilities(request=.88))
        else:
            row = DecisionClassificationResult(flow.ref, DecisionClassification.UNCLEAR, .39,
                DecisionProbabilities(unclear=.54))
        rows.append(row)
    return DecisionEvidence(request.request_id, tuple(rows))


def synthetic_wrong_response(request, values):
    return {'schema_version': SCHEMA_VERSION}


def synthetic_wire_response(request, decision, confidence=1.0):
    """Translate private proposals only in fixtures; REST peers exchange JSON."""
    if isinstance(request, dict):
        request = deserialize_request({key: value for key, value in request.items() if key != 'provider_parameters'})
    return DecisionResponse(request.request_id, DecisionOutcome(decision.outcome), tuple(decision.flows),
        tuple(decision.excluded_flows), decision.greeting_detected, confidence,
        human_attention_requested=decision.human_attention_requested)


class TestTypedAIProvider(unittest.TestCase):
    def setUp(self):
        frappe.set_user('Administrator')
        frappe.local.request = None
        self.prefix = 'typed_' + frappe.generate_hash(length=10)

    def tearDown(self):
        frappe.db.rollback()
        frappe.local.request = None
        frappe.set_user('Administrator')

    def request(self, **changes):
        request = DecisionRequest('correlation', Message('Hello, check my balance'), 'es',
            (FlowReference('balance', 'Balance'), FlowReference('status', 'Service status')), greeting_available=True)
        return replace(request, **changes)

    def register(self, definition):
        from messaging.registry import _catalog
        original = _catalog.application_declarations
        def installed(app, **kwargs):
            groups = dict(original(app, **kwargs))
            if app == 'messaging': groups['ai_providers'] = (definition,)
            return groups
        guard = patch.object(_catalog, 'application_declarations', side_effect=installed)
        guard.start(); self.addCleanup(guard.stop)
        return register_ai_providers(app='messaging', providers=(definition,))

    def test_three_capability_combinations_and_idempotent_registration(self):
        for decision, extraction in ((DecisionCapability(synthetic_interpret), None),
            (None, ParameterExtractionCapability(synthetic_extraction)),
            (DecisionCapability(synthetic_interpret), ParameterExtractionCapability(synthetic_extraction))):
            with self.subTest(decision=bool(decision), extraction=bool(extraction)):
                definition = replace(SYNTHETIC, provider_id=self.prefix + str(bool(decision)).lower() + str(bool(extraction)).lower(),
                    decision=decision, parameter_extraction=extraction,
                    parameters=SYNTHETIC.parameters if decision else (SYNTHETIC.parameters[0],))
                self.assertEqual(self.register(definition)['created'], 1)
                provider = frappe.get_doc('AI Provider', definition.provider_id)
                self.assertEqual(provider.status, 'Enabled')
                before = provider.modified
                self.assertEqual(register_ai_providers(app='messaging', providers=(definition,))['unchanged'], 1)
                self.assertEqual(frappe.db.get_value('AI Provider', provider.name, 'modified'), before)
                with self.assertRaises(frappe.PermissionError):
                    provider_catalog.set_provider_enabled(provider.name, False)
                self.assertEqual((bool(provider.decision), bool(provider.parameter_extraction)), (bool(decision), bool(extraction)))
                values = dict(timeout=5)
                if decision:
                    self.assertIsInstance(adapters.run(provider, values, self.request()), DecisionEvidence)
                if extraction:
                    request = ParameterExtractionRequest('extract', Message('Unknown'), 'es',
                        (ExtractionParameter('number', 'Number', ParameterType.INT),))
                    self.assertIsInstance(adapters.run(provider, values, request), ParameterExtractionResponse)
                else:
                    with self.assertRaises(AIServiceError):
                        adapters.run(provider, values, ParameterExtractionRequest('extract', Message('Unknown'), 'es',
                            (ExtractionParameter('number', 'Number', ParameterType.INT),)))

    def test_internal_provider_rejects_reserved_parameters_for_either_capability(self):
        for key in ('minimum_confidence', 'intent_probability', 'negation_probability'):
            parameter = AIProviderParameter(key, 'Reserved')
            for operation, extraction in ((SYNTHETIC.decision, None), (None, ParameterExtractionCapability(synthetic_extraction))):
                with self.subTest(key=key, extraction=bool(extraction)), self.assertRaises(frappe.ValidationError):
                    self.register(replace(SYNTHETIC, provider_id=self.prefix, decision=operation,
                        parameter_extraction=extraction, parameters=(*SYNTHETIC.parameters, parameter)))
                self.assertFalse(frappe.db.exists('AI Provider', self.prefix))

    def test_internal_execution_validates_each_response_and_protects_options(self):
        definition = replace(SYNTHETIC, provider_id=self.prefix, decision=DecisionCapability(synthetic_private_evidence),
            parameter_extraction=ParameterExtractionCapability(synthetic_wrong_response))
        self.register(definition)
        provider = frappe.get_doc('AI Provider', self.prefix)
        request = self.request()
        result = adapters.run(provider, {'timeout': 5, 'api_key': 'private-test-token'}, request)
        self.assertIsInstance(result, DecisionEvidence)
        log = frappe.get_last_doc('AI Request Log', filters={'provider': provider.name})
        self.assertNotIn('private-test-token', log.as_json())
        normalized = json.loads(log.result_summary)['normalized_response']
        self.assertEqual(normalized['results'][0]['classification'], 'absent')
        with self.assertRaises(AIServiceError):
            adapters.run(provider, {'timeout': 5, 'minimum_confidence': .6}, request)
        with self.assertRaises(InvalidInterpretation):
            adapters.run(provider, {'timeout': 5}, ParameterExtractionRequest(
                'extract', Message('Unknown'), 'es', (ExtractionParameter('number', 'Number', ParameterType.INT),)))

    def test_operational_interpretation_uses_messaging_policy_on_internal_evidence(self):
        from messaging.chatbot.doctype.bot_settings.interpretation import interpret
        definition = replace(SYNTHETIC, provider_id=self.prefix, decision=DecisionCapability(synthetic_mixed_evidence))
        self.register(definition)
        profile = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix, provider=self.prefix))
        profile.append('parameters', dict(parameter_key='api_key', secret_value='synthetic-interpretation'))
        profile.insert()
        adapters.set_profile_status(profile, 'Enabled')
        settings = frappe._dict(interpretation_enabled=1, active_ai_configuration=profile.name)
        flows = {key: dict(name=name, description='Synthetic description', parameters={})
            for key, name in (('status', 'Service status'), ('water', 'Water supply problem'))}
        with patch('messaging.chatbot.doctype.ai_evaluation_run.runtime.approval', return_value={'approved': True}), \
             patch('messaging.chatbot.doctype.bot_settings.language.resolve_process_language', return_value='es'):
            decision = interpret('Estado del servicio de agua', {}, flows, {}, settings)
        self.assertEqual(decision.flows, ['status'])
        self.assertEqual(decision.outcome, 'recognized')
        self.assertFalse(decision.parameters)

    def test_external_provider_availability_and_status_protection(self):
        provider = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix, provider_name='External REST')).insert()
        self.assertEqual(provider.status, 'Disabled')
        self.assertIn('auth_secret', {row.parameter_key for row in provider.parameters})
        provider.status = 'Enabled'
        with self.assertRaises(frappe.PermissionError):
            provider.save()
        with self.assertRaises(frappe.PermissionError):
            provider.db_set('status', 'Unavailable')
        self.assertEqual(provider_catalog.set_provider_enabled(provider.name, True)['status'], 'Enabled')
        self.assertEqual(provider_catalog.set_provider_enabled(provider.name, False)['status'], 'Disabled')
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError):
            provider_catalog.set_provider_enabled(provider.name, True)

    def test_invalid_capability_declarations(self):
        for changes in ({'decision': None}, {'decision': {}}, {'decision': 'decision'},
            {'parameter_extraction': synthetic_extraction}, {'schema_version': '2'}):
            with self.subTest(changes=changes), self.assertRaises(InvalidInterpretation):
                replace(SYNTHETIC, **changes)
        with self.assertRaises(InvalidInterpretation): DecisionCapability('module.function')

    def test_invalid_registration_declarations_raise_validation_error(self):
        with self.assertRaises(InvalidInterpretation):
            replace(SYNTHETIC, enabled=1.0)
        for definitions in (None, {}, 'provider', iter((SYNTHETIC,)), (object(),),
            (replace(SYNTHETIC, app='another_app'),),
            (SYNTHETIC, SYNTHETIC)):
            with self.subTest(definitions=type(definitions).__name__), self.assertRaises(frappe.ValidationError):
                _declared('messaging', definitions)

    def test_owner_operation_and_signature_are_verified(self):
        for operation in (frappe.get_doc, synthetic_validate):
            with self.subTest(operation=operation), self.assertRaises(frappe.ValidationError):
                self.register(replace(SYNTHETIC, provider_id=self.prefix, decision=DecisionCapability(operation)))
        with self.assertRaises(frappe.PermissionError):
            register_ai_providers(app='frappe', providers=(SYNTHETIC,))

    def test_local_arbitrary_response_is_rejected(self):
        definition = replace(SYNTHETIC, provider_id=self.prefix, decision=DecisionCapability(synthetic_wrong_response))
        self.register(definition)
        with self.assertRaises(InvalidInterpretation):
            adapters.run(frappe.get_doc('AI Provider', self.prefix), dict(timeout=5), self.request())

    def test_request_types_versions_fields_and_bounds(self):
        for changes in ({'schema_version':'2'}, {'language':''}, {'message':{}}, {'greeting_available':1},
            {'available_flows':[]}, {'allowed_outcomes':('recognized',)}, {'context':SemanticContext(('other',))}):
            with self.subTest(changes=changes), self.assertRaises(InvalidInterpretation): self.request(**changes)
        for text in ('', 'x' * 16001):
            with self.assertRaises(InvalidInterpretation): Message(text)
        data = to_json(self.request()); data['registration_pending'] = True
        with self.assertRaises(InvalidInterpretation): deserialize_request(data)
        with self.assertRaises(InvalidInterpretation): validate_request({})

    def test_json_round_trip_and_closed_objects(self):
        request = self.request()
        self.assertEqual(deserialize_request(serialize(request)), request)
        response = DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, ('balance', 'status'), greeting_detected=True)
        self.assertEqual(deserialize_response(serialize(response), request), response)
        data = to_json(response); data['decision']['parameters'] = {}
        with self.assertRaises(InvalidInterpretation): deserialize_response(data, request)
        with self.assertRaises(InvalidInterpretation): wire.strict_json('{"key":1,"key":2}')
        self.assertNotIn('registration_pending', serialize(request).decode())

    def test_greeting_alone_and_with_multiple_flows(self):
        request = self.request()
        for flows in ((), ('balance', 'status')):
            result = DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, flows, greeting_detected=True)
            self.assertEqual(validate_response(result, request), result)
        empty = self.request(available_flows=())
        validate_response(DecisionResponse(empty.request_id, DecisionOutcome.RECOGNIZED, greeting_detected=True), empty)
        validate_response(DecisionResponse(empty.request_id, DecisionOutcome.EMPTY_CATALOG), empty)

    def test_human_signal_is_typed_independent_and_requires_availability(self):
        for available in ((), self.request().available_flows):
            request = self.request(available_flows=available, human_attention_available=True)
            for outcome, flows in ((DecisionOutcome.RECOGNIZED, ()),
                                   (DecisionOutcome.AMBIGUOUS, ()),
                                   (DecisionOutcome.RECOGNIZED, tuple(row.ref for row in available))):
                response = DecisionResponse(request.request_id, outcome, flows, greeting_detected=True,
                    human_attention_requested=True)
                self.assertEqual(deserialize_request(serialize(request)), request)
                self.assertEqual(deserialize_response(serialize(response), request), response)
                decision = normalize_response(response, request,
                    {row.ref: row.ref for row in available}, minimum_confidence=.6)
                self.assertTrue(decision.human_attention_requested)
                self.assertTrue(decision.greeting_detected)
                self.assertEqual(decision.ambiguous, outcome is DecisionOutcome.AMBIGUOUS)
            with self.assertRaises(InvalidInterpretation):
                validate_response(response, replace(request, human_attention_available=False))
        for value in (0, 1, 'true', None):
            with self.subTest(value=value), self.assertRaises(InvalidInterpretation):
                self.request(human_attention_available=value)
            with self.subTest(value=value), self.assertRaises(InvalidInterpretation):
                DecisionResponse('correlation', DecisionOutcome.RECOGNIZED, human_attention_requested=value)
        request = self.request(human_attention_available=True)
        response = DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED,
            human_attention_requested=True, confidence=.55)
        self.assertFalse(normalize_response(response, request, {}, minimum_confidence=.6).human_attention_requested)
        for key, data in (('human_attention_available', to_json(request)),
                          ('human_attention_requested', to_json(response)['decision'])):
            del data[key]
            with self.assertRaises(InvalidInterpretation):
                if key == 'human_attention_available':
                    deserialize_request(data)
                else:
                    deserialize_response(dict(schema_version='1', request_id=request.request_id, decision=data), request)

    def test_greeting_does_not_authorize_ambiguous_business_flows(self):
        request = self.request()
        response = DecisionResponse(request.request_id, DecisionOutcome.AMBIGUOUS, greeting_detected=True)
        decision = normalize_response(response, request, {'balance':'internal'}, minimum_confidence=.6)
        self.assertTrue(decision.greeting_detected); self.assertTrue(decision.ambiguous); self.assertFalse(decision.flows)
        with self.assertRaises(InvalidInterpretation):
            DecisionResponse(request.request_id, DecisionOutcome.AMBIGUOUS, ('balance',), greeting_detected=True)

    def test_disabled_greeting_unknown_references_and_correlation(self):
        request = self.request(greeting_available=False)
        for response in (DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, greeting_detected=True),
            DecisionResponse('other', DecisionOutcome.UNRECOGNIZED),
            DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, ('other',)),
            DecisionResponse(request.request_id, DecisionOutcome.EMPTY_CATALOG)):
            with self.subTest(response=response), self.assertRaises(InvalidInterpretation): validate_response(response, request)

    def test_response_invariants_and_insufficient_confidence(self):
        request = self.request()
        for changes in ({'outcome':'recognized'}, {'confidence':True}, {'confidence':float('nan')},
            {'confidence':1.1}, {'flows':('balance','balance')}, {'excluded_flows':('balance',)},
            {'outcome':DecisionOutcome.UNRECOGNIZED}):
            with self.subTest(changes=changes), self.assertRaises(InvalidInterpretation):
                DecisionResponse(**dict(dict(request_id=request.request_id, outcome=DecisionOutcome.RECOGNIZED, flows=('balance',)), **changes))
        response = DecisionResponse(request.request_id, DecisionOutcome.RECOGNIZED, ('balance',), greeting_detected=True, confidence=.55)
        decision = normalize_response(response, request, {'balance':'private'}, minimum_confidence=.6)
        self.assertTrue(decision.ambiguous); self.assertFalse(decision.flows); self.assertFalse(decision.greeting_detected)

    def test_extraction_contract_without_conversational_activation(self):
        request = ParameterExtractionRequest('extract', Message('123'), 'es',
            (ExtractionParameter('number', 'Number', ParameterType.INT),))
        response = ParameterExtractionResponse('extract', (ExtractedParameter('number', ExtractionStatus.EXTRACTED, (123,)),))
        self.assertEqual(deserialize_request(serialize(request), AICapability.PARAMETER_EXTRACTION), request)
        self.assertEqual(deserialize_response(serialize(response), request), response)
        validate_response(ParameterExtractionResponse('extract', (ExtractedParameter('number', ExtractionStatus.NOT_SUPPORTED),)), request)
        for rows in ((ExtractedParameter('other', ExtractionStatus.NOT_FOUND),),
            (ExtractedParameter('number', ExtractionStatus.EXTRACTED, ('123',)),),
            (ExtractedParameter('number', ExtractionStatus.EXTRACTED, (123,124)),)):
            with self.assertRaises(InvalidInterpretation):validate_response(ParameterExtractionResponse('extract', rows), request)

    def test_administrative_guards_and_external_profile_purpose(self):
        self.register(replace(SYNTHETIC, provider_id=self.prefix))
        provider = frappe.get_doc('AI Provider', self.prefix)
        with self.assertRaises(frappe.PermissionError): provider.save()
        with self.assertRaises(frappe.PermissionError): provider.on_trash()
        for payload in (dict(provider_type='Internal'), dict(application='messaging')):
            with self.assertRaises(frappe.PermissionError):
                frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix+'_forged', provider_name='Forged', **payload)).insert()
        external = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix+'_external', provider_name='REST',
            decision=1, parameter_extraction=1, enabled=1)).insert()
        provider_catalog.set_provider_enabled(external.name, True)
        for purpose in ('Decision', 'Parameter Extraction'):
            profile = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix+purpose, provider=external.name,
                profile_type=purpose, enabled=0)).insert()
            self.assertEqual(profile.profile_type, purpose)
        external.decision=0
        with self.assertRaises(frappe.ValidationError):external.save()


    def test_storage_controller_never_imports_registration_contracts(self):
        import ast
        import inspect
        from pathlib import Path
        tree=ast.parse(Path(inspect.getsourcefile(provider_catalog)).read_text())
        for node in ast.walk(tree):
            modules=([node.module or ''] if isinstance(node,ast.ImportFrom) else
                [alias.name for alias in node.names] if isinstance(node,ast.Import) else [])
            self.assertFalse(any(module.startswith('messaging.registry') for module in modules))
        self.assertFalse(hasattr(provider_catalog,'internal_provider'))
        self.assertFalse(hasattr(provider_catalog,'_declared'))
        self.assertEqual(AIProvider.__module__,'messaging.registry.ai_providers.contracts')
        self.assertFalse(Path(wire.__file__).with_name('_ai_provider_registry.py').exists())
        from messaging.registry import _catalog
        tree = ast.parse(Path(_catalog.__file__).read_text())
        for node in ast.walk(tree):
            modules = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            self.assertNotIn('messaging.registry.ai_providers', modules)
        self.assertIs(_catalog.declaration_types()['ai_providers'], AIProvider)


class TestRESTCapabilityWire(unittest.TestCase):
    def setUp(self):
        self.request = DecisionRequest('decision-correlation', Message('Prepared text'), 'es',
            (FlowReference('one', 'One', 'First option'), FlowReference('two', 'Two'), FlowReference('three', 'Three')),
            greeting_available=True, human_attention_available=True, context=SemanticContext(('one',)))
        self.evidence = DecisionEvidence('decision-correlation', (
            DecisionClassificationResult('one', DecisionClassification.REQUEST, .82,
                DecisionProbabilities(request=.91, absent=.02, excluded=0.0, unclear=.07)),
            DecisionClassificationResult('two', DecisionClassification.REQUEST, None, DecisionProbabilities(request=.9)),
            DecisionClassificationResult('three', DecisionClassification.EXCLUDED, .79, DecisionProbabilities(excluded=.92)),
            DecisionClassificationResult('greeting', DecisionClassification.ABSENT, 0.0),
            DecisionClassificationResult('human_attention', DecisionClassification.ABSENT),
        ))
        self.body = dict(to_json(self.evidence), wire_version=DECISION_WIRE)

    def test_decision_transmits_typed_evidence_without_a_selection(self):
        from messaging.chatbot.doctype.ai_configuration.policy import DecisionPolicy

        self.assertEqual(serialize_rest_request(self.request), dict(to_json(self.request), wire_version=DECISION_WIRE))
        self.body['results'][1].pop('confidence')
        self.body['results'][1]['probabilities'] = {'request': .9}
        self.body['results'][-1].pop('probabilities')
        decoded = deserialize_rest_response(serialize(self.body), self.request)
        self.assertEqual(decoded, self.evidence)
        self.assertIsNone(decoded.results[1].confidence)
        self.assertEqual(decoded.results[0].probabilities.excluded, 0.0)
        self.assertEqual(decoded.results[3].confidence, 0.0)
        refs = {'one': 'flow-a', 'two': 'flow-b', 'three': 'flow-c'}
        policy = DecisionPolicy()
        result = normalize_response(decoded, self.request, refs, policy=policy)
        self.assertEqual(result, normalize_response(self.evidence, self.request, refs, policy=policy))
        self.assertEqual((result.flows, result.excluded_flows), (['flow-a', 'flow-b'], ['flow-c']))

    def test_decision_rejects_invalid_evidence(self):
        for changes in (
            {'request_id': 'different'}, {'schema_version': '2'}, {'wire_version': 'decision-v1'},
            {'results': self.body['results'][:-1]}, {'results': self.body['results'] + self.body['results'][:1]},
            {'results': [dict(self.body['results'][0], option_id='foreign'), *self.body['results'][1:]]},
            {'decision': {'outcome': 'ambiguous'}},
        ):
            with self.subTest(changes=changes.keys()), self.assertRaises(InvalidInterpretation):
                deserialize_rest_response(dict(self.body, **changes), self.request)
        for row in (
            {'classification': 'recognized'}, {'classification': []}, {'confidence': True}, {'confidence': 1.1},
            {'probabilities': {'request': .6, 'unclear': .8}}, {'probabilities': {'request': .4, 'unclear': .6}},
            {'probabilities': {'request': .5, 'absent': .1, 'excluded': .1, 'unclear': .1}},
            {'probabilities': {'request': .9, 'unknown': .1}}, {'probabilities': {}}, {'action': 'execute'},
        ):
            with self.subTest(row=row), self.assertRaises(InvalidInterpretation):
                deserialize_rest_response(dict(self.body, results=[dict(self.body['results'][0], **row),
                    *self.body['results'][1:]]), self.request)
        for raw in ('{', '{"wire_version":"decision-v2","wire_version":"decision-v1"}',
            '{"probability":NaN}', '{"probability":1e999}'):
            with self.subTest(raw=raw), self.assertRaises(InvalidInterpretation):
                deserialize_rest_response(raw, self.request)

    def test_low_confidence_unclear_preserves_request_and_material_uncertainty_clarifies(self):
        from messaging.chatbot.doctype.ai_configuration.policy import DecisionPolicy

        refs = {'one': 'a', 'two': 'b', 'three': 'c'}
        for confidence, probability, expected in ((.35, .51, ['a']), (.70, .70, [])):
            body = copy.deepcopy(self.body)
            body['results'][1].update(classification='unclear', confidence=confidence, probabilities={'unclear': probability})
            decision = normalize_response(deserialize_rest_response(body, self.request), self.request, refs,
                policy=DecisionPolicy())
            self.assertEqual((decision.flows, decision.ambiguous), (expected, not expected))

    def test_extraction_wire_uses_exact_public_types_without_execution(self):
        request = ParameterExtractionRequest('extraction-correlation', Message('Source text'), 'es', (
            ExtractionParameter('count', 'Count', ParameterType.INT, True, description='Item count'),
            ExtractionParameter('enabled', 'Enabled', ParameterType.BOOLEAN),
            ExtractionParameter('amount', 'Amount', ParameterType.DECIMAL),
            ExtractionParameter('date', 'Date', ParameterType.DATE),
            ExtractionParameter('name', 'Name', ParameterType.NAME),
            ExtractionParameter('reference', 'Reference', ParameterType.EXPRESSION),
        ), context='Authorized context')
        outcome = ParameterExtractionResponse(request.request_id, (
            ExtractedParameter('count', ExtractionStatus.EXTRACTED, (0, 3)),
            ExtractedParameter('enabled', ExtractionStatus.EXTRACTED, (False,)),
            ExtractedParameter('amount', ExtractionStatus.EXTRACTED, ('000.1200',), evidence=('000.1200',)),
            ExtractedParameter('date', ExtractionStatus.EXTRACTED, ('2026-11-19',)),
            ExtractedParameter('name', ExtractionStatus.NOT_FOUND),
            ExtractedParameter('reference', ExtractionStatus.AMBIGUOUS, evidence=('000 or 001',)),
        ))
        outbound = serialize_rest_request(request)
        self.assertEqual(outbound.pop('wire_version'), EXTRACTION_WIRE)
        self.assertEqual(deserialize_request(outbound, AICapability.PARAMETER_EXTRACTION), request)
        response = dict(to_json(outcome), wire_version=EXTRACTION_WIRE)
        self.assertEqual(deserialize_rest_response(serialize(response), request), outcome)
        for index, values in ((0, [False]), (1, [0]), (1, [True, False]), (2, [.12]), (2, ['1e-2']),
            (3, ['2026-02-30']), (3, ['20261119']), (4, ['invented']), (5, ['001'])):
            malformed = copy.deepcopy(response)
            malformed['parameters'][index]['values'] = values
            with self.subTest(index=index, values=values), self.assertRaises(InvalidInterpretation):
                deserialize_rest_response(malformed, request)
        for invalid in (dict(response, request_id='foreign'), dict(response, wire_version=DECISION_WIRE),
            dict(response, parameters=response['parameters'][:-1]),
            dict(response, parameters=response['parameters'] + response['parameters'][:1])):
            with self.assertRaises(InvalidInterpretation):
                deserialize_rest_response(invalid, request)
