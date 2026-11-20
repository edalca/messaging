"""Transactional notification preparation from exact Frappe document events."""
from contextvars import ContextVar
from datetime import timedelta
from functools import partial

import frappe
from frappe.utils import now_datetime

from messaging.chatbot.doctype.bot_settings.language import presentation_language
from messaging.common.primitives import identity_key
from messaging.communication.doctype.communication_provider.communication_provider import (
	provider_definition,
)
from messaging.communication.doctype.notification_queue.notification_queue import internal_write
from messaging.communication.doctype.notification_rule.conditions import (
	canonical_conditions,
	condition_token,
	matches,
	occurrence_key,
)
from messaging.communication.doctype.notification_rule.contract import (
	prepared_data,
	selected_output,
)
from messaging.communication.doctype.notification_rule.notification_rule import (
	EXCLUDED,
	delivery_fingerprint,
	notification_destination,
	rule_index,
	validate_notification_delay,
)
from messaging.communication.doctype.process_definition.execution import (
	execute_process,
	is_process_test,
)
from messaging.communication.doctype.process_execution_log.process_execution_log import (
	record_process_execution_log,
)
from messaging.security.execution_context import _automation_context

active_occurrences = ContextVar("notification_active_occurrences", default=frozenset())


def observe(doc, event):
	"""Prepare in the caller's transaction; no callback, transport, commit or durable side channel."""
	if is_process_test():
		return
	if doc.doctype in EXCLUDED or frappe.flags.in_migrate or frappe.flags.in_install:
		return
	rules = rule_index().get(doc.doctype, {}).get(event, [])
	if not rules:
		return
	captured_at = now_datetime()
	for name in rules:
		guard = (name, doc.doctype, doc.name, event)
		if guard in active_occurrences.get():
			continue
		row = frappe.db.get_value("Notification Rule", name, "name")
		if not row:
			continue
		rule = frappe.get_doc("Notification Rule", name)
		if rule.status != "Enabled" or rule.source_doctype != doc.doctype:
			continue
		token = active_occurrences.set(active_occurrences.get() | {guard})
		try:
			for condition in canonical_conditions(rule):
				if condition["document_event"] != event or not matches(condition, doc):
					continue
				if event == 'on_update_after_submit' and condition['field_name']:
					previous = doc.get_doc_before_save()
					if previous and previous.get(condition['field_name']) == doc.get(condition['field_name']):
						continue
				key = occurrence_key(rule, doc, condition)
				with _automation_context():
					prepare(
						rule,
						doc,
						event,
						condition_token(condition),
						key,
						captured_at,
						condition=condition,
					)
		finally:
			active_occurrences.reset(token)


def prepare(rule, doc, event, occurrence, key, captured_at, *, condition):
	context = dict(
		execution_context="Notification Preparation",
		notification_key=key,
		source_doctype=doc.doctype,
		source_document=doc.name,
		source_event=event,
		occurrence=occurrence,
	)
	# A unique insert is the pre-invocation claim. Do not rely on snapshot reads of a
	# receipt committed by a concurrent transaction (MariaDB snapshot isolation).
	savepoint = "notification_" + frappe.generate_hash(length=12)
	frappe.db.savepoint(savepoint)
	try:
		receipt = record_process_execution_log(
			process_method=rule.process_method,
			execution_outcome="Running",
			source_type="Notification Rule",
			source_name=rule.name,
			execution_id=key,
			**context,
		)
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		frappe.db.rollback(save_point=savepoint)
		frappe.clear_last_message()
		return
	prepared = {}

	def render(result):
		row = selected_output(rule, result["status"])
		prepared["output"] = row
		reason = "send_disabled"
		if row.send:
			account = frappe.get_doc("Communication Channel Account", rule.account)
			try:
				destination = notification_destination(rule, account, result["recipient"])
			except frappe.ValidationError:
				frappe.clear_last_message()
				reason = "recipient_configuration_invalid" if (
					rule.redirect_recipient
					or "notification_redirect_required" in provider_definition(rule.provider_context)["capabilities"]
				) else "recipient_invalid"
			else:
				if destination is None:
					reason = "recipient_missing"
				else:
					prepared.update(account=account, destination=destination, data=prepared_data(result, rule))
					reason = ""
		return dict(
			status=result["status"],
			output_mode="Template" if not reason else "",
			rendered_message="",
			diagnostic="Notification suppressed: " + reason if reason else "",
			template_reference=row.template_reference if not reason else None,
			template_revision=prepared["data"]["template_revision"] if not reason else "",
		)

	with presentation_language(rule.process_language(), notification=True):
		result = execute_process(
			process_method=rule.process_method,
			arguments=dict(doctype=doc.doctype, name=doc.name, event=event, occurrence=occurrence),
			source_type="Notification Rule",
			source_name=rule.name,
			execution_id=key,
			invocation_mode=rule.process_invocation_mode,
			render_result=render,
			log_context=dict(existing_log=receipt.name, **context),
		)
	# A failed preparation is a durable failure receipt, not a ready notification.
	# The business write may commit with this safe diagnostic; no automatic Process retry.
	if not result["functional"] or prepared.get("data") is None:
		return
	account = prepared["account"]
	provider = frappe.db.get_value("Communication Channel", account.channel, "provider")
	delay = validate_notification_delay(rule.notification_delay_minutes)
	destination = prepared["destination"]
	effective = destination["effective_recipient"]
	selected = prepared["output"]
	provider_reference = (
		selected.template_reference
		if "provider_templates" in provider_definition(rule.provider_context)["capabilities"]
		else None
	)
	with internal_write():
		frappe.get_doc(
			dict(
				doctype="Notification Queue",
				notification_rule=rule.name,
				source_application=rule.source_application,
				source_doctype=doc.doctype,
				source_name=doc.name,
				document_event=event,
				condition_field=condition["field_name"],
				condition_value=condition["value"],
				occurrence=occurrence,
				occurrence_key=key,
				execution_log=result["execution_log"],
				account=account.name,
				channel=account.channel,
				provider=provider,
				account_id=account.account_id,
				content_type="Provider Template" if provider_reference else "Template",
				template_reference=selected.template_reference,
				provider_template_reference=provider_reference,
				rendered_content=prepared["data"]["prepared_text"],
				delivery_mode=destination["delivery_mode"],
				delivery_fingerprint=delivery_fingerprint(rule, destination),
				effective_recipient=effective,
				emulator_contact=destination["emulator_contact"],
				identity_key=identity_key(account.channel, account.name, effective),
				event_at=captured_at,
				scheduled_at=captured_at + timedelta(minutes=delay),
				status="Pending",
				delivery_status="Pending",
				next_attempt_at=captured_at + timedelta(minutes=delay),
				**prepared["data"],
			)
		).insert(ignore_permissions=True)


def document_event(doc, method=None):
	"""Business observers are separate from channel ingress; absent consumers do no DB work.

	Notification preparation runs transactionally. Existing installed observers retain
	their after-commit contract and are independent from durable notification capture.
	"""
	# Registry synchronization is catalogue maintenance, never a business event.
	if doc.doctype == "Process Definition":
		return
	observe(doc, method)
	observers = getattr(frappe.local, "messaging_business_observers", None)
	if observers is None:
		observers = frappe.local.messaging_business_observers = frappe.get_hooks(
			"messaging_business_event_observers", {}
		)
	paths = observers.get(doc.doctype, {}).get(method, [])
	for path in paths:
		frappe.db.after_commit.add(
			partial(_observe_business_event, path, doc.doctype, doc.name, method)
		)


def _observe_business_event(path, doctype, name, event):
	try:
		frappe.get_attr(path)(doctype=doctype, name=name, event=event)
	except Exception:
		frappe.logger("messaging").warning(
			"Business event observer failed; sensitive details omitted."
		)
