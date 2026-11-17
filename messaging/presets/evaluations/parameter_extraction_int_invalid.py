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
        key='messaging.parameter_extraction.int_invalid',
        name='Int Invalid',
        parameters=(
            EvaluationParameter(
                ref='int',
                label='Int',
                parameter_type='Int',
                min_value=1,
                max_value=99,
                allow_multiple_values=False,
                minimum_values=0,
                maximum_values=0,
                has_min_value=True,
                has_max_value=True,
            ),
        ),
        profile_type='Parameter Extraction',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='Int', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='Int', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='Quantity: 101',
                expected_parameters=(EvaluationExpectedParameter(ref='int', status='NOT_FOUND'),),
                expected_values=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='Cantidad: 101',
                expected_parameters=(EvaluationExpectedParameter(ref='int', status='NOT_FOUND'),),
                expected_values=(),
            ),
        },
    )
