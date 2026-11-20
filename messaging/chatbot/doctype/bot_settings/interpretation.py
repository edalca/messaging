"""Provider-independent interpretation contract, catalog and controlled selection.

See AGENTS.md for the decision contract, trust boundary and provider semantics.
This module cannot invoke Processes, write business data or compose user responses.
"""

import math
import re
from uuid import uuid4
from dataclasses import dataclass, field

import frappe

from messaging.communication.doctype.process_definition.approval import is_current
from messaging.registry.ai_providers import InvalidInterpretation, ProviderTimeout, ProviderUnavailable, DecisionRequest, DecisionOutcome, DecisionEvidence, Message, FlowReference, SemanticContext, validate_response
from messaging.chatbot.doctype.bot_flow.localization import text as localized_text
from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	captured_values,
	get_handler,
	recognize,
	validate_parameter_values,
)

@dataclass
class Decision:
    """Private orchestration proposal, enriched with deterministic capture values."""
    version: int = 1
    flows: list[str] = field(default_factory=list)
    parameters: dict[str, str | int | list] = field(default_factory=dict)
    excluded_flows: list[str] = field(default_factory=list)
    greeting_detected: bool = False
    human_attention_requested: bool = False
    ambiguous: bool = False
    outcome: str = 'unrecognized'
    metrics: dict = field(default_factory=dict)


def build_request(text, context, flows, language, *, greeting_available=False, human_attention_available=False):
    refs = {key: 'flow_' + uuid4().hex for key in flows}
    request = DecisionRequest(request_id=uuid4().hex, message=Message(text), language=language,
        available_flows=tuple(FlowReference(refs[key], flow['name'], flow['description'] or '') for key, flow in flows.items()),
        greeting_available=greeting_available,
        human_attention_available=human_attention_available,
        context=SemanticContext(tuple(refs[key] for key in context.get('previously_recognized_flows', ()) if key in refs)))
    return request, {value: key for key, value in refs.items()}


def normalize_response(result, request, refs, *, minimum_confidence=None, policy=None):
    if type(result) is DecisionEvidence:
        from .decision_policy import interpret_evidence
        return interpret_evidence(result, request, refs, policy)
    validate_response(result, request)
    if policy is not None:
        minimum_confidence = policy.minimum_confidence
    if type(minimum_confidence) not in (int, float) or not math.isfinite(minimum_confidence) or not 0 <= minimum_confidence <= 1:
        raise InvalidInterpretation('Invalid confidence threshold')
    if result.confidence < minimum_confidence:
        return Decision(ambiguous=True, outcome='ambiguous', metrics=dict(confidence=result.confidence))
    return Decision(flows=[refs[ref] for ref in result.flows], excluded_flows=[refs[ref] for ref in result.excluded_flows],
        greeting_detected=result.greeting_detected, human_attention_requested=result.human_attention_requested,
        ambiguous=result.outcome is DecisionOutcome.AMBIGUOUS,
        outcome=result.outcome.value, metrics=dict(confidence=result.confidence))


MAX_PARAMETERS = 100
MAX_FLOWS = 30  # Same incoming batch bound as Chat Turn.
OUTCOMES = {
	"recognized",
	"unrecognized",
	"ambiguous",
	"empty_catalog",
	"explicit",
	"continuation",
	"disabled",
	"unavailable",
	"timeout",
	"invalid",
	"error",
}
TECHNICAL_OUTCOMES = {"unavailable", "timeout", "invalid", "error"}


def catalog():
	"""Read current enabled, permitted records, independently of menu visibility or priority."""
	result = {}
	settings = frappe.get_single('Bot Settings')
	# Registration is a deterministic gate and is never an intention the provider may start.
	assigned = {settings.greeting_flow, settings.human_flow} - {None, ""}
	for row in frappe.get_list("Bot Flow", filters={"status": "Enabled"}, fields=["name"], limit_page_length=0):
		doc = frappe.get_doc("Bot Flow", row.name)
		doc.check_permission("read")
		if doc.name == settings.registration_flow or doc.name in assigned:
			continue
		if not is_current(doc):
			continue
		parameters = {}
		for row in doc.parameters:
			parameter = frappe.get_doc("Bot Parameter", row.parameter)
			parameter.check_permission("read")
			if parameter.enabled:
				parameters[parameter.name] = parameter
		result[doc.name] = dict(
			code=doc.name, name=doc.workflow_name, description=localized_text(doc, "semantic_description"), parameters=parameters,
			multiple_parameters=[row.parameter for row in doc.parameters if row.allow_multiple_values]
		)
	return result


def validate_decision(proposal, flows, allowed_parameters, text, *, multiple_parameters=()):
	"""Reject unknown structure and identities; revalidate every grounded candidate using M1."""
	if not isinstance(proposal, dict) or set(proposal) != set(Decision.__dataclass_fields__):
		raise InvalidInterpretation("Invalid decision structure")
	if type(proposal["version"]) is not int or proposal["version"] != 1:
		raise InvalidInterpretation("Unsupported decision version")
	if any(type(proposal[key]) is not bool for key in ("ambiguous", "greeting_detected", "human_attention_requested")):
		raise InvalidInterpretation("Invalid decision flag")
	for key in ("flows", "excluded_flows"):
		codes = proposal[key]
		if (
			not isinstance(codes, list)
			or any(not isinstance(code, str) or code not in flows for code in codes)
			or len(codes) != len(set(codes))
			or (key == "flows" and len(codes) > MAX_FLOWS)
		):
			raise InvalidInterpretation("Invalid decision Flow")
	if set(proposal["flows"]) & set(proposal["excluded_flows"]):
		raise InvalidInterpretation("Contradictory decision")
	if (
		not isinstance(proposal["outcome"], str)
		or proposal["outcome"] not in OUTCOMES
		or not isinstance(proposal["parameters"], dict)
		or len(proposal["parameters"]) > MAX_PARAMETERS
	):
		raise InvalidInterpretation("Invalid decision outcome")
	if proposal["ambiguous"] and (proposal["flows"] or proposal["parameters"]):
		raise InvalidInterpretation("Ambiguous decisions cannot authorize execution")
	multiple = set(multiple_parameters)
	for code in proposal["flows"]:
		multiple.update(flows[code].get("multiple_parameters", []))
	for code, value in proposal["parameters"].items():
		if (
			code not in allowed_parameters
			or type(value) not in (str, int, list)
			or (isinstance(value, str) and len(value) > 4000)
		):
			raise InvalidInterpretation("Unauthorized Parameter")
		parameter = allowed_parameters[code]
		valid, normalized = validate_parameter_values(parameter, value, multiple=code in multiple)
		expected = value if code not in multiple or isinstance(value, list) else [value]
		items = value if isinstance(value, list) else [value]
		if not parameter.enabled or not valid or normalized != expected or any(not get_handler(parameter.parameter_type).grounded(parameter, v, text) for v in items):
			raise InvalidInterpretation("Ungrounded or invalid Parameter")
	validate_metrics(proposal["metrics"])
	return Decision(**proposal)


def validate_metrics(metrics):
	"""Allow bounded numeric diagnostics without imposing provider probability semantics.

	Only the optional model identifier may be textual. Adapters must supply actual
	observations, never reasoning, instructions, candidate values or error bodies.
	"""
	if not isinstance(metrics, dict):
		raise InvalidInterpretation("Invalid decision metrics")
	budget = 2000

	def visit(values, depth):
		nonlocal budget
		if depth > 3 or not isinstance(values, dict):
			raise InvalidInterpretation("Invalid decision metrics")
		for key, value in values.items():
			budget -= 1
			if budget < 0 or not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", key):
				raise InvalidInterpretation("Invalid metric identity")
			if depth == 0 and key == "model":
				if not isinstance(value, str) or not re.fullmatch(
					r"[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}", value
				):
					raise InvalidInterpretation("Invalid model identity")
			elif isinstance(value, dict):
				visit(value, depth + 1)
			elif type(value) not in (int, float) or not math.isfinite(value):
				raise InvalidInterpretation("Invalid numeric metric")

	visit(metrics, 0)


def interpret(text, context, flows, active_parameters, settings, *, turn_name=None):
	"""Obtain a normalized proposal and enforce the shared trust boundary before M2."""
	if not settings.get("interpretation_enabled"):
		return Decision(outcome="disabled")
	from dataclasses import asdict
	from messaging.chatbot.doctype.ai_configuration.execution import run
	from messaging.chatbot.doctype.ai_configuration.policy import profile_policy
	from messaging.chatbot.doctype.ai_configuration.ai_configuration import resolve_configuration, diagnose, error_code
	from messaging.chatbot.doctype.bot_settings.language import resolve_process_language
	from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope

	parameters = dict(active_parameters)
	for flow in flows.values():
		parameters.update(flow['parameters'])
	transformed, occurrences = recognize(text, parameters)
	request, refs = build_request(transformed, context, flows, resolve_process_language(),
		greeting_available=bool(settings.greeting_enabled), human_attention_available=bool(settings.human_attention_enabled))
	# A durable Processing turn prevents replay after a provider call or failed log write.
	if turn_name:
		frappe.db.commit()
	configuration, provider, values = resolve_configuration(settings.active_ai_configuration, settings=settings)
	policy = profile_policy(configuration, provider)
	try:
		with evaluation_scope(turn_name, configuration=configuration.name):
			result = run(provider, values, request, policy=policy) if policy is not None else run(provider, values, request)
		decision = normalize_response(result, request, refs, policy=policy, minimum_confidence=values.get('minimum_confidence'))
	except Exception as exc:
		diagnose(configuration, error_code(exc))
		raise
	ground_flows = decision.flows
	allowed = dict(active_parameters)
	multiple = set(context.get('multiple_parameters', []))
	for code in ground_flows:
		allowed.update(flows[code]['parameters'])
		multiple.update(flows[code].get('multiple_parameters', []))
	if not decision.ambiguous and ground_flows and decision.outcome in ('recognized', 'continuation'):
		decision.parameters, ambiguous = captured_values(occurrences, allowed, multiple)
		if ambiguous:
			decision.ambiguous, decision.outcome = True, "ambiguous"
			decision.flows, decision.parameters = [], {}
	return validate_decision(asdict(decision), flows, allowed, text, multiple_parameters=multiple)
