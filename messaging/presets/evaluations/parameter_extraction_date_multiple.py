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
        key='messaging.parameter_extraction.date_multiple',
        name='Date Multiple',
        parameters=(
            EvaluationParameter(
                ref='date',
                label='Date',
                parameter_type='Date',
                allow_multiple_values=True,
                minimum_values=1,
                maximum_values=3,
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
                input_text='10/11/2026, 12/11/2026',
                expected_parameters=(EvaluationExpectedParameter(ref='date', status='EXTRACTED'),),
                expected_values=(
                    EvaluationExpectedValue(ref='date', value_date='2026-11-10'),
                    EvaluationExpectedValue(ref='date', value_date='2026-11-12'),
                ),
            ),
            'es': EvaluationCaseDefinition(
                input_text='10/11/2026, 12/11/2026',
                expected_parameters=(EvaluationExpectedParameter(ref='date', status='EXTRACTED'),),
                expected_values=(
                    EvaluationExpectedValue(ref='date', value_date='2026-11-10'),
                    EvaluationExpectedValue(ref='date', value_date='2026-11-12'),
                ),
            ),
        },
    )
