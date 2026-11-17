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
        key='messaging.parameter_extraction.expression_invalid',
        name='Expression Invalid',
        parameters=(
            EvaluationParameter(
                ref='expression',
                label='Expression',
                parameter_type='Expression',
                expression='@@-###',
                allow_multiple_values=False,
                minimum_values=0,
                maximum_values=0,
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
                input_text='PX-20',
                expected_parameters=(EvaluationExpectedParameter(ref='expression', status='NOT_FOUND'),),
                expected_values=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='PX-20',
                expected_parameters=(EvaluationExpectedParameter(ref='expression', status='NOT_FOUND'),),
                expected_values=(),
            ),
        },
    )
