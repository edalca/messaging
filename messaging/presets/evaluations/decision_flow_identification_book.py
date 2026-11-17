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
        key='messaging.decision.flow_identification_book',
        name='Flow Identification Book',
        group='flow_identification',
        available_flows=(
            EvaluationReference(ref='appointment'),
            EvaluationReference(ref='tracking'),
        ),
        profile_type='Decision',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(
                language='es',
                group='flow_identification',
                sample_count=1,
                required=True,
            ),
            EvaluationCoverage(
                language='en',
                group='flow_identification',
                sample_count=1,
                required=True,
            ),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='I want to book a new appointment',
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
                outcome='recognized',
                flows=(EvaluationReference(ref='appointment'),),
                excluded_flows=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='Quiero reservar una cita nueva',
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
                outcome='recognized',
                flows=(EvaluationReference(ref='appointment'),),
                excluded_flows=(),
            ),
        },
    )
