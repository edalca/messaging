"""Child rows follow the parent provider's origin. Ordinary writes cannot edit an internal definition."""
import frappe
from frappe import _
from frappe.model.document import Document

from messaging.chatbot.doctype.ai_provider.ai_provider import provider_sync


class AIProviderParameter(Document):
    def validate(self):
        _protect_internal_parent(self.parent)

    def on_trash(self):
        _protect_internal_parent(self.parent)


def _protect_internal_parent(parent):
    if provider_sync.active() or not parent:
        return
    if frappe.db.get_value('AI Provider', parent, 'provider_type') == 'Internal':
        frappe.throw(_('Internal AI providers are registered by their owning application.'), frappe.PermissionError)
