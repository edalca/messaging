"""Explicit user grants and concurrent capacity for a neutral channel/account scope."""
import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.primitives import identity_key

IDENTITY_FIELDS = ("user", "channel", "account")


class ChatAgent(Document):
	def onload(self):
		user_label = frappe.db.get_value("User", self.user, "full_name") or self.user
		channel_label = frappe.db.get_value("Communication Channel", self.channel, "channel_name") or self.channel
		account_label = frappe.db.get_value("Communication Channel Account", self.account, "account_name") or self.account
		parts = [part for part in (user_label, channel_label, account_label) if part]
		self.set_onload(
			"scope_display",
			{
				"title": " · ".join(parts) or self.name,
				"user_label": user_label,
				"channel_label": channel_label,
				"account_label": account_label,
			},
		)

	def _validate_immutable_identity(self):
		if self.is_new() or self.flags.in_insert:
			return
		persisted = frappe.db.get_value(
			"Chat Agent",
			self.name,
			[*IDENTITY_FIELDS, "scope_key"],
			as_dict=True,
		)
		if not persisted:
			return
		if any(self.get(field) != persisted.get(field) for field in IDENTITY_FIELDS):
			frappe.throw(
				_("Agent identity (User, Channel, Account) is immutable after creation."),
				frappe.ValidationError,
			)
		if self.scope_key != persisted.scope_key:
			frappe.throw(
				_("Scope Key is managed by the server and cannot be changed."),
				frappe.ValidationError,
			)

	def before_validate(self):
		self._validate_immutable_identity()

	def db_update(self):
		self._validate_immutable_identity()
		return super().db_update()

	def validate(self):
		self._validate_immutable_identity()
		if not frappe.db.get_value('User', self.user, 'enabled') or 'Messaging Agent' not in frappe.get_roles(self.user):
			frappe.throw(_("An enabled user with the Messaging Agent role is required."))
		if int(self.capacity or 0) < 1:
			frappe.throw(_("Agent capacity must be positive."))
		if self.is_new() or self.flags.in_insert:
			self.scope_key = identity_key(self.user, self.channel, self.account)

	def on_update(self):
		frappe.publish_realtime(
			"messaging_conversation_update",
			{"channel": self.channel, "account": self.account},
			after_commit=True,
		)

	def db_set(self, fieldname, value=None, update_modified=True, notify=False, commit=False):
		fields = fieldname.keys() if isinstance(fieldname, dict) else (fieldname,)
		if any(field in (*IDENTITY_FIELDS, "scope_key") for field in fields):
			frappe.throw(
				_("Agent identity (User, Channel, Account) is immutable after creation."),
				frappe.ValidationError,
			)
		return super().db_set(
			fieldname,
			value=value,
			update_modified=update_modified,
			notify=notify,
			commit=commit,
		)


def setup_roles():
	if not frappe.db.exists('Role', 'Messaging Agent'):
		frappe.get_doc(dict(doctype='Role', role_name='Messaging Agent', desk_access=1)).insert(ignore_permissions=True)


def on_doctype_update():
	frappe.db.add_index("Chat Agent", ["user", "channel", "account", "enabled"], "agent_scope")
