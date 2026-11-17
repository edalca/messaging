import frappe
from frappe import _
from frappe.model.document import Document
from messaging.common.configuration import EncryptedConfigurationValue


class AIConfigurationValue(EncryptedConfigurationValue, Document):
    def validate(self):
        frappe.throw(_('Save AI values through their owning configuration.'), frappe.PermissionError)

    def on_trash(self):
        frappe.throw(_('Save AI values through their owning configuration.'), frappe.PermissionError)

    def before_rename(self, *args, **kwargs):
        frappe.throw(_('Provider value ownership is immutable.'), frappe.PermissionError)
