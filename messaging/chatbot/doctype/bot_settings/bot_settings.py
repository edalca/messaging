import re
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.chatbot.doctype.bot_settings.language import (
	get_bot_settings,
	resolve_process_language,
	validate_chatbot_languages,
	validate_language,
)
from messaging.chatbot.doctype.bot_settings.localization import (
	DEFAULT_SAFE_INTERNAL_ERROR_MESSAGE,
	UNSAFE_ERROR_MESSAGE_PATTERNS,
	get_internal_error_message,
	text as localized_text,
	validate_safe_error_message,
)
from messaging.communication.doctype.communication_template.communication_template import validate_template_syntax


class BotSettings(Document):
	def check_if_latest(self):
		# Lock the stable metadata row before Singles to serialize initial saves.
		try:
			frappe.db.get_value('DocType', 'Bot Settings', 'name', for_update=True)
			super().check_if_latest()
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			frappe.throw(_("Bot Settings changed. Reload before saving the configuration."), frappe.TimestampMismatchError)

	def _validate_links(self):
		from messaging.chatbot.doctype.bot_settings.flow_consumers import CONSUMERS
		retained = {}
		for consumer, (flow_field, states_field, _fields) in CONSUMERS.items():
			enabled = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
			if not self.get(enabled):
				# Native Link validation clears missing references. Disabled consumers must
				# retain those references while all other document links remain protected.
				for field in (flow_field, states_field):
					retained[field] = self.get(field)
					self.set(field, [] if field == states_field else None)
		try:
			super()._validate_links()
		finally:
			for field, value in retained.items():
				self.set(field, value)

	def before_validate(self) -> None:
		validate_chatbot_languages(self, assign_default=True)
		if self.human_attention_enabled:
			validate_human_requests_required(self.human_requests_required)
			validate_human_inactivity(self.human_inactivity_minutes)

	def validate(self) -> None:
		validate_chatbot_languages(self)
		from messaging.chatbot.doctype.bot_settings.flow_consumers import require_valid_consumers
		require_valid_consumers(self)
		self.validate_ai()
		self._validate_informative_message()
		operational = self.has_automation()
		attempts = int(self.max_invalid_attempts or 0)
		if (operational or self.max_invalid_attempts) and not 1 <= attempts <= 100:
			frappe.throw(_("Maximum Invalid Attempts per Flow must be between 1 and 100."))
		from messaging.chatbot.doctype.bot_settings.localization import validate
		validate(self, operational=operational)
		previous = self.get_doc_before_save()
		language_values = lambda doc: [(row.language, row.enabled, row.is_default) for row in doc.chatbot_languages or []]
		if previous is None or language_values(self) != language_values(previous):
			from messaging.chatbot.doctype.bot_flow.localization import default_language_issues
			issues = []
			default = validate_chatbot_languages(self)
			for name in frappe.get_all('Bot Flow', filters={'status': 'Enabled'}, pluck='name'):
				issues.extend(default_language_issues(frappe.get_doc('Bot Flow', name), default, settings=self))
			if issues:
				frappe.throw('\n'.join(issues))

	def has_automation(self):
		return any(self.get(field) for field in (
			'interpretation_enabled', 'registration_enabled', 'greeting_enabled', 'human_attention_enabled',
			'informative_show_available_categories', 'show_menu_on_attempts_exhausted')) or bool(frappe.db.exists('Bot Flow', {'status': 'Enabled'}))

	def require_operational_configuration(self, *, flow=None, channel=None):
		"""Check current dependencies before automatic conversation work or activation."""
		from messaging.chatbot.doctype.bot_settings.localization import validate
		validate(self, operational=True)
		if not 1 <= int(self.max_invalid_attempts or 0) <= 100:
			frappe.throw(_("Maximum Invalid Attempts per Flow must be between 1 and 100."))
		if channel is not None:
			from messaging.communication.doctype.communication_channel.communication_channel import get_channel_quota_policy
			get_channel_quota_policy(channel, settings=self, operational=True)
		if flow is None or self.registration_flow == flow:
			self._validate_registration()
		self._validate_greeting_and_courtesy(validate_greeting=flow is None or self.greeting_flow == flow)

	def validate_ai(self):
		from messaging.chatbot.doctype.ai_configuration.ai_configuration import resolve_configuration
		if self.active_ai_configuration and frappe.db.get_value('AI Configuration', self.active_ai_configuration, 'profile_type') != 'Decision':
			frappe.throw(_("Select a Decision AI profile."))
		old = self.get_doc_before_save()
		changed = not old or old.active_ai_configuration != self.active_ai_configuration
		if self.active_ai_configuration and (changed or (self.interpretation_enabled and (not old or not old.interpretation_enabled))):
			try:
				# Read the current locked configuration and definitions, not a prior snapshot.
				resolve_configuration(self.active_ai_configuration, for_update=True, settings=self)
			except Exception as exc:
				from messaging.chatbot.doctype.ai_configuration.ai_configuration import error_code, DIAGNOSTICS
				frappe.throw(_(DIAGNOSTICS[error_code(exc)]))
		if self.interpretation_enabled and not self.active_ai_configuration:
			frappe.throw(_("Select an enabled AI configuration that passed connection validation."))


	def _validate_registration(self, *, synchronize_fields=False, read_only=False) -> None:
		from messaging.chatbot.doctype.bot_settings.flow_consumers import ConsumerIssue
		if not self.registration_enabled:
			return
		exempt_max = int(self.max_registration_exempt_messages or 0)
		if exempt_max < 0:
			raise ConsumerIssue(_("Max Registration Exempt Messages cannot be negative."), "max_registration_exempt_messages")
		if int(self.exempt_registration_from_quotas or 0) and exempt_max < 1:
			raise ConsumerIssue(_("Max Registration Exempt Messages must be at least 1 when registration quota exemption is enabled."), "max_registration_exempt_messages")

		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		configuration(self, 'registration', operational=bool(self.registration_enabled), synchronize_fields=synchronize_fields, read_only=read_only)

	def registration_flow_configuration(self, *, operational=False):
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		config = configuration(self, 'registration', operational=operational)
		return config['flow'] if config else None

	def validate_registration_flow(self, flow):
		from messaging.chatbot.doctype.bot_settings.flow_consumers import validate_registration_flow
		validate_registration_flow(self, flow)

	def _validate_greeting_and_courtesy(self, *, synchronize_fields=False, validate_greeting=True) -> None:
		if validate_greeting:
			self._validate_greeting_flow(synchronize_fields=synchronize_fields)

	def _validate_greeting_flow(self, *, synchronize_fields=False, read_only=False) -> None:
		"""Use the shared functional-state selection without persisting output fields."""
		if not self.greeting_enabled:
			return
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration, ConsumerIssue
		config = configuration(self, 'greeting', operational=True, synchronize_fields=synchronize_fields, read_only=read_only)
		if config is None:
			return
		selected = config['flow'].name
		if selected in {self.registration_flow, self.human_flow} - {None, ''}:
			raise ConsumerIssue(_('The Greeting Bot Flow must be distinct from the Registration and Human Support Flows.'), 'flow')
		flow = config['flow']
		from messaging.chatbot.doctype.bot_flow.localization import text as flow_text
		if not flow_text(flow, 'semantic_description').strip():
			raise ConsumerIssue(_('The Greeting Bot Flow needs a semantic description.'), 'flow')

	def _validate_informative_message(self) -> None:
		info_msg = (localized_text(self, "informative_message", validate_chatbot_languages(self)) or "").strip()
		validate_template_syntax(info_msg, allow_empty=True)

	def validate_human_configuration(self, *, synchronize_fields=False, read_only=False) -> None:
		if not self.human_attention_enabled:
			return
		validate_human_requests_required(self.human_requests_required)
		self.validate_human_schedule()
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		configuration(self, 'human', operational=bool(self.human_attention_enabled), synchronize_fields=synchronize_fields, read_only=read_only)
		if self.human_attention_enabled:
			validate_human_inactivity(self.human_inactivity_minutes)

	def human_flow_configuration(self, *, operational=False):
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		config = configuration(self, 'human', operational=operational)
		return config['flow'] if config else None

	def validate_human_schedule(self, *, required=None):
		from messaging.chatbot.doctype.bot_settings.flow_consumers import ConsumerIssue
		from messaging.presets.processes.human_availability import HUMAN_FLOW
		if required is None:
			required = self.human_attention_enabled and self.human_flow == HUMAN_FLOW
		if not required:
			return
		from datetime import timedelta
		from frappe.utils import get_timedelta
		days = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
		rows = self.human_support_week or []
		if len(rows) != 7 or {row.weekday for row in rows} != set(days):
			raise ConsumerIssue(_('Define each of the seven days in the human support schedule.'), 'human_support_week')
		for row in rows:
			if row.enabled not in (0, 1):
				raise ConsumerIssue(_('Select a valid daily availability checkbox.'), 'human_support_week')
			if not row.enabled:
				continue
			try:
				valid = all(value is not None and str(value).strip() and not str(value).strip().startswith('-')
					for value in (row.start_time, row.end_time))
				interval = [get_timedelta(value if isinstance(value, timedelta) else str(value))
					for value in (row.start_time, row.end_time)]
				valid = valid and all(value is not None for value in interval)
				valid = valid and timedelta() <= interval[0] < interval[1] < timedelta(days=1)
			except (ValueError, TypeError, OverflowError):
				valid = False
			if not valid:
				raise ConsumerIssue(_('An open day requires a start time earlier than its end time.'), 'human_support_week')

	def human_support_available(self):
		"""Evaluate one local daily interval, inclusive at opening and exclusive at closing."""
		from frappe.utils import now_datetime, get_time
		self.validate_human_schedule(required=True)
		current = now_datetime()
		weekday = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')[current.weekday()]
		row = next(row for row in self.human_support_week if row.weekday == weekday)
		return bool(row.enabled and get_time(row.start_time) <= current.time() < get_time(row.end_time))


def get_menu_flows(settings_doc: BotSettings | None = None) -> list[dict[str, Any]]:
	"""Return the ordered list of enabled Bot Flows to display in menus/catalogs without duplicating definitions."""
	if not frappe.db.table_exists("Bot Flow"):
		return []

	workflows = frappe.get_all(
		"Bot Flow",
		filters={"status": "Enabled", "show_in_menu": 1},
		fields=[
			"workflow_code",
			"workflow_name",
			"menu_order",
			"priority",
		],
		order_by="menu_order asc, workflow_name asc, workflow_code asc",
	)
	from messaging.chatbot.doctype.bot_flow.localization import text
	settings = settings_doc or get_bot_settings()
	assigned = {settings.registration_flow, settings.greeting_flow, settings.human_flow} - {None, ''}
	eligible = []
	for wf in workflows:
		if wf['workflow_code'] in assigned:
			continue
		flow = frappe.get_doc('Bot Flow', wf['workflow_code'])
		if flow.test_state(remember_invalid=True) != 'Passed':
			continue
		eligible.append(wf)
		wf['menu_label'] = text(flow, 'menu_label')
		wf['menu_description'] = text(flow, 'menu_description')
	return [
		{
			"workflow_code": wf["workflow_code"],
			"workflow_name": wf["workflow_name"],
			"label": (wf.get("menu_label") or "").strip() or wf["workflow_name"].strip(),
			"description": (wf.get("menu_description") or "").strip(),
			"priority": int(wf.get("priority") or 0),
		}
		for wf in eligible
	]


def compose_informative_response(settings_doc: BotSettings | None = None) -> str:
	"""Return the informative prose. The shared category menu is appended by the engine."""
	doc = settings_doc or get_bot_settings()
	return (localized_text(doc, "informative_message") or "").strip()


def validate_human_inactivity(value):
    """Validate before Int coercion, also when a client sends a fractional JSON value."""
    from messaging.chatbot.doctype.bot_settings.flow_consumers import ConsumerIssue
    if type(value) is bool or value is None or not re.fullmatch(r'[0-9]+', str(value)) or int(value) < 1:
        raise ConsumerIssue(_('Human inactivity timeout must be an integer of at least one minute.'), 'human_inactivity_minutes')
    return int(value)


def validate_human_requests_required(value):
    """Reject fractions and booleans before Frappe coerces an Int field."""
    from messaging.chatbot.doctype.bot_settings.flow_consumers import ConsumerIssue
    if type(value) is bool or value is None or not re.fullmatch(r'[0-9]+', str(value)) or int(value) < 1:
        raise ConsumerIssue(_('Human requests required must be a positive integer.'), 'human_requests_required')
    return int(value)


def is_registration_complete(contact, settings_doc=None):
    """Gate only unregistered contacts when registration is enabled in Bot Settings."""
    doc = settings_doc or get_bot_settings()
    if not doc.registration_enabled:
        return True
    from messaging.conversation.doctype.chat_contact.chat_contact import registration_complete
    return registration_complete(contact, doc)


@frappe.whitelist(methods=['POST'])
def message_requirements(candidate):
    """Read-only hints for the current draft; saving still validates every value."""
    values = frappe.parse_json(candidate)
    if not isinstance(values, dict) or values.get('doctype') != 'Bot Settings':
        raise frappe.PermissionError(_('Invalid message configuration.'))
    doc = frappe.get_doc(values)
    doc.check_permission('read')
    from messaging.chatbot.doctype.bot_settings.localization import required_message_keys
    return sorted(required_message_keys(doc, operational=doc.has_automation()))


def _flow_preset_languages(*, pending_languages=False):
    """Validate persisted languages before opening or applying default flows."""
    from messaging.common.locking import lock_document_types
    lock_document_types('Bot Settings')
    settings = get_bot_settings()
    if not settings.get('chatbot_languages'):
        if pending_languages:
            frappe.throw(_("You have added languages that are not saved yet. Save Bot Settings first and try adding default flows again."),
                title=_("Save languages"))
        frappe.throw(_("Configure at least one enabled default language in the General tab and save the changes before adding default flows."),
            title=_("Language required"))
    validate_chatbot_languages(settings)
    return [row.language for row in settings.chatbot_languages if row.enabled]


@frappe.whitelist(methods=['POST'])
def validate_flow_preset_languages(pending_languages=False):
    """Check saved languages; draft presence only selects the missing-language message."""
    from messaging.security.permissions import require_system_manager
    require_system_manager()
    frappe.get_single('Bot Settings').check_permission('write')
    _flow_preset_languages(pending_languages=frappe.utils.cint(pending_languages))
    return True


@frappe.whitelist(methods=['POST'])
def apply_flow_presets(keys, reset_keys=None, expected_versions=None):
    """Install database presets or explicitly restore selected versions from Bot Settings."""
    from messaging.security.permissions import require_system_manager
    from messaging.common.locking import document_savepoint, lock_document_types
    from messaging.chatbot.doctype.bot_flow.bot_flow import install_preset
    require_system_manager()
    frappe.get_single('Bot Settings').check_permission('write')
    frappe.has_permission('Bot Flow Presets', 'read', throw=True)
    keys = frappe.parse_json(keys)
    reset_keys = frappe.parse_json(reset_keys) if reset_keys is not None else []
    expected_versions = frappe.parse_json(expected_versions) if expected_versions is not None else None
    if (type(keys) is not list or not keys or len(keys) > 100 or any(type(key) is not str for key in keys)
            or len(keys) != len(set(keys)) or type(reset_keys) is not list
            or any(type(key) is not str for key in reset_keys) or not set(reset_keys) <= set(keys)):
        frappe.throw(_('Select a unique list of presets.'))
    if expected_versions is not None and (not isinstance(expected_versions, dict) or set(expected_versions) != set(keys)):
        frappe.throw(_('Reload the preset catalogue before applying changes.'))
    active = _flow_preset_languages()
    lock_document_types('Bot Flow Presets', 'Bot Flow', 'Bot Parameter', 'Bot Flow Template')
    result = dict(created=[], completed=[], existing=[], not_applied=[])
    for key in sorted(keys):
        try:
            with document_savepoint():
                status, name, warnings = install_preset(key, restore=key in reset_keys, languages=active,
                    expected_version=None if expected_versions is None else expected_versions[key])
                result[status].append(dict(key=key, name=name, warnings=warnings))
        except (frappe.ValidationError, frappe.PermissionError, frappe.DoesNotExistError,
                frappe.DuplicateEntryError, frappe.UniqueValidationError) as error:
            result['not_applied'].append(dict(key=key, message=str(error)))
    return result
