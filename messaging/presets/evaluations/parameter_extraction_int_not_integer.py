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
        key='messaging.parameter_extraction.int_not_integer',
        name='Int Not Integer',
        parameters=(
            EvaluationParameter(
                ref='int',
                label='Int',
                parameter_type='Int',
                allow_multiple_values=False,
                minimum_values=0,
                maximum_values=0,
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
                input_text='12.5',
                expected_parameters=(EvaluationExpectedParameter(ref='int', status='NOT_FOUND'),),
                expected_values=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='12.5',
                expected_parameters=(EvaluationExpectedParameter(ref='int', status='NOT_FOUND'),),
                expected_values=(),
            ),
        },
    )
