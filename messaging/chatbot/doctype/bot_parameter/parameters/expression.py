"""Configured literal expressions and complete identifier recognition."""

import re

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_parameter.parameters.base import StructuredType, Extraction

MAX_EXPRESSIONS = 32
MAX_EXPRESSION_LENGTH = 256
MAX_EXPRESSION_SIZE = 8192


def expression_error(line, cause):
	frappe.throw(_("Invalid expression on line {0}: {1}").format(line, cause), frappe.ValidationError)


def compile_expression(expression):
	"""Compile a bounded literal language; administrator input never supplies regex syntax."""
	if expression is None or expression == "":
		return []
	if not isinstance(expression, str) or len(expression) > MAX_EXPRESSION_SIZE:
		expression_error(1, _("Expression field exceeds 8192 characters."))
	patterns, seen = [], set()
	for number, line in enumerate(expression.split("\n"), 1):
		if not line:
			continue
		if len(patterns) >= MAX_EXPRESSIONS:
			expression_error(number, _("At most 32 expressions are allowed."))
		if len(line) > MAX_EXPRESSION_LENGTH:
			expression_error(number, _("Expression exceeds 256 characters."))
		parts, tokens = [], []
		index = 0
		while index < len(line):
			char = line[index]
			if char == "\\":
				index += 1
				if index == len(line):
					expression_error(number, _("Trailing escape character."))
				char = line[index]
				if char not in "#@\\":
					expression_error(number, _("Escape sequence is not allowed."))
				token = {char}
			elif char == "#":
				token = set("0123456789")
			elif char == "@":
				token = set("abcdefghijklmnopqrstuvwxyz")
			elif char.isascii() and (char.isalnum() or char == "-"):
				token = {char.lower()}
			else:
				expression_error(
					number, _("Only ASCII letters, digits, -, #, @ and allowed escapes are accepted.")
				)
			tokens.append(frozenset(token))
			parts.append("[" + "".join(re.escape(c) for c in sorted(token)) + "]")
			index += 1
		key = tuple(tokens)
		if key in seen:
			expression_error(number, _("Duplicate expression."))
		seen.add(key)
		patterns.append((re.compile("".join(parts), re.IGNORECASE | re.ASCII), key))
	return patterns


def extract_expression_spans(expression, text):
	"""Prefer the longest complete identifier at each position, preserving verbatim values."""
	if not isinstance(text, str) or len(text) > 16000:
		return []
	matches = []
	for pattern, _tokens in compile_expression(expression):
		bounded = re.compile(r"(?<![\w@#\\-])(?ai:" + pattern.pattern + r")(?![\w@#\\-])")
		matches.extend((m.start(), m.end(), m.group()) for m in bounded.finditer(text))
	result, end = [], -1
	for start, stop, value in sorted(matches, key=lambda m: (m[0], -(m[1] - m[0]))):
		if start >= end:
			result.append((start, stop, value))
			end = stop
	return result


def extract_expression_values(expression, text):
	return [value for _start, _stop, value in extract_expression_spans(expression, text)]


class ExpressionType(StructuredType):
	allows_expression = True
	priority = 0

	def validate_definition(self, parameter):
		if not compile_expression(parameter.expression):
			frappe.throw(_("Expression requires at least one valid pattern."), frappe.ValidationError)
		if parameter.allow_ai_extraction:
			frappe.throw(_("Expression parameters cannot use AI assisted extraction."))

	def validate(self, parameter, value):
		if not isinstance(value, str):
			return False, None
		value = value.strip()
		return (
			(True, value)
			if any(p.fullmatch(value) for p, _ in compile_expression(parameter.expression))
			else (False, None)
		)

	def extract(self, parameter, text, *, structured_spans=()):
		spans = extract_expression_spans(parameter.expression, text)
		return Extraction("EXTRACTED" if spans else "NOT_FOUND", tuple(spans))

	def deduplication_key(self, value):
		return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))
