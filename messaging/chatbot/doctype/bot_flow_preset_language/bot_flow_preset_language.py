"""Localized preset contributions can be written only through their parent."""
import inspect

import frappe
from frappe import _
from frappe.model.document import Document
from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import (
    _MUTATION_PERMISSIONS, _deny_manual_write, _persist, _provenance, _require_preset_write,
    canonical, configuration_hash, record_language,
)
from messaging.security import permissions as ownership


class BotFlowPresetLanguage(Document):
    def has_permission(self, permtype='read', **kwargs):
        if permtype in _MUTATION_PERMISSIONS:
            return False
        return super().has_permission(permtype, **kwargs)

    def db_insert(self, *args, **kwargs):
        _require_preset_write(self)
        return super().db_insert(*args, **kwargs)

    def db_update(self, *args, **kwargs):
        _require_preset_write(self)
        return super().db_update(*args, **kwargs)

    before_validate = _deny_manual_write
    validate = _deny_manual_write
    db_set = _deny_manual_write
    before_change = _deny_manual_write
    delete = _deny_manual_write
    on_trash = _deny_manual_write
    before_rename = _deny_manual_write


def add_language(*, preset, language, semantic_description, menu_label=None, menu_description=None,
        internal_error_message=None, input_initial_message=None, input_continuation_message=None,
        parameters=None, outcomes=None):
    """Stage one localized contribution. Synchronization saves it through the parent."""
    frame = inspect.currentframe()
    try:
        app, method = _provenance(frame.f_back)
    finally:
        del frame
    data = dict(semantic_description=semantic_description)
    for key, value in (
        ('menu_label', menu_label),
        ('menu_description', menu_description),
        ('internal_error_message', internal_error_message),
        ('input_initial_message', input_initial_message),
        ('input_continuation_message', input_continuation_message),
    ):
        if value is not None:
            data[key] = value
    if parameters:
        data['parameters'] = parameters
    if outcomes:
        data['outcomes'] = outcomes
    return record_language(preset=preset, language=language, data=data, app=app, method=method)


def apply_language(*, preset, language, data, app, method, documents=None):
    """Attach one language row to its parent. Child rows are not independent documents."""
    if documents is None:
        doc = frappe.get_doc('Bot Flow Presets', preset, for_update=True)
        before = doc.as_dict()
    else:
        if preset not in documents:
            frappe.throw(_('The main preset declaration is unavailable: {0}').format(preset))
        doc, before = documents[preset]
    if not doc.available or doc.application not in ownership.installed_applications():
        frappe.throw(_('The main preset declaration is unavailable: {0}').format(preset))
    row = next((item for item in doc.languages if item.language == language), None)
    if row and row.application != app:
        raise frappe.PermissionError(_('The preset language belongs to another application.'))
    if row is None:
        row = doc.append('languages', dict(language=language))
    row.update(dict(application=app, source_method=method, configuration=canonical(data),
        configuration_hash=configuration_hash(data), available=1))
    return _persist(doc, before) if documents is None else preset
