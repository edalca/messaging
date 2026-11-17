"""Focused tests for Chat Agent creation, identity immutability, availability edits, and scope isolation."""
import json
import unittest
from unittest.mock import patch

import frappe
from frappe.client import set_value as client_set_value

from messaging.common.primitives import identity_key
from messaging.communication.doctype.communication_channel_account.test_communication_channel_account import (
	registered_providers,
)
from messaging.communication.doctype.communication_provider.communication_provider import (
	get_provider_states,
	set_provider_enabled,
	_lock_provider_states,
	_write_provider_states,
)
from messaging.communication.doctype.communication_provider.test_communication_provider import (
	sync_communication_providers,
)
from messaging.conversation.doctype.chat_session import human, inbox, notifications
from messaging.presets.communication_providers.emulr import definition as emulator_definition


class TestChatAgent(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.prefix = "agt-" + frappe.generate_hash(length=6)
		self.users = []
		self.agents = []
		self.contacts = []
		self.sessions = []
		self.accounts = []
		self.provider_states_before = None

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for session_name in reversed(self.sessions):
			frappe.db.delete("Bot Dispatch", {"session": session_name})
			frappe.db.delete("Chat Message", {"session": session_name})
			frappe.db.delete("Chat Turn", {"session": session_name})
			frappe.db.delete("Notification Log", {"document_type": "Chat Session", "document_name": session_name})
			frappe.db.delete("Chat Session", {"name": session_name})
		for contact_name in reversed(self.contacts):
			frappe.db.delete("Chat Contact", {"name": contact_name})
		for agent_name in reversed(self.agents):
			if frappe.db.exists("Chat Agent", agent_name):
				frappe.delete_doc("Chat Agent", agent_name, ignore_permissions=True)
		for account_name in reversed(self.accounts):
			if frappe.db.exists("Communication Channel Account", account_name):
				frappe.delete_doc("Communication Channel Account", account_name, ignore_permissions=True)
		for email in reversed(self.users):
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True)
		if self.provider_states_before is not None:
			_lock_provider_states()
			_write_provider_states(self.provider_states_before)
		frappe.db.commit()

	def _make_user(self, *, enabled=1, roles=("Messaging Agent",)):
		email = f"{self.prefix}-{len(self.users)}@example.invalid"
		with patch("frappe.enqueue"):
			user = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "Test Agent",
					"enabled": enabled,
					"send_welcome_email": 0,
					"user_type": "System User",
					"roles": [{"role": role} for role in roles],
				}
			).insert(ignore_permissions=True)
		self.users.append(user.name)
		frappe.db.commit()
		return user.name

	def _make_agent(self, user, channel="emulator", account=None, *, enabled=1, available=1, capacity=2, scope_key=None):
		payload = {
			"doctype": "Chat Agent",
			"user": user,
			"channel": channel,
			"account": account or f"{self.prefix}-acc-1",
			"enabled": enabled,
			"available": available,
			"capacity": capacity,
		}
		if scope_key is not None:
			payload["scope_key"] = scope_key
		doc = frappe.get_doc(payload).insert()
		self.agents.append(doc.name)
		frappe.db.commit()
		return doc

	def test_creation_metadata_validation_scope_key_and_uniqueness(self):
		meta = frappe.get_meta("Chat Agent")
		for fieldname in ("user", "channel", "account"):
			df = meta.get_field(fieldname)
			self.assertEqual(df.set_only_once, 1)
			self.assertFalse(df.hidden)
			self.assertFalse(df.read_only)
		scope_df = meta.get_field("scope_key")
		self.assertEqual(scope_df.read_only, 1)
		self.assertEqual(scope_df.unique, 1)

		no_role_user = self._make_user(enabled=1, roles=())
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Chat Agent",
					"user": no_role_user,
					"channel": "emulator",
					"account": f"{self.prefix}-acc-1",
					"capacity": 1,
				}
			).insert()

		disabled_user = self._make_user(enabled=0, roles=("Messaging Agent",))
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Chat Agent",
					"user": disabled_user,
					"channel": "emulator",
					"account": f"{self.prefix}-acc-1",
					"capacity": 1,
				}
			).insert()

		valid_user = self._make_user()
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Chat Agent",
					"user": valid_user,
					"channel": "emulator",
					"account": f"{self.prefix}-acc-1",
					"capacity": 0,
				}
			).insert()

		account = f"{self.prefix}-acc-1"
		agent = self._make_agent(valid_user, "emulator", account, scope_key="forged-manual-scope-key")
		expected_scope = identity_key(valid_user, "emulator", account)
		self.assertEqual(agent.scope_key, expected_scope)

		# Duplicate scope is prevented.
		with self.assertRaises((frappe.DuplicateEntryError, frappe.UniqueValidationError)):
			frappe.get_doc(
				{
					"doctype": "Chat Agent",
					"user": valid_user,
					"channel": "emulator",
					"account": account,
					"enabled": 1,
					"available": 1,
					"capacity": 1,
				}
			).insert()

	def test_existing_document_identity_immutable_and_availability_editable(self):
		user_one = self._make_user()
		user_two = self._make_user()
		account_one = f"{self.prefix}-acc-1"
		account_two = f"{self.prefix}-acc-2"
		agent = self._make_agent(user_one, "emulator", account_one, enabled=1, available=1, capacity=2)
		original_scope = agent.scope_key

		# Availability and capacity can be updated without altering identity or scope_key.
		loaded = frappe.get_doc("Chat Agent", agent.name)
		loaded.enabled = 0
		loaded.available = 0
		loaded.capacity = 4
		loaded.save()
		loaded.reload()
		self.assertEqual((loaded.enabled, loaded.available, loaded.capacity), (0, 0, 4))
		self.assertEqual((loaded.user, loaded.channel, loaded.account, loaded.scope_key), (user_one, "emulator", account_one, original_scope))

		# Changing user, channel, account, or scope_key is rejected on ordinary save, even as Administrator.
		for field, new_value in (
			("user", user_two),
			("channel", "other-channel"),
			("account", account_two),
			("scope_key", identity_key(user_two, "emulator", account_one)),
		):
			doc = frappe.get_doc("Chat Agent", agent.name)
			doc.set(field, new_value)
			with self.assertRaises(frappe.ValidationError):
				doc.save()

			# Even ignore_validate=True cannot bypass before_save protection.
			doc_bypass = frappe.get_doc("Chat Agent", agent.name)
			doc_bypass.set(field, new_value)
			doc_bypass.flags.ignore_validate = True
			with self.assertRaises(frappe.ValidationError):
				doc_bypass.save()

			# REST/client set_value path is rejected.
			with self.assertRaises(frappe.ValidationError):
				client_set_value("Chat Agent", agent.name, field, new_value)

			# db_set on identity fields is rejected.
			doc_db = frappe.get_doc("Chat Agent", agent.name)
			with self.assertRaises(frappe.ValidationError):
				doc_db.db_set(field, new_value)

		loaded.reload()
		self.assertEqual(
			(loaded.user, loaded.channel, loaded.account, loaded.scope_key),
			(user_one, "emulator", account_one, original_scope),
		)

		# A separate scope for the same user creates an independent Chat Agent record.
		second_scope = self._make_agent(user_one, "emulator", account_two, capacity=1)
		self.assertNotEqual(second_scope.name, agent.name)
		self.assertEqual(second_scope.scope_key, identity_key(user_one, "emulator", account_two))

	def test_regression_notifications_capacity_take_and_my_conversations_scope(self):
		self.provider_states_before = get_provider_states()
		sync_communication_providers()
		set_provider_enabled("EMULR", True)
		if not frappe.db.exists("Communication Channel", "emulator"):
			frappe.get_doc(
				{
					"doctype": "Communication Channel",
					"provider": "EMULR",
					"channel_name": "Channel Emulator",
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		with registered_providers([emulator_definition()]):
			account_doc = frappe.get_doc(
				{
					"doctype": "Communication Channel Account",
					"channel": "emulator",
					"account_id": f"{self.prefix}-acc-reg",
					"account_name": "Test Regression Emulator Account",
					"enabled": 1,
					"parameters": [{"parameter_key": "transport_outcome", "value": json.dumps("Accepted")}],
				}
			).insert(ignore_permissions=True)
			self.accounts.append(account_doc.name)
			frappe.db.commit()

			agent_one = self._make_user()
			agent_two = self._make_user()
			account = account_doc.name
			grant_one = self._make_agent(agent_one, "emulator", account, enabled=1, available=1, capacity=1)
			self._make_agent(agent_two, "emulator", account, enabled=1, available=0, capacity=2)

			recipients = notifications.eligible_agent_recipients("emulator", account)
			self.assertEqual([r["user"] for r in recipients], [agent_one])

			# Update availability on second agent without altering identity; now both are eligible.
			grant_two_name = frappe.db.get_value(
				"Chat Agent", {"user": agent_two, "channel": "emulator", "account": account}, "name"
			)
			grant_two = frappe.get_doc("Chat Agent", grant_two_name)
			grant_two.available = 1
			grant_two.save()
			recipients_after = notifications.eligible_agent_recipients("emulator", account)
			self.assertEqual({r["user"] for r in recipients_after}, {agent_one, agent_two})

			# Create two queued sessions in this scope.
			s1 = self._make_queued_session("emulator", account, f"{self.prefix}-s1")
			s2 = self._make_queued_session("emulator", account, f"{self.prefix}-s2")

			# agent_one (capacity=1) takes s1, then cannot take s2 due to capacity.
			frappe.set_user(agent_one)
			res1 = human.act(s1, "Take")
			self.assertEqual(res1["status"], "Human Active")
			self.assertEqual(res1["assigned_agent"], agent_one)
			with self.assertRaises(frappe.ValidationError):
				human.act(s2, "Take")

			# My Conversations only returns s1 for agent_one, and agent_two cannot view s1.
			mine_one = inbox.conversations(mine=1, status="")
			self.assertEqual([row["name"] for row in mine_one], [s1])

			frappe.set_user(agent_two)
			mine_two = inbox.conversations(mine=1, status="")
			self.assertEqual([row["name"] for row in mine_two], [])
			with self.assertRaises(frappe.PermissionError):
				inbox.history(s1, mine_only=1)

			# Verify existing grant_one still keeps exact identity and scope_key.
			frappe.set_user("Administrator")
			grant_one_doc = frappe.get_doc("Chat Agent", grant_one.name)
			self.assertEqual(grant_one_doc.user, agent_one)
			self.assertEqual(grant_one_doc.channel, "emulator")
			self.assertEqual(grant_one_doc.account, account)
			self.assertEqual(grant_one_doc.scope_key, identity_key(agent_one, "emulator", account))

	def _make_queued_session(self, channel, account, sender):
		contact = frappe.get_doc(
			{
				"doctype": "Chat Contact",
				"channel": channel,
				"account": account,
				"external_sender": sender,
				"identity_key": identity_key(channel, account, sender),
				"registration_status": "Complete",
				"parameter_values": '{"name": "Regression Contact"}',
			}
		).insert(ignore_permissions=True)
		self.contacts.append(contact.name)
		session = frappe.get_doc(
			{
				"doctype": "Chat Session",
				"contact": contact.name,
				"channel": channel,
				"account": account,
				"active_key": contact.identity_key,
				"status": "Human Queued",
				"human_requested_at": frappe.utils.now_datetime(),
				"opened_at": frappe.utils.now_datetime(),
			}
		).insert(ignore_permissions=True)
		self.sessions.append(session.name)
		contact.active_session = session.name
		contact.save(ignore_permissions=True)
		frappe.db.commit()
		return session.name

	def test_readable_series_onload_display_and_legacy_coexistence(self):
		legacy = frappe.get_all(
			"Chat Agent",
			filters={"name": ["not like", "AGENT-%"]},
			fields=["name", "user", "channel", "account", "scope_key"],
			limit=1,
		)
		user = self._make_user()
		first = self._make_agent(user, "emulator", f"{self.prefix}-ser-1")
		second = self._make_agent(user, "emulator", f"{self.prefix}-ser-2")
		self.assertRegex(first.name, r"^AGENT-[0-9]{5}$")
		self.assertRegex(second.name, r"^AGENT-[0-9]{5}$")
		self.assertNotEqual(first.name, second.name)
		loaded = frappe.get_doc("Chat Agent", first.name)
		loaded.run_method("onload")
		display = loaded.get_onload("scope_display")
		self.assertTrue(display and display["title"])
		if legacy:
			old = frappe.get_doc("Chat Agent", legacy[0].name)
			self.assertEqual(old.name, legacy[0].name)
			self.assertEqual(old.scope_key, legacy[0].scope_key)
			old.run_method("onload")
			self.assertTrue(old.get_onload("scope_display")["title"])
