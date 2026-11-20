"""Decision classification evidence returned by internal AI providers."""
from dataclasses import dataclass, fields
from enum import StrEnum

from .contracts import (
    MAX_FLOWS, MAX_RESPONSE_BYTES, SCHEMA_VERSION, DecisionRequest,
    InvalidInterpretation, _confidence, _identity, _ref, _tuple, serialize,
)


class DecisionClassification(StrEnum):
    REQUEST = 'request'
    ABSENT = 'absent'
    EXCLUDED = 'excluded'
    UNCLEAR = 'unclear'


class DecisionSignal(StrEnum):
    GREETING = 'greeting'
    HUMAN_ATTENTION = 'human_attention'


@dataclass(frozen=True, slots=True)
class DecisionProbabilities:
    request: float | None = None
    absent: float | None = None
    excluded: float | None = None
    unclear: float | None = None

    def __post_init__(self):
        scores = [getattr(self, item.name) for item in fields(self)]
        supplied = [score for score in scores if score is not None]
        for score in supplied:
            _confidence(score)
        # Allow rounding of independently reported scores to two decimal places.
        if not supplied or sum(supplied) > 1.02 or len(supplied) == len(scores) and sum(supplied) < 0.98:
            raise InvalidInterpretation('Invalid Decision probability distribution')


@dataclass(frozen=True, slots=True)
class DecisionClassificationResult:
    option_id: str
    classification: DecisionClassification
    confidence: float | None = None
    probabilities: DecisionProbabilities | None = None

    def __post_init__(self):
        _ref(self.option_id)
        if type(self.classification) is not DecisionClassification:
            raise InvalidInterpretation('Invalid Decision classification')
        if self.confidence is not None:
            _confidence(self.confidence)
        if self.probabilities is not None:
            if type(self.probabilities) is not DecisionProbabilities:
                raise InvalidInterpretation('Invalid Decision probabilities')
            self.probabilities.__post_init__()
            chosen = getattr(self.probabilities, self.classification.value)
            if chosen is not None and any(
                (other := getattr(self.probabilities, item.name)) is not None and other > chosen + 0.01
                for item in fields(DecisionProbabilities)
            ):
                raise InvalidInterpretation('Decision classification contradicts its probabilities')


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    request_id: str
    results: tuple[DecisionClassificationResult, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        _identity(self.schema_version, self.request_id)
        _tuple(self.results, DecisionClassificationResult, MAX_FLOWS + len(DecisionSignal))
        if len({row.option_id for row in self.results}) != len(self.results):
            raise InvalidInterpretation('Duplicate Decision option')
        for row in self.results:
            row.__post_init__()


def validate_decision_evidence(result, request):
    """Require one typed result per offered option, including offered signals."""
    from .wire import validate_request

    if type(request) is not DecisionRequest or type(result) is not DecisionEvidence:
        raise InvalidInterpretation('Invalid Decision evidence type')
    validate_request(request)
    result.__post_init__()
    if result.request_id != request.request_id or result.schema_version != request.schema_version:
        raise InvalidInterpretation('Invalid interpretation correlation or version')
    offered = {row.ref for row in request.available_flows}
    if offered.intersection(DecisionSignal):
        raise InvalidInterpretation('Decision option uses a reserved signal identity')
    if request.greeting_available:
        offered.add(DecisionSignal.GREETING)
    if request.human_attention_available:
        offered.add(DecisionSignal.HUMAN_ATTENTION)
    if {row.option_id for row in result.results} != offered:
        raise InvalidInterpretation('Decision evidence must cover exactly the offered options')
    if len(serialize(result)) > MAX_RESPONSE_BYTES:
        raise InvalidInterpretation('Oversized AI response')
    return result
