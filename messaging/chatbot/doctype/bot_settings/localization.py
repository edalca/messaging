"""Bot Settings owns the localized global messages."""
import re

import frappe
from frappe import N_, _
from messaging.chatbot.doctype.bot_settings.language import (
    current_language,
    get_bot_settings,
    resolve_process_language,
    validate_chatbot_languages,
)

UNSAFE_ERROR_MESSAGE_PATTERNS = (
    re.compile(r"traceback\s*\(", re.IGNORECASE),
    re.compile(r"file\s+\"/", re.IGNORECASE),
    re.compile(r"/srv/|/home/|/usr/|/var/", re.IGNORECASE),
    re.compile(r"\.py\b", re.IGNORECASE),
    re.compile(r"api[_-]?key|secret|password|bearer\s+", re.IGNORECASE),
)

DEFAULT_SAFE_INTERNAL_ERROR_MESSAGE = N_(
    "We are sorry, we could not complete your request because of a technical problem. "
    "You can try again."
)

MESSAGE_LABELS = {
    'default_internal_error_message': 'Default Safe Internal Error Message',
    'quota_courtesy_message': 'Quota Courtesy Message',
    'informative_message': 'Informative Message',
    'informative_categories_header': 'Category Menu Header',
    'attempts_exhausted_message': 'Attempts Exhausted Message',
    'human_disabled_message': 'Message When Human Support is Disabled',
    'human_active_message': 'Human Support Assigned Message',
    'human_completed_message': 'Human Support Completed Message',
}
MESSAGE_KEYS = tuple(MESSAGE_LABELS)
MESSAGE_SECTIONS = {
    'default_internal_error_message': 'general_tab',
    'quota_courtesy_message': 'general_tab',
    'informative_message': 'section_informative',
    'informative_categories_header': 'section_informative',
    'attempts_exhausted_message': 'capture_attempts_section',
    'human_disabled_message': 'human_support_tab',
    'human_active_message': 'human_support_tab',
    'human_completed_message': 'human_support_tab',
}
SAFE_MESSAGES = {'default_internal_error_message'}


def validate_safe_error_message(
    message: str | None, *, field_label: str = N_("Internal Error Message")
) -> str:
    """Ensure an error message intended for end-users contains no traces, internal paths, secrets, or placeholders."""
    field_label = _(field_label)
    if not isinstance(message, str) or not message.strip():
        frappe.throw(_("{0} is required.").format(field_label), frappe.ValidationError)

    cleaned = message.strip()
    if "{{" in cleaned or "}}" in cleaned or "{%" in cleaned:
        frappe.throw(
            _("{0} must be a static safe message and cannot include dynamic template variables.").format(
                field_label
            ),
            frappe.ValidationError,
        )

    for pattern in UNSAFE_ERROR_MESSAGE_PATTERNS:
        if pattern.search(cleaned):
            frappe.throw(
                _(
                    "{0} cannot contain stack traces, internal file paths, Python module references, or sensitive tokens."
                ).format(field_label),
                frappe.ValidationError,
            )

    return cleaned


def get_internal_error_message(message=None):
    """Localize configured safe text using the same effective Process language."""
    try:
        language = resolve_process_language()
        if message is None:
            return text(get_bot_settings(), 'default_internal_error_message', language) or _(DEFAULT_SAFE_INTERNAL_ERROR_MESSAGE, lang=language)
        return message.strip()
    except frappe.ValidationError:
        # A configuration failure must not expose another exception.
        return _(DEFAULT_SAFE_INTERNAL_ERROR_MESSAGE, lang=current_language() or 'en')


def text(settings, key, language=None):
    """Resolve the enabled conversation language, then the configured default."""
    if key not in MESSAGE_KEYS:
        raise ValueError('Unknown Bot Settings message key')
    default = validate_chatbot_languages(settings)
    enabled = {row.language for row in settings.chatbot_languages if row.enabled}
    for code in dict.fromkeys((language or current_language(), default)):
        if code in enabled:
            row = next((row for row in settings.message_translations or []
                if row.language == code and row.message_key == key), None)
            if row and (row.message or '').strip():
                return row.message
    return ''


def validate(settings, *, operational=False):
    from messaging.communication.doctype.communication_template.communication_template import validate_template_syntax
    default = validate_chatbot_languages(settings)
    enabled = {row.language for row in settings.chatbot_languages if row.enabled}
    if operational and not default:
        frappe.throw(_('Configure at least one Chatbot Language.'))
    previous = settings.get_doc_before_save() or frappe.get_single('Bot Settings')
    prior_rows = {row.name: row for row in previous.message_translations or []}
    configured = {row.language for row in settings.chatbot_languages}
    seen = set()
    for row in settings.message_translations or []:
        identity = (row.message_key, row.language)
        if identity in seen:
            frappe.throw(_('Each message must have a unique translation per language.'))
        seen.add(identity)
        if row.message_key not in MESSAGE_KEYS:
            frappe.throw(_('The translation must reference a configured message.'))
        old = prior_rows.get(row.name)
        retained = old and all(old.get(field) == row.get(field) for field in ('message_key', 'language', 'message'))
        if row.language not in configured or (row.language not in enabled and not retained):
            frappe.throw(_('Translations can only be edited for enabled Chatbot Languages.'))
        value = row.message or ''
        if len(value.encode('utf-8')) > 65535 or (row.message_key == 'attempts_exhausted_message' and len(value) > 4000):
            frappe.throw(_('Localized message exceeds its maximum length.'))
        if value.strip():
            if row.message_key in SAFE_MESSAGES:
                validate_safe_error_message(value, field_label=_(MESSAGE_LABELS[row.message_key]))
            else:
                validate_template_syntax(value, allowed_variables=set())
    required = required_message_keys(settings, operational=operational)
    missing = []
    for key in sorted(required):
        if not text(settings, key, default).strip():
            section = settings.meta.get_field(MESSAGE_SECTIONS[key]).label
            missing.append(_('Language {0}: {1} / {2} is required.').format(default, _(section), _(MESSAGE_LABELS[key])))
    if missing:
        frappe.throw('\n'.join(missing))


def courtesy_message_required() -> bool:
    """Return whether any Communication Channel requires the shared Quota Courtesy Message."""
    if not frappe.db.table_exists("Communication Channel") or not frappe.db.has_column("Communication Channel", "quota_courtesy_enabled"):
        return False
    return bool(frappe.db.exists("Communication Channel", {"quotas_enabled": 1, "quota_courtesy_enabled": 1}))


def required_message_keys(settings, *, operational=False):
    """Return the same conditional requirements used by validation and Desk hints."""
    required = set()
    if operational or any(row.enabled for row in settings.chatbot_languages or []):
        required.update(('default_internal_error_message', 'informative_message', 'attempts_exhausted_message', 'human_disabled_message'))
    if courtesy_message_required():
        required.add('quota_courtesy_message')
    if settings.human_attention_enabled:
        required.update(('human_disabled_message', 'human_active_message', 'human_completed_message'))
    return required
