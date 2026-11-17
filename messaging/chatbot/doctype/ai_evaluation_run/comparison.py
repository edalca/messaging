"""Adapt synthetic fixtures without resolving or executing business Flows."""
from messaging.chatbot.doctype.bot_settings.interpretation import build_request, normalize_response
import copy
import json
import re
from uuid import uuid4

from messaging.registrations import ai_providers as wire
from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import validate_extraction

DECISION_FIELDS = ('flows', 'excluded_flows', 'greeting_detected', 'human_attention_requested', 'ambiguous', 'outcome')
REFERENCE_FIELDS = frozenset(('flows', 'excluded_flows'))


def contract_defect(exc):
    """Only fixed diagnostics from the installed validator can cross the error boundary."""
    safe = {'Duplicate JSON key', 'Invalid interpretation JSON', 'Invalid interpretation correlation or version',
        'Invalid wire decision structure', 'Invalid wire flag', 'Invalid wire outcome', 'Invalid wire confidence',
        'Unauthorized wire Flow', 'Contradictory wire decision', 'Inconsistent wire decision',
        'Oversized interpretation request', 'Oversized evaluation request'}
    message = str(exc) if isinstance(exc, wire.InvalidInterpretation) else ''
    return message if message in safe else None


def decision_request(source, language):
    request = wire.deserialize_request(dict(copy.deepcopy(source), schema_version=wire.SCHEMA_VERSION,
        request_id=uuid4().hex, language=language, allowed_outcomes=sorted(wire.WIRE_OUTCOMES)))
    return request, {row.ref: row.ref for row in request.available_flows}


def _same_references(actual, expected):
    """Reference lists are sets. Order is not a contract requirement and is not rewritten."""
    return (isinstance(actual, list) and isinstance(expected, list) and len(actual) == len(expected)
        and len(set(actual)) == len(actual) and set(actual) == set(expected))


def functional_scope(group, expected):
    """Capture continuation is answered locally before the classifier is called.

    Those fixtures remain direct contract checks. They do not measure the operational route.
    """
    outcome = expected.get('outcome') if isinstance(expected, dict) else None
    if group == 'continuation' or outcome == 'continuation':
        return 'contract_only'
    return 'operational'


def compare_decision(raw, request, refs, expected, minimum_confidence):
    if isinstance(raw, (str, bytes, bytearray, dict)):
        raw = wire.deserialize_response(raw, request)
    decision = normalize_response(raw, request, refs, minimum_confidence=minimum_confidence)
    normalized = {key: getattr(decision, key) for key in DECISION_FIELDS}
    differences = []
    for key in DECISION_FIELDS:
        if normalized[key] == expected[key]:
            continue
        if key in REFERENCE_FIELDS and _same_references(normalized[key], expected[key]):
            continue
        reason = {
            'flows': 'Selected Flows differ', 'excluded_flows': 'Negation differs',
            'ambiguous': 'Ambiguity differs', 'outcome': 'Outcome differs',
        }.get(key, 'Decision flag differs')
        differences.append(dict(field=key, reason=reason, expected=expected[key], actual=normalized[key]))
    return normalized, differences


def compare_extraction(source, group, actual, expected):
    # This validates a future executor's output; it never fabricates that executor.
    validate_extraction(source, actual)
    return [dict(field='parameters.' + ref + '.' + key, expected=expected['parameters'][ref][key], actual=actual['parameters'][ref][key])
        for ref in sorted(expected['parameters']) for key in ('status', 'values')
        if actual['parameters'][ref][key] != expected['parameters'][ref][key]]


def protected_response(value, secrets):
    """Persist bounded diagnostics with credential reflections and credential keys removed."""
    if type(value) in (wire.DecisionResponse, wire.ParameterExtractionResponse):
        value = wire.to_json(value)
    text = value.decode('utf-8', errors='replace') if isinstance(value, (bytes, bytearray)) else (
        value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=lambda _v: '[unsupported]'))
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        text = text.replace(secret, '[redacted]')
        text = text.replace(json.dumps(secret)[1:-1], '[redacted]')
    text = re.sub(r'(?i)(bearer\s+|basic\s+)[a-z0-9+/=_\.\-]+', r'\1[redacted]', text)
    text = re.sub(r'(?i)("(?:api[_-]?key|token|password|authorization|auth_secret)"\s*:\s*)"(?:\\.|[^"\\])*"',
        r'\1"[redacted]"', text)
    return text[:250000]
