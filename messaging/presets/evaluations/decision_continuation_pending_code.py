"""Default evaluation scenario and localized values."""
from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import (
    EvaluationCaseDefinition,
    EvaluationCoverage,
    EvaluationLocalizedText,
    EvaluationReference,
    EvaluationTemplateDefinition,
)


def definition() -> EvaluationTemplateDefinition:
    return EvaluationTemplateDefinition(
        key='messaging.decision.continuation_pending_code',
        name='Continuation Pending Code',
        group='continuation',
        available_flows=(
            EvaluationReference(ref='appointment'),
            EvaluationReference(ref='tracking'),
        ),
        profile_type='Decision',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='continuation', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='continuation', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='PX-204',
                flow_texts=(
                    EvaluationLocalizedText(
                        ref='appointment',
                        label='Book an appointment',
                        description='Choose a date for a new appointment.',
                    ),
                    EvaluationLocalizedText(
                        ref='tracking',
                        label='Track a parcel',
                        description='Check progress of an existing parcel.',
                    ),
                ),
                ambiguous=False,
                outcome='unrecognized',
                flows=(),
                excluded_flows=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='PX-204',
                flow_texts=(
                    EvaluationLocalizedText(
                        ref='appointment',
                        label='Reservar cita',
                        description='Elegir fecha para una cita nueva.',
                    ),
                    EvaluationLocalizedText(
                        ref='tracking',
                        label='Consultar envío',
                        description='Consultar el progreso de un envío existente.',
                    ),
                ),
                ambiguous=False,
                outcome='unrecognized',
                flows=(),
                excluded_flows=(),
            ),
        },
    )
