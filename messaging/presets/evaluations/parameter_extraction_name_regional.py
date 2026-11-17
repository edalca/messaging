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
        key='messaging.parameter_extraction.name_regional',
        name='Name Regional',
        parameters=(
            EvaluationParameter(
                ref='name',
                label='Name',
                parameter_type='Name',
                allow_contextual_name_extraction=True,
                allow_multiple_values=False,
                minimum_values=0,
                maximum_values=0,
            ),
        ),
        profile_type='Parameter Extraction',
        mandatory=True,
        scope='General',
        coverage=(
            EvaluationCoverage(language='es', group='Name', sample_count=1, required=True),
            EvaluationCoverage(language='en', group='Name', sample_count=1, required=True),
        ),
        cases={
            'en': EvaluationCaseDefinition(
                input_text='My name is Niamh O’Neill',
                expected_parameters=(EvaluationExpectedParameter(ref='name', status='EXTRACTED'),),
                expected_values=(EvaluationExpectedValue(ref='name', value_text='Niamh O’Neill'),),
            ),
            'es': EvaluationCaseDefinition(
                input_text='Me llamo José Núñez',
                expected_parameters=(EvaluationExpectedParameter(ref='name', status='EXTRACTED'),),
                expected_values=(EvaluationExpectedValue(ref='name', value_text='José Núñez'),),
            ),
        },
    )
