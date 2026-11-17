"""Default evaluation scenario and localized values."""
from messaging.chatbot.doctype.ai_evaluation_case_template.configuration import (
    EvaluationCaseDefinition,
    EvaluationCoverage,
    EvaluationExpectedParameter,
    EvaluationExpectedValue,
    EvaluationParameter,
    EvaluationTemplateDefinition,
)


def definition() -> EvaluationTemplateDefinition:
    return EvaluationTemplateDefinition(
        key='messaging.parameter_extraction.date_valid',
        name='Date Valid',
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
                input_text='Date: 31/12/2026',
                expected_parameters=(EvaluationExpectedParameter(ref='date', status='EXTRACTED'),),
                expected_values=(EvaluationExpectedValue(ref='date', value_date='2026-12-31'),),
            ),
            'es': EvaluationCaseDefinition(
                input_text='Fecha: 31/12/2026',
                expected_parameters=(EvaluationExpectedParameter(ref='date', status='EXTRACTED'),),
                expected_values=(EvaluationExpectedValue(ref='date', value_date='2026-12-31'),),
            ),
        },
    )
