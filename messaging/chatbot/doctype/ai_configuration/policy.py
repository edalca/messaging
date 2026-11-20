"""Native interpretation settings by AI profile purpose."""
from dataclasses import dataclass, fields

import frappe
from frappe import _

from messaging.registry.ai_providers import InvalidInterpretation


@dataclass(frozen=True, slots=True)
class DecisionPolicy:
    minimum_confidence: float = 0.60
    intent_probability: float = 0.75
    negation_probability: float = 0.65

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) not in (int, float) or not 0.5 < value <= 1:
                raise InvalidInterpretation('Invalid Decision acceptance threshold')


RESERVED_PARAMETERS = {
    'Decision': frozenset(item.name for item in fields(DecisionPolicy)),
    'Parameter Extraction': frozenset(),
}
RESERVED_KEYS = frozenset().union(*RESERVED_PARAMETERS.values())


def profile_policy(doc, provider, *, validate_inactive=False):
    values = {key: doc.get(key) for key in RESERVED_PARAMETERS['Decision']}
    if doc.profile_type != 'Decision':
        if validate_inactive and any(value not in (None, 0) for value in values.values()):
            frappe.throw(_('Decision acceptance settings require a Decision profile.'))
        return None
    defaults = DecisionPolicy()
    previous = doc.get_doc_before_save() if validate_inactive else None
    try:
        if previous and any(value == 0 and previous.get(key) for key, value in values.items()):
            raise InvalidInterpretation('Invalid Decision acceptance threshold')
        return DecisionPolicy(**{key: getattr(defaults, key) if value in (None, 0) else value
            for key, value in values.items()})
    except InvalidInterpretation:
        frappe.throw(_('Decision acceptance thresholds must be greater than 0.5 and at most 1.'))
