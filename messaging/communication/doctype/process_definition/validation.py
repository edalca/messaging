"""Administrative tests and configuration projections for Process consumers."""
from contextlib import nullcontext
import json

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_settings.language import resolve_process_language, presentation_language
from messaging.communication.doctype.communication_template.communication_template import extract_component_variables
from messaging.registry.processes import _input_definitions, project_outcomes
from messaging.security.permissions import require_system_manager

from . import approval
from .approval import APPROVAL_FIELDS, CONSUMER_TYPES


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
RULE_FIELDS = (
	"process_definition",
	"process_method",
	"process_invocation_mode",
	"source_application",
	"source_doctype",
)


def _fields(doc, names):
	return {name: doc.get(name) for name in names}


def _configuration(candidate):
	"""Construct an untrusted Desk candidate using metadata, never client attributes or flags."""
	require_system_manager()
	if isinstance(candidate, str):
		candidate = json.loads(candidate)
	if not isinstance(candidate, dict) or candidate.get("doctype") not in CONSUMER_TYPES:
		frappe.throw(_("Invalid Process source"), frappe.PermissionError)
	doctype = candidate["doctype"]
	name = candidate.get("name")
	new = bool(candidate.get("__islocal"))
	if new:
		doc = frappe.new_doc(doctype)
		doc.check_permission("create")
	else:
		doc = frappe.get_doc(doctype, name)
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
				field.fieldname in APPROVAL_FIELDS
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
				continue
			value = values[field.fieldname]
			if field.fieldtype in ("Table", "Table MultiSelect"):
				if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
					frappe.throw(_("Invalid Process argument"))
				value = [copy_fields(frappe.get_meta(field.options), row) for row in value]
			result[field.fieldname] = value
		return result

	values = copy_fields(doc.meta, candidate)
	doc.update(values)
	if new:
		doc.name = "candidate-" + frappe.generate_hash(length=20)
	doc.set_parent_in_children()
	# Do not allow writable higher-permission fields to bypass native document permissions.
	doc.validate_higher_perm_levels()
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


def run_test(doc, definition):
	"""Check the saved configuration and discover the registered contract."""
	from .execution import execute_process

	persisted_hash = (approval.contract_hash(doc), approval.delivery_hash(doc))
	_validate_candidate(doc)
	if (approval.contract_hash(doc), approval.delivery_hash(doc)) != persisted_hash:
		raise frappe.ValidationError(_("Save the current Process configuration before validating."))
	with presentation_language(doc.process_language(), notification=True) if doc.doctype == "Notification Rule" else nullcontext():
		result = execute_process(
			process_method=definition.method,
			arguments={},
			source_type=doc.doctype,
			source_name=doc.name,
			candidate=doc,
			administrative_test=True,
			log_context=dict(execution_context="Administrative Test"),
		)
	passed = bool(result["functional"] and result["status"] == "test")
	return dict(
		status="Passed" if passed else "Failed",
		functional_status="test" if passed else None,
		execution_log=result["execution_log"],
		message=result.get("diagnostic") if not passed else None,
	)


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


def action_identity(candidate, doctype):
	"""Only a saved document identity and its expected revision authorize an action."""
	require_system_manager()
	candidate = frappe.parse_json(candidate)
	if (
		not isinstance(candidate, dict)
		or candidate.get("doctype", doctype) != doctype
		or candidate.get("__islocal")
		or not isinstance(candidate.get("name"), str)
		or not candidate.get("modified")
	):
		raise frappe.ValidationError(_("Save pending changes before changing Status."))
	return candidate["name"], candidate["modified"]
