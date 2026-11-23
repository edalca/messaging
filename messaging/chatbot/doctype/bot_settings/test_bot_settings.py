import csv
import importlib
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

import frappe
from frappe.desk.form.load import getdoc, getdoctype
from frappe.model.base_document import get_controller
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.chatbot.doctype.bot_settings.localization import text as settings_text

from messaging.communication.doctype.process_execution_log.process_execution_log import (
	record_process_execution_log,
	redact_sensitive_mapping,
)
from messaging.chatbot.doctype.bot_settings.bot_settings import (
	compose_informative_response,
	get_bot_settings,
	is_registration_complete,
	resolve_process_language,
	validate_language,
	validate_chatbot_languages,
)


def set_localized_text(settings, key, value, language='es'):
    row = next((r for r in settings.message_translations if r.message_key == key and r.language == language), None)
    if row is None:
        row = settings.append('message_translations', dict(message_key=key, language=language))
    row.message = value
    return row


def configure_test_settings():
    """Explicit synthetic configuration; never called by application lifecycle."""
    from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
    settings = get_bot_settings()
    settings.set('chatbot_languages', [dict(language='es', enabled=1, is_default=1)])
    settings.set('message_translations', [])
    for key in MESSAGE_KEYS:
        set_localized_text(settings, key, 'Synthetic ' + key.replace('_', ' ') + '.')
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
    """Clear every stored Bot Settings value without saving."""
    settings = get_bot_settings()
    for field in settings.meta.fields:
        if field.fieldtype in ('Table', 'Table MultiSelect'):
            settings.set(field.fieldname, [])
        elif field.fieldtype not in ('Tab Break', 'Section Break', 'Column Break', 'HTML', 'Button'):
            settings.set(field.fieldname, 0 if field.fieldtype == 'Check' else None)
    return settings


def restore_bot_settings(snapshot, enabled_flows=(), channels_before=None, fixture_channels=()):
    """Restore and commit the exact Single snapshot even if later fixture deletion fails."""
    from messaging.communication.doctype.communication_channel.communication_channel import QUOTA_POLICY_FIELDS
    frappe.db.rollback()
    frappe.set_user("Administrator")
    current = frappe.get_single("Bot Settings")
    for field in current.meta.fields:
        if field.fieldtype not in ("Tab Break", "Section Break", "Column Break"):
            current.set(field.fieldname, snapshot.get(field.fieldname))
    for row in current.get_all_children():
        if not frappe.db.exists(row.doctype, row.name):
            row.set("__islocal", True)
    current.update_single(current.get_valid_dict())
    current.update_children()
    if enabled_flows:
        frappe.db.set_value("Bot Flow", {"name": ["in", list(enabled_flows)]}, "status", "Enabled", update_modified=False)
    for ch_name in fixture_channels or ():
        if not channels_before or ch_name not in channels_before:
            frappe.db.delete("Communication Channel", {"name": ch_name})
    for ch_name, ch_row in (channels_before or {}).items():
        if frappe.db.exists("Communication Channel", ch_name):
            frappe.db.set_value("Communication Channel", ch_name, {f: ch_row.get(f) for f in QUOTA_POLICY_FIELDS}, update_modified=False)
    frappe.clear_document_cache("Bot Settings", "Bot Settings")
    frappe.db.commit()


class TestBotSettings(FrappeTestCase):
	def setUp(self) -> None:
		super().setUp()
		frappe.set_user("Administrator")


	def tearDown(self) -> None:
		frappe.db.rollback()
		super().tearDown()


	def test_process_log_outcomes_and_sensitive_redaction(self) -> None:
		redacted = redact_sensitive_mapping(
			{"identity": "0801-1990-12345", "api_token": "secret-token", "attempts": 2}
		)
		self.assertNotIn("0801-1990-12345", str(redacted))
		self.assertNotIn("secret-token", str(redacted))

		log_ok = record_process_execution_log(
			process_method="app.mod.func",
			execution_outcome="Functional Status",
			functional_status="success",
			output_mode_used="Template",
			arguments={"identity": "0801-1990-12345"},
			raw_response={"version": 1, "recipient": "", "status": "success", "data": {"balance": "100"}, "message": ""},
			duration_ms=15,
		)
		self.assertEqual(log_ok.execution_outcome, "Functional Status")
		self.assertEqual(log_ok.functional_status, "success")
		self.assertNotIn("0801-1990-12345", log_ok.redacted_arguments_json)
		self.assertNotIn("100", log_ok.response_summary_json)

		log_invalid = record_process_execution_log(
			process_method="app.mod.func",
			execution_outcome="Invalid Schema Response",
			output_mode_used="Internal Error Fallback",
			diagnostic_error_type="ProcessSchemaValidationError",
			diagnostic_detail="Missing 'message' property",
		)
		self.assertEqual(log_invalid.execution_outcome, "Invalid Schema Response")
		self.assertIsNone(log_invalid.functional_status)


	def test_spanish_catalog_covers_native_metadata_and_messages(self) -> None:
		from frappe.translate import (
			extract_messages_from_python_code,
			get_messages_from_doctype,
			get_translations_from_csv,
		)

		root = Path(frappe.get_app_path("messaging"))
		with (root / "translations" / "es.csv").open(encoding="utf-8", newline="") as stream:
			rows = list(csv.reader(stream))
		self.assertTrue(all(len(row) in (2, 3) and all(row) for row in rows))
		catalog = {(row[0] + ':' + row[2] if len(row) == 3 else row[0]).replace('\\n', '\n'):
			row[1].replace('\\n', '\n').strip() for row in rows}
		self.assertEqual(len(rows), len(catalog), "Duplicate translation keys")
		self.assertEqual(get_translations_from_csv("es", "messaging"), catalog)
		for row in rows:
			source, target = row[:2]
			with self.subTest(source=source):
				self.assertTrue(source.replace("·", "-").isascii(), "Source text must remain in English")
				self.assertEqual(re.findall(r"\{\{[A-Za-z_]\w*\}\}|\{\d+\}|\{%.*?%\}", source),
					re.findall(r"\{\{[A-Za-z_]\w*\}\}|\{\d+\}|\{%.*?%\}", target))
				self.assertEqual((source.count("{"), source.count("}")),
					(target.count("{"), target.count("}")))

		# Verify the changed administrative metadata and message owners only.
		sources = set()
		for doctype in ('bot_settings', 'bot_parameter', 'bot_parameter_translation', 'flow_parameter', 'flow_consumer_state', 'registration_field', 'human_request_field'):
			path = root / 'chatbot' / 'doctype' / doctype / (doctype + '.json')
			sources.update(field['label'] for field in json.loads(path.read_text())['fields'] if field.get('label'))
		channel_json = root / 'communication' / 'doctype' / 'communication_channel' / 'communication_channel.json'
		sources.update(field['label'] for field in json.loads(channel_json.read_text())['fields'] if field.get('label'))
		for relative in ('chatbot/doctype/bot_parameter/bot_parameter.py', 'chatbot/doctype/bot_settings/bot_settings.py', 'chatbot/doctype/bot_settings/localization.py', 'communication/doctype/communication_channel/communication_channel.py'):
			path = root / relative
			sources.update(message[1] for message in extract_messages_from_python_code(path.read_text()))
		self.assertFalse(sources - catalog.keys(), f"Missing translations: {sources - catalog.keys()}")


	def test_desk_forms_load_and_permissions_are_enforced(self) -> None:
		new_doctypes = (
			"Bot Settings",
			"Bot Parameter",
			"Bot Flow",
			"Process Execution Log",
			"Flow Consumer State",
			"Registration Field",
			"Human Request Field",
			"Flow Parameter",
			"Flow Output",
		)
		old_doctypes = (
			"Conversation Settings",
			"Conversation Parameter",
			"Conversation Workflow",
			"Conversation Process Log",
			"Conversation Registration Parameter",
			"Conversation Menu Workflow",
			"Conversation Authorized Process",
			"Conversation Workflow Parameter",
			"Conversation Workflow Output",
		)

		for dt in new_doctypes:
			self.assertTrue(frappe.db.exists("DocType", dt), f"Missing new DocType {dt}")
			meta = frappe.get_meta(dt)
			expected_module = "Communication" if dt == "Process Execution Log" else "Chatbot"
			self.assertEqual(meta.module, expected_module)
			module_path = f"messaging.{expected_module.lower()}.doctype.{frappe.scrub(dt)}.{frappe.scrub(dt)}"
			controller_module = importlib.import_module(module_path)
			self.assertEqual(get_controller(dt).__module__, controller_module.__name__)

		self.assertEqual(frappe.db.get_value("Module Def", "Communication", "app_name"), "messaging")
		self.assertTrue(Path(frappe.get_app_path("messaging", "conversation", "doctype")).exists())

		for old_dt in old_doctypes:
			self.assertFalse(frappe.db.exists("DocType", old_dt), f"Old DocType {old_dt} still exists")
			self.assertFalse(frappe.db.table_exists(old_dt), f"Old table for {old_dt} still exists")

		for dt in (
			"Bot Settings",
			"Bot Parameter",
			"Bot Flow",
			"Process Execution Log",
		):
			getdoctype(dt)
			self.assertTrue(frappe.has_permission(dt, "read", user="Administrator"))

		getdoc("Bot Settings", "Bot Settings")

		self.assertFalse(frappe.db.exists("DocType", "Menu Flow"))
		self.assertFalse(frappe.db.table_exists("Menu Flow"))
		# Verify Link and Table field options integrity
		settings_meta = frappe.get_meta("Bot Settings")
		self.assertIsNone(settings_meta.get_field("greeting_menu_selection_mode"))
		self.assertIsNone(settings_meta.get_field("greeting_menu_workflows"))
		self.assertEqual(settings_meta.get_field("registration_states").options, "Flow Consumer State")
		self.assertIsNone(settings_meta.get_field("authorized_process_methods"))

		flow_meta = frappe.get_meta("Bot Flow")
		self.assertEqual(flow_meta.get_field("parameters").options, "Flow Parameter")
		self.assertEqual(flow_meta.get_field("outputs").options, "Flow Output")

		self.assertEqual(frappe.get_meta("Flow Consumer State").get_field("state").options, "Bot Flow State")
		self.assertEqual(frappe.get_meta("Flow Parameter").get_field("parameter").options, "Bot Parameter")
		self.assertEqual(frappe.get_meta("Process Execution Log").get_field("workflow").fieldtype, "Data")
		self.assertTrue(frappe.get_meta("Process Execution Log").get_field("workflow").read_only)

		# Verify Desk User without System Manager/Workspace Manager cannot write Bot Settings or Bot Flow
		test_email = "test_desk_readonly_m1@example.com"
		if not frappe.db.exists("User", test_email):
			u = frappe.get_doc(
				{
					"doctype": "User",
					"email": test_email,
					"first_name": "Readonly",
					"send_welcome_email": 0,
					"roles": [{"role": "Desk User"}],
				}
			).insert(ignore_permissions=True)
		else:
			u = frappe.get_doc("User", test_email)

		self.assertFalse(frappe.has_permission("Bot Settings", "write", user=u.name))
		self.assertFalse(frappe.has_permission("Bot Flow", "write", user=u.name))


	def test_module_navigation_resolves_without_child_table_entries(self) -> None:
		from frappe.boot import get_app_data
		from frappe.desk.doctype.dock.dock import resolve_app_dock
		from frappe.desk.doctype.sidebar.sidebar import resolve_sidebar

		icons = {
			"Communication": "communication-envelope-duotone",
			"Conversation": "conversation-chat-duotone",
			"Chatbot": "chatbot-bot-duotone",
		}
		main_doctypes = {
			"Communication": {
				"Process Definition",
				"Process Execution Log",
				"Communication Channel",
				"Communication Channel Account",
				"Communication Provider",
				"Communication Template",
				"Notification Rule",
				"Notification Queue",
			},
			"Chatbot": {
				"Bot Settings",
				"Bot Flow",
				"Bot Flow Template",
				"Bot Parameter",
				"Bot Dispatch",
				"AI Request Log",
				"AI Provider",
				"AI Configuration",
				"AI Evaluation Case Template",
				"AI Evaluation Case",
				"AI Evaluation Run",
				"AI Evaluation Result",
			},
			"Conversation": {
				"Chat Contact",
				"Chat Session",
				"Chat Message",
				"Chat Turn",
				"Chat Agent",
				"Emulator Contact",
			},
		}
		dock = resolve_app_dock("messaging")
		self.assertEqual([row["link_to"] for row in dock], ["Conversation", "Chatbot", "Communication"])
		for row in dock:
			self.assertEqual(row["link_type"], "Sidebar")
			self.assertEqual(row["icon"], icons[row["link_to"]])

		app = next(row for row in get_app_data() if row["app_name"] == "messaging")
		self.assertTrue(app["on_apps_screen"])
		self.assertEqual(app["app_title"], "Messaging")
		for module, icon in icons.items():
			sidebar = resolve_sidebar(module, "Administrator")
			self.assertIsNotNone(sidebar)
			self.assertEqual(sidebar.app, "messaging")
			self.assertEqual(sidebar.header_icon, icon)
			if module == "Communication":
				self.assertTrue(sidebar.landing)
			elif module == "Conversation":
				# Native Page routes are resolved by the Desk client.
				self.assertEqual(next(row["link_to"] for row in sidebar.items if row.get("type") == "Link"), "conversation-inbox")
			self.assertFalse(any(row.get("link_type") == "Workspace" for row in sidebar.items))
			expected = main_doctypes[module]
			for doctype in expected:
				owner_module = "Emulator" if doctype == "Emulator Contact" else module
				self.assertEqual(frappe.get_meta(doctype).module, owner_module)
				self.assertEqual(
					get_controller(doctype).__module__,
					f"messaging.{owner_module.lower()}.doctype.{frappe.scrub(doctype)}.{frappe.scrub(doctype)}",
				)
			self.assertEqual({row["link_to"] for row in sidebar.items if row.get("type") == "Link" and row.get("link_type") == "DocType"}, expected)
			if module == "Conversation":
				self.assertIn("conversation-inbox", {row["link_to"] for row in sidebar.items if row.get("link_type") == "Page"})
				self.assertEqual(next(row['label'] for row in sidebar.items if row.get('link_to') == 'emulator-console'), frappe._('Chat Emulator'))
			for row in sidebar.items:
				if row.get("type") == "Link" and row.get("link_type") in ("Workspace", "DocType"):
					self.assertTrue(frappe.db.exists(row["link_type"], row["link_to"]))


	def test_module_icons_are_registered_with_native_desk_loader(self) -> None:
		from frappe.utils.jinja_globals import include_icons

		asset_path = "/assets/messaging/icons/icons.svg"
		self.assertIn(asset_path, frappe.get_hooks("app_include_icons", app_name="messaging"))
		loader = include_icons(asset_path, preload=False)
		self.assertIn(asset_path, loader)
		self.assertIn('getElementById("all-symbols")', loader)
		asset = Path(frappe.get_app_path("messaging", "public", "icons", "icons.svg"))
		svg = ElementTree.fromstring(asset.read_text())
		ns = {"svg": "http://www.w3.org/2000/svg"}
		symbols = {node.attrib["id"]: node for node in svg.findall("svg:symbol", ns)}
		self.assertEqual(set(symbols), {"icon-communication-envelope-duotone", "icon-conversation-chat-duotone", "icon-chatbot-bot-duotone"})
		for node in symbols.values():
			self.assertEqual(node.attrib["viewBox"], "0 0 24 24")
			self.assertEqual(node.attrib["class"], "soft-icon")
			self.assertTrue(node.findall(".//*[@class='soft-body']"))
		style = svg.find("svg:style", ns).text
		self.assertIn("prefers-reduced-motion: no-preference", style)
		self.assertIn("--icon-motion-opacity", style)
		self.assertIn(".messaging-communication-envelope", style)
		self.assertIn(".messaging-chat-front", style)
		self.assertIn(".messaging-chatbot-head", style)
		self.assertNotIn("qota-", asset.read_text())
		self.assertEqual(asset.read_bytes(), Path(frappe.get_site_path("..", "assets", "messaging", "icons", "icons.svg")).read_bytes())


	def test_independence_from_qota_and_whatsapp_and_three_modules_only(self) -> None:
		from messaging import hooks

		self.assertEqual(getattr(hooks, "required_apps", []), [])
		self.assertFalse(getattr(hooks, "before_migrate", None))
		self.assertFalse(Path(frappe.get_app_path("messaging", "communication", "migrate.py")).exists())
		self.assertFalse(Path(frappe.get_app_path("messaging", "channel_emulator")).exists())

		app_root = Path(frappe.get_app_path("messaging"))
		modules = [
			line.strip()
			for line in (app_root / "modules.txt").read_text(encoding="utf-8").splitlines()
			if line.strip()
		]
		self.assertEqual(modules, ["Conversation", "Chatbot", "Communication", "Emulator"])

		db_modules = sorted(
			frappe.get_all("Module Def", filters={"app_name": "messaging"}, pluck="name")
		)
		self.assertEqual(db_modules, ["Chatbot", "Communication", "Conversation", "Emulator"])

		# Ensure no runtime controller file imports qota or whatsapp or hardcodes QOTA workflows
		for py_file in app_root.rglob("*.py"):
			if py_file.name.startswith("test_"):
				continue
			source = py_file.read_text(encoding="utf-8")
			self.assertNotIn("import qota", source, f"Forbidden qota import in {py_file}")
			self.assertNotIn("from qota", source, f"Forbidden qota import in {py_file}")
			self.assertNotIn("consultar_saldo", source, f"Forbidden hardcoded workflow in {py_file}")



class TestLocalLanguage(FrappeTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user('Administrator')

    def tearDown(self):
        frappe.db.rollback()
        super().tearDown()

    def settings(self):
        settings=configure_test_settings()
        settings.set('chatbot_languages',[dict(language='es',enabled=1,is_default=1),dict(language='en',enabled=1,is_default=0)])
        settings.save()
        return settings

    def test_local_detection_without_ai_and_ambiguous_numeric_text(self):
        from messaging.chatbot.doctype.bot_settings.language import detect_language
        for text in ('0', '12345', 'hola', 'hello', 'ok', 'SC-00125', '😀 😃',
                     'abcde fghij klmno pqrst', 'hola hello saldo balance please gracias'):
            self.assertFalse(detect_language(text).reliable, text)
        for text, code in (
            ('Hello, I want to check my balance.', 'en'),
            ('I would like to know my current account balance please', 'en'),
            ('Quiero consultar mi saldo pendiente.', 'es'),
            ('Quisiera consultar el saldo pendiente de mi contrato por favor', 'es'),
            ('Je voudrais consulter le solde de mon compte.', 'fr'),
            ('Eu gostaria de consultar o saldo da minha conta', 'pt'),
            ('你好，我想查询我的账户余额', 'zh')):
            evidence = detect_language(text)
            self.assertTrue(evidence.reliable, (text, evidence))
            self.assertEqual(evidence.language, code)
            self.assertGreater(evidence.confidence, evidence.runner_up)

    def test_language_resolution_preserves_context_and_contact_preference(self):
        from messaging.chatbot.doctype.bot_settings.language import contact_language, presentation_language
        settings = self.settings()
        contact = frappe._dict(preferred_language=None, language_confidence=0)
        self.assertEqual(contact_language(contact, '12345', settings), 'es')
        self.assertEqual(contact_language(contact, 'Hello, I want to check my balance.', settings), 'en')
        self.assertEqual(contact.preferred_language, 'en')
        for ambiguous in ('0', 'hola', 'SC-00125'):
            self.assertEqual(contact_language(contact, ambiguous, settings), 'en')
            self.assertEqual(contact_language(contact, ambiguous, settings, context_language='es'), 'es')
        self.assertEqual(
            contact_language(contact, 'Je voudrais consulter le solde de mon compte.', settings, context_language='en'),
            'es',
        )
        with presentation_language('en'):
            self.assertEqual(resolve_process_language(), 'en')
        self.assertEqual(resolve_process_language(), 'es')
        settings.chatbot_languages[1].enabled = 0
        with patch('messaging.chatbot.doctype.bot_settings.language._detector', side_effect=AssertionError('One language needs no detector')):
            self.assertEqual(contact_language(contact, 'Hello, I want to check my balance.', settings), 'es')

    def test_concurrent_detector_and_presentation_contexts_are_independent(self):
        from concurrent.futures import ThreadPoolExecutor
        from messaging.chatbot.doctype.bot_settings.language import detect_language, presentation_language, current_language
        texts = [('Hello, I want to check my balance.', 'en'), ('Quiero consultar mi saldo pendiente.', 'es')]
        def identify(item):
            message, code = item
            with presentation_language(code):
                evidence = detect_language(message)
                return evidence.language, evidence.reliable, current_language()
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(identify, texts * 4))
        self.assertEqual(results, [('en', True, 'en'), ('es', True, 'es')] * 4)
        self.assertIsNone(current_language())

    def test_contact_language_has_native_persistence_fields(self):
        meta=frappe.get_meta('Chat Contact')
        self.assertEqual(meta.get_field('preferred_language').options,'Language')
        contact=frappe.get_doc(dict(doctype='Chat Contact',name='language_'+frappe.generate_hash(length=10),
            preferred_language='en',language_confidence=.99))
        contact.db_insert()
        self.assertEqual(frappe.get_doc('Chat Contact',contact.name).preferred_language,'en')

    def test_language_scope_restores_after_failure_and_limits_to_enabled_languages(self):
        from messaging.chatbot.doctype.bot_settings.language import presentation_language
        self.settings()
        with self.assertRaises(RuntimeError),presentation_language('en'):
            self.assertEqual(resolve_process_language(),'en')
            raise RuntimeError('Controlled scoped failure')
        self.assertEqual(resolve_process_language(),'es')
        with presentation_language('fr'):
            self.assertEqual(resolve_process_language(),'es')


class TestEmptySettingsAndMatrices(FrappeTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user('Administrator')
        frappe.db.set_value('Bot Flow', {'status': 'Enabled'}, 'status', 'Disabled', update_modified=False)

    def tearDown(self):
        frappe.db.rollback()
        frappe.clear_document_cache('Bot Settings', 'Bot Settings')
        super().tearDown()

    def empty(self):
        return empty_settings()

    def test_empty_single_opens_saves_and_reloads_without_defaults(self):
        settings = self.empty()
        with patch.object(settings, 'get_doc_before_save', return_value=None):
            settings.save()
        getdoc('Bot Settings', 'Bot Settings')
        settings.reload()
        self.assertFalse(settings.chatbot_languages)
        self.assertFalse(settings.message_translations)
        self.assertFalse(settings.registration_states)
        self.assertFalse(settings.active_ai_configuration)
        self.assertIsNone(validate_chatbot_languages(settings))
        for field in settings.meta.fields:
            if field.fieldtype == 'Check':
                self.assertFalse(settings.get(field.fieldname), field.fieldname)
            expected_defaults = (None, '', '0', '1') if field.fieldname == 'human_requests_required' else (None, '', '0')
            self.assertIn(field.default, expected_defaults, field.fieldname)
        self.assertFalse(settings_text(settings, 'informative_message'))
        with self.assertRaises(frappe.ValidationError):
            resolve_process_language()
        with self.assertRaises(frappe.ValidationError):
            settings.require_operational_configuration()

    def test_first_language_assigns_default_and_requires_messages_immediately(self):
        from messaging.chatbot.doctype.bot_settings.localization import required_message_keys
        settings = self.empty()
        settings.append('chatbot_languages', dict(language='es', enabled=1, is_default=0))
        with patch.object(settings, 'get_doc_before_save', return_value=None), \
             self.assertRaises(frappe.ValidationError) as error:
            settings.save()
        self.assertTrue(settings.chatbot_languages[0].is_default)
        self.assertIn('es', str(error.exception))
        self.assertIn('Default Safe Internal Error Message', str(error.exception))
        settings = self.empty()
        settings.set('chatbot_languages', [dict(language='es', enabled=1)])
        for key in required_message_keys(settings):
            set_localized_text(settings, key, 'Required ' + key.replace('_', ' ') + '.')
        with patch.object(settings, 'get_doc_before_save', return_value=None):
            settings.save()
        settings.append('chatbot_languages', dict(language='en', enabled=1, is_default=0))
        settings.save(); settings.reload()
        self.assertEqual(resolve_process_language(), 'es')
        self.assertFalse(any(row.language == 'en' for row in settings.message_translations))
        settings.chatbot_languages[0].is_default = 0
        with self.assertRaises(frappe.ValidationError): settings.save()
        settings.reload()
        settings.set('chatbot_languages', [settings.chatbot_languages[0], settings.chatbot_languages[0].as_dict()])
        with self.assertRaises(frappe.ValidationError): settings.save()

    def test_default_cannot_be_removed_disabled_or_switched_without_its_messages(self):
        settings = configure_test_settings()
        settings.append('chatbot_languages', dict(language='en', enabled=1))
        settings.save()
        original = settings.as_dict()
        for rows in ([], [dict(language='en', enabled=1)],
                     [dict(language='es', enabled=0, is_default=1), dict(language='en', enabled=1)],
                     [dict(language='es', enabled=1), dict(language='en', enabled=1, is_default=1)],
                     [dict(language='es', enabled=1, is_default=1), dict(language='en', enabled=1, is_default=1)]):
            settings.set('chatbot_languages', rows)
            with self.subTest(rows=rows), self.assertRaises(frappe.ValidationError): settings.save()
            persisted = frappe.get_single('Bot Settings')
            self.assertEqual([(r.language, r.enabled, r.is_default) for r in persisted.chatbot_languages],
                [(r['language'], r['enabled'], r['is_default']) for r in original['chatbot_languages']])
            settings.reload()
        settings.set('chatbot_languages', [dict(language='es', enabled=1)])
        settings.save()
        self.assertTrue(settings.chatbot_languages[0].is_default)

    def test_all_message_cells_roundtrip_and_effective_default_fallback(self):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        settings = self.empty()
        settings.set('chatbot_languages', [dict(language='es', enabled=1, is_default=1), dict(language='en', enabled=1)])
        for language in ('es', 'en'):
            for key in MESSAGE_KEYS:
                set_localized_text(settings, key, language + ' ' + key.replace('_', ' ') + '.\nSecond line.', language)
        settings.save(); settings.reload()
        self.assertEqual(len(settings.message_translations), 16)
        for language in ('es', 'en'):
            for key in MESSAGE_KEYS:
                self.assertEqual(settings_text(settings, key, language), language + ' ' + key.replace('_', ' ') + '.\nSecond line.')
        set_localized_text(settings, 'informative_message', '', 'en')
        settings.save()
        self.assertEqual(settings_text(settings, 'informative_message', 'en'), settings_text(settings, 'informative_message', 'es'))

    def test_quota_timezone_is_required_and_validated_only_when_quotas_are_enabled(self):
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        from messaging.registry import _installed_declarations, register_communication_providers
        register_communication_providers(app='messaging', providers=_installed_declarations('messaging', 'communication_providers'))
        set_provider_enabled('EMULR', True)
        configure_test_settings()
        if frappe.db.exists('Communication Channel', 'emulator'):
            channel = frappe.get_doc('Communication Channel', 'emulator')
        else:
            channel = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR', channel_name='Emulator')).insert()
            set_channel_status(channel.name, 'Enabled')
            channel.reload()
        channel.update(dict(quotas_enabled=1, daily_quota_limit=20, monthly_quota_limit=60,
            quota_reservation_ttl_seconds=120, quota_timezone='America/Tegucigalpa', quota_courtesy_enabled=0))
        channel.save(); channel.reload()
        self.assertEqual(channel.quota_timezone, 'America/Tegucigalpa')
        for timezone in ('', 'Invalid/Timezone'):
            channel.reload()
            channel.quota_timezone = timezone
            with self.subTest(timezone=timezone), self.assertRaises(frappe.ValidationError) as error:
                channel.save()
            self.assertNotIsInstance(error.exception, frappe.TimestampMismatchError)
        channel.reload()
        channel.quotas_enabled = 0
        for timezone in ('Invalid/Timezone', ''):
            channel.quota_timezone = timezone
            channel.save(); channel.reload()
            self.assertEqual(channel.quota_timezone, timezone)

    def test_quotas_use_native_timezone_autocomplete_and_have_no_window_control(self):
        from frappe.core.doctype.user.user import get_timezones
        meta = frappe.get_meta('Communication Channel')
        timezone = meta.get_field('quota_timezone')
        self.assertEqual(timezone.fieldtype, 'Autocomplete')
        self.assertEqual(timezone.mandatory_depends_on, 'eval:doc.quotas_enabled')
        self.assertIn('America/Tegucigalpa', get_timezones()['timezones'])
        names = [field.fieldname for field in meta.fields]
        self.assertEqual(names[names.index('daily_quota_limit') + 1], 'monthly_quota_limit')
        settings_meta = frappe.get_meta('Bot Settings')
        for retired in ('quotas_tab', 'quotas_enabled', 'daily_quota_limit', 'monthly_quota_limit', 'quota_timezone',
                        'quota_consumption_basis', 'composite_response_single_unit', 'quota_reservation_ttl_seconds',
                        'reconcile_quota_on_acceptance', 'quota_courtesy_enabled', 'quota_courtesy_max_per_cycle'):
            self.assertIsNone(settings_meta.get_field(retired), retired)

    def test_global_message_catalog_matches_the_remaining_cards(self):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        self.assertEqual(set(MESSAGE_KEYS), {'default_internal_error_message', 'quota_courtesy_message',
            'informative_message', 'informative_categories_header', 'attempts_exhausted_message',
            'human_disabled_message', 'human_active_message', 'human_completed_message'})

    def test_message_integrity_uniqueness_limits_and_safe_errors(self):
        settings = self.empty()
        settings.append('chatbot_languages', dict(language='es', enabled=1, is_default=1))
        for key, language, value in [('unknown', 'es', 'Text'), ('informative_message', 'en', 'Text'),
            ('attempts_exhausted_message', 'es', 'x' * 4001), ('default_internal_error_message', 'es', 'traceback (private)'),
            ('informative_message', 'es', '{{unknown}}')]:
            settings.set('message_translations', [dict(message_key=key, language=language, message=value)])
            with self.subTest(key=key, language=language), self.assertRaises(frappe.ValidationError):
                settings.save()
        settings.set('message_translations', [dict(message_key='informative_message', language='es', message='Text')] * 2)
        with self.assertRaises(frappe.ValidationError):
            settings.save()

    def test_enabling_features_requires_complete_operational_dependencies(self):
        for field in ('interpretation_enabled', 'registration_enabled', 'greeting_enabled', 'human_attention_enabled'):
            settings = self.empty(); settings.set(field, 1)
            with self.subTest(field=field), self.assertRaises(frappe.ValidationError):
                settings.save()
        settings = configure_test_settings()
        settings.greeting_enabled = 1
        with self.assertRaises(frappe.ValidationError):
            settings.save()
        settings.greeting_enabled = 0
        set_localized_text(settings, 'default_internal_error_message', '')
        settings.informative_show_available_categories = 1
        with self.assertRaises(frappe.ValidationError): settings.save()
        settings = configure_test_settings()
        settings.registration_enabled = 1
        with self.assertRaisesRegex(frappe.ValidationError, 'Select a Bot Flow'): settings.save()
        settings = configure_test_settings()
        settings.interpretation_enabled = 1
        with patch('messaging.chatbot.doctype.ai_configuration.execution.run', side_effect=AssertionError('AI must not run')):
            with self.assertRaises(frappe.ValidationError): settings.save()

    def test_message_editors_use_independent_native_html_fields(self):
        meta = frappe.get_meta('Bot Settings')
        self.assertEqual([field.label for field in meta.fields if field.fieldtype == 'Tab Break'],
            ['General', 'Registration', 'Greeting', 'Human Support'])
        editors = {field.fieldname for field in meta.fields if field.fieldtype == 'HTML'}
        self.assertEqual(editors, {'informative_message_cards', 'informative_categories_header_card',
            'attempts_exhausted_message_card', 'general_message_cards', 'quota_courtesy_message_card',
            'human_message_cards', 'human_active_message_card', 'human_completed_message_card'})
        names = [field.fieldname for field in meta.fields]
        for first, column, second in [
            ('informative_message_cards', 'informative_messages_column', 'informative_categories_header_card'),
            ('human_message_cards', 'human_messages_column', 'human_active_message_card'),
        ]:
            index = names.index(first)
            self.assertEqual(names[index:index + 3], [first, column, second])
            self.assertEqual(meta.get_field(column).fieldtype, 'Column Break')
        self.assertEqual(meta.get_field('human_active_message_card').depends_on, 'eval:doc.human_attention_enabled')
        self.assertEqual(meta.get_field('message_translations').options, 'Bot Settings Translation')
        self.assertTrue(meta.get_field('message_translations').hidden)
        self.assertEqual(meta.get_field('informative_message_cards').label, 'Informative Message')
        self.assertEqual(meta.get_field('human_message_cards').label, 'Message When Human Support is Disabled')

    def test_greeting_cards_follow_controls(self):
        meta = frappe.get_meta('Bot Settings')
        names = [field.fieldname for field in meta.fields]
        order = ['section_courtesy', 'quota_courtesy_message_card', 'greeting_enabled', 'column_break_greeting',
            'show_menu_on_greeting_only', 'greeting_flow_section', 'greeting_flow', 'greeting_states']
        self.assertEqual(sorted(order, key=names.index), order)
        self.assertEqual(meta.get_field('greeting_flow').fieldtype, 'Link')
        self.assertEqual(meta.get_field('greeting_flow').options, 'Bot Flow')
        self.assertEqual(meta.get_field('greeting_flow').depends_on, 'eval:doc.greeting_enabled')
        self.assertEqual(meta.get_field('greeting_flow').mandatory_depends_on, 'eval:doc.greeting_enabled')
        self.assertEqual(meta.get_field('show_menu_on_greeting_only').depends_on, 'eval:doc.greeting_enabled')
        self.assertIsNone(meta.get_field('greeting_message_card'))
        self.assertIsNone(meta.get_field('greeting_menu_message_card'))
        self.assertFalse(meta.get_field('quota_courtesy_message_card').depends_on)
        self.assertIsNone(meta.get_field('quotas_tab'))
        self.assertIsNone(meta.get_field('greeting_messages_section'))
        self.assertIsNone(meta.get_field('greeting_message_cards'))

    def test_disabled_greeting_preserves_menu_preference_without_automation(self):
        settings = self.empty()
        settings.show_menu_on_greeting_only = 1
        settings.exempt_registration_from_quotas = 1
        with patch.object(settings, 'get_doc_before_save', return_value=None):
            settings.save()
        settings.reload()
        self.assertFalse(settings.greeting_enabled)
        self.assertTrue(settings.show_menu_on_greeting_only)
        self.assertTrue(settings.exempt_registration_from_quotas)
        self.assertFalse(settings.has_automation())
        self.assertFalse(settings.message_translations)
        self.assertFalse(settings.greeting_flow)
        self.assertEqual(compose_informative_response(settings), '')

    def test_greeting_and_courtesy_require_text_only_when_enabled(self):
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        from messaging.registry import _installed_declarations, register_communication_providers
        register_communication_providers(app='messaging', providers=_installed_declarations('messaging', 'communication_providers'))
        set_provider_enabled('EMULR', True)
        if frappe.db.exists('Communication Channel', 'emulator'):
            channel = frappe.get_doc('Communication Channel', 'emulator')
        else:
            channel = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR', channel_name='Emulator')).insert()
            set_channel_status(channel.name, 'Enabled')
            channel.reload()
        channel.update(dict(quotas_enabled=1, daily_quota_limit=20, monthly_quota_limit=60,
            quota_reservation_ttl_seconds=120, quota_timezone='UTC', quota_courtesy_enabled=0, quota_courtesy_max_per_cycle=0))
        channel.save()
        settings = configure_test_settings()
        settings.greeting_enabled = 0
        settings.show_menu_on_greeting_only = 1
        set_localized_text(settings, 'quota_courtesy_message', '')
        settings.save()
        settings.greeting_enabled = 1
        with self.assertRaises(frappe.ValidationError): settings.save()
        settings.reload()
        channel.reload()
        channel.quota_courtesy_enabled = 1
        channel.quota_courtesy_max_per_cycle = 1
        with self.assertRaises(frappe.ValidationError): channel.save()
        set_localized_text(settings, 'quota_courtesy_message', 'Independent courtesy.')
        settings.save(); settings.reload()
        channel.reload()
        channel.quota_courtesy_enabled = 1
        channel.quota_courtesy_max_per_cycle = 1
        channel.save(); channel.reload()
        self.assertTrue(channel.quota_courtesy_enabled)
        self.assertFalse(settings.greeting_enabled)
        self.assertTrue(settings.show_menu_on_greeting_only)
        self.assertEqual(settings_text(settings, 'quota_courtesy_message'), 'Independent courtesy.')

    def test_required_message_hints_match_validation_without_writes(self):
        from messaging.chatbot.doctype.bot_settings.bot_settings import message_requirements
        from messaging.chatbot.doctype.bot_settings.localization import required_message_keys
        from messaging.communication.doctype.communication_provider.communication_provider import set_provider_enabled
        from messaging.registry import _installed_declarations, register_communication_providers
        register_communication_providers(app='messaging', providers=_installed_declarations('messaging', 'communication_providers'))
        set_provider_enabled('EMULR', True)
        if frappe.db.exists('Communication Channel', 'emulator'):
            channel = frappe.get_doc('Communication Channel', 'emulator')
        else:
            channel = frappe.get_doc(dict(doctype='Communication Channel', provider='EMULR', channel_name='Emulator')).insert()
            set_channel_status(channel.name, 'Enabled')
            channel.reload()
        channel.update(dict(quotas_enabled=1, daily_quota_limit=20, monthly_quota_limit=60,
            quota_reservation_ttl_seconds=120, quota_timezone='UTC', quota_courtesy_enabled=0, quota_courtesy_max_per_cycle=0))
        channel.save()
        settings = self.empty()
        with patch('frappe.get_doc', side_effect=AssertionError('Foreign controllers must not be instantiated')):
            with self.assertRaises(frappe.PermissionError): message_requirements({'doctype': 'User'})
        self.assertEqual(message_requirements(settings.as_dict()), [])
        channel.quota_courtesy_enabled = 1
        channel.quota_courtesy_max_per_cycle = 1
        channel.db_set({'quota_courtesy_enabled': 1, 'quota_courtesy_max_per_cycle': 1}, update_modified=False)
        settings.greeting_enabled = 1
        expected = {'default_internal_error_message', 'informative_message', 'attempts_exhausted_message',
            'human_disabled_message', 'quota_courtesy_message'}
        before = frappe.db.count('Process Execution Log')
        with patch('messaging.registry.processes.discovery.discover_contract', side_effect=AssertionError('Hints cannot discover a Process')):
            self.assertEqual(set(message_requirements(settings.as_dict())), expected)
        self.assertEqual(required_message_keys(settings, operational=True), expected)
        self.assertEqual(frappe.db.count('Process Execution Log'), before)
        self.assertNotIn('greeting_menu_header', expected)
        settings.human_attention_enabled = 1
        self.assertIn('human_active_message', message_requirements(settings.as_dict()))
        frappe.set_user('Guest')
        with self.assertRaises(frappe.PermissionError): message_requirements(settings.as_dict())

    def test_lifecycle_has_no_configuration_initializers(self):
        from messaging import setup
        settings = self.empty()
        with patch('messaging.conversation.doctype.chat_agent.chat_agent.setup_roles'), \
             patch('messaging.security.service_identity.ensure_messaging_service_user'), \
             patch('messaging.registry.synchronize_registry'), \
             patch('messaging.presets.evaluations.register'), \
             patch('messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets.provision_presets'), \
             patch('messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed'), \
             patch.object(settings, 'get_doc_before_save', return_value=None):
            settings.save()
            setup.after_install(); setup.after_migrate()
        self.assertFalse(get_bot_settings().chatbot_languages)
        self.assertFalse(get_bot_settings().message_translations)

    def test_disabled_message_translation_is_retained_and_not_resolved_or_editable(self):
        settings = configure_test_settings()
        settings.append('chatbot_languages', dict(language='en', enabled=1))
        set_localized_text(settings, 'informative_message', 'Default text.')
        set_localized_text(settings, 'informative_message', 'English text.', 'en')
        settings.save()
        settings.chatbot_languages[1].enabled = 0
        settings.save(); settings.reload()
        self.assertEqual(settings_text(settings, 'informative_message', 'en'), 'Default text.')
        set_localized_text(settings, 'informative_message', 'Changed.', 'en')
        with self.assertRaises(frappe.ValidationError): settings.save()


from messaging.communication.doctype.process_definition.test_process_definition import ProcessFixtures
import unittest
from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name


class TestHumanFlowConfiguration(ProcessFixtures, unittest.TestCase):
    def configured_flow(self):
        from messaging.chatbot.doctype.bot_flow.test_bot_flow import TestBotFlow, consumer_contract_flow
        flow, _parameter = TestBotFlow.configured(self, declaration=consumer_contract_flow)
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.activate(flow)
        return flow

    def test_partial_configuration_and_complete_state_allowlist(self):
        flow = self.configured_flow()
        settings = get_bot_settings()
        settings.human_flow = flow.name
        settings.save();settings.reload()
        self.assertFalse(settings.human_attention_enabled)
        self.assertFalse(settings.human_transfer_states)
        settings.set('human_transfer_states', [dict(state=state_name(flow.name, 'success')), dict(state=state_name(flow.name, 'not_found'))])
        settings.save();settings.reload()
        self.assertEqual([row.state for row in settings.human_transfer_states], [state_name(flow.name, status) for status in ('success','not_found')])
        child = frappe.get_meta('Flow Consumer State')
        self.assertEqual([field.fieldname for field in child.fields], ['state'])
        self.assertEqual(child.get_field('state').fieldtype, 'Link')

    def test_duplicate_unknown_cancel_and_retry_states_are_rejected_on_server(self):
        flow = self.configured_flow()
        for states in (['success','success'], ['unknown']):
            settings = get_bot_settings();settings.human_flow=flow.name
            settings.set('human_transfer_states',[dict(state=state_name(flow.name, status)) for status in states])
            with self.subTest(states=states), self.assertRaises(frappe.ValidationError):
                from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
                configuration(settings, 'human')
        for action in ('Cancel Flow','Retry Parameter'):
            flow.reload();flow.outputs[1].flow_action=action
            flow.outputs[1].retry_parameter=flow.parameters[0].parameter if action=='Retry Parameter' else None
            with self.assertRaisesRegex(frappe.ValidationError, 'Flow actions'):
                flow.save()
            settings=get_bot_settings();settings.human_flow=flow.name
            settings.set('human_transfer_states',[dict(state=state_name(flow.name, 'not_found'))])
            with self.subTest(action=action), self.assertRaises(frappe.ValidationError):
                from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
                with patch('messaging.chatbot.doctype.bot_settings.flow_consumers.output_catalog',
                    return_value={'not_found': dict(state=state_name(flow.name, 'not_found'), action=action, fields={})}):
                    configuration(settings, 'human')

    def test_activation_requires_active_approved_flow_and_states(self):
        flow=self.configured_flow()
        self.activate(flow, False)
        settings=get_bot_settings();settings.human_flow=flow.name;settings.human_attention_enabled=1
        settings.set('human_transfer_states',[dict(state=state_name(flow.name, 'success'))])
        settings.set('human_fields',[dict(field_key='contract', label='Contract', states='success', keep=1)])
        with self.assertRaisesRegex(frappe.ValidationError,'The selected Bot Flow is disabled'):settings.save()
        self.activate(flow)
        settings=get_bot_settings();settings.human_flow=flow.name;settings.human_attention_enabled=1
        settings.set('human_transfer_states', []); settings.set('human_fields', [])
        with self.assertRaisesRegex(frappe.ValidationError,'at least one'):settings.save()
        settings=get_bot_settings();settings.human_flow=flow.name;settings.human_attention_enabled=1
        settings.set('human_transfer_states',[dict(state=state_name(flow.name, 'success'))])
        settings.set('human_fields',[dict(field_key='contract', label='Contract', states='success', keep=1)])
        settings.save()
        self.activate(flow, False)
        with self.assertRaises(frappe.ValidationError): get_bot_settings().human_flow_configuration(operational=True)

    def test_changing_flow_does_not_replace_incompatible_states(self):
        flow=self.configured_flow()
        settings=get_bot_settings();settings.human_flow=flow.name
        settings.set('human_transfer_states',[dict(state=state_name(flow.name, 'success'))]);settings.save()
        other=self.flow('negative_flow');other.save()
        settings.human_flow=other.name
        with self.assertRaises(frappe.ValidationError):
            from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
            configuration(settings, 'human')
        self.assertEqual(settings.human_transfer_states[0].state,state_name(flow.name, 'success'))

    def test_settings_is_no_longer_a_process_approval_source(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process as validate_flow_process
        from messaging.communication.doctype.notification_rule.notification_rule import validate_process as validate_rule_process
        from messaging.communication.doctype.process_definition import approval, validation
        settings=get_bot_settings()
        self.assertNotIn('Bot Settings', approval.CONSUMER_TYPES)
        with self.assertRaises(frappe.PermissionError):
            validation._configuration(settings.as_dict())
        for endpoint in (validate_flow_process, validate_rule_process):
            with self.subTest(endpoint=endpoint.__module__), self.assertRaises(frappe.ValidationError):
                endpoint(settings.as_dict())
        for field in ('human_availability_process_method','process_test_status','process_test_fingerprint',
                      'process_test_checked_at','process_test_checked_by','process_test_log',
                      'approval_hash','approved_process_revision','approved_contract_hash'):
            self.assertIsNone(settings.meta.get_field(field))

    def test_human_flow_can_be_reactivated_without_disabling_settings(self):
        flow=self.configured_flow();self.activate(flow)
        settings=get_bot_settings();settings.human_flow=flow.name;settings.human_attention_enabled=1
        settings.set('human_transfer_states',[dict(state=state_name(flow.name, 'success'))])
        settings.set('human_fields',[dict(field_key='contract', label='Contract', states='success', keep=1)])
        settings.save()
        from messaging.chatbot.doctype.bot_flow.bot_flow import set_active
        set_active(flow.as_dict(),0);flow.reload()
        with self.assertRaises(frappe.ValidationError):get_bot_settings().human_flow_configuration(operational=True)
        set_active(flow.as_dict(),1)
        self.assertEqual(get_bot_settings().human_flow_configuration(operational=True).status, 'Enabled')

    def test_human_message_catalog_has_exactly_three_native_messages(self):
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        human_keys={key for key in MESSAGE_KEYS if key.startswith('human_')}
        self.assertEqual(human_keys, {'human_disabled_message','human_active_message','human_completed_message'})
        self.assertEqual(len(MESSAGE_KEYS), 8)


class TestFlowConsumerLayout(FrappeTestCase):
    def setUp(self):
        super().setUp()
        frappe.set_user('Administrator')

    def tearDown(self):
        frappe.db.rollback()
        super().tearDown()

    def test_human_section_order_columns_and_three_messages(self):
        meta = frappe.get_meta('Bot Settings')
        fields = [f.fieldname for f in meta.fields]
        human = fields[fields.index('human_support_tab') + 1:]
        self.assertEqual([f for f in human if meta.get_field(f).fieldtype == 'Section Break'],
            ['section_human_attention', 'human_flow_section', 'human_fields_section', 'human_schedule_section', 'human_messages_section'])
        self.assertLess(human.index('human_attention_enabled'), human.index('human_operations_column'))
        self.assertLess(human.index('human_operations_column'), human.index('human_inactivity_minutes'))
        self.assertLess(human.index('human_inactivity_minutes'), human.index('human_messages_section'))
        self.assertEqual(meta.get_field('human_operations_column').depends_on, 'eval:doc.human_attention_enabled')
        self.assertEqual(meta.get_field('human_transfer_states').fieldtype, 'Table MultiSelect')
        self.assertEqual(meta.get_field('registration_states').fieldtype, 'Table MultiSelect')
        self.assertEqual(meta.get_field('registration_fields').options, 'Registration Field')
        self.assertEqual([f.fieldname for f in frappe.get_meta('Registration Field').fields], ['field_key', 'label', 'keep'])
        self.assertEqual([f.fieldname for f in frappe.get_meta('Human Request Field').fields], ['field_key', 'label', 'states', 'keep'])
        for dt in ('Registration Parameter', 'Human Transfer State'):
            self.assertFalse(frappe.db.exists('DocType', dt))
            self.assertFalse(frappe.db.table_exists(dt))
        self.assertNotIn('filter_questions', frappe.db.get_table_columns('Chat Session'))
        self.assertIsNone(meta.get_field('registration_messages_matrix'))
        from messaging.chatbot.doctype.bot_settings.localization import MESSAGE_KEYS
        self.assertEqual({key for key in MESSAGE_KEYS if key.startswith('human_')},
            {'human_disabled_message', 'human_active_message', 'human_completed_message'})
        self.assertFalse(any(key.startswith('registration_') for key in MESSAGE_KEYS))

    def test_human_timeout_rejects_raw_invalid_values_before_int_storage(self):
        from messaging.chatbot.doctype.bot_settings.bot_settings import validate_human_inactivity
        for value in (None, '', 0, '0', -1, '-1', 1.5, '1.5', True, 1.0):
            with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
                validate_human_inactivity(value)
            settings = get_bot_settings()
            settings.human_attention_enabled = 1
            settings.human_inactivity_minutes = value
            with self.subTest(save_value=value), self.assertRaisesRegex(frappe.ValidationError, 'integer of at least one minute'):
                settings.save()
        self.assertEqual(validate_human_inactivity('1'), 1)
        settings = get_bot_settings()
        settings.human_attention_enabled = 0
        settings.human_inactivity_minutes = 8
        settings.save(); settings.reload()
        self.assertEqual(settings.human_inactivity_minutes, 8)

    def test_bot_flow_general_has_independent_cards_in_one_grid(self):
        meta = frappe.get_meta('Bot Flow')
        fields = [f.fieldname for f in meta.fields]
        self.assertEqual(meta.get_field('translation_cards').fieldtype, 'HTML')
        self.assertLess(fields.index('translation_cards'), fields.index('process_flow_tab'))
        self.assertEqual(meta.get_field('menu_order').depends_on, 'eval:doc.show_in_menu')
        self.assertFalse(meta.get_field('translation_cards').depends_on)
        for name in ('menu_messages_matrix', 'semantic_messages_matrix', 'input_messages_matrix',
                'semantic_messages_section', 'section_break_input'):
            self.assertIsNone(meta.get_field(name))
        for name, child in [('translations', 'Flow Translation'), ('input_translations', 'Flow Input Translation')]:
            self.assertTrue(meta.get_field(name).hidden)
            self.assertEqual(meta.get_field(name).options, child)


class TestFeatureActivation(ProcessFixtures, unittest.TestCase):
    def enabled_registration_flow(self):
        from messaging.chatbot.doctype.bot_flow.test_bot_flow import TestBotFlow, contract_business, contract_flow
        from messaging.registry.processes import BotProcess
        from messaging.registry.processes import register_processes
        flow, _ = TestBotFlow.configured(self)
        self.definitions.append(BotProcess(id=flow.process_definition, label='Technical inputs', definition=contract_flow, handler=contract_business))
        register_processes(app='messaging', processes=self.definitions)
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.assertEqual(flow.status, 'Enabled')
        return flow

    def enabled_flow(self, function='technical_flow'):
        flow = self.flow(function)
        self.assertEqual(self.approve(flow)['status'], 'Passed')
        self.assertEqual(flow.status, 'Enabled')
        return flow

    def test_each_feature_requires_flow_and_complete_states_only_while_enabled(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import CONSUMERS
        ordinary = self.enabled_flow()
        for consumer, (flow_field, states_field, fields_field) in CONSUMERS.items():
            flow = self.enabled_registration_flow() if consumer == 'registration' else ordinary
            enabled = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
            with self.subTest(consumer=consumer):
                if flow.status != 'Enabled':
                    self.activate(flow)
                settings = get_bot_settings()
                settings.set(enabled, 1)
                with self.assertRaises(frappe.ValidationError): settings.save()
                settings.reload(); settings.set(enabled, 1)
                settings.set(flow_field, flow.name)
                with self.assertRaisesRegex(frappe.ValidationError, 'at least one Complete Flow state'): settings.save()
                settings.reload(); settings.set(enabled, 1); settings.set(flow_field, flow.name)
                settings.set(states_field, [dict(state=state_name(flow.name, 'success'))])
                if consumer == 'registration':
                    settings.set(fields_field, [dict(field_key='contract', label='Contract', keep=1)])
                elif consumer == 'human':
                    settings.set(fields_field, [dict(field_key='language', label='Language', states='success', keep=1)])
                settings.save(); settings.reload()
                self.assertEqual(settings.get(states_field)[0].state, state_name(flow.name, 'success'))
                settings.set(enabled, 0)
                settings.save(); settings.reload()
                retained = [(row.name, row.state) for row in settings.get(states_field)]
                retained_fields = [(r.field_key, r.label, r.keep, r.get('states')) for r in settings.get(fields_field)] if fields_field else []
                self.activate(flow, False)
                settings.save(); settings.reload()
                self.assertEqual(settings.get(flow_field), flow.name)
                self.assertEqual([(r.name, r.state) for r in settings.get(states_field)], retained)
                if fields_field:
                    self.assertEqual([(r.field_key, r.label, r.keep, r.get('states')) for r in settings.get(fields_field)], retained_fields)
                settings.set(enabled, 1)
                with self.assertRaisesRegex(frappe.ValidationError, 'The selected Bot Flow is disabled'): settings.save()
                settings.reload(); settings.set(enabled, 0)
                self.activate(flow)
                settings.set(enabled, 1); settings.save()
                settings.set(enabled, 0)
                settings.set(flow_field, None); settings.set(states_field, [])
                if fields_field: settings.set(fields_field, [])
                settings.save()

    def test_greeting_reuses_complete_states_without_output_storage(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration, options
        flow = self.enabled_flow()
        other = self.flow('negative_flow'); other.save()
        def candidate(states):
            settings = get_bot_settings()
            settings.greeting_enabled = 1; settings.greeting_flow = flow.name
            settings.set('greeting_states', [dict(state=state) for state in states])
            return settings
        settings = candidate([state_name(flow.name, 'success')] * 2)
        with self.assertRaisesRegex(frappe.ValidationError, 'unique'): settings.save()
        settings = candidate([state_name(other.name, 'not_found')])
        with self.assertRaisesRegex(frappe.ValidationError, 'Complete Flow'): settings.save()
        settings = candidate([state_name(flow.name, 'success')])
        settings.save()
        config = configuration(settings, 'greeting', operational=True)
        self.assertEqual(config['states'], ['success'])
        self.assertEqual(config['fields'], {})
        self.assertEqual(config['selected'], {})
        self.assertEqual(options(flow.name, frappe.as_json([state_name(flow.name, 'success')]), consumer='greeting')['fields'], [])
        self.assertIsNone(settings.meta.get_field('greeting_fields'))
        self.activate(flow, False)
        with self.assertRaises(frappe.ValidationError):
            get_bot_settings().save()
        with self.assertRaises(frappe.ValidationError): configuration(get_bot_settings(), 'greeting', operational=True)

    def test_multiple_consumer_flows_revalidate_and_reactivate_independently(self):
        from messaging.chatbot.doctype.bot_flow.bot_flow import validate_process
        from messaging.communication.doctype.process_definition import approval
        registration = self.enabled_registration_flow()
        greeting = self.enabled_flow('alternate_flow')
        settings = get_bot_settings()
        settings.registration_enabled = 1
        settings.registration_flow = registration.name
        settings.set('registration_states', [dict(state=state_name(registration.name, 'success'))])
        settings.set('registration_fields', [dict(field_key='contract', label='Contract', keep=1)])
        settings.greeting_enabled = 1
        settings.greeting_flow = greeting.name
        settings.set('greeting_states', [dict(state=state_name(greeting.name, 'success'))])
        settings.save()
        self.activate(registration, False)
        self.activate(greeting, False)
        with self.assertRaises(frappe.ValidationError):
            get_bot_settings().require_operational_configuration()
        self.assertEqual(self.activate(registration, True)['consumer_status'], 'Enabled')
        self.assertEqual(self.activate(greeting, True)['consumer_status'], 'Enabled')
        get_bot_settings().require_operational_configuration()
        for doc in (registration, greeting):
            doc.reload()
            values = approval.invalidate(doc, 'process_revision_changed')
            approval._write(doc, values)
            doc.reload()
            self.assertEqual(doc.status, 'Unavailable')
            self.assertFalse(approval.is_current(doc))
        with self.assertRaises(frappe.ValidationError):
            get_bot_settings().require_operational_configuration()
        self.assertEqual(validate_process(registration.as_dict())['consumer_status'], 'Disabled')
        registration.reload()
        self.assertEqual(self.activate(registration, True)['consumer_status'], 'Enabled')
        with self.assertRaises(frappe.ValidationError):
            get_bot_settings().require_operational_configuration()
        self.assertEqual(validate_process(greeting.as_dict())['consumer_status'], 'Disabled')
        greeting.reload()
        self.assertEqual(self.activate(greeting, True)['consumer_status'], 'Enabled')
        get_bot_settings().require_operational_configuration()

    def test_selected_greeting_completion_controls_only_the_shared_menu(self):
        from copy import deepcopy
        from messaging.conversation.doctype.chat_session.chat_session import apply_flow_result
        from messaging.conversation.doctype.chat_turn.turn_decision import assign_configured_flows
        flow = TestHumanFlowConfiguration.configured_flow(self)
        self.activate(flow)
        settings = get_bot_settings()
        settings.greeting_enabled = 1; settings.greeting_flow = flow.name
        settings.show_menu_on_greeting_only = 1
        settings.set('greeting_states', [dict(state=state_name(flow.name, 'success'))]); settings.save()
        item = dict(flow=flow.name, request_id='greeting', values={})
        for status, action, expected in [('success', 'Complete Flow', True), ('not_found', 'Complete Flow', False), ('success', 'Cancel Flow', False)]:
            with self.subTest(status=status, action=action):
                state = dict(queue=[deepcopy(item)], values={}, menu_after_greeting=True)
                session = frappe._dict(active_flow=flow.name, status='Executing')
                result = dict(functional=True, status=status, flow_action=action)
                with patch('messaging.conversation.doctype.chat_session.chat_session.invoke', side_effect=AssertionError('Applying a receipt cannot execute a Process')):
                    parts, menu, waiting = apply_flow_result(result, flow, state['queue'][0], session, state, frappe._dict(name='turn'), settings, [])
                self.assertEqual(bool(state.get('menu_after_greeting')), expected)
                self.assertEqual(parts, []); self.assertFalse(menu); self.assertFalse(waiting)
        for selected, expected in [([flow.name], True), ([flow.name, 'business'], False)]:
            state = {}
            assign_configured_flows(dict(flows=selected), settings, state)
            self.assertEqual(bool(state.get('menu_after_greeting')), expected)
        state = dict(queue=[dict(flow='business')])
        assign_configured_flows(dict(flows=[flow.name]), settings, state)
        self.assertNotIn('menu_after_greeting', state)
        for exhausted in (False, True):
            retry_item = deepcopy(item)
            retry_item['values'] = {flow.parameters[0].parameter: '12345'}
            state = dict(queue=[retry_item], values={}, menu_after_greeting=True)
            result = dict(functional=True, status='not_found', flow_action='Retry Parameter', retry_parameter=flow.parameters[0].parameter)
            with patch('messaging.conversation.doctype.chat_session.chat_session.reject_attempt', return_value=exhausted):
                parts, menu, waiting = apply_flow_result(result, flow, state['queue'][0], session, state, frappe._dict(name='turn'), settings, [])
            self.assertEqual(bool(state.get('menu_after_greeting')), not exhausted)
            self.assertEqual(waiting, not exhausted)
            self.assertEqual(len(parts), 1)

    def test_shared_menu_header_is_independent_of_informative_categories(self):
        from messaging.conversation.doctype.chat_session.chat_session import main_menu
        settings = get_bot_settings()
        settings.informative_show_available_categories = 0
        set_localized_text(settings, 'informative_categories_header', 'Shared menu header.')
        with patch('messaging.conversation.doctype.chat_session.chat_session.mechanical_menu_options', return_value={'business': dict(flow='business', label='Business')}):
            menu = main_menu(settings, bind=False)
        self.assertEqual(menu.count('Shared menu header.'), 1)
        self.assertIn('1. Business', menu)

    def test_disabled_human_notice_does_not_require_the_hidden_flow(self):
        from messaging.conversation.doctype.chat_session.human import request_human
        from messaging.chatbot.doctype.bot_settings.localization import required_message_keys
        settings = get_bot_settings()
        settings.human_attention_enabled = 0
        settings.human_flow = None; settings.human_inactivity_minutes = 0
        settings.set('human_transfer_states', []); settings.set('human_fields', [])
        set_localized_text(settings, 'human_active_message', '')
        settings.save()
        session = frappe._dict(status='Active')
        message = request_human(session, {}, {}, dict(human_requested=True), settings, turn='synthetic')
        self.assertEqual(message, settings_text(settings, 'human_disabled_message'))
        self.assertTrue(message)
        self.assertNotIn('human_active_message', required_message_keys(settings, operational=True))


class TestNativeHumanSchedule(unittest.TestCase):
    """The schedule is conditional configuration, evaluated in the site's local time."""
    def settings(self):
        from messaging.presets.processes.human_availability import HUMAN_FLOW
        return frappe.get_doc(dict(doctype='Bot Settings', human_attention_enabled=1, human_flow=HUMAN_FLOW,
            human_support_week=[dict(weekday=day, enabled=day in ('Monday', 'Tuesday'), start_time='09:00:00' if day in ('Monday', 'Tuesday') else None,
                end_time='17:00:00' if day in ('Monday', 'Tuesday') else None)
                for day in ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]))

    def test_required_days_duplicates_intervals_and_closed_days(self):
        settings = self.settings()
        settings.validate_human_schedule()
        for mutate in (lambda: settings.human_support_week.pop(),
            lambda: setattr(settings.human_support_week[6], 'weekday', 'Monday'),
            lambda: setattr(settings.human_support_week[0], 'start_time', None),
            lambda: setattr(settings.human_support_week[0], 'end_time', '08:00:00'),
            lambda: setattr(settings.human_support_week[0], 'end_time', '09:00:00'),
            lambda: setattr(settings.human_support_week[0], 'start_time', '25:00:00'),
            lambda: setattr(settings.human_support_week[0], 'end_time', '24:00:00'),
            lambda: setattr(settings.human_support_week[0], 'start_time', '-01:00:00'),
            lambda: setattr(settings.human_support_week[0], 'end_time', 'invalid')):
            settings = self.settings()
            mutate()
            with self.assertRaises(frappe.ValidationError): settings.validate_human_schedule()
        settings = self.settings()
        settings.human_support_week[0].start_time = '00:00:00'
        settings.validate_human_schedule()

    def test_site_local_boundaries_closed_day_and_day_transition(self):
        from datetime import datetime
        settings = self.settings()
        for day, hour, minute, expected in ((2, 8, 59, False), (2, 9, 0, True), (2, 16, 59, True),
            (2, 17, 0, False), (2, 23, 59, False), (3, 0, 0, False), (3, 9, 0, True), (4, 10, 0, False), (8, 10, 0, False)):
            with self.subTest(day=day, hour=hour), patch('frappe.utils.now_datetime', return_value=datetime(2026, 11, day, hour, minute)):
                self.assertEqual(settings.human_support_available(), expected)

    def test_disabled_and_custom_flows_do_not_read_or_validate_schedule_and_reselect_preserves_rows(self):
        from messaging.presets.processes.human_availability import HUMAN_FLOW
        settings = self.settings()
        rows = [row.as_dict() for row in settings.human_support_week]
        settings.human_flow = 'custom_support'
        settings.validate_human_schedule()
        self.assertEqual([row.as_dict() for row in settings.human_support_week], rows)
        settings.human_flow = HUMAN_FLOW
        settings.validate_human_schedule()
        self.assertEqual([row.as_dict() for row in settings.human_support_week], rows)
        for enabled, flow in ((0, HUMAN_FLOW), (1, 'custom_support')):
            settings.human_attention_enabled, settings.human_flow = enabled, flow
            def forbidden_read(obj):
                raise AssertionError('Custom or disabled feature must not read the schedule')
            with patch.object(type(settings), 'human_support_week', new=property(forbidden_read), create=True):
                settings.validate_human_schedule()

    def test_metadata_condition_matches_only_exact_default_flow(self):
        meta = frappe.get_meta('Bot Settings')
        condition = "eval:doc.human_attention_enabled && doc.human_flow === 'messaging_human_availability'"
        self.assertEqual(meta.get_field('human_schedule_section').depends_on, condition)
        self.assertEqual(meta.get_field('human_support_week').depends_on, condition)
        self.assertEqual(meta.get_field('human_support_week').mandatory_depends_on, condition)
        self.assertFalse(frappe.get_meta('Bot Flow').has_field('human_support_week'))


class TestEffectiveFlowSelections(unittest.TestCase):
    def settings(self, consumer, selected='custom_flow', enabled=0, old=None):
        field = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
        doc=frappe._dict({consumer+'_flow':selected,field:enabled})
        doc.get_doc_before_save=lambda:old
        return doc

    def flow(self, status):
        return frappe._dict(name='custom_flow',lifecycle_status=status,check_permission=lambda *args:None,
            has_permission=lambda *args:True)

    def test_enabled_consumers_require_readiness_and_disabled_selections_are_retained(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import validate_selection
        for consumer in ('registration','greeting','human'):
            for enabled in (0,1):
                for status in ('Enabled','Disabled','Untested','Unavailable'):
                    settings=self.settings(consumer,enabled=enabled)
                    with patch('frappe.get_doc',return_value=self.flow(status)):
                        if not enabled or status=='Enabled':validate_selection(settings,consumer)
                        else:
                            with self.subTest(consumer=consumer,status=status,enabled=enabled),self.assertRaises(frappe.ValidationError):
                                validate_selection(settings,consumer)
            old=self.settings(consumer,enabled=0)
            current=self.settings(consumer,enabled=1,old=old)
            with patch('frappe.get_doc',return_value=self.flow('Untested')):
                with self.assertRaises(frappe.ValidationError):validate_selection(current,consumer)

    def test_unavailable_enabled_references_block_saving_and_execution(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
        for consumer in ('registration','greeting','human'):
            for status in ('Disabled','Untested','Unavailable'):
                old=self.settings(consumer,enabled=1)
                current=self.settings(consumer,enabled=1,old=old)
                current.unrelated='changed'
                table='human_transfer_states' if consumer=='human' else consumer+'_states'
                old[table]=[frappe._dict(state='historical',modified='old timestamp')]
                current[table]=[frappe._dict(state='historical',modified='new timestamp')]
                with patch('frappe.get_doc',return_value=self.flow(status)):
                    with self.assertRaises(frappe.ValidationError):
                        configuration(current,consumer,operational=True,synchronize_fields=True)
                    self.assertEqual(current[consumer+'_flow'],'custom_flow')
                    with self.assertRaises(frappe.ValidationError):configuration(current,consumer,operational=True)
                current[consumer+'_flow']='replacement'
                with patch('frappe.get_doc',return_value=self.flow('Disabled')):
                    with self.assertRaises(frappe.ValidationError):configuration(current,consumer,synchronize_fields=True)

    def test_selector_filters_effective_state_before_pagination(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import flow_options
        rows=[frappe._dict(name=state,workflow_name=state) for state in ('Disabled','Untested','Unavailable','Enabled')]
        with patch('frappe.get_list',return_value=rows),patch('frappe.has_permission',return_value=True),patch('frappe.get_doc',side_effect=lambda dt,name:self.flow(name)):
            self.assertEqual(flow_options('Bot Flow','','name',0,1,{}), [('Enabled','Enabled')])

    def test_selector_scans_bounded_pages_before_effective_result_offset(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import flow_options
        stale = [frappe._dict(name='stale_' + str(index), workflow_name='Stale') for index in range(20)]
        eligible = [frappe._dict(name='ready_' + str(index), workflow_name='Ready') for index in range(2)]
        with patch('frappe.get_list', side_effect=[stale, eligible]) as query, patch(
            'frappe.has_permission', return_value=True), patch('frappe.get_doc',
                side_effect=lambda dt, name: self.flow('Enabled' if name.startswith('ready_') else 'Untested')):
            self.assertEqual(flow_options('Bot Flow', '', 'name', 1, 1, {}), [('ready_1', 'Ready')])
        self.assertEqual([call.kwargs['limit_start'] for call in query.call_args_list], [0, 20])
        self.assertEqual([call.kwargs['limit_page_length'] for call in query.call_args_list], [20, 20])


class TestRequiredLanguageMessages(FrappeTestCase):
    def setUp(self):
        frappe.set_user('Administrator')

    def tearDown(self):
        frappe.db.rollback()

    def test_default_messages_are_required_and_optional_languages_can_be_empty(self):
        from messaging.chatbot.doctype.bot_settings.localization import required_message_keys
        settings = configure_test_settings()
        settings.append('chatbot_languages', dict(language='en', enabled=1))
        settings.append('message_translations', dict(language='en', message_key='informative_message', message=''))
        settings.save()
        required = required_message_keys(settings)
        self.assertNotIn('informative_categories_header', required)
        self.assertNotIn('human_active_message', required)
        self.assertNotIn('quota_courtesy_message', required)
        for key in required:
            settings.reload()
            row = next(r for r in settings.message_translations if r.language == 'es' and r.message_key == key)
            original = row.message
            row.message = ''
            with self.subTest(key=key), self.assertRaises(frappe.ValidationError) as error:
                settings.save()
            self.assertIn('es', str(error.exception))
            self.assertEqual(frappe.db.get_value('Bot Settings Translation', row.name, 'message'), original)
        settings.reload()
        settings.human_attention_enabled = 1
        self.assertIn('human_active_message', required_message_keys(settings))
        settings.human_attention_enabled = 0
        with patch('messaging.chatbot.doctype.bot_settings.localization.courtesy_message_required', return_value=True):
            self.assertIn('quota_courtesy_message', required_message_keys(settings))


class TestHumanRequestThreshold(unittest.TestCase):
    def test_default_metadata_and_strict_positive_integer_before_coercion(self):
        from messaging.chatbot.doctype.bot_settings.bot_settings import validate_human_requests_required
        field = frappe.get_meta('Bot Settings').get_field('human_requests_required')
        self.assertEqual((field.fieldtype, str(field.default)), ('Int', '1'))
        self.assertEqual(field.depends_on, 'eval:doc.human_attention_enabled')
        for value in (0, -1, 1.5, 1.0, True, False, None, '', '1.5', 'abc'):
            with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
                validate_human_requests_required(value)
        self.assertEqual(validate_human_requests_required('3'), 3)
        self.assertEqual(validate_human_requests_required(1), 1)
        settings = frappe.get_doc(dict(doctype='Bot Settings', human_attention_enabled=1, human_requests_required=1.5))
        with self.assertRaises(frappe.ValidationError):
            settings.before_validate()


class TestConsumerDiagnostics(unittest.TestCase):
    """Synthetic dependencies exercise the real shared validators without changing site Processes."""
    def setUp(self):
        from contextlib import ExitStack
        from messaging.chatbot.doctype.bot_settings import flow_consumers as consumers
        self.consumers = consumers
        for doctype in ('Flow Consumer State', 'Registration Field', 'Human Request Field', 'Human Support Day'):
            get_controller(doctype)
            frappe.get_meta(doctype)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.settings = frappe.get_doc(dict(doctype='Bot Settings', name='Bot Settings',
            chatbot_languages=[dict(language='es', enabled=1, is_default=1)],
            human_requests_required=1, human_inactivity_minutes=5, max_invalid_attempts=3,
            show_menu_on_attempts_exhausted=0, exempt_registration_from_quotas=0, max_registration_exempt_messages=0))
        self.settings._doc_before_save = frappe._dict()
        self.flow = frappe._dict(name='synthetic_flow', workflow_name='Synthetic Flow',
            process_definition='synthetic.process', status='Enabled', lifecycle_status='Enabled',
            flags=frappe._dict(), check_permission=lambda *args: None, test_state=lambda: 'Passed',
            parameters=[frappe._dict(required=1, parameter='synthetic_parameter')])
        self.outputs = {'success': dict(state='synthetic_state', status_code='success',
            action='Complete Flow', fields={'name': 'string'})}
        self.definition = frappe._dict(name='synthetic.process')
        self.stack.enter_context(patch('frappe.get_doc', return_value=self.flow))
        self.stack.enter_context(patch('frappe.db.get_value', side_effect=lambda dt, *args, **kwargs:
            frappe._dict(bot_flow=self.flow.name, status_code='success') if dt == 'Bot Flow State' else 1))
        self.stack.enter_context(patch.object(consumers, 'output_catalog', side_effect=lambda flow: self.outputs))
        self.stack.enter_context(patch('messaging.communication.doctype.process_definition.process_definition.resolve_definition', return_value=self.definition))
        self.verify = self.stack.enter_context(patch('messaging.registry.processes.discovery.verify_registered_revision'))
        self.stack.enter_context(patch('frappe.has_permission', return_value=True))
        self.stack.enter_context(patch('messaging.chatbot.doctype.bot_parameter.bot_parameter.get_request_phrase', return_value='Name?'))
        self.language = self.stack.enter_context(patch('messaging.chatbot.doctype.bot_flow.localization.require_default_language'))
        self.stack.enter_context(patch('messaging.chatbot.doctype.bot_flow.localization.text', return_value='Synthetic semantics'))
        self.logger = self.stack.enter_context(patch.object(consumers, '_verification_error', return_value='synthetic-error-reference'))
        self.execute = self.stack.enter_context(patch('messaging.communication.doctype.process_definition.execution.execute_process', side_effect=AssertionError('A diagnostic must never execute a Process')))

    def enable(self, consumer):
        self.flow.name = 'synthetic_' + consumer
        flow_field, states_field, fields_field = self.consumers.CONSUMERS[consumer]
        self.settings.set('human_attention_enabled' if consumer == 'human' else consumer + '_enabled', 1)
        self.settings.set(flow_field, self.flow.name)
        self.settings.set(states_field, [dict(state='synthetic_state')])
        if fields_field:
            self.settings.set(fields_field, [dict(field_key='name', label='Name', keep=1,
                **({'states': 'success'} if consumer == 'human' else {}))])
        return flow_field, states_field, fields_field

    def diagnose(self, consumer):
        result = self.consumers.diagnose_consumer(self.settings, consumer)
        self.execute.assert_not_called()
        return result

    def test_unchanged_settings_and_menu_only_saves_check_availability_without_contract_validation(self):
        for consumer in self.consumers.CONSUMERS:
            self.enable(consumer)
            self.settings._doc_before_save = frappe._dict(self.settings.as_dict())
            self.verify.reset_mock()
            self.consumers.output_catalog.reset_mock()
            self.language.reset_mock()
            for preference in (0, 1):
                self.settings.show_menu_on_greeting_only = preference
                result = self.diagnose(consumer)
                self.assertEqual(result['state'], 'operational')
                self.assertFalse(result['configuration_checked'])
            self.verify.assert_not_called()
            self.consumers.output_catalog.assert_not_called()
            self.language.assert_not_called()

    def test_consumer_edit_validates_only_its_changed_contract(self):
        self.enable('registration')
        self.enable('greeting')
        self.enable('human')
        self.settings._doc_before_save = frappe._dict(self.settings.as_dict())
        self.settings.registration_fields[0].keep = 0
        self.assertTrue(self.consumers.configuration_changed(self.settings, 'registration'))
        self.assertFalse(self.consumers.configuration_changed(self.settings, 'greeting'))
        self.assertFalse(self.consumers.configuration_changed(self.settings, 'human'))
        result = self.diagnose('registration')
        self.assertEqual(result['state'], 'incomplete')
        self.assertTrue(result['configuration_checked'])

    def test_unchanged_consumer_still_rejects_effectively_inactive_flow(self):
        self.enable('greeting')
        self.settings._doc_before_save = frappe._dict(self.settings.as_dict())
        self.flow.status = 'Disabled'
        self.assertEqual(self.diagnose('greeting')['state'], 'unavailable')
        self.flow.status = 'Enabled'
        self.flow.test_state = lambda: 'Untested'
        self.assertEqual(self.diagnose('greeting')['state'], 'technical_required')
        self.verify.assert_not_called()
        self.consumers.output_catalog.assert_not_called()

    def test_time_serialization_and_child_metadata_do_not_count_as_configuration_edits(self):
        from datetime import timedelta
        self.enable('human')
        old = frappe._dict(self.settings.as_dict())
        old['human_support_week'] = [frappe._dict(weekday='Monday', enabled=1, start_time=timedelta(hours=8), end_time=timedelta(hours=17))]
        self.settings.set('human_support_week', [dict(weekday='Monday', enabled=1, start_time='8:00:00', end_time='17:00:00')])
        self.settings._doc_before_save = old
        self.settings.human_transfer_states[0].modified = 'different row metadata'
        self.assertFalse(self.consumers.configuration_changed(self.settings, 'human'))

    def test_each_consumer_uses_its_own_contract_without_writes(self):
        for consumer in self.consumers.CONSUMERS:
            self.enable(consumer)
            before = self.settings.as_dict()
            with self.subTest(consumer=consumer):
                self.assertEqual(self.diagnose(consumer)['state'], 'operational')
                self.assertEqual(self.settings.as_dict(), before)
        self.assertFalse(self.settings.get('greeting_fields'))
        self.logger.assert_not_called()

    def test_unexpected_validation_exception_is_logged_as_internal(self):
        self.enable('greeting')
        with patch.object(self.consumers, 'options', side_effect=frappe.ValidationError('PRIVATE_INFRASTRUCTURE_DETAIL')):
            result = self.diagnose('greeting')
        self.assertEqual(result['state'], 'internal_error')
        self.assertNotIn('PRIVATE_INFRASTRUCTURE_DETAIL', json.dumps(result))

    def test_missing_flow_states_and_disabled_retention_for_each_consumer(self):
        for consumer in self.consumers.CONSUMERS:
            flow_field, states_field, _ = self.enable(consumer)
            self.settings.set(states_field, [])
            result = self.diagnose(consumer)
            self.assertEqual((result['state'], result['issues'][0]['field']), ('incomplete', states_field))
            self.settings.set(flow_field, None)
            self.assertEqual(self.diagnose(consumer)['state'], 'incomplete')
            self.settings.set('human_attention_enabled' if consumer == 'human' else consumer + '_enabled', 0)
            self.verify.reset_mock()
            self.assertEqual(self.diagnose(consumer)['state'], 'disabled')
            self.verify.assert_not_called()

    def test_disabled_flow_retired_process_and_code_revision_are_distinct(self):
        from messaging.communication.doctype.process_definition.process_definition import ProcessUnavailableError
        for consumer in self.consumers.CONSUMERS:
            self.enable(consumer)
            self.flow.status = 'Disabled'
            self.assertEqual(self.diagnose(consumer)['state'], 'unavailable')
            self.flow.status = 'Enabled'
            self.verify.side_effect = frappe.ValidationError('Process code changed. Synchronize its owning application.')
            result = self.diagnose(consumer)
            self.assertEqual(result['state'], 'technical_required')
            self.assertIn('Process code changed', result['issues'][0]['message'])
            self.verify.side_effect = None
            with patch('messaging.communication.doctype.process_definition.process_definition.resolve_definition', side_effect=ProcessUnavailableError('Process unavailable')):
                self.assertEqual(self.diagnose(consumer)['state'], 'unavailable')
            self.assertEqual(self.diagnose(consumer)['state'], 'operational')

    def test_stale_approval_and_internal_evidence_failure_are_distinct(self):
        self.enable('greeting')
        self.flow.test_state = lambda: 'Untested'
        self.assertEqual(self.diagnose('greeting')['state'], 'technical_required')
        self.flow.flags.process_verification_error = 'Synthetic verification failure'
        result = self.diagnose('greeting')
        self.assertEqual(result['state'], 'internal_error')
        self.assertEqual(result['issues'][0]['reference'], 'synthetic-error-reference')

    def test_duplicate_foreign_cancel_retry_and_removed_states(self):
        for consumer in self.consumers.CONSUMERS:
            _, table, _ = self.enable(consumer)
            self.settings.append(table, dict(state='synthetic_state'))
            result = self.diagnose(consumer)
            self.assertEqual((result['state'], result['issues'][0]['field']), ('incomplete', table))
            self.settings.set(table, [dict(state='foreign')])
            self.assertEqual(self.diagnose(consumer)['state'], 'incomplete')
            self.settings.set(table, [dict(state='synthetic_state')])
            for action in ('Cancel Flow', 'Retry Parameter'):
                self.outputs['success']['action'] = action
                self.assertEqual(self.diagnose(consumer)['state'], 'incomplete')
            self.outputs['success']['action'] = 'Complete Flow'
            self.assertEqual(self.diagnose(consumer)['state'], 'operational')

    def test_required_capture_fields_and_human_field_relations(self):
        _, _, fields = self.enable('registration')
        self.flow.parameters = []
        self.assertIn('required input', self.diagnose('registration')['issues'][0]['message'])
        self.flow.parameters = [frappe._dict(required=1, parameter='synthetic_parameter')]
        self.settings.get(fields)[0].keep = 0
        self.assertEqual(self.diagnose('registration')['issues'][0]['field'], fields)
        self.settings.get(fields)[0].keep = 1
        self.settings.get(fields)[0].field_key = 'removed'
        self.assertEqual(self.diagnose('registration')['issues'][0]['field'], fields)
        _, _, fields = self.enable('human')
        self.settings.get(fields)[0].states = 'removed'
        self.assertEqual(self.diagnose('human')['issues'][0]['field'], fields)
        self.settings.get(fields)[0].states = 'success'
        self.assertEqual(self.diagnose('human')['state'], 'operational')

    def test_internal_errors_never_become_manual_configuration_errors(self):
        self.enable('human')
        self.verify.side_effect = OSError('Synthetic private technical details')
        result = self.diagnose('human')
        self.assertEqual(result['state'], 'internal_error')
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['issues'][0]['reference'], 'synthetic-error-reference')
        self.verify.side_effect = None
        self.assertEqual(self.diagnose('human')['state'], 'operational')

    def test_backend_save_guard_groups_features_and_disabled_features_do_not_block(self):
        for consumer in self.consumers.CONSUMERS:
            _, states, _ = self.enable(consumer)
            self.settings.set(states, [])
        with self.assertRaises(frappe.ValidationError) as error:
            self.consumers.require_valid_consumers(self.settings)
        for label in ('Registration', 'Greeting', 'Human Support'):
            self.assertIn(label, str(error.exception))
        for consumer in self.consumers.CONSUMERS:
            self.settings.set('human_attention_enabled' if consumer == 'human' else consumer + '_enabled', 0)
        self.consumers.require_valid_consumers(self.settings)

    def test_greeting_menu_preference_does_not_change_flow_states_or_approval(self):
        self.enable('greeting')
        for value in (0, 1):
            self.settings.show_menu_on_greeting_only = value
            before = self.settings.as_dict()
            self.assertEqual(self.diagnose('greeting')['state'], 'operational')
            self.assertEqual(self.settings.as_dict(), before)

    def test_disabled_missing_links_are_retained_but_other_native_links_are_checked(self):
        from messaging.chatbot.doctype.bot_settings.bot_settings import BotSettings
        # Construct before the get_doc mock so the real owner's override is tested.
        settings = BotSettings(dict(doctype='Bot Settings', name='Bot Settings',
            greeting_enabled=0, greeting_flow='removed_flow', greeting_states=[dict(state='removed_state')],
            active_ai_configuration='required_native_reference'))
        before = settings.as_dict()
        def native(doc):
            self.assertIsNone(doc.greeting_flow)
            self.assertEqual(doc.greeting_states, [])
            self.assertEqual(doc.active_ai_configuration, 'required_native_reference')
            raise frappe.LinkValidationError('Synthetic unrelated link failure')
        with patch.object(Document, '_validate_links', native):
            with self.assertRaises(frappe.LinkValidationError): settings._validate_links()
        self.assertEqual(settings.as_dict(), before)


class TestDiagnosticEvidence(unittest.TestCase):
    def setUp(self):
        self.created_error_logs = set()
        self.original_user = frappe.session.user
        frappe.set_user('Administrator')
        frappe.db.savepoint('settings_diagnostic_evidence')

    def tearDown(self):
        frappe.db.rollback(save_point='settings_diagnostic_evidence')
        for name in self.created_error_logs:
            if frappe.db.exists('Error Log', name):
                frappe.delete_doc('Error Log', name)
        frappe.set_user(self.original_user)
        frappe.clear_last_message()

    def test_real_error_log_reference_is_redacted_and_reused(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import _verification_error
        references = []
        original = frappe.cache.set_value
        with patch.object(frappe.cache, 'set_value', wraps=original) as cached:
            for _ in range(2):
                try:
                    raise RuntimeError('SYNTHETIC_SECRET_MUST_NOT_BE_LOGGED')
                except RuntimeError:
                    reference = _verification_error('synthetic_diagnostic_evidence')
                    references.append(reference)
                    if reference:
                        self.created_error_logs.add(reference)
            self.assertTrue(references[0])
            self.assertEqual(references[0], references[1])
            log = frappe.get_doc('Error Log', references[0])
            self.assertIn('RuntimeError', log.error)
            self.assertNotIn('SYNTHETIC_SECRET_MUST_NOT_BE_LOGGED', log.error)
            for call in cached.call_args_list:
                if str(call.args[0]).startswith('messaging:settings_verification:'):
                    self.assertEqual(call.kwargs['expires_in_sec'], 300)
                    frappe.cache.delete_value(call.args[0])

    def test_real_menu_only_save_does_not_revalidate_flow_contracts_or_sources(self):
        settings = frappe.get_single('Bot Settings')
        settings.show_menu_on_greeting_only = int(not settings.show_menu_on_greeting_only)
        with patch('messaging.chatbot.doctype.bot_settings.flow_consumers.output_catalog', side_effect=AssertionError('Unchanged contracts must not be validated')), patch(
            'messaging.registry.processes.discovery.verify_registered_revision', side_effect=AssertionError('Unchanged Flow sources must not be inspected')), patch(
            'messaging.chatbot.doctype.bot_flow.localization.default_language_issues', side_effect=AssertionError('Unchanged Flow languages must not be revalidated')):
            settings.save()
        self.assertEqual(frappe.get_single('Bot Settings').show_menu_on_greeting_only, settings.show_menu_on_greeting_only)

    def test_disabled_single_saves_missing_references_and_retains_its_values(self):
        settings = frappe.get_single('Bot Settings')
        for field in ('registration_enabled', 'greeting_enabled', 'human_attention_enabled'):
            settings.set(field, 0)
        settings.greeting_flow = 'synthetic_removed_flow'
        settings.set('greeting_states', [dict(state='synthetic_removed_state')])
        settings.human_requests_required = 0
        settings.human_inactivity_minutes = 0
        settings.save()
        reloaded = frappe.get_single('Bot Settings')
        self.assertEqual(reloaded.greeting_flow, 'synthetic_removed_flow')
        self.assertEqual(reloaded.greeting_states[0].state, 'synthetic_removed_state')
        self.assertEqual(reloaded.human_requests_required, 0)
        self.assertEqual(reloaded.human_inactivity_minutes, 0)

    def test_diagnostic_sources_have_exact_spanish_translations(self):
        from frappe.translate import extract_messages_from_python_code
        root = Path(frappe.get_app_path('messaging'))
        with (root / 'translations/es.csv').open(newline='') as stream:
            rows = list(csv.reader(stream))
        keys = [message[1] for message in extract_messages_from_python_code(
            (root / 'chatbot/doctype/bot_settings/flow_consumers.py').read_text())]
        keys += ['Operational', 'Incomplete configuration', 'Dependency unavailable',
            'Technical validation required', 'Verification error', 'Checking']
        for key in set(keys):
            with self.subTest(key=key):
                self.assertEqual(sum(row[0] == key and len(row) == 2 for row in rows), 1)

    def test_diagnostic_requires_permissions_and_rejects_other_documents(self):
        from messaging.chatbot.doctype.bot_settings.flow_consumers import diagnose
        with patch('frappe.has_permission', side_effect=frappe.PermissionError):
            with self.assertRaises(frappe.PermissionError): diagnose('{}', 'greeting')
        for data, consumer in [({'doctype': 'Bot Flow', 'name': 'Bot Settings'}, 'greeting'),
            ({'doctype': 'Bot Settings', 'name': 'other'}, 'greeting'), ({}, 'unknown')]:
            with self.assertRaises(frappe.ValidationError): diagnose(data, consumer)
