"""Shared consumer identity and log redaction for current Process execution."""
import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from messaging.communication.doctype.process_definition import execution as process
from messaging.chatbot.doctype.bot_flow import execution as flow_execution
from messaging.chatbot.doctype.bot_settings import bot_settings as settings_module
from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log


class TestProcessExecutionLog(unittest.TestCase):
	def setUp(self):
		frappe.set_user('Administrator')
		frappe.db.savepoint('current_process_log')

	def tearDown(self):
		frappe.db.rollback(save_point='current_process_log')

	def test_current_consumer_identity_and_redaction(self):
		log=record_process_execution_log(process_method='messaging.synthetic.process',
			execution_outcome='Functional Status',functional_status='success',arguments={'token':'synthetic-sensitive-value'})
		self.assertEqual(log.source_type,'Bot Flow')
		self.assertNotIn('synthetic-sensitive-value',log.redacted_arguments_json)
		other=record_process_execution_log(process_method='messaging.synthetic.available',execution_context='Human Availability',
			execution_outcome='Functional Status',functional_status='available',source_type='Human Availability',source_name='Bot Settings')
		self.assertEqual((other.source_type,other.source_name),('Human Availability','Bot Settings'))

class TestAuditWriteProtection(unittest.TestCase):
	def setUp(self):
		frappe.set_user('Administrator')
		frappe.db.savepoint('audit_write_protection')

	def tearDown(self):
		frappe.db.rollback(save_point='audit_write_protection')

	def test_all_process_sources_reject_forged_flags_and_skipped_validation(self):
		for source in ('Bot Flow', 'Human Availability', 'Notification Rule'):
			for flags in ({'messaging_write': True, 'recording_notification': True}, {'ignore_validate': True}):
				doc = frappe.get_doc(dict(doctype='Process Execution Log', process_method='messaging.synthetic.process',
					source_type=source, execution_context='Workflow Process', execution_outcome='Internal Execution Error'))
				doc.flags.update(flags)
				with self.assertRaises(frappe.PermissionError): doc.insert(ignore_permissions=True)

	def test_legitimate_log_cannot_be_edited_deleted_or_renamed(self):
		log = record_process_execution_log(process_method='messaging.synthetic.process', execution_outcome='Internal Execution Error')
		log.diagnostic_detail = 'forged'
		with self.assertRaises(frappe.PermissionError): log.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError): frappe.delete_doc(log.doctype, log.name, ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError): log.run_method('before_rename', log.doctype, log.name, 'synthetic-forged-name')
		self.assertTrue(frappe.db.exists(log.doctype, log.name))

	def test_independent_write_guards_restore_after_nested_exception(self):
		from messaging.common.write_guards import InternalWriteGuard
		one, two = InternalWriteGuard('test_one'), InternalWriteGuard('test_two')
		with one():
			self.assertTrue(one.active()); self.assertFalse(two.active())
			with self.assertRaises(RuntimeError):
				with one(), two(): raise RuntimeError('controlled')
			self.assertTrue(one.active()); self.assertFalse(two.active())
		self.assertFalse(one.active()); self.assertFalse(two.active())

	def test_other_documents_reject_browser_flags(self):
		for doctype, flag in [('Chat Message', 'messaging_write'), ('AI Request Log', 'messaging_evaluation'),
			('Communication Provider', 'catalog_sync'), ('Notification Queue', 'internal_write')]:
			doc = frappe.new_doc(doctype)
			doc.flags.update({flag: True, 'ignore_validate': True})
			with self.assertRaises(frappe.PermissionError): doc.run_method('before_validate')

"""Source-only presentation policy and scoped Frappe message regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import frappe

from messaging.registry.processes import inspection as policy
from messaging.communication.doctype.process_definition import validation as testing
from messaging.communication.doctype.process_definition.execution import process_messages


class TestStaticPolicy(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'sample'
        self.root.mkdir()
        (self.root / '__init__.py').write_text("raise AssertionError('Never import inspected code')\n")

    def inspect(self, source, helper=None):
        (self.root / 'processes.py').write_text(source)
        if helper is not None:
            (self.root / 'helpers.py').write_text(helper)
        return policy.inspect_process('sample.processes.run', self.root)

    def test_direct_alias_and_presentation_helpers(self):
        cases = [
            ('import frappe', 'frappe.throw("secret")', 'FRAPPE_THROW'),
            ('import frappe as f', 'f.msgprint("secret")', 'FRAPPE_MSGPRINT'),
            ('from frappe import throw as fail', 'fail("secret")', 'FRAPPE_THROW'),
            ('from frappe import msgprint as show', 'show("secret")', 'FRAPPE_MSGPRINT'),
            ('from frappe.utils.messages import throw as fail', 'fail("secret")', 'FRAPPE_THROW'),
            ('from frappe.utils import messages as ui', 'ui.toast("secret")', 'FRAPPE_UI_OUTPUT'),
            ('import frappe', 'frappe.throw_permission_error()', 'FRAPPE_UI_OUTPUT'),
        ]
        for imports, statement, rule in cases:
            with self.subTest(statement=statement):
                result = self.inspect(imports + '\ndef run():\n    ' + statement + '\n')
                self.assertEqual(result['status'], 'violations')
                self.assertEqual(result['findings'][0]['rule'], rule)
                self.assertEqual(result['findings'][0]['line'], 3)
                self.assertNotIn('secret', json.dumps(result))

    def test_message_container_mutations_and_aliases(self):
        statements = [
            'frappe.local.message_log = []', 'frappe.message_log.append("private")',
            'frappe.local.message_log[0] = "private"', 'del frappe.local.message_log[:]',
            'log = frappe.local.message_log\n    log.extend([])',
            'setattr(frappe.local, "message_log", [])',
            'frappe.response["_server_messages"] = "private"',
            'frappe.local.response.update(_server_messages="private")',
            'response = frappe.local.response\n    response.update({"_server_messages": "private"})',
        ]
        for statement in statements:
            with self.subTest(statement=statement):
                result = self.inspect('import frappe\ndef run():\n    ' + statement + '\n')
                self.assertTrue(result['findings'])
        result = self.inspect('import frappe\ndef run():\n    log = frappe.local.message_log\n    return len(log)\n')
        self.assertFalse(result['findings'], 'Reading an alias is not a mutation')

    def test_local_imported_helpers_callbacks_and_cycles(self):
        result = self.inspect('from .helpers import helper as h\ndef run():\n    return h()\n',
            'from frappe import msgprint as show\ndef helper():\n    show("private")\n')
        self.assertEqual(result['findings'][0]['function'], 'sample.helpers.helper')
        self.assertEqual(result['findings'][0]['rule'], 'FRAPPE_MSGPRINT')
        result = self.inspect('import frappe\ndef run():\n    return helper()\ndef helper():\n    run()\n    frappe.throw("private")\n')
        self.assertEqual(len(result['functions']), 2)
        self.assertEqual(len(result['findings']), 1)
        result = self.inspect('import frappe\ndef run():\n    return dispatch(helper)\ndef dispatch(fn):\n    return fn()\ndef helper():\n    frappe.msgprint("private")\n')
        self.assertEqual(result['findings'][0]['rule'], 'FRAPPE_MSGPRINT')

    def test_legitimate_exceptions_reads_and_operational_writes(self):
        source = '''import frappe
def run(test=False):
    frappe.db.get_value("Example", "name")
    frappe.get_doc("Example", "name")
    frappe.get_all("Example")
    if not test:
        frappe.db.set_value("Example", "name", "field", "value")
    raise ValueError("Technical failure")
'''
        result = self.inspect(source)
        self.assertTrue(result['inspectable'])
        self.assertFalse(result['findings'])

    def test_missing_main_dynamic_calls_and_unreachable_violations(self):
        result = self.inspect('def unrelated():\n    pass\n')
        self.assertFalse(result['inspectable'])
        result = self.inspect('def run(callback):\n    return callback()\n')
        self.assertEqual(result['status'], 'partial')
        self.assertFalse(result['findings'])
        result = self.inspect('import frappe\ndef run():\n    return 1\ndef unused():\n    frappe.throw("unreachable")\n')
        self.assertEqual(result['status'], 'clear')

    def test_cache_rechecks_helper_bytes_and_policy(self):
        source = 'from .helpers import helper\ndef run():\n    return helper()\n'
        first = self.inspect(source, 'def helper():\n    return 1\n')
        second = self.inspect(source, 'import frappe\ndef helper():\n    frappe.throw("private")\n')
        self.assertNotEqual(first['sources'], second['sources'])
        self.assertTrue(second['findings'])
        with patch.object(policy, 'POLICY_VERSION', policy.POLICY_VERSION + 1):
            third = self.inspect(source)
        self.assertNotEqual(third['policy'], second['policy'])

    def test_loaded_helper_source_change_requires_worker_reload(self):
        import sys
        from types import CodeType, FunctionType, SimpleNamespace
        from messaging.communication.doctype.process_definition.test_process_definition import technical_flow
        from messaging.security.permissions import verify_loaded_method
        helper_file = self.root / 'helpers.py'
        helper_file.write_text('def helper():\n    return 1\n')
        code = next(value for value in compile(helper_file.read_bytes(), str(helper_file), 'exec').co_consts
            if isinstance(value, CodeType))
        helper = FunctionType(code, {})
        inspection = dict(functions=['sample.helpers.helper'])
        with patch.dict(sys.modules, {'sample.helpers': SimpleNamespace(helper=helper)}):
            verify_loaded_method(technical_flow, inspection=inspection)
            helper_file.write_text('def helper():\n    return 2\n')
            with self.assertRaises(frappe.ValidationError):
                verify_loaded_method(technical_flow, inspection=inspection)


class TestProcessMessageScope(unittest.TestCase):
    def test_native_throw_caught_failure_direct_mutation_and_nested_restoration(self):
        original = frappe.local.message_log
        original[:] = [frappe._dict(message='Existing administrative message')]
        previous_mute = frappe.flags.mute_messages
        frappe.response['_server_messages'] = 'existing serialized messages'
        try:
            with process_messages():
                with self.assertRaises(frappe.ValidationError):
                    frappe.throw('Private Process exception')
                frappe.msgprint('Private message', realtime=True)
                frappe.local.message_log.append(dict(message='Direct private mutation'))
                with process_messages():
                    frappe.msgprint('Nested private message')
                frappe.response['_server_messages'] = 'private serialization'
            self.assertIs(frappe.local.message_log, original)
            self.assertEqual([m.message for m in original], ['Existing administrative message'])
            self.assertEqual(frappe.response['_server_messages'], 'existing serialized messages')
            self.assertEqual(frappe.flags.mute_messages, previous_mute)
            with self.assertRaises(frappe.ValidationError):
                frappe.throw('Legitimate form validation')
            self.assertIn('Legitimate form validation', str(frappe.local.message_log))
        finally:
            original.clear()
            frappe.response.pop('_server_messages', None)

"""Candidate configuration, session and Redis regressions for registered Processes."""
import unittest
from unittest.mock import patch

import frappe

from messaging.communication.doctype.process_definition import validation as testing
from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures


class TestProcessPrevalidation(ProcessFixtures, unittest.TestCase):
    def test_native_descriptor_hides_reserved_context_and_discards_browser_paths(self):
        flow = self.flow()
        payload = dict(flow.as_dict(), process_method='frappe.untrusted.client')
        descriptor = testing.describe(payload)
        self.assertIsNone(descriptor['configuration_error'])
        self.assertFalse({'context', 'language', 'test'} & {f['fieldname'] for f in descriptor['fields']})
        candidate = testing._configuration(payload)
        testing._validate_candidate(candidate)
        reconstructed = testing._configuration(candidate.as_dict())
        testing._validate_candidate(reconstructed)
        self.assertEqual(candidate.process_method, reconstructed.process_method)
        self.assertNotEqual(candidate.process_method, 'frappe.untrusted.client')

    def test_changed_revision_and_missing_session_reject_promotion(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process
        flow = self.flow()
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        candidate = flow.as_dict()
        flow.workflow_name = 'Changed display name'
        flow.save()
        with self.assertRaises(frappe.TimestampMismatchError):
            validate_process(candidate)
        with self.assertRaises(frappe.PermissionError):
            testing.test_method(flow.as_dict(), {})
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError):
            validate_process(flow.as_dict())

    def test_endpoint_methods_roles_and_document_permissions(self):
        flow = self.flow()
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[testing.test_method], ['POST'])
        self.assertEqual(frappe.allowed_http_methods_for_whitelisted_func[testing.describe], ['POST'])
        with patch('frappe.model.document.Document.check_permission', side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError):
                testing.test_method(flow.as_dict(), {})
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError):
            testing.describe(flow.as_dict())

    def test_failed_retest_preserves_legitimate_persistent_receipt(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import _lifecycle_write
        flow = self.flow()
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        old = frappe.get_doc(flow.doctype, flow.name).as_dict()
        with _lifecycle_write(), patch(
            'messaging.communication.doctype.process_definition.execution.verify_loaded_method',
            side_effect=frappe.ValidationError('Process code changed. Reload the application process before testing.'),
        ):
            self.assertEqual(testing.test_method(flow.as_dict(), {})['status'], 'Failed')
        self.assertEqual(frappe.get_doc(flow.doctype, flow.name).as_dict(), old)
        self.assertEqual(testing.test_state(frappe.get_doc(flow.doctype, flow.name)), 'Passed')

    def test_readable_series_and_legacy_coexistence(self):
        from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log
        one = record_process_execution_log(
            process_method='messaging.tests.synthetic_method',
            execution_outcome='Functional Status',
            functional_status='ok',
            source_type='Bot Flow',
            source_name='synthetic_flow',
            execution_context='Administrative Test',
            execution_id='exec-series-1',
        )
        two = record_process_execution_log(
            process_method='messaging.tests.synthetic_method',
            execution_outcome='Functional Status',
            functional_status='ok',
            source_type='Bot Flow',
            source_name='synthetic_flow',
            execution_context='Administrative Test',
            execution_id='exec-series-2',
        )
        self.assertRegex(one.name, r'^PEX-[0-9]{6}$')
        self.assertRegex(two.name, r'^PEX-[0-9]{6}$')
        self.assertNotEqual(one.name, two.name)
        self.assertEqual(one.execution_id, 'exec-series-1')
        legacy = frappe.get_all('Process Execution Log', filters={'name': ['not like', 'PEX-%']}, pluck='name', limit=1)
        if legacy:
            self.assertTrue(frappe.get_doc('Process Execution Log', legacy[0]).process_method)
