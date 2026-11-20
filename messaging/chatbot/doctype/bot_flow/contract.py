"""Bot Flow's editable projection of the single Python Process contract."""
import hashlib
import json

import frappe
from frappe import _

from messaging.registry.processes import FLOW_ACTIONS, _input_definitions, project_outcomes
from messaging.communication.doctype.process_definition.process_definition import (
    bind_definition,
    registered_contract,
    resolve_definition,
)
from messaging.communication.doctype.process_definition.validation import (
	_configuration,
	validate_preset_outputs,
)


def canonical_contract(contract):
	return json.dumps(contract, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def project_contract(contract, flow):
	"""Combine immutable declarations with additional ordinary capture bindings."""
	declared = [{**item, "origin": "declared"} for item in contract["parameters"]]
	names = {item["name"] for item in declared}
	configured = []
	if contract.get("configurable_parameters"):
		for row in flow.parameters or []:
			name = row.process_argument or row.parameter
			if name in names:
				continue
			parameter = frappe.get_doc("Bot Parameter", row.parameter)
			configured.append(
				dict(
					name=name,
					type=input_type(parameter),
					origin="configurable",
					multiple=bool(row.allow_multiple_values),
					required=bool(row.required),
				)
			)
	inputs = _input_definitions([*declared, *configured])
	return {
		**contract,
		"parameters": inputs,
		"outcomes": project_outcomes(
			contract["outcomes"], inputs, configurable=bool(contract.get("configurable_parameters"))
		),
	}


def synchronize(flow, *, contract=None, replace=False):
	"""Project one definition; preserve manual settings only within its own contract.

	Discovery may replace the active draft. Ordinary saves reject stale projections
	and handler paths instead of laundering another definition's configuration.
	"""
	previous_save = flow.get_doc_before_save()
	definition = resolve_definition(flow.process_definition, expected_type="bot_flow",
		allow_retired=bool(previous_save and previous_save.process_definition == flow.process_definition))
	previous = None if flow.is_new() else frappe.get_doc("Bot Flow", flow.name)
	snapshot = json.loads(flow.get("process_contract") or "{}")
	changed = previous and previous.process_definition != flow.process_definition
	if replace:
		flow.set("parameters", [])
		flow.set("outputs", [])
		flow.set("output_translations", [])
		flow.process_method = None
		flow.process_contract = None
		snapshot = {}
	else:
		if snapshot and snapshot.get("definition") != definition.name:
			raise frappe.ValidationError(
				_(
					"The discovered contract belongs to another Process Definition. Discover the selected definition again."
				)
			)
		if flow.process_method and flow.process_method != definition.method:
			raise frappe.ValidationError(
				_("The handler does not match the selected Process Definition.")
			)
		if changed:
			old_names = {
				row.name
				for table in ("parameters", "outputs", "output_translations")
				for row in previous.get(table) or []
			}
			if any(
				row.name in old_names
				for table in ("parameters", "outputs", "output_translations")
				for row in flow.get(table) or []
			):
				raise frappe.ValidationError(
					_(
						"Rows from the previous Process Definition cannot be reused. Discover the selected definition again."
					)
				)
	bind_definition(flow, allow_retired=True)
	validate_process_assignment(flow, definition)
	if contract is None:
		contract = registered_contract(definition)
	contract = project_contract(contract, flow)
	previous_contract = json.loads(previous.get("process_contract") or "{}") if previous else {}
	contract = {**contract, "definition": definition.name}
	if (
		snapshot
		and snapshot != contract
		and (not previous or snapshot != previous_contract)
		and not contract.get("configurable_parameters")
	):
		raise frappe.ValidationError(
			_("The discovered contract does not match the selected Process Definition.")
		)
	inputs = {item["name"]: item for item in contract["parameters"]}
	outputs = contract["outcomes"]
	seen = set()
	for row in flow.parameters or []:
		name = row.process_argument or row.parameter
		if name not in inputs or name in seen:
			raise frappe.ValidationError(
				_(
					"Input binding needs attention: {0}. Remove the obsolete or duplicate binding explicitly."
				).format(name)
			)
		seen.add(name)
		declared = inputs[name]
		if row.parameter and not compatible(
			frappe.get_doc("Bot Parameter", row.parameter), declared, row
		):
			raise frappe.ValidationError(
				_("Input binding is incompatible with the changed Python contract: {0}").format(name)
			)
		row.process_argument, row.internal_type = name, declared["type"]
		row.allow_multiple_values = int(declared["multiple"])
		if bool(row.required) != declared["required"]:
			raise frappe.ValidationError(_("Process parameter requirements are defined by the contract."))
	for name, item in inputs.items():
		if name not in seen:
			flow.append(
				"parameters",
				dict(
					process_argument=name,
					internal_type=item["type"],
					allow_multiple_values=int(item["multiple"]),
					capture_order=len(flow.parameters) + 1,
					required=int(item["required"]),
				),
			)
	seen = set()
	for row in flow.outputs or []:
		if row.status_code not in outputs or row.status_code in seen:
			raise frappe.ValidationError(
				_(
					"Output configuration needs attention: {0}. Remove the obsolete or duplicate output explicitly."
				).format(row.status_code)
			)
		seen.add(row.status_code)
		row.output_fields = canonical_contract(outputs[row.status_code]["fields"])
		reconcile_output(row, outputs[row.status_code], previous_contract)
	for status, outcome in outputs.items():
		if status not in seen:
			flow.append(
				"outputs",
				dict(
					status_code=status,
					output_fields=canonical_contract(outcome["fields"]),
					output_mode=outcome["mode"][0].title(),
					flow_action=declared_action(outcome),
				),
			)
	flow.process_contract = canonical_contract(contract)
	flow.process_contract_fingerprint = hashlib.sha256(flow.process_contract.encode()).hexdigest()
	flow.has_parameters = int(bool(flow.parameters))
	return contract


def declared_action(outcome):
	return FLOW_ACTIONS[outcome["action"]]


def reconcile_output(row, outcome, previous_contract):
	previous = previous_contract.get("outcomes", {}).get(row.status_code)
	if (row.output_mode or "").lower() not in outcome["mode"]:
		if previous and (row.output_mode or "").lower() in previous["mode"]:
			row.output_mode = outcome["mode"][0].title()
		else:
			raise frappe.ValidationError(_("The selected output mode is not declared by the Process."))
	action = declared_action(outcome)
	if row.flow_action and row.flow_action != action and previous == outcome:
		raise frappe.ValidationError(_("Flow actions are defined by the Process."))
	row.flow_action = action
	if action != "Retry Parameter":
		row.retry_parameter = None


def compatible(parameter, declared, row):
	kind = input_type(parameter)
	return kind == declared["type"] and bool(row.allow_multiple_values) == declared["multiple"]


def input_type(parameter):
	return {
		"Int": "integer",
		"Decimal": "number",
		"Boolean": "boolean",
		"Date": "date",
		"String": "string",
		"Name": "string",
		"Expression": "string",
	}.get(parameter.parameter_type)


def validate_bindings(flow):
	contract = json.loads(flow.process_contract)
	inputs = {item["name"]: item for item in contract["parameters"]}
	for row in flow.parameters:
		if not row.parameter or not compatible(
			frappe.get_doc("Bot Parameter", row.parameter), inputs[row.process_argument], row
		):
			raise frappe.ValidationError(
				_("Select a compatible Bot Parameter for input {0}.").format(row.process_argument)
			)


@frappe.whitelist(methods=["POST"])
def discover(candidate):
	flow = _configuration(candidate)
	if flow.doctype != "Bot Flow":
		raise frappe.PermissionError(_("Invalid Process source"))
	previous = None if flow.is_new() else frappe.get_doc("Bot Flow", flow.name)
	contract = synchronize(
		flow, replace=not previous or previous.process_definition != flow.process_definition
	)
	return dict(
		definition=flow.process_definition,
		contract=contract,
		parameters=[row.as_dict() for row in flow.parameters],
		outputs=[row.as_dict() for row in flow.outputs],
		process_method=flow.process_method,
		process_contract=flow.process_contract,
		process_contract_fingerprint=flow.process_contract_fingerprint,
	)


def validate_process_assignment(flow, definition=None, *, current_read=False):
	"""The database unique key arbitrates concurrent saves; validation explains the conflict."""
	if not flow.get("process_definition"):
		raise frappe.ValidationError(_("Select an available registered Process."))
	assigned = frappe.db.get_value(
		"Bot Flow",
		{"process_definition": flow.process_definition},
		["name", "workflow_name"],
		as_dict=True,
		for_update=current_read,
	)
	if assigned and assigned.name != flow.name:
		identifier = (
			definition.process_id
			if definition
			else frappe.db.get_value("Process Definition", flow.process_definition, "process_id")
		)
		raise frappe.ValidationError(
			_(
				'Process definition "{0}" is already assigned to Bot Flow "{1}". It cannot be used in another flow.'
			).format(identifier or flow.process_definition, assigned.workflow_name or assigned.name)
		)


def validate_default_contract(definition, discovered, *, technical=False):
	"""Validate Flow capture bindings and retry semantics against the real Process."""
	declared = {item["name"]: item for item in discovered["parameters"]}
	configured = []
	for name, item in definition.get("inputs", {}).items():
		if name not in declared and not discovered.get("configurable_parameters"):
			frappe.throw(_("Input is not declared by the installed Process: {0}").format(name))
		if name in declared and any(field in item for field in ("required", "allow_multiple_values")):
			frappe.throw(_("Declared input requirements and types belong to the Process."))
		spec = item["parameter"]
		code = spec["code"] if isinstance(spec, dict) else spec
		bound = None
		if not technical and not isinstance(spec, dict) and frappe.db.exists("Bot Parameter", code):
			bound = frappe.get_doc("Bot Parameter", code)
			bound.check_permission("read")
		if not bound and not isinstance(spec, dict):
			if technical:
				# An existing Link is resolved by this owner at administrative provisioning.
				if name not in declared:
					frappe.throw(
						"Configurable capture inputs require an initial typed parameter definition."
					)
				parameter_type = declared[name]["type"]
			else:
				frappe.throw(_("Missing Bot Parameter: {0}").format(code))
		else:
			parameter_type = input_type(bound or frappe._dict(spec))
		if not parameter_type or (name in declared and parameter_type != declared[name]["type"]):
			frappe.throw(_("The capture parameter is incompatible with the Process: {0}").format(name))
		if name not in declared:
			configured.append(
				dict(
					name=name,
					type=parameter_type,
					required=item.get("required", True),
					multiple=item.get("allow_multiple_values", False),
					origin="configurable",
				)
			)
	if set(declared) - set(definition.get("inputs", {})):
		frappe.throw(_("Every declared capture input needs an initial Bot Parameter binding."))
	outcomes = validate_preset_outputs(definition, discovered, configured)
	for status, output in definition["outcomes"].items():
		if output.get("retry_parameter") and outcomes[status].get("action") != "retry_parameter":
			frappe.throw(_("Only Retry Parameter outcomes can select a retry input."))
		if outcomes[status].get("action") == "retry_parameter" and output.get(
			"retry_parameter"
		) not in definition.get("inputs", {}):
			frappe.throw(_("Select a configured retry input."))


def default_contract(definition, app):
	"""Validate the installed, readable Process and this Flow's initial bindings."""
	process = resolve_definition(definition["process"], expected_type="bot_flow")
	process.check_permission("read")
	discovered = registered_contract(process)
	validate_default_contract(definition, discovered)
	return discovered
