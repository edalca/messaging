"""Typed capability, ownership, local execution and JSON contract regressions."""
from messaging.chatbot.doctype.bot_settings.interpretation import build_request, normalize_response
import copy
import json
import unittest
from unittest.mock import patch

import frappe
from messaging.chatbot.doctype.ai_provider import ai_provider as provider_catalog

from dataclasses import replace
from messaging.registrations.ai_providers import *
from messaging.registrations import ai_providers as wire
from messaging.chatbot.doctype.ai_configuration import execution as adapters
from messaging import registry as registrations


def synthetic_interpret(request, values):
    return DecisionResponse(request.request_id, DecisionOutcome.UNRECOGNIZED if request.available_flows else DecisionOutcome.EMPTY_CATALOG)


def synthetic_validate(values):
    return None


SYNTHETIC = AIProvider(provider_id='synthetic', provider_name='Synthetic', app='messaging',
    decision=DecisionCapability(synthetic_interpret), validate_configuration=synthetic_validate,
    parameters=(AIProviderParameter('timeout','Timeout','Int',required=True,default_value='10',minimum=1,maximum=60),
        AIProviderParameter('minimum_confidence','Minimum Confidence','Float',required=True,default_value='0.60',minimum=0.500001,maximum=1),
        AIProviderParameter('model','Model',required=True,default_value='synthetic-latest'),
        AIProviderParameter('api_key','API Key','Password',required=True),
        AIProviderParameter('intent_probability','Intent Probability','Float',required=True,default_value='0.75',minimum=0.500001,maximum=1),
        AIProviderParameter('negation_probability','Negation Probability','Float',required=True,default_value='0.65',minimum=0.500001,maximum=1)))


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
                before = provider.modified
                self.assertEqual(register_ai_providers(app='messaging', providers=(definition,))['unchanged'], 1)
                self.assertEqual(frappe.db.get_value('AI Provider', provider.name, 'modified'), before)
                self.assertEqual((bool(provider.decision), bool(provider.parameter_extraction)), (bool(decision), bool(extraction)))
                values = dict(timeout=5, minimum_confidence=.6) if decision else dict(timeout=5)
                if decision:
                    self.assertIsInstance(adapters.run(provider, values, self.request()), DecisionResponse)
                if extraction:
                    request = ParameterExtractionRequest('extract', Message('Unknown'), 'es',
                        (ExtractionParameter('number', 'Number', ParameterType.INT),))
                    self.assertIsInstance(adapters.run(provider, values, request), ParameterExtractionResponse)
                else:
                    with self.assertRaises(AIServiceError):
                        adapters.run(provider, values, ParameterExtractionRequest('extract', Message('Unknown'), 'es',
                            (ExtractionParameter('number', 'Number', ParameterType.INT),)))

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
            adapters.run(frappe.get_doc('AI Provider', self.prefix), dict(timeout=5, minimum_confidence=.6), self.request())

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
        self.assertEqual(AIProvider.__module__,'messaging.registrations.ai_providers.contracts')
        self.assertFalse(Path(wire.__file__).with_name('_ai_provider_registry.py').exists())
        from messaging.registry import _catalog
        tree = ast.parse(Path(_catalog.__file__).read_text())
        for node in ast.walk(tree):
            modules = ([node.module or ''] if isinstance(node, ast.ImportFrom) else
                [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
            self.assertNotIn('messaging.registry.ai_providers', modules)
        self.assertIs(_catalog.declaration_types()['ai_providers'], AIProvider)
