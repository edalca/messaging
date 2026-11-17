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
        key='messaging.parameter_extraction.int_multiple',
        name='Int Multiple',
        parameters=(
            EvaluationParameter(
                ref='int',
                label='Int',
                parameter_type='Int',
                allow_multiple_values=True,
                minimum_values=1,
                maximum_values=3,
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
                input_text='12, 23',
                expected_parameters=(EvaluationExpectedParameter(ref='int', status='EXTRACTED'),),
                expected_values=(
                    EvaluationExpectedValue(ref='int', value_int=12),
                    EvaluationExpectedValue(ref='int', value_int=23),
                ),
            ),
            'es': EvaluationCaseDefinition(
                input_text='12, 23',
                expected_parameters=(EvaluationExpectedParameter(ref='int', status='EXTRACTED'),),
                expected_values=(
                    EvaluationExpectedValue(ref='int', value_int=12),
                    EvaluationExpectedValue(ref='int', value_int=23),
                ),
            ),
        },
    )
