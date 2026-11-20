"""Typed AI capability contracts and their closed, versioned JSON representation.

Public consumers import these objects through messaging.registry.ai_providers.
Administrative persistence and conversation orchestration remain with their DocTypes.
"""
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
import inspect
from enum import StrEnum
import json
import math
import re
from typing import TYPE_CHECKING, Literal, TypeAlias

import frappe
from frappe import _

if TYPE_CHECKING:
    from .decision import DecisionEvidence

SCHEMA_VERSION = '1'
MAX_FLOWS = 30
MAX_REQUEST_BYTES = 120000
MAX_RESPONSE_BYTES = 250000
from messaging.chatbot.doctype.ai_provider.parameters import PROVIDER_ID, validate_parameter_schema
ProviderConfiguration: TypeAlias = Mapping[str, str | int | float | bool | None]


class InvalidInterpretation(ValueError):
    """A request or response violated the closed capability contract."""


class ProviderUnavailable(RuntimeError):
    """The configured provider has no usable configuration or service."""


class ProviderTimeout(TimeoutError):
    """The selected provider exceeded its transport timeout."""


class AIServiceError(ProviderUnavailable):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class AICapability(StrEnum):
    DECISION = 'Decision'
    PARAMETER_EXTRACTION = 'Parameter Extraction'


class DecisionOutcome(StrEnum):
    RECOGNIZED = 'recognized'
    UNRECOGNIZED = 'unrecognized'
    AMBIGUOUS = 'ambiguous'
    EMPTY_CATALOG = 'empty_catalog'


class ParameterType(StrEnum):
    STRING = 'String'
    INT = 'Int'
    DATE = 'Date'
    NAME = 'Name'
    EXPRESSION = 'Expression'
    DECIMAL = 'Decimal'
    BOOLEAN = 'Boolean'


class ExtractionStatus(StrEnum):
    EXTRACTED = 'EXTRACTED'
    NOT_FOUND = 'NOT_FOUND'
    AMBIGUOUS = 'AMBIGUOUS'
    NOT_SUPPORTED = 'NOT_SUPPORTED'


FUNCTIONAL_TYPES = tuple(capability.value for capability in AICapability)
WIRE_OUTCOMES = frozenset(outcome.value for outcome in DecisionOutcome)
DECISION_KEYS = frozenset(('flows', 'excluded_flows', 'greeting_detected', 'human_attention_requested', 'outcome', 'confidence'))


def _text(value, maximum, *, empty=False):
    if type(value) is not str or len(value) > maximum or (not empty and not value.strip()):
        raise InvalidInterpretation('Invalid contract text')


def _ref(value):
    if type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', value):
        raise InvalidInterpretation('Invalid public reference')


def _tuple(value, kind, maximum=30):
    if type(value) is not tuple or len(value) > maximum or any(type(row) is not kind for row in value):
        raise InvalidInterpretation('Invalid contract collection')


def _confidence(value):
    if type(value) not in (int, float) or not 0 <= value <= 1 or not math.isfinite(value):
        raise InvalidInterpretation('Invalid wire confidence')


def _identity(version, request_id):
    if version != SCHEMA_VERSION or type(version) is not str:
        raise InvalidInterpretation('Unsupported AI contract version')
    _text(request_id, 100)


REGISTRATION_SCHEMA_VERSION = "1"
SUPPORTED_REGISTRATION_SCHEMAS = frozenset({"1"})


def _validate_version(version):
    if type(version) is not str or version not in SUPPORTED_REGISTRATION_SCHEMAS:
        raise ValueError(f"Unsupported ai_providers registration schema version: {version!r}")


@dataclass(frozen=True, slots=True)
class Message:
    text: str

    def __post_init__(self):
        _text(self.text, 16000)


@dataclass(frozen=True, slots=True)
class FlowReference:
    ref: str
    label: str
    description: str = ''

    def __post_init__(self):
        _ref(self.ref)
        _text(self.label, 500)
        _text(self.description, 8000, empty=True)


@dataclass(frozen=True, slots=True)
class SemanticContext:
    previously_recognized_flows: tuple[str, ...] = ()

    def __post_init__(self):
        _tuple(self.previously_recognized_flows, str)
        if len(set(self.previously_recognized_flows)) != len(self.previously_recognized_flows):
            raise InvalidInterpretation('Duplicate semantic reference')
        for ref in self.previously_recognized_flows:
            _ref(ref)


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    request_id: str
    message: Message
    language: str
    available_flows: tuple[FlowReference, ...] = ()
    greeting_available: bool = False
    context: SemanticContext = field(default_factory=SemanticContext)
    allowed_outcomes: tuple[DecisionOutcome, ...] = tuple(DecisionOutcome)
    schema_version: str = SCHEMA_VERSION
    human_attention_available: bool = field(default=False, kw_only=True)

    def __post_init__(self):
        _identity(self.schema_version, self.request_id)
        if type(self.message) is not Message or type(self.context) is not SemanticContext:
            raise InvalidInterpretation('Invalid Decision request type')
        _text(self.language, 40)
        if type(self.greeting_available) is not bool or type(self.human_attention_available) is not bool:
            raise InvalidInterpretation('Invalid wire flag')
        _tuple(self.available_flows, FlowReference)
        _tuple(self.allowed_outcomes, DecisionOutcome, 4)
        refs = {row.ref for row in self.available_flows}
        if (len(refs) != len(self.available_flows) or not self.allowed_outcomes
            or len(set(self.allowed_outcomes)) != len(self.allowed_outcomes)
            or set(self.context.previously_recognized_flows) - refs):
            raise InvalidInterpretation('Invalid Decision request references')
        if len(serialize(self)) > MAX_REQUEST_BYTES:
            raise InvalidInterpretation('Oversized interpretation request')


@dataclass(frozen=True, slots=True)
class DecisionResponse:
    """Historical aggregate used by legacy Decision records and fixtures."""

    request_id: str
    outcome: DecisionOutcome
    flows: tuple[str, ...] = ()
    excluded_flows: tuple[str, ...] = ()
    greeting_detected: bool = False
    confidence: float = 1.0
    schema_version: str = SCHEMA_VERSION
    human_attention_requested: bool = field(default=False, kw_only=True)

    def __post_init__(self):
        _identity(self.schema_version, self.request_id)
        if type(self.outcome) is not DecisionOutcome:
            raise InvalidInterpretation('Invalid wire outcome')
        if type(self.greeting_detected) is not bool or type(self.human_attention_requested) is not bool:
            raise InvalidInterpretation('Invalid wire flag')
        _confidence(self.confidence)
        for refs in (self.flows, self.excluded_flows):
            _tuple(refs, str)
            if len(set(refs)) != len(refs):
                raise InvalidInterpretation('Unauthorized wire Flow')
            for ref in refs:
                _ref(ref)
        if set(self.flows) & set(self.excluded_flows):
            raise InvalidInterpretation('Contradictory wire decision')
        if ((self.outcome != DecisionOutcome.RECOGNIZED and self.flows)
            or (self.outcome == DecisionOutcome.RECOGNIZED
                and not (self.flows or self.excluded_flows or self.greeting_detected or self.human_attention_requested))):
            raise InvalidInterpretation('Inconsistent wire decision')


@dataclass(frozen=True, slots=True)
class ExtractionParameter:
    ref: str
    label: str
    parameter_type: ParameterType
    allow_multiple_values: bool = False
    description: str = field(default='', kw_only=True)

    def __post_init__(self):
        _ref(self.ref)
        _text(self.label, 500)
        _text(self.description, 8000, empty=True)
        if type(self.parameter_type) is not ParameterType or type(self.allow_multiple_values) is not bool:
            raise InvalidInterpretation('Invalid extraction definition')


@dataclass(frozen=True, slots=True)
class ParameterExtractionRequest:
    request_id: str
    message: Message
    language: str
    parameters: tuple[ExtractionParameter, ...]
    schema_version: str = SCHEMA_VERSION
    context: str = field(default='', kw_only=True)

    def __post_init__(self):
        _identity(self.schema_version, self.request_id)
        _text(self.language, 40)
        _text(self.context, 8000, empty=True)
        if type(self.message) is not Message:
            raise InvalidInterpretation('Invalid extraction message')
        _tuple(self.parameters, ExtractionParameter)
        if not self.parameters or len({p.ref for p in self.parameters}) != len(self.parameters):
            raise InvalidInterpretation('Invalid extraction references')
        if len(serialize(self)) > MAX_REQUEST_BYTES:
            raise InvalidInterpretation('Oversized interpretation request')


@dataclass(frozen=True, slots=True)
class ExtractedParameter:
    ref: str
    status: ExtractionStatus
    values: tuple[str | int | bool, ...] = ()
    evidence: tuple[str, ...] = field(default=(), kw_only=True)

    def __post_init__(self):
        _ref(self.ref)
        if type(self.status) is not ExtractionStatus or type(self.values) is not tuple or len(self.values) > 100:
            raise InvalidInterpretation('Invalid extraction result')
        if (self.status == ExtractionStatus.EXTRACTED) != bool(self.values):
            raise InvalidInterpretation('Inconsistent extraction status')
        if any(type(value) not in (str, int, bool) or type(value) is int and value.bit_length() > 13288
            or len(str(value)) > 4000 for value in self.values):
            raise InvalidInterpretation('Invalid extraction value')
        _tuple(self.evidence, str, 100)
        for item in self.evidence:
            _text(item, 4000)


@dataclass(frozen=True, slots=True)
class ParameterExtractionResponse:
    request_id: str
    parameters: tuple[ExtractedParameter, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        _identity(self.schema_version, self.request_id)
        _tuple(self.parameters, ExtractedParameter)
        if len({p.ref for p in self.parameters}) != len(self.parameters):
            raise InvalidInterpretation('Duplicate extraction result')


@dataclass(frozen=True, slots=True)
class DecisionCapability:
    execute: Callable[[DecisionRequest, ProviderConfiguration], 'DecisionEvidence']

    def __post_init__(self):
        if not callable(self.execute):
            raise InvalidInterpretation('Invalid Decision operation')


@dataclass(frozen=True, slots=True)
class ParameterExtractionCapability:
    execute: Callable[[ParameterExtractionRequest, ProviderConfiguration], ParameterExtractionResponse]

    def __post_init__(self):
        if not callable(self.execute):
            raise InvalidInterpretation('Invalid extraction operation')


@dataclass(frozen=True, slots=True)
class AIProviderParameter:
    parameter_key: str
    label: str
    field_type: Literal['Data', 'Password', 'Check', 'Int', 'Float', 'Select'] = 'Data'
    required: bool = False
    default_value: str | None = None
    options: str = ''
    minimum: float | None = None
    maximum: float | None = None
    max_length: int = 4000
    sort_order: int = 0
    description: str = ''


    def __post_init__(self):
        validate_parameter_schema([frappe._dict(asdict(self))])


@dataclass(frozen=True, slots=True)
class AIProvider:
    provider_id: str
    provider_name: str
    app: str
    validate_configuration: Callable[[ProviderConfiguration], None]
    parameters: tuple[AIProviderParameter, ...]
    decision: DecisionCapability | None = None
    parameter_extraction: ParameterExtractionCapability | None = None
    schema_version: str = SCHEMA_VERSION
    enabled: bool = True
    description: str = ''

    registration_schema_version: str = field(default=REGISTRATION_SCHEMA_VERSION, kw_only=True)

    def __post_init__(self):
        _validate_version(self.registration_schema_version)
        object.__setattr__(self, 'parameters', tuple(self.parameters))
        if (not isinstance(self.provider_id, str) or not PROVIDER_ID.fullmatch(self.provider_id)
            or not isinstance(self.provider_name, str) or not self.provider_name.strip()
            or not isinstance(self.app, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', self.app)
            or type(self.enabled) not in (bool, int) or self.enabled not in (0, 1)
            or not isinstance(self.description, str)
            or any(type(p) is not AIProviderParameter for p in self.parameters)):
            raise InvalidInterpretation('Invalid AI Provider declaration')
        for parameter in self.parameters:
            parameter.__post_init__()
        if len({p.parameter_key for p in self.parameters}) != len(self.parameters):
            raise InvalidInterpretation('Duplicate AI Provider parameter')
        if ((self.decision is not None and type(self.decision) is not DecisionCapability)
            or (self.parameter_extraction is not None and type(self.parameter_extraction) is not ParameterExtractionCapability)
            or (self.decision is None and self.parameter_extraction is None)
            or not callable(self.validate_configuration) or self.schema_version != SCHEMA_VERSION):
            raise InvalidInterpretation('Invalid AI Provider capabilities')






def to_json(value):
    """Return JSON values; transport configuration never participates in this conversion."""
    if type(value) is DecisionResponse:
        return dict(schema_version=value.schema_version, request_id=value.request_id,
            decision={key: to_json(getattr(value, key)) for key in DECISION_KEYS})
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value):
        result = {key: to_json(getattr(value, key)) for key in value.__dataclass_fields__}
        optional = {ExtractionParameter: 'description', ParameterExtractionRequest: 'context',
            ExtractedParameter: 'evidence'}.get(type(value))
        if optional and not result[optional]:
            del result[optional]
        return result
    if isinstance(value, (tuple, list)):
        return [to_json(item) for item in value]
    if isinstance(value, dict):
        return {key: to_json(item) for key, item in value.items()}
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise InvalidInterpretation('Unsupported JSON value')


def serialize(value):
    try:
        return json.dumps(to_json(value), ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise InvalidInterpretation('Invalid interpretation JSON') from None
