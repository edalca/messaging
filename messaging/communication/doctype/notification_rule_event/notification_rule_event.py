"""Event conditions can only be persisted through their immutable owning rule."""
import frappe
from frappe import _
from frappe.model.document import Document


class NotificationRuleEvent(Document):
    def validate(self):
        raise frappe.PermissionError(_('Save event conditions through their owning notification rule.'))

    def on_trash(self):
        self.validate()

    def before_rename(self, *args, **kwargs):
        self.validate()
