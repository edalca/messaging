"""Local language identification and scoped presentation over native Languages."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from dataclasses import dataclass
import re
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document

_current = ContextVar('messaging_presentation_language', default=None)
_notification = ContextVar('messaging_notification_language', default=False)


def current_language():
    return _current.get()


@contextmanager
def presentation_language(language, *, notification=None):
    token = _current.set(language)
    notification_token = _notification.set(_notification.get() if notification is None else notification)
    try:
        yield
    finally:
        _current.reset(token)
        _notification.reset(notification_token)


def validate_language(language: Any) -> str:
    """Require the exact identifier of an enabled native Language record."""
    if not isinstance(language, str) or not language or language != language.strip():
        frappe.throw(_("Each Chatbot Language must reference an enabled native Language code."))
    record = frappe.db.get_value("Language", language, ["name", "language_code", "enabled"], as_dict=True)
    if not record or record.name != language or record.language_code != language or record.enabled != 1:
        frappe.throw(_("Each Chatbot Language must reference an enabled native Language code."))
    return record.name


def validate_chatbot_languages(settings: Document, *, assign_default=False) -> str | None:
    """Validate the complete allowed-language table and return its unique default."""
    rows = settings.get("chatbot_languages") or []
    previous = settings.get_doc_before_save() if assign_default else None
    previous_default = next((r.language for r in previous.chatbot_languages if r.enabled and r.is_default), None) if previous else None
    if not rows:
        if previous_default:
            frappe.throw(_("Select another enabled default language before removing the current default."))
        return None
    seen = set()
    for row in rows:
        code = validate_language(row.language)
        if code in seen:
            frappe.throw(_("Language {0} is duplicated in Chatbot Languages.").format(code))
        seen.add(code)
    if not any(row.enabled for row in rows):
        frappe.throw(_("Enable at least one Chatbot Language."))
    defaults = [row for row in rows if row.is_default]
    active = [row for row in rows if row.enabled]
    if assign_default and not defaults and active and (
        not previous_default or (len(active) == 1 and active[0].language == previous_default)
    ):
        active[0].is_default = 1
        defaults = [active[0]]
    if len(defaults) != 1:
        frappe.throw(_("Select exactly one default Chatbot Language."))
    if not defaults[0].enabled:
        frappe.throw(_("The default Chatbot Language must be enabled."))
    return defaults[0].language


def get_bot_settings(*, for_update=False, shared=False):
    """Return the Bot Settings single document."""
    if shared:
        # Administrative saves lock this stable row before writing Singles.
        frappe.db.sql("SELECT name FROM `tabDocType` WHERE name=%s LOCK IN SHARE MODE", ("Bot Settings",))
    return frappe.get_doc("Bot Settings", "Bot Settings", for_update=for_update)


def resolve_process_language() -> str:
    """Resolve the validated default from current settings for each Process invocation."""
    if _notification.get():
        return validate_language(current_language())
    settings = get_bot_settings()
    default = validate_chatbot_languages(settings)
    if not default:
        frappe.throw(_("Configure at least one Chatbot Language."))
    language = current_language()
    return language if language in {r.language for r in settings.chatbot_languages if r.enabled} else default


def configured_languages(*, lock=False):
    if lock:
        from messaging.common.locking import lock_document_types
        lock_document_types('Bot Settings')
    settings = get_bot_settings()
    default = validate_chatbot_languages(settings)
    if not default:
        raise frappe.ValidationError(_('Configure at least one active language and a default language in Bot Settings.'))
    return [r.language for r in settings.chatbot_languages if r.enabled], default


MIN_LANGUAGE_CONFIDENCE = 0.18
MIN_LANGUAGE_MARGIN = 0.10
MIN_LANGUAGE_RATIO = 2.5


@dataclass(frozen=True, slots=True)
class LanguageDetection:
    language: str | None = None
    confidence: float = 0.0
    runner_up: float = 0.0
    reliable: bool = False


@lru_cache(maxsize=1)
def _detector():
    from lingua import LanguageDetectorBuilder
    # Lingua's immutable detector and its lazily loaded models are thread-safe.
    return LanguageDetectorBuilder.from_all_languages().build()


def detect_language(text):
    """Return evidence only for sufficiently long, unambiguous local text.

    Models cover all supported languages, independently of administrative settings.
    Evidence is transient; subscriber text is never cached or logged here.
    """
    words = re.findall(r'[^\W\d_]+', (text or '')[:4000].lower())
    unsegmented = bool(re.search(r'[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0e00-\u0e7f]', ' '.join(words)))
    if (len(words) < 3 and not unsegmented) or sum(map(len, words)) < 12:
        return LanguageDetection()
    candidates = _detector().compute_language_confidence_values(' '.join(words))
    if not candidates:
        return LanguageDetection()
    first = candidates[0]
    second = candidates[1].value if len(candidates) > 1 else 0.0
    reliable = (first.value >= MIN_LANGUAGE_CONFIDENCE
        and first.value - second >= MIN_LANGUAGE_MARGIN
        and first.value >= MIN_LANGUAGE_RATIO * second)
    return LanguageDetection(first.language.iso_code_639_1.name.lower(), first.value, second, reliable)


def contact_language(contact, text, settings, *, context_language=None):
    """Resolve turn language: clear enabled detection, session context, contact preference, then default."""
    default = validate_chatbot_languages(settings)
    enabled = {row.language for row in settings.chatbot_languages if row.enabled}
    if len(enabled) <= 1:
        return default
    evidence = detect_language(text)
    matching = [code for code in enabled if code.split('-')[0] == evidence.language]
    if evidence.reliable and len(matching) == 1:
        selected = matching[0]
        if contact.get('name'):
            doc = contact if hasattr(contact, 'remember_language') else frappe.get_doc('Chat Contact', contact['name'])
            doc.remember_language(selected, evidence.confidence)
            contact.preferred_language, contact.language_confidence = selected, evidence.confidence
        else:
            contact.preferred_language, contact.language_confidence = selected, evidence.confidence
        return selected
    if not evidence.reliable:
        effective_context = context_language or current_language()
        if effective_context in enabled:
            return effective_context
        preferred = (
            contact.get('preferred_language')
            if hasattr(contact, 'get')
            else getattr(contact, 'preferred_language', None)
        ) if contact else None
        if preferred in enabled:
            return preferred
    return default
