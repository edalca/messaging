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
        key='messaging.decision.multi_intent_tracking_appointment',
        name='Multi Intent Tracking Appointment',
        group='multi_intent',
        available_flows=(
            EvaluationReference(ref='appointment'),
            EvaluationReference(ref='tracking'),
        ),
        profile_type='Decision',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='multi_intent', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='multi_intent', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='Track my parcel and then book an appointment',
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
                flows=(EvaluationReference(ref='tracking'), EvaluationReference(ref='appointment')),
                excluded_flows=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='Consulta mi envío y después reserva una cita',
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
                flows=(EvaluationReference(ref='tracking'), EvaluationReference(ref='appointment')),
                excluded_flows=(),
            ),
        },
    )
