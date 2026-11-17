"""Validation and projection of persisted Bot Flow preset configuration."""
from copy import deepcopy

import frappe

from messaging.common.immutable_configuration import _invalid, _text, _identifier


_OPTION_FIELDS = ('priority', 'show_in_menu', 'menu_order', 'use_custom_internal_error_message')
_INPUT_FIELDS = ('parameter', 'capture_order', 'minimum_values', 'maximum_values', 'required', 'allow_multiple_values')
_PARAMETER_FIELDS = (
    'code', 'label', 'parameter_type', 'expression', 'string_mode', 'min_length', 'max_length',
    'min_value', 'max_value', 'description', 'allow_contextual_name_extraction', 'allow_ai_extraction',
)
_OUTCOME_FIELDS = ('mode', 'retry_parameter')
_TEMPLATE_FIELDS = ('template_name', 'header', 'body', 'footer')


def _mapping(value, detail):
    if type(value) is not dict:
        _invalid(detail)
    return value


def _non_negative(value, detail):
    if type(value) is not int or value < 0:
        _invalid(detail)


def _optional_text(value):
    if value is not None and type(value) is not str:
        _invalid('Localized text must be a string.')
    if isinstance(value, str):
        _text(value)


def _validate_input(argument, item):
    _identifier(argument)
    item = _mapping(item, 'Each input must be a mapping.')
    if set(item) - set(_INPUT_FIELDS):
        _invalid('Unknown capture setting.')
    specification = item.get('parameter')
    if isinstance(specification, dict):
        if 'languages' in specification:
            _invalid('Parameter translations belong to localized contributions.')
        if set(specification) - set(_PARAMETER_FIELDS):
            _invalid('Unknown parameter field.')
        from messaging.chatbot.doctype.bot_parameter.definition import validate_preset_parameter
        validate_preset_parameter(specification)
    elif type(specification) is not str:
        _invalid('Each input requires a parameter definition or an existing parameter name.')
    else:
        _text(specification, required=True)
    for key in ('capture_order', 'minimum_values', 'maximum_values'):
        if key in item:
            _non_negative(item[key], 'Capture settings must be non-negative.')
    if item.get('maximum_values') and item.get('minimum_values') and item['minimum_values'] > item['maximum_values']:
        _invalid('Capture minimum exceeds maximum.')
    for key in ('required', 'allow_multiple_values'):
        if key in item and type(item[key]) is not bool:
            _invalid('Capture flags must be booleans.')


def _validate_outcome(status, output, inputs):
    _identifier(status)
    output = _mapping(output, 'Each outcome must be a mapping.')
    if 'template' in output:
        _invalid('Template content belongs to a localized contribution.')
    if set(output) - set(_OUTCOME_FIELDS) or output.get('mode') not in ('message', 'template'):
        _invalid('Each outcome requires a message or template mode.')
    retry = output.get('retry_parameter')
    if retry is not None and (type(retry) is not str or retry not in inputs):
        _invalid('Retry Parameter requires a configured input.')


def _available_languages(definition, active):
    """A language is installable only when all referenced initial content exists."""
    result = []
    for code in active:
        if any(output['mode'] == 'template' and (not output.get('template') or code not in
                definition.get('templates', {}).get(output.get('template'), {}).get('languages', {}))
                for output in definition['outcomes'].values()):
            continue
        if code not in definition['languages'] or any(code not in template['languages']
                for template in definition.get('templates', {}).values()):
            continue
        if any(isinstance(item.get('parameter'), dict) and code not in item['parameter']['languages']
                for item in definition.get('inputs', {}).values()):
            continue
        if any('parameter' in item for item in definition.get('inputs', {}).values()) and not all(
            definition['languages'][code].get(field, '').strip() for field in ('input_initial_message', 'input_continuation_message')):
            continue
        required = ['semantic_description']
        if definition.get('options', {}).get('show_in_menu'):
            required.append('menu_label')
        if definition.get('options', {}).get('use_custom_internal_error_message'):
            required.append('internal_error_message')
        if any(not definition['languages'][code].get(key, '').strip() for key in required):
            continue
        result.append(code)
    return result


def combine_preset_configuration(*, key, name, process, data, languages):
    """Project persisted contributions into the existing Bot Flow provisioning fields."""
    result = dict(key=key, name=name, process=process, **deepcopy(data), languages={}, templates={})
    for language, localized in languages.items():
        result['languages'][language] = {k: v for k, v in localized.items() if k not in ('outcomes', 'parameters')}
        for argument, text in localized.get('parameters', {}).items():
            result['inputs'][argument]['parameter'].setdefault('languages', {})[language] = deepcopy(text)
        for status, text in localized.get('outcomes', {}).items():
            result['outcomes'][status]['template'] = status
            template = result['templates'].setdefault(status, dict(template_name=text['template_name'], languages={}))
            # Different language owners may choose localized Template Names.
            template['languages'][language] = {k: v for k, v in text.items() if k != 'template_name'}
            template.setdefault('localized_names', {})[language] = text['template_name']
    return result


def validate_preset_configuration(process, data, *, language=None, localized=None):
    """Validate initial fields against persisted Process metadata without discovery or a Flow."""
    from .contract import validate_default_contract
    from .localization import TEXT_FIELDS, INPUT_FIELDS
    from messaging.communication.doctype.process_definition.process_definition import resolve_definition, registered_contract
    from messaging.communication.doctype.communication_template.syntax import extract_component_variables
    definition = resolve_definition(process, expected_type='bot_flow')
    definition.check_permission('read')
    contract = registered_contract(definition)
    if type(data) is not dict or set(data) - {'inputs', 'outcomes', 'options'}:
        _invalid('Common configuration accepts only inputs, outcomes and options.')
    inputs, outcomes, options = data.get('inputs', {}), data.get('outcomes'), data.get('options', {})
    if any(type(item) is not dict for item in (inputs, outcomes, options)):
        _invalid('Inputs, outcomes and options must be mappings.')
    if set(options) - set(_OPTION_FIELDS):
        _invalid('Unknown Bot Flow option.')
    for key in ('priority', 'menu_order'):
        if key in options:
            _non_negative(options[key], 'Priority and Menu Order must be non-negative integers.')
    for key in ('show_in_menu', 'use_custom_internal_error_message'):
        if key in options and type(options[key]) is not bool:
            _invalid('Bot Flow flags must be booleans.')
    if bool(inputs) != bool(options.get('priority', 0)):
        _invalid('Capture inputs require a positive priority; parameterless Flows require zero.')
    if not outcomes:
        _invalid('Functional outcomes are required.')
    for argument, item in inputs.items():
        _validate_input(argument, item)
    for status, output in outcomes.items():
        _validate_outcome(status, output, inputs)
    validate_default_contract(dict(inputs=deepcopy(inputs), outcomes=deepcopy(outcomes), templates={}), contract)
    if language is None:
        return contract
    if not isinstance(language, str) or not frappe.db.exists('Language', language):
        _invalid('Select an existing Language.')
    if type(localized) is not dict or set(localized) - {*TEXT_FIELDS, *INPUT_FIELDS, 'parameters', 'outcomes'}:
        _invalid('Localized contributions cannot change common Flow fields.')
    text = {key: value for key, value in localized.items() if key in (*TEXT_FIELDS, *INPUT_FIELDS)}
    if type(text.get('semantic_description')) is not str:
        _invalid('Semantic description is required.')
    _text(text['semantic_description'], required=True)
    for value in text.values():
        _optional_text(value)
    for argument, phrase in localized.get('parameters', {}).items():
        if argument not in inputs or not isinstance(inputs[argument].get('parameter'), dict):
            _invalid('Parameter translations require an initial typed binding.')
        if type(phrase) is not dict or set(phrase) != {'request_phrase'} or type(phrase.get('request_phrase')) is not str:
            _invalid('Each parameter translation requires one request phrase.')
        _text(phrase['request_phrase'], required=True)
    for status, content in localized.get('outcomes', {}).items():
        if status not in outcomes:
            _invalid('Localized outcome is absent from the Process configuration.')
        if outcomes[status]['mode'] != 'template':
            _invalid('Message outcomes cannot have translations or templates.')
        if type(content) is not dict or set(content) - set(_TEMPLATE_FIELDS):
            _invalid('Localized Template contains unknown content.')
        if not isinstance(content.get('template_name'), str) or not content['template_name'].strip():
            _invalid('Localized Template requires a Template Name.')
        from messaging.communication.doctype.communication_template.communication_template import validate_preset_content
        validate_preset_content(content, language)
        for component in ('header', 'body', 'footer'):
            for variable in extract_component_variables(content.get(component) or '', component.title()):
                if contract['outcomes'][status]['fields'].get(variable) not in ('string', 'nullable_string'):
                    _invalid('Template variable is not a declared string output: ' + variable)
    return contract
