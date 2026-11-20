"""Persisted Process approval and status transitions shared by its consumers."""
import hashlib
import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from messaging.common.locking import document_savepoint
from messaging.security.permissions import require_system_manager

from .process_definition import ProcessUnavailableError, definition_revision, resolve_definition
from .revisions import canonical_json

CONSUMER_TYPES = {"Bot Flow": "bot_flow", "Notification Rule": "notification"}
VALIDATION_CONTRACT = 6
APPROVED_STATUSES = ("Disabled", "Enabled")
APPROVAL_FIELDS = (
	"approval_hash", "approved_process_revision", "approved_contract_hash", "approved_delivery_hash",
	"process_test_checked_at", "process_test_checked_by", "process_test_log",
	"process_test_evidence", "approval_invalidation",
)


class ProcessConsumer(Document):
	def db_set(self, fieldname, value=None, **kwargs):
		fields = fieldname if isinstance(fieldname, dict) else (fieldname,)
		if {"status", *APPROVAL_FIELDS}.intersection(fields):
			raise frappe.PermissionError(_("Use the available actions to change Status."))
		return super().db_set(fieldname, value, **kwargs)


def digest(value):
	return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def contract_hash(doc):
	return digest(doc._consumer_fingerprint())


def delivery_hash(doc):
	return digest(doc._approval_delivery_fingerprint()) if doc.doctype == "Notification Rule" else None


def approval_hash(doc, definition, contract, delivery):
	return digest(dict(
		consumer=[doc.doctype, doc.name],
		process=definition.name,
		creation=str(definition.creation),
		revision=definition.capability_revision,
		contract=contract,
		delivery=delivery,
		validation_contract=VALIDATION_CONTRACT,
	))


def definition_for(doc):
	return resolve_definition(doc.process_definition, expected_type=CONSUMER_TYPES[doc.doctype])


def is_current(doc, definition=None):
	if doc.status not in APPROVED_STATUSES or not all(
		doc.get(field) for field in ("approval_hash", "approved_process_revision", "approved_contract_hash", "process_test_log")
	):
		return False
	if definition is None:
		try:
			definition = definition_for(doc)
		except ProcessUnavailableError:
			return False
	if doc.process_definition != definition.name or doc.approved_process_revision != definition.capability_revision:
		return False
	try:
		current_contract = contract_hash(doc)
		current_delivery = delivery_hash(doc)
	except (frappe.ValidationError, frappe.DoesNotExistError, ValueError, TypeError):
		return False
	return (
		doc.approved_contract_hash == current_contract
		and doc.get("approved_delivery_hash") == current_delivery
		and doc.approval_hash == approval_hash(doc, definition, current_contract, current_delivery)
	)


def require_approved(doc, definition=None):
	if not is_current(doc, definition):
		raise frappe.ValidationError(_("Validate this document before enabling or running its Process."))


def protect(doc):
	"""Restore server-owned evidence and prevent ordinary writes to Status."""
	previous = doc.get_doc_before_save()
	status = previous.status if previous else "Untested"
	if (doc.status or status) != status:
		raise frappe.PermissionError(_("Use the available actions to change Status."))
	doc.status = status
	for field in APPROVAL_FIELDS:
		if doc.meta.has_field(field):
			doc.set(field, previous.get(field) if previous else None)


def invalidate(doc, reason, *, definition=None, previous=None):
	"""Retain the receipt and its log while withdrawing its authority."""
	if doc.status not in APPROVED_STATUSES or not doc.approval_hash:
		return {}
	history = json.loads(doc.approval_invalidation or "[]")
	history.append(dict(
		reason=reason,
		at=str(now_datetime()),
		approval_hash=doc.approval_hash,
		execution_log=doc.process_test_log,
		process=(previous or doc).process_definition if definition is None else definition.name,
		approved_revision=doc.approved_process_revision,
		registered_revision=definition.capability_revision if definition else None,
	))
	values = dict(status="Unavailable", approval_invalidation=canonical_json(history))
	doc.update(values)
	return values


def fields_changed(doc, *fields):
	"""Ignore framework row metadata when comparing saved functional inputs."""
	previous = doc.get_doc_before_save()
	if not previous:
		return True

	def value(source, field):
		result = source.get(field)
		if source.meta.get_field(field).fieldtype == "Table":
			if not result:
				return []
			columns = set(result[0].meta.get_valid_columns())
			return [{df.fieldname: row.get(df.fieldname) for df in row.meta.fields
				if df.fieldname in columns} for row in result]
		return result

	return any(value(doc, field) != value(previous, field) for field in fields)


def check_save(doc):
	previous = doc.get_doc_before_save()
	if previous and doc.status in APPROVED_STATUSES and not is_current(doc):
		reason = "consumer_contract_changed"
		if (doc.doctype == "Notification Rule" and doc.process_definition == previous.process_definition
			and doc.approved_contract_hash == contract_hash(doc)):
			reason = "delivery_contract_changed"
		invalidate(doc, reason, previous=previous)


def dependency_changed(doc, reason):
	"""Withdraw a receipt when its referenced configuration was changed."""
	if not is_current(doc):
		values = invalidate(doc, reason)
		if values:
			_write(doc, values)


def _write(doc, values):
	frappe.db.set_value(doc.doctype, doc.name, values)
	if doc.doctype == "Notification Rule":
		doc._approval_status_changed()


def process_changed(definition, method=None):
	"""Revoke linked receipts in the Registry's own transaction."""
	previous = definition.get_doc_before_save()
	if not previous or (previous.capability_revision, previous.status) == (definition.capability_revision, definition.status):
		return
	reason = "process_unavailable" if definition.status != "Enabled" else "process_revision_changed"
	for doctype in CONSUMER_TYPES:
		table = frappe.qb.DocType(doctype)
		rows = (
			frappe.qb.from_(table)
			.select(table.name, table.status, table.approval_hash, table.approved_process_revision,
				table.process_test_log, table.approval_invalidation)
			.where(table.process_definition == definition.name)
			.orderby(table.name)
			.for_update()
			.run(as_dict=True)
		)
		for row in rows:
			if row.approved_process_revision != previous.capability_revision or row.status not in APPROVED_STATUSES:
				continue
			doc = frappe.get_doc(dict(row, doctype=doctype, process_definition=definition.name))
			values = invalidate(doc, reason, definition=definition)
			if values:
				_write(doc, values)


def lock_execution(doctype, name):
	"""Lock the consumer and reject a snapshot that predates a concurrent change."""
	table = frappe.qb.DocType(doctype)
	query, values = (
		frappe.qb.from_(table)
		.select(table.modified, table.status, table.approval_hash)
		.where(table.name == name)
		.walk()
	)
	rows = frappe.db.sql(query + " LOCK IN SHARE MODE", values)
	if not rows or rows[0][1] != "Enabled":
		raise frappe.PermissionError(_("Process authorization is unavailable"))
	doc = frappe.get_doc(doctype, name)
	if rows[0] != (doc.modified, doc.status, doc.approval_hash):
		raise frappe.PermissionError(_("Process authorization is unavailable"))
	return doc


def _get_consumer(doctype, name, modified):
	require_system_manager()
	doc = frappe.get_doc(doctype, name, for_update=True)
	doc.check_permission("read")
	doc.check_permission("write")
	if str(modified) != str(doc.modified):
		raise frappe.TimestampMismatchError(_("The document changed. Reload it before changing Status."))
	return doc


def receipt(doc, definition, *, log, checked_at, checked_by):
	contract = contract_hash(doc)
	delivery = delivery_hash(doc)
	values = dict(
		approval_hash=approval_hash(doc, definition, contract, delivery),
		approved_process_revision=definition.capability_revision,
		approved_contract_hash=contract,
		process_test_log=log,
		process_test_checked_at=checked_at,
		process_test_checked_by=checked_by,
	)
	if doc.doctype == "Notification Rule":
		values["approved_delivery_hash"] = delivery
	return values


def validate(doctype, name, modified, run_test):
	with document_savepoint():
		doc = _get_consumer(doctype, name, modified)
		if doc.status not in ("Untested", "Unavailable"):
			raise frappe.ValidationError(_("Validate only documents in Untested or Unavailable status."))
		try:
			definition = definition_for(doc)
			result = run_test(doc, definition)
		except (frappe.ValidationError, frappe.DoesNotExistError) as exc:
			result = dict(status="Failed", message=str(exc), execution_log=None)
		evidence = dict(
			scope="structural_contract", result=result["status"], log=result.get("execution_log"),
			checked_at=str(now_datetime()), checked_by=frappe.session.user,
		)
		values = dict(process_test_evidence=canonical_json(evidence))
		if result["status"] == "Passed":
			approved = receipt(doc, definition, log=result["execution_log"],
				checked_at=evidence["checked_at"], checked_by=evidence["checked_by"])
			recorded = frappe.db.get_value("Process Execution Log", result["execution_log"], "configuration_fingerprint")
			if recorded != approved["approval_hash"]:
				raise frappe.TimestampMismatchError(_("The document changed. Reload it before changing Status."))
			values.update(approved, status="Disabled")
		_write(doc, values)
		return dict(result, consumer_status=values.get("status", doc.status))


def set_active(doctype, name, modified, active):
	if type(active) not in (int, bool, str) or active not in (0, 1, "0", "1"):
		raise frappe.ValidationError(_("Invalid activation."))
	active = bool(int(active))
	with document_savepoint():
		doc = _get_consumer(doctype, name, modified)
		if doc.status != ("Disabled" if active else "Enabled"):
			raise frappe.ValidationError(_("This action is unavailable for the current Status."))
		if active and not is_current(doc):
			values = invalidate(doc, "approval_obsolete") or dict(status="Unavailable")
			_write(doc, values)
			return dict(consumer_status="Unavailable", message=_("The Process approval is obsolete. Validate this document again."))
		status = "Enabled" if active else "Disabled"
		_write(doc, dict(status=status))
		if active:
			doc.status = status
			doc._validate_activation()
		return dict(consumer_status=status)


def runtime_evidence(doc, definition, *, administrative_test=False):
	from .process_definition import registered_snapshot

	return dict(
		process_definition=definition.name,
		definition_revision=definition_revision(definition),
		runtime_revision=registered_snapshot(definition)["runtime"],
		configuration_fingerprint=(
			approval_hash(doc, definition, contract_hash(doc), delivery_hash(doc))
			if administrative_test else doc.approval_hash
		),
	)
