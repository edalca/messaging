"""Public ai providers contracts and registration operations."""
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
    AIProvider,

)
from .contracts import ProviderConfiguration
from .contracts import to_json, serialize
from .wire import strict_json, _closed, _list, deserialize_request, validate_request, validate_response, deserialize_response, record_call, capture_response
from .decision import (
    DecisionClassification, DecisionClassificationResult, DecisionEvidence,
    DecisionProbabilities, DecisionSignal, validate_decision_evidence,
)
from .extraction import validate_extraction_request, validate_extraction_response

from .operations import retire_providers, _declared, _register_ai_providers, internal_provider, register_ai_providers, validate_registration_schema
from messaging.registry._catalog import register_declaration_type

register_declaration_type('ai_providers', AIProvider, validate=validate_registration_schema,
    synchronize=_register_ai_providers, retire=retire_providers)
