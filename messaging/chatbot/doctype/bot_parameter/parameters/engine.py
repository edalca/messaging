"""Native parameter coordination; no provider calls, credentials or intent classification."""

from decimal import Decimal
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, replace

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_parameter.parameters import get_handler
from messaging.chatbot.doctype.bot_parameter.parameters.base import MAX_VALUES, MAX_VALUE_LENGTH, MAX_ARGUMENT_SIZE


def validate_parameter_values(parameter, raw_value, *, multiple=False):
	"""Enforce scalar/list input shape and bounded, flat arguments at every trusted boundary."""
	import json
	from messaging.common.primitives import decimal_json

	parameter = frappe.get_doc("Bot Parameter", parameter) if isinstance(parameter, str) else parameter
	if multiple:
		if type(raw_value) is not list:
			return False, None
		items = raw_value
		if not items or len(items) > MAX_VALUES:
			return False, None
	else:
		if isinstance(raw_value, list):
			return False, None
		items = [raw_value]
	result, seen = [], set()
	for item in items:
		valid, value = validate_parameter_value(parameter, item)
		if not valid:
			return False, None
		key = get_handler(parameter.parameter_type).deduplication_key(value)
		if key not in seen:
			seen.add(key)
			result.append(value)
	if len(json.dumps(result, ensure_ascii=False, default=decimal_json).encode()) > MAX_ARGUMENT_SIZE:
		return False, None
	return True, result if multiple else result[0]


def validate_input_limits(row):
	"""Relation cardinality is independent of the native parameter type."""
	minimum, maximum = row.get('minimum_values') or 0, row.get('maximum_values') or 0
	if any(type(value) is not int or value < 0 or value > MAX_VALUES for value in (minimum, maximum)):
		frappe.throw(_("Input cardinality must use whole numbers between 0 and 100."))
	if (minimum or maximum) and not row.get('allow_multiple_values'):
		frappe.throw(_("Input cardinality requires a multiple parameter."))
	if maximum and maximum < max(1, minimum):
		frappe.throw(_("Maximum values cannot be smaller than minimum values."))
	return max(1, minimum), maximum or MAX_VALUES


def validate_input_value(row, parameter, raw_value):
	minimum, maximum = validate_input_limits(row)
	multiple = bool(row.get('allow_multiple_values'))
	if multiple and (type(raw_value) is not list or not minimum <= len(raw_value) <= maximum):
		return False, None
	valid, value = validate_parameter_values(parameter, raw_value, multiple=multiple)
	if valid and multiple and not minimum <= len(value) <= maximum:
		return False, None
	return valid, value


def validate_parameter_value(parameter, raw_value):
	parameter = frappe.get_doc("Bot Parameter", parameter) if isinstance(parameter, str) else parameter
	if type(raw_value) not in (str, int, bool, Decimal) or len(str(raw_value)) > MAX_VALUE_LENGTH:
		return False, None
	try:
		handler = get_handler(parameter.parameter_type)
	except ValueError:
		return False, None
	return handler.validate(parameter, raw_value)


def capture_text(parameter, text, multiple=False):
	proposal = get_handler(parameter.parameter_type).capture(parameter, text, multiple=multiple)
	if proposal.status != "VALID":
		return False, None
	return validate_parameter_values(
		parameter, list(proposal.values) if multiple else proposal.values[0], multiple=multiple
	)


@dataclass(frozen=True)
class Occurrence:
	parameter: str
	start: int
	end: int
	value: str
	marker: str
	equivalent_parameters: tuple[str, ...] = ()
	masked_start: int | None = None
	masked_end: int | None = None


def public_marker(label):
	text = unicodedata.normalize("NFKD", label)
	text = "".join(c for c in text if not unicodedata.combining(c)).lower()
	return re.sub(r"[^a-z0-9]+", "_", text).strip("_") or "parameter"


def recognize(text, parameters):
	"""Earliest position, longest match, then stable local key resolves overlaps.

	Free text has no identifying pattern: it remains subject to contextual capture.
	Numeric candidates use the same token boundaries and validator as capture.
	Colliding public labels receive ordinals, never technical identifiers.
	"""
	from messaging.registrations.ai_providers import InvalidInterpretation

	if not isinstance(text, str) or len(text) > 16000:
		raise InvalidInterpretation("Interpretation text exceeds the recognition bound")
	bases = {key: public_marker(p.label) for key, p in parameters.items() if p.enabled}
	counts, ordinals = Counter(bases.values()), Counter()
	markers = {}
	# Reserve all base labels before assigning suffixes, including labels ending in digits.
	occupied = set(bases.values())
	for key in sorted(bases):
		base = bases[key]
		if counts[base] == 1:
			markers[key] = "[" + base + "]"
		else:
			while True:
				ordinals[base] += 1
				candidate = base + "_" + str(ordinals[base])
				if candidate not in occupied:
					break
			occupied.add(candidate)
			markers[key] = "[" + candidate + "]"
	matches = []
	# Types declare dependencies; consumers never switch on native type names.
	for contextual in (False, True):
		structured_spans = [(m.start, m.end) for m in matches]
		for key in sorted(markers):
			parameter = parameters[key]
			handler = get_handler(parameter.parameter_type)
			if handler.needs_structured_spans != contextual:
				continue
			extraction = handler.extract(parameter, text, structured_spans=structured_spans)
			if extraction.status != "EXTRACTED":
				continue
			for start, end, value in extraction.spans:
				if validate_parameter_value(parameter, value)[0]:
					matches.append(Occurrence(key, start, end, value, markers[key]))
	selected, end = [], -1
	for occurrence in sorted(
		matches,
		key=lambda m: (
			m.start,
			-(m.end - m.start),
			get_handler(parameters[m.parameter].parameter_type).priority,
			m.parameter,
		),
	):
		if occurrence.start >= end:
			aliases = tuple(
				sorted(
					{
						item.parameter
						for item in matches
						if (item.start, item.end) == (occurrence.start, occurrence.end)
					}
				)
			)
			occurrence = Occurrence(
				occurrence.parameter,
				occurrence.start,
				occurrence.end,
				occurrence.value,
				occurrence.marker,
				aliases,
			)
			selected.append(occurrence)
			end = occurrence.end
	pieces, end, masked_length = [], 0, 0
	for index, occurrence in enumerate(selected):
		masked_start = masked_length + occurrence.start - end
		selected[index] = replace(
			occurrence, masked_start=masked_start, masked_end=masked_start + len(occurrence.marker)
		)
		pieces.extend((text[end : occurrence.start], occurrence.marker))
		masked_length = masked_start + len(occurrence.marker)
		end = occurrence.end
	pieces.append(text[end:])
	return "".join(pieces), selected


def captured_values(occurrences, allowed, multiple):
	"""Only local candidates belonging to the selected Flow may become arguments."""
	values, ambiguous = {}, False
	for key, parameter in allowed.items():
		items = [
			item.value
			for item in occurrences
			if (item.parameter == key or key in item.equivalent_parameters)
			and len(set(item.equivalent_parameters or (item.parameter,)) & set(allowed)) == 1
		]
		if not items:
			continue
		# Singular ambiguity never selects an arbitrary occurrence.
		valid, normalized = validate_parameter_values(parameter, items, multiple=True)
		if valid and key not in multiple and len(normalized) > 1:
			ambiguous = True
		if valid and (key in multiple or len(normalized) == 1):
			values[key] = normalized if key in multiple else normalized[0]
	return values, ambiguous


def capture_context(text, parameters, *, known_parameters=None, multiple=()):
	"""Apply safe local candidates while preserving atomic structured-value rejection."""
	_transformed, occurrences = recognize(text, known_parameters or parameters)
	values, _ambiguous = captured_values(occurrences, parameters, set(multiple))
	rejected = set()
	for key in list(values):
		if not get_handler(parameters[key].parameter_type).strict_context:
			continue
		residue = text
		for item in reversed(occurrences):
			if key not in (item.equivalent_parameters or (item.parameter,)):
				residue = residue[: item.start] + " " * (item.end - item.start) + residue[item.end :]
		if not capture_text(parameters[key], residue, key in multiple)[0]:
			values.pop(key)
			rejected.add(key)
	return values, rejected, occurrences


def valid_values(values, allowed):
	result = {}
	for code in allowed:
		if code not in values or not frappe.db.exists("Bot Parameter", code):
			continue
		parameter = frappe.get_doc("Bot Parameter", code)
		ok, value = validate_parameter_values(
			parameter, values[code], multiple=isinstance(values[code], list)
		)
		if parameter.enabled and ok:
			result[code] = value
	return result


def capture(rows, values, request, *, accept_text, requested_parameter=None):
	"""Capture unambiguous local candidates, then validate an explicitly requested answer.

	An initial contextual message cannot arbitrarily fill overlapping definitions or
	free text. A subsequent answer belongs to the first field actually requested.
	Every accepted candidate passes the relation's normal type and cardinality checks.
	"""
	rows = sorted(rows, key=lambda row: row.capture_order)
	known = {row.parameter: frappe.get_doc("Bot Parameter", row.parameter) for row in rows}
	clean, rejected = {}, set()

	def check(row, raw):
		parameter = known[row.parameter]
		return validate_input_value(row, parameter, raw) if parameter.enabled else (False, None)

	for row in rows:
		if row.parameter in values:
			ok, value = check(row, values[row.parameter])
			if ok:
				clean[row.parameter] = value
	values = clean
	for row in rows:
		if row.parameter in request.get("parameters", {}):
			ok, value = check(row, request["parameters"][row.parameter])
			if ok:
				values[row.parameter] = value
			elif row.parameter not in values:
				rejected.add(row.parameter)
	pending = [row for row in rows if row.parameter not in values]
	text = request.get("text") if accept_text else None
	if text and pending:
		allowed = {row.parameter: known[row.parameter] for row in pending if row.parameter not in rejected}
		candidates, contextual_rejections, occurrences = capture_context(
			text, allowed, known_parameters=known,
			multiple={row.parameter for row in pending if row.get("allow_multiple_values")},
		)
		rejected.update(contextual_rejections)
		for row in pending:
			if row.parameter in candidates:
				ok, value = check(row, candidates[row.parameter])
				if ok:
					values[row.parameter] = value
				else:
					rejected.add(row.parameter)
		# No contextual remainder is recycled as a whole free-text answer. Exact
		# overlapping identifiers are safe only when answering a specific question.
		target = next((row for row in pending if row.parameter == requested_parameter), None)
		if target and target.parameter not in values and not candidates and not rejected and not any(
			row.parameter in request.get("parameters", {}) for row in pending):
			parameter = known[target.parameter]
			handler = get_handler(parameter.parameter_type)
			exact = handler.strict_context and occurrences and all(
				target.parameter in (item.equivalent_parameters or (item.parameter,)) for item in occurrences)
			if not occurrences or exact:
				ok, value = capture_text(parameter, text, bool(target.get("allow_multiple_values")))
				if handler.extract(parameter, text).status == "AMBIGUOUS":
					ok = False
				if ok:
					ok, value = check(target, value)
				if ok:
					values[target.parameter] = value
				else:
					rejected.add(target.parameter)
	missing = [row for row in rows if (row.required and row.parameter not in values) or row.parameter in rejected]
	return values, missing, bool(rejected)


def typed_answer(text):
	"""Only compact identifier answers bypass semantic interpretation, including malformed numbers."""
	parts = re.split(r"[\s,;]+", text.strip().rstrip(".!?"))
	return bool(
		parts
		and all(
			part.lower() in ("y", "e", "and")
			or (re.fullmatch(r"[\w@#\\-]+", part) and any(c.isdigit() for c in part))
			for part in parts
		)
	)
