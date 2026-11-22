"""Minute scheduler and transport reconciliation for already prepared snapshots.

Entry points own their transactions and use the canonical contact connection lock.
Business eligibility uses the approved preparation Process; no conversational turn or quota reservation occurs here.
"""
from messaging.communication.doctype.communication_provider.communication_provider import handoff, validate_result, advances_delivery, POSITIVE_DELIVERY, operation
from messaging.security.permissions import is_system_manager
from contextlib import ExitStack
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo
import json
import uuid

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from messaging.chatbot.doctype.bot_settings.language import presentation_language
from messaging.communication.doctype.notification_queue.notification_queue import internal_write
from messaging.communication.doctype.process_definition import approval
from messaging.communication.doctype.communication_channel_account.binding import resolve_binding
from messaging.security.execution_context import automatic_job, is_automation
from messaging.common.locking import contact_lock
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_session.chat_session import notification_blocker

BATCH_SIZE = 50
CLAIM_MINUTES = 5


def require_dispatcher():
	if not is_automation() and not is_system_manager():
		frappe.throw(_("Notification dispatch requires System Manager permission."), frappe.PermissionError)


def save(doc):
	with internal_write():
		# Snapshots are immutable. Preserve references for audit even if an external
		# application has removed a source document outside normal link checks.
		doc.flags.ignore_links = True
		doc.save(ignore_permissions=True)


def envelope(name):
	doc = frappe.get_doc('Notification Queue', name)
	return dict(source_type='Notification Queue', notification=doc.name, provider=doc.provider,
		channel=doc.channel, account=doc.account, account_id=doc.account_id, destination=doc.effective_recipient,
		recipient=doc.recipient, emulator_contact=doc.emulator_contact, delivery_mode=doc.delivery_mode,
		outbound_id=doc.occurrence_key, message_type='Notification', content_type='Template' if doc.content_type == 'Provider Template' else 'Text',
		output_mode=doc.content_type, template_revision=doc.template_revision or '',
		provider_template_reference=doc.get('provider_template_reference') or '',
		content=doc.prepared_text or '', template_reference=(doc.get('provider_template_reference') if doc.content_type == 'Provider Template' else doc.template_reference) or '',
		variables=json.loads(doc.variables_json or '{}'))


def result(doc):
	return dict(notification=doc.name, state=doc.status, acceptance=doc.status,
		delivery=doc.delivery_status, reference=doc.transport_reference)


def defer(doc, code, now):
	doc.status = 'Deferred'
	doc.defer_count = (doc.defer_count or 0) + 1
	doc.diagnostic_code = code
	minutes = 1 if code in ('human_attention', 'chatbot_episode', 'chatbot_processing') else min(60, 5 * 2 ** min(doc.defer_count - 1, 4))
	doc.next_attempt_at = now + timedelta(minutes=minutes)
	save(doc)
	frappe.publish_realtime('messaging_conversation_update', dict(channel=doc.channel, account=doc.account), after_commit=True)
	frappe.db.commit()
	return result(doc)


def submit_notice_cancelled(doc):
	"""Return a terminal reason when a pending submit notice outlived its document.

	Only ``on_submit`` depends on the document remaining submitted. ``on_cancel``
	stays eligible at ``docstatus`` 2. The read is repeated on every attempt and
	does not lock the source across transport.
	"""
	if doc.document_event != "on_submit":
		return None
	docstatus = frappe.db.get_value(doc.source_doctype, doc.source_name, "docstatus")
	if docstatus is not None and int(docstatus) == 2:
		return "source_cancelled"
	return None


def _omit_cancelled_submit(doc):
	reason = submit_notice_cancelled(doc)
	if not reason:
		return None
	doc.status, doc.diagnostic_code = "Cancelled", reason
	save(doc)
	frappe.db.commit()
	return result(doc)


def revalidate(doc):
	"""Recheck the original event; changed data never rewrites the immutable snapshot."""
	rule = approval.lock_execution("Notification Rule", doc.notification_rule)
	if not approval.is_current(rule):
		return "rule_approval_obsolete"

	from messaging.communication.doctype.communication_template.status import notification_template_gate
	if doc.template_reference:
		variables = None if doc.content_type == 'Provider Template' else json.loads(doc.variables_json or '{}')
		blocked = notification_template_gate(doc.template_reference, doc.account, variables=variables,
			expected_revision=doc.template_revision or '', live=True)
		if blocked:
			return blocked
	from messaging.communication.doctype.notification_rule.notification_rule import notification_destination, delivery_fingerprint
	from messaging.communication.doctype.notification_rule.contract import prepared_data
	from messaging.communication.doctype.process_definition.execution import execute_process
	account = frappe.get_doc('Communication Channel Account', doc.account)
	try:
		destination = notification_destination(rule, account, doc.recipient or '')
	except frappe.ValidationError:
		return 'recipient_configuration_invalid'
	if destination is None:
		return 'recipient_missing'
	if (rule.account != doc.account or not doc.delivery_fingerprint
		or destination != dict(effective_recipient=doc.effective_recipient, emulator_contact=doc.emulator_contact, delivery_mode=doc.delivery_mode)
		or delivery_fingerprint(rule, destination) != doc.delivery_fingerprint):
		return 'delivery_configuration_changed'
	if not frappe.db.exists(doc.source_doctype, doc.source_name):
		return 'source_unavailable'
	cancelled = submit_notice_cancelled(doc)
	if cancelled:
		return cancelled
	source = frappe.get_doc(doc.source_doctype, doc.source_name)
	from messaging.communication.doctype.notification_rule.conditions import matches
	if not matches(dict(document_event=doc.document_event, field_name=doc.condition_field or '', value=doc.condition_value or ''), source):
		return 'condition_changed'
	prepared = {}
	def render(response):
		prepared['data'] = prepared_data(response, rule)
		return dict(status=response['status'], output_mode='', rendered_message='')
	with presentation_language(rule.process_language(), notification=True):
		response = execute_process(process_method=rule.process_method,
			arguments=dict(doctype=doc.source_doctype, name=doc.source_name, event=doc.document_event, occurrence=doc.occurrence),
			source_type='Notification Rule', source_name=rule.name, execution_id=doc.claim_id,
			render_result=render, log_context=dict(execution_context='Notification Preparation',
				source_doctype=doc.source_doctype, source_document=doc.source_name, source_event=doc.document_event, occurrence=doc.occurrence))
	if response.get('no_result'):
		return 'event_no_longer_eligible'
	if not response['functional']:
		return 'business_validation_unavailable'
	if prepared.get('data') is None:
		return 'event_no_longer_eligible'
	fresh = dict(prepared['data'], variables_json=json.loads(prepared['data']['variables_json']))
	stored = dict(recipient=doc.recipient or '', prepared_text=doc.prepared_text or '',
		variables_json=json.loads(doc.variables_json or '{}'), template_revision=doc.template_revision or '')
	if doc.template_contract_json:
		stored['template_contract_json'] = doc.template_contract_json
	if fresh != stored:
		return 'business_snapshot_changed'
	return None


def _binding(doc):
	definition, account = resolve_binding(doc.provider, doc.account)
	if (account.channel, account.account_id) != (doc.channel, doc.account_id):
		frappe.throw(_("The notification does not match the channel account."))
	return definition


@automatic_job
def deliver(name, *, now=None):
	"""Claim durably, validate under the canonical contact lock, then hand off once."""
	require_dispatcher()
	now = get_datetime(now) if now else now_datetime()
	doc = frappe.get_doc('Notification Queue', name)
	keys = {doc.identity_key}
	if doc.recipient:
		keys.add(identity_key(doc.channel, doc.account, doc.recipient))
	with ExitStack() as locks:
		# Acquire both identities before mutations; sorted order prevents cross-redirect deadlocks.
		for key in sorted(keys):
			locks.enter_context(contact_lock(key))
		doc = frappe.get_doc('Notification Queue', name, for_update=True)
		if doc.status not in ('Pending', 'Deferred') or get_datetime(doc.scheduled_at) > now or get_datetime(doc.next_attempt_at) > now:
			return result(doc)
		doc.previous_state = doc.status
		doc.status = 'Processing'
		doc.claim_id = str(uuid.uuid4())
		doc.last_attempt_at = now
		doc.attempt_count = (doc.attempt_count or 0) + 1
		save(doc)
		frappe.db.commit()
		# Stop before any deferral so a cancelled submit is not retried.
		omitted = _omit_cancelled_submit(doc)
		if omitted:
			return omitted
		from messaging.communication.doctype.process_definition.process_definition import resolve_definition
		try:
			resolve_definition(frappe.db.get_value('Notification Rule', doc.notification_rule,
				'process_definition'), expected_type='notification')
		except frappe.ValidationError:
			doc.status, doc.diagnostic_code = 'Cancelled', 'process_unavailable'
			save(doc); frappe.db.commit()
			return result(doc)
		rule_status = frappe.db.get_value('Notification Rule', doc.notification_rule, 'status')
		if rule_status == 'Unavailable':
			doc.status, doc.diagnostic_code = 'Cancelled', 'rule_approval_obsolete'
			save(doc); frappe.db.commit()
			return result(doc)
		if rule_status != 'Enabled':
			return defer(doc, 'rule_disabled', now)
		account = frappe.db.get_value('Communication Channel Account', doc.account, ['channel', 'account_id'], as_dict=True)
		provider = frappe.db.get_value('Communication Channel', doc.channel, 'provider')
		if not account or not provider or (account.channel, account.account_id, provider) != (doc.channel, doc.account_id, doc.provider):
			doc.status, doc.diagnostic_code = 'Cancelled', 'account_identity_invalid'
			save(doc); frappe.db.commit()
			return result(doc)
		try:
			definition = _binding(doc)
		except Exception:
			frappe.db.rollback()
			return defer(doc, 'account_unavailable', now)
		from messaging.communication.doctype.communication_template.external import delivery_lock, managed
		if managed(definition) and (doc.content_type != 'Provider Template' or not doc.template_contract_json):
			return defer(doc, 'template_status_unknown', now)
		capability = 'provider_templates' if doc.content_type == 'Provider Template' else 'text'
		if capability not in definition['capabilities'] or 'validate_notification' not in definition['operations']:
			doc.status, doc.diagnostic_code = 'Cancelled', 'content_unsupported'
			save(doc); frappe.db.commit()
			return result(doc)
		utc_now = now.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone())).astimezone(timezone.utc).replace(tzinfo=None)
		blocker = next((reason for key in sorted(keys) if (reason := notification_blocker(key, utc_now))), None)
		if blocker:
			return defer(doc, blocker, now)
		if managed(definition):
			try:
				acquired = locks.enter_context(delivery_lock(definition, doc.channel, doc.account))
			except Exception:
				return defer(doc, 'template_status_unknown', now)
			if not acquired:
				return defer(doc, 'template_sync_in_progress', now)
			frappe.db.rollback()
		try:
			reason = revalidate(doc)
		except frappe.PermissionError:
			frappe.db.rollback()
			return defer(doc, 'rule_disabled', now)
		except Exception:
			frappe.db.rollback()
			reason = 'delivery_revalidation_failed'
		if reason:
			if reason in ('business_validation_unavailable', 'template_not_active', 'template_unavailable', 'template_status_unknown'):
				return defer(doc, reason, now)
			doc.status, doc.diagnostic_code = 'Cancelled', reason
			save(doc); frappe.db.commit()
			return result(doc)
		expected_approval = frappe.db.get_value("Notification Rule", doc.notification_rule, "approval_hash")
		payload = envelope(doc.name)
		if doc.content_type == 'Provider Template':
			try:
				from messaging.communication.doctype.communication_template.communication_template import provider_template_contract
				contract = provider_template_contract(doc.account, doc.provider_template_reference)
				if contract['revision'] != doc.template_revision:
					return defer(doc, 'template_unavailable', now)
				mapped = operation(definition, 'prepare_provider_template', payload)
				if not isinstance(mapped, dict) or len(json.dumps(mapped, allow_nan=False).encode()) > 65536:
					raise frappe.ValidationError
				payload['provider_template_parameters'] = mapped
			except Exception:
				doc.status, doc.diagnostic_code = 'Cancelled', 'provider_template_invalid'
				save(doc); frappe.db.commit()
				return result(doc)
		try:
			valid = operation(definition, 'validate_notification', payload)
		except Exception:
			frappe.db.rollback()
			return defer(doc, 'content_validation_unavailable', now)
		if valid is not True:
			doc.status, doc.transport_outcome, doc.diagnostic_code = 'Rejected', 'Rejected', 'destination_or_content_invalid'
			doc.resolved_at = now
			save(doc); frappe.db.commit()
			return result(doc)
		# Repeat the read immediately before the handoff marker. A marker is written
		# only when the submit document is still uncancelled, so a crash here stays recoverable.
		omitted = _omit_cancelled_submit(doc)
		if omitted:
			return omitted
		# The connection lock serializes reception and Human transitions through handoff.
		# This durable marker is written BEFORE calling send. A lost result is Unknown.
		doc.handoff_at, doc.diagnostic_code = now, None
		save(doc)
		frappe.publish_realtime('messaging_conversation_update', dict(channel=doc.channel, account=doc.account), after_commit=True)
		frappe.db.commit()
		# Invalidation may win the race at the commit boundary. Hold the current
		# Rule lock through transport, and cancel safely if no handoff was made.
		try:
			rule = approval.lock_execution("Notification Rule", doc.notification_rule)
		except frappe.PermissionError:
			rule = None
		if rule is None or rule.approval_hash != expected_approval:
			doc.handoff_at = None
			doc.status, doc.diagnostic_code = "Cancelled", "rule_approval_obsolete"
			save(doc)
			frappe.db.commit()
			return result(doc)
		response = handoff(definition, payload)
		doc.reload()
		return _reconcile(doc, response)


def _reconcile(doc, response):
	validate_result(response)
	outcome, reference = response['outcome'], response.get('reference')
	if doc.transport_reference and reference and doc.transport_reference != reference:
		frappe.throw(_("The transport reference does not match this notification."))
	if doc.status in ('Accepted', 'Rejected'):
		if doc.status != outcome:
			frappe.throw(_("A definitive transport outcome cannot be reversed."))
		return result(doc)
	if doc.status not in ('Processing', 'Unknown') or not doc.handoff_at:
		frappe.throw(_("The notification has not been handed to transport."))
	doc.status = outcome
	doc.transport_outcome = outcome
	if response.get('delivery_status') == 'Sent' and advances_delivery(doc.delivery_status, 'Sent'):
		doc.delivery_status = 'Sent'
	if reference:
		doc.transport_reference = reference
	doc.resolved_at = now_datetime()
	doc.diagnostic_code = 'transport_uncertain' if outcome == 'Unknown' else None
	save(doc)
	frappe.publish_realtime('messaging_conversation_update', dict(channel=doc.channel, account=doc.account), after_commit=True)
	frappe.db.commit()
	return result(doc)


def _reported(provider, account, name):
	definition, binding = resolve_binding(provider, account, incoming=True)
	doc = frappe.get_doc('Notification Queue', name)
	if (doc.provider, doc.account, doc.channel, doc.account_id) != (provider, binding.name, binding.channel, binding.account_id):
		frappe.throw(_("The notification does not match the channel account."), frappe.PermissionError)
	return definition, doc


def report_result(provider, account, name, response):
	try:
		validate_result(response)
	except ValueError:
		frappe.throw(_("Invalid transport outcome."))
	_definition, doc = _reported(provider, account, name)
	with contact_lock(doc.identity_key):
		_reported(provider, account, name)
		doc.reload()
		return _reconcile(doc, response)


def report_status(provider, account, name, status, reference):
	definition, doc = _reported(provider, account, name)
	if ('delivery_status' not in definition['capabilities'] or status not in ('Sent', 'Delivered', 'Read', 'Failed')
		or not isinstance(reference, str) or not reference.strip() or len(reference) > 140):
		frappe.throw(_("Invalid delivery status."))
	with contact_lock(doc.identity_key):
		_reported(provider, account, name)
		doc.reload()
		if not doc.handoff_at or doc.status not in ('Processing', 'Unknown', 'Accepted', 'Rejected'):
			frappe.throw(_("The notification has not been handed to transport."))
		if doc.transport_reference and doc.transport_reference != reference:
			frappe.throw(_("The transport reference does not match this notification."))
		if doc.status == 'Rejected':
			return dict(condition='conflicting_definitive_result')
		if doc.status in ('Processing', 'Unknown') and status in POSITIVE_DELIVERY:
			_reconcile(doc, dict(outcome='Accepted', reference=reference))
			doc.reload()
		if advances_delivery(doc.delivery_status, status):
			doc.delivery_status, doc.transport_reference = status, reference
			if status == 'Delivered': doc.delivered_at = now_datetime()
			if status == 'Read': doc.read_at = now_datetime()
			if status == 'Failed': doc.diagnostic_code = 'provider_delivery_failed'
			save(doc)
			frappe.publish_realtime('messaging_conversation_update', dict(channel=doc.channel, account=doc.account), after_commit=True)
			frappe.db.commit()
		return result(doc)


def recover_claims(now):
	rows = frappe.get_all('Notification Queue', filters={'status': 'Processing',
		'last_attempt_at': ['<=', now - timedelta(minutes=CLAIM_MINUTES)]},
		fields=['name', 'identity_key'], order_by='last_attempt_at, name', limit_page_length=BATCH_SIZE)
	for row in rows:
		try:
			with contact_lock(row.identity_key):
				doc = frappe.get_doc('Notification Queue', row.name, for_update=True)
				if doc.status != 'Processing' or get_datetime(doc.last_attempt_at) > now - timedelta(minutes=CLAIM_MINUTES):
					continue
				if doc.handoff_at:
					doc.status, doc.transport_outcome, doc.diagnostic_code = 'Unknown', 'Unknown', 'interrupted_handoff'
					save(doc); frappe.db.commit()
				else:
					defer(doc, 'interrupted_before_handoff', now)
		except Exception:
			frappe.db.rollback()
			frappe.logger('messaging').warning('Notification claim recovery deferred; sensitive details omitted.')


@automatic_job
def dispatch_due(*, now=None, names=None):
	"""One native cron job, bounded ordered batches. Optional exact names support controlled runs."""
	require_dispatcher()
	now = get_datetime(now) if now else now_datetime()
	# Controlled runs must never sweep unrelated claims in the shared site.
	if names is None:
		recover_claims(now)
	filters = {'status': ['in', ['Pending', 'Deferred']], 'scheduled_at': ['<=', now], 'next_attempt_at': ['<=', now]}
	if names is not None:
		if not names:
			return []
		filters['name'] = ['in', names]
	rows = frappe.get_all('Notification Queue', filters=filters, pluck='name',
		order_by='scheduled_at, creation, name', limit_page_length=BATCH_SIZE)
	results = []
	for name in rows:
		try:
			results.append(deliver(name, now=now))
		except Exception:
			frappe.db.rollback()
			frappe.logger('messaging').warning('Notification dispatch deferred; sensitive details omitted.')
	return results
