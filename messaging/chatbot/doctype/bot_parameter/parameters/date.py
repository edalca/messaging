"""One human date format with a canonical Frappe-compatible ISO value."""

from datetime import date, datetime
import re

from messaging.chatbot.doctype.bot_parameter.parameters.base import StructuredType, Extraction

TOKEN = re.compile(r"(?<![\w/+-])[0-9]{2}/[0-9]{2}/[0-9]{4}(?![\w/+-])")


class DateType(StructuredType):
	input_hint = "Use DD/MM/YYYY, for example 26/10/2026."
	priority = 1

	def validate(self, parameter, value):
		if not isinstance(value, str):
			return False, None
		text = value.strip()
		try:
			if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", text):
				return True, datetime.strptime(text, "%d/%m/%Y").date().isoformat()
			if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
				return True, date.fromisoformat(text).isoformat()
		except ValueError:
			pass
		return False, None

	def extract(self, parameter, text, *, structured_spans=()):
		spans = [(m.start(), m.end(), m.group()) for m in TOKEN.finditer(text)]
		return Extraction("EXTRACTED" if spans else "NOT_FOUND", tuple(spans))

	def grounded(self, parameter, value, text):
		return str(value) in text or any(
			self.validate(parameter, m.group())[1] == value for m in TOKEN.finditer(text)
		)
