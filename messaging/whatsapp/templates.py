"""Translate between Messaging template projections and WhatsApp Cloud API."""
import json
import re
from string import ascii_lowercase

import frappe
from frappe import _

from messaging.communication.doctype.communication_provider.communication_provider import definition_hash
from messaging.communication.doctype.communication_template.communication_template import _snake_case, validate_output_variables
from messaging.communication.doctype.communication_template.syntax import (
    RESERVED_TEMPLATE_KEYS, TEMPLATE_PLACEHOLDER_PATTERN, TEMPLATE_VARIABLE_PATTERN,
)
from messaging.whatsapp.api import GraphError, numeric_id
from messaging.whatsapp.configuration import client, value
from messaging.whatsapp.languages import LANGUAGE_CODES

LIMITS = {'header': 60, 'body': 1024, 'footer': 60}
PARAMETER_LINE_BREAK = re.compile(r'\r\n|[\r\n\u0085\u2028\u2029]')
PARAMETER_SPACING = re.compile(r' *\t[ \t]*| {5,}')
PARAMETER_CONTROLS = re.compile(r'[\x00-\x1f\x7f-\x9f]')
POSITION = re.compile(r'{{([1-9][0-9]?)}}')
NAMED = re.compile(r'{{([a-z][a-z0-9_]*)}}')
FORMATS = (('POSITIONAL', False), ('NAMED', True))
FORMAT_CODES = tuple(code for code, _ in FORMATS)
REMOTE_NAME = re.compile(r'[a-z0-9_]{1,512}')
NAME = re.compile(r'[a-z0-9_]{1,140}')
STATUS = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
CATEGORIES = ('UTILITY', 'MARKETING')
PUBLICATION_PENDING = {'PENDING', 'IN_APPEAL', 'PENDING_APPROVAL'}
PUBLICATION_REJECTED = {'REJECTED', 'DELETED', 'PENDING_DELETION'}


def template_scope_key(binding):
    account = frappe.get_doc('Communication Channel Account', binding['account'])
    waba = value(account, 'waba_id')
    if not numeric_id(waba):
        frappe.throw(_('Invalid WhatsApp account configuration.'))
    return waba


def _published_meta_name(template):
    raw = template.get('published_snapshot')
    if not raw:
        return None
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return None
    name = snapshot.get('delivery', {}).get('name')
    return name if isinstance(name, str) and NAME.fullmatch(name) else None


def identity(template):
    return dict(name=_published_meta_name(template) or _snake_case(template.template_name),
        language=template.provider_language)


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Duplicate WhatsApp template parameter mapping')
    return result


def _internal_name(key):
    return (isinstance(key, str) and 1 <= len(key) <= 64 and TEMPLATE_VARIABLE_PATTERN.fullmatch(key)
        and (key == 'message' or key not in RESERVED_TEMPLATE_KEYS))


def _external_name(key):
    return isinstance(key, str) and NAMED.fullmatch('{{' + key + '}}') is not None


def _declared_variables(template):
    names = []
    seen = set()
    for row in template.get('template_variables') or []:
        key = row.get('parameter_name')
        if not _internal_name(key) or key in seen:
            frappe.throw(_('Invalid WhatsApp template parameter names.'))
        seen.add(key)
        names.append(key)
    return names


def _format(template):
    selected = value(template, 'parameter_format') or 'POSITIONAL'
    if selected not in FORMAT_CODES:
        frappe.throw(_('Invalid WhatsApp template parameter format.'))
    return selected


def components(template):
    named = _format(template) == 'NAMED'
    declared = _declared_variables(template)
    declared_set = set(declared)
    used = set()
    for field, limit in LIMITS.items():
        text = template.get(field)
        if not text:
            continue
        occurrences = TEMPLATE_PLACEHOLDER_PATTERN.findall(text)
        if (field == 'footer' and occurrences) or (field == 'header' and len(occurrences) > 1):
            frappe.throw(_('Invalid WhatsApp template component.'))
        unique = list(dict.fromkeys(occurrences))
        if any(not _internal_name(key) or key not in declared_set for key in unique):
            frappe.throw(_('Invalid WhatsApp template parameter names.'))
        if named:
            if any(not _external_name(key) for key in unique):
                frappe.throw(_('Invalid WhatsApp template parameter names.'))
            parameters = {key: key for key in sorted(unique)}
            external = text
        else:
            if len(occurrences) != len(unique) or used & set(unique):
                frappe.throw(_('Invalid WhatsApp template parameter positions.'))
            keys = [key for key in declared if key in set(unique)]
            parameters = {key: index for index, key in enumerate(keys, 1)}
            external = TEMPLATE_PLACEHOLDER_PATTERN.sub(lambda match: '{{%s}}' % parameters[match[1]], text)
        if len(parameters) > 99 or len(external) > limit:
            frappe.throw(_('Invalid WhatsApp template component.'))
        used.update(unique)
        yield dict(type=field.upper(), text=external, **({'format': 'TEXT'} if field == 'header' else {})), parameters
    if used != declared_set:
        frappe.throw(_('Invalid WhatsApp template parameter names.'))


def validate_template(template, variables=None):
    mapped = identity(template)
    if not NAME.fullmatch(mapped['name']) or mapped['language'] not in LANGUAGE_CODES:
        frappe.throw(_('Invalid WhatsApp template identity.'))
    if not (template.body or '').strip():
        frappe.throw(_('WhatsApp templates require a nonempty body.'))
    list(components(template))
    return True


def describe_template(template):
    return dict(key=definition_hash(identity(template)),
        revision=_revision(_format(template), [row for row, _ in components(template)]))


def _revision(parameter_format, content):
    # Retain the hash of every positional revision already stored in Messaging.
    return definition_hash(dict(parameter_format=parameter_format, components=content)
        if parameter_format == 'NAMED' else content)


def _named_examples_match(example, field, names):
    if example is None:
        return True  # The published placeholders themselves identify the send parameters.
    key = field.lower() + '_text_named_params'
    if not isinstance(example, dict) or set(example) - {key}:
        return False
    entries = example.get(key, [])
    if not isinstance(entries, list):
        return False
    values = {}
    for entry in entries:
        if (not isinstance(entry, dict) or not _external_name(name := entry.get('param_name'))
            or not isinstance(sample := entry.get('example'), str) or not sample.strip()
            or name in values and values[name] != sample):
            return False
        values[name] = sample
    return set(values) == set(names)


def _remote_components(remote):
    if remote.get('category') not in CATEGORIES:
        return [], 'unsupported_category'
    if remote.get('parameter_format') not in (None, *FORMAT_CODES):
        return [], 'unsupported_parameter_format'
    named = remote.get('parameter_format') == 'NAMED'
    pattern = NAMED if named else POSITION
    rows = remote.get('components')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return [], 'invalid_external_components'
    if any(row.get('type') == 'CAROUSEL' for row in rows):
        return [], 'unsupported_carousel'
    result = {}
    for row in rows:
        field = row.get('type')
        if field == 'BUTTONS':
            return [], 'unsupported_buttons'
        if not isinstance(field, str) or field not in ('HEADER', 'BODY', 'FOOTER') or field in result:
            return [], 'unsupported_component'
        if field == 'HEADER' and row.get('format') in ('IMAGE', 'VIDEO', 'DOCUMENT', 'LOCATION'):
            return [], 'unsupported_media_header'
        if field == 'HEADER' and row.get('format') != 'TEXT':
            return [], 'unsupported_header_format'
        if field != 'HEADER' and row.get('format') not in (None, 'TEXT'):
            return [], 'unsupported_component'
        text = row.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > LIMITS[field.lower()]:
            return [], 'unsupported_component_content'
        tokens = pattern.findall(text)
        if ((field == 'HEADER' and len(tokens) > 1) or (field == 'FOOTER' and tokens) or
            len(set(tokens)) > 99 or re.search(r'[{}]', pattern.sub('', text)) or '\x00' in text or
            field != 'BODY' and re.search(r'[\r\n]', text)):
            return [], 'unsupported_parameter_names' if named else 'unsupported_parameter_positions'
        if named and any(not _internal_name(token) for token in tokens):
            return [], 'unsupported_named_parameters'
        if named and not _named_examples_match(row.get('example'), field, tokens):
            return [], 'invalid_named_parameter_examples'
        if not named and sorted(map(int, tokens)) != list(range(1, len(tokens) + 1)):
            return [], 'unsupported_parameter_positions'
        result[field] = dict(type=field, text=text, **({'format': 'TEXT'} if field == 'HEADER' else {}))
    if 'BODY' not in result:
        return [], 'unsupported_component'
    return [result[field.upper()] for field in LIMITS if field.upper() in result], None


def _reading(row):
    name, language, status = (row.get(field) for field in ('name', 'language', 'status'))
    if (not isinstance(name, str) or not REMOTE_NAME.fullmatch(name) or not isinstance(language, str)
        or not language or len(language) > 140 or not language.isprintable()
        or not isinstance(status, str) or not STATUS.fullmatch(status) or not numeric_id(row.get('id'))):
        raise GraphError()
    content, issue = _remote_components(row)
    if not NAME.fullmatch(name):
        issue = issue or 'unsupported_template_name'
    if language not in LANGUAGE_CODES:
        issue = 'unsupported_language'
    parameter_format = row.get('parameter_format')
    metadata = dict(name=name, provider_language=language)
    if parameter_format in (None, *FORMAT_CODES):
        parameter_format = metadata['parameter_format'] = parameter_format or 'POSITIONAL'
    else:
        parameter_format = None
    if isinstance(category := row.get('category'), str) and 0 < len(category) <= 140 and category.isprintable():
        metadata['category'] = category
    return dict(reference=row['id'], key=definition_hash(dict(name=name, language=language)),
        revision=_revision(parameter_format, content), status=status, issue=issue,
        publication='rejected' if status in PUBLICATION_REJECTED else 'pending' if status in PUBLICATION_PENDING else 'confirmed',
        content=dict(name=name, language=language, parameter_format=parameter_format, components=content), metadata=metadata)


def list_templates(binding):
    return [_reading(row) for row in client(binding['account']).templates()]


def matching_template(graph, key):
    matching = [row for row in graph.templates(key['name']) if all(row.get(field) == entry for field, entry in key.items())]
    if len(matching) > 1:
        raise GraphError()
    return matching[0] if matching else None


def query_template_status(template, account):
    match = matching_template(client(account), identity(template))
    return _reading(match) if match else None


def verify_template_revision(binding, snapshot):
    graph = client(binding['account'])
    if graph.business != snapshot['scope']:
        return False
    delivery = snapshot['delivery']
    match = matching_template(graph, dict(name=delivery['name'], language=delivery['language']))
    if not match:
        return False
    reading = _reading(match)
    if reading['issue'] or (reading['reference'], reading['revision']) != (snapshot['reference'], snapshot['remote_revision']):
        return False
    return reading['status']


def _positional_name(field, position):
    prefix, remainder = divmod(position - 1, 26)
    return field + '_' + (ascii_lowercase[prefix - 1] if prefix else '') + ascii_lowercase[remainder]


def _preferred_component_keys(preferred, field):
    text = preferred.get(field) or ''
    used = set(TEMPLATE_PLACEHOLDER_PATTERN.findall(text))
    if not used:
        return []
    legacy = (preferred.get('parameters') or {}).get('position_order')
    if legacy:
        try:
            order = json.loads(legacy, object_pairs_hook=_unique_object).get(field, [])
        except (TypeError, ValueError):
            order = []
        if isinstance(order, list) and all(isinstance(key, str) for key in order):
            return [*(key for key in order if key in used),
                *(key for key in TEMPLATE_PLACEHOLDER_PATTERN.findall(text) if key not in order)]
    variables = preferred.get('variables') or []
    return [*(key for key in variables if key in used),
        *(key for key in TEMPLATE_PLACEHOLDER_PATTERN.findall(text) if key not in variables)]


def _project_named(remote):
    names = list(dict.fromkeys(name for part in remote['components'] for name in NAMED.findall(part['text'])))
    if any(not _internal_name(name) for name in names):
        frappe.throw(_('WhatsApp named template parameters are incompatible with Messaging parameter naming rules.'))
    fields = dict.fromkeys(LIMITS, '')
    named = {}
    for part in remote['components']:
        field, text = part['type'].lower(), part['text']
        keys = {name: name for name in sorted(dict.fromkeys(NAMED.findall(text)))}
        if keys:
            named[field] = keys
        fields[field] = text
    return dict(draft=dict(template_name=remote['name'], provider_language=remote['language'],
        parameters=dict(parameter_format='NAMED'), variables=names, **fields),
        delivery=dict(name=remote['name'], language=remote['language'], parameter_format='NAMED', names=named))


def project_template(template, reading):
    remote, preferred = reading['content'], reading['preferred']
    parameter_format = remote['parameter_format']
    if preferred.get('parameters', {}).get('parameter_format', 'POSITIONAL') != parameter_format:
        preferred = {}
    if parameter_format == 'NAMED':
        return _project_named(remote)
    mapped, fields, reserved = {}, dict.fromkeys(LIMITS, ''), set()
    for component in remote['components']:
        field, text = component['type'].lower(), component['text']
        original = _preferred_component_keys(preferred, field)
        names = []
        for position in range(1, len(POSITION.findall(text)) + 1):
            key = original[position - 1] if position <= len(original) else _positional_name(field, position)
            while key in reserved or (position > len(original) and key in original):
                key += '_position'
            reserved.add(key)
            names.append(key)
        if names:
            mapped[field] = names
        fields[field] = POSITION.sub(lambda match: '{{%s}}' % names[int(match[1]) - 1], text)
    return dict(draft=dict(template_name=remote['name'], provider_language=remote['language'],
        parameters={}, variables=[key for names in mapped.values() for key in names], **fields),
        delivery=dict(name=remote['name'], language=remote['language'], positions=mapped))


def prepare_template_publication(template, options):
    category, examples = options.get('category'), options.get('examples')
    if category not in CATEGORIES:
        frappe.throw(_('Select a WhatsApp template identity and category.'))
    try:
        examples = json.loads(examples or '{}') if isinstance(examples, str) else examples or {}
    except (TypeError, ValueError):
        examples = None
    expected = {row.parameter_name for row in template.template_variables}
    if (not isinstance(examples, dict) or set(examples) != expected
        or any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in examples.values())):
        frappe.throw(_('Enter an example for each WhatsApp template variable.'))
    parameter_format = _format(template)
    remote = options.get('remote')
    if remote and remote['content']['parameter_format'] != parameter_format:
        frappe.throw(_('The external template parameter format is immutable.'))
    rows = []
    for component, parameters in components(template):
        if parameters:
            field = component['type'].lower()
            if parameter_format == 'NAMED':
                component['example'] = {field + '_text_named_params': [
                    dict(param_name=name, example=examples[key]) for key, name in parameters.items()]}
            else:
                values = [examples[key] for key in parameters]
                component['example'] = dict(header_text=values) if field == 'header' else dict(body_text=[values])
        rows.append(component)
    payload = dict(category=category, components=rows)
    if not remote:
        payload.update(identity(template))
    if not remote or parameter_format == 'NAMED':
        payload['parameter_format'] = parameter_format
    return dict(payload=payload, reference=remote['reference'] if remote else None)


def publish_template(binding, request):
    try:
        response = client(binding['account']).save_template(request['payload'], request['reference'])
    except GraphError as error:
        return dict(outcome='rejected' if error.rejected else 'unknown')
    if response.get('success') is False:
        return dict(outcome='rejected')
    reference = response.get('id') or request['reference']
    if numeric_id(reference) and (not request['reference'] or request['reference'] == reference):
        return dict(outcome='submitted', reference=reference)
    return dict(outcome='unknown')


def prepare_provider_template(envelope):
    from messaging.communication.doctype.communication_template.external import published_contract
    if not envelope.get('template_revision'):
        raise ValueError('Missing WhatsApp template revision')
    snapshot = published_contract(envelope['template_reference'], envelope['account'], envelope['template_revision'])
    variables, delivery = envelope['variables'], snapshot['external']['delivery']
    if snapshot['external']['key'] != definition_hash(dict(name=delivery['name'], language=delivery['language'])):
        raise ValueError('Invalid WhatsApp delivery identity')
    validate_output_variables(variables, snapshot['variables'])
    return message_template(delivery, variables)


def _parameter_text(value):
    if not isinstance(value, str):
        raise ValueError('Invalid WhatsApp template parameter')
    lines = PARAMETER_LINE_BREAK.split(value)
    if len(lines) > 1:
        value = '; '.join(line.strip(' \t') for line in lines if line.strip(' \t'))
    value = PARAMETER_SPACING.sub(' ', value)
    if not value.strip() or PARAMETER_CONTROLS.search(value):
        raise ValueError('Invalid WhatsApp template parameter')
    return value


def message_template(delivery, variables):
    variables = {key: _parameter_text(value) for key, value in variables.items()}
    validate_output_variables(variables)
    if delivery['language'] not in LANGUAGE_CODES:
        raise ValueError('Invalid WhatsApp template language')
    parameter_format = delivery.get('parameter_format', 'POSITIONAL')
    if parameter_format == 'NAMED' and set(delivery) == {'name', 'language', 'parameter_format', 'names'}:
        components = delivery['names']
    elif parameter_format == 'POSITIONAL' and set(delivery) in (
        {'name', 'language', 'positions'}, {'name', 'language', 'parameter_format', 'positions'}
    ):
        components = delivery['positions']
    else:
        raise ValueError('Invalid WhatsApp template parameter format')
    if not isinstance(components, dict) or set(components) - {'header', 'body'}:
        raise ValueError('Invalid WhatsApp template parameter components')
    mapped = []
    named = parameter_format == 'NAMED'
    used = {}
    for field in ('header', 'body'):
        if field not in components:
            continue
        entries = components[field]
        if not isinstance(entries, dict if named else list) or not entries or len(entries) > (1 if field == 'header' else 99):
            raise ValueError('Invalid WhatsApp template parameter components')
        parameters = []
        for key, name in (sorted(entries.items()) if named else ((key, key) for key in entries)):
            if (not _internal_name(key) or not _external_name(name) or key in used and (not named or used[key] != name)
                or name in used.values() and key not in used):
                raise ValueError('Invalid WhatsApp template parameter mapping')
            used[key] = name
            parameter = dict(type='text', text=variables[key])
            if named:
                parameter['parameter_name'] = name
            parameters.append(parameter)
        mapped.append(dict(type=field, parameters=parameters))
    if set(used) != set(variables):
        raise ValueError('Invalid WhatsApp template parameter mapping')
    return dict(name=delivery['name'], language=dict(code=delivery['language']), components=mapped)


def affected_templates(channel, account, waba, name, language):
    key = definition_hash(dict(name=name, language=language))
    own = frappe.db.get_value('Communication Template',
        dict(channel=channel, account=account, external_key=key), 'name')
    if not own:
        from messaging.whatsapp.configuration import PROVIDER
        channels = frappe.get_all('Communication Channel', filters={'provider': PROVIDER}, pluck='name')
        if channels:
            own = frappe.db.get_value('Communication Template',
                dict(channel=['in', channels], external_scope=waba, external_key=key), 'name', order_by='name')
    return [own] if own else []
