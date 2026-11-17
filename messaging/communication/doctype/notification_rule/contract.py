"""Notification Rule projection and non-conversational Process parameter bindings."""
import json
from decimal import Decimal

import frappe
from frappe import _

from messaging.communication.doctype.communication_provider.communication_provider import (
	provider_definition,
)
from messaging.communication.doctype.communication_template.communication_template import (
	resolve_output,
	validate_template_fields,
)
from messaging.communication.doctype.notification_rule.conditions import condition_fields
from messaging.communication.doctype.process_definition.execution import _decimal_source_value
from messaging.common.primitives import decimal_json
from messaging.registrations.processes import ProcessSchemaValidationError, _input_definitions, _input_value, project_outcomes, validate_process_schema_v1
from messaging.communication.doctype.process_definition.process_definition import bind_definition, registered_contract
from messaging.registry.processes.discovery import (
	consumer_contract,
)
from messaging.communication.doctype.process_definition.validation import (
	_configuration,
	validate_preset_outputs,
)


def synchronize(rule, *, contract=None):
	"""Preserve per-state presentation and Send decisions; never execute a handler."""
	definition = bind_definition(rule)
	if contract is None:
		contract = registered_contract(definition)
	old_contract = json.loads(rule.process_contract or "{}")
	definition_changed = bool(old_contract and old_contract.get("definition") != definition.name)

	for table, identity in (
		("process_parameters", "parameter_name"),
		("outputs", "status_code"),
	):
		for row in rule.get(table) or []:
			stored = (
				frappe.db.get_value(
					row.doctype,
					row.name,
					["parent", "parenttype", "parentfield", identity],
					as_dict=True,
				)
				if row.name
				else None
			)
			if stored and (
				stored.parent,
				stored.parenttype,
				stored.parentfield,
				stored.get(identity),
			) != (rule.name, rule.doctype, table, row.get(identity)):
				raise frappe.PermissionError(
					_("Process configuration row ownership and identity are immutable.")
				)

	contract = project_contract(contract, rule)
	parameters = {item["name"]: item for item in contract["parameters"]}
	outcomes = contract["outcomes"]

	if definition_changed:
		# Process definition changed: reconcile existing inputs and outcomes gracefully
		retained_parameters = []
		for row in rule.process_parameters:
			if row.parameter_name in parameters:
				item = parameters[row.parameter_name]
				if row.internal_type != item["type"] or bool(row.multiple) != item["multiple"]:
					row.fixed_value = None
					row.source_field = None
				row.internal_type = item["type"]
				row.required = int(item["required"])
				row.multiple = int(item["multiple"])
				retained_parameters.append(row)
		rule.set("process_parameters", retained_parameters)

		retained_outputs = []
		for row in rule.outputs:
			if row.status_code in outcomes:
				outcome = outcomes[row.status_code]

				row.output_fields = json.dumps(outcome["fields"], sort_keys=True)
				retained_outputs.append(row)
		rule.set("outputs", retained_outputs)

		# Reset approval receipt since the underlying Process definition changed
		rule.process_test_status = "Untested"
		rule.process_test_fingerprint = None
		rule.process_test_log = None
		rule.process_test_checked_at = None
		rule.process_test_checked_by = None
		rule.process_test_evidence = None

	seen = set()
	for row in list(rule.process_parameters):
		if row.parameter_name not in parameters or row.parameter_name in seen:
			raise frappe.ValidationError(
				_("Remove obsolete or duplicate Process parameter bindings explicitly.")
			)
		seen.add(row.parameter_name)
		item = parameters[row.parameter_name]
		if (
			not definition_changed
			and item["origin"] == "declared"
			and row.internal_type
			and (
				row.internal_type != item["type"]
				or bool(row.required) != item["required"]
				or bool(row.multiple) != item["multiple"]
			)
		):
			raise frappe.ValidationError(
				_("Declared Process parameter properties cannot be changed.")
			)
		row.internal_type, row.required, row.multiple = (
			item["type"],
			int(item["required"]),
			int(item["multiple"]),
		)

	for name in parameters:
		if name not in seen:
			rule.append(
				"process_parameters",
				dict(
					parameter_name=name,
					value_source="Fixed Value",
					internal_type=parameters[name]["type"],
					required=int(parameters[name]["required"]),
					multiple=int(parameters[name]["multiple"]),
				),
			)

	seen = set()
	for row in list(rule.outputs):
		if row.status_code not in outcomes or row.status_code in seen:
			raise frappe.ValidationError(
				_("Remove obsolete or duplicate Process outcomes explicitly.")
			)
		seen.add(row.status_code)
		outcome = outcomes[row.status_code]

		row.output_fields = json.dumps(outcome["fields"], sort_keys=True)

	for status, outcome in outcomes.items():
		if status in seen:
			continue
		values = dict(
			status_code=status,
			output_fields=json.dumps(outcome["fields"], sort_keys=True),
			send=int("template" in outcome["mode"]),
		)

		rule.append("outputs", values)

	for row in rule.outputs:
		if row.send not in (None, 0, 1, '0', '1'):
			raise frappe.ValidationError(_('Send must be a checkbox value.'))
		row.send = int(row.send or 0)
		if row.send and 'template' not in outcomes[row.status_code]['mode']:
			raise frappe.ValidationError(_('Sending notification outcomes must allow Template in the registered Process.'))

	rule.process_contract = json.dumps(
		{**contract, "definition": definition.name}, sort_keys=True, separators=(",", ":")
	)
	return contract


def project_contract(contract, rule):
	declared = [{**item, "origin": "declared"} for item in contract["parameters"]]
	names = {item["name"] for item in declared}
	configured = []
	if contract.get("configurable_parameters"):
		configured = [
			dict(
				name=row.parameter_name,
				type=row.internal_type,
				required=bool(row.required),
				multiple=bool(row.multiple),
				origin="configurable",
			)
			for row in rule.process_parameters
			if row.parameter_name not in names
		]
	inputs = _input_definitions([*declared, *configured])
	return {
		**contract,
		"parameters": inputs,
		"outcomes": project_outcomes(
			contract["outcomes"], inputs, configurable=bool(contract.get("configurable_parameters"))
		),
	}


PARAMETER_FIELD_TYPES = {
	"string": (
		"Data",
		"Small Text",
		"Text",
		"Long Text",
		"Text Editor",
		"Code",
		"Markdown Editor",
		"Link",
		"Select",
		"Dynamic Link",
		"Read Only",
		"Email",
		"Phone",
		"URL",
	),
	"integer": ("Int",),
	"number": ("Currency", "Float", "Percent"),
	"boolean": ("Check",),
	"date": ("Date",),
}


def parameter_fields(meta):
	return condition_fields(
		meta, field_types={kind for kinds in PARAMETER_FIELD_TYPES.values() for kind in kinds}
	)


def _field(meta, name, kind, multiple):
	field = next((field for field in parameter_fields(meta) if field.fieldname == name), None)
	if multiple or not field or field.fieldtype not in PARAMETER_FIELD_TYPES[kind]:
		raise frappe.ValidationError(
			_("Select a source field compatible with the Process parameter type.")
		)
	return field


def _fixed(row, item):
	try:
		value = json.loads(row.fixed_value, parse_float=Decimal)
	except (ValueError, TypeError):
		raise frappe.ValidationError(
			_("Supply a valid JSON scalar or list for the fixed Process value.")
		)
	if item["multiple"]:
		if type(value) is not list or not value:
			raise frappe.ValidationError(_("A multiple Process input requires a non-empty list."))
		return [_input_value(item["type"], v) for v in value]
	return _input_value(item["type"], value)


def validate_parameters(rule, contract, *, require_values=False):
	meta = frappe.get_meta(rule.source_doctype)
	definitions = {item["name"]: item for item in contract["parameters"]}
	for row in rule.process_parameters:
		item = definitions[row.parameter_name]
		configured = (
			(row.fixed_value is not None and row.fixed_value != "")
			if row.value_source == "Fixed Value"
			else bool(row.source_field)
		)
		if not configured:
			if (require_values or rule.enabled) and item["required"]:
				raise frappe.ValidationError(
					_("Configure every required Process parameter before enabling the rule.")
				)
			continue
		if row.value_source == "Fixed Value":
			row.fixed_value = json.dumps(_fixed(row, item), ensure_ascii=False, default=decimal_json)
		elif row.value_source == "Source Field":
			_field(meta, row.source_field, item["type"], item["multiple"])
		else:
			raise frappe.ValidationError(_("Select Fixed Value or Source Field."))


def resolve_parameters(rule, source):
	contract = json.loads(rule.process_contract or "{}")
	definitions = {item["name"]: item for item in contract["parameters"]}
	result = {}
	for row in rule.process_parameters:
		item = definitions[row.parameter_name]
		if row.value_source == "Fixed Value":
			if row.fixed_value is not None and row.fixed_value != "":
				result[row.parameter_name] = _fixed(row, item)
			elif item["required"]:
				raise frappe.ValidationError(
					_("Required Process parameter '{0}' is not configured.").format(row.parameter_name)
				)
		elif row.value_source == "Source Field" and row.source_field:
			_field(
				frappe.get_meta(rule.source_doctype),
				row.source_field,
				item["type"],
				item["multiple"],
			)
			# source is the permission-filtered current snapshot from the existing executor.
			if row.source_field not in source:
				raise frappe.PermissionError(_("The Process source field is not readable."))
			value = source[row.source_field]
			if item["type"] == "number" and value is not None:
				value = _decimal_source_value(source, row.source_field)
			if value is not None:
				if item["type"] == "boolean" and type(value) is int and value in (0, 1):
					value = bool(value)
				result[row.parameter_name] = _input_value(item["type"], value)
			elif item["required"]:
				raise frappe.ValidationError(
					_("Required Process parameter '{0}' is missing in source document.").format(row.parameter_name)
				)
	return result


def require_output_template(row):
    """One documentary requirement shared by save, approval and preparation."""
    if row.send and not row.template_reference:
        frappe.throw(_("Select a communication template for output '{0}'.").format(row.status_code))
    return row.template_reference


def validate_output_templates(rule, contract):
    for row in rule.outputs:
        reference = row.template_reference
        if reference:
            validate_template_fields(reference,
                {name: kind for name, kind in contract['outcomes'][row.status_code]['fields'].items()
                    if name != 'recipient'}, usage='Notification', account=rule.account, active=False)


def require_notifiable_outcomes(contract):
    if not contract['outcomes']:
        frappe.throw(_('The selected Process has no notifiable business outcomes. This rule cannot be validated or enabled.'))


def selected_output(rule, status):
	rows = [row for row in rule.outputs if row.status_code == status]
	if len(rows) != 1:
		raise frappe.ValidationError(_("Process returned an undeclared functional status."))
	row = frappe._dict(rows[0].as_dict())
	if not row.send:
		return row
	require_output_template(row)
	selected = frappe.get_doc('Communication Template', row.template_reference)
	row.language = selected.language
	return row


def _rule_provider(rule):
	if rule.get("provider_context"):
		return rule.provider_context
	if rule.get("account"):
		channel = frappe.db.get_value("Communication Channel Account", rule.account, "channel")
		if channel:
			provider = frappe.db.get_value("Communication Channel", channel, "provider")
			if provider:
				rule.provider_context = provider
				return provider
	return None


def validate_notification_contract(rule):
	contract = project_contract(registered_contract(bind_definition(rule)), rule)
	require_notifiable_outcomes(contract)
	if not rule.outputs:
		frappe.throw(_("Configure at least one output before enabling or validating this Notification Rule."))
	sending_outputs = [row for row in rule.outputs if row.send]
	if not sending_outputs:
		frappe.throw(_("Configure at least one output with Send enabled before enabling or validating this Notification Rule."))
	validate_output_templates(rule, contract)
	if not rule.account:
		frappe.throw(_("Configure a Communication Channel Account before validating this rule."))
	validate_parameters(rule, contract, require_values=True)
	provider = _rule_provider(rule)
	for row in sending_outputs:
		outcome = contract["outcomes"][row.status_code]
		redirected = bool(
			provider
			and "notification_redirect_required" in provider_definition(provider)["capabilities"]
		)
		if not redirected and outcome["fields"].get("recipient") != "string":
			raise frappe.ValidationError(
				_("Notification send must declare a recipient string field.")
			)
		if 'template' not in outcome['mode']:
			raise frappe.ValidationError(_('Sending notification outcomes must allow Template in the registered Process.'))
		require_output_template(row)
		validate_template_fields(row.template_reference,
			{name: kind for name, kind in outcome['fields'].items() if name != 'recipient'},
			usage='Notification', account=rule.account)

	return contract


def _invalid(safe_message=None):
	err = ProcessSchemaValidationError(
		safe_message or "Invalid notification preparation result; values omitted."
	)
	err.safe_administrative_message = safe_message
	raise err


def prepared_data(result, rule, *, administrative_test=False):
	"""Notification extension to Schema v1: send based solely on row.send and exact string template variables."""
	result = validate_process_schema_v1(result)
	data = result["data"]
	row = selected_output(rule, result["status"])
	if not row.send:
		return None
	recipient = result["recipient"]
	provider = _rule_provider(rule)
	mandatory_redirect = bool(
		provider
		and "notification_redirect_required" in provider_definition(provider)["capabilities"]
	)
	if (
		not isinstance(recipient, str)
		or (not recipient.strip() and not mandatory_redirect)
		or recipient != recipient.strip()
		or len(recipient) > 140
		or any(ord(c) < 32 or ord(c) == 127 for c in recipient)
	):
		_invalid(_("Notification Process response 'recipient' is missing or invalid."))
	try:
		output = resolve_output(
			"Template", data=data, template_reference=row.template_reference,
			account=rule.account, usage="Notification",
		)
	except frappe.ValidationError:
		_invalid(_("Notification Output does not satisfy its content contract."))
	return dict(
		recipient=recipient,
		prepared_text=output["text"],
		variables_json=json.dumps(output["variables"], ensure_ascii=False, allow_nan=False),
		template_revision=output["revision"],
	)


@frappe.whitelist(methods=["POST"])
def discover(candidate):
	rule = _configuration(candidate)
	if rule.doctype != "Notification Rule":
		raise frappe.PermissionError(_("Invalid Process source"))
	synchronize(rule)
	return dict(
		definition=rule.process_definition,
		process_method=rule.process_method,
		process_contract=rule.process_contract,
		process_parameters=[r.as_dict() for r in rule.process_parameters],
		outputs=[r.as_dict() for r in rule.outputs],
	)


def validate_default_contract(definition, discovered, *, technical=False):
	"""Validate notification input types and native fixed/source bindings at their owner."""
	declared = {item["name"]: item for item in discovered["parameters"]}
	configured = []
	for name, values in definition.get("inputs", {}).items():
		if name not in declared and not discovered.get("configurable_parameters"):
			frappe.throw(_("Input is not declared by the installed Process: {0}").format(name))
		if name in declared and any(
			field in values for field in ("required", "multiple", "internal_type")
		):
			frappe.throw(_("Declared input requirements and types belong to the Process."))
		item = declared.get(name) or dict(
			name=name,
			type=values.get("internal_type"),
			required=values.get("required", True),
			multiple=values.get("multiple", False),
			origin="configurable",
		)
		if name not in declared:
			configured.append(item)
		row = frappe._dict(values)
		if row.value_source == "Fixed Value" and row.fixed_value:
			row.fixed_value = json.dumps(_fixed(row, item), ensure_ascii=False, default=decimal_json)
		elif row.value_source == "Source Field" and not technical:
			_field(
				frappe.get_meta(definition["options"]["source_doctype"]),
				row.source_field,
				item["type"],
				item["multiple"],
			)
	validate_preset_outputs(definition, discovered, configured)


def default_contract(definition, app):
	"""Validate the installed Process and native initial parameter sources."""
	discovered = consumer_contract(
		definition["process"], app=app, expected_type="notification"
	)
	validate_default_contract(definition, discovered)
	return discovered
