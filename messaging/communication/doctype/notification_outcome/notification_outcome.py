"""Child configuration is saved only through its owning Notification Rule."""
import frappe
from frappe import _
from frappe.model.document import Document


class NotificationOutcome(Document):
    def validate(self):
        raise frappe.PermissionError(_('Save Process configuration through its owning notification rule.'))

    def on_trash(self):
        self.validate()

    def before_rename(self, *args, **kwargs):
        self.validate()
