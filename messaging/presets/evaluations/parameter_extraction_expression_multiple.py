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
        key='messaging.parameter_extraction.expression_multiple',
        name='Expression Multiple',
        parameters=(
            EvaluationParameter(
                ref='expression',
                label='Expression',
                parameter_type='Expression',
                expression='@@-###',
                allow_multiple_values=True,
                minimum_values=1,
                maximum_values=3,
            ),
        ),
        profile_type='Parameter Extraction',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='Expression', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='Expression', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='PX-204, QZ-310',
                expected_parameters=(EvaluationExpectedParameter(ref='expression', status='EXTRACTED'),),
                expected_values=(
                    EvaluationExpectedValue(ref='expression', value_text='PX-204'),
                    EvaluationExpectedValue(ref='expression', value_text='QZ-310'),
                ),
            ),
            'es': EvaluationCaseDefinition(
                input_text='PX-204, QZ-310',
                expected_parameters=(EvaluationExpectedParameter(ref='expression', status='EXTRACTED'),),
                expected_values=(
                    EvaluationExpectedValue(ref='expression', value_text='PX-204'),
                    EvaluationExpectedValue(ref='expression', value_text='QZ-310'),
                ),
            ),
        },
    )
