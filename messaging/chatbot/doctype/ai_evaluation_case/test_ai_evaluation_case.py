from messaging.chatbot.doctype.ai_evaluation_case_template.synchronization import register_evaluations
"""Independent case provenance and deterministic default identities, with rollback fixtures."""
from dataclasses import replace
import unittest

import frappe

from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import EvaluationCaseDefinition, EvaluationReference
from messaging.chatbot.doctype.ai_evaluation_case_template.test_ai_evaluation_case_template import (
    declaration, typed, insert_case,
)
from .ai_evaluation_case import default_case_identity


class TestEvaluationCaseRegistration(unittest.TestCase):
    def setUp(self):
        frappe.set_user('Administrator')
        frappe.local.request = None
        self.prefix = 'messaging.test_' + frappe.generate_hash(length=10)
        self.definition = typed(declaration(self.prefix))

    def tearDown(self):
        frappe.db.rollback()

    def register(self, definition=None):
        return register_evaluations(app='messaging', evaluations=(definition or self.definition,))

    def test_identity_origin_and_manual_case_on_registered_template(self):
        self.register()
        template = frappe.get_doc('AI Evaluation Case Template', self.prefix)
        language = next(iter(self.definition.cases))
        name = default_case_identity(template.name, language)
        case = frappe.get_doc('AI Evaluation Case', name)
        self.assertEqual((case.name,case.case_id,case.application,case.case_template), (name,name,'messaging',template.name))
        raw = dict(self.definition.initial_values()['cases'][0], case_id='manual', variant='alternate')
        manual = insert_case(template,raw)
        self.assertFalse(manual.application)
        manual.input_text += '!'; manual.save()
        self.assertEqual(self.register()['cases_unchanged'],1)
        manual.reload();self.assertFalse(manual.application)
        before = manual.template_revision
        self.register(replace(self.definition,previously_recognized_flows=(EvaluationReference('tracking'),)))
        manual.reload()
        self.assertFalse(manual.application)
        self.assertNotEqual(manual.template_revision,before)
        for doc in (manual,case):
            doc.reload()
            doc.application = 'frappe'
            with self.assertRaises(frappe.PermissionError):doc.save()

    def test_manual_localized_collision_is_not_adopted(self):
        self.register()
        template = frappe.get_doc('AI Evaluation Case Template', self.prefix)
        raw = dict(self.definition.initial_values()['cases'][0],case_id='manual',language='en')
        # The registered fixture is Spanish; its other covered language can be authored manually.
        definition = replace(self.definition, coverage=self.definition.coverage +
            (replace(self.definition.coverage[0],language='en'),), cases={**self.definition.cases,
                'en': EvaluationCaseDefinition(**{k:v for k,v in raw.items() if k in ('input_text',)})})
        from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import _registry_write
        with _registry_write():
            template.append('coverage',dict(language='en',group=template.group,sample_count=1,required=1));template.save()
        raw.update(flow_texts=self.definition.initial_values()['cases'][0]['flow_texts'],
            flows=self.definition.initial_values()['cases'][0]['flows'])
        manual=insert_case(template,raw)
        with self.assertRaises(frappe.PermissionError):self.register(definition)
        self.assertFalse(frappe.get_doc(manual.doctype,manual.name).application)

    def test_structural_types_and_immutable_language_cases(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import EvaluationExpectedValue
        for mutation in (dict(mandatory=1),dict(cases={}),dict(cases={'bad code':next(iter(self.definition.cases.values()))})):
            with self.subTest(mutation=mutation),self.assertRaises(frappe.ValidationError):replace(self.definition,**mutation)
        with self.assertRaises(TypeError):self.definition.cases['fr']=next(iter(self.definition.cases.values()))
        with self.assertRaises(frappe.ValidationError):EvaluationExpectedValue('number',value_int=True)
        with self.assertRaises(TypeError):EvaluationCaseDefinition(input_text='Text',name='forged')

    def test_normalized_template_collision_is_atomic(self):
        first=replace(self.definition,key=self.prefix+'.part')
        second=replace(self.definition,key=self.prefix+'_part')
        with self.assertRaises(frappe.ValidationError):register_evaluations(app='messaging',evaluations=(first,second))
        self.assertFalse(frappe.db.exists('AI Evaluation Case Template',first.key))

    def test_repeated_registration_and_language_extension_preserve_origin_without_runs(self):
        before={dt:frappe.db.count(dt) for dt in ('AI Evaluation Run','AI Evaluation Result')}
        self.register()
        original=frappe.get_doc('AI Evaluation Case',default_case_identity(self.prefix,'es'))
        definition=replace(self.definition,coverage=self.definition.coverage+(replace(self.definition.coverage[0],language='en'),),
            cases={**self.definition.cases,'en':self.definition.cases['es']})
        self.assertEqual(self.register(definition)['cases_created'],1)
        self.assertEqual(self.register(definition)['cases_unchanged'],2)
        self.assertEqual(frappe.get_doc(original.doctype,original.name).application,'messaging')
        self.assertEqual({dt:frappe.db.count(dt) for dt in before},before)
