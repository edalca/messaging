"""Specialized Bot and Notification process contracts."""
import unittest
from dataclasses import fields, replace
import frappe

from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures, execution_context
from messaging.communication.doctype.process_definition.execution import run_handler
from messaging.communication.doctype.process_definition.revisions import registration_revisions
from typing import get_args, get_type_hints
from messaging.registry.processes import BotDefinition, BotHandler, BotParams, BotProcess, BotRequest, BotResponse, NotifyDefinition, NotifyHandler, NotifyNoResponse, NotifyParams, NotifyProcess, NotifyRequest, NotifyResponse, ProcessOutcomes, validate_process_schema_v1
from messaging.registry.processes import register_processes
from messaging.registry.processes.contracts import REGISTRATION_SCHEMA_VERSION, SUPPORTED_REGISTRATION_SCHEMAS, _validate_version, normalize_result, validate_definition_signature, validate_handler_signature
from messaging.registry.processes.context import _ProcessContext
from messaging.registry.processes.discovery import _source_digest, discover_contract
from messaging.common.primitives import _freeze
from messaging.registry.processes.test_support import (
    BOT_CALLS, bot_definition, bot_handle, notify_definition, notify_handle,
    positional_definition, wrong_bot_handler,
)


class TestVersionMetadata(unittest.TestCase):
    def test_only_the_metadata_only_messaging_initializer_ignores_version_bumps(self):
        previous = b'__version__ = "16.0.0rc1"\n'
        current = b'__version__ = "16.0.0rc2"\n'
        self.assertEqual(_source_digest('messaging', previous), _source_digest('messaging', current))
        self.assertNotEqual(
            _source_digest('messaging', current),
            _source_digest('messaging', current + b'import operator\n'),
        )
        self.assertNotEqual(_source_digest('qota_preset', previous), _source_digest('qota_preset', current))


class TestSpecializedProcesses(ProcessFixtures, unittest.TestCase):
    def test_bot_and_notify_registration_discovers_definition_without_calling_handler(self):
        BOT_CALLS.clear()
        identifier = 'messaging.' + self.prefix + '_bot'
        declaration = BotProcess(id=identifier, label='Balance Query', definition=bot_definition, handler=bot_handle)
        register_processes(app='messaging', processes=[declaration])
        self.assertEqual(BOT_CALLS, [])
        stored = frappe.get_doc('Process Definition', identifier)
        self.assertEqual(stored.process_type, 'bot_flow')
        self.assertEqual(stored.definition_method, bot_definition.__module__ + '.bot_definition')
        self.assertEqual(stored.method, bot_handle.__module__ + '.bot_handle')
        contract, callback = discover_contract(stored)
        self.assertEqual(BOT_CALLS, [])
        self.assertIs(callback, bot_handle)
        self.assertEqual(contract['parameters'][0]['name'], 'contract_number')
        self.assertIn('complete_flow', contract['outcomes']['success']['action'])
        snapshot = frappe.parse_json(stored.registered_snapshot)
        self.assertEqual(set(snapshot['declaration']['functions']), {'definition', 'handler'})
        original = registration_revisions(stored, snapshot)
        changed = dict(snapshot)
        changed_declaration = dict(snapshot['declaration'])
        changed_functions = dict(snapshot['declaration']['functions'])
        changed_functions['handler'] = stored.method + '_other'
        changed_declaration['functions'] = changed_functions
        changed['declaration'] = changed_declaration
        self.assertNotEqual(registration_revisions(stored, changed)['capability_revision'], original['capability_revision'])
        stored.definition_method = stored.definition_method + '_other'
        self.assertNotEqual(registration_revisions(stored, snapshot)['contract_revision'], original['contract_revision'])
        stored.definition_method = bot_definition.__module__ + '.bot_definition'
        repeated = register_processes(app='messaging', processes=[declaration])
        self.assertEqual(repeated['unchanged'], 1)

    def test_invalid_definition_and_handler_signatures_are_rejected(self):
        with self.assertRaises(frappe.ValidationError):
            validate_definition_signature(positional_definition)
        with self.assertRaises(frappe.ValidationError):
            validate_handler_signature(wrong_bot_handler, 'bot_flow')
        with self.assertRaises(frappe.ValidationError):
            BotProcess(id='messaging.' + self.prefix + '_bad', label='Bad', definition=positional_definition, handler=bot_handle)
        with self.assertRaises(frappe.ValidationError):
            NotifyProcess(id='messaging.' + self.prefix + '_bad', label='Bad', definition=notify_definition, handler=bot_handle)

        def legacy_definition(*, parameters, outcomes):
            outcomes.register('success', fields={}, mode=['message'], action='complete_flow')

        with self.assertRaises(frappe.ValidationError):
            validate_definition_signature(legacy_definition)
        with self.assertRaises(frappe.ValidationError):
            BotProcess(id='messaging.' + self.prefix + '_legacy', label='Legacy',
                definition=legacy_definition, handler=bot_handle)

    def test_constructors_protocols_and_internal_registration_version(self):
        self.assertEqual([item.name for item in fields(BotProcess)], ['id', 'label', 'definition', 'handler'])
        self.assertEqual([item.name for item in fields(NotifyProcess)], ['id', 'label', 'definition', 'handler'])
        self.assertEqual(BotProcess.contract, 1)
        self.assertEqual(NotifyProcess.contract, 1)
        self.assertIn(REGISTRATION_SCHEMA_VERSION, SUPPORTED_REGISTRATION_SCHEMAS)
        with self.assertRaises(ValueError):
            _validate_version('2')
        definition_hints = get_type_hints(BotDefinition.__call__)
        self.assertIs(definition_hints['params'], BotParams)
        self.assertIs(definition_hints['outcomes'], ProcessOutcomes)
        self.assertIs(definition_hints['return'], type(None))
        handler_hints = get_type_hints(BotHandler.__call__)
        self.assertIs(handler_hints['request'], BotRequest)
        self.assertIs(handler_hints['return'], BotResponse)
        notify_definition_hints = get_type_hints(NotifyDefinition.__call__)
        self.assertIs(notify_definition_hints['params'], NotifyParams)
        self.assertIs(notify_definition_hints['outcomes'], ProcessOutcomes)
        notify_handler_hints = get_type_hints(NotifyHandler.__call__)
        self.assertIs(notify_handler_hints['request'], NotifyRequest)
        self.assertEqual(set(get_args(notify_handler_hints['return'])), {NotifyResponse, NotifyNoResponse})
        identifier = 'messaging.' + self.prefix + '_typed'
        bot = BotProcess(id=identifier, label='Typed balance', definition=bot_definition, handler=bot_handle)
        notice = NotifyProcess(id=identifier + '_notice', label='Typed notice', definition=notify_definition, handler=notify_handle)
        self.assertEqual(bot.type, 'bot_flow')
        self.assertEqual(notice.type, 'notification')
        for name, constructor, definition, handler in (
            ('bot', BotProcess, bot_definition, bot_handle),
            ('notice', NotifyProcess, notify_definition, notify_handle),
        ):
            for extra in (dict(registration_schema_version='1'), dict(contract=1), dict(type='bot_flow')):
                with self.subTest(process=name, extra=sorted(extra)), self.assertRaises(TypeError):
                    constructor(id=identifier + '_extra', label='Extra', definition=definition, handler=handler, **extra)
        bot_context = _ProcessContext('messaging.params', 'bot_flow', '', False, {})
        notice_context = _ProcessContext('messaging.params', 'notification', '', False, {})
        self.assertIsInstance(bot_context.params, BotParams)
        self.assertNotIsInstance(bot_context.params, NotifyParams)
        self.assertIsInstance(notice_context.params, NotifyParams)
        bot_context.params.register('contract_number', type='string', required=True, multiple=True)
        bot_context.params.register_configurable()
        notice_context.params.register('payment_id', type='string', required=False)
        self.assertEqual(bot_context.params._definitions[0]['multiple'], True)
        self.assertTrue(bot_context.params._configurable)
        self.assertFalse(notice_context.params._definitions[0]['required'])
        fresh = _ProcessContext('messaging.params', 'bot_flow', '', True, {})
        fresh.declare(bot_definition)
        self.assertIsNone(fresh._state['callback'])
        self.assertEqual(fresh.contract()['parameters'][0]['name'], 'contract_number')

    def test_requests_are_distinct_and_responses_are_exclusive(self):
        bot = BotRequest(params={'contract_number': '00123'}, language='es')
        notice = NotifyRequest(params={'payment_id': 'PAY-1'}, event='on_submit', language='es')
        self.assertIsInstance(bot, BotRequest)
        self.assertNotIsInstance(bot, NotifyRequest)
        self.assertEqual([item.name for item in fields(bot)],
            ['params', 'contact', 'language', 'channel', 'account', 'session', 'message'])
        self.assertEqual([item.name for item in fields(notice)],
            ['params', 'language', 'source', 'event', 'channel', 'account'])
        self.assertEqual(bot.params['contract_number'], '00123')
        outcomes = {'payment_received': {'fields': {'recipient': 'string', 'amount': 'string'}, 'mode': ['template']},
            'success': {'fields': {'balance': 'string'}, 'mode': ['message'], 'action': 'complete_flow'}}
        success = BotResponse('success', data={'balance': '10'}, message='Prepared')
        paid = NotifyResponse('payment_received', data={'recipient': '504', 'amount': '10'})
        quiet = NotifyNoResponse('payment_not_eligible')
        self.assertEqual(normalize_result(success, outcomes=outcomes, definitions=(), process_type='bot_flow')['status'], 'success')
        self.assertIs(normalize_result(quiet, outcomes=outcomes, definitions=(), process_type='notification'), quiet)
        self.assertEqual(normalize_result(paid, outcomes=outcomes, definitions=(), process_type='notification')['recipient'], '504')
        for kind, value in (('bot_flow', quiet), ('bot_flow', paid), ('notification', success), ('notification', None)):
            with self.subTest(kind=kind, value=type(value).__name__), self.assertRaises(frappe.ValidationError):
                normalize_result(value, outcomes=outcomes, definitions=(), process_type=kind)
        context = _ProcessContext('messaging.test', 'notification', '', False, {})
        context.declare(notify_definition)
        self.assertIsNone(context._state['callback'])
        self.assertEqual(set(context.contract()['outcomes']), {'payment_received'})

    def test_notify_no_response_is_distinct_from_a_technical_error(self):
        identifier = 'messaging.' + self.prefix + '_completion'
        register_processes(app='messaging', processes=[NotifyProcess(
            id=identifier, label='Completion', definition=notify_definition, handler=notify_handle)])
        definition = frappe.get_doc('Process Definition', identifier)
        self.assertEqual(definition.process_type, 'notification')
        notice_snapshot = frappe.parse_json(definition.registered_snapshot)
        self.assertEqual(notice_snapshot['declaration']['functions']['definition'], definition.definition_method)
        self.assertEqual(notice_snapshot['declaration']['functions']['handler'], definition.method)
        quiet = run_handler(definition, execution_context(identifier, 'notification', 'es', False,
            _freeze({'payment_id': 'ineligible'})), {})
        self.assertIsInstance(quiet, NotifyNoResponse)
        self.assertEqual(quiet.reason, 'payment_not_eligible')
        with self.assertRaises(RuntimeError):
            run_handler(definition, execution_context(identifier, 'notification', 'es', False,
                _freeze({'payment_id': 'broken'})), {})
        paid = run_handler(definition, execution_context(identifier, 'notification', 'es', False,
            _freeze({'payment_id': '15'})), {})
        self.assertEqual(paid['status'], 'payment_received')
        self.assertEqual(paid['recipient'], '50400000000')
        self.assertEqual(paid['data']['amount'], '15')
        self.assertNotIn('recipient', paid['data'])
        bot_id = 'messaging.' + self.prefix + '_execution'
        register_processes(app='messaging', processes=[
            NotifyProcess(id=identifier, label='Completion', definition=notify_definition, handler=notify_handle),
            BotProcess(id=bot_id, label='Balance Query', definition=bot_definition, handler=bot_handle)])
        bot = frappe.get_doc('Process Definition', bot_id)
        result = run_handler(bot, execution_context(bot_id, 'bot_flow', 'es', False,
            _freeze({'contract_number': '00123'})), {})
        self.assertEqual(result['data'], {'balance': '00123'})
        self.assertEqual(result['message'], 'Prepared')
        validate_process_schema_v1(result)

    def test_installed_presets_use_the_specialized_declarations(self):
        from messaging.registry import load_declarations
        from messaging.presets import register as defaults
        declarations = load_declarations(defaults.__file__, 'processes')
        self.assertTrue(declarations)
        self.assertTrue(all(type(item) is BotProcess for item in declarations))
        self.assertEqual({item.id for item in declarations}, {
            'messaging.registration', 'messaging.greeting', 'messaging.human_availability'})
