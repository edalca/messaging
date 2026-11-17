"""Unit tests for per-user Conversation Preference storage, permissions, and timezone boundaries."""
from datetime import datetime, timedelta
import unittest

import frappe

from messaging.common.primitives import utcnow
from messaging.conversation.doctype.conversation_preference.conversation_preference import (
	_PREFERENCE_WRITE_GUARD,
	clear_visible_history,
	get_preference,
	preference_map,
	preference_permission,
	preference_query,
	set_archived,
	set_pinned,
	site_naive_to_utc_naive,
	utc_to_site_naive,
)
from messaging.emulator.doctype.emulator_contact import test_emulator_contact as fixtures


class TestConversationPreference(unittest.TestCase):
	def setUp(self):
		self.fixture = fixtures.TestEmulatorContact(methodName="runTest")
		self.addCleanup(self.fixture.doCleanups)
		self.fixture.setUp()
		self.addCleanup(self.fixture.tearDown)
		self.users = []
		self.addCleanup(self._cleanup_preferences_and_users)

	def _cleanup_preferences_and_users(self):
		f = self.fixture
		frappe.db.delete("Conversation Preference", {"account": ["in", [f.a, f.b]]})
		frappe.db.delete("Conversation Preference", {"sender": f.sender})
		for user in self.users:
			frappe.db.delete("Conversation Preference", {"user": user})
			if frappe.db.exists("User", user):
				frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _create_user(self, tag):
		email = f"conv-pref-{tag}-{frappe.generate_hash(length=6)}@example.com"
		doc = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": f"Pref {tag}",
			"enabled": 1,
			"roles": [{"role": "System Manager"}],
		})
		doc.insert(ignore_permissions=True)
		self.users.append(email)
		frappe.db.commit()
		return email

	def test_direct_writes_require_guard_and_scope_permissions(self):
		f = self.fixture
		user_a = self._create_user("a")
		user_b = self._create_user("b")

		direct = frappe.get_doc({
			"doctype": "Conversation Preference",
			"user": user_a,
			"channel": "emulator",
			"account": f.a,
			"sender": f.sender,
		})
		with self.assertRaises(frappe.PermissionError):
			direct.insert(ignore_permissions=True)

		doc = set_pinned(user_a, "emulator", f.a, f.sender, True)
		frappe.db.commit()
		with self.assertRaises(frappe.PermissionError):
			doc.save(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			doc.delete(ignore_permissions=True)

		self.assertEqual(preference_query("Guest"), "1=0")
		self.assertIn(frappe.db.escape(user_a), preference_query(user_a))
		self.assertTrue(preference_permission(doc, "read", user_a))
		self.assertFalse(preference_permission(doc, "read", user_b))
		self.assertFalse(preference_permission(doc, "read", "Guest"))
		self.assertFalse(preference_permission(doc, "write", user_a))
		with _PREFERENCE_WRITE_GUARD():
			self.assertTrue(preference_permission(doc, "write", user_a))

	def test_identity_isolation_across_accounts_and_users(self):
		f = self.fixture
		user_a = self._create_user("iso-a")
		user_b = self._create_user("iso-b")

		set_pinned(user_a, "emulator", f.a, f.sender, True)
		set_archived(user_a, "emulator", f.b, f.sender, True)
		frappe.db.commit()

		pref_a1 = get_preference(user_a, "emulator", f.a, f.sender)
		pref_a2 = get_preference(user_a, "emulator", f.b, f.sender)
		pref_b1 = get_preference(user_b, "emulator", f.a, f.sender)

		self.assertEqual(pref_a1.is_pinned, 1)
		self.assertEqual(pref_a1.is_archived, 0)
		self.assertIsNotNone(pref_a1.pinned_at)

		self.assertEqual(pref_a2.is_pinned, 0)
		self.assertEqual(pref_a2.is_archived, 1)
		self.assertIsNotNone(pref_a2.archived_at)

		self.assertEqual(pref_b1.is_pinned, 0)
		self.assertEqual(pref_b1.is_archived, 0)

		bulk = preference_map(user_a, "emulator", [f.a, f.b], [f.sender])
		self.assertEqual(set(bulk.keys()), {(f.a, f.sender), (f.b, f.sender)})

		set_pinned(user_a, "emulator", f.a, f.sender, False)
		frappe.db.commit()
		self.assertEqual(get_preference(user_a, "emulator", f.a, f.sender).is_pinned, 0)
		self.assertIsNone(get_preference(user_a, "emulator", f.a, f.sender).pinned_at)

	def test_clear_visible_history_monotonic_and_timezone_safe(self):
		f = self.fixture
		user_a = self._create_user("clr")
		future_utc = (utcnow() + timedelta(minutes=5)).replace(microsecond=0)
		future_site = utc_to_site_naive(future_utc)
		self.assertEqual(site_naive_to_utc_naive(future_site), future_utc)

		sent = f.message(text="History item")
		msg_name = frappe.db.get_value("Chat Message", {"turn": sent["turn"]}, "name")
		frappe.db.set_value("Chat Message", msg_name, "sent_at", future_utc)
		frappe.db.commit()

		doc = clear_visible_history(user_a, "emulator", f.a, f.sender)
		frappe.db.commit()
		self.assertGreaterEqual(frappe.utils.get_datetime(doc.cleared_before), future_utc)

		# Advancing clock backwards in DB check must never lower cleared_before.
		earlier = future_utc - timedelta(minutes=10)
		frappe.db.set_value("Chat Message", msg_name, "sent_at", earlier)
		frappe.db.commit()
		doc2 = clear_visible_history(user_a, "emulator", f.a, f.sender)
		frappe.db.commit()
		self.assertGreaterEqual(
			frappe.utils.get_datetime(doc2.cleared_before),
			frappe.utils.get_datetime(doc.cleared_before),
		)
		# Source record remains untouched.
		self.assertTrue(frappe.db.exists("Chat Message", msg_name))
