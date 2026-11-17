"""Process result schema, technical revisions and administrative evidence."""
from contextlib import nullcontext

import hashlib
import json
import time
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import now_datetime
from redis.exceptions import RedisError

from messaging.chatbot.doctype.bot_settings.language import get_bot_settings, resolve_process_language, presentation_language
from messaging.communication.doctype.communication_template.communication_template import (
	extract_component_variables,
)
from messaging.communication.doctype.process_definition.execution import execute_process
from messaging.registrations.processes import FIELD_TYPES, ProcessSchemaValidationError, REQUIRED_PROCESS_SCHEMA_KEYS, STATUS_CODE_PATTERN, SUPPORTED_PROCESS_SCHEMA_VERSION, _field_value, _identifier, _input_definitions, _input_value, configurable_output_fields, project_outcomes, validate_captured_process_argument, validate_definition_signature, validate_handler_signature, validate_output_data, validate_process_schema_v1
from messaging.communication.doctype.process_definition.process_definition import (
    ProcessUnavailableError,
    bind_definition,
    definition_revision,
    validate_process_method_path,
)
from messaging.registry.processes.discovery import (
	inspect_process,
)
from messaging.security.permissions import (
	require_system_manager,
)


RECEIPT_FIELDS = (
	"process_test_status",
	"process_test_fingerprint",
	"process_test_checked_at",
	"process_test_checked_by",
	"process_test_log",
	"process_test_evidence",
)
PARAMETER_FIELDS = (
	"enabled",
	"parameter_type",
	"expression",
	"string_mode",
	"min_length",
	"max_length",
	"min_value",
	"max_value",
	"label",
	"description",
)
FLOW_FIELDS = (
	"process_definition",
	"process_method",
	"process_invocation_mode",
	"process_schema_version",
)
APPROVAL_TTL = 900
CONSUMERS = ("Bot Flow", "Notification Rule")

RULE_FIELDS = (
	"process_definition",
	"process_method",
	"process_invocation_mode",
	"source_application",
	"source_doctype",
	"account",
	"redirect_recipient",
)


def _fields(doc, names):
	return {name: doc.get(name) for name in names}


def fingerprint(doc, *, source_name=None, include_schedule=True, definition=None):
	"""Bind approval to the registered revision and current consumer dependencies."""
	definition = definition or bind_definition(doc)
	# Runtime language selection does not change the validated Process interface.
	config = dict(
		contract=6,
		source_type=doc.doctype,
		source_name=doc.name if source_name is None else source_name,
	)
	if definition:
		config["definition"] = definition_revision(definition)
		config["capabilities"] = definition.capability_revision
	config.update(doc._consumer_fingerprint(include_schedule=include_schedule))
	return hashlib.sha256(
		json.dumps(config, sort_keys=True, ensure_ascii=False, default=str).encode()
	).hexdigest()


def runtime_evidence(doc, definition, *, administrative_test=False):
	return dict(
		process_definition=definition.name,
		definition_revision=definition_revision(definition),
		runtime_revision=_registered_runtime(definition),
		configuration_fingerprint=_temporary_fingerprint(doc)
		if administrative_test
		else fingerprint(doc),
	)


from .revisions import runtime_sources, runtime_revision


def _registered_runtime(definition):
	from messaging.communication.doctype.process_definition.process_definition import registered_snapshot
	return registered_snapshot(definition)["runtime"]


def _definition_revision(doc):
	return definition_revision(bind_definition(doc))


def _scope(doc):
	if doc.is_new():
		return "create:" + (doc.get("workflow_code") or doc.get("rule_name") or "").strip()
	return "document:" + doc.name


def _temporary_fingerprint(doc):
	return fingerprint(doc, source_name=_scope(doc) if doc.is_new() else None)


def _revision(doc):
	return None if doc.is_new() else str(frappe.db.get_value(doc.doctype, doc.name, "modified") or "")


def _approval_identity(doc):
	require_system_manager()
	sid = frappe.session.get("sid")
	if not isinstance(sid, str) or not sid or sid == "Guest":
		raise frappe.PermissionError(
			_("An active administrative session is required to validate and save the Process method.")
		)
	return dict(
		user=frappe.session.user,
		session=hashlib.sha256(sid.encode()).hexdigest(),
		doctype=doc.doctype,
		scope=_scope(doc),
		fingerprint=_temporary_fingerprint(doc),
	)


def _approval_key(identity):
	return frappe.cache.make_key(
		"messaging:process_approval:"
		+ hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
	)


def _redis_error():
	raise frappe.ValidationError(
		_("Temporary Process validation is unavailable. Try validating the method again before saving.")
	)


def _read_approval(doc):
	identity = _approval_identity(doc)
	key = _approval_key(identity)
	try:
		raw = frappe.cache.get(key)
		receipt = json.loads(raw) if raw else None
	except (RedisError, ValueError, TypeError):
		_redis_error()
	if (
		not receipt
		or any(receipt.get(k) != v for k, v in identity.items())
		or receipt.get("status") != "Passed"
		or receipt.get("expires_at", 0) <= time.time()
		or receipt.get("revision") != _revision(doc)
		or not _log_current(
			doc, receipt.get("execution_log"), temporary=True, current_fingerprint=identity["fingerprint"]
		)
	):
		return key, None
	return key, receipt


def _delete_approval(key, expected=None):
	if expected is None:
		frappe.cache.delete(key)
	else:
		frappe.cache.eval(
			"if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0",
			1,
			key,
			expected,
		)


def _log_current(doc, name, *, temporary=False, current_fingerprint=None, definition=None):
	if not name or not frappe.db.exists("Process Execution Log", name):
		return False
	log = frappe.get_doc("Process Execution Log", name)
	creation_scope = "create:" + (doc.get("workflow_code") or doc.get("rule_name") or "")
	candidate_name = "candidate-" + hashlib.sha256(creation_scope.encode()).hexdigest()
	source_name = creation_scope if log.source_name == candidate_name else None
	return (
		log.execution_context == "Administrative Test"
		and log.execution_outcome == "Functional Status"
		and log.functional_status == "test"
		and log.source_type == doc.doctype
		and log.source_name in (doc.name, candidate_name)
		and log.process_definition == doc.get("process_definition")
		and log.process_method == doc.process_method
		and log.definition_revision == definition_revision(definition or bind_definition(doc))
		and log.runtime_revision == _registered_runtime(definition or bind_definition(doc))
		and log.configuration_fingerprint
		== (
			fingerprint(doc, source_name=source_name)
			if source_name
			else current_fingerprint
			if current_fingerprint is not None
			else _temporary_fingerprint(doc)
			if temporary
			else fingerprint(doc)
		)
	)


def test_state(doc, *, remember_invalid=False):
	doc.flags.process_verification_error = None
	if not doc.get("process_definition"):
		return "Untested"
	try:
		definition = bind_definition(doc)
	except ProcessUnavailableError:
		return "Unavailable"
	if doc.get("process_test_status") not in ("Passed", "Failed"):
		return "Untested"
	key = _invalidation_key(doc) if doc.doctype == "Bot Flow" and doc.get("process_test_log") else None
	try:
		if key and frappe.cache.get(key):
			return "Untested"
		current = fingerprint(doc, definition=definition)
		if doc.doctype == "Notification Rule" and doc.get("process_test_status") == "Failed":
			if doc.process_test_fingerprint != current:
				return "Untested"
			try:
				evidence = json.loads(doc.get("process_test_evidence") or "{}")
			except (ValueError, TypeError):
				evidence = {}
			if evidence.get("error"):
				doc.flags.process_verification_error = evidence["error"]
			return "Failed"
		valid = doc.process_test_fingerprint == current and _log_current(
			doc, doc.get("process_test_log"), current_fingerprint=current, definition=definition
		)
		if not valid and key and remember_invalid:
			frappe.cache.set(key, "invalid")
		return doc.process_test_status if valid else "Untested"
	except ProcessUnavailableError:
		return "Unavailable"
	except (
		ValueError,
		OSError,
		ImportError,
		TimeoutError,
		frappe.ValidationError,
		frappe.PermissionError,
		RedisError,
	):
		doc.flags.process_verification_error = _(
			"The Process contract could not be verified. Review its discovery before validating or activating the Flow."
		)
		if key and remember_invalid:
			try:
				frappe.cache.set(key, "invalid")
			except RedisError:
				pass
		return "Untested"


def _approval_message(doc):
	return (
		_("Save and validate the Flow before activating it.")
		if doc.doctype == "Bot Flow"
		else _("Validate the Process structure and administrative configuration before saving.")
	)


def require_approved(doc):
	if test_state(doc, remember_invalid=True) != "Passed":
		raise frappe.ValidationError(_approval_message(doc))


def output_token(row):
	return hashlib.sha256(row.status_code.encode()).hexdigest()


def output_states(flow):
	valid = test_state(flow) == "Passed"
	key, receipt = _read_approval(flow) if not valid else (None, None)
	return [
		dict(
			output=output_token(row),
			status_code=row.status_code,
			output_mode=row.output_mode,
			flow_action=row.flow_action,
			status="Passed" if valid or receipt else "Untested",
			execution_log=flow.process_test_log
			if valid
			else receipt["execution_log"]
			if receipt
			else None,
			expires_at=receipt["expires_at"] if receipt else None,
		)
		for row in flow.outputs or []
	]


def condition_states(rule):
	valid = test_state(rule) == "Passed"
	key, receipt = _read_approval(rule) if not valid else (None, None)
	return [
		dict(
			condition=token,
			**c,
			status="Passed" if valid or receipt else "Untested",
			expires_at=receipt["expires_at"] if receipt else None,
		)
		for token, c in rule._canonical_conditions_with_tokens()
	]


def clear_approval(doc):
	"""Discard the current receipt without modifying its historical execution log."""
	for name in RECEIPT_FIELDS:
		doc.set(name, "Untested" if name == "process_test_status" else None)


def validate_readiness(doc, *, enabled):
	previous = None if doc.is_new() else frappe.get_doc(doc.doctype, doc.name)
	for name in RECEIPT_FIELDS:
		doc.set(name, previous.get(name) if previous else None)
	if test_state(doc) == "Passed":
		return
	if doc.doctype == "Bot Flow" and not doc._allows_receipt_promotion():
		clear_approval(doc)
		doc.status = "Disabled"
		return
	clear_approval(doc)
	required = bool(enabled)
	if not required and not frappe.session.get("sid"):
		return
	doc.check_permission("create" if doc.is_new() else "write")
	key, receipt = _read_approval(doc)
	if not receipt:
		if required:
			raise frappe.ValidationError(_approval_message(doc))
		return
	doc.update(
		dict(
			process_test_status="Passed",
			process_test_fingerprint=(
				receipt["fingerprint"] if doc.doctype == "Bot Flow" else fingerprint(doc)
			),
			process_test_checked_at=receipt["checked_at"],
			process_test_checked_by=receipt["user"],
			process_test_log=receipt["execution_log"],
			process_test_evidence=json.dumps(
				dict(scope="structural_contract", log=receipt["execution_log"])
			),
		)
	)
	# Native transaction owns persistence; temporary evidence survives a rollback.
	frappe.db.after_commit.add(lambda: _delete_approval(key, json.dumps(receipt, sort_keys=True)))


def _configuration(candidate, *, for_update=False, persisted_only=False):
	"""Construct an untrusted Desk candidate using metadata, never client attributes or flags."""
	require_system_manager()
	if isinstance(candidate, str):
		candidate = json.loads(candidate)
	if not isinstance(candidate, dict) or candidate.get("doctype") not in CONSUMERS:
		frappe.throw(_("Invalid Process source"), frappe.PermissionError)
	doctype = candidate["doctype"]
	name = candidate.get("name")
	new = bool(candidate.get("__islocal"))
	if persisted_only and new:
		raise frappe.ValidationError(_("Save the Flow before validating or changing its activation."))
	if for_update and persisted_only and doctype == "Bot Flow":
		# Settings saves lock this metadata row before Singles. Keep the same
		# order for explicit approval/activation, including concurrent requests.
		locked_settings = get_bot_settings(for_update=True, shared=True)
		if str(locked_settings.modified) != str(get_bot_settings().modified):
			raise frappe.TimestampMismatchError(
				_("Configuration changed. Reload before validating and saving the method.")
			)
	if new:
		doc = frappe.new_doc(doctype)
		doc.check_permission("create")
	else:
		doc = frappe.get_doc(doctype, name, for_update=for_update)
		doc.check_permission("read")
		doc.check_permission("write")
		if str(candidate.get("modified") or "") != str(doc.modified or ""):
			frappe.throw(
				_("Configuration changed. Reload before validating and saving the method."),
				frappe.TimestampMismatchError,
			)
	stored_children = {(row.doctype, row.name): row for row in doc.get_all_children()} if not new else {}

	def copy_fields(meta, values):
		stored = stored_children.get((meta.name, values.get("name")))
		result = (
			{"name": values["name"]}
			if meta.name == "Communication Provider Value" and values.get("name")
			else {}
		)
		if stored:
			result["name"] = stored.name
			result.update(
				{
					field.fieldname: stored.get(field.fieldname)
					for field in meta.fields
					if field.read_only
					and field.fieldtype not in ("Section Break", "Column Break", "Tab Break", "HTML", "Button")
				}
			)
		for field in meta.fields:
			if (
				field.fieldname in RECEIPT_FIELDS
				or (
					field.read_only
					and (meta.name, field.fieldname)
					not in (
						("Flow Parameter", "process_argument"),
						("Flow Output", "status_code"),
						("Flow Output", "flow_action"),
						("Notification Process Parameter", "parameter_name"),
						("Notification Outcome", "status_code"),
					)
				)
				or field.fieldtype in ("Section Break", "Column Break", "Tab Break", "HTML", "Button")
			):
				continue
			if field.fieldname not in values:
				# Desk omits null fields, including those in child rows. A missing
				# persisted value is null, not an unsaved edit; non-null omissions
				# still differ from the stored document and must be rejected.
				if persisted_only:
					result[field.fieldname] = None
				continue
			value = values[field.fieldname]
			if field.fieldtype in ("Table", "Table MultiSelect"):
				if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
					frappe.throw(_("Invalid Process argument"))
				value = [copy_fields(frappe.get_meta(field.options), row) for row in value]
			result[field.fieldname] = value
		return result

	values = copy_fields(doc.meta, candidate)
	if persisted_only and json.dumps(values, sort_keys=True, default=str) != json.dumps(
		copy_fields(doc.meta, doc.as_dict()), sort_keys=True, default=str
	):
		raise frappe.ValidationError(_("Save pending changes before validating or changing activation."))
	doc.update(values)
	if new:
		doc.name = "candidate-" + hashlib.sha256(_scope(doc).encode()).hexdigest()
	doc.set_parent_in_children()
	# Do not allow writable higher-permission fields to bypass native document permissions.
	doc.validate_higher_perm_levels()
	if for_update and persisted_only and doctype == "Bot Flow":
		# A receipt is promoted only while its persisted dependencies cannot be
		# edited. Reject an older repeatable-read snapshot after waiting on a lock.
		dependencies = {("Process Definition", doc.process_definition)}
		dependencies.update(("Bot Parameter", row.parameter) for row in doc.parameters if row.parameter)
		dependencies.update(
			("Bot Flow Template", row.template_reference)
			for row in doc.output_translations
			if row.template_reference
		)
		for dependency_type, dependency_name in sorted(dependencies):
			locked = frappe.get_doc(dependency_type, dependency_name, for_update=True)
			if str(locked.modified) != str(frappe.get_doc(dependency_type, dependency_name).modified):
				raise frappe.TimestampMismatchError(
					_("Configuration changed. Reload before validating and saving the method.")
				)
	return doc


def _validate_candidate(doc):
	doc._validate_process_candidate()
	if doc.doctype == "Bot Flow":
		resolve_process_language()
	doc._action = "save"
	# Use native read-only validation; _validate also stores passwords/files and syncs autoname.
	doc._validate_mandatory()
	for row in [doc, *doc.get_all_children()]:
		row._validate_data_fields()
		row._validate_selects()
		row._validate_non_negative()
		row._validate_length()
	doc._validate_links()


@frappe.whitelist(methods=["POST"])
def describe(candidate):
	doc = _configuration(candidate)
	error = None
	try:
		_validate_candidate(doc)
	except (frappe.ValidationError, frappe.PermissionError) as exc:
		error = str(exc)
	state, expires = ("Untested", None) if error else (test_state(doc), None)
	if not error and state != "Passed":
		key, receipt = _read_approval(doc)
		if receipt:
			state, expires = "Passed", receipt["expires_at"]
	return dict(
		fields=[],
		status=state,
		expires_at=expires,
		configuration_error=error,
		validation_scope="structural_contract",
		outputs=output_states(doc) if doc.doctype == "Bot Flow" and not error else [],
		conditions=condition_states(doc) if doc.doctype == "Notification Rule" and not error else [],
	)


def validate_notification_contract(rule):
	return rule.validate_notification_contract()


@frappe.whitelist(methods=["POST"])
def test_method(candidate, sample=None):
	"""Structural validation only. No business samples, callbacks or effects execute."""
	is_flow = (
		(json.loads(candidate) if isinstance(candidate, str) else candidate).get("doctype") == "Bot Flow"
	)
	if is_flow and not frappe.new_doc("Bot Flow")._allows_receipt_promotion():
		raise frappe.PermissionError(_("Use the saved Flow Validate action."))
	doc = _configuration(candidate, for_update=True, persisted_only=is_flow)
	_validate_candidate(doc)
	if doc.doctype == "Notification Rule":
		validate_notification_contract(doc)
	identity, revision = _approval_identity(doc), _revision(doc)
	key = _approval_key(identity)
	try:
		_delete_approval(key)
	except RedisError:
		_redis_error()
	with presentation_language(doc.process_language(), notification=True) if doc.doctype == 'Notification Rule' else nullcontext():
		result = execute_process(
			process_method="",
			arguments={},
			source_type=doc.doctype,
			source_name=doc.name,
			candidate=doc,
			administrative_test=True,
			log_context=dict(execution_context="Administrative Test"),
		)
	if identity != _approval_identity(doc) or revision != _revision(doc):
		raise frappe.TimestampMismatchError(
			_("Configuration changed. Reload before validating and saving the method.")
		)
	passed = bool(result["functional"] and result["status"] == "test")
	expires = None
	if passed:
		expires = time.time() + APPROVAL_TTL
		receipt = dict(
			identity,
			revision=revision,
			status="Passed",
			checked_at=str(now_datetime()),
			expires_at=expires,
			execution_log=result["execution_log"],
		)
		try:
			if not frappe.cache.set(key, json.dumps(receipt, sort_keys=True), ex=APPROVAL_TTL):
				_redis_error()
		except RedisError:
			_redis_error()
	return dict(
		status="Passed" if passed else "Failed",
		functional_status="test" if passed else None,
		validation_scope="structural_contract",
		technical_valid=passed,
		preparation_complete=False,
		event_proven=False,
		delivered=False,
		expires_at=expires,
		execution_log=result["execution_log"],
		message=_(
			"Process structure and administrative configuration validated. Business logic was not executed."
		)
		if passed
		else result.get("diagnostic"),
		summary=dict(scope="structural_contract"),
		inspection=result.get("inspection"),
	)


def _invalidation_key(doc):
	"""A rejected receipt can never regain validity by reverting code or rolling back a request."""
	return frappe.cache.make_key("messaging:process_invalidated:" + doc.process_test_log)


def validate_preset_outputs(definition, discovered, configured):
	"""Project declared outcomes with the existing Process validators for both consumers."""
	if set(definition["outcomes"]) != set(discovered["outcomes"]):
		frappe.throw(_("Preset outcomes do not match the installed Process."))
	effective = _input_definitions(
		[{**item, "origin": "declared"} for item in discovered["parameters"]] + configured
	)
	outcomes = project_outcomes(
		discovered["outcomes"], effective, configurable=bool(discovered.get("configurable_parameters"))
	)
	for status, output in definition["outcomes"].items():
		outcome = outcomes[status]
		if output["mode"] not in outcome["mode"] and output.get("send", True):
			frappe.throw(_("The output mode is not allowed by the Process: {0}").format(status))
		if output.get("template"):
			for content in definition["templates"][output["template"]]["languages"].values():
				for component, text in content.items():
					for variable in extract_component_variables(text, component.title()):
						if ((variable not in outcome["fields"] or variable == "recipient") if "send" in output
							else outcome["fields"].get(variable) not in ("string", "nullable_string")):
							frappe.throw(
								_("Template variable is not a declared string output: {0}").format(variable)
							)
	return outcomes
