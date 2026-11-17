import json
import traceback
from frappe.model.naming import make_autoname
from messaging.common.write_guards import InternalWriteGuard
from typing import Any

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

VALID_EXECUTION_CONTEXTS = ("Workflow Process", "Human Availability", "Notification Preparation", "Administrative Test")
_recording = InternalWriteGuard('messaging_process_execution_log')
VALID_EXECUTION_OUTCOMES = (
	"Running",
	"Functional Status",
	"No Result",
	"Internal Execution Error",
	"Invalid Schema Response",
)
VALID_OUTPUT_MODES_USED = (
	"",
	"Template",
	"Process Message",
	"Message",
	"Provider Template",
	"Internal Error Fallback",
)


class ProcessExecutionLog(Document):
	def autoname(self) -> None:
		if not self.name:
			self.name = make_autoname("PEX-.######")

	def before_validate(self) -> None:
		if not _recording.active():
			frappe.throw(_("Process execution logs are managed internally."), frappe.PermissionError)
		if not self.executed_at:
			self.executed_at = now_datetime()
		if self.schema_version is None:
			self.schema_version = 1

	def validate(self) -> None:
		if not self.is_new():
			previous = self.get_doc_before_save()
			if not previous.notification_key or previous.execution_outcome != "Running" or self.notification_key != previous.notification_key:
				frappe.throw(_("Process execution logs are immutable."), frappe.PermissionError)
		if self.source_type not in (None, "", "Bot Flow", "Notification Rule", "Human Availability"):
			frappe.throw(_("Invalid execution source."))
		if self.execution_context not in VALID_EXECUTION_CONTEXTS:
			frappe.throw(
				_("Execution Context must be one of: {0}.").format(", ".join(VALID_EXECUTION_CONTEXTS)),
				frappe.ValidationError,
			)

		if not (self.process_method or "").strip():
			frappe.throw(_("Process Method is required in Process Execution Log."), frappe.ValidationError)

		if self.execution_outcome not in VALID_EXECUTION_OUTCOMES:
			frappe.throw(
				_("Execution Outcome must be one of: {0}.").format(", ".join(VALID_EXECUTION_OUTCOMES)),
				frappe.ValidationError,
			)
		if self.execution_outcome == 'Running' and not self.notification_key:
			frappe.throw(_("Running execution receipts require a notification occurrence."))

		if self.execution_outcome == "Functional Status":
			if not (self.functional_status or "").strip():
				frappe.throw(
					_("Functional Status is required when Execution Outcome is 'Functional Status'."),
					frappe.ValidationError,
				)
		else:
			self.functional_status = None

		if (self.output_mode_used or "") not in VALID_OUTPUT_MODES_USED:
			frappe.throw(
				_("Invalid Output Mode Used '{0}'.").format(self.output_mode_used),
				frappe.ValidationError,
			)

	def on_trash(self):
		frappe.throw(_("Process execution logs cannot be deleted through Desk."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Process execution log identity is immutable."), frappe.PermissionError)


def require_execution_receipt(name, *, source_type, source_name, session, turn, execution_id, status):
	"""Verify the immutable identity and functional outcome of a recorded execution."""
	fields = ["source_type", "source_name", "session", "turn", "execution_id",
		"execution_outcome", "functional_status", "process_definition", "configuration_fingerprint"]
	log = frappe.db.get_value("Process Execution Log", name, fields, as_dict=True) if name else None
	expected = (source_type, source_name, session, turn, execution_id, "Functional Status", status)
	if not log or tuple(log.get(field) for field in fields[:7]) != expected:
		raise frappe.ValidationError(_("The Process receipt does not match the recorded execution."))
	return log


def redact_sensitive_mapping(payload: dict[str, Any] | None) -> dict[str, str]:
	"""Redact personal parameters, tokens, and sensitive values by keeping only key names and value type metadata."""
	if not isinstance(payload, dict):
		return {}

	redacted: dict[str, str] = {}
	for key, val in payload.items():
		safe_key = str(key)[:64]
		if val is None:
			redacted[safe_key] = "null"
		elif isinstance(val, bool):
			redacted[safe_key] = f"bool:{str(val).lower()}"
		elif isinstance(val, int):
			redacted[safe_key] = "int:[REDACTED]"
		elif isinstance(val, str):
			redacted[safe_key] = f"str(len={len(val)}):[REDACTED]"
		elif isinstance(val, dict):
			redacted[safe_key] = f"object(keys={len(val)}):[REDACTED]"
		elif isinstance(val, list):
			redacted[safe_key] = f"array(len={len(val)}):[REDACTED]"
		else:
			redacted[safe_key] = f"{type(val).__name__}:[REDACTED]"
	return redacted


def summarize_process_response_safely(raw_response: Any) -> dict[str, Any]:
	"""Summarize Process response metadata without storing raw personal or sensitive result payloads."""
	if not isinstance(raw_response, dict):
		return {"root_type": type(raw_response).__name__}

	data_obj = raw_response.get("data")
	msg_val = raw_response.get("message")
	return {
		"version": raw_response.get("version") if isinstance(raw_response.get("version"), int) else None,
		"status": raw_response.get("status") if isinstance(raw_response.get("status"), str) else None,
		"data_keys": sorted(str(k) for k in data_obj.keys()) if isinstance(data_obj, dict) else None,
		"has_recipient": bool(isinstance(raw_response.get("recipient"), str) and raw_response["recipient"].strip()),
		"has_message": bool(isinstance(msg_val, str) and msg_val.strip()),
		"notification_action": data_obj.get('action') if isinstance(data_obj, dict) and data_obj.get('action') in ('send', 'skip') else None,
	}


def record_process_execution_log(
	*,
	process_method: str,
	execution_outcome: str,
	execution_context: str = "Workflow Process",
	workflow: str | None = None,
	functional_status: str | None = None,
	output_mode_used: str | None = None,
	arguments: dict[str, Any] | None = None,
	raw_response: Any = None,
	diagnostic_error_type: str | None = None,
	diagnostic_detail: str | None = None,
	exception: BaseException | None = None,
	duration_ms: int | None = None,
	session: str | None = None,
	turn: str | None = None,
	execution_id: str | None = None,
	source_type: str | None = None,
	source_name: str | None = None,
	notification_key: str | None = None,
	source_doctype: str | None = None,
	source_document: str | None = None,
	source_event: str | None = None,
	occurrence: str | None = None,
	existing_log: str | None = None,
	process_definition: str | None = None,
	definition_revision: str | None = None,
	runtime_revision: str | None = None,
	configuration_fingerprint: str | None = None,
	condition_identity: str | None = None,
	output_identity: str | None = None,
	output_flow_action: str | None = None,
	output_retry_parameter: str | None = None,
	source_revision: str | None = None,
	output_template: str | None = None,
	output_template_revision: str | None = None,
) -> Document:
	"""Create a sanitized Process Execution Log entry and link native Frappe Error Log on exceptions."""
	error_log_name = None
	if exception is not None:
		# Preserve the failure chain and locations without storing exception values or locals.
		causes, seen = [], set()
		cause = exception
		while cause is not None and id(cause) not in seen and len(causes) < 8:
			seen.add(id(cause))
			causes.append(type(cause).__name__ + '\n' + '\n'.join(
				f'{frame.filename}:{frame.lineno} in {frame.name}'
				for frame in traceback.extract_tb(cause.__traceback__)[-40:]))
			cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
		err_doc = frappe.log_error(
			title=f"Messaging Process Error: {process_method}",
			message='Process execution failed; exception values omitted.\n' + '\nCaused by:\n'.join(causes),
			defer_insert=False,
		)
		if err_doc and getattr(err_doc, "name", None):
			error_log_name = err_doc.name

	log_doc = frappe.get_doc(
		{
			"doctype": "Process Execution Log",
			"execution_context": execution_context,
			"notification_key": notification_key,
			"source_doctype": source_doctype,
			"source_document": source_document,
			"source_event": source_event,
			"occurrence": occurrence,
			"source_type": source_type or "Bot Flow",
			"source_name": source_name or workflow,
			"workflow": workflow,
			"session": session,
			"turn": turn,
			"execution_id": execution_id,
			"process_method": process_method,
			"process_definition": process_definition,
			"definition_revision": definition_revision,
			"runtime_revision": runtime_revision,
			"configuration_fingerprint": configuration_fingerprint,
			"condition_identity": condition_identity,
			"output_identity": output_identity,
			"output_flow_action": output_flow_action,
			"output_retry_parameter": output_retry_parameter,
			"source_revision": source_revision,
			"output_template": output_template,
			"output_template_revision": output_template_revision,
			"schema_version": 1,
			"execution_outcome": execution_outcome,
			"functional_status": functional_status if execution_outcome == "Functional Status" else None,
			"output_mode_used": output_mode_used or "",
			"duration_ms": duration_ms,
			"diagnostic_error_type": diagnostic_error_type,
			"diagnostic_detail": (diagnostic_detail or "")[:500] if diagnostic_detail else None,
			"frappe_error_log": error_log_name,
			"redacted_arguments_json": json.dumps(redact_sensitive_mapping(arguments), ensure_ascii=False),
			"response_summary_json": json.dumps(
				summarize_process_response_safely(raw_response), ensure_ascii=False
			),
		}
	)
	if existing_log:
		previous = frappe.get_doc('Process Execution Log', existing_log)
		if not notification_key or previous.notification_key != notification_key or previous.execution_outcome != 'Running':
			raise frappe.PermissionError(_('Invalid Process execution receipt'))
		values = log_doc.as_dict()
		for field in log_doc.meta.fields:
			if field.fieldtype not in ('Section Break', 'Column Break'):
				previous.set(field.fieldname, values.get(field.fieldname))
		log_doc = previous
	with _recording():
		if existing_log:
			log_doc.save(ignore_permissions=True)
		else:
			log_doc.insert(ignore_permissions=True)
	return log_doc
