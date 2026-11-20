"""Interpret complete Decision evidence using Messaging's profile policy."""
from messaging.registry.ai_providers import (
    DecisionClassification as Classification,
    DecisionSignal,
    InvalidInterpretation,
    validate_decision_evidence,
)
from messaging.chatbot.doctype.ai_configuration.policy import DecisionPolicy


def _score(row):
    return getattr(row.probabilities, row.classification.value, None)


def _accepted(row, threshold, minimum_confidence):
    score = _score(row)
    return (score is not None and score >= threshold
        and (row.confidence is None or row.confidence >= minimum_confidence))


def interpret_evidence(result, request, refs, policy):
    from .interpretation import Decision

    validate_decision_evidence(result, request)
    if type(policy) is not DecisionPolicy:
        raise InvalidInterpretation('Missing native Decision policy')
    policy.__post_init__()
    by_option = {row.option_id: row for row in result.results}
    selected, excluded = [], []
    unresolved, material_ambiguity = False, False
    for flow in request.available_flows:
        row = by_option[flow.ref]
        if row.classification is Classification.REQUEST and _accepted(row, policy.intent_probability, policy.minimum_confidence):
            selected.append(refs[flow.ref])
        elif row.classification is Classification.EXCLUDED and _accepted(row, policy.negation_probability, policy.minimum_confidence):
            excluded.append(refs[flow.ref])
        elif row.classification is not Classification.ABSENT:
            unresolved = True
            if row.classification is Classification.UNCLEAR:
                scores = [score for score in (row.confidence, _score(row)) if score is not None]
                material_ambiguity |= bool(scores) and min(scores) >= policy.minimum_confidence

    signals = {}
    for signal in DecisionSignal:
        row = by_option.get(signal)
        signals[signal] = bool(row and row.classification is Classification.REQUEST
            and _accepted(row, max(policy.intent_probability, policy.minimum_confidence), policy.minimum_confidence))

    ambiguous = material_ambiguity or (unresolved and not selected)
    if ambiguous:
        outcome = 'ambiguous'
    elif selected or excluded or any(signals.values()):
        outcome = 'recognized'
    else:
        outcome = 'unrecognized' if request.available_flows else 'empty_catalog'

    # Turn diagnostics contain only counts. The restricted AI Request Log keeps
    # the validated per-option evidence without message text or credentials.
    return Decision(flows=[] if ambiguous else selected, excluded_flows=excluded,
        greeting_detected=signals[DecisionSignal.GREETING],
        human_attention_requested=signals[DecisionSignal.HUMAN_ATTENTION],
        ambiguous=ambiguous, outcome=outcome, metrics=dict(
            accepted_requests=len(selected), accepted_exclusions=len(excluded),
            missing_class_probability=sum(_score(row) is None for row in result.results)))
