"""Immutable prepared notifications with private, bounded operational transitions."""
from frappe.model.naming import make_autoname
from messaging.common.write_guards import InternalWriteGuard

import frappe
from frappe import _
from frappe.model.document import Document

internal_write = InternalWriteGuard('notification_internal_write')
SNAPSHOT = ('notification_rule', 'source_application', 'source_doctype', 'source_name', 'document_event',
	'condition_field', 'condition_value', 'occurrence', 'occurrence_key', 'execution_log', 'account', 'channel', 'provider', 'account_id',
	'recipient', 'effective_recipient', 'emulator_contact', 'identity_key', 'content_type', 'template_reference', 'provider_template_reference', 'template_revision', 'variables_json', 'prepared_text',
	'event_at', 'scheduled_at', 'delivery_mode', 'delivery_fingerprint')
TRANSITIONS = {
	'Pending': {'Processing', 'Cancelled'}, 'Deferred': {'Processing', 'Cancelled'},
	'Processing': {'Deferred', 'Accepted', 'Rejected', 'Unknown', 'Cancelled'},
	'Unknown': {'Accepted', 'Rejected'}, 'Accepted': set(), 'Rejected': set(), 'Cancelled': set(),
}




class NotificationQueue(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("NTF-.######")

	def before_validate(self):
		if not internal_write.active():
			frappe.throw(_("Notification snapshots and processing state are managed internally."), frappe.PermissionError)

	def validate(self):
		if self.is_new():
			if self.status != 'Pending' or any(not self.get(f) for f in ('occurrence_key', 'execution_log', 'account', 'effective_recipient', 'delivery_mode', 'delivery_fingerprint', 'scheduled_at')):
				frappe.throw(_("Invalid prepared notification."))
			return
		previous = self.get_doc_before_save()
		if any(self.has_value_changed(f) for f in SNAPSHOT):
			frappe.throw(_("Prepared notification snapshots are immutable."))
		if self.status != previous.status and self.status not in TRANSITIONS.get(previous.status, set()):
			frappe.throw(_("Invalid notification state transition."))

	def on_trash(self):
		frappe.throw(_("Notification records cannot be deleted through Desk."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Notification identity is immutable."))


def on_doctype_update():
	frappe.db.add_index('Notification Queue', ['status', 'next_attempt_at', 'scheduled_at'], 'notification_due')
	frappe.db.add_index('Notification Queue', ['status', 'last_attempt_at'], 'notification_claim_recovery')
	frappe.db.add_index('Notification Queue', ['channel', 'account', 'effective_recipient', 'handoff_at'], 'notification_effective_history')
