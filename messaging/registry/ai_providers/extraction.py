"""Internal Parameter Extraction validation, without capture or flow actions."""
from datetime import date
import re

from .contracts import (
    MAX_RESPONSE_BYTES, InvalidInterpretation, ParameterType, ParameterExtractionRequest,
    ParameterExtractionResponse, serialize,
)

_DECIMAL = re.compile(r'[+-]?[0-9]+(?:\.[0-9]+)?')


def validate_extraction_request(request):
    if type(request) is not ParameterExtractionRequest:
        raise InvalidInterpretation('Invalid extraction request type')
    request.__post_init__()
    request.message.__post_init__()
    for parameter in request.parameters:
        parameter.__post_init__()
    return request


def validate_extraction_response(result, request):
    validate_extraction_request(request)
    if type(result) is not ParameterExtractionResponse:
        raise InvalidInterpretation('Invalid extraction response type')
    result.__post_init__()
    if result.request_id != request.request_id or result.schema_version != request.schema_version:
        raise InvalidInterpretation('Invalid interpretation correlation or version')
    specs = {row.ref: row for row in request.parameters}
    if {row.ref for row in result.parameters} != set(specs):
        raise InvalidInterpretation('Unauthorized extraction reference')
    for row in result.parameters:
        row.__post_init__()
        spec = specs[row.ref]
        if len(row.values) > 1 and not spec.allow_multiple_values:
            raise InvalidInterpretation('Invalid extraction cardinality')
        for value in row.values:
            _validate_value(value, spec.parameter_type)
    if len(serialize(result)) > MAX_RESPONSE_BYTES:
        raise InvalidInterpretation('Oversized AI response')
    return result


def _validate_value(value, kind):
    expected = {ParameterType.INT: int, ParameterType.BOOLEAN: bool}.get(kind, str)
    if type(value) is not expected or expected is str and not value.strip():
        raise InvalidInterpretation('Invalid extraction value type')
    if kind is ParameterType.DECIMAL and not _DECIMAL.fullmatch(value):
        raise InvalidInterpretation('Invalid exact extraction decimal')
    if kind is ParameterType.DATE:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError:
            raise InvalidInterpretation('Invalid extraction ISO date') from None
