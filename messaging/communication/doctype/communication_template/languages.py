"""Resolve provider language codes to independent Frappe Language records."""
import re

import frappe
from frappe import _

from messaging.common.primitives import enabled_language

CONVENTIONAL_LOCALE = re.compile(r'[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})+')


def require_language(code):
    if not enabled_language(code):
        frappe.throw(_('Select an enabled native Language.'))


def declared_language(definition, code):
    catalog = definition.get('template_languages', {})
    entry = catalog.get(code) if isinstance(code, str) else None
    if not entry:
        frappe.throw(_('Select a language declared by this communication provider.'))
    return entry


def _bot_default():
    # Messaging has a single site-wide Bot Settings document and no per-account override.
    from messaging.chatbot.doctype.bot_settings.language import get_bot_settings

    rows = [row for row in (get_bot_settings().get('chatbot_languages') or []) if row.is_default]
    if len(rows) == 1 and rows[0].enabled:
        return enabled_language(rows[0].language)
    return None


def resolve_language(definition, code):
    """Return a native code and its origin without creating Language records."""
    entry = declared_language(definition, code)
    language = enabled_language(entry['language']) or enabled_language(code)
    if not language and CONVENTIONAL_LOCALE.fullmatch(code):
        alternate = code.replace('_', '-') if '_' in code else code.replace('-', '_')
        row = frappe.db.get_value('Language', alternate, ['name', 'language_code', 'enabled'], as_dict=True)
        if row and row.enabled == 1 and row.name == row.language_code and row.name.casefold() == alternate.casefold():
            language = row.name
    if language:
        return language, 'Automatic'
    language = _bot_default()
    return (language, 'Provisional') if language else (None, 'Unassigned')


def set_language_mapping(doc, previous, definition, *, synchronizing=False):
    """Compute the origin server-side and keep persisted mappings across reads."""
    if not definition.get('template_languages'):
        if doc.provider_language:
            frappe.throw(_('This provider uses native template languages.'))
        doc.language_mapping = None
        return

    declared_language(definition, doc.provider_language)
    if previous and (previous.language or '') == (doc.language or ''):
        if not doc.language or doc.language_mapping != 'Manual':
            doc.language_mapping = previous.language_mapping
    elif previous:
        doc.language_mapping = 'Manual' if doc.language else 'Unassigned'
    else:
        language, origin = resolve_language(definition, doc.provider_language)
        if synchronizing or not doc.language or doc.language_mapping == 'Unassigned' or (
            doc.language, doc.language_mapping
        ) == (language, origin):
            doc.language, doc.language_mapping = language, origin
        else:
            doc.language_mapping = 'Manual'
    if (doc.language or doc.status == 'Active') and not (synchronizing and previous):
        require_language(doc.language)
