"""Versioned REST representation of the public AI capability contracts."""
from .contracts import (
    AICapability, DecisionRequest, InvalidInterpretation, MAX_RESPONSE_BYTES,
    ParameterExtractionRequest, serialize, to_json,
)
from .decision import (
    DecisionClassification, DecisionClassificationResult, DecisionEvidence, DecisionProbabilities,
    validate_decision_evidence,
)
from .wire import _closed, _list, deserialize_response, strict_json, validate_request

DECISION_WIRE = 'decision-v2'
EXTRACTION_WIRE = 'parameter-extraction-v2'
WIRE_VERSIONS = {
    AICapability.DECISION: DECISION_WIRE,
    AICapability.PARAMETER_EXTRACTION: EXTRACTION_WIRE,
}


def _version(request):
    if type(request) is DecisionRequest:
        return DECISION_WIRE
    if type(request) is ParameterExtractionRequest:
        return EXTRACTION_WIRE
    raise InvalidInterpretation('Invalid AI request type')


def serialize_rest_request(request):
    return dict(to_json(validate_request(request)), wire_version=_version(request))


def deserialize_rest_response(body, request):
    """Decode wire evidence and validate it against the original typed request."""
    version = _version(request)
    if isinstance(body, str):
        try:
            body = body.encode('utf-8')
        except UnicodeError:
            raise InvalidInterpretation('Invalid interpretation JSON') from None
    if len(body if isinstance(body, (bytes, bytearray)) else serialize(body)) > MAX_RESPONSE_BYTES:
        raise InvalidInterpretation('Oversized AI response')
    data = strict_json(body) if isinstance(body, (str, bytes, bytearray)) else body
    field = 'results' if version == DECISION_WIRE else 'parameters'
    _closed(data, {'wire_version', 'schema_version', 'request_id', field})
    if data['wire_version'] != version:
        raise InvalidInterpretation('Invalid AI REST wire version')
    if version == EXTRACTION_WIRE:
        return deserialize_response({key: value for key, value in data.items() if key != 'wire_version'}, request)

    rows = []
    for row in _list(data['results']):
        _closed(row, {'option_id', 'classification'}, optional={'confidence', 'probabilities'})
        label = row['classification']
        if type(label) is not str or label not in {item.value for item in DecisionClassification}:
            raise InvalidInterpretation('Invalid Decision classification')
        probabilities = row.get('probabilities')
        if probabilities is not None:
            _closed(probabilities, (), optional={item.value for item in DecisionClassification})
            probabilities = DecisionProbabilities(**probabilities)
        rows.append(DecisionClassificationResult(row['option_id'], DecisionClassification(label),
            row.get('confidence'), probabilities))
    return validate_decision_evidence(DecisionEvidence(data['request_id'], tuple(rows), data['schema_version']), request)
