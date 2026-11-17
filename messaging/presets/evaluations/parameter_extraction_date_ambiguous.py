"""Default evaluation scenario and localized values."""
from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import (
    EvaluationCaseDefinition,
    EvaluationCoverage,
    EvaluationExpectedParameter,
    EvaluationParameter,
    EvaluationTemplateDefinition,
)


def definition() -> EvaluationTemplateDefinition:
    return EvaluationTemplateDefinition(
        key='messaging.parameter_extraction.date_ambiguous',
        name='Date Ambiguous',
        parameters=(
            EvaluationParameter(
                ref='date',
                label='Date',
                parameter_type='Date',
                allow_multiple_values=False,
                minimum_values=0,
                maximum_values=0,
            ),
        ),
        profile_type='Parameter Extraction',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='Date', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='Date', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='10/11/2026 or 12/11/2026',
                expected_parameters=(EvaluationExpectedParameter(ref='date', status='AMBIGUOUS'),),
                expected_values=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='10/11/2026 o 12/11/2026',
                expected_parameters=(EvaluationExpectedParameter(ref='date', status='AMBIGUOUS'),),
                expected_values=(),
            ),
        },
    )
