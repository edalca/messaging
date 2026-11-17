"""Pure native parameter definition validation shared by declarations and documents."""
import keyword
import re
import frappe
from frappe import _
from .parameters import get_handler, get_supported_types

CANONICAL_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

def validate_identity(self) -> None:
	code = self.name or ""
	from messaging.registrations.processes import validate_captured_process_argument
	validate_captured_process_argument(code)
	if not CANONICAL_CODE_PATTERN.fullmatch(code) or keyword.iskeyword(code):
		frappe.throw(
			_(
				"Parameter Name must be a lowercase identifier (2-64 characters) and cannot be a Python keyword."
			),
			frappe.ValidationError,
		)

def validate_label(self) -> None:
	if not (self.label or "").strip():
		frappe.throw(_("Parameter Label is required."), frappe.ValidationError)

def validate_type(self) -> None:
	if self.parameter_type not in get_supported_types():
		frappe.throw(
			_("Parameter Type must be one of: {0}.").format(", ".join(get_supported_types())),
			frappe.ValidationError,
		)


def validate_definition(parameter):
    validate_identity(parameter)
    validate_label(parameter)
    validate_type(parameter)
    handler = get_handler(parameter.parameter_type)
    handler.validate_definition(parameter)
    if not handler.allows_expression and parameter.expression not in (None, ''):
        frappe.throw(_("Expression must be empty for other parameter types. Clear the patterns explicitly before changing the type."))


def validate_preset_parameter(declaration):
    """Validate an initial parameter mapping with the native definition rules."""
    if not isinstance(declaration, dict) or not isinstance(declaration.get('code'), str):
        frappe.throw(_('Initial parameter definition is incomplete.'), frappe.ValidationError)
    values = {key: value for key, value in declaration.items() if key not in ('code', 'languages')}
    validate_definition(frappe._dict(values, name=declaration['code']))
