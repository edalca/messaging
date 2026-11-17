"""Immutable evaluation scenarios and their localized cases; DocTypes own persistence."""
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal
import inspect
import frappe
from messaging.chatbot.doctype.bot_parameter.parameters import get_handler, get_supported_types
from messaging.chatbot.doctype.bot_parameter.parameters.engine import validate_input_limits

from messaging.common.immutable_configuration import _Value, _plain, _invalid, _text, _identifier, _languages


from .parameters import PARAMETER_FIELDS, parameter_contract


@dataclass(frozen=True, slots=True)
class EvaluationReference(_Value):
    ref: str

    def validate(self):
        _identifier(self.ref)
        if len(self.ref) > 64:
            _invalid('Evaluation references support at most 64 characters.')


@dataclass(frozen=True, slots=True)
class EvaluationLocalizedText(EvaluationReference):
    label: str
    description: str = ''

    def validate(self):
        EvaluationReference.validate(self)
        _text(self.label, required=True)


@dataclass(frozen=True, slots=True)
class EvaluationParameter(EvaluationReference):
    parameter_type: Literal['String', 'Int', 'Date', 'Name', 'Expression']
    label: str = ''
    string_mode: Literal['', 'Text', 'Single Word'] = ''
    min_length: int = 0
    max_length: int = 0
    has_min_value: bool = False
    min_value: int = 0
    has_max_value: bool = False
    max_value: int = 0
    allow_contextual_name_extraction: bool = False
    expression: str = ''
    allow_multiple_values: bool = False
    minimum_values: int = 0
    maximum_values: int = 0

    def validate(self):
        EvaluationReference.validate(self)
        parameter_contract(frappe._dict(_plain(self)))


@dataclass(frozen=True, slots=True)
class EvaluationCoverage(_Value):
    language: str
    group: str
    sample_count: int
    required: bool
    minimum_parameters: int = 1

    def validate(self):
        _languages({self.language: self})
        _text(self.group, required=True)
        if not 1 <= self.sample_count <= 1000000 or not 1 <= self.minimum_parameters <= 30:
            _invalid('Invalid evaluation coverage bounds.')


@dataclass(frozen=True, slots=True)
class EvaluationExpectedParameter(EvaluationReference):
    status: Literal['EXTRACTED', 'NOT_FOUND', 'AMBIGUOUS', 'NOT_SUPPORTED']


@dataclass(frozen=True, slots=True)
class EvaluationExpectedValue(EvaluationReference):
    value_text: str | None = None
    value_int: int | None = None
    value_date: str | None = None

    def validate(self):
        EvaluationReference.validate(self)
        if sum(value is not None for value in (self.value_text, self.value_int, self.value_date)) != 1:
            _invalid('Expected values require exactly one native value field.')


@dataclass(frozen=True, slots=True)
class EvaluationCaseDefinition(_Value):
    input_text: str
    variant: str = 'default'
    flow_texts: tuple[EvaluationLocalizedText, ...] = ()
    greeting_detected: bool = False
    human_attention_requested: bool = field(default=False, kw_only=True)
    ambiguous: bool = False
    outcome: Literal['recognized', 'unrecognized', 'ambiguous', 'empty_catalog'] = 'recognized'
    flows: tuple[EvaluationReference, ...] = ()
    excluded_flows: tuple[EvaluationReference, ...] = ()
    expected_parameters: tuple[EvaluationExpectedParameter, ...] = ()
    expected_values: tuple[EvaluationExpectedValue, ...] = ()

    def validate(self):
        _text(self.input_text, required=True)
        if len(self.input_text) > 16000:
            _invalid('Evaluation text supports at most 16000 characters.')
        _identifier(self.variant)
        if len(self.variant) > 64:
            _invalid('Evaluation variants support at most 64 characters.')


@dataclass(frozen=True, slots=True)
class EvaluationTemplateDefinition(_Value):
    key: str
    name: str
    profile_type: Literal['Decision', 'Parameter Extraction']
    mandatory: bool
    scope: Literal['General', 'Application', 'Provider']
    coverage: tuple[EvaluationCoverage, ...]
    cases: Mapping[str, EvaluationCaseDefinition]
    description: str = ''
    applicable_application: str = ''
    provider: str = ''
    group: str = ''
    greeting_available: bool = False
    human_attention_available: bool = field(default=False, kw_only=True)
    available_flows: tuple[EvaluationReference, ...] = ()
    parameters: tuple[EvaluationParameter, ...] = ()
    previously_recognized_flows: tuple[EvaluationReference, ...] = ()


    def validate(self):
        _identifier(self.key, dotted=True)
        if '.' not in self.key or len(self.key) > 120:
            _invalid('Evaluation keys require an owner namespace and at most 120 characters.')
        _text(self.name, required=True)
        _languages(self.cases)
        if not self.coverage:
            _invalid('Evaluation coverage is required.')
        coverage = [(row.language, row.group, row.minimum_parameters) for row in self.coverage]
        if len(set(coverage)) != len(coverage) or {row.language for row in self.coverage} != set(self.cases):
            _invalid('Coverage must be unique and match the localized case languages.')

    def initial_values(self):
        values = _plain(self)
        values['template_id'] = values.pop('key')
        values['template_name'] = values.pop('name')
        values['cases'] = [dict(case, language=language) for language, case in sorted(values['cases'].items())]
        return values


__all__ = ('EvaluationTemplateDefinition', 'EvaluationCaseDefinition', 'EvaluationParameter',
    'EvaluationCoverage', 'EvaluationReference', 'EvaluationLocalizedText',
    'EvaluationExpectedParameter', 'EvaluationExpectedValue', 'validate_definitions')


def validate_definitions(definitions):
    for definition in definitions:
        if type(definition) is not EvaluationTemplateDefinition:
            _invalid("Use the owner's typed declaration.")
        definition.__post_init__()

    from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import default_case_identity
    identities = set()
    for definition in definitions:
        for language in definition.cases:
            identity = default_case_identity(definition.key, language)
            if identity in identities:
                _invalid('Default evaluation case identities collide after normalization.')
            identities.add(identity)
