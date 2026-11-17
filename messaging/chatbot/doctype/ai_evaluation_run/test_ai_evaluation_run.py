from messaging.chatbot.doctype.ai_evaluation_case_template.synchronization import register_evaluations
"""Focused evaluator contracts. Doubles always produce Test evidence, never provider approval."""
import copy
import json
import multiprocessing
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import patch

import frappe

from messaging.registrations import ai_providers as wire
from messaging.chatbot.doctype.ai_configuration import execution as ai_providers
from messaging.chatbot.doctype.ai_configuration import ai_configuration as configurations
from messaging.chatbot.doctype.ai_evaluation_case_template.test_ai_evaluation_case_template import messaging_evaluations, typed
from messaging.chatbot.doctype.ai_evaluation_case_template.test_ai_evaluation_case_template import materialized_catalog, insert_custom, insert_case
from messaging.chatbot.doctype.ai_evaluation_case_template import queries
from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import canonical, revision
from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import validate_case
from messaging.chatbot.doctype.ai_evaluation_run import runtime, dependencies, sampling, comparison
from messaging.chatbot.doctype.ai_evaluation_run.ai_evaluation_run import _writing
from messaging.common.primitives import utcnow


def request_worker(site, sites_path, configuration, barrier, results):
    frappe.init(site=site, sites_path=sites_path); frappe.connect(); frappe.set_user('Administrator')
    frappe.flags.in_test = True
    try:
        frappe.db.rollback()
        barrier.wait(timeout=15)
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import SYNTHETIC
        with patch.object(ai_providers, 'internal_provider', return_value=SYNTHETIC), patch.object(dependencies, 'internal_provider', return_value=SYNTHETIC):
            run = runtime._create(configuration)
        frappe.db.commit()
        results.put(('created', run.name))
    except Exception as exc:
        frappe.db.rollback()
        results.put((type(exc).__name__, None))
    finally:
        frappe.destroy()


class TestAIEvaluationRun(unittest.TestCase):
    def test_disabled_profile_is_a_controlled_rejection_without_enqueue(self):
        self.doc.enabled = 0; self.doc.save()
        profile = frappe.get_doc('AI Configuration', self.doc.name).as_dict()
        settings = frappe.get_doc('Bot Settings').as_dict()
        count = frappe.db.count('AI Evaluation Run')
        frappe.clear_last_message()
        with patch.object(frappe, 'enqueue') as enqueue, patch.object(ai_providers, 'run') as provider:
            with self.assertRaisesRegex(frappe.ValidationError, 'The selected AI profile is disabled'):
                runtime.request_evaluation(self.doc.name)
            enqueue.assert_not_called(); provider.assert_not_called()
        message = frappe.message_log[-1]
        self.assertEqual(message['title'], 'AI Profile Disabled')
        self.assertEqual(message['message'], 'The selected AI profile is disabled. Enable it before running an evaluation.')
        self.assertEqual(frappe.db.count('AI Evaluation Run'), count)
        self.assertEqual(frappe.get_doc('AI Configuration', self.doc.name).as_dict(), profile)
        self.assertEqual(frappe.get_doc('Bot Settings').as_dict(), settings)

    def test_request_translates_only_expected_configuration_rejections(self):
        for code in ('provider_disabled', 'configuration_incomplete', 'contract_invalid', 'extraction_inactive'):
            with self.subTest(code=code), patch.object(runtime, '_create', side_effect=ai_providers.AIServiceError(code)), patch.object(frappe, 'enqueue') as enqueue:
                with self.assertRaises(frappe.ValidationError) as error:
                    runtime.request_evaluation(self.doc.name)
                self.assertEqual(str(error.exception), configurations.DIAGNOSTICS[code])
                enqueue.assert_not_called()
        for exception in (RuntimeError('Unexpected failure'), ai_providers.AIServiceError('timeout'),
            ai_providers.AIServiceError('unrecognized_failure')):
            with patch.object(runtime, '_create', side_effect=exception), patch.object(frappe, 'enqueue') as enqueue:
                with self.assertRaises(type(exception)) as error:
                    runtime.request_evaluation(self.doc.name)
                self.assertIs(error.exception, exception)
                enqueue.assert_not_called()

    def setUp(self):
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import install_synthetic_provider
        frappe.set_user("Administrator")
        install_synthetic_provider(self)
        frappe.set_user('Administrator')
        frappe.local.request = None
        self.test_mode = patch.object(frappe, 'in_test', True)
        self.test_mode.start()
        self.addCleanup(self.test_mode.stop)
        from messaging.chatbot.doctype.bot_settings.test_bot_settings import configure_test_settings, restore_bot_settings
        original_settings = frappe.get_single('Bot Settings').as_dict()
        enabled_flows = frappe.get_all('Bot Flow', filters={'status':'Enabled'}, pluck='name')
        self.addCleanup(restore_bot_settings, original_settings, enabled_flows)
        if enabled_flows:
            frappe.db.set_value('Bot Flow', {'name':['in',enabled_flows]}, 'status','Disabled',update_modified=False)
        frappe.db.set_single_value('Bot Settings', {'interpretation_enabled':0,'active_ai_configuration':''}, update_modified=False)
        configure_test_settings()
        self.prefix = 'eval_' + frappe.generate_hash(length=10)
        self.catalog = materialized_catalog()
        self.answers = {r['input_data']['message']['text']: r['expected_data'] for r in self.catalog[0]['cases']}
        self.doc = self.configuration()

    def tearDown(self):
        frappe.db.rollback()
        frappe.local.request = None
        frappe.set_user('Administrator')

    def configuration(self, purpose='Decision'):
        provider = 'synthetic'
        if purpose != 'Decision':
            provider = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix + '_extract',
                provider_name='Synthetic extraction', decision=0, parameter_extraction=1, enabled=1, schema_version='1')).insert().name
        doc = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix + '_' + purpose.replace(' ', ''),
            provider=provider, enabled=1, profile_type=purpose))
        if purpose == 'Decision':
            doc.append('parameters', dict(parameter_key='api_key', secret_value='synthetic-evaluation-secret'))
        with patch.object(configurations, 'run') as network:
            doc.insert()
            network.assert_not_called()
        return doc

    def response(self, provider, values, request):
        return dict(schema_version='1', request_id=request.request_id,
            decision={k:v for k,v in dict(self.answers[request.message.text], confidence=.99).items() if k!='ambiguous'})

    def execute(self, run=None, response=None):
        run = run or runtime._create(self.doc.name, seed='reproducible')
        with patch.object(ai_providers, 'run', side_effect=response or self.response), patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'):
            runtime.execute(run.name)
        return run.reload()

    def optional(self, *, mandatory=False, scope='General', application=None):
        from messaging.chatbot.doctype.ai_evaluation_case_template.test_ai_evaluation_case_template import declaration
        raw = declaration('custom.' + self.prefix)
        raw.update(mandatory=int(mandatory), scope=scope, applicable_application=application)
        doc = insert_custom(raw)
        fixture = insert_case(doc, raw['cases'][0])
        return doc, fixture

    def test_context_uses_enabled_languages_and_verified_process_owners(self):
        settings = frappe.get_single('Bot Settings')
        context = dependencies.operational_context(settings)
        self.assertEqual(context['languages'], ['es'])
        self.assertEqual(context['applications'], [])
        settings.append('chatbot_languages', dict(language='en', enabled=1))
        self.assertEqual(dependencies.operational_context(settings)['languages'], ['en', 'es'])
        settings.chatbot_languages[-1].enabled = 0
        self.assertEqual(dependencies.operational_context(settings)['languages'], ['es'])
        with patch.object(frappe, 'get_all', wraps=frappe.get_all) as reads:
            dependencies.operational_context(settings)
            self.assertTrue(any(c.args[0] == 'Process Definition' for c in reads.call_args_list))

    def test_additive_resolution_optional_and_application_scope(self):
        optional, _fixture = self.optional()
        plan = runtime._plan(self.doc)
        self.assertEqual(len(plan['templates']), 16)
        self.assertEqual(len(runtime._plan(self.doc, [optional.name])['templates']), 17)
        optional.scope = 'Application'; optional.applicable_application = 'frappe'; optional.mandatory = 1; optional.save()
        self.assertNotIn(optional.name, [r['name'] for r in runtime._plan(self.doc)['templates']])
        context = dependencies.operational_context(); context['applications'] = ['frappe']
        self.assertIn(optional.name, [r.name for r in dependencies.applicable(self.doc, context)])
        optional.scope = 'Provider'; optional.applicable_application = ''; optional.provider = 'synthetic'; optional.save()
        self.assertIn(optional.name, [r['name'] for r in runtime._plan(self.doc)['templates']])

    def test_untrusted_optional_or_application_cannot_expand_context(self):
        for invalid in (['messaging.decision'], ['missing'], ['a', 'a'], {'application': 'foreign_app'}):
            with self.subTest(invalid=invalid), self.assertRaises(frappe.ValidationError):
                runtime._plan(self.doc, invalid)
        with self.assertRaises(TypeError):
            runtime.request_evaluation(self.doc.name, applications=['foreign_app'])

    def test_exact_stratified_selection_manifest_and_snapshots(self):
        run = runtime._create(self.doc.name, seed='seed_a')
        languages = set(dependencies.operational_context()['languages'])
        expected = sum(r['sample_count'] for r in self.catalog[0]['coverage'] if r['language'] in languages)
        self.assertEqual((run.selected_count, run.required_count), (expected, expected))
        results = runtime._results(run.name)
        self.assertEqual(len({r.evaluation_case for r in results}), expected)
        self.assertEqual({r.group for r in results}, {r['group'] for r in self.catalog[0]['coverage']})
        self.assertEqual({r.language for r in results}, languages)
        manifest = json.loads(run.manifest)
        expected_revision = manifest.pop('manifest_revision')
        self.assertEqual(revision(manifest), expected_revision)
        self.assertEqual(manifest['algorithm'], sampling.ALGORITHM)
        self.assertTrue(all(r.case_revision and r.template_revision and r.input_data and r.expected_data for r in results))

    def test_sampling_pagination_volume_reproducibility_without_payloads(self):
        rows = [dict(name=f'{i:05}', case_id=f'case_{i}', case_template='synthetic', definition_revision='rev') for i in range(2301)]
        def page(*args, start, page_length, **kwargs):
            self.assertEqual(page_length, 500)
            self.assertFalse(kwargs.get('include_data', False))
            return rows[start:start + page_length]
        with patch.object(queries, 'count_cases', return_value=len(rows)), patch.object(queries, 'list_cases', side_effect=page):
            one = sampling.select_cases('synthetic', 'es', 'group', 20, 'same')
            two = sampling.select_cases('synthetic', 'es', 'group', 20, 'same')
            three = sampling.select_cases('synthetic', 'es', 'group', 20, 'different')
        self.assertEqual(one, two)
        self.assertEqual(len({r['name'] for r in one}), 20)
        self.assertNotEqual(one, three)

    def test_insufficient_coverage_blocks_before_calls(self):
        optional, _case = self.optional(mandatory=True)
        optional.coverage[0].sample_count = 2; optional.save()
        with patch.object(ai_providers, 'run') as call:
            with self.assertRaises(frappe.ValidationError) as error:
                runtime._create(self.doc.name)
            call.assert_not_called()
        self.assertIn(optional.name + '/es/greeting: 1/2', str(error.exception))

    def test_missing_operational_language_is_explicit(self):
        settings = frappe.get_single('Bot Settings')
        settings.append('chatbot_languages', dict(language='fr', enabled=1))
        with patch.object(dependencies, 'operational_context', return_value=dict(kind='Bot Settings', languages=['es', 'fr'], applications=[])):
            plan = runtime._plan(self.doc)
            self.assertTrue(any(r['language'] == 'fr' for r in plan['insufficient']))
            with self.assertRaises(frappe.ValidationError): runtime._create(self.doc.name)

    def test_limits_and_seed_validation(self):
        for seed in ('x' * 65, 'semilla española', '../seed', 123):
            with self.subTest(seed=seed), self.assertRaises(frappe.ValidationError): runtime._plan(self.doc, seed=seed)
        with patch.dict(dependencies.POLICY, max_calls=1):
            with self.assertRaises(frappe.ValidationError): runtime._plan(self.doc)

    def test_pending_request_serialization_and_no_execution_on_registration(self):
        run = runtime._create(self.doc.name)
        with self.assertRaises(frappe.ValidationError): runtime._create(self.doc.name)
        self.assertEqual(run.status, 'Pending')
        with patch.object(frappe, 'enqueue') as enqueue:
            runtime.interrupt(run.name)
            result = runtime.request_evaluation(self.doc.name)
            self.assertEqual(result['status'], 'Pending')
            self.assertTrue(enqueue.call_args.kwargs['enqueue_after_commit'])

    def test_correct_decision_executes_all_and_mocks_never_approve(self):
        run = self.execute()
        self.assertEqual((run.status, run.call_count, run.executed_count, run.passed_count), ('Passed', 16, 16, 16))
        self.assertEqual(run.evidence_kind, 'Test')
        self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_test')
        for result in runtime._results(run.name):
            self.assertEqual((result.status, result.technical_valid, result.functional_valid), ('Passed', 1, 1))
            self.assertTrue(result.wire_request and result.raw_response and result.normalized_response)

    def test_every_declared_decision_scenario_and_order(self):
        for case in self.catalog[0]['cases']:
            request, refs = comparison.decision_request(case['input_data'], case['language'])
            raw = dict(schema_version='1', request_id=request.request_id, decision={k:v for k,v in dict(case['expected_data'], confidence=.99).items() if k!='ambiguous'})
            normalized, differences = comparison.compare_decision(raw, request, refs, case['expected_data'], .6)
            self.assertEqual(differences, [], case['case_id'])
            self.assertEqual(normalized, case['expected_data'])
            if len(case['expected_data']['flows']) > 1:
                raw['decision']['flows'] = list(reversed(raw['decision']['flows']))
                normalized, differences = comparison.compare_decision(raw, request, refs, case['expected_data'], .6)
                self.assertEqual(differences, [])
                self.assertEqual(normalized['flows'], list(reversed(case['expected_data']['flows'])))
                raw['decision']['flows'] = case['expected_data']['flows'][:1]
                self.assertEqual(comparison.compare_decision(raw, request, refs, case['expected_data'], .6)[1][0]['reason'], 'Selected Flows differ')
                raw['decision']['excluded_flows'] = list(reversed(case['expected_data']['flows']))
                expected = dict(case['expected_data'], excluded_flows=list(case['expected_data']['flows']), flows=[])
                raw['decision']['flows'] = []
                raw['decision']['outcome'] = 'recognized'
                normalized, differences = comparison.compare_decision(raw, request, refs, expected, .6)
                self.assertEqual(differences, [])
                self.assertEqual(normalized['excluded_flows'], list(reversed(expected['excluded_flows'])))

    def test_wrong_functional_response_fails_with_specific_differences(self):
        def wrong(provider, values, request):
            result = self.response(provider, values, request)
            result['decision'] = dict(flows=[], excluded_flows=[], greeting_detected=False, human_attention_requested=False, outcome='ambiguous', confidence=.99)
            return result
        run = self.execute(response=wrong)
        self.assertEqual((run.status, run.policy_version), ('Passed', 'technical-required-v1'))
        self.assertTrue(any(r.technical_valid and not r.functional_valid and 'differs' in r.diagnostic for r in runtime._results(run.name)))
        quality = runtime._quality(run)
        self.assertEqual((quality['technical']['valid'], quality['technical']['invalid']), (16, 0))
        self.assertEqual(quality['functional']['not_applicable'], 2)
        self.assertGreater(quality['functional']['differed'], 0)
        self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_test')

    def test_malformed_missing_type_and_unknown_fields_fail_technical_validation(self):
        source = self.catalog[0]['cases'][0]
        request, refs = comparison.decision_request(source['input_data'], 'es')
        good = dict(schema_version='1', request_id=request.request_id, decision={k:v for k,v in dict(source['expected_data'], confidence=.99).items() if k!='ambiguous'})
        bad = [b'not-json', {}, dict(good, decision={}), dict(good, decision=dict(good['decision'], greeting=False)),
            dict(good, decision=dict(good['decision'], parameters={'secret': 'value'}))]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(wire.InvalidInterpretation):
                comparison.compare_decision(raw, request, refs, source['expected_data'], .6)
        run = self.execute(response=lambda *args: b'not-json')
        self.assertEqual(run.status, 'Failed')
        self.assertTrue(all(not r.technical_valid for r in runtime._results(run.name)))

    def test_provider_errors_timeout_and_rate_limit_do_not_retry(self):
        for code in ('timeout', 'authentication_error', 'rate_limited', 'unavailable', 'contract_invalid'):
            with self.subTest(code=code):
                run = runtime._create(self.doc.name)
                with patch.object(ai_providers, 'run', side_effect=ai_providers.AIServiceError(code)) as call, patch.object(frappe.db, 'commit'), patch.object(frappe.db, 'rollback'):
                    runtime.execute(run.name)
                run.reload()
                self.assertNotEqual(run.status, 'Passed')
                self.assertEqual(call.call_count, run.call_count)
                self.assertEqual(call.call_count, 1 if code in ('authentication_error', 'rate_limited') else 16)
                self.assertTrue(all(r.attempt_count <= 1 for r in runtime._results(run.name)))

    def test_optional_failure_does_not_block_mandatory_results(self):
        optional, fixture = self.optional()
        fixture.input_text = 'Unique optional fixture'; fixture.save()
        run = runtime._create(self.doc.name, [optional.name])
        def response(provider, values, request):
            if request.message.text == 'Unique optional fixture':
                return dict(schema_version='1', request_id=request.request_id, decision=dict(
                    flows=[], excluded_flows=[], greeting_detected=False, human_attention_requested=False, outcome='ambiguous', confidence=.99))
            return self.response(provider, values, request)
        run = self.execute(run, response)
        summary = json.loads(run.summary)
        self.assertEqual(run.status, 'Passed')
        self.assertEqual(summary[optional.name]['status'], 'Passed')
        self.assertEqual(summary[optional.name]['functional_differed'], 1)
        self.assertTrue(all(row['status']=='Passed' for name,row in summary.items() if name != optional.name))

    def test_interruption_preserves_completed_samples_and_no_replay(self):
        run = runtime._create(self.doc.name)
        count = 0
        def stop(provider, values, request):
            nonlocal count
            count += 1
            if count == 3: raise KeyboardInterrupt()
            return self.response(provider, values, request)
        with self.assertRaises(KeyboardInterrupt): self.execute(run, stop)
        run.reload()
        self.assertEqual(run.status, 'Incomplete')
        self.assertEqual(sum(r.status == 'Passed' for r in runtime._results(run.name)), 2)
        with patch.object(ai_providers, 'run') as call:
            runtime.execute(run.name); call.assert_not_called()

    def test_global_deadline_retains_incomplete_state(self):
        run = runtime._create(self.doc.name)
        ticks = iter([0, 899])
        with patch.object(runtime, 'time', SimpleNamespace(monotonic=lambda: next(ticks))), self.assertRaises(TimeoutError):
            self.execute(run)
        run.reload()
        self.assertEqual(run.status, 'Incomplete')
        self.assertEqual(run.call_count, 0)

    def test_active_worker_cannot_be_marked_interrupted_early(self):
        run = runtime._create(self.doc.name)
        runtime._write(run, status='Running', deadline_at=utcnow() + timedelta(seconds=900))
        with self.assertRaises(frappe.ValidationError): runtime.interrupt(run.name)
        runtime._write(run, deadline_at=utcnow() - timedelta(seconds=61))
        self.assertEqual(runtime.interrupt(run.name)['status'], 'Incomplete')

    def test_model_parameter_secret_and_revert_invalidate_irreversibly(self):
        run = self.execute()
        generation = self.doc.evaluation_generation
        model = next(r for r in self.doc.parameters if r.parameter_key == 'model')
        original = model.value; model.value = '"synthetic-other"'; self.doc.save(); run.reload()
        self.assertIsNotNone(run.invalidated_at)
        self.assertNotEqual(self.doc.evaluation_generation, generation)
        model.value = original; self.doc.save(); run.reload()
        self.assertIsNotNone(run.invalidated_at)
        self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_invalidated')
        row = next(r for r in self.doc.parameters if r.parameter_key == 'api_key'); row.secret_value = 'replacement-secret'
        before = self.doc.evaluation_generation; self.doc.save()
        self.assertNotEqual(before, self.doc.evaluation_generation)
        self.assertNotIn('replacement-secret', self.doc.as_json())

    def test_irrelevant_provider_description_does_not_invalidate(self):
        provider = configurations.provider_for(self.doc)
        values = configurations.configuration_values(self.doc, provider)
        before = configurations.fingerprint(provider, values, behavior_only=True)
        provider.description = 'Description changed'
        provider.parameters[0].description = 'Administrative note'
        self.assertEqual(before, configurations.fingerprint(provider, values, behavior_only=True))
        changed = dependencies.template_dependency(frappe.get_doc('AI Evaluation Case Template', messaging_evaluations()[0]['template_id']))
        evaluation = frappe.get_doc('AI Evaluation Case Template', messaging_evaluations()[0]['template_id']); evaluation.description = 'Administrative'
        self.assertEqual(changed, dependencies.template_dependency(evaluation))

    def test_catalog_change_then_revert_keeps_old_evidence_invalid(self):
        optional, fixture = self.optional(mandatory=True)
        run = self.execute(runtime._create(self.doc.name))
        original = fixture.input_text
        fixture.input_text += ' updated'; fixture.save()
        run.reload(); self.assertIsNotNone(run.invalidated_at)
        fixture.input_text = original; fixture.save(); run.reload()
        self.assertIsNotNone(run.invalidated_at)

    def test_adapter_policy_language_and_application_scope_changes_invalidate(self):
        run = self.execute()
        self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_test')
        with patch.object(dependencies, 'implementation_revision', return_value='different'):
            self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_invalidated')
        run.reload(); self.assertIsNotNone(run.invalidated_at)
        settings = frappe.get_single('Bot Settings'); settings.append('chatbot_languages', dict(language='en', enabled=1))
        state = runtime.approval(self.doc, settings=settings)
        self.assertEqual(state['status'], 'evaluation_context')

    def test_configuration_change_during_call_cannot_approve(self):
        changed = False
        def response(provider, values, request):
            nonlocal changed
            if not changed:
                changed = True
                doc = frappe.get_doc('AI Configuration', self.doc.name)
                next(r for r in doc.parameters if r.parameter_key == 'model').value = '"synthetic-changed"'; doc.save()
            return self.response(provider, values, request)
        run = self.execute(response=response)
        self.assertEqual(run.status, 'Incomplete')
        self.assertTrue(run.invalidated_at)
        self.assertEqual(run.call_count, 1)

    def test_reproduction_uses_saved_selection_and_rejects_obsolete_dependencies(self):
        one = self.execute()
        two = runtime._create(self.doc.name, seed='ignored', reproduces=one.name)
        self.assertEqual(one.seed, two.seed)
        self.assertEqual([r.evaluation_case for r in runtime._results(one.name)], [r.evaluation_case for r in runtime._results(two.name)])
        self.assertEqual([r.expected_data for r in runtime._results(one.name)], [r.expected_data for r in runtime._results(two.name)])
        runtime.interrupt(two.name)
        next(r for r in self.doc.parameters if r.parameter_key == 'model').value = '"synthetic-new"'; self.doc.save()
        with self.assertRaises(frappe.ValidationError): runtime._create(self.doc.name, reproduces=one.name)

    def test_extraction_prepares_samples_without_executor_or_approval(self):
        doc = self.configuration('Parameter Extraction')
        with patch.object(ai_providers, 'run') as call:
            run = runtime.request_evaluation(doc.name, seed='extraction')
            call.assert_not_called()
        self.assertEqual(run['status'], 'Unavailable')
        results = runtime._results(run['name'])
        self.assertEqual(len(results), 25)
        self.assertEqual({r.group for r in results}, {'String', 'Int', 'Date', 'Name', 'Expression'})
        self.assertTrue(all(r.status == 'Unavailable' and not r.attempt_count for r in results))
        self.assertTrue(all(row['status']=='Unavailable' for row in json.loads(frappe.get_doc('AI Evaluation Run', run['name']).summary).values()))
        self.assertFalse(runtime.approval(doc)['approved'])

    def test_extraction_native_comparison_all_fixtures_and_invalid_values(self):
        for case in self.catalog[1]['cases']:
            self.assertEqual(comparison.compare_extraction(case['input_data'], case['group'], case['expected_data'], case['expected_data']), [])
        multiple = next(r for r in self.catalog[1]['cases'] if any(len(item.get('values') or []) > 1 for item in r['expected_data']['parameters'].values()))
        changed = copy.deepcopy(multiple['expected_data'])
        ref = next(key for key, item in changed['parameters'].items() if len(item['values']) > 1)
        changed['parameters'][ref]['values'] = list(reversed(changed['parameters'][ref]['values']))
        self.assertTrue(comparison.compare_extraction(multiple['input_data'], multiple['group'], changed, multiple['expected_data']))
        case = next(r for r in self.catalog[1]['cases'] if r['group'] == 'Int')
        for invalid in ({'status': 'EXTRACTED', 'values': ['12']}, {'status': 'EXTRACTED', 'values': [True]},
            {'status': 'NOT_FOUND', 'values': [12]}, {'status': 'EXTRACTED', 'values': [12, 13]}):
            with self.subTest(invalid=invalid), self.assertRaises(frappe.ValidationError):
                comparison.compare_extraction(case['input_data'], 'Int', {'parameters': {'int': invalid}}, case['expected_data'])

    def test_evidence_protection_permissions_and_secret_redaction(self):
        run = runtime._create(self.doc.name)
        result = runtime._results(run.name)[0]
        for doc in (run, result):
            doc.status = 'Passed'
            with self.assertRaises(frappe.PermissionError): doc.save(ignore_permissions=True)
            with self.assertRaises(frappe.PermissionError): frappe.delete_doc(doc.doctype, doc.name, ignore_permissions=True)
        raw = '{"authorization":"Bearer private-token","api_key":"different-secret","echo":"synthetic-evaluation-secret"}'
        safe = comparison.protected_response(raw, ['synthetic-evaluation-secret'])
        for secret in ('private-token', 'different-secret', 'synthetic-evaluation-secret'): self.assertNotIn(secret, safe)
        self.assertLessEqual(len(comparison.protected_response('a' * 300000, [])), 250000)
        frappe.set_user('Guest')
        for fn, args in ((runtime.preview, (self.doc.name,)), (runtime.request_evaluation, (self.doc.name,)),
            (runtime.approval_status, (self.doc.name,)), (runtime.progress, (run.name,)), (runtime.interrupt, (run.name,))):
            with self.subTest(fn=fn.__name__), self.assertRaises(frappe.PermissionError): fn(*args)

    def test_activation_gate_preserves_existing_selection_and_precise_reason(self):
        provider = configurations.provider_for(self.doc)
        values = configurations.configuration_values(self.doc, provider)
        configurations.diagnose(self.doc, 'operational', configurations.fingerprint(provider, values), checked=True)
        with self.assertRaises(ai_providers.AIServiceError) as error: configurations.resolve_configuration(self.doc.name)
        self.assertEqual(error.exception.code, 'evaluation_missing')
        settings = frappe.get_single('Bot Settings'); settings.active_ai_configuration = self.doc.name
        with self.assertRaises(frappe.ValidationError) as error: settings.validate_ai()
        self.assertIn('technical evaluation', str(error.exception))
        self.assertEqual(frappe.db.get_single_value('Bot Settings', 'active_ai_configuration'), '')

    def test_real_evidence_eligibility_is_derived_without_persisting_fake_approval(self):
        run = self.execute()
        # Only an in-memory copy represents a real result here. No Real run is written.
        real = copy.deepcopy(run); real.evidence_kind = 'Real'
        original = frappe.get_doc
        def get_doc(*args, **kwargs):
            if args == ('AI Evaluation Run', run.name): return real
            return original(*args, **kwargs)
        with patch.object(frappe, 'get_doc', side_effect=get_doc):
            self.assertTrue(runtime.approval(self.doc)['approved'])
            real.policy_version = 'all-required-v1'
            self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_policy')
            self.assertFalse(runtime.approval(self.doc)['approved'])
            # The obsolete policy invalidates the stored row. Restore only the in-memory copy
            # so the next derived status is the current policy's running state.
            real.policy_version = dependencies.POLICY['version']
            real.invalidated_at = None
            real.invalidation_reason = None
            real.status = 'Running'
            self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_running')
        self.assertIsNotNone(frappe.db.get_value(run.doctype, run.name, 'invalidated_at'))
        self.assertEqual(frappe.db.get_value(run.doctype, run.name, 'evidence_kind'), 'Test')

    def test_concurrent_requests_use_real_database_current_reads(self):
        # Only this fixture is committed, and its evidence remains Test throughout.
        frappe.db.commit()
        names = []
        try:
            context = multiprocessing.get_context('spawn')
            barrier, results = context.Barrier(2), context.Queue()
            processes = [context.Process(target=request_worker, args=(frappe.local.site,
                str(Path(frappe.get_site_path()).parent), self.doc.name, barrier, results)) for _ in range(2)]
            for process in processes: process.start()
            for process in processes:
                process.join(25)
                self.assertFalse(process.is_alive())
            states = [results.get(timeout=5) for _ in processes]
            names = [name for state, name in states if name]
            self.assertEqual(sum(state == 'created' for state, _name in states), 1, states)
            self.assertEqual(sum(state in ('ValidationError', 'QueryDeadlockError') for state, _name in states), 1, states)
        finally:
            frappe.db.rollback()
            names = frappe.get_all('AI Evaluation Run', filters={'configuration': self.doc.name}, pluck='name')
            for name in names:
                frappe.db.delete('AI Evaluation Result', {'evaluation_run': name})
                frappe.db.delete('AI Evaluation Run', {'name': name})
            frappe.delete_doc('AI Configuration', self.doc.name, force=True)
            frappe.db.commit()

    def test_https_adapter_durable_evidence_and_invalid_raw_response(self):
        answers = self.answers
        class Handler(BaseHTTPRequestHandler):
            malformed = False
            def log_message(self, *args): pass
            def do_POST(self):
                request = wire.strict_json(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                body = b'malformed synthetic-evaluation-secret' if self.malformed else wire.serialize(dict(
                    schema_version='1', request_id=request['request_id'], decision={k:v for k,v in dict(answers[request['message']['text']], confidence=.99).items() if k!='ambiguous'}))
                self.wfile.write(body)
        provider = frappe.get_doc(dict(doctype='AI Provider', provider_id=self.prefix + '_https',
            provider_name='Synthetic HTTPS evaluator', enabled=1, schema_version='1')).insert()
        profile = None
        with tempfile.TemporaryDirectory() as folder:
            cert, key = Path(folder) / 'cert.pem', Path(folder) / 'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key),
                '-out', str(cert), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert, key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                profile = frappe.get_doc(dict(doctype='AI Configuration', configuration_name=self.prefix + '_https',
                    provider=provider.name, enabled=1))
                for name, value in dict(endpoint=f'https://localhost:{server.server_port}/decision', auth_type='Bearer').items():
                    profile.append('parameters', dict(parameter_key=name, value=json.dumps(value)))
                profile.append('parameters', dict(parameter_key='auth_secret', secret_value='synthetic-evaluation-secret'))
                profile.insert()
                with patch.dict(frappe.conf, {'messaging_ai_allowed_hosts': ['localhost']}), patch.object(ai_providers.requests.certs, 'where', return_value=str(cert)):
                    run = runtime._create(profile.name)
                    frappe.db.commit()
                    runtime.execute(run.name)
                    frappe.db.rollback(); run.reload()
                    self.assertEqual((run.status, run.call_count, run.evidence_kind), ('Passed', 16, 'Test'))
                    self.assertFalse(runtime.approval(profile)['approved'])
                    for result in runtime._results(run.name):
                        self.assertTrue(json.loads(result.request_log_ids))
                        self.assertNotIn('synthetic-evaluation-secret', result.raw_response)
                    Handler.malformed = True
                    failed = runtime._create(profile.name); frappe.db.commit(); runtime.execute(failed.name)
                    failed.reload(); self.assertEqual(failed.status, 'Failed')
                    result = runtime._results(failed.name)[0]
                    self.assertIn('malformed', result.raw_response)
                    self.assertNotIn('synthetic-evaluation-secret', result.raw_response)
                    self.assertIn('Invalid interpretation JSON', result.diagnostic)
            finally:
                server.shutdown(); server.server_close(); thread.join(3)
                frappe.db.rollback()
                if profile:
                    for run_name in frappe.get_all('AI Evaluation Run', filters={'configuration': profile.name}, pluck='name'):
                        frappe.db.delete('AI Evaluation Result', {'evaluation_run': run_name})
                        frappe.db.delete('AI Evaluation Run', {'name': run_name})
                    for row in frappe.get_all('AI Request Log', filters={'configuration': profile.name}, fields=['name', 'error_log']):
                        if row.error_log: frappe.db.delete('Error Log', {'name': row.error_log})
                        frappe.db.delete('AI Request Log', {'name': row.name})
                    if frappe.db.exists('AI Configuration', profile.name): frappe.delete_doc('AI Configuration', profile.name, force=True)
                frappe.delete_doc('AI Provider', provider.name, force=True)
                if frappe.db.exists('AI Configuration', self.doc.name): frappe.delete_doc('AI Configuration', self.doc.name, force=True)
                frappe.db.commit()

    def test_uncovered_language_and_description_changes_preserve_current_scope(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import EvaluationTemplateDefinition
        run = self.execute()
        definition = copy.deepcopy(messaging_evaluations()[0])
        next(r for r in definition['cases'] if r['language'] == 'en')['input_text'] += ' updated'
        definition['description'] = 'An administrative note'
        register_evaluations(app='messaging', evaluations=[typed(definition)])
        run.reload()
        self.assertFalse(run.invalidated_at)
        self.assertEqual(runtime.approval(self.doc)['status'], 'evaluation_test')
        next(r for r in definition['cases'] if r['language'] == 'es')['input_text'] += ' updated'
        register_evaluations(app='messaging', evaluations=[typed(definition)])
        run.reload()
        self.assertTrue(run.invalidated_at)

    def test_new_mandatory_evaluation_and_coverage_change_invalidate(self):
        run = self.execute()
        extra, _fixture = self.optional(mandatory=True)
        run.reload(); self.assertTrue(run.invalidated_at)
        next_run = self.execute(runtime._create(self.doc.name))
        extra.coverage[0].sample_count = 2; extra.save()
        next_run.reload(); self.assertTrue(next_run.invalidated_at)

    def test_loaded_policy_mismatch_stops_before_network(self):
        run = runtime._create(self.doc.name)
        with patch.object(sampling, 'ALGORITHM', 'changed-loaded-code'), patch.object(ai_providers, 'run') as call, patch.object(frappe.db, 'commit'):
            runtime.execute(run.name)
            call.assert_not_called()
        run.reload()
        self.assertEqual(run.status, 'Incomplete')
        self.assertTrue(all(r.status == 'Incomplete' for r in runtime._results(run.name)))

    def test_optional_missing_language_remains_incomplete_without_blocking(self):
        optional, _fixture = self.optional()
        context = dict(kind='Bot Settings', languages=['en', 'es'], applications=[])
        with patch.object(dependencies, 'operational_context', return_value=context):
            plan = runtime._plan(self.doc, [optional.name])
            self.assertTrue(any(r['language'] == 'en' and r['mandatory'] is False for r in plan['insufficient']))
            run = self.execute(runtime._create(self.doc.name, [optional.name]))
        self.assertEqual(run.status, 'Passed')
        self.assertEqual(json.loads(run.summary)[optional.name]['status'], 'Incomplete')

    def test_template_change_invalidates_irreversibly_and_preserves_historical_snapshots(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import EvaluationTemplateDefinition
        run = self.execute()
        original = [(r.input_data, r.expected_data, r.template_revision, r.localized_revision) for r in runtime._results(run.name)]
        declaration = copy.deepcopy(messaging_evaluations()[0])
        declaration['greeting_available'] = True
        register_evaluations(app='messaging', evaluations=[typed(declaration)])
        run.reload();self.assertTrue(run.invalidated_at)
        self.assertEqual(original, [(r.input_data, r.expected_data, r.template_revision, r.localized_revision) for r in runtime._results(run.name)])
        declaration['greeting_available'] = False
        register_evaluations(app='messaging', evaluations=[typed(declaration)])
        run.reload();self.assertTrue(run.invalidated_at)

    def test_multiparameter_coverage_counts_one_sample_without_executor_or_approval(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.test_ai_evaluation_case_template import TestAIEvaluationCaseTemplate
        raw = TestAIEvaluationCaseTemplate.multiparameter(self)
        raw['coverage'].append(dict(language='es', group='Expression', minimum_parameters=3, sample_count=1, required=True))
        policy = insert_custom(raw);insert_case(policy, raw['cases'][0])
        configuration = self.configuration('Parameter Extraction')
        with patch.object(ai_providers, 'run') as network:
            result = runtime.request_evaluation(configuration.name, seed='combined')
            network.assert_not_called()
        run = frappe.get_doc('AI Evaluation Run', result['name'])
        summary = json.loads(run.summary)[policy.name]
        self.assertEqual((summary['required'], summary['coverage_required'], summary['selected']), (1,4,1))
        self.assertEqual((run.selected_count, run.required_count, run.call_count), (26,26,0))
        self.assertEqual(run.status,'Unavailable');self.assertFalse(runtime.approval(configuration)['approved'])
        self.assertRegex(run.name, r'^EVR-[0-9]{5}$')
        results = runtime._results(run.name)
        self.assertTrue(results)
        for row in results:
            self.assertRegex(row.name, r'^EVL-[0-9]{6}$')
            self.assertEqual(row.evaluation_run, run.name)
        legacy_runs = frappe.get_all('AI Evaluation Run', filters={'name': ['not like', 'EVR-%']}, pluck='name', limit=1)
        if legacy_runs:
            self.assertTrue(frappe.get_doc('AI Evaluation Run', legacy_runs[0]).name)
