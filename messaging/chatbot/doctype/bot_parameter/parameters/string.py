"""General text remains directed capture, without semantic extraction."""

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_parameter.parameters.base import NativeType


class StringType(NativeType):
	free_text = True

	def validate_definition(self, parameter):
		if parameter.get("string_mode") not in (None, "", "Text", "Single Word"):
			frappe.throw(_("Select a valid text format."))
		if parameter.min_length is not None and int(parameter.min_length) < 0:
			frappe.throw(_("Min Length cannot be negative."), frappe.ValidationError)
		if parameter.max_length is not None and int(parameter.max_length) < 0:
			frappe.throw(_("Max Length cannot be negative."), frappe.ValidationError)
		if (
			parameter.min_length
			and parameter.max_length
			and int(parameter.min_length) > int(parameter.max_length)
		):
			frappe.throw(_("Min Length cannot be greater than Max Length."), frappe.ValidationError)

	def validate(self, parameter, value):
		if not isinstance(value, str):
			return False, None
		cleaned = value.strip()
		if not cleaned or (
			parameter.get("string_mode") == "Single Word" and any(c.isspace() for c in cleaned)
		):
			return False, None
		if parameter.min_length and len(cleaned) < int(parameter.min_length):
			return False, None
		if parameter.max_length and len(cleaned) > int(parameter.max_length):
			return False, None
		return True, cleaned
