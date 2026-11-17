"""Deterministic signed integer recognition; formatted identities belong to Expression."""

import re

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_parameter.parameters.base import StructuredType, Extraction

TOKEN = re.compile(r"(?<![\w/+-])[+-]?[0-9]+(?![\w/+-])")


class IntegerType(StructuredType):
	priority = 2

	def validate_definition(self, parameter):
		if (
			parameter.min_value is not None
			and parameter.max_value is not None
			and int(parameter.min_value) > int(parameter.max_value)
		):
			frappe.throw(_("Min Value cannot be greater than Max Value."), frappe.ValidationError)

	def validate(self, parameter, value):
		text = str(value).strip()
		if type(value) not in (str, int) or not re.fullmatch(r"[+-]?[0-9]+", text):
			return False, None
		number = int(text)
		if abs(number) > 9007199254740991:
			return False, None
		if parameter.min_value not in (None, "") and number < int(parameter.min_value):
			return False, None
		if parameter.max_value not in (None, "") and number > int(parameter.max_value):
			return False, None
		return True, number

	def extract(self, parameter, text, *, structured_spans=()):
		spans = [(m.start(), m.end(), m.group()) for m in TOKEN.finditer(text)]
		return Extraction("EXTRACTED" if spans else "NOT_FOUND", tuple(spans))
