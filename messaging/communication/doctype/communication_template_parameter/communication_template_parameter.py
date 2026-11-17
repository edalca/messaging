"""Execution variable rows owned exclusively by Communication Template."""

import frappe
from frappe import _
from frappe.model.document import Document


class CommunicationTemplateParameter(Document):
	def validate(self):
		# Parent saves validate all template variables without calling child document hooks.
		frappe.throw(_("Save template parameters through their owning communication template."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Save template parameters through their owning communication template."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Template parameter ownership is immutable."), frappe.PermissionError)
