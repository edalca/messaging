"""Bounded Unicode name validation and conservative local introduction recognition."""

from dataclasses import dataclass

import regex

from messaging.chatbot.doctype.bot_parameter.parameters.base import Capture, Extraction
from messaging.chatbot.doctype.bot_parameter.parameters.string import StringType

FLAGS = regex.VERSION1 | regex.IGNORECASE | regex.FULLCASE
TIMEOUT = 0.02
MAX_TEXT = 16000
NAME_CHARS = r"[\p{L}\p{M}\p{Zs}'’ʼʻ·・.\-‐‑\u200c\u200d]+"
VALID_NAME = regex.compile(r"\p{L}[\p{L}\p{M}\p{Zs}'’ʼʻ·・.\-‐‑\u200c\u200d]*", FLAGS)
BAD_CONTROL = regex.compile(r"[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]|[\p{Cf}--[\u200c\u200d]]", FLAGS)
PROSE = regex.compile(
	r"^(?:me\s+llamo|mi\s+nombre|my\s+name|quiero|necesito|hola|gracias|no\s+s[eé]|"
	r"i\s+(?:want|need)|cliente\b|un\s+dato\b)|"
	r"\s+(?:y\s+(?:mi|quiero|necesito)|and\s+(?:my|i)|(?:mi\s+)?(?:identidad|contrato|documento)\b)",
	FLAGS,
)
# Reject grammatical request patterns, not individual words that may be names.
REQUEST = regex.compile(
    r"^(?:(?:yo\s+)?(?:quisiera|querría|querria|deseo|quiero|necesito|puedo|podría|podria)\s+"
    r"(?:\p{L}+\s+){0,2}(?:consultar|saber|conocer|ver|revisar|pagar|reportar|solicitar|obtener|hacer)\b|"
    r"(?:me\s+(?:puede[sn]?|podría[ns]?|podria[ns]?)|por\s+favor)\s+|"
    r"(?:cuál|cual|cómo|como|cuándo|cuando|dónde|donde)\s+(?:es|está|esta|puedo|consulto)\b|"
    r"i\s+(?:(?:would|would['’]ve)\s+like\s+to|want\s+to|need\s+to|(?:am|['’]m)\s+(?:looking|trying)\s+to)\s+|"
    r"(?:can|could|would|will)\s+you\s+|(?:please|kindly)\s+\p{L}+\b|"
    r"(?:what|where|when|how)\s+(?:is|are|can|do|does|much)\b)",
    FLAGS,
)
PAIR = regex.compile(r"\s+(?:y|and|o|or)\s+", FLAGS)
PUNCTUATION = regex.compile(r"[,;:!?\n，。！？؛،।]|(?<=\p{L}{2})\.(?=\s|$)", FLAGS)
STOP = regex.compile(
	r"""(?x)
    \s+(?:y|e)\s+(?:mi\b|el\b|la\b|quiero\b|necesito\b|deseo\b|meu\b|minha\b|ho\b|il\b)
    |\s+and\s+(?:my\b|i\b|need\b)
    |\s+et\s+(?:j['’]|je\b|mon\b|ma\b)
    |\s+und\s+(?:meine?\b|ich\b|brauche\b)
    |\s+и\s+(?:мой\b|моя\b|мне\b)
    |\s+و(?:رقم\b|أريد\b|اريد\b|أحتاج\b)
    |\s+(?:con\s+)?(?:mi\s+)?(?:identidad|id|dni|documento|contrato|teléfono|telefono|fecha)\b
    |\s+(?:nací|naci)\s+el\b|\s+pero\b
""",
	FLAGS,
)
PREFIXES = [
	(
		r"(?<![\p{L}\p{M}])(?:me\s+llamo|mi\s+nombre(?:\s+completo)?\s+es|(?:yo\s+)?soy|me\s+dicen)\s+",
		None,
		"self",
	),
	(r"(?<![\p{L}\p{M}])(?:el\s+nombre\s+es\s+|nombre\s*:\s*)", None, "unspecified"),
	(r"(?<![\p{L}\p{M}])(?:my\s+name\s+is|i['’]m)\s+", None, "self"),
	(r"(?<![\p{L}\p{M}])meu\s+nome\s+[ée]\s+", None, "self"),
	(r"(?<![\p{L}\p{M}])je\s+m['’]appelle\s+", None, "self"),
	(r"(?<![\p{L}\p{M}])mi\s+chiamo\s+", None, "self"),
	(r"(?<![\p{L}\p{M}])(?:ich\s+hei(?:ß|ss)e|mein\s+name\s+ist)\s+", None, "self"),
	(r"(?<![\p{L}\p{M}])меня\s+зовут\s+", None, "self"),
	(r"(?<![\p{L}\p{M}])(?:اسمي|إسمي)\s+", None, "self"),
	(r"मेरा\s+नाम\s+", r"\s+है(?:[\s।,.]|$)", "self"),
	(r"我的名字是\s*", None, "self"),
	(r"私の名前は\s*", r"です", "self"),
	(r"제\s*이름은\s*", r"입니다", "self"),
	(
		r"(?<![\p{L}\p{M}])(?:mi\s+(?:esposo|esposa|hermana|hermano|madre|padre)\s+se\s+llama|pertenece\s+a)\s+",
		None,
		"other",
	),
	(r"(?<![\p{L}\p{M}])my\s+(?:wife|husband|mother|father)\s+is\s+", None, "other"),
]
PATTERNS = [
	(regex.compile(p, FLAGS), regex.compile(s, FLAGS) if s else None, role) for p, s, role in PREFIXES
]
REPORTED = regex.compile(r"\b(?:ejemplo|example|dijo|said)\b", FLAGS)
NEGATED = regex.compile(r"\b(?:no|not)\s*$", FLAGS)


@dataclass(frozen=True)
class NameResult:
	status: str
	value: str | None = None
	start: int | None = None
	end: int | None = None


def valid_name(value):
	"""Validate structure, not existence, identity, ethnicity or family-name conventions."""
	if not isinstance(value, str) or not value or len(value) > 4000:
		return False
	try:
		return bool(
			VALID_NAME.fullmatch(value, timeout=TIMEOUT)
			and not BAD_CONTROL.search(value, timeout=TIMEOUT)
			and not PROSE.search(value, timeout=TIMEOUT)
			and not REQUEST.search(value, timeout=TIMEOUT)
			and not PUNCTUATION.search(value, timeout=TIMEOUT)
			and not PAIR.search(value, timeout=TIMEOUT)
		)
	except TimeoutError:
		return False


def extract_name(original, structured_spans=()):
	"""Return original codepoint offsets; masks preserve length and cannot become names.

	Only explicit introductions with safe boundaries qualify. Multiple people,
	reported speech, unresolved clauses and timeouts abstain. No role is inferred.
	"""
	if not isinstance(original, str) or not original or len(original) > MAX_TEXT:
		return NameResult("NOT_FOUND")
	try:
		if BAD_CONTROL.search(original, timeout=TIMEOUT):
			return NameResult("NOT_FOUND")
		chars = list(original)
		for start, end in structured_spans:
			if 0 <= start < end <= len(original):
				chars[start:end] = "[" * (end - start)
		text = "".join(chars)
		candidates, uncertain = set(), False
		for pattern, suffix, role in PATTERNS:
			for match in pattern.finditer(text, timeout=TIMEOUT):
				before = text[max(0, match.start() - 50) : match.start()]
				if NEGATED.search(before, timeout=TIMEOUT) or REPORTED.search(before, timeout=TIMEOUT):
					uncertain = True
					continue
				start = match.end()
				run = regex.match(NAME_CHARS, text[start:], flags=FLAGS, timeout=TIMEOUT)
				if not run:
					continue
				end = start + run.end()
				if suffix:
					ending = suffix.search(text, start, timeout=TIMEOUT)
					if not ending or ending.start() > end:
						continue
					end = ending.start()
				stops = [
					m.start()
					for p in (PUNCTUATION, STOP)
					if (m := p.search(text, start, end, timeout=TIMEOUT))
				]
				if stops:
					end = min(stops)
				elif end < len(text) and (text[end].isalnum() or text[end] == "-"):
					uncertain = True
					continue
				while start < end and text[start].isspace():
					start += 1
				while end > start and (text[end - 1].isspace() or text[end - 1] == "."):
					end -= 1
				candidate = original[start:end]
				if role == "other" or PAIR.search(candidate, timeout=TIMEOUT):
					uncertain = True
				if valid_name(candidate):
					candidates.add((start, end))
				else:
					uncertain = True
		if uncertain or len(candidates) > 1:
			return NameResult("AMBIGUOUS")
		if candidates:
			start, end = candidates.pop()
			return NameResult("EXTRACTED", original[start:end], start, end)
		return NameResult("NOT_FOUND")
	except TimeoutError:
		return NameResult("AMBIGUOUS")


class NameType(StringType):
	personal_name = True
	free_text = False
	needs_structured_spans = True
	priority = 3

	def validate_definition(self, parameter):
		# Text length restrictions are shared; word-count metadata belongs only to String.
		from frappe import _dict

		definition = _dict(parameter.as_dict() if callable(getattr(parameter, "as_dict", None)) else parameter)
		definition.string_mode = "Text"
		super().validate_definition(definition)

	def validate(self, parameter, value):
		if not isinstance(value, str) or not valid_name(value.strip()):
			return False, None
		cleaned = value.strip()
		if parameter.min_length and len(cleaned) < int(parameter.min_length):
			return False, None
		if parameter.max_length and len(cleaned) > int(parameter.max_length):
			return False, None
		return True, cleaned

	def extract(self, parameter, text, *, structured_spans=()):
		if not parameter.get("allow_contextual_name_extraction"):
			return Extraction("NOT_SUPPORTED")
		result = extract_name(text, structured_spans)
		return Extraction(
			result.status, ((result.start, result.end, result.value),) if result.status == "EXTRACTED" else ()
		)

	def capture(self, parameter, text, *, multiple=False):
		extraction = self.extract(parameter, text)
		if extraction.status == "AMBIGUOUS":
			return Capture("INVALID")
		if extraction.status == "EXTRACTED":
			return Capture("VALID", tuple(value for _start, _end, value in extraction.spans))
		return super().capture(parameter, text, multiple=multiple)
