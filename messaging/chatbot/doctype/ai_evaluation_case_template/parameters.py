"""Native evaluation parameter restrictions shared by configuration and document validation."""
import frappe
from messaging.common.immutable_configuration import _invalid
from messaging.chatbot.doctype.bot_parameter.parameters import get_handler, get_supported_types
from messaging.chatbot.doctype.bot_parameter.parameters.engine import validate_input_limits


PARAMETER_FIELDS = {
	"String": {"string_mode", "min_length", "max_length"},
	"Name": {"min_length", "max_length", "allow_contextual_name_extraction"},
	"Int": {"min_value", "max_value"},
	"Date": set(),
	"Expression": {"expression"},
}

def parameter_contract(row):
	"""Use the same native definitions and cardinality validator as Bot Parameter."""
	kind = row.parameter_type
	if kind not in get_supported_types():
		_invalid("Select a valid native parameter type.")
	definition = {"label": row.label or row.ref}
	for key in set().union(*PARAMETER_FIELDS.values()):
		value = row.get(key)
		if key not in PARAMETER_FIELDS[kind]:
			if value not in (None, "", 0):
				_invalid("Parameter restrictions must match the selected native type.")
			continue
		if key in ("min_value", "max_value"):
			if row.get("has_" + key):
				if type(value) is not int:
					_invalid("Parameter bounds must be whole numbers.")
				definition[key] = value
			elif value not in (None, "", 0):
				_invalid("Enable the integer bound before configuring its value.")
		elif value not in (None, "", 0):
			definition[key] = (
				bool(value) if key == "allow_contextual_name_extraction" else value
			)
	if kind != "Int" and (row.has_min_value or row.has_max_value):
		_invalid("Parameter restrictions must match the selected native type.")
	if not isinstance(definition["label"], str) or not definition["label"].strip():
		_invalid("Parameter labels are required.")
	for key in ("min_length", "max_length", "min_value", "max_value"):
		if key in definition and type(definition[key]) is not int:
			_invalid("Parameter bounds must be whole numbers.")
	for key in (
		"allow_multiple_values",
		"has_min_value",
		"has_max_value",
		"allow_contextual_name_extraction",
	):
		if row.get(key) not in (None, 0, 1):
			_invalid("Parameter flags must be booleans.")
	parameter = frappe._dict(
		dict(
			definition,
			parameter_type=kind,
			allow_ai_extraction=0,
		)
	)
	get_handler(kind).validate_definition(parameter)
	cardinality = dict(
		allow_multiple_values=bool(row.allow_multiple_values),
		minimum_values=row.minimum_values or 0,
		maximum_values=row.maximum_values or 0,
	)
	validate_input_limits(cardinality)
	return dict(ref=row.ref, type=kind, parameter=definition, cardinality=cardinality)
