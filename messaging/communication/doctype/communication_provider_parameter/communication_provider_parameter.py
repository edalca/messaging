"""Provider-owned definitions can only be written with their catalog parent."""
import frappe
from frappe import _
from frappe.model.document import Document


class CommunicationProviderParameter(Document):
	def validate(self):
		# Parent saves validate all definitions without calling child document hooks.
		frappe.throw(_("Provider definitions are managed by the owning application."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Provider definitions are managed by the owning application."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Provider identity is immutable."), frappe.PermissionError)
