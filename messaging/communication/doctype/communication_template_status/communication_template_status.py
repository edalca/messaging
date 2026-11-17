"""Scoped external template status. Only the provider status contract may write these rows."""

import frappe
from frappe import _
from frappe.model.document import Document


class CommunicationTemplateStatus(Document):
	def validate(self):
		frappe.throw(_("Template status is updated by the provider contract."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Template status is updated by the provider contract."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Template status ownership is immutable."), frappe.PermissionError)
