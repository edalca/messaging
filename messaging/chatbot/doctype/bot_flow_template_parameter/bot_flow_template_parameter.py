"""Variable rows owned by Bot Flow Template."""

import frappe
from frappe import _
from frappe.model.document import Document


class BotFlowTemplateParameter(Document):
	def validate(self):
		frappe.throw(_("Save template parameters through their owning bot flow template."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Save template parameters through their owning bot flow template."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Template parameter ownership is immutable."), frappe.PermissionError)
