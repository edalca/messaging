"""Rollback-only preset synchronization, ownership, localization and installation."""
from contextlib import ExitStack
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import frappe
from werkzeug.wrappers import Request
from messaging.registry.test_registry import owner_application
from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures
from messaging.chatbot.doctype.bot_flow.configuration import validate_preset_configuration
from messaging.chatbot.doctype.bot_flow_presets import bot_flow_presets as presets
from messaging.chatbot.doctype.bot_settings.bot_settings import apply_flow_presets
from messaging.registrations.processes import BotRequest, BotResponse


def capture_flow(*, params, outcomes):
    params.register('name', type='string', required=True, multiple=False)
    outcomes.register('success', fields={'language': 'string'}, mode=['template', 'message'], action='complete_flow')


def capture_business(request: BotRequest) -> BotResponse:
    return BotResponse('success', data={'language': request.language})


class TestBotFlowPresets(ProcessFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.process = self.register()
        self.key = self.prefix + '_preset'
        self.common = dict(inputs={}, outcomes={'success': {'mode': 'template'}}, options={'priority': 0})
        self.es = dict(semantic_description='Spanish context', outcomes={'success': {
            'template_name': self.prefix + ' Spanish', 'body': 'Language {{language}}'}})
        self.en = dict(semantic_description='English context', outcomes={'success': {
            'template_name': self.prefix + ' English', 'body': 'Hello {{language}}'}})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        source = '''from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import save_preset
from messaging.chatbot.doctype.bot_flow_preset_language.bot_flow_preset_language import add_language
KEY = KEY_VALUE
PROCESS = PROCESS_VALUE
COMMON = COMMON_VALUE
LOCALIZED = LOCALIZED_VALUE

def register():
    options = COMMON.get('options') or {}
    save_preset(key=KEY, name='Test Preset', process=PROCESS, inputs=COMMON.get('inputs'),
        outcomes=COMMON.get('outcomes'), priority=options.get('priority', 0),
        show_in_menu=options.get('show_in_menu', False), menu_order=options.get('menu_order', 0))
    add_language(preset=KEY, language='es', **LOCALIZED)
'''.replace('KEY_VALUE', repr(self.key)).replace('PROCESS_VALUE', repr(self.process)).replace('COMMON_VALUE', repr(self.common)).replace('LOCALIZED_VALUE', repr(self.es))
        self.owner, self.main = self.stack.enter_context(owner_application(source, prefix='zz_owner'))
        source = '''from messaging.chatbot.doctype.bot_flow_preset_language.bot_flow_preset_language import add_language
LOCALIZED = LOCALIZED_VALUE

def register():
    add_language(preset=KEY_VALUE, language='en', **LOCALIZED)
'''.replace('KEY_VALUE', repr(self.key)).replace('LOCALIZED_VALUE', repr(self.en))
        self.extension, self.language = self.stack.enter_context(owner_application(source, prefix='aa_extension'))
        self.include_main = True
        self.include_extension = True

    def synchronize(self):
        main = self.main.provision((self.main.register,) if self.include_main else ())
        extension = self.language.provision((self.language.register,) if self.include_extension else ())
        return dict(presets=main['presets'], languages=main['languages'] + extension['languages'])

    def enable_english(self):
        settings = frappe.get_doc('Bot Settings')
        if not any(row.language == 'en' for row in settings.chatbot_languages):
            settings.append('chatbot_languages', dict(language='en', enabled=1, is_default=0))
            settings.save()

    def install(self):
        self.synchronize()
        self.enable_english()
        result = apply_flow_presets([self.key])
        self.assertFalse(result['not_applied'], result)
        return frappe.get_doc('Bot Flow', result['created'][0]['name'])

    def listing(self):
        return next(row for row in presets.list_presets()['presets'] if row['key'] == self.key)

    def test_order_idempotence_hashes_and_no_consumer_materialization(self):
        counts = [frappe.db.count(kind) for kind in ('Bot Flow', 'Bot Flow Template')]
        self.assertLess(self.extension, self.owner)
        self.assertEqual(self.synchronize(), dict(presets=1, languages=2))
        doc = frappe.get_doc('Bot Flow Presets', self.key)
        before = (doc.modified, doc.configuration_hash, [(r.language, r.configuration_hash) for r in doc.languages])
        self.synchronize();doc.reload()
        self.assertEqual(before, (doc.modified, doc.configuration_hash, [(r.language, r.configuration_hash) for r in doc.languages]))
        self.assertEqual(counts, [frappe.db.count(kind) for kind in ('Bot Flow', 'Bot Flow Template')])
        self.assertEqual(doc.application, self.owner)
        self.assertEqual({r.language: r.application for r in doc.languages}, dict(es=self.owner, en=self.extension))
        self.assertEqual(presets.configuration_hash(dict(b=2, a=1)), presets.configuration_hash(dict(a=1, b=2)))

    def test_owner_collisions_roll_back_and_restore_context(self):
        self.synchronize()
        before = frappe.get_doc('Bot Flow Presets', self.key).as_dict()
        operation = dict(preset=self.key, language='es', data=self.es, app=self.extension, method=self.extension + '.contribution.register')
        with self.assertRaises(frappe.PermissionError):
            presets._save_language(**operation)
        with self.assertRaises(frappe.PermissionError):
            presets._save_main(key=self.key, name='Foreign', process=self.process, data=self.common,
                app=self.extension, method=self.extension + '.contribution.register')
        self.assertEqual(before, frappe.get_doc('Bot Flow Presets', self.key).as_dict())
        self.language.LOCALIZED = dict(semantic_description='Invalid', outcomes={'unknown': self.en['outcomes']['success']})
        with self.assertRaises(frappe.ValidationError):
            self.synchronize()
        self.assertIsNone(presets._synchronization.get())
        self.assertEqual(before, frappe.get_doc('Bot Flow Presets', self.key).as_dict())

    def test_unknown_and_message_outcomes_reject_localized_content(self):
        for localized in (dict(semantic_description='Text', outcomes={'unknown': self.en['outcomes']['success']}),
                dict(semantic_description='Text', priority=10)):
            with self.subTest(localized=localized), self.assertRaises(frappe.ValidationError):
                validate_preset_configuration(self.process, self.common, language='en', localized=localized)
        common = deepcopy(self.common)
        common['outcomes']['success']['mode'] = 'message'
        with self.assertRaisesRegex(frappe.ValidationError, 'Message outcomes'):
            validate_preset_configuration(self.process, common, language='en', localized=self.en)
        validate_preset_configuration(self.process, common, language='en', localized=dict(semantic_description='Message context'))
        common['outcomes']['unknown'] = common['outcomes'].pop('success')
        with self.assertRaises(frappe.ValidationError):
            validate_preset_configuration(self.process, common)

    def test_install_creates_native_templates_and_independent_versions(self):
        flow = self.install()
        self.assertEqual(flow.application, self.owner)
        self.assertEqual((flow.status, flow.process_test_status), ('Disabled', 'Untested'))
        self.assertEqual({r.language for r in flow.output_translations}, {'es', 'en'})
        for row in flow.output_translations:
            template = frappe.get_doc('Bot Flow Template', row.template_reference)
            self.assertEqual((template.doctype, template.language), ('Bot Flow Template', row.language))
            self.assertIn('language', [r.parameter_name for r in template.template_variables])
        versions = json.loads(flow.preset_versions)
        self.assertEqual(versions['languages']['en']['application'], self.extension)
        self.assertFalse(self.listing()['selectable'])
        self.assertEqual(len(apply_flow_presets([self.key])['existing']), 1)

    def test_partial_changes_require_confirmation_and_preserve_other_languages(self):
        flow = self.install()
        before = flow.as_dict()
        spanish = next(row.template_reference for row in flow.output_translations if row.language == 'es')
        self.language.LOCALIZED['outcomes']['success']['body'] = 'Changed {{language}}'
        self.synchronize()
        self.assertEqual(before, frappe.get_doc('Bot Flow', flow.name).as_dict())
        row = self.listing()
        self.assertEqual(row['changed_languages'], ['en'])
        self.assertFalse(row['common_changed'])
        self.assertTrue(apply_flow_presets([self.key])['not_applied'])
        result = apply_flow_presets([self.key], reset_keys=[self.key])
        self.assertFalse(result['not_applied'], result)
        flow.reload()
        self.assertEqual(spanish, next(row.template_reference for row in flow.output_translations if row.language == 'es'))
        english = next(row.template_reference for row in flow.output_translations if row.language == 'en')
        self.assertEqual(frappe.get_doc('Bot Flow Template', english).body, 'Changed {{language}}')
        self.assertFalse(self.listing()['selectable'])

    def test_manual_changes_are_visible_and_restoration_keeps_identity(self):
        flow = self.install()
        flow.translations[0].semantic_description = 'Administrative edit'
        flow.save()
        self.assertIn(flow.translations[0].language, self.listing()['manual_changes'])
        self.assertTrue(apply_flow_presets([self.key])['not_applied'])
        result = apply_flow_presets([self.key], reset_keys=[self.key])
        self.assertFalse(result['not_applied'], result)
        self.assertEqual(result['completed'][0]['name'], flow.name)
        self.assertFalse(self.listing()['requires_reset'])

    def test_omitted_declarations_preserve_persisted_defaults(self):
        self.synchronize()
        self.enable_english()
        before = frappe.get_doc('Bot Flow Presets', self.key).as_dict()
        self.include_extension = False
        self.synchronize()
        self.assertEqual(before, frappe.get_doc('Bot Flow Presets', self.key).as_dict())
        self.include_main = False
        self.synchronize()
        self.assertEqual(before, frappe.get_doc('Bot Flow Presets', self.key).as_dict())

    def test_read_dialog_does_not_execute_hooks_and_ordinary_writes_are_rejected(self):
        self.synchronize()
        with patch.object(presets, '_provenance', side_effect=AssertionError('Dialog cannot execute declarations')):
            self.assertEqual(self.listing()['status'], 'Pending creation')
        doc = frappe.get_doc('Bot Flow Presets', self.key)
        with self.assertRaises(frappe.PermissionError):
            doc.save(ignore_permissions=True)
        with self.assertRaises(frappe.PermissionError):
            presets.save_preset(key=self.key, name='Spoofed', process=self.process,
                inputs=self.common.get('inputs'), outcomes=self.common.get('outcomes'),
                priority=self.common.get('options', {}).get('priority', 0))
        with patch.object(frappe.local, 'request', Request.from_values(), create=True):
            with self.assertRaises(frappe.PermissionError):
                self.main.register()
        frappe.set_user('Guest')
        try:
            with self.assertRaises(frappe.PermissionError):
                presets.list_presets()
            with self.assertRaises(frappe.PermissionError):
                apply_flow_presets([self.key])
        finally:
            frappe.set_user('Administrator')

    def test_common_update_is_detected_and_explicitly_restored(self):
        flow = self.install()
        self.main.COMMON['options']['menu_order'] = 17
        self.synchronize()
        self.assertTrue(self.listing()['common_changed'])
        self.assertEqual(frappe.get_doc('Bot Flow', flow.name).menu_order, 0)
        result = apply_flow_presets([self.key], reset_keys=[self.key])
        self.assertFalse(result['not_applied'], result)
        flow.reload()
        self.assertEqual(flow.menu_order, 17)
        self.assertFalse(self.listing()['requires_reset'])

    def test_new_language_is_applied_without_replacing_installed_content(self):
        self.synchronize()
        result = apply_flow_presets([self.key])
        self.assertFalse(result['not_applied'], result)
        flow = frappe.get_doc('Bot Flow', result['created'][0]['name'])
        self.assertEqual({r.language for r in flow.translations}, {'es'})
        self.assertEqual(self.listing()['installed_languages'], ['es'])
        self.assertEqual(self.listing()['declared_languages'], ['en', 'es'])
        self.approve(flow)
        self.activate(flow)
        self.assertEqual(flow.status, 'Enabled')
        spanish = next(row.template_reference for row in flow.output_translations if row.language == 'es')
        self.enable_english()
        self.assertEqual(self.listing()['pending_languages'], ['en'])
        self.assertEqual(self.listing()['installed_languages'], ['es'])
        result = apply_flow_presets([self.key])
        self.assertFalse(result['not_applied'], result)
        flow.reload()
        self.assertEqual({r.language for r in flow.translations}, {'es', 'en'})
        self.assertEqual(flow.status, 'Disabled')
        self.assertEqual(spanish, next(row.template_reference for row in flow.output_translations if row.language == 'es'))
        self.assertEqual(self.listing()['installed_languages'], ['en', 'es'])

    def test_installed_languages_follow_flow_rows_not_hashes(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import _preset_write
        from messaging.chatbot.doctype.bot_flow.preset_application import installed_flow_languages
        sample = frappe._dict(
            translations=[frappe._dict(language='es'), frappe._dict(language='')],
            input_translations=[frappe._dict(language='es')],
            output_translations=[frappe._dict(language='pt')],
            parameters=[frappe._dict(request_phrases=[frappe._dict(language='fr')])],
        )
        self.assertEqual(installed_flow_languages(sample), ['es', 'pt'])
        self.assertEqual(installed_flow_languages(None), [])
        self.synchronize()
        self.assertEqual(self.listing()['installed_languages'], [])
        self.assertEqual(self.listing()['declared_languages'], ['en', 'es'])
        flow = self.install()
        self.assertEqual(self.listing()['installed_languages'], ['en', 'es'])
        for table in ('translations', 'input_translations', 'output_translations'):
            flow.set(table, [row for row in (flow.get(table) or []) if row.language != 'en'])
        versions = json.loads(flow.preset_versions)
        versions['languages']['fr'] = dict(versions['languages']['en'])
        del versions['languages']['en']
        with _preset_write():
            flow.preset_versions = json.dumps(versions, sort_keys=True)
            flow.save()
        listed = self.listing()
        self.assertEqual(listed['installed_languages'], ['es'])
        self.assertEqual(listed['declared_languages'], ['en', 'es'])
        self.assertIn('en', listed['pending_languages'])
        self.assertNotIn('en', listed['installed_languages'])
        self.assertIn('fr', listed['unavailable_languages'])
        self.assertNotIn('fr', listed['installed_languages'])

    def test_restoring_localized_capture_preserves_the_shared_parameter(self):
        from messaging.registrations.processes import BotProcess
        from messaging.registry.processes import register_processes
        self.process += '_capture'
        self.main.PROCESS = self.process
        self.definitions.append(BotProcess(id=self.process, label='Capture', definition=capture_flow, handler=capture_business))
        register_processes(app='messaging', processes=self.definitions)
        self.main.COMMON['inputs'] = {'name': {'parameter': {
            'code': self.prefix + '_name', 'label': 'Name', 'parameter_type': 'Name'}, 'capture_order': 1}}
        self.main.COMMON['options']['priority'] = 100000 + int(self.prefix.removeprefix('technical_'), 16) % 1000000
        for localized, phrase in ((self.main.LOCALIZED, 'Your name?'), (self.language.LOCALIZED, 'Name please?')):
            localized.update(input_initial_message='Start', input_continuation_message='Continue',
                parameters={'name': {'request_phrase': phrase}})
        flow = self.install()
        original = flow.parameters[0].parameter
        original_phrases = [(row.language, row.request_phrase) for row in frappe.get_doc('Bot Parameter', original).request_phrases]
        self.language.LOCALIZED['parameters']['name']['request_phrase'] = 'Updated English phrase'
        self.synchronize()
        result = apply_flow_presets([self.key], reset_keys=[self.key])
        self.assertFalse(result['not_applied'], result)
        flow.reload()
        self.assertNotEqual(original, flow.parameters[0].parameter)
        self.assertEqual(original_phrases, [(row.language, row.request_phrase) for row in frappe.get_doc('Bot Parameter', original).request_phrases])
        self.assertFalse(self.listing()['requires_reset'])

    def test_stale_confirmation_and_missing_template_content_are_not_applied(self):
        self.synchronize()
        version = presets.declaration_versions(frappe.get_doc('Bot Flow Presets', self.key))
        self.main.COMMON['options']['menu_order'] = 3
        self.synchronize()
        result = apply_flow_presets([self.key], expected_versions={self.key: version})
        self.assertTrue(result['not_applied'])
        self.main.LOCALIZED.pop('outcomes')
        self.synchronize()
        self.assertIn('es', self.listing()['missing_content'])
        self.assertFalse(self.listing()['selectable'])

    def test_manual_document_and_api_mutations_are_denied_even_with_ignored_permissions(self):
        from frappe.client import delete as api_delete, insert as api_insert, set_value
        self.synchronize()
        manager = self.prefix + '@example.test'
        frappe.get_doc(dict(doctype='User', email=manager, first_name='Preset Manager',
            enabled=1, user_type='System User', send_welcome_email=0,
            roles=[dict(role='System Manager')])).insert()
        parent = frappe.get_doc('Bot Flow Presets', self.key)
        names = [('Bot Flow Presets', parent.name), ('Bot Flow Preset Language', parent.languages[0].name)]
        before = {kind: frappe.get_doc(kind, name).as_dict() for kind, name in names}
        for user in ('Administrator', manager):
            frappe.set_user(user)
            for kind, name in names:
                doc = frappe.get_doc(kind, name)
                doc.flags.update(ignore_permissions=True, ignore_validate=True, in_import=True)
                for permission in ('create', 'write', 'delete', 'import'):
                    with self.subTest(user=user, kind=kind, permission=permission):
                        self.assertFalse(doc.has_permission(permission))
                        with self.assertRaises(frappe.PermissionError):
                            doc.check_permission(permission)
                operations = (
                    lambda: doc.insert(ignore_permissions=True),
                    lambda: doc.save(ignore_permissions=True),
                    doc.db_insert, doc.db_update,
                    lambda: doc.db_set('available', 0),
                    lambda: doc.delete(ignore_permissions=True, force=True),
                    lambda: frappe.delete_doc(kind, name, ignore_permissions=True, force=True),
                    lambda: doc.run_method('before_rename', name, name + '_renamed', False),
                    lambda: set_value(kind, name, 'available', 0),
                    lambda: api_insert(doc.as_dict()),
                )
                for operation in operations:
                    with self.subTest(user=user, kind=kind, operation=operation):
                        with self.assertRaises(frappe.PermissionError):
                            operation()
                # Frappe conceals unreadable child-parent write permissions as a missing document.
                with self.assertRaises((frappe.PermissionError, frappe.DoesNotExistError)):
                    api_delete(kind, name)
                self.assertEqual(before[kind], frappe.get_doc(kind, name).as_dict())
            parent = frappe.get_doc('Bot Flow Presets', self.key)
            parent.set('languages', [])
            with self.assertRaises(frappe.PermissionError):
                parent.update_children()
        # Read-only documentary permissions do not prevent the verified owner synchronization.
        self.language.LOCALIZED['semantic_description'] = 'Updated by the authorized owner'
        self.synchronize()
        self.assertEqual(json.loads(next(row.configuration for row in
            frappe.get_doc('Bot Flow Presets', self.key).languages if row.language == 'en'))[
                'semantic_description'], 'Updated by the authorized owner')

    def test_guard_and_owner_hooks_cannot_write_outside_authorized_synchronization(self):
        self.synchronize()
        doc = frappe.get_doc('Bot Flow Presets', self.key)
        before = doc.as_dict()
        with presets._preset_write():
            for operation in (lambda: doc.save(ignore_permissions=True), doc.db_update,
                    doc.languages[0].db_update, lambda: presets._persist(doc, None)):
                with self.assertRaises(frappe.PermissionError):
                    operation()
        for register in (self.main.register, self.language.register):
            with self.assertRaises(frappe.PermissionError):
                register()
        self.assertEqual(before, frappe.get_doc('Bot Flow Presets', self.key).as_dict())
        self.assertIsNone(presets._synchronization.get())

    def test_active_parent_write_cannot_authorize_independent_language_or_parent_documents(self):
        from frappe.model.document import Document
        self.synchronize()
        original_save = Document.save
        attempts = []

        def save(parent, *args, **kwargs):
            if parent.doctype == 'Bot Flow Presets':
                independent = frappe.get_doc('Bot Flow Presets', parent.name)
                row = frappe.get_doc('Bot Flow Preset Language', parent.languages[0].name)
                for operation in (lambda: original_save(independent, ignore_permissions=True),
                        row.db_update, lambda: parent.languages[0].save(ignore_permissions=True)):
                    with self.assertRaises(frappe.PermissionError):
                        operation()
                    attempts.append(True)
            return original_save(parent, *args, **kwargs)

        self.main.COMMON['options']['menu_order'] = 4
        with patch.object(Document, 'save', new=save):
            self.synchronize()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(json.loads(frappe.get_doc('Bot Flow Presets', self.key).configuration)[
            'options']['menu_order'], 4)

    def test_desk_metadata_and_boot_allow_reading_but_no_catalogue_mutation(self):
        from pathlib import Path
        from messaging.boot import boot_session
        for folder in ('bot_flow_presets', 'bot_flow_preset_language'):
            path = Path(__file__).parents[1] / folder / (folder + '.json')
            model = json.loads(path.read_text())
            self.assertTrue(all(field['read_only'] for field in model['fields']))
            self.assertFalse(model['allow_import'])
            self.assertFalse(model['allow_rename'])
            for permission in model['permissions']:
                self.assertTrue(permission['read'])
                self.assertFalse(any(permission.get(key) for key in ('create', 'write', 'delete', 'import')))
        mutations = ('can_create', 'can_write', 'can_delete', 'can_import')
        boot = {'user': {key: ['Bot Flow Presets', 'Bot Flow Preset Language', 'Bot Flow'] for key in mutations}}
        boot_session(boot)
        self.assertEqual(boot['user'], {key: ['Bot Flow'] for key in mutations})

    def test_installed_bot_flow_still_accepts_ordinary_administrative_edits(self):
        flow = self.install()
        self.assertTrue(flow.has_permission('write'))
        flow.workflow_name = 'Administrator edited Flow'
        flow.save()
        self.assertEqual(frappe.get_doc('Bot Flow', flow.name).workflow_name, 'Administrator edited Flow')

    def test_explicit_provisioning_preserves_hashes_ownership_and_omitted_languages(self):
        from importlib import import_module
        self.synchronize()
        doc = frappe.get_doc('Bot Flow Presets', self.key)
        common_hash = doc.configuration_hash
        original = {row.language: (row.name, row.application, row.configuration_hash) for row in doc.languages}
        self.language.LOCALIZED['semantic_description'] = 'Updated English definition'
        self.synchronize()
        doc.reload()
        self.assertEqual(doc.configuration_hash, common_hash)
        for row in doc.languages:
            self.assertEqual((row.name, row.application), original[row.language][:2])
            self.assertEqual(row.configuration_hash == original[row.language][2], row.language == 'es')
        self.include_extension = False
        self.synchronize()
        doc.reload()
        self.assertTrue(doc.available)
        self.assertTrue(next(row.available for row in doc.languages if row.language == 'en'))
        self.assertEqual(len(doc.languages), 2)
