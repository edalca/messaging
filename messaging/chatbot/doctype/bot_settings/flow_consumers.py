"""Settings selections over the synchronized contract; no discovery or business invocation."""
import hashlib
import json
import re

import frappe
from frappe import _

from messaging.communication.doctype.process_definition.approval import is_current
from messaging.chatbot.doctype.bot_flow.contract import canonical_contract, validate_bindings
from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import state_name
from messaging.registry.processes import FIELD_TYPES, _identifier, validate_output_data

CONSUMERS = {
    'registration': ('registration_flow', 'registration_states', 'registration_fields'),
    'greeting': ('greeting_flow', 'greeting_states', None),
    'human': ('human_flow', 'human_transfer_states', 'human_fields'),
}



class ConsumerIssue(frappe.ValidationError):
    """A known administrative violation of a consumer's own contract."""
    def __init__(self, message, relation, kind='incomplete'):
        super().__init__(message)
        self.relation = relation
        self.kind = kind

def output_catalog(flow):
    """Validate the persisted projection without executing its Python handler."""
    from messaging.communication.doctype.process_definition.process_definition import resolve_definition
    definition = resolve_definition(flow.process_definition, expected_type='bot_flow')
    contract = frappe.parse_json(flow.process_contract or '{}')
    if (contract.get('definition') != definition.name or flow.process_method != definition.method
        or hashlib.sha256(canonical_contract(contract).encode()).hexdigest() != flow.process_contract_fingerprint
        or not isinstance(contract.get('outcomes'), dict)):
        raise ConsumerIssue(_('The Bot Flow contract needs to be synchronized and validated.'), 'flow', 'technical_required')
    outputs = {}
    for row in flow.outputs:
        outcome = contract['outcomes'].get(row.status_code)
        fields = outcome['fields'] if outcome else None
        from messaging.chatbot.doctype.bot_flow.contract import declared_action
        if outcome and (row.output_mode.lower() not in outcome['mode'] or row.flow_action != declared_action(outcome)):
            raise ConsumerIssue(_('The Bot Flow contract needs to be synchronized and validated.'), 'flow', 'technical_required')
        if (fields is None or row.status_code in outputs or fields != frappe.parse_json(row.output_fields or '{}')
            or not isinstance(fields, dict) or any(kind not in FIELD_TYPES for kind in fields.values())):
            raise ConsumerIssue(_('The Bot Flow contract needs to be synchronized and validated.'), 'flow', 'technical_required')
        _identifier(row.status_code)
        for key in fields:
            _identifier(key)
        outputs[row.status_code] = dict(state=state_name(flow.name, row.status_code),
            status_code=row.status_code, action=row.flow_action, fields=fields)
    if set(outputs) != set(contract['outcomes']):
        raise ConsumerIssue(_('The Bot Flow contract needs to be synchronized and validated.'), 'flow', 'technical_required')
    try:
        validate_bindings(flow)
    except frappe.ValidationError as error:
        raise ConsumerIssue(str(error), 'flow', 'technical_required') from error
    return outputs


def field_union(outputs, statuses):
    fields = {}
    for status in statuses:
        if status not in outputs or outputs[status]['action'] != 'Complete Flow':
            raise ConsumerIssue(_('Select only Complete Flow states from the selected Bot Flow.'), 'states')
        for key, kind in outputs[status]['fields'].items():
            if key in fields and fields[key]['type'] != kind:
                raise ConsumerIssue(_('Output field {0} has inconsistent types across selected states.').format(key), 'fields')
            entry = fields.setdefault(key, dict(field_key=key, type=kind, states=[]))
            entry['states'].append(status)
    return fields


def selected_states(flow, outputs, rows):
    names = {item['state']: status for status, item in outputs.items()}
    states = []
    for row in rows:
        state = frappe.db.get_value('Bot Flow State', row.state, ['bot_flow', 'status_code'], as_dict=True)
        if (not state or state.bot_flow != flow.name or row.state not in names
            or state.status_code != names[row.state] or names[row.state] in states):
            raise ConsumerIssue(_('Functional states must be unique and belong to the selected Bot Flow.'), 'states')
        if outputs[names[row.state]]['action'] != 'Complete Flow':
            raise ConsumerIssue(_('Select only Complete Flow states from the selected Bot Flow.'), 'states')
        states.append(names[row.state])
    return states


def validate_registration(flow, outputs, states, fields):
    """Every authorized success must persist data from a Flow with required capture."""
    if not any(row.required for row in flow.parameters):
        raise ConsumerIssue(_('The Registration Bot Flow must have at least one required input parameter.'), 'flow')
    if not states:
        raise ConsumerIssue(_('Select at least one Complete Flow state for successful registration.'), 'states')
    selected = {row.field_key for row in fields if row.keep}
    declared = field_union(outputs, states)
    if selected - declared.keys():
        raise ConsumerIssue(_('Output fields must be unique and declared by the selected functional states.'), 'fields')
    for status in states:
        if not outputs[status]['fields']:
            raise ConsumerIssue(_('Registration state {0} must declare at least one output field.').format(status), 'flow')
        if not selected.intersection(outputs[status]['fields']):
            raise ConsumerIssue(_('Select Add to Registration for at least one output field of state {0}.').format(status), 'fields')


def validate_registration_flow(settings, flow):
    """Protect enabled registration when its ordinary Flow is edited."""
    if settings.registration_enabled and settings.registration_flow == flow.name:
        outputs = output_catalog(flow)
        validate_registration(flow, outputs, selected_states(flow, outputs, settings.registration_states),
            settings.registration_fields)


def validate_selection(settings, consumer):
    """Disabled consumers retain their selections; enabled consumers need current readiness."""
    flow_field, _states, _fields = CONSUMERS[consumer]
    enabled_field = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
    selected = settings.get(flow_field)
    if selected and settings.get(enabled_field):
        flow = frappe.get_doc('Bot Flow', selected)
        flow.check_permission('read')
        if (flow.status != 'Enabled' or not is_current(flow)):
            raise frappe.ValidationError(_('Select an effectively enabled Bot Flow.'))



def configuration(settings, consumer, *, operational=False, synchronize_fields=False, expected_flow=None, read_only=False):
    if synchronize_fields:
        validate_selection(settings, consumer)
    flow_field, states_field, fields_field = CONSUMERS[consumer]
    enabled_field = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
    if synchronize_fields and not settings.get(enabled_field):
        return None
    if operational and not settings.get(enabled_field):
        raise frappe.ValidationError(_('The Flow consumer is disabled.'))
    selected = settings.get(flow_field)
    if expected_flow is not None and selected != expected_flow:
        raise ConsumerIssue(_('The selected Bot Flow has changed.'), 'flow')
    rows = settings.get(states_field) or []
    configured = (settings.get(fields_field) or []) if fields_field else []
    if not selected:
        if consumer == 'registration' and settings.registration_enabled:
            raise ConsumerIssue(_('Select a valid Registration Bot Flow.'), 'flow')
        if operational or rows or configured:
            raise ConsumerIssue(_('Select a Bot Flow before configuring its functional states and fields.'), 'flow')
        return None
    flow = frappe.get_doc('Bot Flow', selected, for_update=operational and not read_only)
    flow.check_permission('read')
    if operational and not read_only and (flow.status != 'Enabled' or not is_current(flow)):
        raise frappe.ValidationError(_('Select an effectively enabled Bot Flow.'))
    outputs = output_catalog(flow)
    states = selected_states(flow, outputs, rows)
    union = field_union(outputs, states) if fields_field else {}
    if consumer == 'registration' and settings.registration_enabled:
        validate_registration(flow, outputs, states, configured)
    old = settings.get_doc_before_save()
    if old and old.get(flow_field) != selected:
        previous = {r.name for table in (states_field, fields_field) if table for r in old.get(table) or []}
        if any(r.name in previous for r in [*rows, *configured]):
            raise ConsumerIssue(_('Clear the previous Flow selections before configuring another Bot Flow.'), 'states')
    previous_fields = {r.name: r.field_key for r in old.get(fields_field) or []} if old and fields_field else {}
    seen = set()
    for row in configured:
        if row.name in previous_fields and previous_fields[row.name] != row.field_key:
            raise ConsumerIssue(_('Field Key cannot be changed.'), 'fields')
        if row.field_key not in union or row.field_key in seen:
            raise ConsumerIssue(_('Output fields must be unique and declared by the selected functional states.'), 'fields')
        seen.add(row.field_key)
        label = validate_label(row.label)
        if not read_only:
            row.label = label
        expected_states = ', '.join(union[row.field_key]['states'])
        if consumer == 'human':
            if not synchronize_fields and row.states != expected_states:
                raise ConsumerIssue(_('Output field states no longer match the selected contract.'), 'fields')
            if not read_only:
                row.states = expected_states
        if row.keep not in (0, 1):
            raise ConsumerIssue(_('Select a valid Save checkbox value.'), 'fields')
    if synchronize_fields:
        for key, item in union.items():
            if key not in seen:
                settings.append(fields_field, dict(field_key=key, label=key.replace('_', ' ').capitalize(), keep=0,
                    **({'states': ', '.join(item['states'])} if consumer == 'human' else {})))
    elif seen != set(union):
        raise ConsumerIssue(_('Output fields no longer match the selected contract. Save Bot Settings again.'), 'fields')
    if operational:
        from messaging.communication.doctype.process_definition.approval import require_approved
        if flow.status != 'Enabled' or not states:
            raise ConsumerIssue(_('Enable and validate the Bot Flow and select at least one Complete Flow state.'), 'states')
        require_approved(flow)
        for row in flow.parameters:
            from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_request_phrase
            if not frappe.db.get_value('Bot Parameter', row.parameter, 'enabled'):
                raise ConsumerIssue(_('The selected Bot Flow has a disabled parameter.'), 'flow')
            try:
                get_request_phrase(row.parameter, settings=settings)
            except frappe.ValidationError as error:
                raise ConsumerIssue(str(error), 'flow') from error
    return dict(flow=flow, outputs=outputs, states=states, fields=union,
        selected={r.field_key: r for r in configured if r.keep})


def validate_label(value):
    if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 140
        or any(ord(char) < 32 for char in value) or re.search(r'[<>]', value)):
        raise ConsumerIssue(_('Use a nonempty plain text Label of at most 140 characters.'), 'fields')
    return value.strip()


def selected_values(config, status, data):
    if status not in config['states'] or config['outputs'][status]['action'] != 'Complete Flow':
        raise frappe.ValidationError(_('The functional state is not authorized for this operation.'))
    declared = config['outputs'][status]['fields']
    validate_output_data(declared, data)
    if any(key not in data for key in config['selected'] if key in declared):
        raise frappe.ValidationError(_('Process result does not match the selected output fields.'))
    return {key: dict(label=row.label, value=data[key], type=declared[key])
        for key, row in config['selected'].items() if key in declared}


def revision(config):
    return hashlib.sha256(canonical_contract(dict(flow=config['flow'].name,
        fingerprint=config['flow'].approval_hash, states=sorted(config['states']),
        fields=sorted(config['selected']))).encode()).hexdigest()


@frappe.whitelist()
def options(flow, states=None, consumer='registration'):
    """Desk preview, based exclusively on synchronized outputs and native Link identities."""
    frappe.has_permission('Bot Settings', 'read', throw=True)
    if consumer not in CONSUMERS:
        raise frappe.ValidationError(_('Invalid Flow consumer.'))
    doc = frappe.get_doc('Bot Flow', flow)
    doc.check_permission('read')
    outputs = output_catalog(doc)
    eligible = [dict(state=item['state'], status_code=status, fields=item['fields']) for status, item in outputs.items()
        if item['action'] == 'Complete Flow']
    names = {item['state']: item['status_code'] for item in eligible}
    selected = frappe.parse_json(states or '[]')
    if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected):
        raise ConsumerIssue(_('Select only Complete Flow states from the selected Bot Flow.'), 'states')
    if len(set(selected)) != len(selected):
        raise ConsumerIssue(_('A Complete Flow state is selected more than once. Remove the duplicate selection.'), 'states')
    if any(name not in names for name in selected):
        raise ConsumerIssue(_('Select only Complete Flow states from the selected Bot Flow.'), 'states')
    union = field_union(outputs, [names[name] for name in selected]) if CONSUMERS[consumer][2] else {}
    return dict(flow=flow, states=eligible, fields=list(union.values()),
        has_required_parameters=any(row.required for row in doc.parameters))


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def state_options(doctype, txt, searchfield, start, page_len, filters):
    catalog = options(filters.get('flow'), '[]') if filters.get('flow') else dict(states=[])
    matches = [row for row in catalog['states'] if txt.casefold() in row['status_code'].casefold() or txt.casefold() == row['state'].casefold()]
    return [(row['state'], row['status_code']) for row in matches[int(start):int(start) + min(int(page_len), 100)]]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def flow_options(doctype, txt, searchfield, start, page_len, filters):
    """Filter before pagination, using the same effective state as execution."""
    frappe.has_permission('Bot Settings', 'read', throw=True)
    matches = []
    offset, size = max(0, int(start)), min(max(1, int(page_len)), 100)
    scanned, batch_size = 0, max(20, size)
    while len(matches) < offset + size:
        rows = frappe.get_list('Bot Flow', filters={'status': 'Enabled'},
            or_filters={'name': ['like', '%' + txt + '%'], 'workflow_name': ['like', '%' + txt + '%']},
            fields=['name', 'workflow_name'], order_by='workflow_name, name',
            limit_start=scanned, limit_page_length=batch_size)
        for row in rows:
            flow = frappe.get_doc('Bot Flow', row.name)
            if flow.has_permission('read') and flow.status == 'Enabled' and is_current(flow):
                matches.append((row.name, row.workflow_name))
                if len(matches) >= offset + size:
                    break
        if len(rows) < batch_size:
            break
        scanned += len(rows)
    return matches[offset:offset + size]


CONSUMER_LABELS = {'registration': 'Registration', 'greeting': 'Greeting', 'human': 'Human Support'}
CONSUMER_TABS = {'registration': 'registration_tab', 'greeting': 'greeting_tab', 'human': 'human_support_tab'}


def _verification_error(consumer, *, defer=False):
    """Log a bounded stack without candidate values, exception text or local variables."""
    import sys
    import traceback
    error = sys.exception()
    trace = ''.join(f'File {frame.filename!r}, line {frame.lineno}, in {frame.name}\n'
        for frame in traceback.extract_tb(error.__traceback__)) + type(error).__name__
    key = 'messaging:settings_verification:' + hashlib.sha256((consumer + trace).encode()).hexdigest()
    try:
        reference = frappe.cache.get_value(key)
        if reference and frappe.db.exists('Error Log', reference):
            return reference
        log = frappe.log_error(title='Bot Settings Flow verification', message=trace,
            reference_doctype='Bot Settings', reference_name='Bot Settings', defer_insert=defer)
        reference = log.name if log else None
        if reference:
            frappe.cache.set_value(key, reference, expires_in_sec=300)
        return reference
    except Exception:
        # Preserve safe evidence even when the database or Redis cannot record Error Log.
        frappe.logger('messaging').error(trace)
        return None


def configuration_signature(settings, consumer):
    """Compare consumer values, excluding document timestamps and other row metadata."""
    flow_field, states_field, fields_field = CONSUMERS[consumer]
    enabled = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
    fields = [enabled, flow_field, states_field, 'chatbot_languages',
        'max_invalid_attempts', 'show_menu_on_attempts_exhausted']
    tables = {states_field: ('state',), 'chatbot_languages': ('language', 'enabled', 'is_default')}
    if fields_field:
        fields.append(fields_field)
        tables[fields_field] = ('field_key', 'label', 'keep', 'states') if consumer == 'human' else ('field_key', 'label', 'keep')
    if consumer == 'registration':
        fields.extend(('exempt_registration_from_quotas', 'max_registration_exempt_messages'))
    elif consumer == 'greeting':
        fields.extend(('registration_flow', 'human_flow'))
    else:
        fields.extend(('human_support_week', 'human_inactivity_minutes', 'human_requests_required'))
        tables['human_support_week'] = ('weekday', 'enabled', 'start_time', 'end_time')
    return tuple((field, tuple(tuple(str(row.get(key)) if key in ('start_time', 'end_time') and row.get(key) is not None
            else row.get(key) for key in tables[field]) for row in settings.get(field) or [])
        if field in tables else settings.get(field)) for field in fields)


def configuration_changed(settings, consumer):
    """Only edits of this consumer's configuration require its detailed validation."""
    previous = settings.get_doc_before_save()
    if previous is None:
        previous = frappe.get_single('Bot Settings')
    return configuration_signature(settings, consumer) != configuration_signature(previous, consumer)


def diagnose_consumer(settings, consumer, *, defer_errors=False):
    """Read effective availability; validate the consumer contract only after relevant edits."""
    from messaging.communication.doctype.process_definition.process_definition import resolve_definition, ProcessUnavailableError
    from messaging.registry.processes.discovery import verify_registered_revision
    from messaging.chatbot.doctype.bot_flow.localization import require_default_language

    flow_field, states_field, fields_field = CONSUMERS[consumer]
    enabled_field = 'human_attention_enabled' if consumer == 'human' else consumer + '_enabled'
    result = dict(consumer=consumer, label=_(CONSUMER_LABELS[consumer]), tab=CONSUMER_TABS[consumer],
        flow=settings.get(flow_field), state='disabled', issues=[], catalog=None, configuration_checked=False)
    if not settings.get(enabled_field):
        return result
    state, field = 'incomplete', flow_field
    action = _('Select a Bot Flow, or disable this feature to keep its selections.')
    try:
        if not result['flow']:
            raise ConsumerIssue(_('Select a Bot Flow for this enabled feature.'), 'flow')
        state = 'unavailable'
        action = _('Restore the dependency and enable the Bot Flow, or disable this feature.')
        flow = frappe.get_doc('Bot Flow', result['flow'])
        flow.check_permission('read')
        result['flow_label'] = flow.workflow_name or flow.name
        definition = resolve_definition(flow.process_definition, expected_type='bot_flow')
        if flow.status != 'Enabled':
            raise ConsumerIssue(_('The selected Bot Flow is disabled.'), 'flow', 'unavailable')
        state = 'technical_required'
        action = _('Synchronize the Process owning application if needed, then review and validate the Bot Flow.')
        if not is_current(flow):
            raise ConsumerIssue(_('The Bot Flow has no current technical approval for its Process revision and dependencies.'), 'flow', 'technical_required')
        if not configuration_changed(settings, consumer):
            result['state'] = 'operational'
            return result
        result['configuration_checked'] = True
        try:
            verify_registered_revision(definition)
        except frappe.ValidationError as error:
            raise ConsumerIssue(str(error), 'flow', 'technical_required') from error
        # The catalogue stays useful when a field selection is incomplete.
        result['catalog'] = options(flow.name, json.dumps([row.state for row in settings.get(states_field) or []]), consumer)
        state, field = 'incomplete', states_field
        action = _('Review the Complete Flow states and output fields in this tab, or disable the feature.')
        if not settings.get(states_field):
            raise ConsumerIssue(_('Select at least one Complete Flow state for this enabled feature.'), 'states')
        try:
            require_default_language(flow, settings=settings)
        except frappe.ValidationError as error:
            raise ConsumerIssue(str(error), 'flow') from error
        if consumer == 'registration':
            settings._validate_registration(read_only=True)
        elif consumer == 'greeting':
            settings._validate_greeting_flow(read_only=True)
        else:
            settings.validate_human_configuration(read_only=True)
        result['state'] = 'operational'
        return result
    except ConsumerIssue as error:
        field = {'flow': flow_field, 'states': states_field, 'fields': fields_field}.get(error.relation, error.relation)
        state = error.kind
        if state == 'incomplete':
            action = _('Review the affected field in this tab and the selected Flow contract, or disable the feature.')
            if error.relation == 'fields':
                action = _('Reselect the Complete Flow states to refresh output fields, then review the fields to keep; or disable the feature.')
            elif error.relation == 'states':
                action = _('Select unique Complete Flow states from this Flow, or disable the feature.')
        message, reference = str(error), None
    except ProcessUnavailableError as error:
        state = 'unavailable'
        message, reference = str(error), None
        action = _('Restore the registered Process and its owning application, or disable this feature.')
    except frappe.DoesNotExistError:
        state = 'unavailable'
        message, reference = _('A required Bot Flow or dependency no longer exists.'), None
    except frappe.PermissionError:
        state = 'internal_error'
        message = _('The current user cannot verify a required dependency.')
        reference = _verification_error(consumer, defer=defer_errors)
        action = _('Review dependency permissions and the technical error log, then check again.')
    except Exception:
        state = 'internal_error'
        message = _('An internal error prevented verification of the Bot Flow. Its availability cannot be confirmed.')
        reference = _verification_error(consumer, defer=defer_errors)
        action = _('Review the technical error log, then check again.')
    result['state'] = state
    result['issues'] = [dict(field=field, kind=state, severity='error', message=message, action=action,
        reference=reference)]
    return result


def require_valid_consumers(settings):
    """A Single save must not retain an enabled but unusable consumer."""
    from frappe.utils import escape_html
    issues = []
    for consumer in CONSUMERS:
        diagnostic = diagnose_consumer(settings, consumer, defer_errors=True)
        for issue in diagnostic['issues']:
            issues.append('{0} / {1}: {2} {3}'.format(diagnostic['label'], issue['field'], issue['message'], issue['action']))
    if issues:
        frappe.throw('<br>'.join(escape_html(issue) for issue in issues))


@frappe.whitelist(methods=['POST'])
def diagnose(candidate, consumer):
    """Administrative preview over an unsaved Single, with no Process execution or synchronization."""
    frappe.has_permission('Bot Settings', 'read', throw=True)
    if consumer not in CONSUMERS:
        raise frappe.ValidationError(_('Invalid Flow consumer.'))
    data = frappe.parse_json(candidate)
    if not isinstance(data, dict) or data.get('doctype') != 'Bot Settings' or data.get('name') != 'Bot Settings':
        raise frappe.ValidationError(_('Invalid Bot Settings candidate.'))
    doc = frappe.get_doc(data)
    doc.check_permission('read')
    doc._doc_before_save = frappe.get_single('Bot Settings')
    return diagnose_consumer(doc, consumer)
