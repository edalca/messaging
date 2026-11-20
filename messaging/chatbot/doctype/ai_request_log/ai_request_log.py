"""Immutable, protected provider evidence, independent of operational AI contracts."""
import base64
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4

import frappe
from frappe import _
from frappe.model.document import Document
from messaging.common.write_guards import InternalWriteGuard
from messaging.registry.ai_providers import AICapability
from messaging.security.permissions import is_system_manager

_recording = InternalWriteGuard("messaging_ai_request_log")

_scope = ContextVar("messaging_ai_evaluation", default=None)
_call = ContextVar("messaging_ai_provider_call", default=None)
MAX_DIAGNOSTIC_BYTES = 250000
REDACTED = "[redacted]"
_private_key = re.compile(
	r"(?i)(password|secret|authorization|authentication|api[_-]?key|cookie|credential|"
	r"headers|email|phone|address|recipient|sender|contact|person|username|ssn|birthdate|passport|"
	r"^message$|^prompt$|^instructions$|^content$|^text$|^body$|^state$|^values$|^name$|"
	r"^(?:(?:access|refresh|id)[_-]?)?token$)"
)


def protected_json(value, *, secrets=(), public_values=()):
	"""Keep JSON structure and scores; free text is private unless explicitly declared technical.

	Adapters may declare bounded public identifiers/enumerations, never personal text.
	Secret reflections are removed even from keys or declared public identifiers.
	An unsafe/oversized document is explicitly omitted, never silently sliced.
	"""
	from messaging.registry.ai_providers import strict_json
	try:
		if isinstance(value, (str, bytes, bytearray)):
			if len(value) > MAX_DIAGNOSTIC_BYTES:
				return None, "omitted_size_limit"
			value = strict_json(value)
		encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
		if len(encoded.encode()) > MAX_DIAGNOSTIC_BYTES:
			return None, "omitted_size_limit"
		public = {v for v in public_values if isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", v)}
		private = {str(v) for v in secrets if v is not None and str(v)}
		redacted = False

		def text(item):
			nonlocal redacted
			if any(s in item for s in private):
				redacted = True
				return REDACTED
			return item

		def protect(item, depth=0):
			nonlocal redacted
			if depth > 40:
				raise ValueError
			if isinstance(item, dict):
				result = {}
				for key, child in item.items():
					if type(key) is not str or text(key) == REDACTED:
						raise ValueError
					if key not in public and not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.:-]{0,99}', key):
						result[f'{REDACTED} key {len(result) + 1}'], redacted = REDACTED, True
						continue
					if _private_key.search(key):
						result[key], redacted = REDACTED, True
					else:
						result[key] = protect(child, depth + 1)
				return result
			if isinstance(item, (list, tuple)):
				return [protect(v, depth + 1) for v in item]
			if isinstance(item, str):
				if text(item) == REDACTED or item not in public:
					redacted = True
					return REDACTED
				return item
			if item is None or type(item) in (bool, int, float):
				if str(item) in private:
					redacted = True
					return REDACTED
				return item
			raise ValueError

		result = protect(value)
		if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_DIAGNOSTIC_BYTES:
			return None, "omitted_size_limit"
		return result, "redacted" if redacted else "preserved"
	except (ValueError, TypeError, UnicodeError, RecursionError):
		return None, "omitted_invalid_json"


@contextmanager
def provider_call(provider, values, request):
	"""Collect one invocation, then write once after operational contract validation."""
	from messaging.registry.ai_providers import to_json, AICapability, DecisionEvidence, DecisionClassification, DecisionSignal
	import time
	secrets = [values.get(row.parameter_key) for row in provider.parameters if row.field_type == "Password"]
	if provider.provider_type == "External" and values.get("auth_type") == "Basic":
		secrets.extend((values.get("username"), base64.b64encode(
			(values["username"] + ":" + values["auth_secret"]).encode()).decode()))
	public = {request.request_id, request.schema_version, "recognized", "unrecognized", "ambiguous", "empty_catalog",
		"EXTRACTED", "NOT_FOUND", "AMBIGUOUS", "NOT_SUPPORTED"}
	public.update(row.ref for row in getattr(request, "available_flows", getattr(request, "parameters", ())))
	call = dict(secrets=secrets, public_values=public, response_status="not_available")
	token = _call.set(call)
	started = time.monotonic()
	try:
		yield call
	finally:
		_call.reset(token)
		recorded = call.get("recorded", {})
		if call['response_status'] == 'not_available' and recorded.get('http_status') not in (None, 200):
			call['response_status'] = 'omitted_http_error_body'
		summary, metadata_status = protected_json(recorded.pop("summary", {}) or {}, secrets=secrets, public_values=public)
		summary = summary if isinstance(summary, dict) else {'metadata_status': metadata_status}
		normalized = call.get("normalized")
		normalized_status = 'not_available'
		if isinstance(normalized, DecisionEvidence):
			public.update(DecisionClassification)
			public.update(DecisionSignal)
		if normalized is not None:
			normalized, normalized_status = protected_json(to_json(normalized), secrets=secrets, public_values=public)
		summary.update(provider_response=call.get("response"), response_status=call["response_status"],
			provider_context=call.get("context"), context_status=call.get("context_status"),
			normalized_response=normalized, normalized_status=normalized_status, contract_validation=call.get("contract_validation", "Failed"),
			request_id=request.request_id, exception_type=call.get("exception_type"))
		defaults = dict(provider=provider.name, request_text=request.message.text,
			operation=AICapability.DECISION if hasattr(request, "available_flows") else AICapability.PARAMETER_EXTRACTION)
		defaults.update(recorded)
		defaults['provider'] = defaults.get('provider') or provider.name
		if defaults.get('request_text') is None:
			defaults['request_text'] = request.message.text
		defaults['operation'] = AICapability.DECISION if hasattr(request, 'available_flows') else AICapability.PARAMETER_EXTRACTION
		# Metadata is also provider-controlled: a model identifier may reflect a credential.
		for key in ('request_text', 'requested_model', 'actual_model'):
			if isinstance(defaults.get(key), str):
				for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
					defaults[key] = defaults[key].replace(secret, REDACTED)
		if isinstance(defaults.get('result'), dict):
			result = dict(defaults['result'])
			if isinstance(result.get('model'), str) and any(str(s) in result['model'] for s in secrets if s):
				result.pop('model')
			defaults['result'] = result
		defaults["error"] = call.get("error") or defaults.get("error")
		defaults.setdefault("latency", (time.monotonic() - started) * 1000)
		record_call(None, summary=summary, **defaults)


class AIRequestLog(Document):
	def before_validate(self):
		if not _recording.active():
			frappe.throw(_("Technical evaluation records are immutable."), frappe.PermissionError)

	def load_from_db(self):
		# Check evaluation identity before exposing a technical record.
		if not frappe.db.get_value(self.doctype, self.name, 'request_key'):
			frappe.throw(_("This record does not belong to Messaging evaluations."), frappe.PermissionError)
		super().load_from_db()

	def validate(self):
		if not self.is_new():
			frappe.throw(_("Technical evaluation records are immutable."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Technical evaluation records are immutable."), frappe.PermissionError)


@contextmanager
def evaluation_scope(turn, *, configuration=None, evidence=None):
	identifier = uuid4().hex
	if evidence is not None:
		evidence['evaluation_id'] = identifier
	token = _scope.set(dict(evaluation_id=identifier, turn=turn, configuration=configuration, sequence=0, evidence=evidence))
	try:
		yield
	finally:
		_scope.reset(token)


def capture_response(body, *, context=None, public_values=(), protect_evaluation=False):
	"""Capture provider JSON before adaptation, with optional provider-owned context.

	Only protected JSON enters the request log. Evaluation raw evidence retains its
	separate, explicit in-memory lifecycle and existing protection at persistence.
	"""
	scope = _scope.get()
	if scope and scope.get('evidence') is not None and not protect_evaluation:
		scope['evidence']['response'] = body[:250000] if isinstance(body, (str, bytes, bytearray)) else body
	call = _call.get()
	if call is not None:
		declared = public_values if isinstance(public_values, (list, tuple, set, frozenset)) else ()
		public = call['public_values'] | {v for v in declared if isinstance(v, str)}
		call['response'], call['response_status'] = protected_json(body, secrets=call['secrets'], public_values=public)
		if protect_evaluation and scope and scope.get('evidence') is not None:
			scope['evidence']['response'] = call['response']
		if context is not None:
			call['context'], call['context_status'] = protected_json(context, secrets=call['secrets'], public_values=public)


def safe_error(code, *, turn=None):
	return frappe.get_doc(dict(doctype="Error Log", method="Messaging AI Evaluation Error",
		error="AI evaluation failed: " + code + ". Sensitive details omitted.", metadata="{}",
		reference_doctype="Chat Turn" if turn else None, reference_name=turn)).insert(ignore_permissions=True).name


def record_call(settings=None, result=None, summary=None, latency=0, error=None, *, provider=None, requested_model=None, actual_model=None, request_text=None, transmission_state=None, operation=AICapability.DECISION, http_status=None):
	"""Logging cannot trigger a retry. Missing tokens have explicit reported flags.

	The caller already owns the contact connection lock and a durable Processing marker.
	A failed insert is rolled back to a savepoint, retaining the external call checkpoint.
	"""
	call = _call.get()
	if call is not None:
		call['recorded'] = dict(result=result, summary=summary, latency=latency, error=error,
			provider=provider or (settings.get('ai_provider') or settings.get('provider') if isinstance(settings, dict) else None),
			requested_model=requested_model or (settings.get('requested_model') if isinstance(settings, dict) else None), actual_model=actual_model,
			request_text=request_text, transmission_state=transmission_state, operation=operation,
			http_status=http_status)
		return
	scope = _scope.get() or dict(evaluation_id=uuid4().hex, turn=None, sequence=0)
	scope["sequence"] += 1
	result = result if isinstance(result, dict) else {}
	model = actual_model if actual_model is not None else result.get("model")
	model = model if isinstance(model, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,80}", model) else None
	usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
	values = {}
	for field in ("input_tokens", "output_tokens", "total_tokens"):
		value = usage.get(field)
		reported = type(value) is int and 0 <= value <= 2147483647
		values[field] = value if reported else None
		values[field + "_reported"] = int(reported)
	from messaging.common.primitives import utcnow
	provider_name = provider or (settings.get("ai_provider") or settings.get("provider") if isinstance(settings, dict) else None)
	req_model = requested_model or (settings.get("requested_model") if isinstance(settings, dict) else None)
	summary_payload = summary if isinstance(summary, dict) else {}
	if type(http_status) is int and 100 <= http_status <= 599:
		summary_payload = dict(summary_payload, http_status=http_status)

	frappe.db.savepoint("ai_evaluation_write")
	try:
		doc = frappe.get_doc(dict(doctype="AI Request Log", provider=provider_name,
			configuration=scope.get("configuration"), request_text=request_text, transmission_state=transmission_state,
			requested_model=req_model, actual_model=model,
			operation=AICapability(operation).value,
			turn=scope["turn"], evaluation_id=scope["evaluation_id"], request_sequence=scope["sequence"],
			request_key=scope["evaluation_id"] + ":" + str(scope["sequence"]),
			status="Failed" if error else "Completed", latency_ms=round(latency, 3), evaluated_at=utcnow(),
			result_summary=json.dumps(summary_payload),
			error_code=error, error_log=safe_error(error, turn=scope["turn"]) if error else None,
			**values))
		with _recording():
			doc.insert(ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="ai_evaluation_write")
		try:
			safe_error("log_write_failed", turn=scope["turn"])
		except Exception:
			pass
	# Preserve call evidence even if later decision validation fails. Never roll back the call marker.
	if scope["turn"]:
		frappe.db.commit()


def on_doctype_update():
	frappe.db.add_index("AI Request Log", ["turn", "evaluation_id", "request_sequence"], "turn_evaluation")


def get_permission_query_conditions(user=None):
	"""Do not expose orphaned records from a previously removed application schema."""
	if not is_system_manager(user):
		return "1=0"
	return "`tabAI Request Log`.`request_key` IS NOT NULL"


def has_permission(doc, user=None, permission_type=None):
	return is_system_manager(user) and permission_type in (None, 'read', 'select') and bool(doc.request_key)
