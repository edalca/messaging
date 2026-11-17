"""Pure template syntax shared by typed declarations and stored content."""
import re
import frappe
from frappe import _

TEMPLATE_VARIABLE_PATTERN = re.compile(r"^[a-z]+(?:_[a-z]+)*$", re.ASCII)
TEMPLATE_PLACEHOLDER_PATTERN = re.compile(r"\{\{([a-z]+(?:_[a-z]+)*)\}\}", re.ASCII)
RAW_TOKEN_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
TEMPLATE_TOKEN_PATTERN = RAW_TOKEN_PATTERN
SIMPLE_VARIABLE_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def validate_template_syntax(
	template_text: str | None,
	*,
	allow_empty: bool = False,
	allowed_variables: set[str] | None = None,
) -> list[str]:
	"""Validate a '{{variable}}' template string and return its ordered list of referenced variable names."""
	if not isinstance(template_text, str) or not template_text.strip():
		if allow_empty:
			return []
		frappe.throw(_("Template Text is required when Output Mode is 'Template'."), frappe.ValidationError)

	if "{%" in template_text or "%}" in template_text:
		frappe.throw(
			_("Executable template tags ('{% ... %}') are not allowed in templates."),
			frappe.ValidationError,
		)

	open_count = template_text.count("{{")
	close_count = template_text.count("}}")
	if open_count != close_count:
		frappe.throw(
			_("Template has unbalanced '{{' and '}}' placeholders."),
			frappe.ValidationError,
		)

	stripped_tokens = TEMPLATE_TOKEN_PATTERN.sub("", template_text)
	if "{{" in stripped_tokens or "}}" in stripped_tokens:
		frappe.throw(
			_("Template contains malformed '{{' or '}}' delimiters."),
			frappe.ValidationError,
		)

	variables: list[str] = []
	for raw_var in TEMPLATE_TOKEN_PATTERN.findall(template_text):
		var_name = raw_var.strip()
		if not SIMPLE_VARIABLE_PATTERN.match(var_name) or var_name.startswith("_"):
			frappe.throw(
				_(
					"Invalid template placeholder '{{{{{0}}}}}'. Only simple variable names (e.g. {{{{balance}}}}) are permitted."
				).format(raw_var),
				frappe.ValidationError,
			)
		if allowed_variables is not None and var_name not in allowed_variables:
			frappe.throw(
				_("Template variable '{{{{{0}}}}}' is not allowed in this context.").format(var_name),
				frappe.ValidationError,
			)
		if var_name not in variables:
			variables.append(var_name)

	return variables

RESERVED_TEMPLATE_KEYS = frozenset(
	{
		"action",
		"text",
		"recipient",
		"variables",
		"version",
		"status",
		"message",
		"template_reference",
		"content_type",
	}
)

def validate_template_variable_name(name, *, notification=False):
	"""Enforce lowercase ASCII snake_case without digits, symbols, or reserved keys."""
	if not isinstance(name, str) or not (1 <= len(name) <= 64) or not TEMPLATE_VARIABLE_PATTERN.fullmatch(name):
		frappe.throw(
			_(
				"Invalid template parameter name. Use lowercase snake_case letters only (for example, 'numero_contrato')."
			)
		)
	if name in RESERVED_TEMPLATE_KEYS and not (notification and name == 'message'):
		frappe.throw(_("Template parameter '{0}' is a reserved technical field.").format(name))
	return name


def extract_component_variables(text, component_label, *, notification=False):
	"""Parse '{{variable_name}}' occurrences strictly without Jinja or expression evaluation."""
	if text is None or text == "":
		return []
	if not isinstance(text, str) or "\x00" in text:
		frappe.throw(_("Invalid template {0} content.").format(_(component_label)))
	if component_label in ("Header", "Footer"):
		if len(text) > 140 or "\n" in text or "\r" in text:
			frappe.throw(
				_("Template {0} must be a single line of at most 140 characters.").format(_(component_label))
			)
		if not text.strip():
			frappe.throw(_("Template {0} cannot contain only whitespace.").format(_(component_label)))
	else:
		if len(text) > 16000:
			frappe.throw(_("Template Body exceeds the maximum allowed length."))
		if not text.strip():
			frappe.throw(_("Template Body cannot contain only whitespace."))

	if "{%" in text or "%}" in text or "{#" in text or "#}" in text:
		frappe.throw(_("Executable template expressions are not allowed in {0}.").format(_(component_label)))

	if text.count("{{") != text.count("}}"):
		frappe.throw(
			_("Malformed placeholder in {0}. Use exact '{{{{variable_name}}}}' syntax.").format(
				_(component_label)
			)
		)
	stripped = RAW_TOKEN_PATTERN.sub("", text)
	if "{" in stripped or "}" in stripped:
		frappe.throw(
			_("Malformed placeholder in {0}. Use exact '{{{{variable_name}}}}' syntax.").format(
				_(component_label)
			)
		)

	occurrences = []
	for raw_token in RAW_TOKEN_PATTERN.findall(text):
		if (
			not raw_token
			or "{" in raw_token
			or "}" in raw_token
			or any(ch.isspace() for ch in raw_token)
		):
			frappe.throw(
				_("Malformed placeholder in {0}. Use exact '{{{{variable_name}}}}' syntax.").format(
					_(component_label)
				)
			)
		validate_template_variable_name(raw_token, notification=notification)
		occurrences.append(raw_token)
	return occurrences
