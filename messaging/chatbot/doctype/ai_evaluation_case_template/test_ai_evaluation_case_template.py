from messaging.chatbot.doctype.ai_evaluation_case_template.synchronization import register_evaluations
"""Direct functional-type templates, native cases and owner registration contracts."""
import copy
import json
import unittest
from unittest.mock import patch

import frappe
from werkzeug.wrappers import Request

from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import EvaluationTemplateDefinition
from messaging.presets.evaluations import definitions as native_evaluations
from messaging.presets import register as defaults
from messaging.chatbot.doctype.ai_evaluation_case_template import configuration as declarations
from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import default_case_identity


def messaging_evaluations():
    result = []
    for definition in native_evaluations():
        raw = definition.initial_values()
        for case in raw['cases']:
            case['case_id'] = default_case_identity(definition.key, case['language'])
        result.append(raw)
    return result


def typed(raw):
    data = copy.deepcopy(raw)
    data['key'], data['name'] = data.pop('template_id'), data.pop('template_name')
    rows = dict(coverage=declarations.EvaluationCoverage, parameters=declarations.EvaluationParameter,
        available_flows=declarations.EvaluationReference,
        previously_recognized_flows=declarations.EvaluationReference,
        flow_texts=declarations.EvaluationLocalizedText,
        flows=declarations.EvaluationReference,
        excluded_flows=declarations.EvaluationReference, expected_parameters=declarations.EvaluationExpectedParameter,
        expected_values=declarations.EvaluationExpectedValue)
    def convert(values):
        return {k: tuple(rows[k](**r) for r in v) if k in rows else v for k,v in values.items()}
    cases = {}
    for case in data.pop('cases'):
        language = case.pop('language')
        case.pop('case_id', None)
        if language in cases:
            raise frappe.ValidationError('Duplicate localized case language.')
        cases[language] = declarations.EvaluationCaseDefinition(**convert(case))
    return declarations.EvaluationTemplateDefinition(**convert(data), cases=cases)
from messaging.chatbot.doctype.ai_evaluation_case_template import queries
from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import materialize, validate_extraction


def declaration(identifier, *, purpose='Decision'):
    raw = copy.deepcopy(next(r for r in messaging_evaluations() if r['template_id'].endswith('greeting_hello' if purpose == 'Decision' else 'string_valid')))
    raw['template_id'] = identifier
    raw['cases'] = [next(c for c in raw['cases'] if c['language'] == 'es')]
    raw['cases'][0]['case_id'] = default_case_identity(identifier, raw['cases'][0]['language'])
    raw['coverage'] = [r for r in raw['coverage'] if r['language'] == raw['cases'][0]['language']]
    return raw


def insert_custom(raw):
    return frappe.get_doc(dict(doctype='AI Evaluation Case Template', **{k:v for k,v in raw.items() if k!='cases'})).insert()


def insert_case(template, raw):
    return frappe.get_doc(dict(doctype='AI Evaluation Case', case_template=template.name, **copy.deepcopy(raw))).insert()


def materialized_catalog():
    result = []
    for purpose in ('Decision','Parameter Extraction'):
        cases, coverage = [], []
        for raw in messaging_evaluations():
            if raw['profile_type'] != purpose:continue
            template=frappe.new_doc('AI Evaluation Case Template');template.update({k:v for k,v in raw.items() if k!='cases'})
            coverage.extend(raw['coverage'])
            for localized in raw['cases']:
                doc=frappe.new_doc('AI Evaluation Case');doc.update(localized)
                doc.group=template.group if purpose=='Decision' else template.parameters[0].parameter_type
                source,expected=materialize(doc,template)
                cases.append(dict(localized,case_template=raw['template_id'],group=doc.group,input_data=source,expected_data=expected))
        result.append(dict(profile_type=purpose,cases=cases,coverage=coverage))
    return result


class TestAIEvaluationCaseTemplate(unittest.TestCase):
    def setUp(self):
        frappe.set_user('Administrator');frappe.local.request=None
        self.mode=patch.object(frappe,'in_test',True);self.mode.start();self.addCleanup(self.mode.stop)
        self.prefix='test_'+frappe.generate_hash(length=10)

    def tearDown(self):
        frappe.local.request=None;frappe.db.rollback();frappe.set_user('Administrator')

    def register(self,raw=None):
        return register_evaluations(app='messaging',evaluations=[typed(raw or declaration('messaging.'+self.prefix))])

    def custom(self,purpose='Decision'):
        raw=declaration('custom.'+self.prefix,purpose=purpose)
        return insert_custom(raw),raw

    def test_no_container_direct_functional_select_and_retired_relations(self):
        self.assertFalse(frappe.db.exists('DocType','AI Evaluation'))
        self.assertFalse(frappe.db.table_exists('AI Evaluation'))
        field=frappe.get_meta('AI Evaluation Case Template').get_field('profile_type')
        self.assertEqual(field.fieldtype,'Select');self.assertFalse(field.read_only)
        self.assertEqual(field.options.splitlines(),['Decision','Parameter Extraction'])
        for name in ('AI Evaluation Case Template','AI Evaluation Case','AI Evaluation Result'):
            self.assertFalse(frappe.get_meta(name).has_field('evaluation'))
            self.assertNotIn('evaluation',frappe.db.get_table_columns(name))

    def test_full_core_idempotence_and_no_provider_calls(self):
        from dataclasses import replace
        definitions = tuple(replace(d, key='messaging.' + self.prefix + '_' + d.key.replace('.', '_'))
            for d in native_evaluations())
        counts = {dt: frappe.db.count(dt) for dt in ('Bot Flow', 'Notification Rule', 'AI Evaluation Run', 'AI Evaluation Result')}
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('No AI during registration')):
            first = register_evaluations(app='messaging', evaluations=definitions)
            before = frappe.get_all('AI Evaluation Case', filters={'case_template': ['in', [d.key for d in definitions]]},
                fields=['name', 'modified', 'definition_revision'], order_by='name')
            result = register_evaluations(app='messaging', evaluations=definitions)
        self.assertEqual({dt: frappe.db.count(dt) for dt in counts}, counts)
        self.assertEqual((first['created'], first['cases_created']), (41, 82))
        self.assertEqual((result['unchanged'], result['cases_unchanged']), (41, 82))
        self.assertEqual(before, frappe.get_all('AI Evaluation Case', filters={'case_template': ['in', [d.key for d in definitions]]},
            fields=['name', 'modified', 'definition_revision'], order_by='name'))

    def test_template_owns_identity_type_and_policy(self):
        self.register();doc=frappe.get_doc('AI Evaluation Case Template','messaging.'+self.prefix)
        self.assertEqual((doc.application,doc.origin,doc.profile_type),('messaging','Registered','Decision'))
        self.assertEqual(doc.name,doc.template_id)
        self.assertEqual(queries.count_cases(doc.name),1)
        self.assertTrue(queries.coverage_status(doc.name)[0]['sufficient'])

    def test_registered_documents_reject_save_delete_and_forgery(self):
        raw=declaration('messaging.'+self.prefix);self.register(raw)
        template=frappe.get_doc('AI Evaluation Case Template',raw['template_id']);case=queries.get_case(template.name,raw['cases'][0]['case_id'])
        for doc in (template,case):
            with self.assertRaises(frappe.PermissionError):doc.save(ignore_permissions=True)
            with self.assertRaises(frappe.PermissionError):doc.delete(ignore_permissions=True)
        forged=frappe.new_doc('AI Evaluation Case Template');forged.update(template.as_dict());forged.name='forged';forged.set('__islocal',True)
        with self.assertRaises(frappe.PermissionError):forged.insert(ignore_permissions=True)

    def test_custom_type_identity_and_owner_are_immutable(self):
        template,raw=self.custom()
        for key,value in [('profile_type','Parameter Extraction'),('template_id','custom.changed'),('application','frappe')]:
            doc=frappe.get_doc(template.doctype,template.name);doc.set(key,value)
            with self.assertRaises((frappe.ValidationError,frappe.PermissionError)):doc.save()

    def test_native_custom_materialization_and_delete_order(self):
        template,raw=self.custom();case=insert_case(template,raw['cases'][0]);revision=case.definition_revision
        case.input_text+='!';case.input_data='{}';case.expected_data='{}';case.save()
        self.assertEqual(json.loads(case.input_data)['message']['text'],case.input_text)
        self.assertNotEqual(case.definition_revision,revision)
        with self.assertRaises(frappe.ValidationError):template.delete()
        case.delete();template.delete()

    def test_namespaces_owner_http_and_guest(self):
        for identifier in ('frappe.foreign','custom.foreign'):
            with self.assertRaises(frappe.ValidationError):self.register(declaration(identifier))
        with self.assertRaises(frappe.PermissionError):register_evaluations(app='frappe',evaluations=[])
        frappe.local.request=Request.from_values()
        with self.assertRaises(frappe.PermissionError):self.register()
        frappe.local.request=None;frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError):self.register()

    def test_atomic_batch_and_complete_case_manifest(self):
        good=declaration('messaging.'+self.prefix);bad=declaration('messaging.'+self.prefix+'_bad');bad['cases'][0]['flows']=[dict(ref='missing')]
        with self.assertRaises(frappe.ValidationError):register_evaluations(app='messaging',evaluations=[typed(good),typed(bad)])
        self.assertFalse(frappe.db.exists('AI Evaluation Case Template',good['template_id']))
        good['cases'].append(dict(good['cases'][0],case_id='alternate',variant='alternate'))
        with self.assertRaises(frappe.ValidationError):self.register(good)

    def test_distinct_localized_and_technical_revisions(self):
        template,raw=self.custom();case=insert_case(template,raw['cases'][0]);before=(case.localized_revision,case.template_revision,case.definition_revision)
        template.set('previously_recognized_flows',[dict(ref='tracking')]);template.save();case.reload()
        self.assertEqual(case.localized_revision,before[0]);self.assertNotEqual(case.template_revision,before[1]);self.assertNotEqual(case.definition_revision,before[2])
        technical=case.template_revision;case.input_text+='!';case.save();self.assertEqual(case.template_revision,technical);self.assertNotEqual(case.localized_revision,before[0])

    def test_language_selectivity_and_template_reuse(self):
        from .ai_evaluation_case_template import case_manifest_revision
        raw=copy.deepcopy(messaging_evaluations()[0]);raw['template_id']='messaging.'+self.prefix;self.register(raw)
        before=case_manifest_revision(raw['template_id'],languages=['es'])
        next(c for c in raw['cases'] if c['language']=='en')['input_text']+='!';self.register(raw)
        self.assertEqual(case_manifest_revision(raw['template_id'],languages=['es']),before)
        self.assertEqual(queries.count_cases(raw['template_id']),2)

    def test_declaration_order_and_copy_are_not_material(self):
        raw=copy.deepcopy(messaging_evaluations()[0]);raw['template_id']='messaging.'+self.prefix;original=copy.deepcopy(raw);self.register(raw)
        self.assertEqual(raw,original);raw['cases'].reverse();raw['coverage'].reverse();raw['available_flows'].reverse()
        self.assertEqual(self.register(raw)['unchanged'],1)

    def test_invalid_coverage_groups_counts_languages_and_minimum(self):
        for change in ({'sample_count':0},{'required':False},{'language':'missing'},{'group':'other'},{'minimum_parameters':2}):
            raw=declaration('messaging.'+self.prefix);raw['coverage'][0].update(change)
            with self.subTest(change=change),self.assertRaises(frappe.ValidationError):self.register(raw)
        template,raw=self.custom();self.assertFalse(queries.coverage_status(template.name)[0]['sufficient'])
        insert_case(template,raw['cases'][0]);template.coverage[0].sample_count=2;template.save();self.assertFalse(queries.coverage_status(template.name)[0]['sufficient'])

    def test_decision_zero_flows_and_invalid_references_outcomes(self):
        raw=declaration('messaging.'+self.prefix)
        raw.update(available_flows=[])
        raw['cases'][0].update(flow_texts=[],outcome='empty_catalog',flows=[],excluded_flows=[])
        self.register(raw)
        case = queries.get_case(raw['template_id'], raw['cases'][0]['case_id'])
        self.assertEqual(json.loads(case.input_data)['available_flows'], [])
        expected = json.loads(case.expected_data)
        self.assertEqual(expected['outcome'], 'empty_catalog')
        self.assertEqual(expected['flows'], [])
        self.assertEqual(expected['excluded_flows'], [])
        for field in ('flows', 'excluded_flows'):
            bad = copy.deepcopy(raw)
            bad['template_id'] += '_bad_' + field
            bad['cases'][0][field] = [dict(ref='greeting')]
            with self.subTest(empty_catalog_reference=field), self.assertRaisesRegex(frappe.ValidationError, 'Expected Decision contradicts the AI contract'):
                self.register(bad)
        for change in ({'flows':[dict(ref='missing')]},{'outcome':'made_up'},{'outcome':'continuation'},{'ambiguous':True}):
            bad=declaration('messaging.'+self.prefix+'_bad');bad['cases'][0].update(change)
            with self.assertRaises(frappe.ValidationError):self.register(bad)

    def test_all_82_materialized_contracts_and_continuation(self):
        catalog=materialized_catalog();self.assertEqual([len(r['cases']) for r in catalog],[32,50])
        case=next(r for r in catalog[0]['cases'] if r['group']=='continuation')
        self.assertNotIn('capture_pending', case['input_data']['context'])
        self.assertNotIn('pending_parameters', case['input_data']['context'])
        self.assertEqual(case['expected_data']['outcome'], 'unrecognized')
        self.assertEqual(case['expected_data']['flows'], [])
        for case in catalog[1]['cases']:validate_extraction(case['input_data'],case['expected_data'])

    def test_coverage_applicability_additive_and_native_queries(self):
        from messaging.chatbot.doctype.ai_provider.test_ai_provider import install_synthetic_provider
        install_synthetic_provider(self)
        raw=declaration('messaging.'+self.prefix);raw.update(scope='Provider',provider='synthetic');self.register(raw)
        names={r.name for r in queries.resolve_templates(profile_type='Decision',provider='synthetic')};self.assertEqual(len(names),17)
        raw.update(scope='Application',provider='',applicable_application='frappe');self.register(raw)
        self.assertNotIn(raw['template_id'],{r.name for r in queries.resolve_templates(profile_type='Decision')})
        self.assertIn(raw['template_id'],{r.name for r in queries.resolve_templates(profile_type='Decision',applications=['frappe'])})
        first=queries.list_cases(raw['template_id'],page_length=1);self.assertNotIn('input_data',first[0])
        with self.assertRaises(frappe.ValidationError):queries.list_cases(raw['template_id'],page_length=501)

    def test_row_ownership_and_direct_write_protection(self):
        core=frappe.get_doc('AI Evaluation Case Template',messaging_evaluations()[0]['template_id'])
        with self.assertRaises(frappe.ValidationError):frappe.get_doc(core.coverage[0].as_dict()).save(ignore_permissions=True)
        template,raw=self.custom();template.set('available_flows',[core.available_flows[0].as_dict()])
        with self.assertRaises(frappe.PermissionError):template.save()

    def multiparameter(self):
        raw=declaration('custom.'+self.prefix,purpose='Parameter Extraction')
        raw['parameters']=[dict(ref='incident_codes',label='Incidents',parameter_type='Expression',expression='INC-####\nALT-@@##',allow_multiple_values=1,minimum_values=1,maximum_values=3),dict(ref='customer_name',label='Customer',parameter_type='Name',allow_contextual_name_extraction=1),dict(ref='contract_number',label='Contract',parameter_type='Int',has_min_value=1,min_value=1)]
        raw['coverage']=[dict(language='es',group=k,sample_count=1,required=True) for k in ('Expression','Name','Int')]
        raw['cases'][0].update(input_text='Soy José Núñez, contrato 12345: INC-0042 e INC-2098.',expected_parameters=[dict(ref=r,status='EXTRACTED') for r in ('incident_codes','customer_name','contract_number')],expected_values=[dict(ref='incident_codes',value_text='INC-0042'),dict(ref='incident_codes',value_text='INC-2098'),dict(ref='customer_name',value_text='José Núñez'),dict(ref='contract_number',value_int=12345)])
        return raw

    def test_multiparameter_native_values_expression_and_full_comparison(self):
        from messaging.chatbot.doctype.ai_evaluation_run.comparison import compare_extraction
        raw=self.multiparameter();template=insert_custom(raw);case=insert_case(template,raw['cases'][0]);source,expected=materialize(case)
        self.assertEqual(expected['parameters']['incident_codes']['values'],['INC-0042','INC-2098']);self.assertEqual(expected['parameters']['contract_number']['values'],[12345])
        actual=copy.deepcopy(expected);actual['parameters']['incident_codes']['values'].pop()
        self.assertEqual(compare_extraction(source,None,actual,expected)[0]['field'],'parameters.incident_codes.values')
        for ref in ('Name','Int','Expression'):self.assertEqual(queries.count_cases(template.name,group=ref),1)

    def test_multiparameter_missing_types_status_and_cardinality(self):
        raw=self.multiparameter();template=insert_custom(raw);case=insert_case(template,raw['cases'][0]);source,expected=materialize(case)
        for mode in ('missing','integer','status','overflow'):
            actual=copy.deepcopy(expected)
            if mode=='missing':actual['parameters'].pop('customer_name')
            elif mode=='integer':actual['parameters']['contract_number']['values']=['12345']
            elif mode=='status':actual['parameters']['incident_codes']['status']='NOT_FOUND'
            else:actual['parameters']['incident_codes']['values']=['INC-0001','INC-0002','INC-0003','INC-0004']
            with self.assertRaises(frappe.ValidationError):validate_extraction(source,actual)

    def test_coverage_union_and_insufficient_combination(self):
        from messaging.chatbot.doctype.ai_evaluation_run.sampling import select_strata
        raw=self.multiparameter();raw['coverage'].append(dict(language='es',group='Expression',sample_count=1,required=True,minimum_parameters=3))
        template=insert_custom(raw);insert_case(template,raw['cases'][0]);strata=[dict(case_template=template.name,**r) for r in queries.coverage_status(template.name)]
        chosen=select_strata(strata,'seed');self.assertEqual(len(chosen),1);self.assertEqual(len(chosen[0]['coverage_strata']),4)
        template.coverage[-1].minimum_parameters=4;template.save();self.assertFalse(queries.coverage_status(template.name)[-1]['sufficient'])

    def test_external_registration_cannot_replace_messaging(self):
        raw=declaration('frappe.'+self.prefix);ns={'__name__':'frappe','raw':typed(raw),'register_evaluations':register_evaluations}
        exec(compile("result=register_evaluations(app='frappe',evaluations=[raw])",frappe.get_app_path('frappe','__init__.py'),'exec'),ns)
        self.assertEqual(ns['result']['created'],1)
        ns['raw']=typed(declaration('messaging.forbidden'))
        with self.assertRaises(frappe.ValidationError):exec(compile("register_evaluations(app='frappe',evaluations=[raw])",frappe.get_app_path('frappe','__init__.py'),'exec'),ns)


class TestEvaluationCatalogEquivalence(unittest.TestCase):
    def test_native_catalog_semantics(self):
        import hashlib
        from .ai_evaluation_case_template import metadata, template_contract
        result = {}
        for definition in native_evaluations():
            raw = definition.initial_values()
            template = frappe.new_doc('AI Evaluation Case Template')
            template.update({k:v for k,v in raw.items() if k != 'cases'})
            semantic = dict(metadata=metadata(template), scenario=template_contract(template), cases=[])
            for localized in sorted(raw['cases'], key=lambda c:c['language']):
                case = frappe.new_doc('AI Evaluation Case');case.update(localized)
                case.group = template.group if template.profile_type == 'Decision' else template.parameters[0].parameter_type
                source, expected = materialize(case, template)
                semantic['cases'].append(dict(language=case.language, variant=case.variant,
                    group=case.group, input=source, expected=expected))
            result[definition.key] = semantic
        digest = hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(digest, 'bdbc7eac3e594b4e21569333c0cf4b13403ed6a7d70d824806025554902851e1')
        self.assertEqual((len(result), sum(len(r['cases']) for r in result.values())), (41,82))

        for key in ('messaging.decision.human_agent', 'messaging.decision.human_operator'):
            self.assertTrue(result[key]['scenario']['human_attention_available'])
            for case in result[key]['cases']:
                self.assertTrue(case['expected']['human_attention_requested'])
                self.assertEqual(case['expected']['outcome'], 'recognized')
                self.assertEqual(case['expected']['flows'], [])
