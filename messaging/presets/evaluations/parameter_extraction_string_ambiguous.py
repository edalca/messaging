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
        key='messaging.parameter_extraction.string_ambiguous',
        name='String Ambiguous',
        parameters=(
            EvaluationParameter(
                ref='string',
                label='String',
                parameter_type='String',
                string_mode='Single Word',
                allow_multiple_values=False,
                minimum_values=0,
                maximum_values=0,
            ),
        ),
        profile_type='Parameter Extraction',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='String', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='String', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='red or blue',
                expected_parameters=(EvaluationExpectedParameter(ref='string', status='AMBIGUOUS'),),
                expected_values=(),
            ),
            'es': EvaluationCaseDefinition(
                input_text='rojo o azul',
                expected_parameters=(EvaluationExpectedParameter(ref='string', status='AMBIGUOUS'),),
                expected_values=(),
            ),
        },
    )
