"""Process contract, discovery, registration and execution boundary regressions."""
from dataclasses import FrozenInstanceError
import json
import socket
import subprocess
import signal
import time
import unittest
from unittest.mock import patch

import frappe

from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.registry.processes import BotProcess, BotRequest, BotResponse, NotifyNoResponse, NotifyProcess, NotifyRequest, NotifyResponse, ProcessReference, _freeze
from messaging.registry.processes import register_processes
from messaging.communication.doctype.process_definition import execution as process, validation as testing
from messaging.communication.doctype.process_definition.execution import _ProcessContext
from messaging.registry.processes.discovery import discover_contract
from messaging.communication.doctype.process_definition.execution import run_handler
from messaging.communication.doctype.process_definition.process_definition import definition_revision


def execution_context(process_id, process_type, language, test, arguments, **kwargs):
    """Synthetic request context for direct runtime boundary tests."""
    source = kwargs.get('_source')
    if process_type == 'notification':
        request = NotifyRequest(params=arguments, language=language or None,
            event=kwargs.get('event') or None,
            source=ProcessReference(source['doctype'], source['name']) if source else None)
    else:
        request = BotRequest(params=arguments, language=language or None)
    return _ProcessContext(process_id, process_type, language, test, arguments, _request=request, **kwargs)


def technical_flow(*, params, outcomes):
    outcomes.register('success', fields={'language': 'string'}, mode=['message', 'template'], action='complete_flow')


def technical_business(request: BotRequest) -> BotResponse:
    return BotResponse('success', data={'language': request.language}, message='Technical result')


def alternate_flow(*, params, outcomes):
    outcomes.register('success', fields={}, mode=['message', 'template'], action='complete_flow')


def alternate_business(request: BotRequest) -> BotResponse:
    return BotResponse('success', message='Alternate result')


def negative_flow(*, params, outcomes):
    outcomes.register('not_found', fields={}, mode=['message', 'template'], action='complete_flow')


def negative_business(request: BotRequest) -> BotResponse:
    return BotResponse('not_found', message='Technical absence')


def technical_notification(*, params, outcomes):
    outcomes.register('send', fields={'recipient': 'string'}, mode=['message', 'template'])
    outcomes.register('skip', fields={}, mode=['message', 'template'])


def notification_business(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyResponse('send', data={'recipient':'technical-recipient'}, message=request.source.get_document()['description'])


def technical_skip(*, params, outcomes):
    outcomes.register('skip', fields={}, mode=['message', 'template'])


def skip_business(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyResponse('skip')


def typed_flow(*, params, outcomes):
    params.register('contracts', type='string', multiple=True, required=False)
    params.register('query_date', type='date', multiple=False, required=False)
    params.register('limit', type='integer', multiple=False, required=False)
    outcomes.register('success', fields={'contract':'string','count':'integer'}, mode=['message', 'template'], action='complete_flow')
    outcomes.register('not_found', fields={}, mode=['message', 'template'], action='complete_flow')


def typed_business(request: BotRequest) -> BotResponse:
    return BotResponse('success', data={'contract':request.params['contracts'][0], 'count':request.params['limit']}, message=request.params['query_date'])


def unsafe_database(*, params, outcomes):
    frappe.db.sql("UPDATE `tabToDo` SET description='forbidden' WHERE name='absent'")


def unsafe_read(*, params, outcomes):
    frappe.get_all('ToDo')


def unsafe_network(*, params, outcomes):
    socket.create_connection(('invalid.example', 443))


def unsafe_callback(*, params, outcomes):
    frappe.db.after_commit.add(lambda: None)


def unsafe_notification(*, params, outcomes):
    frappe.sendmail(recipients=['invalid@example.test'], message='Forbidden')


def unsafe_effect(*, params, outcomes):
    from messaging.communication.doctype.process_definition.execution import effect_boundary
    with effect_boundary({'ToDo':['create']}):
        frappe.get_doc(dict(doctype='ToDo', description='forbidden')).insert()


def wrong_signature(*, language, test=False):
    raise AssertionError('Must never execute')


def discovery_only_flow(*, params, outcomes):
    outcomes.register('success', fields={}, mode=['message', 'template'], action='complete_flow')


def discovery_only_business(request: BotRequest) -> BotResponse:
    raise AssertionError('Discovery must never execute this callback')


def idle_bot(request: BotRequest) -> BotResponse:
    raise AssertionError('handler must not run')


def idle_notify(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    raise AssertionError('handler must not run')


def incomplete_contract(*, params, outcomes):
    return None


def invalid_notification(*, params, outcomes):
    outcomes.register('send', fields={'recipient': 'integer'}, mode=['message', 'template'])


def configure_test_settings():
    """Local copy so this handler module does not import the Bot Settings test module."""
    from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings
    from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
    settings = get_bot_settings()
    settings.set('chatbot_languages', [dict(language='es', enabled=1, is_default=1)])
    settings.set('message_translations', [])
    for key in MESSAGE_KEYS:
        row = next((r for r in settings.message_translations if r.message_key == key and r.language == 'es'), None)
        if row is None:
            row = settings.append('message_translations', dict(message_key=key, language='es'))
        row.message = 'Synthetic ' + key.replace('_', ' ') + '.'
    settings.update(dict(max_invalid_attempts=3,
        interpretation_enabled=0, active_ai_configuration=None,
        human_inactivity_minutes=60, human_requests_required=1,
        registration_enabled=0, greeting_enabled=0, human_attention_enabled=0,
        registration_flow=None, greeting_flow=None, human_flow=None))
    for table in ('registration_states', 'registration_fields', 'greeting_states', 'human_transfer_states', 'human_fields', 'human_support_week'):
        settings.set(table, [])
    settings.save()
    return settings


def empty_settings():
    from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings
    settings = get_bot_settings()
    for field in settings.meta.fields:
        if field.fieldtype in ('Table', 'Table MultiSelect'):
            settings.set(field.fieldname, [])
        elif field.fieldtype not in ('Tab Break', 'Section Break', 'Column Break', 'HTML', 'Button'):
            settings.set(field.fieldname, 0 if field.fieldtype == 'Check' else None)
    return settings


def invalid_discovery_schema(*, params, outcomes):
    outcomes.register('success', fields={}, mode=['message', 'template'], action='complete_flow')
    response = {}
    response['data'] = {'nested': {}}
    return response


DECLARATION_HANDLERS = {
    'technical_flow': 'technical_business',
    'alternate_flow': 'alternate_business',
    'negative_flow': 'negative_business',
    'technical_notification': 'notification_business',
    'technical_skip': 'skip_business',
    'typed_flow': 'typed_business',
    'discovery_only_flow': 'discovery_only_business',
    'incomplete_contract': 'idle_bot',
    'invalid_notification': 'idle_notify',
    'invalid_discovery_schema': 'idle_bot',
    'wrong_signature': 'idle_bot',
    'unsafe_database': 'idle_bot',
    'unsafe_read': 'idle_bot',
    'unsafe_network': 'idle_bot',
    'unsafe_callback': 'idle_bot',
    'unsafe_notification': 'idle_bot',
    'unsafe_effect': 'idle_bot',
    'catalogue_notification': 'catalogue_notification_business',
    'component_notification': 'prepare_component_notification',
    'async_probe_handler': 'async_probe_result',
    'dialogue_handler': 'dialogue_result',
    'configurable_contract': 'configurable_business',
    'configurable_notice': 'configurable_notice_business',
    'configurable_output_contract': 'configurable_output_business',
    'request_probe': 'observe_request',
    'multiple_notification': 'prepare_multiple_notification',
    'empty_notification': 'prepare_empty_notification',
}


class ProcessFixtures:
    def setUp(self):
        self.original_session = frappe._dict(frappe.session)
        frappe.set_user('Administrator')
        frappe.session.sid = 'registry-test-' + frappe.generate_hash(length=16)
        frappe.db.savepoint('registry_test')
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('External network forbidden'))
        self.network_mock = self.network.start()
        self.prefix = 'technical_' + frappe.generate_hash(length=8)
        self.definitions = []
        for name in frappe.get_all('Bot Flow', filters={'status': 'Enabled'}, pluck='name'):
            frappe.db.set_value('Bot Flow', name, 'status', 'Disabled', update_modified=False)
        settings = configure_test_settings()

    def tearDown(self):
        frappe.set_user('Administrator')
        frappe.db.rollback(save_point='registry_test')
        self.network.stop()
        frappe.local.session = self.original_session
        frappe.clear_last_message()
        frappe.local.notification_rules_changed = True

    def register(self, function='technical_flow', kind='bot_flow', identifier=None, label='Technical Process'):
        identifier = identifier or 'messaging.' + self.prefix + '_' + function
        declaration = (NotifyProcess if kind == 'notification' else BotProcess)(
            id=identifier, label=label, definition=globals()[function],
            handler=globals()[DECLARATION_HANDLERS[function]])
        self.definitions = [d for d in self.definitions if d.id != identifier] + [declaration]
        register_processes(app='messaging', processes=self.definitions)
        return identifier

    def flow(self, function='technical_flow', **values):
        identifier = self.register(function)
        doc = frappe.new_doc('Bot Flow')
        doc.update(dict(workflow_code=self.prefix + '_' + frappe.generate_hash(length=5), workflow_name='Technical Flow',
            semantic_description='Isolated runtime verification', priority=0, status='Untested',
            process_definition=identifier,
            outputs=[dict(status_code='not_found' if function == 'negative_flow' else 'success', output_mode='Message')], **values))
        from messaging.chatbot.doctype.bot_flow.contract import synchronize
        from messaging.chatbot.doctype.bot_settings.bot_settings import resolve_process_language
        doc.append('translations', dict(language=resolve_process_language(), menu_label='Technical option',
            semantic_description=doc.semantic_description))
        synchronize(doc)
        return doc

    def approve(self, doc, values=None):
        from messaging.communication.doctype.process_definition import approval
        from messaging.chatbot.doctype.bot_flow import bot_flow
        from messaging.communication.doctype.notification_rule import notification_rule

        owner = bot_flow if doc.doctype == 'Bot Flow' else notification_rule
        doc.save()
        if approval.is_current(doc):
            result = dict(status='Passed', execution_log=doc.process_test_log)
        else:
            result = owner.validate_process(doc.as_dict())
            doc.reload()
        if result['status'] == 'Passed' and doc.status == 'Disabled':
            owner.set_active(doc.as_dict(), 1)
            doc.reload()
        return result

    def activate(self, doc, active=True):
        from messaging.chatbot.doctype.bot_flow.bot_flow import set_active
        result = set_active(doc.as_dict(), int(active))
        doc.reload()
        return result

    def notification(self, function='technical_notification', conditions=None):
        from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        sync_communication_providers()
        set_provider_enabled('EMULR', True)
        if not frappe.db.exists('Communication Channel', 'emulator'):
            channel = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR',
                channel_name='Technical emulator channel')).insert()
            set_channel_status(channel.name, 'Enabled')
        source = frappe.get_doc(dict(doctype='ToDo', description='Controlled framework document', status='Open')).insert()
        contact = frappe.get_doc(dict(doctype='Emulator Contact', display_name='Technical recipient')).insert()
        account = frappe.get_doc(dict(doctype='Communication Channel Account', channel='emulator',
            account_id=self.prefix, account_name='Technical test account', enabled=1)).insert()
        rule = frappe.new_doc('Notification Rule')
        rule.update(dict(rule_name='Technical rule ' + self.prefix, notification_delay_minutes=1,
            process_definition=self.register(function, 'notification'),
            source_application='frappe', source_doctype='ToDo', account=account.name, content_type='Text',
            events=conditions or [dict(document_event='on_update',field_name='status',value='"Open"')],
            parameters=[dict(parameter_key='notification_contact',value=json.dumps(contact.name))]))
        return rule, source

    def execute(self, flow, **kwargs):
        return process.execute_process(process_method='untrusted.browser.path',arguments={},source_type='Bot Flow',source_name=flow.name,**kwargs)


class TestProcessAvailability(ProcessFixtures, unittest.TestCase):
    def test_desk_status_preserves_activation_without_retired_checkboxes(self):
        from frappe.desk.form.load import getdoc
        from messaging.communication.doctype.process_definition import process_definition as catalogue
        for active, function in ((True, 'technical_flow'), (False, 'alternate_flow')):
            flow = self.flow(function, show_in_menu=0)
            self.approve(flow)
            if active:
                self.activate(flow)
            else:
                self.activate(flow, False)
            administrative_status = 'Enabled' if active else 'Disabled'
            with patch('messaging.security.permissions.installed_applications', return_value=['frappe']):
                frappe.response.docs = []
                getdoc('Bot Flow', flow.name)
                shown = next(doc for doc in frappe.response.docs if doc.doctype == 'Bot Flow')
                self.assertEqual(shown.status, 'Unavailable')
                self.assertNotIn('enabled', shown.as_dict())
                self.assertEqual(frappe.db.get_value('Bot Flow', flow.name, 'status'), administrative_status)
                frappe.response.docs = []
                getdoc('Process Definition', flow.process_definition)
                shown = next(doc for doc in frappe.response.docs if doc.doctype == 'Process Definition')
                self.assertEqual(shown.status, 'Unavailable')
                self.assertNotIn('available', shown.as_dict())
            flow.reload()
            self.assertEqual(flow.lifecycle_status, administrative_status)

    def test_uninstall_hook_preserves_definitions_consumers_and_is_idempotent(self):
        from messaging.communication.doctype.process_definition import process_definition as catalogue
        flow = self.flow()
        self.approve(flow)
        self.activate(flow)
        before_flow = frappe.db.get_value('Bot Flow', flow.name, '*', as_dict=True)
        definition = frappe.get_doc('Process Definition', flow.process_definition)
        before_definition = definition.as_dict()
        # The real installer is exercised separately on an isolated site. This
        # transaction test verifies the exact writes without committing fixtures.
        with patch.object(frappe.db, 'commit') as commit:
            catalogue.after_app_uninstall('messaging')
            commit.assert_not_called()
            with patch('messaging.security.permissions.installed_applications', return_value=['frappe']):
                catalogue.after_app_uninstall('messaging')
                commit.assert_called_once()
                commit.reset_mock()
                catalogue.after_app_uninstall('messaging')
                commit.assert_not_called()
        definition.reload()
        self.assertEqual(definition.status, 'Unavailable')
        for key in ('name', 'creation', 'method', 'capability_revision', 'contract_version', 'application'):
            self.assertEqual(definition.get(key), before_definition[key])
        self.assertEqual(before_flow, frappe.db.get_value('Bot Flow', flow.name, '*', as_dict=True))
        self.assertTrue(frappe.db.exists('Process Execution Log', flow.process_test_log))
        self.assertEqual(definition.lifecycle_status, 'Unavailable')
        self.assertEqual(flow.lifecycle_status, 'Unavailable')
        # Re-registration belongs to the owner. Restoring the identical contract
        # must not discard its previous approval just because it was absent.
        register_processes(app='messaging', processes=self.definitions)
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertEqual(testing.test_state(flow), 'Passed')

    def test_disabled_flow_recovers_disabled_and_changed_contract_needs_approval(self):
        from messaging.communication.doctype.process_definition import process_definition as catalogue
        flow = self.flow(show_in_menu=0)
        self.approve(flow)
        self.activate(flow, False)
        before = frappe.db.get_value('Bot Flow', flow.name, '*', as_dict=True)
        self.assertEqual(flow.lifecycle_status, 'Disabled')
        with patch.object(frappe.db, 'commit'), patch('messaging.security.permissions.installed_applications', return_value=['frappe']):
            catalogue.after_app_uninstall('messaging')
            self.assertEqual(flow.lifecycle_status, 'Unavailable')
            self.assertEqual(testing.test_state(flow, remember_invalid=True), 'Unavailable')
        register_processes(app='messaging', processes=self.definitions)
        self.assertEqual(flow.lifecycle_status, 'Disabled')
        self.assertEqual(before, frappe.db.get_value('Bot Flow', flow.name, '*', as_dict=True))
        self.register('alternate_flow', identifier=flow.process_definition)
        self.assertEqual(flow.lifecycle_status, 'Untested')
        self.assertEqual(flow.status, 'Disabled')

    def test_missing_owner_blocks_warm_cache_and_execution_without_hook(self):
        from messaging.communication.doctype.process_definition.process_definition import resolve_definition
        flow = self.flow()
        self.approve(flow)
        self.activate(flow)
        self.assertIn('messaging', frappe.get_installed_apps())
        installed = [app for app in frappe.get_installed_apps() if app != 'messaging']
        frappe.db.set_value('DefaultValue', {'parent': '__global', 'defkey': 'installed_apps'},
            'defvalue', json.dumps(installed))
        # Simulate a long-lived request/worker retaining Frappe's installed list.
        with patch.object(frappe, 'get_installed_apps', return_value=installed + ['messaging']), patch.object(
                process, 'run_handler', side_effect=AssertionError('Absent owner must not run')) as handler:
            with self.assertRaises(frappe.ValidationError):
                resolve_definition(flow.process_definition, expected_type='bot_flow')
            result = self.execute(flow)
            self.assertFalse(result['functional'])
            handler.assert_not_called()
        self.assertEqual(frappe.db.get_value('Process Definition', flow.process_definition, 'status'), 'Enabled')

    def test_unavailable_flow_is_excluded_before_queue_capture_and_catalogues(self):
        from messaging.communication.doctype.process_definition import process_definition as catalogue
        from messaging.conversation.doctype.chat_session import chat_session as sessions
        from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings, get_menu_flows
        from messaging.chatbot.doctype.bot_settings.interpretation import catalog
        flow = self.flow(show_in_menu=1)
        flow.translations[0].menu_label = 'Technical option'
        self.approve(flow)
        self.activate(flow)
        self.assertIn(flow.name, catalog())
        self.assertIn(flow.name, {row['workflow_code'] for row in get_menu_flows()})
        state = dict(queue=[], values={})
        sessions.enqueue(state, [flow.name], get_bot_settings())
        self.assertEqual(len(state['queue']), 1)
        with patch('messaging.security.permissions.installed_applications', return_value=['frappe']):
            self.assertNotIn(flow.name, catalog())
            self.assertNotIn(flow.name, {row['workflow_code'] for row in get_menu_flows()})
            empty = dict(queue=[], values={})
            sessions.enqueue(empty, [flow.name], get_bot_settings())
            self.assertEqual(empty['queue'], [])
            session = frappe._dict(active_flow=flow.name, status='Collecting')
            turn = frappe._dict(decisions='[]', executions='[]')
            with patch.object(sessions, 'capture', side_effect=AssertionError('No capture after suspension')):
                sessions.advance(None, session, turn, state, {}, get_bot_settings(), None)
            self.assertEqual(state['queue'], [])


class TestProcessContract(ProcessFixtures, unittest.TestCase):
    def definition(self, function='technical_flow'):
        return frappe.get_doc('Process Definition', self.register(function))

    def test_web_discovery_uses_bounded_child_and_never_calls_business(self):
        from messaging.registry.processes import discovery as registry
        definition = self.definition('discovery_only_flow')
        real_run = subprocess.run
        with patch.object(registry.threading, 'current_thread', return_value=object()), patch.object(
                registry.subprocess, 'run', wraps=real_run) as isolated:
            contract, callback = discover_contract(definition, language='en')
        self.assertEqual(contract, {'parameters': [], 'outcomes': {'success': {'fields': {}, 'mode': ['message', 'template'], 'action': 'complete_flow'}}})
        self.assertIs(callback, discovery_only_business)
        self.assertEqual(isolated.call_args.kwargs['timeout'], process.PROCESS_TIMEOUT_SECONDS)

    def test_registered_metadata_reads_are_independent_and_need_no_discovery(self):
        from messaging.registry.processes import discovery as registry
        from messaging.communication.doctype.process_definition.process_definition import registered_contract
        definition = self.definition('discovery_only_flow')
        with patch.object(registry, '_discover_contract', side_effect=AssertionError('Read discovery')):
            first = registered_contract(definition)
            second = registered_contract(definition)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)

    def test_discovery_outage_does_not_affect_registered_approvals(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import state, set_active
        from messaging.registry.processes import discovery as registry
        flow = self.flow(); self.approve(flow)
        with patch.object(registry, '_discover_contract', side_effect=subprocess.TimeoutExpired('discovery',30)):
            flow.onload()
            self.assertEqual(flow.get_onload('flow_lifecycle')['lifecycle_status'],'Enabled')
            self.assertEqual(state(flow.name)['lifecycle_status'],'Enabled')
            self.activate(flow,False)
            self.assertEqual(set_active(flow.as_dict(),1)['lifecycle_status'],'Enabled')

    def test_unreadable_discovery_response_is_rejected_without_approval(self):
        from messaging.registry.processes import discovery as registry
        definition = self.definition('discovery_only_flow')
        for output in ('null', '[]', 'not json', '{}'):
            with self.subTest(output=output), patch.object(registry.threading, 'current_thread', return_value=object()), patch.object(
                    registry.subprocess, 'run', return_value=subprocess.CompletedProcess(['discovery'], 0, stdout=output)):
                with self.assertRaisesRegex(frappe.ValidationError, 'could not be verified'):
                    discover_contract(definition)

    def test_business_deadline_still_rejects_web_threads_and_restores_timer(self):
        with patch.object(process.threading, 'current_thread', return_value=object()):
            with self.assertRaisesRegex(RuntimeError, 'deadline support'):
                with process.execution_deadline():
                    self.fail('Unsupported worker must not enter business execution')
        previous = signal.getsignal(signal.SIGALRM)
        with patch.object(process, 'PROCESS_TIMEOUT_SECONDS', 0.02):
            with self.assertRaises(TimeoutError):
                with process.execution_deadline():
                    while True:
                        pass
        self.assertIs(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0.0, 0.0))

    def test_single_handler_and_result_defaults_are_isolated(self):
        definition = BotProcess(id='messaging.technical', label='Technical', definition=technical_flow, handler=technical_business)
        with self.assertRaises(FrozenInstanceError):
            definition.handler = alternate_flow
        with self.assertRaises(TypeError):
            BotProcess(id='messaging.technical', label='Technical', type='bot_flow', reader=technical_flow)
        first, second = BotResponse('success'), BotResponse('success')
        first.data['private'] = 'value'
        self.assertEqual(second.data, {})
        for status in ('internal_error', '', None, 'bad status'):
            with self.subTest(status=status), self.assertRaises(frappe.ValidationError):
                BotResponse(status)
        for value in ({'bad':[]}, {'bad':float('nan')}):
            with self.assertRaises(frappe.ValidationError):
                BotResponse('success', data=value)

    def test_registration_and_discovery_work_without_chatbot_languages(self):
        from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings, resolve_process_language
        before_parameters = frappe.db.count('Bot Parameter')
        before_flows = frappe.db.count('Bot Flow')
        settings = empty_settings()
        settings.human_requests_required = 1
        with patch.object(settings, 'get_doc_before_save', return_value=None):
            settings.save()
        definition = self.definition('typed_flow')
        contract, callback = discover_contract(definition)
        self.assertEqual(callback, typed_business)
        self.assertEqual(len(contract['parameters']), 3)
        self.assertFalse(get_bot_settings().chatbot_languages)
        self.assertEqual(frappe.db.count('Bot Parameter'), before_parameters)
        self.assertEqual(frappe.db.count('Bot Flow'), before_flows)
        with self.assertRaises(frappe.ValidationError):
            resolve_process_language()

    def test_discovery_collects_every_declaration_without_values_or_business_execution(self):
        definition = self.definition('typed_flow')
        # Mutating helper behavior without replacing its function reference keeps ownership verifiable.
        contract, callback = discover_contract(definition, language='en')
        self.assertEqual(callback, typed_business)
        self.assertEqual([item['type'] for item in contract['parameters']], ['string','date','integer'])
        self.assertEqual(set(contract['outcomes']), {'success','not_found'})
        context = execution_context(definition.process_id, 'bot_flow','en',True,_freeze({}))
        context.declare(typed_flow)
        self.assertEqual(context.response(), dict(version=1,status='test',recipient='',data={},message=''))
        self.assertFalse(hasattr(context.params, '__getitem__'))

    def test_input_declarations_reject_duplicates_names_types_and_multiplicity(self):
        from messaging.communication.doctype.process_definition.validation import _input_definitions
        good = dict(name='contracts', type='string', multiple=True, required=False)
        for items in ([good,good], [dict(good,name='_bad')], [dict(good,type='Expression')],
            [dict(good,multiple=1)], [dict(good,unknown=True)], {'contracts':'string'}):
            with self.subTest(items=items), self.assertRaises(frappe.ValidationError):_input_definitions(items)

    def test_outputs_reject_invalid_duplicate_reserved_and_non_scalar_types(self):
        context = execution_context('messaging.test','bot_flow','en',True,{})
        for status, fields in [('test',{}), ('internal_error',{}), ('Bad Status',{}),
            ('success',{'field':'object'}), ('success',{'_field':'string'}), ('success',[])]:
            with self.subTest(status=status,fields=fields), self.assertRaises(frappe.ValidationError):
                context.outcomes.register(status, fields=fields, mode=['message'], action='complete_flow')
        context.outcomes.register('success', fields={}, mode=['message'], action='complete_flow')
        with self.assertRaises(frappe.ValidationError):
            context.outcomes.register('success', fields={}, mode=['message'], action='complete_flow')

    def test_real_callback_is_called_once_with_normalized_values_and_language(self):
        definition = self.definition('typed_flow')
        values = {'contracts':['00125'], 'query_date':'2026-11-01', 'limit':3}
        context = execution_context(definition.process_id,'bot_flow','en',False,_freeze(values))
        response = run_handler(definition,context,{})
        self.assertEqual(response, dict(version=1,status='success',recipient='',data={'contract':'00125','count':3},message='2026-11-01'))
        with self.assertRaises(frappe.ValidationError):context._bind_handler(typed_business)
        self.assertEqual(run_handler(definition, execution_context(definition.process_id,'bot_flow','es',False,_freeze(values)),{}),response)

    def test_real_execution_rejects_unknown_status_extra_and_wrong_typed_fields(self):
        cases = [BotResponse('unknown'), BotResponse('success',{'value':1}),
            BotResponse('success',{'value':'ok','private':'other'}), {'status':'success'}]
        for value in cases:
            def declaration(*, params, outcomes):
                outcomes.register('success',fields={'value':'string'},mode=['message'],action='complete_flow')
            context = execution_context('messaging.test','bot_flow','en',False,{},_invoke=lambda fn,request:fn(request))
            context.declare(declaration)
            context._bind_handler(lambda request: value)
            with self.subTest(value=value), self.assertRaises(frappe.ValidationError):context.response()

    def test_real_input_type_checks(self):
        definition = self.definition('typed_flow')
        good = dict(contracts=['00125'], query_date='2026-11-01', limit=1)
        for change in (dict(contracts='00125'),dict(contracts=[]),dict(limit=True),dict(limit='1'),
            dict(query_date='2026-02-30'),dict(extra='value')):
            with self.subTest(change=change), self.assertRaises(frappe.ValidationError):
                run_handler(definition,execution_context(definition.process_id,'bot_flow','en',False,_freeze(dict(good,**change))),{})

    def test_discovery_blocks_database_reads_writes_callbacks_network_mail_and_effects(self):
        before = frappe.db.count('ToDo')
        for name in ('unsafe_database','unsafe_read','unsafe_network','unsafe_callback','unsafe_notification','unsafe_effect'):
            definition = frappe._dict(application='messaging',process_id='messaging.test',process_type='bot_flow',
                definition_method=__name__+'.'+name, method=__name__+'.idle_bot')
            with self.subTest(name=name), self.assertRaises((frappe.PermissionError,frappe.ValidationError)):
                discover_contract(definition,language='en')
        self.assertEqual(frappe.db.count('ToDo'),before)
        self.network_mock.assert_not_called()

    def test_registry_owner_guard_idempotence_identity_and_no_business_execution(self):
        initial = frappe.db.count('Process Execution Log')
        definition = self.definition()
        self.assertEqual(register_processes(app='messaging',processes=self.definitions)['unchanged'],1)
        self.assertEqual(frappe.get_doc('Process Definition',definition.name).modified,definition.modified)
        self.assertEqual(frappe.db.count('Process Execution Log'),initial)
        self.assertEqual(definition.contract_version,1)
        definition.status = 'Unavailable'
        with self.assertRaises(frappe.PermissionError):
            definition.save()
        with self.assertRaises(frappe.PermissionError):
            frappe.delete_doc('Process Definition',definition.name)
        with self.assertRaises(frappe.PermissionError):
            frappe.rename_doc('Process Definition',definition.name,definition.name+'_other')
        with self.assertRaises(frappe.ValidationError):
            register_processes(app='messaging',processes=[self.definitions[0],self.definitions[0]])
        self.assertEqual(frappe.get_doc('Process Definition',definition.name).status,'Enabled')

    def test_registry_rejects_invalid_batch_before_creating_updating_or_retiring(self):
        definition = self.definition('discovery_only_flow')
        before = {row.name: row for row in frappe.get_all('Process Definition', fields=['*'])}
        counts = {dt: frappe.db.count(dt) for dt in ('Process Execution Log', 'Bot Flow', 'Notification Queue', 'ToDo')}
        valid = BotProcess(id=definition.name, label='Changed label', definition=discovery_only_flow, handler=discovery_only_business)
        new = BotProcess(id='messaging.' + self.prefix + '_new', label='New', definition=technical_flow, handler=technical_business)
        for handler, kind in ((incomplete_contract, 'bot_flow'), (invalid_notification, 'notification'),
            (invalid_discovery_schema, 'bot_flow'), (unsafe_read, 'bot_flow'), (wrong_signature, 'bot_flow')):
            cls = NotifyProcess if kind == 'notification' else BotProcess
            business = globals()[DECLARATION_HANDLERS[handler.__name__]]
            with self.subTest(handler=handler.__name__), self.assertRaises(frappe.ValidationError):
                invalid = cls(id='messaging.' + self.prefix + '_invalid', label='Invalid', definition=handler, handler=business)
                register_processes(app='messaging', processes=[valid, new, invalid])
            self.assertEqual(before, {row.name: row for row in frappe.get_all('Process Definition', fields=['*'])})
        self.assertEqual(counts, {dt: frappe.db.count(dt) for dt in counts})
        self.network_mock.assert_not_called()

    def test_notification_declarations_reject_incompatible_schema_during_discovery(self):
        for status, fields in [('send', {'recipient': 'integer'}), ('skip', {'recipient': 'nullable_string'}), ('skip', {'action': 'nested'})]:
            context = execution_context('messaging.test', 'notification', '', True, {})
            with self.subTest(status=status, fields=fields), self.assertRaises(frappe.ValidationError):
                context.outcomes.register(status, fields=fields, mode=['message'])

    def test_web_registration_preserves_structural_diagnostic(self):
        with patch('messaging.registry.processes.discovery.threading.current_thread', return_value=object()):
            with self.assertRaisesRegex(frappe.ValidationError, 'Notification recipient'):
                register_processes(app='messaging', processes=[NotifyProcess(id='messaging.' + self.prefix + '_invalid',
                    label='Invalid notification', definition=invalid_notification, handler=idle_notify)])

    def test_registry_rolls_back_catalogue_when_persistence_fails(self):
        from messaging.communication.doctype.process_definition.process_definition import ProcessDefinition
        definition = self.definition('discovery_only_flow')
        before = {row.name: row for row in frappe.get_all('Process Definition', fields=['*'])}
        declarations = [BotProcess(id=definition.name, label='Changed label', definition=discovery_only_flow, handler=discovery_only_business),
            BotProcess(id='messaging.' + self.prefix + '_new', label='New', definition=technical_flow, handler=technical_business)]
        original = ProcessDefinition.save
        calls = []
        def save(doc, *args, **kwargs):
            calls.append(doc.name)
            if len(calls) == 2:
                raise frappe.ValidationError('Controlled persistence failure')
            return original(doc, *args, **kwargs)
        with patch.object(ProcessDefinition, 'save', autospec=True, side_effect=save):
            with self.assertRaisesRegex(frappe.ValidationError, 'Controlled persistence failure'):
                register_processes(app='messaging', processes=declarations)
        self.assertEqual(len(calls), 2)
        self.assertEqual(before, {row.name: row for row in frappe.get_all('Process Definition', fields=['*'])})

    def test_administrative_validation_requires_evidence_and_enables_flow(self):
        flow = self.flow()
        flow.process_test_status = 'Passed'
        flow.insert()
        self.assertEqual(testing.test_state(flow), 'Untested')
        self.assertEqual(flow.status, 'Disabled')
        result = self.approve(flow)
        self.assertEqual(result['status'], 'Passed', result)
        self.assertEqual(flow.lifecycle_status, 'Enabled')
        self.assertEqual(testing.test_state(flow), 'Passed')
        result = self.execute(flow)
        self.assertTrue(result['functional'], result)
        self.assertEqual(result['text'], 'Technical result')
        log = frappe.get_doc('Process Execution Log', flow.process_test_log)
        self.assertEqual(log.functional_status, 'test')
        self.assertEqual(json.loads(flow.process_test_evidence)['scope'], 'structural_contract')

    def test_validation_rejects_unsaved_and_dirty_flow_configurations(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process
        flow = self.flow()
        with self.assertRaises(frappe.ValidationError):
            validate_process(flow.as_dict())
        flow.insert()
        flow.workflow_name = 'An unsaved edit'
        with self.assertRaises(frappe.ValidationError):
            validate_process(flow.as_dict())
        with self.assertRaises(frappe.PermissionError):
            testing.test_method(flow.as_dict())

    def test_guest_cannot_discover_or_validate(self):
        candidate = self.flow().as_dict()
        frappe.set_user('Guest')
        from messaging.chatbot.doctype.bot_flow.contract import discover
        for operation in (lambda:discover(candidate),lambda:testing.test_method(candidate)):
            with self.assertRaises(frappe.PermissionError):
                operation()

    def test_notification_discovery_and_structural_approval_need_no_source_document(self):
        rule,source = self.notification()
        from messaging.communication.doctype.notification_rule.contract import synchronize
        synchronize(rule)
        template = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name='struct-' + self.prefix, language='es', body='Structural notification')).insert()
        for output in rule.outputs:
            output.send = 1 if output.status_code == 'send' else 0
            output.template_reference = template.name if output.status_code == 'send' else None
        initial=frappe.db.count('Notification Queue')
        result=self.approve(rule)
        self.assertEqual(result['status'],'Passed',result)
        self.assertEqual(testing.test_state(rule),'Passed')
        self.assertFalse(result['preparation_complete'])
        self.assertFalse(result['event_proven'])
        self.assertEqual(frappe.db.count('Notification Queue'),initial)

    def test_recreated_definition_cannot_reuse_an_old_approval(self):
        from messaging.communication.doctype.process_definition.process_definition import _registry_write
        from messaging.chatbot.doctype.bot_flow.bot_flow import set_active
        flow = self.flow()
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        old_log = flow.process_test_log
        old_definition = frappe.get_doc('Process Definition', flow.process_definition)
        old_code = flow.workflow_code
        frappe.delete_doc('Bot Flow', flow.name)
        with _registry_write():
            frappe.delete_doc('Process Definition', old_definition.name)
        self.register(identifier=old_definition.process_id)
        recreated = frappe.get_doc('Process Definition', old_definition.name)
        self.assertNotEqual(definition_revision(old_definition), definition_revision(recreated))
        self.assertTrue(frappe.db.exists('Process Execution Log', old_log))
        new_flow = self.flow()
        new_flow.workflow_code = old_code
        new_flow.process_test_status = 'Passed'
        new_flow.process_test_log = old_log
        new_flow.insert()
        self.assertEqual(new_flow.lifecycle_status, 'Untested')
        with self.assertRaises(frappe.ValidationError):
            set_active(new_flow.as_dict(), 1)
        self.assertEqual(self.approve(new_flow)['status'], 'Passed')
        self.assertNotEqual(new_flow.process_test_log, old_log)


def request_probe(*, params, outcomes):
    outcomes.register('observed', fields={'profile': 'nullable_string'}, mode=['message'], action='complete_flow')


def observe_request(request: BotRequest) -> BotResponse:
    assert not request.params
    assert request.language == 'es'
    assert not hasattr(request, 'source') and not hasattr(request, 'event')
    assert request.contact.doctype == 'Chat Contact'
    assert request.channel.doctype == 'Communication Channel'
    assert request.account.doctype == 'Communication Channel Account'
    assert request.session.doctype == 'Chat Session'
    assert request.message.doctype == 'Chat Message'
    assert not hasattr(request, 'flow')
    assert request.contact.profile_value('absent_key') is None
    return BotResponse('observed', data={'profile': request.contact.profile_value('synthetic_key')}, message='Observed')


def multiple_notification(*, params, outcomes):
    params.register('decision', type='string', required=False)
    params.register('value', type='string', required=True)
    outcomes.register('ready', fields={'recipient': 'string', 'value': 'string'}, mode=['message', 'template'])
    outcomes.register('quiet', fields={}, mode=['template', 'message'])
    outcomes.register('skip', fields={}, mode=['template'])


def prepare_multiple_notification(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    assert type(request.source) is ProcessReference
    assert request.source.doctype == 'ToDo' and request.source.name
    assert request.event == 'on_update'
    assert request.language == 'es'
    assert request.account.doctype == 'Communication Channel Account'
    assert request.channel.doctype == 'Communication Channel'
    assert not hasattr(request, 'session') and not hasattr(request, 'message') and not hasattr(request, 'contact')
    decision = request.params.get('decision', 'ready')
    if decision != 'ready':
        return NotifyResponse(decision)
    return NotifyResponse('ready', {'recipient': 'technical-recipient', 'value': request.params['value']},
        message=request.params['value'])


def empty_notification(*, params, outcomes):
    outcomes.register('ready', fields={}, mode=['template'])


def prepare_empty_notification(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    assert not request.params
    assert request.source.doctype == 'ToDo'
    return NotifyResponse('ready')


class TestUnifiedProcessAPI(ProcessFixtures, unittest.TestCase):
    def context(self, kind='bot_flow', values=None):
        request = NotifyRequest(language='es') if kind == 'notification' else BotRequest(language='es')
        return _ProcessContext('messaging.test', kind, 'es', False, values or {},
            _invoke=lambda fn, request: fn(request), _request=request)

    def test_named_injection_optional_parameters_and_no_return(self):
        called = []
        def business(request: BotRequest) -> BotResponse:
            called.append(request)
            return BotResponse('done')
        def declaration(*, params, outcomes):
            outcomes.register('done', fields={}, mode=['message', 'template'], action='cancel_flow')
            params.register('optional', type='integer', required=False)
        context = self.context()
        context.declare(declaration)
        self.assertFalse(called)
        context._bind_handler(business)
        self.assertEqual(context.response()['status'], 'done')
        self.assertIsInstance(called.pop(), BotRequest)
        self.assertEqual(self.context().params._definitions, [])

    def test_invalid_signatures_handlers_parameters_and_modes(self):
        from messaging.communication.doctype.process_definition.validation import validate_definition_signature, validate_handler_signature, _input_definitions
        for function in (lambda context: None, lambda outcomes: None, lambda *args: None, lambda **kwargs: None):
            with self.assertRaises(frappe.ValidationError): validate_definition_signature(function)
        for value in (None, [], ['message', 'message'], ['provider_template'], 'template', [1]):
            with self.subTest(mode=value), self.assertRaises(frappe.ValidationError):
                self.context().outcomes.register('done', fields={}, mode=value, action='complete_flow')
        for fields in ({'x': 'object'}, {'_private': 'string'}, []):
            with self.assertRaises(frappe.ValidationError):
                self.context().outcomes.register('done', fields=fields, mode=['template'], action='complete_flow')
        context = self.context()
        context.params.register('values', type='integer', multiple=True)
        with self.assertRaises(frappe.ValidationError):context.params.register('values', type='integer')
        for change in ({'type': 'Int'}, {'multiple': 1}, {'required': 1}, {'name': 'language'}):
            with self.assertRaises(frappe.ValidationError):
                _input_definitions([dict(name='value', type='string', multiple=False, required=True, **{}) | change])
        for handler in (None, lambda: None, lambda a, b: None):
            with self.assertRaises(frappe.ValidationError): validate_handler_signature(handler, 'bot_flow')
        context = self.context()
        context._bind_handler(idle_bot)
        with self.assertRaises(frappe.ValidationError):context._bind_handler(idle_bot)
        with self.assertRaises(frappe.ValidationError):
            self.context().outcomes.register('done', fields={}, mode=['message'], action='unknown')
        context = self.context('notification')
        context.outcomes.register('ready', fields={}, mode=['template'], action={'ignored': True})
        self.assertNotIn('action', context.outcomes._definitions['ready'])
        with self.assertRaises(frappe.ValidationError):context.outcomes.register('ready', fields={}, mode=['template'])

    def test_requests_keep_only_the_fields_each_consumer_uses(self):
        from dataclasses import fields
        bot = BotRequest(params={'codes': ['00123']})
        self.assertEqual([f.name for f in fields(bot)], ['params', 'contact', 'language', 'channel', 'account', 'session', 'message'])
        self.assertEqual(bot.params['codes'], ('00123',))
        for name in ('contact', 'language', 'channel', 'account', 'session', 'message'):
            self.assertIsNone(getattr(bot, name))
        with self.assertRaises(FrozenInstanceError):bot.language = 'en'
        with self.assertRaises(TypeError):bot.params['codes'] = ()
        notice = NotifyRequest(params={'codes': ['00123']})
        self.assertEqual([f.name for f in fields(notice)], ['params', 'language', 'source', 'event', 'channel', 'account'])
        reference = ProcessReference('ToDo', 'synthetic')
        with self.assertRaises(FrozenInstanceError):reference.name = 'other'
        with patch('frappe.get_doc', side_effect=AssertionError('No implicit document load')):
            self.assertEqual(NotifyRequest(source=reference).source.name, 'synthetic')
        with patch('frappe.get_doc', side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):reference.get_document()

    def test_results_required_parameters_and_flat_types_are_checked_once(self):
        for value in (BotResponse('unknown'), BotResponse('done'), BotResponse('done', {'count': True}),
                      BotResponse('done', {'count': 1, 'extra': 'x'}), {}):
            def declaration(*, params, outcomes):
                outcomes.register('done', fields={'count': 'integer'}, mode=['message'], action='complete_flow')
            context = self.context();context.declare(declaration)
            context._bind_handler(lambda request: value)
            with self.subTest(value=value), self.assertRaises(frappe.ValidationError):context.response()
        def declaration(*, params, outcomes):
            params.register('codes', type='string', multiple=True)
            outcomes.register('done', fields={}, mode=['template'], action='complete_flow')
        for values in ({}, {'codes': []}, {'codes': '00123'}, {'codes': ['00123'], 'extra': True}):
            context=self.context(values=values);context.declare(declaration)
            context._bind_handler(lambda request: BotResponse('done'))
            with self.assertRaises(frappe.ValidationError):context.response()
        context=self.context(values={'codes': ['00123']});context.declare(declaration)
        context._bind_handler(lambda request: BotResponse('done'))
        self.assertEqual(context.response()['data'], {})

    def notification_fixture(self):
        rule, source = self.notification()
        rule.process_definition = self.register('multiple_notification', 'notification')
        from messaging.communication.doctype.notification_rule.contract import synchronize
        synchronize(rule)
        for row in rule.process_parameters:
            if row.parameter_name == 'value':row.fixed_value='"Synthetic value"'
        template = frappe.get_doc(dict(doctype='Communication Template', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix + ' ready', language='es',
            body='Synthetic {{value}}', template_variables=[dict(parameter_name='value')])).insert()
        for row in rule.outputs:
            if row.status_code == 'ready':
                row.send, row.template_reference = 1, template.name
            else:
                row.send, row.template_reference = 0, None
        self.assertEqual(self.approve(rule)['status'], 'Passed')
        rule.enabled=1;rule.save()
        return rule, source

    def test_multiple_notification_outcomes_send_skip_and_persistence(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.events import prepare
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.common.primitives import utcnow
        rule,source = self.notification_fixture()
        before = frappe.db.count('Notification Queue')
        condition=canonical_conditions(rule)[0]
        for decision, expected in (('ready', 1), ('quiet', 0), ('skip', 0)):
            rule.enabled=0
            next(row for row in rule.process_parameters if row.parameter_name=='decision').fixed_value=json.dumps(decision)
            rule.save();self.assertEqual(self.approve(rule)['status'],'Passed');rule.enabled=1;rule.save()
            before_case=frappe.db.count('Notification Queue')
            key='synthetic-'+frappe.generate_hash(length=20)
            prepare(rule,source,'on_update',condition_token(condition),key,utcnow(),condition=condition)
            self.assertEqual(frappe.db.count('Notification Queue')-before_case,expected)
            log=frappe.get_doc('Process Execution Log', {'notification_key': key})
            self.assertEqual((log.execution_outcome,log.functional_status),('Functional Status', decision))
            if not expected:self.assertIn('suppressed',log.diagnostic_detail)
        self.assertEqual(frappe.db.count('Notification Queue')-before,1)
        synchronize(rule);rule.reload()
        self.assertFalse(next(row for row in rule.outputs if row.status_code=='quiet').send)
        self.network_mock.assert_not_called()

    def test_notification_fixed_source_field_and_required_configuration(self):
        from messaging.communication.doctype.notification_rule.contract import resolve_parameters, validate_parameters, synchronize
        from messaging.communication.doctype.notification_rule.events import prepared_data
        rule,source = self.notification_fixture()
        row=next(r for r in rule.process_parameters if r.parameter_name=='value')
        self.assertEqual(resolve_parameters(rule, source.as_dict())['value'], 'Synthetic value')
        row.value_source='Source Field';row.source_field='description'
        self.assertEqual(resolve_parameters(rule, source.as_dict())['value'],source.description)
        row.source_field='docstatus'
        with self.assertRaises(frappe.ValidationError):validate_parameters(rule, json.loads(rule.process_contract))
        row.source_field='description'
        with self.assertRaises(frappe.PermissionError):resolve_parameters(rule, {'doctype':source.doctype,'name':source.name})
        row.source_field=''
        with self.assertRaises(frappe.ValidationError):validate_parameters(rule,json.loads(rule.process_contract))
        row.value_source='Fixed Value';row.fixed_value='1'
        with self.assertRaises(frappe.ValidationError):validate_parameters(rule,json.loads(rule.process_contract))
        row.fixed_value='"fixed"'
        source.description='Changed since event'
        source.save()
        rule.enabled=0;rule.save();self.approve(rule);rule.enabled=1;rule.save()
        definition=frappe.get_doc('Process Definition',rule.process_definition)
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions,condition_token
        args=dict(doctype='ToDo',name=source.name,event='on_update',occurrence=condition_token(canonical_conditions(rule)[0]))
        context=process.build_context(definition,rule,args,administrative_test=False)
        self.assertEqual(context._request.source.name,source.name)
        self.assertNotIn('description',context._request.source.__slots__)
        self.assertEqual(context._source['description'],'Changed since event')

    def test_notification_send_disabled_does_not_render_even_without_destination(self):
        from messaging.communication.doctype.notification_rule.events import prepared_data
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule,source=self.notification_fixture()
        response=dict(version=1,status='quiet',recipient='',data={'action':'send'},message='')
        with patch('messaging.communication.doctype.communication_template.communication_template.resolve_output',side_effect=AssertionError('Suppressed output cannot render')):
            self.assertIsNone(prepared_data(response,rule))
            self.assertIsNone(prepared_data(dict(response,status='skip',data={'action':'skip'}),rule))
        state=next(row for row in rule.outputs if row.status_code=='ready');state.send=0
        synchronize(rule)
        self.assertFalse(state.send)
        self.assertNotIn('action',rule.meta.get_valid_columns())
        self.assertFalse(any(field.fieldname=='content_type' for field in rule.meta.fields))

    def test_bot_request_uses_real_session_contact_and_originating_message(self):
        from messaging.conversation.doctype.chat_contact.chat_contact import get_contact
        from messaging.conversation.doctype.chat_session.chat_session import get_session
        from messaging.conversation.doctype.chat_message.chat_message import append_message
        from messaging.common.primitives import utcnow
        rule,source=self.notification()
        contact=get_contact('emulator',rule.account,'synthetic-'+self.prefix)
        contact.parameter_values=json.dumps({'synthetic_key':'Existing value'})
        contact.save()
        session=get_session(contact,utcnow())
        turn=frappe.get_doc(dict(doctype='Chat Turn',event_key=self.prefix,contact=contact.name,session=session.name,status='Processing')).insert()
        incoming=append_message(session,self.prefix,'Incoming','Synthetic request',turn=turn.name)
        flow=frappe.new_doc('Bot Flow')
        flow.update(dict(workflow_code=self.prefix,workflow_name='Request probe',status='Disabled',priority=0,show_in_menu=0,
            process_definition=self.register('request_probe')))
        flow.append('translations', dict(language='es', semantic_description='Synthetic request probe'))
        from messaging.chatbot.doctype.bot_flow.contract import synchronize
        synchronize(flow);self.approve(flow);self.activate(flow)
        result=self.execute(flow,session=session.name,turn=turn.name,execution_id=self.prefix)
        self.assertTrue(result['functional'],result)
        self.assertEqual(result['status'],'observed')
        definition=frappe.get_doc('Process Definition',flow.process_definition)
        context=process.build_context(definition,flow,{},administrative_test=False,
            execution=dict(session=session.name,turn=turn.name))
        self.assertEqual(context._request.message.name,incoming.name)
        self.assertEqual(context._request.contact.name,contact.name)
        from messaging.registry.processes import ProcessContactReference
        with self.assertRaises(frappe.PermissionError):ProcessContactReference('Chat Contact',contact.name).profile_value('synthetic_key')
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError):context._request.contact.get_document()


    def test_notification_zero_parameters_fixed_template_and_deferred_revalidation(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.events import prepare
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.common.primitives import utcnow
        from messaging.communication.doctype.notification_queue.dispatch import revalidate
        rule,source=self.notification()
        rule.process_definition=self.register('empty_notification','notification')
        synchronize(rule)
        template=frappe.get_doc(dict(doctype='Communication Template',channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix,language='es', body='Fixed notification without variables.')).insert()
        rule.outputs[0].template_reference=template.name
        self.assertEqual(self.approve(rule)['status'],'Passed')
        rule.enabled=1;rule.save()
        self.assertFalse(rule.process_parameters)
        condition=canonical_conditions(rule)[0]
        key=self.prefix+'-empty'
        prepare(rule,source,'on_update',condition_token(condition),key,utcnow(),condition=condition)
        queue=frappe.get_doc('Notification Queue',{'occurrence_key':key})
        self.assertEqual(queue.prepared_text,'Fixed notification without variables.')
        self.assertEqual(json.loads(queue.variables_json),{})
        queue.claim_id=self.prefix+'-revalidate'
        self.assertIsNone(revalidate(queue))
        self.assertEqual(queue.prepared_text,'Fixed notification without variables.')
        rule.enabled=0;rule.outputs[0].send=0;rule.save()
        self.assertEqual(revalidate(queue),'delivery_configuration_changed')
        self.network_mock.assert_not_called()

    def test_notification_template_contract_preserves_send_and_reference(self):
        from copy import deepcopy
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule,source=self.notification();synchronize(rule)
        row=rule.outputs[0];row.template_reference='retained-reference';row.send=0
        synchronize(rule)
        self.assertEqual((row.template_reference,row.send),('retained-reference',0))
        contract=deepcopy(json.loads(rule.process_contract));contract.pop('definition')
        contract['outcomes'][row.status_code]['mode']=['message']
        synchronize(rule,contract=contract)
        self.assertEqual((row.template_reference,row.send),('retained-reference',0))
        row.send=1
        with self.assertRaises(frappe.ValidationError):synchronize(rule,contract=contract)

    def test_notification_children_reject_direct_writes_and_cross_rule_reuse(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule,source=self.notification();synchronize(rule)
        for row in rule.outputs:
            row.send=0
        rule.insert()
        for row in rule.outputs:
            with self.assertRaises(frappe.PermissionError):row.save()
        another=frappe.copy_doc(rule);another.rule_name='Other synthetic rule'
        another.outputs[0].name=rule.outputs[0].name
        with self.assertRaises(frappe.PermissionError):synchronize(another)

    def test_automated_template_preparation_keeps_native_source_permissions(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        from messaging.communication.doctype.notification_rule.events import prepare
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.security.execution_context import _automation_context
        from messaging.security.service_identity import SERVICE_USER
        from messaging.common.primitives import utcnow
        rule,source=self.notification()
        rule.process_definition=self.register('empty_notification','notification')
        synchronize(rule)
        template=frappe.get_doc(dict(doctype='Communication Template',channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            template_name=self.prefix,language='es', body='Approved synthetic template.')).insert()
        rule.outputs[0].template_reference=template.name
        self.approve(rule);rule.enabled=1;rule.save()
        condition=canonical_conditions(rule)[0]
        key=self.prefix+'-denied'
        with _automation_context():
            prepare(rule,source,'on_update',condition_token(condition),key,utcnow(),condition=condition)
        self.assertFalse(frappe.db.exists('Notification Queue',{'occurrence_key':key}))
        # Share only this synthetic source document; no service role is broadened.
        from frappe.share import add
        add('ToDo',source.name,user=SERVICE_USER,read=1,notify=0)
        key=self.prefix+'-allowed'
        with _automation_context():
            prepare(rule,source,'on_update',condition_token(condition),key,utcnow(),condition=condition)
        queue=frappe.get_doc('Notification Queue',{'occurrence_key':key})
        self.assertEqual(queue.prepared_text,'Approved synthetic template.')
        self.network_mock.assert_not_called()


    def test_configurable_output_nulls_preserve_flat_schema_and_reject_extra_fields(self):
        from messaging.presets.processes import registration as registration_process
        declarations=[dict(name='value',type='string',multiple=False,required=True),
            dict(name='optional',type='integer',multiple=False,required=False)]
        def context():
            return _ProcessContext('messaging.capture','bot_flow','es',False,{'name':'Contact','value':'captured'},
                _configured_inputs=declarations,_request=BotRequest(),_invoke=lambda fn,request:fn(request))
        valid=context();valid.declare(registration_process.definition)
        valid._bind_handler(registration_process.handle)
        self.assertEqual(valid.response()['data'],{'name':'Contact','value':'captured','optional':None})
        invalid=context();invalid.declare(registration_process.definition)
        invalid._bind_handler(lambda request: BotResponse('registered',{'value':'captured','extra':'rejected'}))
        with self.assertRaises(frappe.ValidationError):invalid.response()


def configurable_contract(*, params, outcomes):
    params.register('declared', type='boolean', required=False)
    params.register_configurable()
    outcomes.register('success', fields={}, mode=['message', 'template'], action='complete_flow')


def configurable_business(request: BotRequest) -> BotResponse:
    return BotResponse('success')


def configurable_notice(*, params, outcomes):
    params.register('declared', type='boolean', required=False)
    params.register_configurable()
    outcomes.register('success', fields={}, mode=['message', 'template'])


def configurable_notice_business(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyResponse('success')


class TestConfigurableParameters(unittest.TestCase):
    def context(self, declaration, *, values=None, configured=(), kind='bot_flow', test=False, business=None):
        request = NotifyRequest() if kind == 'notification' else BotRequest()
        context = _ProcessContext('messaging.configurable', kind, '', test, values or {},
            _configured_inputs=configured, _request=request, _invoke=lambda fn, request: fn(request))
        context.declare(declaration)
        if business is None:
            business = (lambda request: NotifyResponse('done')) if kind == 'notification' else (lambda request: BotResponse('done'))
        context._bind_handler(business)
        return context

    def test_combined_groups_definitions_and_immutable_typed_values(self):
        from decimal import Decimal
        seen = []
        def declaration(*, params, outcomes):
            params.register('contract', type='string')
            params.register('optional', type='date', required=False)
            params.register_configurable()
            params.register('count', type='integer', required=False)
            outcomes.register('done', fields={}, mode=['message'], action='complete_flow')
        def business(request):
            seen.append(request)
            return NotifyResponse('done') if kind == 'notification' else BotResponse('done')
        configured = [dict(name='amount', type='number', required=True, multiple=True),
            dict(name='accepted', type='boolean', required=True, multiple=False),
            dict(name='other', type='string', required=False, multiple=False)]
        for kind in ('bot_flow', 'notification'):
            context = self.context(declaration, kind=kind, configured=configured, business=business,
                values={'contract': '001', 'count': 3, 'amount': ['2.50', '-15.75000000000000000001'], 'accepted': False})
            self.assertFalse(seen)
            context.response()
            request = seen.pop()
            self.assertEqual(dict(request.params.declared), {'contract': '001', 'count': 3})
            self.assertEqual(dict(request.params.configurable), {'amount': (Decimal('2.50'), Decimal('-15.75000000000000000001')), 'accepted': False})
            self.assertEqual(request.params['amount'][0].as_tuple().exponent, -2)
            self.assertIs(request.params.get('accepted'), False)
            self.assertNotIn('optional', request.params)
            self.assertFalse(request.params.definition('optional').required)
            definition = request.params.definition('amount')
            self.assertEqual((definition.type, definition.required, definition.multiple, definition.origin), ('number', True, True, 'configurable'))
            self.assertEqual(request.params.definition('contract').origin, 'declared')
            with self.assertRaises(TypeError): request.params.configurable['accepted'] = True
            with self.assertRaises(TypeError): request.params['amount'] = ()
            with self.assertRaises(FrozenInstanceError): definition.type = 'string'
            with self.assertRaises(FrozenInstanceError): request.params._values = {}
            self.assertEqual(set(dict(request.params)), {'contract', 'count', 'amount', 'accepted'})
            self.assertEqual(dict(request.params.items()), dict(request.params))
            # Metadata is a snapshot, independent of mutable configuration.
            configured[0]['type'] = 'string'
            self.assertEqual(definition.type, 'number')
            configured[0]['type'] = 'number'

    def test_all_declaration_combinations_and_empty_configuration(self):
        for declared in (False, True):
            for configurable in (False, True):
                def declaration(*, params, outcomes):
                    if declared: params.register('value', type='integer', required=False)
                    if configurable: params.register_configurable()
                    outcomes.register('done', fields={}, mode=['template'], action='complete_flow')
                context = self.context(declaration)
                self.assertEqual(context.response()['status'], 'done')
                self.assertEqual(len(context.contract()['parameters']), int(declared))
                self.assertEqual(bool(context.contract().get('configurable_parameters')), configurable)

    def test_duplicates_disallowed_and_required_inputs_validated(self):
        item = dict(name='declared', type='string', required=True, multiple=False)
        for test in (True, False):
            with self.assertRaises(frappe.ValidationError):
                self.context(configurable_contract, configured=[item], test=test)
        configured = [dict(name='amount', type='number', required=True, multiple=False)]
        context = self.context(configurable_contract, configured=configured)
        with self.assertRaises(frappe.ValidationError): context.response()
        context = self.context(configurable_contract, configured=configured, values={'amount': False})
        with self.assertRaises(frappe.ValidationError): context.response()
        context = self.context(configurable_contract, values={'extra': 1})
        with self.assertRaises(frappe.ValidationError): context.response()
        context = self.context(configurable_contract)
        with self.assertRaises(frappe.ValidationError): context.params.register_configurable()

    def test_five_input_types_exact_serialization_and_reconstruction(self):
        from decimal import Decimal
        from messaging.common.primitives import decimal_json
        from messaging.communication.doctype.process_definition.validation import _input_value
        for kind, value in [('string','001'), ('integer',7), ('boolean',False), ('date','2026-11-05')]:
            self.assertEqual(_input_value(kind, value), value)
        for text in ('2.50', '100', '-15.75', '0.125', '12345678901234567890.1234567890123456789012345'):
            number = _input_value('number', text)
            self.assertIs(type(number), Decimal)
            stored = json.loads(json.dumps({'value': number}, default=decimal_json))
            self.assertEqual(_input_value('number', stored['value']).as_tuple(), number.as_tuple())
        for bad in (True, False, 1.25, 'NaN', 'Infinity', '-Infinity', Decimal('NaN'), Decimal('Infinity'), '1,250', '1.2.3', ' 2.50 ', '1e3', ''):
            with self.subTest(bad=bad), self.assertRaises(frappe.ValidationError): _input_value('number', bad)
        for bad in ('false', 0, 1, None):
            with self.assertRaises(frappe.ValidationError): _input_value('boolean', bad)
        with self.assertRaises(frappe.ValidationError): _input_value('integer', True)


class TestConfigurableProjections(ProcessFixtures, unittest.TestCase):
    def test_discovery_combined_contract_without_business_invocation(self):
        identifier = self.register('configurable_contract')
        definition = frappe.get_doc('Process Definition', identifier)
        context = _ProcessContext(identifier, 'bot_flow', '', True, {},
            _invoke=lambda fn, request: self.fail('Discovery invoked business code'))
        context.declare(configurable_contract)
        self.assertEqual(context.response()['status'], 'test')
        contract, callback = discover_contract(definition)
        self.assertTrue(contract['configurable_parameters'])
        self.assertEqual(contract['parameters'], [dict(name='declared',type='boolean',required=False,multiple=False)])
        self.assertEqual(callback, configurable_business)

    def test_notification_combines_fixed_and_source_values_and_preserves_outputs(self):
        from decimal import Decimal
        from messaging.communication.doctype.notification_rule.contract import synchronize, validate_parameters, resolve_parameters
        rule, source = self.notification('configurable_notice')
        rule.append('process_parameters', dict(parameter_name='amount', internal_type='number', required=1,
            multiple=1, value_source='Fixed Value', fixed_value='[2.50, 0.125000000000000000000001]'))
        rule.append('process_parameters', dict(parameter_name='note', internal_type='string', required=0,
            value_source='Source Field', source_field='description'))
        contract = synchronize(rule)
        rule.outputs[0].send = 0
        row = next(r for r in rule.process_parameters if r.parameter_name == 'declared')
        row.fixed_value = 'false'
        validate_parameters(rule, contract)
        values = resolve_parameters(rule, source.as_dict())
        self.assertIs(values['declared'], False)
        self.assertEqual(values['amount'], [Decimal('2.50'), Decimal('0.125000000000000000000001')])
        self.assertEqual(values['note'], source.description)
        rule.insert()
        rule.reload()
        self.assertEqual(resolve_parameters(rule, source.as_dict()), values)
        self.assertEqual([item['origin'] for item in contract['parameters']], ['declared','configurable','configurable'])
        rule.outputs[0].send = 0
        rule.outputs[0].output_mode = 'Template'
        before = json.loads(rule.process_contract)
        synchronize(rule)
        self.assertEqual(json.loads(rule.process_contract), before)
        self.assertEqual((rule.outputs[0].send, rule.outputs[0].output_mode), (0, 'Template'))
        rule.append('process_parameters', dict(parameter_name='declared', internal_type='string'))
        with self.assertRaises(frappe.ValidationError): synchronize(rule)

    def test_bot_flow_projects_types_and_rejects_declared_override(self):
        from messaging.chatbot.doctype.bot_flow.contract import project_contract, input_type, compatible
        contract = dict(parameters=[dict(name='declared',type='boolean',required=False,multiple=False)],
            configurable_parameters=True, outcomes={})
        decimal = frappe.get_doc(dict(doctype='Bot Parameter', name=self.prefix, label='Amount', parameter_type='Decimal', enabled=1)).insert()
        flow = frappe._dict(parameters=[frappe._dict(parameter=decimal.name, process_argument='amount', required=1, allow_multiple_values=0)])
        projected = project_contract(contract, flow)
        self.assertEqual(projected['parameters'][1],dict(name='amount',type='number',required=True,multiple=False,origin='configurable'))
        self.assertEqual(input_type(decimal), 'number')
        self.assertFalse(compatible(decimal, contract['parameters'][0], flow.parameters[0]))
        self.assertEqual(input_type(frappe._dict(parameter_type='Expression')), 'string')
        flow.parameters.append(flow.parameters[0])
        with self.assertRaises(frappe.ValidationError): project_contract(contract, flow)


    def test_notification_source_number_and_check_preserve_database_precision(self):
        from decimal import Decimal
        from messaging.communication.doctype.notification_rule.contract import resolve_parameters
        source = frappe.get_doc(dict(doctype='Currency', currency_name=self.prefix,
            fraction='Synthetic fraction', fraction_units=100, enabled=0,
            smallest_currency_fraction_value='0.123456789')).insert()
        source.reload()
        definitions = [dict(name='amount', type='number', required=True, multiple=False, origin='configurable'),
            dict(name='accepted', type='boolean', required=True, multiple=False, origin='declared')]
        rule = frappe._dict(source_doctype='Currency', process_contract=json.dumps(dict(parameters=definitions)),
            process_parameters=[frappe._dict(parameter_name='amount', value_source='Source Field', source_field='smallest_currency_fraction_value'),
                frappe._dict(parameter_name='accepted', value_source='Source Field', source_field='enabled')])
        values = resolve_parameters(rule, source.as_dict())
        self.assertEqual(values['amount'].as_tuple(), Decimal('0.123456789').as_tuple())
        self.assertIs(values['accepted'], False)


class TestUnifiedOutcomes(unittest.TestCase):
    def context(self, *, kind='bot_flow', fields=None, include_configurable=False, allow_configurable=True,
        configured=(), values=None, data=None):
        def declaration(*, params, outcomes):
            params.register('declared', type='string', required=False)
            if allow_configurable:
                params.register_configurable()
            outcomes.register('done', fields=fields or {}, mode=['message'], action='complete_flow', configurable=include_configurable)
        request = NotifyRequest() if kind == 'notification' else BotRequest()
        response_type = NotifyResponse if kind == 'notification' else BotResponse
        context = _ProcessContext('messaging.outputs',kind,'',False,values or {},
            _configured_inputs=configured,_request=request,_invoke=lambda fn,request:fn(request))
        context.declare(declaration)
        context._bind_handler(lambda request, payload=data: response_type('done', data=payload or {}))
        return context

    def test_default_false_empty_fields_and_declared_inputs_are_independent(self):
        from messaging.registry.processes import ProcessOutcomes
        self.assertFalse(hasattr(ProcessOutcomes, 'register_captured'))
        for kind in ('bot_flow','notification'):
            context=self.context(kind=kind,values={'declared':'input'})
            self.assertEqual(context.outcomes._definitions['done']['fields'],{})
            self.assertNotIn('configurable',context.contract()['outcomes']['done'])
            self.assertEqual(context.response()['status'],'done')
            context=self.context(kind=kind,fields={'identifier':'string'},values={'declared':'input'},data={'identifier':'result'})
            self.assertEqual(context.outcomes._definitions['done']['fields'],{'identifier':'string'})
            self.assertEqual(context.response()['data']['identifier'],'result')

    def test_effective_configurable_schema_combines_only_additional_inputs(self):
        from decimal import Decimal
        definitions=[dict(name='amount',type='number',required=True,multiple=False),
            dict(name='accepted',type='boolean',required=True,multiple=False),
            dict(name='optional',type='integer',required=False,multiple=False)]
        values={'declared':'not an output','amount':Decimal('2.50'),'accepted':False}
        for kind in ('bot_flow','notification'):
            context=self.context(kind=kind,fields={'identifier':'string'},include_configurable=True,configured=definitions,
                values=values,data={'identifier':'result','amount':'2.50','accepted':False})
            self.assertEqual(context.outcomes._definitions['done']['fields'],dict(identifier='string',amount='string',accepted='boolean',optional='nullable_integer'))
            self.assertEqual(context.contract()['outcomes']['done']['fields'],{'identifier':'string'})
            self.assertNotIn('declared',context.outcomes._definitions['done']['fields'])
            result=context.response()['data']
            self.assertEqual(result['amount'],'2.50');self.assertIs(result['accepted'],False)
            self.assertIsNone(result['optional'])

    def test_no_value_copy_and_extra_data_rejected(self):
        configured=[dict(name='additional',type='string',required=True,multiple=False)]
        for data in ({'additional':'result','extra':'not allowed'},{'declared':'input','additional':'result'}):
            context=self.context(include_configurable=True,configured=configured,values={'declared':'input','additional':'input'},data=data)
            with self.subTest(data=data),self.assertRaises(frappe.ValidationError):context.response()
        context=self.context(include_configurable=True,configured=configured,values={'additional':'input'},data={'additional':'result'})
        self.assertEqual(context.response()['data'],{'additional':'result'})
        context=self.context(include_configurable=True,configured=configured,values={'additional':'input'})
        self.assertEqual(context.response()['data'], {})

    def test_v1_structure_and_typed_functional_results(self):
        from messaging.registry.processes import (
            ProcessSchemaValidationError, normalize_result, validate_process_schema_v1,
        )
        empty = dict(version=1, status='done', recipient='', data={}, message='')
        self.assertEqual(validate_process_schema_v1(empty), empty)
        for key in empty:
            with self.subTest(missing=key), self.assertRaises(ProcessSchemaValidationError):
                validate_process_schema_v1({field: value for field, value in empty.items() if field != key})
        for bad in (dict(version=2), dict(version=True), dict(version='1'), dict(status=''),
            dict(recipient=None), dict(data=[]), dict(data={'key': float('inf')}), dict(message=1),
            dict(extra=True)):
            with self.subTest(bad=bad), self.assertRaises(ProcessSchemaValidationError):
                validate_process_schema_v1(empty | bad)

        outcomes = {'done': {'fields': {'detail': 'string', 'count': 'integer'}}}
        for kind, response_type in (('bot_flow', BotResponse), ('notification', NotifyResponse)):
            def normalize(value):
                return normalize_result(value, outcomes=outcomes, definitions=(), process_type=kind)

            self.assertEqual(normalize(response_type('done')), empty)
            self.assertEqual(normalize(response_type('done', data={'detail': ''}))['data'], {'detail': ''})
            for status, data in (('', {}), ('other', {}), ('done', {'count': True}), ('done', {'extra': ''})):
                with self.subTest(kind=kind, status=status, data=data), self.assertRaises(ProcessSchemaValidationError):
                    normalize(response_type(status, data=data))
        omitted = NotifyNoResponse('not_eligible')
        self.assertIs(normalize(omitted), omitted)
        with self.assertRaises(frappe.ValidationError):
            normalize_result(omitted, outcomes=outcomes, definitions=(), process_type='bot_flow')

    def test_flag_type_capability_collisions_and_scalar_schema(self):
        for flag in (0,1,None,'true',[],{}):
            with self.subTest(flag=flag),self.assertRaises(frappe.ValidationError):self.context(include_configurable=flag)
        with self.assertRaises(frappe.ValidationError):self.context(include_configurable=True,allow_configurable=False)
        with self.assertRaises(frappe.ValidationError):self.context(include_configurable=True,fields={'additional':'string'},
            configured=[dict(name='additional',type='string',required=False,multiple=False)])
        with self.assertRaises(frappe.ValidationError):self.context(include_configurable=True,
            configured=[dict(name='additional',type='string',required=False,multiple=True)])
        # Multiple inputs remain valid when the output does not request projection.
        context=self.context(configured=[dict(name='additional',type='string',required=False,multiple=True)])
        self.assertEqual(context.response()['status'],'done')
        for kind in ('bot_flow','notification'):
            context=self.context(kind=kind,include_configurable=True)
            self.assertEqual(context.outcomes._definitions['done']['fields'],{})

    def test_five_types_project_to_compatible_scalar_output_fields(self):
        from messaging.communication.doctype.process_definition.validation import project_outcomes
        definitions=[dict(name=kind,type=kind,required=True,multiple=False,origin='configurable')
            for kind in ('string','integer','number','boolean','date')]
        outcomes={'done':dict(fields={},mode=['message'],configurable=True)}
        projected=project_outcomes(outcomes,definitions,configurable=True)
        self.assertEqual(projected['done']['fields'],dict(string='string',integer='integer',number='string',boolean='boolean',date='string'))
        self.assertEqual(project_outcomes(outcomes,definitions,configurable=True),projected)
        self.assertEqual(outcomes['done']['fields'],{})


def configurable_output_contract(*, params, outcomes):
    params.register('declared', type='boolean', required=False)
    params.register_configurable()
    outcomes.register('success', fields={'identifier':'string'}, configurable=True,
        mode=['message','template'], action='complete_flow')


def configurable_output_business(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyResponse('success',data={'identifier':'synthetic',**{key: format(value, 'f') if type(value).__name__ == 'Decimal' else value for key, value in request.params.configurable.items()}},message='Prepared synthetic result.')


class TestConfigurableOutcomesProjection(ProcessFixtures, unittest.TestCase):
    def test_notification_effective_outputs_and_runtime_use_the_same_schema(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule,source=self.notification('configurable_output_contract')
        rule.append('process_parameters',dict(parameter_name='note',internal_type='string',required=1,multiple=0,
            value_source='Fixed Value',fixed_value='"Additional value"'))
        contract=synchronize(rule)
        expected={'identifier':'string','note':'string'}
        self.assertEqual(contract['outcomes']['success']['fields'],expected)
        rule.outputs[0].send=0
        rule.insert();rule.reload()
        self.assertEqual(json.loads(rule.outputs[0].output_fields),expected)
        before=rule.process_contract;synchronize(rule)
        self.assertEqual(rule.process_contract,before);self.assertFalse(rule.outputs[0].send)
        definition=frappe.get_doc('Process Definition',rule.process_definition)
        with patch.object(process,'source_document',return_value=source.as_dict()):
            context=process.build_context(definition,rule,dict(doctype=source.doctype,name=source.name,event='on_update',occurrence='synthetic'),
                administrative_test=False,invoke=lambda fn,request:fn(request))
        context.declare(configurable_output_contract)
        context._bind_handler(configurable_output_business)
        self.assertEqual(context.outcomes._definitions['success']['fields'],expected)
        self.assertEqual(context.response()['data'],dict(identifier='synthetic',note='Additional value'))


# Shared synthetic Process fixtures for durable conversation tests.


CALLS = list()


@frappe.whitelist()
def controlled_process(value=None, optional="default"):
	CALLS.append(value)
	return dict(version=1, recipient="", status="success", data=dict(result=str(value) if value is not None else "hours", optional=optional), message="Process reply")


def controlled_values(values=None):
	CALLS.append(values)
	if not isinstance(values, list):
		raise TypeError("Expected a list")
	return dict(version=1, recipient="", status="invalid" if "99999" in values else "success",
		data={"count": len(values)}, message="Values: " + ", ".join(values))


def controlled_human(value=None):
	if value == 4:
		raise RuntimeError('Synthetic human failure')
	status = 'retry' if value == 0 else 'denied' if value == 2 else 'unlisted' if value == 3 else 'success'
	return dict(version=1, recipient='', status=status, data={}, message='Human decision: ' + status + '.')


def controlled_other_status():
	return dict(version=1, recipient="", status="unavailable", data={}, message="Unavailable")


def controlled_invalid():
	return dict(version=True, status="success", data={}, message="")


def controlled_exception():
	CALLS.append("exception")
	raise RuntimeError("private-value password=secret /srv/private.py")


def controlled_type_error():
	CALLS.append("type_error")
	raise TypeError("Synthetic private argument")


def controlled_timeout():
	time.sleep(2)
	return controlled_process()


def controlled_required(required_argument):
	CALLS.append(required_argument)
	return controlled_process()


def controlled_language_only(*, language):
	CALLS.append(dict(language=language))
	return dict(version=1, recipient="", status="success", data={}, message="Language: " + language)


def controlled_contracts_language(*, contracts, language):
	CALLS.append(dict(contracts=contracts, language=language))
	return dict(version=1, recipient="", status="success", data={}, message="Language: " + language)


_dialogue_contracts = {}


def dialogue_handler(*, params, outcomes):
    fixture = _dialogue_contracts[params._context.process_id]
    for item in fixture['inputs']:
        params.register(item['name'], type=item['type'], multiple=item['multiple'], required=item.get('required', True))
    for status, fields in fixture['outputs'].items():
        outcomes.register(status, fields=fields, mode=['message', 'template'],
            action=fixture.get('actions', {}).get(status, 'complete_flow'))


def dialogue_result(request: BotRequest) -> BotResponse:
    from messaging.registry.processes import validate_process_schema_v1
    from messaging.communication.doctype.process_definition.process_definition import _effect
    fixture = _dialogue_contracts['messaging.' + _effect.get()['source_name'] + '_process']
    method = globals()[fixture['method']]
    arguments = {key: list(value) if isinstance(value, tuple) else value for key, value in request.params.items()}
    import inspect
    if 'language' in inspect.signature(method).parameters:
        arguments['language'] = request.language
    result = method(**arguments)
    validate_process_schema_v1(result)
    return BotResponse(result['status'], data=result['data'], message=result['message'])


def controlled_values_with_other(values, other):
	if other != "retained":raise ValueError("Missing preserved argument")
	return controlled_values(values)


MIXED_CALLS = list()


def controlled_mixed(*, person, identity, visit_date, description, language):
	MIXED_CALLS.append(dict(person=person, identity=identity, visit_date=visit_date,
		description=description, language=language))
	return dict(version=1, status="success", recipient="", data={}, message="Business response.")


def controlled_capture(*, value=None, person=None, identity=None, visit_date=None, count=None,
		word=None, description=None, language):
	"""Read-only Process for native capture journeys, including real EMULR HTTP calls."""
	values = dict(value=value, person=person, identity=identity, visit_date=visit_date,
		count=count, word=word, description=description, language=language)
	return dict(version=1, status="success", recipient="", data=values,
		message="Captured: " + json.dumps(values, ensure_ascii=False, sort_keys=True))


def controlled_uncertain():
	"""Exercise the durable recovery path without external effects or a real timeout."""
	raise TimeoutError("Synthetic uncertain Process result")


def controlled_consumer(value=None):
    """Synthetic business callback for ordinary Flow consumers; never calls a provider."""
    CALLS.append(value)
    if value == 4:
        raise RuntimeError('Synthetic consumer failure')
    if value == 7:
        raise TimeoutError('Synthetic uncertain consumer result')
    status = {0: 'retry', 2: 'already_registered', 3: 'unlisted', 6: 'cancelled'}.get(value, 'registered')
    data = {'registered': {'full_name': 'Juan Pérez', 'identity_number': '0801199012345'},
        'already_registered': {'full_name': 'Juan Pérez', 'reference': 'REF-1'},
        'unlisted': {'full_name': 'Juan Pérez'}, 'retry': {}, 'cancelled': {}}[status]
    if value == 5:
        data['full_name'] = 17
    return dict(version=1, recipient='', status=status, data=data, message='Consumer result: ' + status + '.')



def component_notification(*, params, outcomes):
    outcomes.register('ready', fields={'contact_name': 'string', 'contract_code': 'string',
        'pending_amount': 'string'}, mode=['template'])


def prepare_component_notification(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    scenario = request.source.name.rsplit('_', 1)[-1]
    values = dict(contact_name='Juan {{contract_code}}' if scenario == 'literal' else 'Juan Perez',
        contract_code='SC-00000', pending_amount='L 1,250.00')
    if scenario == 'missing':
        values.pop('pending_amount')
    return NotifyResponse('ready', data=values)


def async_probe_handler(*, params, outcomes):
    """Static Code-First fixture discoverable in a fresh native RQ work horse."""
    outcomes.register('success', fields={'result': 'string', 'optional': 'string'}, mode=['message', 'template'], action='complete_flow')


def async_probe_result(request: BotRequest) -> BotResponse:
    return BotResponse('success', data={'result': 'native worker', 'optional': 'safe'}, message='Synthetic worker response.')



class TestHumanRequestRedis(unittest.TestCase):
    """Real Redis atomicity and expiry, without touching conversational documents."""
    def setUp(self):
        self.contact = 'synthetic-' + frappe.generate_hash(length=24)
        self.keys = set()

    def tearDown(self):
        if self.keys:
            frappe.cache.delete(*self.keys)

    def keys_for(self, message, contact=None):
        keys = process._human_request_keys(contact or self.contact, message)
        self.keys.update(keys)
        return keys

    def increment(self, message, contact=None):
        from messaging.common.primitives import identity_key
        return frappe.cache.eval(process.HUMAN_REQUEST_INCREMENT, 2,
            *self.keys_for(message, contact), identity_key(message), process.HUMAN_REQUEST_TTL)

    def test_atomic_independent_requests_and_duplicate_messages(self):
        from concurrent.futures import ThreadPoolExecutor
        from messaging.common.primitives import identity_key
        rows = [(self.keys_for('request-' + str(n)), identity_key('request-' + str(n))) for n in range(16)]
        client = frappe.cache
        def increment(row):
            return client.eval(process.HUMAN_REQUEST_INCREMENT, 2, *row[0], row[1], process.HUMAN_REQUEST_TTL)
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sorted(pool.map(increment, rows)), list(range(1, 17)))
            self.assertEqual(list(pool.map(increment, [rows[0]] * 16)), [16] * 16)
        self.assertEqual(self.increment('other', contact=self.contact + '-other'), 1)
        self.assertEqual(self.increment('request-0'), 16)

    def test_fixed_period_expiry_and_receipts_survive_reset(self):
        self.assertEqual(process.HUMAN_REQUEST_TTL, 86400)
        self.assertEqual(self.increment('one'), 1)
        counter, receipt = self.keys_for('one')
        self.assertTrue(86390 <= frappe.cache.ttl(counter) <= 86400)
        frappe.cache.expire(counter, 60)
        self.assertEqual(self.increment('two'), 2)
        self.assertLessEqual(frappe.cache.ttl(counter), 60)
        # Expire through Redis itself rather than waiting 24 hours.
        frappe.cache.pexpire(counter, 1)
        time.sleep(0.02)
        self.assertFalse(frappe.cache.exists(counter, shared=True))
        self.assertEqual(self.increment('one'), 0)
        self.assertEqual(self.increment('three'), 1)
        self.assertEqual(process._reset_human_request_count(self.contact, 'two'), 0)
        self.assertEqual(self.increment('four'), 2)
        self.assertEqual(process._reset_human_request_count(self.contact, 'four'), 1)
        self.assertTrue(frappe.cache.exists(receipt, shared=True))
        self.assertEqual(self.increment('four'), 0)
        self.assertEqual(self.increment('five'), 1)

    def test_privacy_site_isolation_and_denied_anonymous_access(self):
        keys = self.keys_for('private-message')
        for key in keys:
            value = key.decode() if isinstance(key, bytes) else key
            self.assertNotIn(self.contact, value)
            self.assertNotIn('private-message', value)
        site = frappe.local.site
        try:
            frappe.local.site = site + '-other'
            other = process._human_request_keys(self.contact, 'private-message')
            self.assertTrue(set(keys).isdisjoint(other))
        finally:
            frappe.local.site = site
        with patch.object(frappe.cache, 'eval', side_effect=AssertionError('Unauthorized Redis access')):
            with self.assertRaises(frappe.PermissionError):
                process.record_human_request(BotRequest())
            from messaging.presets.processes.human_availability import process as human_process
            from messaging.registry.processes.discovery import _discover_contract
            spec, _handler, response = _discover_contract(frappe._dict(
                application='messaging',
                definition_method=human_process.definition.__module__ + '.' + human_process.definition.__name__,
                method=human_process.handler.__module__ + '.' + human_process.handler.__name__,
                process_id='messaging.human_availability', process_type='bot_flow'), language='es')
            self.assertEqual(spec['outcomes']['filtered']['action'], 'cancel_flow')
            self.assertEqual(response['status'], 'test')


def catalogue_notification(*, params, outcomes):
    params.register('decision', type='string', required=False)
    params.register('amount', type='number', required=False)
    params.register('flag', type='boolean', required=False)
    params.register('count', type='integer', required=False)
    params.register('day', type='date', required=False)
    params.register('labels', type='string', required=False, multiple=True)
    params.register_configurable()
    for status in ('skip', 'send', 'success'):
        outcomes.register(status, fields={'recipient': 'string', 'amount': 'number', 'flag': 'boolean',
            'count': 'integer', 'day': 'date'}, mode=['template'], configurable=True)


def catalogue_notification_business(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    from decimal import Decimal
    return NotifyResponse(request.params.get('decision', 'success'), data={
        'recipient': 'synthetic-recipient', 'amount': request.params.get('amount', Decimal('350.000000000000000000001')),
        'flag': request.params.get('flag', False), 'count': request.params.get('count', 0),
        'day': request.params.get('day', '2026-11-09'),
        **dict(request.params.configurable),
    }, message='This message must never become notification content.')


class TestPersistedProcessCatalogue(ProcessFixtures, unittest.TestCase):
    """One durable contract for registration, administration and execution."""

    def configured_rule(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule, source = self.notification('catalogue_notification')
        synchronize(rule)
        template = frappe.get_doc(dict(doctype='Communication Template', template_name=self.prefix,
            language='es', channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active',
            body='Amount {{amount}} / {{count}} / {{flag}} / {{day}}',
            template_variables=[dict(parameter_name=k) for k in ('amount','count','flag','day')])).insert()
        for row in rule.outputs:
            row.template_reference, row.send = template.name, 1
        rule.insert()
        return rule, source, template

    def test_complete_metadata_idempotence_and_registration_never_runs_business(self):
        from messaging.registry.processes import discovery as registry
        from messaging.communication.doctype.process_definition.process_definition import registered_contract
        identifier = self.register('catalogue_notification', 'notification')
        definition = frappe.get_doc('Process Definition', identifier)
        original = definition.as_dict()
        contract = registered_contract(definition)
        self.assertTrue(contract['configurable_parameters'])
        self.assertEqual([p['type'] for p in contract['parameters']], ['string','number','boolean','integer','date','string'])
        self.assertTrue(contract['parameters'][-1]['multiple'])
        self.assertTrue(contract['outcomes']['skip']['configurable'])
        self.assertNotIn('decision', contract['outcomes']['skip']['fields'])
        snapshot = json.loads(definition.registered_snapshot)
        self.assertEqual(registry.registration_revisions(definition, snapshot)['capability_revision'], definition.capability_revision)
        self.assertNotEqual(definition.contract_revision, definition.capability_revision)
        # The declaration is checked once per registration; business callbacks are forbidden there.
        with patch.object(registry, '_discover_contract', wraps=registry._discover_contract) as discovery:
            self.register('catalogue_notification', 'notification', identifier=identifier)
        self.assertEqual(discovery.call_count, 1)
        self.assertEqual(frappe.get_doc('Process Definition', identifier).as_dict(), original)
        changed_code = json.loads(definition.registered_snapshot)
        changed_code['sources'][changed_code['implementation_sources'][-1]]['digest'] = '0' * 64
        revisions = registry.registration_revisions(definition, changed_code)
        self.assertEqual(revisions['contract_revision'],definition.contract_revision)
        self.assertNotEqual(revisions['capability_revision'],definition.capability_revision)
        changed_paths = json.loads(definition.registered_snapshot)
        for item in changed_paths['sources'].values():
            item['path'] = '/another/deployment/path.py'
        self.assertEqual(registry.registration_revisions(definition, changed_paths)['capability_revision'], definition.capability_revision)

    def test_open_save_and_idempotent_registry_preserve_approvals(self):
        from frappe.desk.form.load import getdoc
        from messaging.communication.doctype.process_definition import approval, validation
        from messaging.registry.processes import discovery as registry

        flow = self.flow('discovery_only_flow')
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        rule, source, template = self.configured_rule()
        self.assertEqual(self.approve(rule)['status'], 'Passed')
        with patch.object(registry, '_discover_contract', side_effect=AssertionError('Discovery during read')), \
            patch.object(validation, 'run_test', side_effect=AssertionError('Test on open or save')):
            for doc in (flow, rule):
                frappe.response.docs = []
                getdoc(doc.doctype, doc.name)
                shown = next(item for item in frappe.response.docs if item.doctype == doc.doctype)
                self.assertEqual(shown.status, 'Enabled')
            original = (rule.approval_hash, rule.process_test_log, rule.status)
            rule.notification_delay_minutes = 7
            rule.rule_name = 'Updated administrative label'
            rule.save()
            self.assertEqual((rule.approval_hash, rule.process_test_log, rule.status), original)
            self.assertTrue(approval.is_current(rule))
        register_processes(app='messaging', processes=self.definitions)
        rule.reload()
        self.assertEqual((rule.approval_hash, rule.process_test_log, rule.status), original)

    def test_revision_change_is_selective_transactional_and_idempotent(self):
        from copy import deepcopy
        from messaging.chatbot.doctype.bot_settings.interpretation import catalog
        from messaging.communication.doctype.process_definition import approval, validation
        from messaging.communication.doctype.notification_rule.notification_rule import set_active
        from messaging.registry.processes import discovery as registry

        flow = self.flow('discovery_only_flow')
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        rule, source, template = self.configured_rule()
        self.assertEqual(self.approve(rule)['status'], 'Passed')
        set_active(rule.as_dict(), 0)
        rule.reload()
        other = self.flow('alternate_flow')
        self.assertEqual(self.approve(other)['status'], 'Passed')
        self.assertIn(flow.name, catalog())
        self.assertIn(other.name, catalog())
        snapshot = {d.id: registry._uncached_capability_revision(frappe.get_doc('Process Definition', d.id))
            for d in self.definitions}
        targets = (flow.process_definition, rule.process_definition)
        original = {doc.name: (doc.status, doc.approval_hash, doc.process_test_log) for doc in (flow, rule, other)}

        def changed(definition):
            result = deepcopy(snapshot[definition.process_id])
            if definition.process_id in targets:
                result['sources'][result['implementation_sources'][-1]]['digest'] = '0' * 64
            return result

        with patch.object(registry, '_uncached_capability_revision', side_effect=changed), \
            patch.object(validation, 'run_test', side_effect=AssertionError('Testing during registration')):
            frappe.db.savepoint('synthetic_revision')
            register_processes(app='messaging', processes=self.definitions)
            for doc in (flow, rule):
                doc.reload()
                self.assertEqual(doc.status, 'Unavailable')
                self.assertFalse(approval.is_current(doc))
                self.assertEqual((doc.approval_hash, doc.process_test_log), original[doc.name][1:])
                self.assertEqual(json.loads(doc.approval_invalidation)[0]['reason'], 'process_revision_changed')
            self.assertNotIn(flow.name, catalog())
            self.assertIn(other.name, catalog())
            register_processes(app='messaging', processes=self.definitions)
            rule.reload()
            self.assertEqual(len(json.loads(rule.approval_invalidation)), 1)
            rule.notification_delay_minutes = 5
            rule.save()
            self.assertEqual(rule.status, 'Unavailable')
            other.reload()
            self.assertEqual((other.status, other.approval_hash, other.process_test_log), original[other.name])
            self.assertEqual(rule.outputs[0].template_reference, template.name)
            frappe.db.rollback(save_point='synthetic_revision')
        for doc in (flow, rule):
            doc.reload()
            self.assertEqual((doc.status, doc.approval_hash, doc.process_test_log), original[doc.name])
            self.assertTrue(approval.is_current(doc))

    def test_unsynchronized_code_is_blocked_without_rediscovery(self):
        from messaging.registry.processes import discovery as registry
        flow = self.flow('discovery_only_flow'); self.approve(flow)
        with patch.object(registry, '_discover_contract', side_effect=AssertionError('Unexpected discovery')), \
            patch('messaging.communication.doctype.process_definition.revisions.runtime_revision', return_value='unregistered-runtime'):
            result = self.execute(flow)
        self.assertFalse(result['functional'])
        self.assertEqual(frappe.get_doc('Process Execution Log',result['execution_log']).execution_outcome,'Internal Execution Error')

    def test_typed_values_multiple_absence_and_configurable_outputs(self):
        from decimal import Decimal
        from messaging.communication.doctype.notification_rule.contract import synchronize, resolve_parameters
        rule, source = self.notification('catalogue_notification')
        rule.append('process_parameters', dict(parameter_name='note',internal_type='string',required=0,
            multiple=0,value_source='Fixed Value',fixed_value='""'))
        contract = synchronize(rule)
        values = dict(amount='"350.000000000000000000001"', flag='false', count='0', day='"2026-11-09"', labels='["a,b", "", "c"]')
        for row in rule.process_parameters:
            if row.parameter_name in values: row.fixed_value = values[row.parameter_name]
        result = resolve_parameters(rule, source.as_dict())
        self.assertEqual(result['amount'], Decimal('350.000000000000000000001'))
        self.assertIs(result['flag'], False); self.assertEqual(result['count'],0)
        self.assertNotIn('decision', result); self.assertEqual(result['note'],'')
        self.assertEqual(result['labels'],['a,b','','c'])
        self.assertEqual(contract['outcomes']['success']['fields']['note'],'nullable_string')
        self.assertNotIn('decision', contract['outcomes']['success']['fields'])
        rule.process_parameters[-1].fixed_value = 'invalid json'
        from messaging.communication.doctype.notification_rule.contract import validate_parameters
        with self.assertRaises(frappe.ValidationError): validate_parameters(rule, contract)

    def test_send_checkbox_controls_all_statuses_and_queue_evidence(self):
        from messaging.communication.doctype.notification_rule.conditions import canonical_conditions, condition_token
        from messaging.communication.doctype.notification_rule.events import prepare
        from messaging.communication.doctype.notification_rule.contract import prepared_data
        from frappe.utils import now_datetime
        rule, source, template = self.configured_rule()
        for status in ('skip','send','success'):
            for send in (0,1):
                with self.subTest(status=status,send=send):
                    keep_other = 'success' if status != 'success' else 'skip'
                    for row in rule.outputs:
                        row.send = send if row.status_code == status else (1 if not send and row.status_code == keep_other else 0)
                    next(p for p in rule.process_parameters if p.parameter_name=='decision').fixed_value=json.dumps(status)
                    self.approve(rule)
                    rule.enabled=1;rule.save()
                    condition=canonical_conditions(rule)[0]; key='catalogue-'+frappe.generate_hash(length=20)
                    before=frappe.db.count('Notification Queue')
                    prepare(rule,source,'on_update',condition_token(condition),key,now_datetime(),condition=condition)
                    self.assertEqual(frappe.db.count('Notification Queue')-before,send)
                    logs=frappe.get_all('Process Execution Log',filters={'notification_key':key},fields=['functional_status','diagnostic_detail'])
                    self.assertEqual(logs[0].functional_status,status)
                    if send:
                        queue=frappe.get_doc('Notification Queue',{'occurrence_key':key})
                        self.assertIn('350.000000000000000000001 / 0 / false / 2026-11-09',queue.prepared_text)
                        self.assertNotIn('must never',queue.prepared_text)
                        self.assertEqual(queue.content_type,'Template')
                    else:
                        self.assertIn('send_disabled',logs[0].diagnostic_detail)
                    self.assertEqual(next(r for r in rule.outputs if r.status_code==status).template_reference,template.name)

    def test_template_names_subset_scalars_and_language_variants(self):
        from messaging.communication.doctype.communication_template.communication_template import validate_template_fields
        from messaging.communication.doctype.notification_rule.contract import validate_notification_contract, selected_output
        rule, source, template = self.configured_rule()
        validate_notification_contract(rule)
        template.body='Count {{Count}}';template.set('template_variables',[dict(parameter_name='Count')])
        with self.assertRaises(frappe.ValidationError): template.save()
        template.reload()
        template.body='Missing {{missing}}';template.set('template_variables',[dict(parameter_name='missing')]);template.save()
        with self.assertRaises(frappe.ValidationError): validate_notification_contract(rule)
        template.body='Amount {{amount}}';template.set('template_variables',[dict(parameter_name='amount')]);template.save()
        validate_notification_contract(rule)
        settings=frappe.get_doc('Bot Settings');settings.append('chatbot_languages',dict(language='en',enabled=1));settings.save()
        validate_notification_contract(rule)
        english=frappe.get_doc(dict(doctype='Communication Template',template_name=template.template_name,language='en',
            channel=frappe.db.get_value('Communication Channel', {'provider': 'EMULR'}, 'name'), status='Active', body='English {{amount}}',template_variables=[dict(parameter_name='amount')])).insert()
        validate_notification_contract(rule)
        self.assertEqual(selected_output(rule,'success').template_reference,template.name)
        template.template_name = self.prefix + '_renamed_label'
        with self.assertRaisesRegex(frappe.ValidationError, 'immutable'):
            template.save()
        template.reload()
        self.assertEqual(selected_output(rule,'success').template_reference,template.name)

    def test_notification_metadata_has_two_tabs_and_no_message_or_translations(self):
        meta=frappe.get_meta('Notification Rule')
        self.assertEqual([f.label for f in meta.fields if f.fieldtype=='Tab Break'],['General','Process Flow'])
        self.assertFalse(meta.has_field('output_translations'))
        outcome=frappe.get_meta('Notification Outcome')
        self.assertFalse(outcome.has_field('output_mode'))
        self.assertEqual([f.fieldname for f in outcome.fields if f.in_list_view],['status_code','template_reference','send'])


    def test_notification_process_layout_matches_bot_flow_sections_and_columns(self):
        flow = frappe.get_meta('Bot Flow')
        rule = frappe.get_meta('Notification Rule')
        names = ['process_flow_tab','section_break_process','process_definition',
            'column_break_fckh','process_method','section_break_parameters',
            'parameters','section_break_outputs','outputs']
        projected = ['process_flow_tab','section_break_process','process_definition',
            'column_break_fckh','process_method','section_break_parameters',
            'process_parameters','section_break_outputs','outputs']
        self.assertEqual([f.fieldname for f in flow.fields if f.fieldname in names], names)
        self.assertEqual([f.fieldname for f in rule.fields if f.fieldname in projected], projected)
        for left,right in zip(names,projected):
            a,b=flow.get_field(left),rule.get_field(right)
            self.assertEqual((a.fieldtype,a.label),(b.fieldtype,b.label))
        self.assertTrue(rule.get_field('process_method').read_only)
        self.assertTrue(rule.get_field('process_invocation_mode').hidden)
        self.assertLess(rule.fields.index(rule.get_field('parameter_controls')),rule.fields.index(rule.get_field('process_flow_tab')))


    def test_notification_send_checkbox_normalizes_false_and_rejects_other_values(self):
        from messaging.communication.doctype.notification_rule.contract import synchronize
        rule,source=self.notification()
        synchronize(rule)
        row=rule.outputs[0]
        for value,expected in ((None,0),(0,0),('0',0),(1,1),('1',1)):
            row.send=value;synchronize(rule)
            self.assertEqual(row.send,expected)
        for value in (2,'false','send','skip'):
            row.send=value
            with self.assertRaises(frappe.ValidationError):synchronize(rule)
