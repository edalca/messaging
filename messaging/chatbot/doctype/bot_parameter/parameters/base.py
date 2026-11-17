"""Minimal contracts for native validation, directed capture and contextual extraction."""

from dataclasses import dataclass
import re

MAX_VALUES = 100
MAX_VALUE_LENGTH = 4000
MAX_ARGUMENT_SIZE = 65536


@dataclass(frozen=True)
class Extraction:
	status: str  # EXTRACTED, AMBIGUOUS, NOT_FOUND or NOT_SUPPORTED.
	spans: tuple = ()


@dataclass(frozen=True)
class Capture:
	status: str  # VALID or INVALID; values are proposals, validated again by the engine.
	values: tuple = ()


class NativeType:
	priority = 10
	free_text = False
	personal_name = False
	allows_expression = False
	input_hint = None
	needs_structured_spans = False
	strict_context = False

	def validate(self, parameter, value):
		raise NotImplementedError("Native types must implement value validation")

	def validate_definition(self, parameter):
		pass

	def extract(self, parameter, text, *, structured_spans=()):
		return Extraction("NOT_SUPPORTED")

	def capture(self, parameter, text, *, multiple=False):
		return Capture("VALID", tuple(text.split(",")) if multiple else (text,))

	def deduplication_key(self, value):
		return value

	def grounded(self, parameter, value, text):
		return str(value) in text


class StructuredType(NativeType):
	strict_context = True

	def capture(self, parameter, text, *, multiple=False):
		extraction = self.extract(parameter, text)
		if extraction.status != "EXTRACTED":
			return Capture("INVALID")
		items = [value for _start, _end, value in extraction.spans]
		if not multiple and len(items) != 1:
			return Capture("INVALID")
		residue = text
		for start, end, _value in reversed(extraction.spans):
			residue = residue[:start] + " " * (end - start) + residue[end:]
		if any(any(c.isdigit() for c in token) for token in re.findall(r"[\w@#\\-]+", residue)):
			return Capture("INVALID")
		return Capture("VALID", tuple(items))
