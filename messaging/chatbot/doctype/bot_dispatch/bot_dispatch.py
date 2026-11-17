from messaging.communication.doctype.communication_provider.communication_provider import advances_delivery
from messaging.common.primitives import utcnow
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.conversation.doctype.chat_message.chat_message import dispatch_content

from frappe.model.naming import make_autoname
from messaging.common.locking import contact_lock
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_contact.chat_contact import lock_contact, require_operator

PENDING_STATES = ("Reserved", "Prepared", "Held", "Sending", "Unknown")


class BotDispatch(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("DSP-.######")

	def before_save(self):
		if self.is_new():
			return
		from messaging.conversation.doctype.chat_message.chat_message import persist_dispatch_content
		persist_dispatch_content(self)

	def after_insert(self):
		if self.content:
			from messaging.conversation.doctype.chat_message.chat_message import persist_dispatch_content
			persist_dispatch_content(self)
			frappe.db.set_value('Bot Dispatch', self.name, dict(message=self.message, content=None), update_modified=False)

	def validate(self):
		contact = frappe.db.get_value("Chat Contact", self.contact, ["channel", "account"], as_dict=True)
		session = frappe.db.get_value("Chat Session", self.session, "contact")
		if not contact or (self.channel,self.account) != (contact.channel,contact.account) or session != self.contact:
			frappe.throw(_("The dispatch does not match the conversation identity."))
		if self.message_type == 'Human':
			if self.quota_kind != 'Human' or not self.agent or self.turn:
				frappe.throw(_("Invalid human dispatch."))
		elif not _is_human_completion(self, frappe.get_doc('Chat Session', self.session)):
			turn = frappe.db.get_value("Chat Turn", self.turn, ["contact", "session"], as_dict=True)
			if not turn or turn.contact != self.contact or turn.session != self.session:
				frappe.throw(_("The dispatch does not match the conversation identity."))
		if self.status == "Accepted" and not self.accepted_at:
			frappe.throw(_("Accepted messages require an acceptance timestamp."))




def month_start(now, timezone_name):
	local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(timezone_name))
	return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(
		timezone.utc).replace(tzinfo=None)


def _used(contact, kind, *, account=None, since=None, cycle=None, inclusive=False):
	"""Pending reservations always occupy capacity, including those from older windows."""
	conditions = ["contact=%s", "quota_kind=%s"]
	args = [contact, kind]
	if account is not None:
		conditions.append("account=%s")
		args.append(account)
	if cycle is not None:
		conditions.append("courtesy_cycle=%s")
		args.append(cycle)
	if since is None:
		conditions.append("status != 'Rejected'")
	else:
		operator = ">=" if inclusive else ">"
		conditions.append(f"(status IN ('Reserved','Prepared','Held','Sending','Unknown') OR (status='Accepted' AND accepted_at{operator}%s))")
		args.append(since)
	return frappe.db.sql("SELECT COUNT(*) FROM `tabBot Dispatch` WHERE " + " AND ".join(conditions), args)[0][0]


def reserve(contact, session, turn, settings, *, registration=False, max_text_length=None, now=None):
	"""Caller holds the contact row and connection locks. No transport runs here."""
	from messaging.communication.doctype.communication_channel.communication_channel import get_channel_quota_policy
	now = now or utcnow()
	if not getattr(contact, "channel", None) or not getattr(contact, "account", None):
		frappe.throw(_("Contact channel and account are required for quota reservation."), frappe.ValidationError)
	policy = get_channel_quota_policy(contact.channel, settings=settings, operational=True)
	key = identity_key(turn.name, "automatic")
	existing = frappe.db.get_value("Bot Dispatch", {"outbound_key": key})
	if existing:
		doc = frappe.get_doc("Bot Dispatch", existing)
		if doc.quota_kind == 'Registration' and not registration and policy.quotas_enabled:
			if (_used(contact.name, 'Standard', account=contact.account, since=now-timedelta(hours=24)) >= policy.daily_quota_limit
				or _used(contact.name, 'Standard', account=contact.account, since=month_start(now, policy.quota_timezone), inclusive=True) >= policy.monthly_quota_limit):
				return None
			doc.quota_kind = 'Standard'
			doc.save(ignore_permissions=True)
		return doc
	kind = "Standard"
	if not policy.quotas_enabled:
		kind = "Unmetered"
	elif (registration and settings.exempt_registration_from_quotas
		  and _used(contact.name, "Registration", account=contact.account) < settings.max_registration_exempt_messages):
		kind = "Registration"
	elif (_used(contact.name, "Standard", account=contact.account, since=now-timedelta(hours=24)) >= policy.daily_quota_limit
		  or _used(contact.name, "Standard", account=contact.account, since=month_start(now, policy.quota_timezone), inclusive=True) >= policy.monthly_quota_limit):
		if (policy.quota_courtesy_enabled and _used(contact.name, "Courtesy", account=contact.account, cycle=contact.quota_courtesy_cycle)
				< policy.quota_courtesy_max_per_cycle):
			kind = "Courtesy"
		else:
			return None
	ttl_seconds = int(policy.quota_reservation_ttl_seconds or 60)
	return frappe.get_doc(dict(
		doctype="Bot Dispatch", outbound_key=key, contact=contact.name, session=session.name,
		turn=turn.name, channel=contact.channel, account=contact.account, quota_kind=kind,
		courtesy_cycle=contact.quota_courtesy_cycle, reserved_at=now,
		review_after=now+timedelta(seconds=ttl_seconds),
		max_text_length=max_text_length,
	)).insert(ignore_permissions=True)


def prepare(dispatch, content, *, functional=False):
	dispatch.content = content
	dispatch.functional_result = int(functional)
	dispatch.status = "Held" if dispatch.max_text_length and len(content) > dispatch.max_text_length else "Prepared"
	dispatch.save(ignore_permissions=True)


def envelope(dispatch_name, *, transport=False):
	"""Return the neutral transport contract; callers must be trusted backend operators."""
	doc = frappe.get_doc("Bot Dispatch", dispatch_name)
	require_operator() if transport else require_dispatch_operator(doc)
	binding = frappe.db.get_value('Communication Channel Account', doc.account, ['channel', 'account_id'], as_dict=True) or {}
	options = json.loads(doc.menu_options or '{}')
	from messaging.chatbot.doctype.bot_settings.bot_settings import get_menu_flows
	labels = {row['workflow_code']: row['label'] for row in get_menu_flows()} if options else {}
	return dict(outbound_id=doc.outbound_key, dispatch=doc.name, contact=doc.contact,
				provider=frappe.db.get_value("Communication Channel", binding.get("channel"), "provider") if binding else None, account_id=binding.get("account_id"),
				content_type="interactive" if options else "text",
				interactive=[dict(id=key, label=labels.get(code, code)) for key, code in options.items()],
				destination=frappe.db.get_value("Chat Contact", doc.contact, "external_sender"),
				channel=doc.channel, account=doc.account, content=dispatch_content(doc),
				message_type=doc.message_type, state=doc.status, review_after=str(doc.review_after),
				transport_reference=doc.transport_reference, delivery_status=doc.delivery_status, diagnostic_code=doc.diagnostic_code)


def reconcile(dispatch_name, outcome, *, reference=None, now=None, transport=False, delivery_status=None):
	"""A definitive adapter result is authoritative. Unknown never releases capacity."""
	require_operator() if transport else require_dispatch_operator(frappe.get_doc("Bot Dispatch", dispatch_name))
	if outcome not in ("Accepted", "Rejected", "Unknown"):
		frappe.throw(_("Invalid transport outcome."))
	contact_name = frappe.db.get_value("Bot Dispatch", dispatch_name, "contact")
	key = frappe.db.get_value("Chat Contact", contact_name, "identity_key")
	with contact_lock(key):
		try:
			contact = lock_contact(contact_name)
			doc = frappe.get_doc("Bot Dispatch", dispatch_name)
			if doc.status == outcome:
				from messaging.conversation.doctype.chat_session.chat_session import settle_technical_failure
				settle_technical_failure(frappe.get_doc("Chat Session", doc.session), now or utcnow())
				result = envelope(doc.name, transport=transport)
				frappe.publish_realtime("messaging_conversation_update", {"channel": doc.channel, "account": doc.account, "session": doc.session}, after_commit=True)
				frappe.db.commit()
				if outcome != "Unknown" and doc.message_type == "Automatic":
					from messaging.conversation.doctype.chat_contact.chat_contact import resume_pending_inputs
					resume_pending_inputs(contact_name)
				return result
			if doc.status in ("Accepted", "Rejected"):
				frappe.throw(_("A definitive transport result cannot be changed."))
			if doc.status not in ("Sending", "Unknown"):
				frappe.throw(_("The message has not been claimed for dispatch."))
			stamp = now or utcnow()
			doc.status = outcome
			doc.transport_reference = reference or doc.transport_reference
			if doc.message and reference:
				frappe.db.set_value("Chat Message", doc.message, "external_id", reference)
			if outcome == "Accepted":
				doc.accepted_at = stamp
				if delivery_status == 'Sent' and advances_delivery(doc.delivery_status, 'Sent'):
					doc.delivery_status = 'Sent'
				# Only an accepted, metered response with a resolved functional Output renews courtesy.
				if doc.quota_kind == "Standard" and doc.functional_result:
					contact.quota_courtesy_cycle += 1
					contact.save(ignore_permissions=True)
			if outcome != "Unknown":
				doc.resolved_at = stamp
			doc.save(ignore_permissions=True)
			from messaging.conversation.doctype.chat_session.chat_session import settle_technical_failure
			settle_technical_failure(frappe.get_doc("Chat Session", doc.session), stamp)
			frappe.publish_realtime("messaging_conversation_update", {"channel": doc.channel, "account": doc.account, "session": doc.session}, after_commit=True)
			frappe.db.commit()
			if outcome != "Unknown" and doc.message_type == "Automatic":
				from messaging.conversation.doctype.chat_contact.chat_contact import resume_pending_inputs
				resume_pending_inputs(contact_name)
			return envelope(doc.name, transport=transport)
		except Exception:
			frappe.db.rollback()
			raise


def claim(dispatch_name):
	"""Commit the one-time handoff before any adapter call; lost handoffs require reconciliation."""
	require_dispatch_operator(frappe.get_doc("Bot Dispatch", dispatch_name))
	contact_name = frappe.db.get_value("Bot Dispatch", dispatch_name, "contact")
	key = frappe.db.get_value("Chat Contact", contact_name, "identity_key")
	with contact_lock(key):
		try:
			lock_contact(contact_name)
			doc = frappe.get_doc("Bot Dispatch", dispatch_name)
			if doc.status != "Prepared":
				frappe.db.commit()
				return None
			if doc.message_type == 'Automatic':
				from messaging.conversation.doctype.chat_session.human import HUMAN_OPEN
				session = frappe.get_doc('Chat Session', doc.session)
				if session.status in ('Closed', 'Expired') and not _is_human_completion(doc, session):
					doc.status, doc.resolved_at, doc.diagnostic_code = 'Rejected', utcnow(), 'episode_closed_before_handoff'
					doc.save(ignore_permissions=True)
					frappe.db.commit()
					return None
				if session.status in HUMAN_OPEN and session.human_request_turn != doc.turn:
					frappe.db.commit()
					return None
			else:
				require_dispatch_operator(doc, responding=True)
			doc.status = "Sending"
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return envelope(doc.name)
		except Exception:
			frappe.db.rollback()
			raise


def _is_human_completion(doc, session):
	"""Only the deterministic notice of a successfully closed human request may send."""
	return (session.status == 'Closed' and session.close_reason in ('Manual', 'Inactivity')
		and bool(session.closed_at)
		and doc.message_type == 'Automatic' and doc.quota_kind == 'Human'
		and not doc.agent and not doc.menu_options and not doc.functional_result
		and doc.turn == session.human_request_turn
		and doc.outbound_key == identity_key(session.name, session.human_request_turn, 'human_completion'))


def simulate_dispatch(dispatch_name, outcome="Accepted"):
	"""Controlled test transport. Acceptance here never asserts real provider delivery."""
	if claim(dispatch_name) is None:
		return envelope(dispatch_name)
	return reconcile(dispatch_name, outcome, reference="simulated")


def on_doctype_update():
	frappe.db.add_index("Bot Dispatch", ["channel", "account", "transport_reference"], "provider_reference")
	frappe.db.add_index("Bot Dispatch", ["contact", "quota_kind", "status", "accepted_at"], "quota_usage")
	frappe.db.add_index("Bot Dispatch", ["contact", "quota_kind", "courtesy_cycle", "status"], "courtesy_usage")


def require_dispatch_operator(doc, *, responding=False):
	if doc.message_type != 'Human':
		require_operator()
		return
	from messaging.conversation.doctype.chat_session.permissions import check_session
	session = frappe.get_doc('Chat Session', doc.session)
	check_session(session, responding=responding)
	if doc.agent != frappe.session.user:
		frappe.throw(_("A human message belongs to its sending agent."), frappe.PermissionError)
