"""Closed JSON encoding and validation for AI capability exchanges."""
import json
import math
from .contracts import (
    SCHEMA_VERSION,
    MAX_FLOWS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    InvalidInterpretation,
    ProviderUnavailable,
    ProviderTimeout,
    AIServiceError,
    AICapability,
    DecisionOutcome,
    ParameterType,
    ExtractionStatus,
    FUNCTIONAL_TYPES,
    WIRE_OUTCOMES,
    DECISION_KEYS,
    _text,
    _ref,
    _tuple,
    _confidence,
    _identity,
    to_json,
    serialize,
    REGISTRATION_SCHEMA_VERSION,
    SUPPORTED_REGISTRATION_SCHEMAS,
    _validate_version,
    Message,
    FlowReference,
    SemanticContext,
    DecisionRequest,
    DecisionResponse,
    ExtractionParameter,
    ParameterExtractionRequest,
    ExtractedParameter,
    ParameterExtractionResponse,
    DecisionCapability,
    ParameterExtractionCapability,
    AIProviderParameter,
    AIProvider
)
from .extraction import validate_extraction_response


def strict_json(body):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidInterpretation('Duplicate JSON key')
            result[key] = value
        return result

    def finite_number(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return number

    try:
        return json.loads(body.decode('utf-8') if isinstance(body, (bytes, bytearray)) else body,
            object_pairs_hook=pairs, parse_float=finite_number,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise InvalidInterpretation('Invalid interpretation JSON') from None


def _closed(value, keys, *, optional=()):
    if type(value) is not dict or not set(keys) <= value.keys() <= set(keys).union(optional):
        raise InvalidInterpretation('Invalid closed contract structure')


def _list(value):
    if type(value) is not list:
        raise InvalidInterpretation('Invalid JSON array')
    return tuple(value)


def deserialize_request(value, capability=AICapability.DECISION):
    """Decode the public request, excluding the REST-only provider_parameters envelope."""
    try:
        value = strict_json(value) if isinstance(value, (str, bytes, bytearray)) else value
        common = {'schema_version', 'request_id', 'message', 'language'}
        if capability is AICapability.DECISION:
            _closed(value, common | {'available_flows', 'greeting_available', 'human_attention_available', 'context', 'allowed_outcomes'})
            _closed(value['message'], {'text'})
            _closed(value['context'], {'previously_recognized_flows'})
            flows = []
            for row in _list(value['available_flows']):
                _closed(row, {'ref', 'label', 'description'})
                flows.append(FlowReference(**row))
            return DecisionRequest(request_id=value['request_id'], message=Message(**value['message']),
                language=value['language'], available_flows=tuple(flows), greeting_available=value['greeting_available'],
                human_attention_available=value['human_attention_available'],
                context=SemanticContext(_list(value['context']['previously_recognized_flows'])),
                allowed_outcomes=tuple(DecisionOutcome(row) for row in _list(value['allowed_outcomes'])),
                schema_version=value['schema_version'])
        if capability is not AICapability.PARAMETER_EXTRACTION:
            raise InvalidInterpretation('Invalid capability')
        _closed(value, common | {'parameters'}, optional={'context'})
        _closed(value['message'], {'text'})
        parameters = []
        for row in _list(value['parameters']):
            _closed(row, {'ref', 'label', 'parameter_type', 'allow_multiple_values'}, optional={'description'})
            kind = ParameterType(row['parameter_type'])
            parameters.append(ExtractionParameter(**dict(row, parameter_type=kind)))
        return ParameterExtractionRequest(value['request_id'], Message(**value['message']), value['language'],
            tuple(parameters), value['schema_version'], context=value.get('context', ''))
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise InvalidInterpretation('Invalid AI request contract') from exc


def validate_request(request):
    if type(request) not in (DecisionRequest, ParameterExtractionRequest):
        raise InvalidInterpretation('Invalid AI request type')
    # Reconstruct to check even a frozen object deliberately mutated by a local implementation.
    deserialize_request(to_json(request), AICapability.DECISION if type(request) is DecisionRequest else AICapability.PARAMETER_EXTRACTION)
    return request


def validate_response(result, request):
    if type(request) is ParameterExtractionRequest:
        return validate_extraction_response(result, request)
    validate_request(request)
    if type(result) is not DecisionResponse:
        raise InvalidInterpretation('Invalid AI response type')
    result.__post_init__()
    if result.request_id != request.request_id or result.schema_version != request.schema_version:
        raise InvalidInterpretation('Invalid interpretation correlation or version')
    if len(serialize(result)) > MAX_RESPONSE_BYTES:
        raise InvalidInterpretation('Oversized AI response')
    refs = {row.ref for row in request.available_flows}
    if set(result.flows + result.excluded_flows) - refs:
        raise InvalidInterpretation('Unauthorized wire Flow')
    if (result.outcome not in request.allowed_outcomes
        or (result.greeting_detected and not request.greeting_available)
        or (result.human_attention_requested and not request.human_attention_available)
        or result.outcome == DecisionOutcome.EMPTY_CATALOG and refs):
        raise InvalidInterpretation('Inconsistent wire decision')
    return result


def deserialize_response(value, request):
    try:
        if len(value if isinstance(value, (str, bytes, bytearray)) else serialize(value)) > MAX_RESPONSE_BYTES:
            raise InvalidInterpretation('Oversized AI response')
        value = strict_json(value) if isinstance(value, (str, bytes, bytearray)) else value
        if type(request) is DecisionRequest:
            _closed(value, {'schema_version', 'request_id', 'decision'})
            data = value['decision']
            _closed(data, DECISION_KEYS)
            result = DecisionResponse(request_id=value['request_id'], schema_version=value['schema_version'],
                outcome=DecisionOutcome(data['outcome']), flows=_list(data['flows']),
                excluded_flows=_list(data['excluded_flows']), greeting_detected=data['greeting_detected'],
                human_attention_requested=data['human_attention_requested'], confidence=data['confidence'])
        else:
            _closed(value, {'schema_version', 'request_id', 'parameters'})
            rows = []
            for row in _list(value['parameters']):
                _closed(row, {'ref', 'status', 'values'}, optional={'evidence'})
                rows.append(ExtractedParameter(row['ref'], ExtractionStatus(row['status']), _list(row['values']),
                    evidence=_list(row.get('evidence', []))))
            result = ParameterExtractionResponse(value['request_id'], tuple(rows), value['schema_version'])
        return validate_response(result, request)
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise InvalidInterpretation('Invalid AI response contract') from exc




def record_call(*args, **kwargs):
    """Record provider evidence through the sole AI Request Log writer."""
    from messaging.chatbot.doctype.ai_request_log.ai_request_log import record_call as write
    return write(*args, **kwargs)


def capture_response(body, *, context=None, public_values=()):
    """Expose diagnostic JSON separately from Decision/Extraction responses.

    Context describes provider-owned questions or technical relationships.
    Public values are non-personal identifiers/enumerations safe to preserve;
    free text and secret reflections are redacted by the request log owner.
    """
    from messaging.chatbot.doctype.ai_request_log.ai_request_log import capture_response as capture
    return capture(body, context=context, public_values=public_values)
