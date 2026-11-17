"""Code-owned projection, compatible bindings, localization and approval regressions."""
from messaging.registry import load_declarations
import json
import unittest
from unittest.mock import patch

import frappe

from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures
from messaging.communication.doctype.process_definition.validation import test_state
from messaging.communication.doctype.process_definition.validation import ProcessSchemaValidationError, validate_process_schema_v1
from messaging.registrations.processes import BotProcess, BotRequest, BotResponse
from messaging.registry.processes import register_processes
from messaging.chatbot.doctype.bot_flow.contract import synchronize, validate_bindings, discover
from messaging.chatbot.doctype.bot_flow.bot_flow import resolve_flow_output
from messaging.chatbot.doctype.bot_settings.language import presentation_language, resolve_process_language
from messaging.chatbot.doctype.bot_flow.localization import text
from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_request_phrase
from messaging.chatbot.doctype.bot_flow.execution import process_arguments


def contract_flow(*, params, outcomes):
    params.register('contracts', type='string', multiple=True, required=True)
    outcomes.register('success', fields={'contract':'string'}, mode=['message', 'template'], action='complete_flow')
    outcomes.register('not_found', fields={}, mode=['message', 'template'], action='retry_parameter')


def contract_business(request: BotRequest) -> BotResponse:
    return BotResponse('success', data={'contract':request.params['contracts'][0]}, message='Dynamic result')


def consumer_contract_flow(*, params, outcomes):
    params.register('contracts', type='string', multiple=True, required=True)
    outcomes.register('success', fields={'contract':'string'}, mode=['message', 'template'], action='complete_flow')
    outcomes.register('not_found', fields={}, mode=['message', 'template'], action='complete_flow')


class TestBotFlow(ProcessFixtures, unittest.TestCase):
    def configured(self, declaration=contract_flow):
        identifier='messaging.'+self.prefix
        register_processes(app='messaging',processes=[BotProcess(id=identifier, label='Technical inputs', definition=declaration, handler=contract_business)])
        parameter=frappe.get_doc(dict(doctype='Bot Parameter',name=self.prefix+'_contracts',label='Contract',
            parameter_type='Expression',expression='SC-#####\n#####',enabled=1,
            request_phrases=[dict(language='es',request_phrase='Please provide the contract.')])).insert()
        flow=frappe.new_doc('Bot Flow')
        flow.update(dict(workflow_code=self.prefix,workflow_name='Technical flow',semantic_description='Technical flow description',
            process_definition=identifier,status='Disabled',priority=1001,
            input_initial_message='Provide the contract.',input_continuation_message='Provide the pending contract.'))
        from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
        language=resolve_process_language()
        flow.append('translations',dict(language=language,semantic_description=flow.semantic_description,menu_label='Technical menu label'))
        flow.append('input_translations',dict(language=language,input_initial_message=flow.input_initial_message,input_continuation_message=flow.input_continuation_message))
        synchronize(flow)
        flow.set('outputs',sorted(flow.outputs,key=lambda row:row.status_code!='success'))
        for index,row in enumerate(flow.outputs,1):row.idx=index
        flow.parameters[0].update(dict(parameter=parameter.name,required=1,capture_order=1,minimum_values=1,maximum_values=3))
        if declaration is contract_flow:
            flow.outputs[1].update(dict(flow_action='Retry Parameter',retry_parameter=parameter.name))
        return flow,parameter

    def template(self, body='Contract {{contract}}',language='es'):
        return frappe.get_doc(dict(doctype='Bot Flow Template',template_name=self.prefix+' '+language,
            language=language,enabled=1,body=body,template_variables=[dict(parameter_name='contract')])).insert()

    def languages(self):
        settings=frappe.get_doc('Bot Settings')
        settings.set('chatbot_languages',[dict(language='es',enabled=1,is_default=1),dict(language='en',enabled=1,is_default=0)])
        settings.save()
        return settings

    def test_required_text_hints_follow_backend_consumers_without_discovery(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import message_requirements
        flow, parameter = self.configured()
        candidate = flow.as_dict()
        with patch('frappe.get_doc', side_effect=AssertionError('Foreign controllers must not be instantiated')):
            with self.assertRaises(frappe.PermissionError): message_requirements({'doctype': 'User'})
        candidate['show_in_menu'] = 1
        with patch('messaging.registry.processes.discovery.discover_contract', side_effect=AssertionError('Hints cannot discover a Process')):
            self.assertEqual(set(message_requirements(candidate)), {'semantic_description', 'menu_label', 'input_initial_message', 'input_continuation_message'})
            candidate['show_in_menu'] = 0
            candidate['parameters'] = []
            self.assertIn('input_initial_message', message_requirements(candidate))
            candidate['process_contract'] = '{}'
            self.assertEqual(message_requirements(candidate), ['semantic_description'])
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError): message_requirements(candidate)

    def test_general_layout_uses_native_sections_and_columns(self):
        meta = frappe.get_meta('Bot Flow')
        general = [field for field in meta.fields][:next(index for index, field in enumerate(meta.fields)
            if field.fieldname == 'process_flow_tab')]
        self.assertEqual([field.label for field in general if field.fieldtype == 'Section Break'],
            ['Flow Information', 'Flow Messages', 'Menu or Catalog'])
        names = [field.fieldname for field in general]
        self.assertEqual(names[names.index('workflow_name') + 1], 'translation_cards')
        self.assertLess(names.index('translation_cards'), names.index('column_break_general'))
        self.assertEqual(names[names.index('section_break_semantics') + 1:names.index('section_break_menu')],
            ['initial_message_card', 'column_break_messages', 'continuation_message_card'])
        for name in ('menu_order', 'menu_translation_card'):
            self.assertEqual(meta.get_field(name).depends_on, 'eval:doc.show_in_menu')
            self.assertFalse(meta.get_field(name).reqd)
        self.assertFalse(meta.get_field('translation_cards').depends_on)
        for name in ('translation_cards', 'initial_message_card', 'continuation_message_card', 'menu_translation_card'):
            self.assertEqual(meta.get_field(name).fieldtype, 'HTML')

    def test_menu_label_requires_only_default_language_and_preserves_hidden_values(self):
        self.languages(); flow, parameter = self.configured()
        flow.append('translations', dict(language='en', menu_label='Secondary label'))
        default = flow.translations[0]
        for label in (None, '', '   '):
            default.menu_label = label
            with self.subTest(label=label), self.assertRaisesRegex(frappe.ValidationError, 'Menu Label is required'):
                flow.save()
        self.assertTrue(flow.workflow_name)
        flow.show_in_menu = 0
        default.menu_label = None
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.activate(flow)
        flow.reload()
        self.assertEqual(flow.status, 'Enabled')
        default = flow.translations[0]
        default.menu_label = 'Default label'
        flow.menu_order = 37
        flow.show_in_menu = 1
        flow.save(); flow.reload()
        self.assertEqual(test_state(flow), 'Passed')
        snapshot = [{key: value for key, value in row.as_dict().items() if key not in ('modified', 'modified_by')}
            for row in flow.translations + flow.input_translations]
        flow.show_in_menu = 0
        flow.save(); flow.reload()
        self.assertEqual(flow.menu_order, 37)
        self.assertEqual([{key: value for key, value in row.as_dict().items() if key not in ('modified', 'modified_by')}
            for row in flow.translations + flow.input_translations], snapshot)
        flow.show_in_menu = 1
        flow.save(); flow.reload()
        self.assertEqual([{key: value for key, value in row.as_dict().items() if key not in ('modified', 'modified_by')}
            for row in flow.translations + flow.input_translations], snapshot)
        flow.translations[1].menu_label = ''
        flow.save(); flow.reload()
        self.assertEqual(text(flow, 'menu_label', 'en'), 'Default label')
        self.assertEqual(len(flow.translations), 2)
        for table, field in [('translations', 'semantic_description'),
            ('input_translations', 'input_initial_message'), ('input_translations', 'input_continuation_message')]:
            row = flow.get(table)[0]
            original = row.get(field)
            row.set(field, '')
            with self.subTest(field=field), self.assertRaises(frappe.ValidationError):
                flow.validate_configuration()
            row.set(field, original)

    def test_discovery_generates_rows_and_preserves_bindings_row_identity_and_actions(self):
        flow,parameter=self.configured()
        projected=discover(flow.as_dict())
        self.assertEqual(projected['parameters'][0]['process_argument'],'contracts')
        self.assertEqual(projected['parameters'][0]['internal_type'],'string')
        self.assertEqual({r['status_code'] for r in projected['outputs']},{'success','not_found'})
        self.assertEqual(self.approve(flow)['status'],'Passed')
        before=[r.as_dict() for r in flow.parameters+flow.outputs]
        fingerprint=flow.process_contract_fingerprint
        synchronize(flow)
        self.assertEqual([r.as_dict() for r in flow.parameters+flow.outputs],before)
        self.assertEqual(flow.process_contract_fingerprint,fingerprint)
        self.assertEqual(test_state(flow),'Passed')

    def test_code_changes_add_rows_and_require_intervention_for_removals_and_type_changes(self):
        flow,parameter=self.configured();self.approve(flow)
        contract=json.loads(flow.process_contract)
        contract['outcomes']['new_status']={'fields': {}, 'mode': ['message'], 'action': 'complete_flow'}
        synchronize(flow,contract=contract)
        self.assertEqual(flow.outputs[-1].status_code,'new_status')
        self.assertEqual(flow.outputs[1].retry_parameter,parameter.name)
        removed=json.loads(flow.process_contract);del removed['outcomes']['success']
        with self.assertRaises(frappe.ValidationError):
            synchronize(flow,contract=removed)
        changed=json.loads(flow.process_contract);changed['parameters'][0]['type']='integer'
        with self.assertRaises(frappe.ValidationError):
            synchronize(flow,contract=changed)
        self.assertTrue(frappe.db.exists('Bot Flow',flow.name))

    def test_manual_status_or_input_cannot_redefine_python_contract(self):
        flow,parameter=self.configured()
        for table,item in [('parameters',dict(parameter='fake',process_argument='forged')),
            ('outputs',dict(status_code='forged',output_mode='Message'))]:
            size=len(flow.get(table))
            flow.append(table,item)
            with self.assertRaises(frappe.ValidationError):
                flow.validate_configuration()
            flow.set(table,flow.get(table)[:size])

    def test_expression_binding_and_one_to_three_contracts(self):
        flow,parameter=self.configured();flow.validate_configuration()
        values=process_arguments(flow,{parameter.name:['SC-00125','00126']})
        self.assertEqual(len(values['contracts']),2)
        for invalid in ([],['00001','00002','00003','00004'],['wrong']):
            with self.subTest(invalid=invalid),self.assertRaises(frappe.ValidationError):
                process_arguments(flow,{parameter.name:invalid})
        parameter.parameter_type='Int'
        with patch('frappe.get_doc',return_value=parameter),self.assertRaises(frappe.ValidationError):
            validate_bindings(flow)

    def test_structural_approval_covers_all_outputs_without_business_scenarios(self):
        flow,parameter=self.configured()
        result=self.approve(flow)
        self.assertEqual(result['status'],'Passed',result)
        self.assertEqual(result['validation_scope'],'structural_contract')
        self.assertEqual(test_state(flow),'Passed')
        flow.outputs[0].flow_action='Cancel Flow'
        self.assertEqual(test_state(flow),'Untested')
        with self.assertRaises(frappe.ValidationError):flow.save()
        with self.assertRaises(frappe.ValidationError):
            self.activate(flow)

    def test_template_fields_validate_without_values_in_every_language(self):
        self.languages();flow,parameter=self.configured()
        spanish=self.template();english=self.template('Account {{contract}}','en')
        flow.outputs[0].output_mode='Template'
        flow.append('output_translations',dict(language='es',status_code='success',template_reference=spanish.name))
        flow.append('output_translations',dict(language='en',status_code='success',template_reference=english.name))
        flow.validate_configuration()
        self.assertEqual(self.approve(flow)['status'],'Passed')
        response=dict(version=1,status='success',recipient='',data={'contract':'00125'},message='ignored')
        with presentation_language('en'):
            result=resolve_flow_output(flow,response)
        self.assertEqual(result['rendered_message'],'Account 00125')
        english.body='Account {{unknown}}';english.set('template_variables',[dict(parameter_name='unknown')]);english.save()
        with self.assertRaises(frappe.ValidationError):
            flow.validate_configuration()

    def test_localized_semantics_menu_questions_and_dynamic_message_precedence(self):
        self.languages();flow,parameter=self.configured()
        flow.append('translations',dict(language='en',semantic_description='English intent',menu_label='Check balance',
            menu_description='English menu description'))
        flow.append('input_translations',dict(language='en',input_initial_message='Enter your account.',input_continuation_message='Try the account again.'))
        parameter.append('request_phrases',dict(language='en',request_phrase='Which account?'));parameter.save()

        self.assertEqual(self.approve(flow)['status'],'Passed')
        with presentation_language('en'):
            self.assertEqual(text(flow,'menu_label'),'Check balance')
            self.assertEqual(text(flow,'semantic_description'),'English intent')
            self.assertEqual(text(flow,'input_initial_message'),'Enter your account.')
            self.assertEqual(get_request_phrase(flow.parameters[0].parameter),'Which account?')
            response=dict(version=1,status='success',recipient='',data={'contract':'00125'},message='Dynamic result')
            self.assertEqual(resolve_flow_output(flow,response)['rendered_message'],'Dynamic result')
            response['message']=''
            with self.assertRaises(frappe.ValidationError):
                resolve_flow_output(flow,response)
        self.assertEqual(text(flow,'input_initial_message'),'Provide the contract.')

    def test_custom_internal_error_translations_roundtrip_fallback_and_inheritance(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import get_flow_effective_internal_error_message
        self.languages()
        flow, _ = self.configured()
        flow.use_custom_internal_error_message = 1
        flow.translations[0].internal_error_message = 'Please try this request again later.'
        flow.append('translations', dict(language='en', internal_error_message='Please contact our support team.'))
        flow.save()
        flow.reload()
        with presentation_language('en'):
            self.assertEqual(get_flow_effective_internal_error_message(flow), 'Please contact our support team.')
            flow.translations[1].internal_error_message = ''
            self.assertEqual(get_flow_effective_internal_error_message(flow), 'Please try this request again later.')
        flow.use_custom_internal_error_message = 0
        flow.save()
        flow.reload()
        self.assertEqual(flow.translations[0].internal_error_message, 'Please try this request again later.')
        with patch('messaging.chatbot.doctype.bot_settings.localization.get_internal_error_message', return_value='Global safe message'):
            self.assertEqual(get_flow_effective_internal_error_message(flow), 'Global safe message')
        flow.use_custom_internal_error_message = 1
        flow.save()
        flow.reload()
        self.assertEqual(get_flow_effective_internal_error_message(flow), 'Please try this request again later.')
        with patch('messaging.chatbot.doctype.bot_flow.localization.text', side_effect=frappe.ValidationError), \
                patch('messaging.chatbot.doctype.bot_settings.localization.get_internal_error_message', return_value='Global safe message'):
            self.assertEqual(get_flow_effective_internal_error_message(flow), 'Global safe message')

    def test_custom_internal_error_requires_default_and_validates_all_translations(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import message_requirements
        self.languages()
        flow, _ = self.configured()
        flow.use_custom_internal_error_message = 1
        flow.append('translations', dict(language='en', internal_error_message='Please try again later.'))
        with presentation_language('en'), self.assertRaises(frappe.ValidationError):
            flow.validate_configuration()
        self.assertIn('internal_error_message', message_requirements(flow.as_dict()))
        flow.translations[0].internal_error_message = 'Please try again later.'
        flow.validate_configuration()
        for unsafe in ('Traceback (most recent call last):', '{{secret}}', '/home/frappe/private.py'):
            flow.translations[1].internal_error_message = unsafe
            with self.subTest(unsafe=unsafe), self.assertRaises(frappe.ValidationError):
                flow.validate_configuration()
        flow.translations[1].internal_error_message = ''
        flow.use_custom_internal_error_message = 0
        flow.translations[0].internal_error_message = ''
        flow.validate_configuration()
        self.assertNotIn('internal_error_message', message_requirements(flow.as_dict()))

    def test_missing_parameter_phrase_blocks_configuration_without_generic_fallback(self):
        flow, parameter = self.configured()
        parameter.set('request_phrases', []);parameter.save()
        with self.assertRaisesRegex(frappe.ValidationError, 'default language'):
            flow.validate_configuration()

    def test_translation_duplicate_language_unknown_input_and_status_rejected(self):
        self.languages();flow,parameter=self.configured()
        for table,values in [('translations',dict(language='fr',menu_label='Unsupported')),
            ('output_translations',dict(language='en',status_code='unknown',message='Unknown'))]:
            flow.append(table,values)
            with self.subTest(table=table),self.assertRaises(frappe.ValidationError):
                flow.validate_configuration()
            flow.set(table,[])
        flow.append('translations',dict(language='en',menu_label='First'))
        flow.append('translations',dict(language='en',menu_label='Second'))
        with self.assertRaises(frappe.ValidationError):
            flow.validate_configuration()

    def test_default_texts_and_templates_can_be_configured_only_in_language_tables(self):
        self.languages();flow,parameter=self.configured()
        flow.semantic_description=flow.input_initial_message=flow.input_continuation_message=None
        flow.translations[0].semantic_description='Consulta de contratos'
        flow.input_translations[0].update(dict(input_initial_message='Indique el contrato.',input_continuation_message='Indique el dato pendiente.'))
        template=self.template()
        flow.outputs[0].output_mode='Template'
        flow.append('output_translations',dict(language='es',status_code='success',template_reference=template.name))
        self.assertEqual(self.approve(flow)['status'],'Passed')
        response=dict(version=1,status='success',recipient='',data={'contract':'00125'},message='')
        with presentation_language('en'):
            self.assertEqual(text(flow,'input_initial_message'),'Indique el contrato.')
            self.assertEqual(resolve_flow_output(flow,response)['rendered_message'],'Contract 00125')


class TestProcessSchema(unittest.TestCase):
    def test_strict_process_schema_v1_validator(self) -> None:
        valid = validate_process_schema_v1(
            '{"version": 1, "recipient": "", "status": "success", "data": {"amount": 250}, "message": ""}'
        )
        self.assertEqual(valid["version"], 1)
        self.assertEqual(valid["status"], "success")
        self.assertEqual(valid["data"], {"amount": 250})
        self.assertEqual(valid["message"], "")

        invalid_payloads = [
            "not-json",
            [],
            {"version": 1, "recipient": "", "status": "success", "data": {}},  # missing message
            {"version": 1, "recipient": "", "status": "success", "message": ""},  # missing data
            {"version": 1, "recipient": "", "status": "success", "data": None, "message": ""},  # null data
            {"version": 1, "recipient": "", "status": "success", "data": {}, "message": None},  # null message
            {"version": 2, "status": "success", "data": {}, "message": ""},  # unsupported version
            {"version": True, "status": "success", "data": {}, "message": ""},  # bool as int
            {"version": "1", "status": "success", "data": {}, "message": ""},  # str version
            {"version": 1, "recipient": "", "status": "", "data": {}, "message": ""},  # empty status
            {"version": 1, "recipient": "", "status": "   ", "data": {}, "message": ""},  # whitespace status
            {"version": 1, "recipient": "", "status": "internal_error", "data": {}, "message": ""},  # reserved status
            {"version": 1, "recipient": "", "status": "success", "data": [], "message": ""},  # list data
            {
                "version": 1, "recipient": "",
                "status": "success",
                "data": {},
                "message": "",
                "extra": 1,
            },  # extra top-level key
        ]
        for bad in invalid_payloads:
            with self.assertRaises(ProcessSchemaValidationError):
                validate_process_schema_v1(bad)

        # Human availability contract validation

    def test_schema_requires_root_recipient_and_flat_json_scalars(self):
        base = dict(version=1, status='success', recipient='', data={}, message='')
        for key in base:
            bad = {k:v for k,v in base.items() if k != key}
            with self.subTest(missing=key), self.assertRaises(ProcessSchemaValidationError):
                validate_process_schema_v1(bad)
        for recipient in (None, 12, {}, []):
            with self.subTest(recipient=type(recipient).__name__), self.assertRaises(ProcessSchemaValidationError):
                validate_process_schema_v1({**base, 'recipient':recipient})
        for value in ({}, [], (), {'nested':1}, float('nan'), float('inf'), -float('inf'), object()):
            with self.subTest(value=type(value).__name__), self.assertRaises(ProcessSchemaValidationError):
                validate_process_schema_v1({**base, 'data':{'value':value}})
        with self.assertRaises(ProcessSchemaValidationError):
            validate_process_schema_v1({**base, 'data':{1:'invalid key'}})
        for value in ('', 0, 1.5, False, True, None):
            self.assertEqual(validate_process_schema_v1({**base, 'data':{'value':value}})['data']['value'], value)
        with self.assertRaises(ProcessSchemaValidationError):
            validate_process_schema_v1('{"version":1,"status":"success","recipient":"","data":{"value":NaN},"message":""}')

    def test_process_method_security_validation(self) -> None:
        for unsafe_method in (
            "os.system",
            "subprocess.Popen.communicate",
            "builtins.eval.run",
            "frappe.db.sql",
            "frappe.utils.safe_exec.safe_exec",
            "app.module._private_func",
            "eval('1+1')",
        ):
            from messaging.communication.doctype.process_definition.execution import validate_process_method_path
            with self.assertRaises(frappe.ValidationError):
                validate_process_method_path(unsafe_method)



class TestContactLanguagePersistence(ProcessFixtures, unittest.TestCase):
    def test_detection_persists_contact_preference_with_scoped_native_write(self):
        from messaging.chatbot.doctype.bot_settings.language import contact_language
        from messaging.security.execution_context import _automation_context
        from messaging.conversation.doctype.chat_contact.chat_contact import get_contact
        from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings
        settings=get_bot_settings()
        settings.set('chatbot_languages',[dict(language='es',enabled=1,is_default=1),dict(language='en',enabled=1,is_default=0)])
        settings.save()
        rule,source=self.notification()
        contact=get_contact('emulator',rule.account,self.prefix)
        with _automation_context():
            self.assertEqual(contact_language(contact,'I would like to know my current account balance please',settings),'en')
            self.assertFalse(frappe.has_permission('Chat Contact','write'))
        self.assertEqual(frappe.get_doc('Chat Contact',contact.name).preferred_language,'en')
        with _automation_context():
            self.assertEqual(contact_language(contact,'0',settings),'en')
            self.assertEqual(contact_language(contact,'0',settings,context_language='es'),'es')
            self.assertEqual(contact_language(contact,'Je voudrais consulter le solde de mon compte.',settings),'es')
        self.assertEqual(frappe.get_doc('Chat Contact',contact.name).preferred_language,'en')


class TestIntegratedLocalization(ProcessFixtures, unittest.TestCase):
    """Exercise parent-owned translations with real Frappe persistence and rollback."""
    configured = TestBotFlow.configured
    template = TestBotFlow.template
    languages = TestBotFlow.languages

    def test_parameter_save_reload_discard_and_binding_uses_the_selected_parameter(self):
        self.languages();flow,parameter=self.configured()
        parameter.request_phrases[0].request_phrase='Contrato?';parameter.save()
        self.assertEqual(self.approve(flow)['status'],'Passed')
        parameter.request_phrases[0].request_phrase='Unsaved'
        self.assertEqual(get_request_phrase(parameter.name),'Contrato?')
        parameter.reload()
        other=frappe.copy_doc(parameter);other.name=self.prefix+'_other'
        other.request_phrases[0].request_phrase='Another reusable question';other.insert()
        flow.parameters[0].parameter=other.name
        flow.outputs[1].retry_parameter=other.name
        self.assertEqual(self.approve(flow)['status'],'Passed')
        self.assertEqual(get_request_phrase(flow.parameters[0].parameter),'Another reusable question')
        other.request_phrases[0].request_phrase='Saved question';other.save()
        self.assertEqual(get_request_phrase(other.name),'Saved question')
        self.assertEqual(test_state(flow),'Passed')

    def test_input_message_plain_text_roundtrip_matches_parameter_phrases(self):
        self.languages(); flow, parameter = self.configured()
        message = 'Indica el <contrato> & el dato.\nSegunda línea.'
        for field in ('menu_label', 'menu_description', 'semantic_description'):
            flow.translations[0].set(field, '<dato> & "texto" á 😀')
        flow.input_translations[0].input_initial_message = message
        flow.input_translations[0].input_continuation_message = message
        flow.append('input_translations', dict(language='en', input_initial_message=message, input_continuation_message=message))
        flow.save(); flow.reload()
        for field in ('menu_label', 'menu_description', 'semantic_description'):
            self.assertEqual(text(flow, field, 'es'), '<dato> & "texto" á 😀')
        self.assertEqual(text(flow, 'input_initial_message', 'es'), message)
        self.assertEqual(text(flow, 'input_continuation_message', 'en'), message)
        parameter.reload()
        parameter.request_phrases[0].request_phrase = message
        parameter.save(); parameter.reload()
        self.assertEqual(get_request_phrase(parameter.name), message)

    def test_disabled_flow_languages_retained_and_parameter_resolves_default(self):
        settings=self.languages();flow,parameter=self.configured()
        flow.append('translations',dict(language='en',semantic_description='English meaning'))
        flow.append('input_translations',dict(language='en',input_initial_message='English input',input_continuation_message='More English'))
        parameter.append('request_phrases',dict(language='en',request_phrase='English question'));parameter.save()
        self.assertEqual(self.approve(flow)['status'],'Passed')
        settings.chatbot_languages[1].enabled=0;settings.save()
        flow.validate_configuration()
        self.assertNotEqual(text(flow,'semantic_description','en'),'English meaning')
        self.assertEqual(text(flow,'input_initial_message','en'),'Provide the contract.')
        self.assertEqual(get_request_phrase(parameter.name,'en'),'Please provide the contract.')
        from messaging.chatbot.doctype.bot_parameter.bot_parameter import language_options
        self.assertNotIn('en',[r[0] for r in language_options('Language','','name',0,20,{})])

    def test_input_rows_reject_duplicates_and_disabled_languages(self):
        settings=self.languages();settings.chatbot_languages[1].enabled=0;settings.save()
        flow,parameter=self.configured()
        for language in ('es','en','fr'):
            flow.append('input_translations',dict(language=language,input_initial_message='Initial',input_continuation_message='Continue'))
            with self.subTest(language=language),self.assertRaises(frappe.ValidationError):flow.validate_configuration()
            flow.set('input_translations',flow.input_translations[:1])

    def test_templates_validate_language_fields_and_runtime_values(self):
        self.languages();flow,parameter=self.configured();template=self.template()
        flow.outputs[0].output_mode='Template'
        translated=flow.append('output_translations',dict(language='en',status_code='success',template_reference=template.name))
        from messaging.chatbot.doctype.bot_flow.localization import validate,output
        with self.assertRaises(frappe.ValidationError):validate(flow)
        translated.language='es';flow.validate_configuration()
        with presentation_language('en'):
            self.assertEqual(output(flow,flow.outputs[0])['language'],'es')
            response=dict(version=1,status='success',recipient='',data={'contract':''},message='')
            with self.assertRaises(frappe.ValidationError):resolve_flow_output(flow,response)
        flow.set('output_translations',[])
        with self.assertRaises(frappe.ValidationError):output(flow,flow.outputs[0])

    def test_output_mode_switch_preserves_language_templates(self):
        self.languages();flow,parameter=self.configured();template=self.template()
        flow.outputs[0].output_mode='Template'
        flow.append('output_translations',dict(language='es',status_code='success',template_reference=template.name))
        self.assertEqual(self.approve(flow)['status'],'Passed')
        identity=flow.output_translations[0].name
        flow.outputs[0].output_mode='Message'
        self.assertEqual(self.approve(flow)['status'],'Passed')
        flow.reload();self.assertEqual(flow.output_translations[0].name,identity)
        response=dict(version=1,status='success',recipient='',data={'contract':'00125'},message='Business text')
        self.assertEqual(resolve_flow_output(flow,response)['rendered_message'],'Business text')
        response['message']=''
        with self.assertRaises(frappe.ValidationError):resolve_flow_output(flow,response)
        flow.outputs[0].output_mode='Template'
        self.assertEqual(self.approve(flow)['status'],'Passed')
        self.assertEqual(resolve_flow_output(flow,response)['rendered_message'],'Contract 00125')

    def test_administrative_approval_never_calls_business(self):
        flow,parameter=self.configured()
        import sys
        calls=[]
        previous=sys.getprofile()
        def observe(frame,event,arg):
            if event=='call' and frame.f_code is contract_business.__code__:calls.append(event)
        sys.setprofile(observe)
        try:
            self.assertEqual(self.approve(flow)['status'],'Passed')
        finally:sys.setprofile(previous)
        self.assertEqual(calls,[])
        identity=flow.process_test_log
        flow.reload();self.assertEqual(test_state(flow),'Passed')
        self.assertEqual(flow.process_test_log,identity)
        flow.input_translations[0].input_initial_message='A real content change'
        self.assertEqual(test_state(flow),'Passed')


class TestProcessAssignment(ProcessFixtures, unittest.TestCase):
    def options(self, current=None):
        from messaging.chatbot.doctype.bot_flow.bot_flow import available_process_definitions
        return {row[0] for row in available_process_definitions('Process Definition', '', 'name', 0, 100,
            {'flow': current})}

    def test_free_disabled_assigned_edit_and_delete_release_definition(self):
        flow = self.flow()
        definition = frappe.get_doc('Process Definition', flow.process_definition).as_dict()
        self.assertIn(flow.process_definition, self.options())
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.activate(flow, False)
        self.assertEqual(flow.status, 'Disabled')
        self.assertNotIn(flow.process_definition, self.options())
        self.assertIn(flow.process_definition, self.options(flow.name))
        flow.save()
        second = frappe.copy_doc(flow)
        second.workflow_code = self.prefix + '_second'
        second.workflow_name = 'Another Flow'
        with self.assertRaisesRegex(frappe.ValidationError, 'already assigned'):
            second.insert()
        self.assertEqual(frappe.get_doc('Process Definition', flow.process_definition).as_dict(), definition)
        frappe.delete_doc('Bot Flow', flow.name)
        self.assertTrue(frappe.db.exists('Process Definition', flow.process_definition))
        self.assertIn(flow.process_definition, self.options())
        self.assertEqual(self.approve(second)['status'], 'Passed')

    def test_unique_key_protects_insert_and_update_when_validation_is_bypassed(self):
        flow = self.flow()
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        candidate = frappe.get_doc(dict(doctype='Bot Flow', name=self.prefix + '_duplicate',
            workflow_code=self.prefix + '_duplicate', workflow_name='Other name',
            process_definition=flow.process_definition))
        with self.assertRaisesRegex(frappe.ValidationError, 'already assigned'):
            candidate.db_insert()
        other = self.flow('alternate_flow')
        self.assertEqual(self.approve(other)['status'], 'Passed')
        other.process_definition = flow.process_definition
        with self.assertRaisesRegex(frappe.ValidationError, 'already assigned'):
            other.db_update()

    def test_notifications_unavailable_and_missing_definitions_are_rejected(self):
        from messaging.communication.doctype.process_definition.process_definition import bind_definition
        identifier = self.register('technical_notification', 'notification')
        self.assertNotIn(identifier, self.options())
        flow = self.flow()
        flow.process_definition = identifier
        with self.assertRaises(frappe.ValidationError):
            bind_definition(flow)
        for missing in ('', 'missing.definition'):
            flow.process_definition = missing
            with self.assertRaises(frappe.ValidationError):
                bind_definition(flow)


class TestFlowLifecycle(ProcessFixtures, unittest.TestCase):
    configured = TestBotFlow.configured
    template = TestBotFlow.template
    languages = TestBotFlow.languages

    def test_failed_test_cannot_enable_or_create_approval(self):
        from messaging.communication.doctype.process_definition import validation
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process
        flow = self.flow(); flow.insert()
        with patch.object(validation, 'execute_process', return_value=dict(functional=False,
            status=None, execution_log=None, diagnostic='Synthetic structural failure')):
            result = validate_process(flow.as_dict())
        flow.reload()
        self.assertEqual(result['status'], 'Failed')
        self.assertEqual(result['message'], 'Synthetic structural failure')
        self.assertEqual(flow.status, 'Disabled')
        self.assertNotEqual(flow.process_test_status, 'Passed')

    def test_obsolete_candidate_and_changed_dependency_cannot_enable(self):
        from messaging.communication.doctype.process_definition import validation
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, set_active
        flow, parameter = self.configured(); flow.insert()
        candidate = flow.as_dict()
        execute = validation.execute_process
        def changed(*args, **kwargs):
            result = execute(*args, **kwargs)
            frappe.db.set_value('Bot Parameter', parameter.name, 'enabled', 0)
            return result
        with patch.object(validation, 'execute_process', side_effect=changed), self.assertRaises(frappe.TimestampMismatchError):
            validate_process(candidate)
        flow.reload()
        self.assertEqual(flow.status, 'Disabled')
        self.assertNotEqual(flow.process_test_status, 'Passed')
        self.assertTrue(frappe.db.get_value('Bot Parameter', parameter.name, 'enabled'))
        validate_process(flow.as_dict()); flow.reload()
        with self.assertRaises(frappe.TimestampMismatchError):
            validate_process(candidate)
        self.activate(flow, False)
        from messaging.communication.doctype.process_definition.process_definition import _registry_write
        with _registry_write():
            definition = frappe.get_doc('Process Definition', flow.process_definition)
            definition.status = 'Unavailable'; definition.save()
        with self.assertRaises(frappe.ValidationError):
            set_active(flow.as_dict(), 1)
        self.assertEqual(frappe.db.get_value('Bot Flow', flow.name, 'status'), 'Disabled')

    def test_manual_activation_does_not_discover_configuration_or_run_tester(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import BotFlow, validate_process, set_active
        from messaging.communication.doctype.process_definition import validation
        flow = self.flow(); flow.insert()
        self.assertEqual(validate_process(flow.as_dict())['lifecycle_status'], 'Enabled')
        flow.reload(); log = flow.process_test_log
        self.activate(flow, False)
        flow.workflow_name = 'Saved while administratively disabled'; flow.save(); flow.reload()
        flow.onload()
        self.assertEqual(flow.status, 'Disabled')
        self.assertEqual(flow.get_onload('flow_lifecycle')['lifecycle_status'], 'Disabled')
        with patch.object(validation, 'test_method', side_effect=AssertionError('No tester')), patch.object(
            BotFlow, 'validate_configuration', side_effect=AssertionError('No save projection')):
            self.assertEqual(set_active(flow.as_dict(), 1)['lifecycle_status'], 'Enabled')
        flow.reload(); self.assertEqual(flow.process_test_log, log)

    def test_capture_messages_are_retained_and_semantics_always_required(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, message_requirements
        flow = self.flow(show_in_menu=0)
        flow.append('input_translations', dict(language='es', input_initial_message='Retained start',
            input_continuation_message='Retained continuation'))
        flow.insert()
        self.assertEqual(message_requirements(flow.as_dict()), ['semantic_description'])
        self.assertEqual(validate_process(flow.as_dict())['lifecycle_status'], 'Enabled')
        flow.reload()
        self.assertEqual(flow.input_translations[0].input_initial_message, 'Retained start')
        self.assertEqual(flow.input_translations[0].input_continuation_message, 'Retained continuation')
        flow.translations[0].semantic_description = ''
        with self.assertRaisesRegex(frappe.ValidationError, 'Semantic Description'):
            flow.save()
        captured, parameter = self.configured(); captured.priority = 0
        with self.assertRaises(frappe.ValidationError):
            captured.insert()
        captured.priority = 1001; captured.input_translations[0].input_initial_message = ''
        with self.assertRaisesRegex(frappe.ValidationError, 'Initial Input Message'):
            captured.insert()

    def test_layout_has_process_first_and_only_two_visible_process_fields(self):
        meta = frappe.get_meta('Bot Flow')
        names = [field.fieldname for field in meta.fields]
        self.assertLess(names.index('section_break_process'), names.index('section_break_parameters'))
        section = names[names.index('section_break_process') + 1:names.index('section_break_parameters')]
        self.assertEqual([name for name in section if not meta.get_field(name).hidden and meta.get_field(name).fieldtype != 'Column Break'],
            ['process_definition', 'process_method'])
        self.assertTrue(meta.get_field('process_definition').reqd)
        self.assertTrue(meta.get_field('process_definition').unique)
        self.assertTrue(meta.get_field('process_method').read_only)
        self.assertIsNone(meta.get_field('enabled'))
        self.assertTrue(meta.get_field('status').read_only)
        self.assertFalse(meta.get_field('status').is_virtual)
        self.assertEqual(meta.get_field('status').options.splitlines(), ['Untested', 'Disabled', 'Enabled', 'Unavailable'])
        self.assertFalse(meta.is_submittable)

    def test_save_checks_configuration_without_approval_or_activation(self):
        from messaging.communication.doctype.process_definition import validation
        flow, parameter = self.configured()
        before = frappe.db.count('Process Execution Log')
        with patch.object(validation, 'test_method', side_effect=AssertionError('Save must not validate')):
            flow.insert()
        self.assertEqual(flow.lifecycle_status, 'Untested')
        self.assertEqual(flow.status, 'Disabled')
        self.assertEqual(frappe.db.count('Process Execution Log'), before)
        flow.update(dict(process_test_status='Passed'))
        flow.save()
        self.assertEqual(flow.lifecycle_status, 'Untested')
        flow.status = 'Enabled'
        flow.flags.messaging_bot_flow_lifecycle = True
        with self.assertRaises(frappe.PermissionError):
            flow.save()
        from frappe.client import set_value
        with self.assertRaises(frappe.PermissionError):
            set_value('Bot Flow', flow.name, 'status', 'Enabled')
        self.assertEqual(frappe.db.get_value('Bot Flow', flow.name, 'status'), 'Disabled')
        response = set_value('Bot Flow', flow.name, 'status', 'Unavailable')
        self.assertEqual(response['status'], 'Disabled')

    def test_validation_enables_and_manual_deactivation_preserves_evidence(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, set_active
        flow, parameter = self.configured()
        with self.assertRaises(frappe.ValidationError):
            validate_process(flow.as_dict())
        flow.insert()
        with self.assertRaises(frappe.ValidationError):
            set_active(flow.as_dict(), 1)
        dirty = flow.as_dict()
        dirty['workflow_name'] = 'Unsaved change'
        with self.assertRaises(frappe.ValidationError):
            validate_process(dirty)
        self.assertEqual(validate_process(flow.as_dict())['status'], 'Passed')
        flow.reload()
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        log = flow.process_test_log
        self.activate(flow)
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.activate(flow, False)
        self.assertEqual(flow.lifecycle_status, 'Disabled')
        self.assertEqual(flow.process_test_log, log)
        self.assertEqual(test_state(flow), 'Passed')
        self.activate(flow)
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertEqual(flow.process_test_log, log)
        dirty = flow.as_dict()
        dirty['workflow_name'] = 'Another unsaved change'
        with self.assertRaises(frappe.ValidationError):
            set_active(dirty, 0)

    def test_desk_omitted_null_fields_validate_without_accepting_unsaved_changes(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process, set_active
        flow, parameter = self.configured()
        flow.insert()

        def desk(value):
            if isinstance(value, dict):
                return {key: desk(item) for key, item in value.items() if item is not None}
            if isinstance(value, list):
                return [desk(item) for item in value]
            return value

        candidate = desk(flow.as_dict())
        self.assertNotIn('internal_error_message', candidate)
        self.assertNotIn('retry_parameter', candidate['outputs'][0])
        missing = desk(flow.as_dict())
        del missing['parameters'][0]['parameter']
        with self.assertRaises(frappe.ValidationError):
            validate_process(missing)
        changed = desk(flow.as_dict())
        changed['outputs'][0]['flow_action'] = 'Show Menu'
        with self.assertRaises(frappe.ValidationError):
            validate_process(changed)
        self.assertEqual(validate_process(candidate)['status'], 'Passed')
        flow.reload()
        log = flow.process_test_log
        self.assertEqual(set_active(desk(flow.as_dict()), 1)['lifecycle_status'], 'Enabled')
        flow.reload()
        self.assertEqual(set_active(desk(flow.as_dict()), 0)['lifecycle_status'], 'Disabled')
        flow.reload()
        self.assertEqual(flow.process_test_log, log)

    def test_text_edits_preserve_active_state_and_technical_receipt(self):
        flow, parameter = self.configured()
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.activate(flow)
        log = flow.process_test_log
        flow.workflow_name = 'An updated menu fallback'
        flow.translations[0].semantic_description = 'Updated semantic description'
        flow.translations[0].menu_label = 'Updated menu label'
        flow.translations[0].menu_description = 'Updated menu description'
        flow.input_translations[0].input_initial_message = 'Updated initial question'
        flow.input_translations[0].input_continuation_message = 'Updated continuation question'
        parameter.reload()
        parameter.request_phrases[0].request_phrase = 'Updated contract question'
        parameter.save()
        flow.save()
        flow.reload()
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertEqual(flow.status, 'Enabled')
        self.assertEqual(flow.process_test_log, log)
        self.assertEqual(test_state(flow), 'Passed')

    def test_capture_contract_change_requires_explicit_validation_and_enables(self):
        flow, parameter = self.configured()
        self.approve(flow)
        self.activate(flow)
        old_log = flow.process_test_log
        flow.parameters[0].maximum_values = 2
        flow.save()
        self.assertEqual(flow.lifecycle_status, 'Untested')
        self.assertEqual(flow.status, 'Disabled')
        with self.assertRaises(frappe.ValidationError):
            self.activate(flow)
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertNotEqual(flow.process_test_log, old_log)
        self.activate(flow)
        self.assertEqual(flow.lifecycle_status, 'Enabled')

    def test_runtime_detects_stale_code_and_reversion_does_not_restore_approval(self):
        from messaging.communication.doctype.process_definition import validation
        from messaging.chatbot.doctype.bot_flow.bot_flow import state
        flow = self.flow()
        self.approve(flow)
        self.activate(flow)
        old_log = flow.process_test_log
        key = validation._invalidation_key(flow)
        self.keys.add(key)
        with patch.object(validation, '_registered_runtime', return_value='changed-code'), \
             patch.object(validation, 'runtime_revision', return_value='changed-code'):
            self.assertEqual(state(flow.name)['lifecycle_status'], 'Untested')
            result = self.execute(flow)
            self.assertFalse(result['functional'])
        self.assertEqual(state(flow.name)['lifecycle_status'], 'Untested')
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertNotEqual(flow.process_test_log, old_log)
        self.activate(flow)
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.network_mock.assert_not_called()

    def test_template_text_preserves_approval_but_operational_changes_invalidate(self):
        self.languages()
        flow, parameter = self.configured()
        template = self.template()
        flow.outputs[0].output_mode = 'Template'
        flow.append('output_translations', dict(language='es', status_code='success', template_reference=template.name))
        self.approve(flow)
        self.activate(flow)
        log = flow.process_test_log
        template.body = 'Updated wording for {{contract}}'
        template.save()
        flow.save()
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertEqual(flow.process_test_log, log)
        template.enabled = 0
        template.save()
        from messaging.chatbot.doctype.bot_flow.bot_flow import state
        from messaging.communication.doctype.process_definition.validation import _invalidation_key
        self.keys.add(_invalidation_key(flow))
        self.assertEqual(state(flow.name)['lifecycle_status'], 'Untested')


class TestDefinitionReplacement(ProcessFixtures, unittest.TestCase):
    configured = TestBotFlow.configured

    def test_process_changes_replace_the_entire_unsaved_contract_without_business_calls(self):
        from messaging.communication.doctype.process_definition.process_definition import resolve_definition, registered_contract
        parameterless = self.register('technical_flow')
        parameterized = self.register('typed_flow')
        alternate = self.register('negative_flow')
        flow = frappe.new_doc('Bot Flow')
        for identifier in (parameterized, parameterless, alternate, parameterless):
            flow.process_definition = identifier
            projected = discover(flow.as_dict())
            expected = registered_contract(resolve_definition(identifier, expected_type='bot_flow'))
            self.assertEqual([r['process_argument'] for r in projected['parameters']], [i['name'] for i in expected['parameters']])
            self.assertEqual({r['status_code'] for r in projected['outputs']}, set(expected['outcomes']))
            self.assertEqual(projected['process_method'], resolve_definition(identifier, expected_type='bot_flow').method)
            self.assertEqual(json.loads(projected['process_contract'])['definition'], identifier)
            flow.set('parameters', projected['parameters']); flow.set('outputs', projected['outputs'])
            flow.process_method = projected['process_method']; flow.process_contract = projected['process_contract']
        self.assertEqual(flow.parameters, [])
        self.network_mock.assert_not_called()

    def test_persisted_replacement_rejects_stale_contract_and_clears_approval(self):
        from messaging.communication.doctype.process_definition.test_process_definition import negative_flow
        flow, parameter = self.configured(); self.approve(flow)
        original = flow.process_definition; old_log = flow.process_test_log
        identifier = 'messaging.' + self.prefix + '_replacement'
        from messaging.communication.doctype.process_definition.test_process_definition import negative_business
        register_processes(app='messaging', processes=[BotProcess(id=original, label='Original', definition=contract_flow, handler=contract_business),
            BotProcess(id=identifier, label='Replacement', definition=negative_flow, handler=negative_business)])
        flow.process_definition = identifier
        with self.assertRaisesRegex(frappe.ValidationError, 'another Process Definition'):
            flow.save()
        stored = frappe.get_doc('Bot Flow', flow.name)
        self.assertEqual(stored.process_definition, original); self.assertEqual(stored.process_test_log, old_log)
        flow.reload(); flow.process_definition = identifier
        projected = discover(flow.as_dict())
        self.assertEqual(projected['parameters'], [])
        self.assertEqual([row['status_code'] for row in projected['outputs']], ['not_found'])
        flow.set('parameters', projected['parameters']); flow.set('outputs', projected['outputs'])
        flow.set('output_translations', [])
        flow.process_method = projected['process_method']; flow.process_contract = projected['process_contract']
        flow.priority = 0
        flow.set('input_translations', [])
        flow.save(); flow.reload()
        self.assertEqual(flow.process_definition, identifier); self.assertFalse(flow.parameters)
        self.assertEqual(flow.outputs[0].status_code, 'not_found')
        self.assertEqual(test_state(flow), 'Untested'); self.assertFalse(flow.process_test_log)
        self.assertTrue(frappe.db.exists('Process Execution Log', old_log))

    def test_server_rejects_foreign_handler_snapshot_and_functional_rows(self):
        flow, parameter = self.configured(); flow.save()
        for field, value in [('process_method', 'messaging.foreign.handler'),
            ('process_contract', json.dumps({**json.loads(flow.process_contract), 'definition': 'messaging.foreign'}))]:
            with self.subTest(field=field):
                old = flow.get(field); flow.set(field, value)
                with self.assertRaises(frappe.ValidationError): flow.save()
                flow.set(field, old)
        for table, row in [('parameters', dict(process_argument='foreign', parameter=parameter.name)),
            ('outputs', dict(status_code='foreign', output_mode='Message'))]:
            with self.subTest(table=table):
                length = len(flow.get(table)); flow.append(table, row)
                with self.assertRaises(frappe.ValidationError): flow.save()
                flow.set(table, flow.get(table)[:length])
        flow.reload()
        before = frappe.get_doc('Bot Flow', flow.name).as_dict()
        with patch('messaging.registry.processes.discovery.discover_contract', side_effect=frappe.ValidationError('Discovery failed')):
            self.assertEqual(discover(flow.as_dict())['process_method'],flow.process_method)
        self.assertEqual(frappe.get_doc('Bot Flow', flow.name).as_dict(), before)


class TestUnifiedFlowContract(ProcessFixtures, unittest.TestCase):
    def test_modes_preserve_selection_reconcile_removal_and_fix_action(self):
        from copy import deepcopy
        flow=self.flow('alternate_flow');flow.insert()
        self.assertEqual(flow.outputs[0].output_mode,'Message')
        template=frappe.get_doc(dict(doctype='Bot Flow Template',
            template_name=self.prefix,language='es',enabled=1,body='Fixed content without variables.')).insert()
        flow.outputs[0].output_mode='Template'
        flow.append('output_translations',dict(status_code='success',language='es',template_reference=template.name))
        flow.save()
        synchronize(flow)
        self.assertEqual(flow.outputs[0].output_mode,'Template')
        contract=deepcopy(json.loads(flow.process_contract));contract.pop('definition')
        contract['outcomes']['success']['mode']=['message']
        old=flow.process_contract_fingerprint
        synchronize(flow,contract=contract)
        self.assertEqual(flow.outputs[0].output_mode,'Message')
        self.assertNotEqual(old,flow.process_contract_fingerprint)
        # Contract changes reconcile persisted choices; a manual forbidden choice is rejected.
        flow.process_contract=frappe.get_doc('Bot Flow',flow.name).process_contract
        flow.outputs[0].flow_action='Cancel Flow'
        with self.assertRaises(frappe.ValidationError):synchronize(flow)
        self.assertEqual(template.reload().body,'Fixed content without variables.')

    def test_single_mode_and_empty_fields_template(self):
        from messaging.chatbot.doctype.bot_flow.contract import project_contract
        from messaging.presets.processes import greeting as greeting_process
        definition=frappe._dict(application='messaging',process_id='messaging.greeting',process_type='bot_flow',
            definition_method=greeting_process.definition.__module__+'.definition',
            method=greeting_process.handle.__module__+'.handle')
        from messaging.registry.processes.discovery import discover_contract
        contract,_callback=discover_contract(definition)
        self.assertEqual(contract['parameters'],[])
        self.assertEqual(contract['outcomes']['greeted']['fields'],{})
        self.assertEqual(contract['outcomes']['greeted']['mode'][0],'template')
        flow=self.flow('alternate_flow')
        flow.set('outputs',[]);flow.process_contract=None
        contract=dict(parameters=[],outcomes={'success':dict(fields={},mode=['template'],action='cancel_flow')})
        synchronize(flow,contract=contract)
        self.assertEqual((flow.outputs[0].output_mode,flow.outputs[0].flow_action),('Template','Cancel Flow'))
        template=frappe.get_doc(dict(doctype='Bot Flow Template',
            template_name=self.prefix,language='es',enabled=1,body='Fixed greeting.')).insert()
        from messaging.communication.doctype.communication_template.communication_template import resolve_output
        self.assertEqual(resolve_output('Template',template_reference=template.name,data={})['text'],'Fixed greeting.')
        with self.assertRaises(frappe.ValidationError):
            resolve_output('Template',template_reference=template.name,data={},usage='Notification',account='wrong')




class TestDefaultLanguageIntegrity(ProcessFixtures, unittest.TestCase):
    languages = TestBotFlow.languages
    configured = TestBotFlow.configured
    template = TestBotFlow.template

    def test_flow_creation_requires_configured_language_and_exact_default_text(self):
        from messaging.chatbot.doctype.bot_settings import language
        flow = self.flow(show_in_menu=0)
        with patch.object(language, 'get_bot_settings', return_value=frappe.new_doc('Bot Settings')), patch(
            'messaging.chatbot.doctype.bot_flow.bot_flow.get_bot_settings', return_value=frappe.new_doc('Bot Settings')):
            with self.assertRaisesRegex(frappe.ValidationError, 'default language'):
                flow.insert()
        flow.translations[0].semantic_description = ''
        with self.assertRaisesRegex(frappe.ValidationError, 'Semantic Description'):
            flow.insert()
        flow.translations[0].semantic_description = 'Meaning'
        flow.append('translations', dict(language='en'))
        self.languages()
        flow.insert()
        self.assertFalse(flow.translations[1].semantic_description)

    def test_default_switch_lists_all_active_flow_fields_and_is_atomic(self):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        settings = self.languages()
        first, parameter = self.configured()
        first.outputs[0].output_mode = 'Template'
        template = self.template()
        first.append('output_translations', dict(language='es', status_code='success', template_reference=template.name))
        self.assertEqual(self.approve(first)['status'], 'Passed')
        second = self.flow('alternate_flow', show_in_menu=1)
        self.assertEqual(self.approve(second)['status'], 'Passed')
        for key in MESSAGE_KEYS:
            settings.append('message_translations', dict(message_key=key, language='en', message='English ' + key.replace('_', ' ')))
        settings.save()
        settings.chatbot_languages[0].is_default = 0
        settings.chatbot_languages[1].is_default = 1
        with self.assertRaises(frappe.ValidationError) as error:
            settings.save()
        message = str(error.exception)
        for field in ('Semantic Description', 'Initial Input Message', 'Continuation Input Message', 'success', 'Menu Label', parameter.name):
            self.assertIn(field, message)
        self.assertIn('en', message)
        persisted = frappe.get_single('Bot Settings')
        self.assertTrue(persisted.chatbot_languages[0].is_default)
        for flow in (first, second):
            self.activate(flow, False)
        settings.reload()
        settings.chatbot_languages[0].is_default = 0
        settings.chatbot_languages[1].is_default = 1
        settings.save()
        self.assertTrue(frappe.get_single('Bot Settings').chatbot_languages[1].is_default)
        with self.assertRaisesRegex(frappe.ValidationError, 'language en'):
            self.activate(first, True)
        self.assertEqual(frappe.db.get_value('Bot Flow', first.name, 'status'), 'Disabled')

    def test_complete_new_default_keeps_active_flows_and_optional_languages(self):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        settings = self.languages()
        flow = self.flow(show_in_menu=0)
        flow.append('translations', dict(language='en', semantic_description='English meaning'))
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        for key in MESSAGE_KEYS:
            settings.append('message_translations', dict(message_key=key, language='en', message='English ' + key.replace('_', ' ')))
        settings.chatbot_languages[0].is_default = 0
        settings.chatbot_languages[1].is_default = 1
        settings.save()
        self.assertEqual(frappe.db.get_value('Bot Flow', flow.name, 'status'), 'Enabled')
        self.assertEqual(resolve_process_language(), 'en')

    def test_conditional_text_and_optional_template_rows(self):
        from messaging.chatbot.doctype.bot_flow.localization import default_language_issues
        self.languages()
        flow = self.flow(show_in_menu=0)
        flow.translations[0].menu_label = ''
        flow.append('translations', dict(language='en'))
        flow.append('input_translations', dict(language='en'))
        self.assertFalse(default_language_issues(flow, 'es'))
        flow.show_in_menu = 1
        self.assertIn('Menu Label', '\n'.join(default_language_issues(flow, 'es')))
        flow.show_in_menu = 0
        flow.use_custom_internal_error_message = 1
        self.assertIn('Custom Internal Error Message', '\n'.join(default_language_issues(flow, 'es')))
        flow.use_custom_internal_error_message = 0
        flow.outputs[0].output_mode = 'Template'
        flow.append('output_translations', dict(language='es', status_code='success', template_reference=frappe.get_doc(dict(doctype='Bot Flow Template', template_name=self.prefix, language='es', enabled=1, body='Localized result')).insert().name))
        flow.append('output_translations', dict(language='en', status_code='success'))
        flow.insert()
        with presentation_language('en'):
            self.assertEqual(resolve_flow_output(flow, dict(version=1, status='success', data={'language':'en'}, message='', recipient=''))['rendered_message'], 'Localized result')
            self.assertEqual(resolve_process_language(), 'en')
