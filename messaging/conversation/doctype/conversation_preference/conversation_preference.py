"""Per-user presentation preferences (pin, archive, visible cutoff) per conversation identity."""
from datetime import timezone
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.locking import document_savepoint
from messaging.common.primitives import identity_key, utcnow
from messaging.common.write_guards import InternalWriteGuard

_PREFERENCE_WRITE_GUARD = InternalWriteGuard("conversation_preference_write")


class ConversationPreference(Document):
	"""Presentation metadata scoped to (user, channel, account, sender). Never stores session state."""

	def validate(self):
		self.user = (self.user or "").strip()
		self.channel = (self.channel or "").strip()
		self.account = (self.account or "").strip()
		self.sender = (self.sender or "").strip()
		if not (self.user and self.channel and self.account and self.sender):
			frappe.throw(_("User, channel, account, and sender are required."))
		self.preference_key = preference_key(self.user, self.channel, self.account, self.sender)
		self.is_pinned = 1 if frappe.utils.cint(self.is_pinned) else 0
		if not self.is_pinned:
			self.pinned_at = None
		elif not self.pinned_at:
			self.pinned_at = utcnow()
		self.is_archived = 1 if frappe.utils.cint(self.is_archived) else 0
		if not self.is_archived:
			self.archived_at = None
		elif not self.archived_at:
			self.archived_at = utcnow()

	def before_insert(self):
		_require_guard()

	def before_save(self):
		_require_guard()

	def on_trash(self):
		_require_guard()


def on_doctype_update():
	frappe.db.add_index(
		"Conversation Preference",
		["user", "channel", "account", "sender"],
		index_name="idx_pref_user_channel_account_sender",
	)


def _require_guard():
	if not _PREFERENCE_WRITE_GUARD.active():
		frappe.throw(
			_("Conversation preferences can only be updated through the dedicated API."),
			frappe.PermissionError,
		)


def preference_key(user, channel, account, sender):
	return identity_key(user, channel, account, sender)


def utc_to_site_naive(value):
	"""Convert a naive UTC datetime (like Chat Message.sent_at / cleared_before) to naive site time."""
	if not value:
		return None
	dt = frappe.utils.get_datetime(value)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=timezone.utc)
	site_tz = ZoneInfo(frappe.utils.get_system_timezone())
	return dt.astimezone(site_tz).replace(tzinfo=None)


def site_naive_to_utc_naive(value):
	"""Convert a naive site-local datetime (like Notification Queue.handoff_at) to naive UTC."""
	if not value:
		return None
	dt = frappe.utils.get_datetime(value)
	if dt.tzinfo is None:
		dt = dt.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
	return dt.astimezone(timezone.utc).replace(tzinfo=None)


def get_preference(user, channel, account, sender):
	"""Return presentation preference fields for one conversation identity, or clean defaults."""
	if not (user and channel and account and sender):
		return frappe._dict(is_pinned=0, pinned_at=None, is_archived=0, archived_at=None, cleared_before=None)
	row = frappe.db.get_value(
		"Conversation Preference",
		{"preference_key": preference_key(user, channel, account, sender)},
		["is_pinned", "pinned_at", "is_archived", "archived_at", "cleared_before"],
		as_dict=True,
	)
	return row or frappe._dict(is_pinned=0, pinned_at=None, is_archived=0, archived_at=None, cleared_before=None)


def preference_map(user, channel, accounts, senders):
	"""Bulk-load presentation preferences keyed by (account, sender) for one user."""
	account_tuple = tuple(accounts or ())
	sender_tuple = tuple(senders or ())
	if not (user and channel and account_tuple and sender_tuple):
		return {}
	rows = frappe.get_all(
		"Conversation Preference",
		filters={
			"user": user,
			"channel": channel,
			"account": ["in", account_tuple],
			"sender": ["in", sender_tuple],
		},
		fields=["account", "sender", "is_pinned", "pinned_at", "is_archived", "archived_at", "cleared_before"],
		limit=0,
	)
	return {(row.account, row.sender): row for row in rows}


def _mutate_preference(user, channel, account, sender, mutator):
	"""Atomically create or update one preference row under the write guard."""
	key = preference_key(user, channel, account, sender)
	with _PREFERENCE_WRITE_GUARD():
		name = frappe.db.get_value("Conversation Preference", {"preference_key": key}, "name", for_update=True)
		if name:
			doc = frappe.get_doc("Conversation Preference", name)
			mutator(doc)
			doc.save(ignore_permissions=True)
			return doc
		try:
			with document_savepoint():
				doc = frappe.get_doc({
					"doctype": "Conversation Preference",
					"user": user,
					"channel": channel,
					"account": account,
					"sender": sender,
					"preference_key": key,
				})
				mutator(doc)
				doc.insert(ignore_permissions=True)
				return doc
		except frappe.DuplicateEntryError:
			name = frappe.db.get_value("Conversation Preference", {"preference_key": key}, "name", for_update=True)
			doc = frappe.get_doc("Conversation Preference", name)
			mutator(doc)
			doc.save(ignore_permissions=True)
			return doc


def set_pinned(user, channel, account, sender, pinned):
	pinned_flag = 1 if frappe.utils.cint(pinned) else 0

	def apply(doc):
		if pinned_flag and not frappe.utils.cint(doc.is_pinned):
			doc.is_pinned = 1
			doc.pinned_at = utcnow()
		elif not pinned_flag:
			doc.is_pinned = 0
			doc.pinned_at = None

	return _mutate_preference(user, channel, account, sender, apply)


def set_archived(user, channel, account, sender, archived):
	archived_flag = 1 if frappe.utils.cint(archived) else 0

	def apply(doc):
		if archived_flag and not frappe.utils.cint(doc.is_archived):
			doc.is_archived = 1
			doc.archived_at = utcnow()
		elif not archived_flag:
			doc.is_archived = 0
			doc.archived_at = None

	return _mutate_preference(user, channel, account, sender, apply)


def clear_visible_history(user, channel, account, sender):
	"""Advance per-user cleared_before cutoff cleanly without deleting any operational records."""
	candidates = [utcnow()]
	contact = frappe.db.get_value(
		"Chat Contact",
		{"channel": channel, "account": account, "external_sender": sender},
		"name",
	)
	if contact:
		latest_msg = frappe.db.sql(
			"""SELECT MAX(sent_at) FROM `tabChat Message`
			WHERE contact=%s AND kind IN ('Incoming', 'Automatic', 'Human')""",
			(contact,),
		)[0][0]
		if latest_msg:
			candidates.append(frappe.utils.get_datetime(latest_msg))
	latest_notif = frappe.db.sql(
		"""SELECT MAX(handoff_at) FROM `tabNotification Queue`
		WHERE channel=%s AND account=%s AND effective_recipient=%s
		AND handoff_at IS NOT NULL AND status IN ('Processing', 'Accepted', 'Rejected', 'Unknown')""",
		(channel, account, sender),
	)[0][0]
	if latest_notif:
		candidates.append(site_naive_to_utc_naive(latest_notif))

	cutoff = max(candidates)

	def apply(doc):
		existing = frappe.utils.get_datetime(doc.cleared_before) if doc.cleared_before else None
		doc.cleared_before = max(existing, cutoff) if existing else cutoff

	return _mutate_preference(user, channel, account, sender, apply)


def preference_query(user):
	if not user or user == "Guest":
		return "1=0"
	return f"`tabConversation Preference`.`user` = {frappe.db.escape(user)}"


def preference_permission(doc, ptype="read", user=None):
	current_user = user or frappe.session.user
	if not current_user or current_user == "Guest":
		return False
	if ptype in ("create", "write", "delete"):
		return _PREFERENCE_WRITE_GUARD.active()
	return getattr(doc, "user", None) == current_user
