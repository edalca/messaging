"""Independent contract and policy regressions using complete synthetic evidence."""
import unittest
from dataclasses import replace

from messaging.chatbot.doctype.ai_configuration.policy import DecisionPolicy
from messaging.chatbot.doctype.bot_settings.interpretation import normalize_response
from messaging.registry import ai_providers as ai


class TestInternalCapabilities(unittest.TestCase):
    def setUp(self):
        self.refs = {f'flow_{index}': key for index, key in enumerate(
            ('location', 'availability', 'balance', 'service_status', 'hours', 'water_issue'))}
        self.request = ai.DecisionRequest('decision', ai.Message('Consulta de servicio'), 'es',
            tuple(ai.FlowReference(ref, key) for ref, key in self.refs.items()))

    def decision(self, changes, *, request=None):
        request = request or self.request
        absent = tuple(
            ai.DecisionClassificationResult(ref, ai.DecisionClassification.ABSENT) for ref in self.refs if ref not in changes)
        return ai.DecisionEvidence(request.request_id, (*absent, *changes.values()))

    def classify(self, ref, classification, confidence=None, **scores):
        return ai.DecisionClassificationResult(ref, ai.DecisionClassification(classification), confidence,
            ai.DecisionProbabilities(**scores) if scores else None)

    def test_historical_service_status_and_separate_ambiguity_controls(self):
        # Protected AIR evidence, without the original message or provider request.
        fixtures = (
            ('AIR-000356', ('request', .84, .88), ('unclear', .39, .54), ['service_status']),
            ('AIR-000357', ('request', .85, .89), ('unclear', .33, .50), ['service_status']),
            ('AIR-000358', ('request', .76, .82), ('unclear', .37, .53), ['service_status']),
            ('AIR-000315', ('unclear', .29, .47), ('request', .53, .65), []),
            ('AIR-000316', ('request', .63, .72), ('request', .59, .69), []),
        )
        for log, status, water, expected in fixtures:
            with self.subTest(log=log):
                rows = {ref: self.classify(ref, choice, confidence, **{choice: probability})
                    for ref, (choice, confidence, probability) in zip(('flow_3', 'flow_5'), (status, water))}
                decision = normalize_response(self.decision(rows), self.request, self.refs, policy=DecisionPolicy())
                self.assertEqual(decision.flows, expected)
                self.assertEqual(decision.ambiguous, not expected)

    def test_multiple_requests_exclusions_and_material_uncertainty(self):
        rows = {
            'flow_3': self.classify('flow_3', 'request', .8, request=.9),
            'flow_0': self.classify('flow_0', 'request', request=.8),
            'flow_5': self.classify('flow_5', 'excluded', .8, excluded=.7),
        }
        decision = normalize_response(self.decision(rows), self.request, self.refs, policy=DecisionPolicy())
        self.assertEqual(decision.flows, ['location', 'service_status'])
        self.assertEqual(decision.excluded_flows, ['water_issue'])
        rows['flow_1'] = self.classify('flow_1', 'unclear', .39, unclear=.8)
        decision = normalize_response(self.decision(rows), self.request, self.refs, policy=DecisionPolicy())
        self.assertEqual(decision.flows, ['location', 'service_status'])
        rows['flow_1'] = self.classify('flow_1', 'unclear', .65, unclear=.8)
        decision = normalize_response(self.decision(rows), self.request, self.refs, policy=DecisionPolicy())
        self.assertTrue(decision.ambiguous)
        self.assertEqual(decision.flows, [])
        self.assertEqual(decision.excluded_flows, ['water_issue'])

    def test_absence_missing_evidence_and_separate_signals(self):
        decision = normalize_response(self.decision({}), self.request, self.refs, policy=DecisionPolicy())
        self.assertEqual(decision.outcome, 'unrecognized')
        request = replace(self.request, greeting_available=True, human_attention_available=True)
        response = self.decision({
            'greeting': self.classify('greeting', 'request', request=.85),
            'human_attention': self.classify('human_attention', 'absent', absent=1),
            'flow_5': self.classify('flow_5', 'request', .9),
        }, request=request)
        decision = normalize_response(response, request, self.refs, policy=DecisionPolicy())
        self.assertTrue(decision.ambiguous)
        self.assertTrue(decision.greeting_detected)
        self.assertFalse(decision.human_attention_requested)
        self.assertIsNone(response.results[-1].probabilities)
        self.assertIsNone(response.results[-3].confidence)

    def test_decision_is_complete_closed_and_correlated(self):
        valid = self.decision({})
        invalid = (
            replace(valid, request_id='other'),
            replace(valid, results=valid.results[1:]),
            replace(valid, results=(*valid.results[1:], self.classify('intruder', 'absent'))),
            ai.DecisionResponse(self.request.request_id, ai.DecisionOutcome.UNRECOGNIZED),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ai.InvalidInterpretation):
                ai.validate_decision_evidence(value, self.request)
        with self.assertRaises(ai.InvalidInterpretation):
            replace(valid, results=(*valid.results, valid.results[0]))
        for scores in ({'request': 2}, {'request': True}, {'request': float('nan')},
            {'request': .1, 'absent': .9}, {'request': .8, 'absent': .6}):
            with self.subTest(scores=scores), self.assertRaises(ai.InvalidInterpretation):
                self.classify('flow_0', 'request', **scores)
        with self.assertRaises(TypeError):
            ai.DecisionClassificationResult('flow_0', ai.DecisionClassification.REQUEST, action='execute')

    def test_extraction_preserves_exact_values_and_rejects_decision_evidence(self):
        parameters = tuple(ai.ExtractionParameter(ref, ref, kind, description='Texto de ejemplo') for ref, kind in
            (('quantity', ai.ParameterType.INT), ('paid', ai.ParameterType.BOOLEAN),
            ('amount', ai.ParameterType.DECIMAL), ('day', ai.ParameterType.DATE)))
        request = ai.ParameterExtractionRequest('extract', ai.Message('Valores de ejemplo'), 'es', parameters,
            context='Contexto autorizado')
        rows = tuple(ai.ExtractedParameter(spec.ref, ai.ExtractionStatus.EXTRACTED, (value,), evidence=('Fuente',))
            for spec, value in zip(parameters, (0, False, '000.12345678901234567890', '2026-11-19')))
        result = ai.ParameterExtractionResponse(request.request_id, rows)
        self.assertIs(ai.validate_extraction_response(result, request), result)
        for index, values in ((0, (True,)), (0, (1, 2)), (1, (0,)), (2, ('1e3',)), (2, (1.0,)), (3, ('20261119',))):
            with self.subTest(values=values), self.assertRaises(ai.InvalidInterpretation):
                altered = replace(rows[index], values=values)
                ai.validate_extraction_response(replace(result, parameters=(*rows[:index], altered, *rows[index+1:])), request)
        with self.assertRaises(ai.InvalidInterpretation):
            ai.validate_extraction_response(replace(result, parameters=rows[:-1]), request)
        with self.assertRaises(ai.InvalidInterpretation):
            ai.validate_extraction_response(self.decision({}), request)
        with self.assertRaises(ai.InvalidInterpretation):
            ai.validate_decision_evidence(result, self.request)
