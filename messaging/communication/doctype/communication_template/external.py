"""Communication Template ownership of external readings, projections and publication."""
from contextlib import contextmanager
import json
import re

import frappe
from frappe import _

from messaging.common.locking import connection_lock, document_savepoint, lock_document_types
from messaging.common.write_guards import InternalWriteGuard
from messaging.communication.doctype.communication_provider.communication_provider import (
    definition_hash, operation, provider_definition, require_provider_enabled,
)
from messaging.security.permissions import require_system_manager
from .languages import require_language, resolve_language

DOCTYPE = 'Communication Template'
external_write = InternalWriteGuard('messaging_template_external_write')
PROTECTED_FIELDS = (
    'sync_state', 'published_revision', 'published_snapshot', 'externally_observed',
    'publication_request', 'external_scope', 'external_key', 'last_sync_error', 'previous_name',
)
CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')


def managed(definition):
    return bool(definition.get('template_statuses'))


def requires_published(definition):
    return bool({'provider_templates', 'template_status'} & set(definition['capabilities']))


def text(value):
    return isinstance(value, str) and 0 < len(value) <= 140 and value == value.strip() and value.isprintable()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def stored(doc, field):
    return json.loads(doc.get(field) or '{}')


def validate_scope(doc, definition, account=None):
    own = doc.get('account')
    if definition.get('template_scope', 'Channel') == 'Account':
        if not own:
            frappe.throw(_('Select a Communication Channel Account for this template.'))
        if account and account != own:
            frappe.throw(_('The template does not match the channel account.'))
    elif own:
        frappe.throw(_('This provider manages templates by channel.'))
    if (own or account) and frappe.db.get_value('Communication Channel Account', own or account, 'channel') != doc.channel:
        frappe.throw(_('The template channel does not match the channel account.'))


def context(definition, channel, account):
    if not channel or frappe.db.get_value('Communication Channel', channel, 'provider') != definition['id']:
        frappe.throw(_('Select a channel belonging to this provider.'))
    validate_scope(frappe._dict(channel=channel, account=account), definition)
    return dict(channel=channel, account=account or None)


def scope_key(definition, binding):
    key = operation(definition, 'template_scope_key', binding)
    if not text(key):
        frappe.throw(_('Invalid external template scope.'))
    return key


def describe(doc, definition):
    result = operation(definition, 'describe_template', doc)
    if not isinstance(result, dict) or set(result) != {'key', 'revision'} or any(not text(value) for value in result.values()):
        frappe.throw(_('Invalid external template description.'))
    return result


def _declared_template_parameters(definition):
    return {p['parameter_key'] for p in definition.get('parameters', []) if p.get('scope') == 'Template'}


def _is_legacy_snapshot(doc, snapshot, definition):
    if not snapshot or not isinstance(snapshot.get('draft'), dict):
        return False
    snap_draft = snapshot['draft']
    if doc.provider_language and 'provider_language' not in snap_draft:
        return True
    snap_params = snap_draft.get('parameters') or {}
    return bool(set(snap_params) - _declared_template_parameters(definition))


def _snapshot_matches_draft(doc, snapshot, definition):
    if not snapshot:
        return False
    if snapshot.get('local_revision') == doc.revision:
        return True
    if not definition or not _is_legacy_snapshot(doc, snapshot, definition):
        return False
    snap_draft = snapshot['draft']
    if any((doc.get(field) or '') != (snap_draft.get(field) or '') for field in ('header', 'body', 'footer')):
        return False
    current_vars = [row.parameter_name for row in doc.template_variables]
    snap_vars = list(snap_draft.get('variables') or [])
    if set(current_vars) != set(snap_vars):
        return False
    current_params = draft(doc)['parameters']
    snap_params = snap_draft.get('parameters') or {}
    formats = definition.get('template_parameter_formats')
    if formats:
        from .communication_template import parameter_format
        if parameter_format(formats, current_params) != parameter_format(formats, snap_params):
            return False
    declared = _declared_template_parameters(definition) - {'parameter_format'}
    if any(current_params.get(key) != snap_params.get(key) for key in declared):
        return False
    return describe(doc, definition) == dict(key=snapshot['key'], revision=snapshot['remote_revision'])


def protect(doc, previous, definition):
    if not external_write.active():
        formats = definition.get('template_parameter_formats')
        if formats and previous and (previous.published_revision or stored(previous, 'publication_request').get('pending')):
            from .communication_template import parameter_format
            if parameter_format(formats, draft(doc)['parameters']) != parameter_format(formats, draft(previous)['parameters']):
                frappe.throw(_('The external template parameter format is immutable.'))
        for field in PROTECTED_FIELDS:
            doc.set(field, previous.get(field) if previous else None)
    if not managed(definition):
        doc.sync_state = 'Pending' if requires_published(definition) else 'Local'
        return
    if doc.is_new() or not doc.external_key:
        doc.external_key = describe(doc, definition)['key']
    elif describe(doc, definition)['key'] != doc.external_key:
        frappe.throw(_('The external template identity is immutable.'))
    if not doc.sync_state:
        doc.sync_state = 'Pending'
    if not external_write.active() and doc.sync_state in ('Pending', 'Synchronized'):
        publication = stored(doc, 'published_snapshot')
        doc.sync_state = 'Synchronized' if _snapshot_matches_draft(doc, publication, definition) else 'Pending'


def draft(doc):
    language = dict(provider_language=doc.provider_language) if doc.provider_language else dict(language=doc.language)
    return dict(template_name=doc.template_name, **language,
        header=doc.header or '', body=doc.body or '', footer=doc.footer or '',
        variables=[row.parameter_name for row in doc.template_variables],
        parameters={row.parameter_key: json.loads(row.value) if row.value else None for row in doc.parameters})


def apply_draft(doc, content):
    for field in ('header', 'body', 'footer'):
        doc.set(field, content[field])
    variables = {row.parameter_name: row for row in doc.template_variables}
    doc.set('template_variables', [variables.get(key) or dict(parameter_name=key) for key in content['variables']])
    for index, row in enumerate(doc.template_variables, 1):
        row.idx = index
    parameters = {row.parameter_key: row for row in doc.parameters}
    doc.set('parameters', [])
    for key, value in content['parameters'].items():
        row = doc.append('parameters', parameters.get(key) or dict(parameter_key=key))
        row.value = encode(value)


def _reading(value, *, expected_key=None):
    required = {'key', 'reference', 'revision', 'status', 'content', 'issue', 'publication'}
    if (not isinstance(value, dict) or set(value) - {'metadata'} != required
        or any(not text(value[field]) for field in ('key', 'reference', 'revision', 'status'))
        or expected_key and value['key'] != expected_key
        or value['publication'] not in ('pending', 'rejected', 'confirmed')
        or not isinstance(value['content'], dict) or len(encode(value['content']).encode()) > 65536
        or value['issue'] is not None and (not isinstance(value['issue'], str) or not CODE.fullmatch(value['issue']))):
        frappe.throw(_('Invalid external template reading.'))
    metadata = value.get('metadata', {})
    if (not isinstance(metadata, dict) or set(metadata) - {'name', 'provider_language', 'category', 'parameter_format'}
        or any(not isinstance(entry, str) or not entry or len(entry) > 512 or not entry.isprintable()
            for entry in metadata.values())):
        frappe.throw(_('Invalid external template reading.'))
    return value


def _observed(record):
    return dict({key: record[key] for key in ('reference', 'revision', 'status', 'publication', 'issue')},
        **{key: record['metadata'][key] for key in ('category', 'parameter_format') if key in record.get('metadata', {})})


def _state(doc, definition=None):
    if doc.last_sync_error:
        return 'Error'
    if stored(doc, 'publication_request').get('pending'):
        return 'Publishing'
    if stored(doc, 'externally_observed').get('issue'):
        return 'Conflict'
    snapshot = stored(doc, 'published_snapshot')
    if _snapshot_matches_draft(doc, snapshot, definition):
        return 'Synchronized'
    return 'Pending'


def _persist(doc, fields):
    saved = {field: doc.get(field) for field in fields}
    frappe.db.set_value(DOCTYPE, doc.name, saved)
    doc.notify_update()


def _fail(doc, code):
    doc.sync_state, doc.last_sync_error = 'Error', code
    _persist(doc, ('sync_state', 'last_sync_error'))


def _missing(doc, scope):
    if doc.external_scope and doc.external_scope != scope:
        return
    if not doc.published_revision and not doc.publication_request:
        return
    observed = stored(doc, 'externally_observed')
    if observed.get('issue') == 'external_template_missing' and not doc.last_sync_error:
        return
    doc.external_scope = scope
    doc.externally_observed = encode(dict(observed, issue='external_template_missing'))
    doc.last_sync_error = None
    doc.sync_state = _state(doc)
    _persist(doc, (*PROTECTED_FIELDS, 'provider_status'))


def _apply(doc, definition, scope, record, *, adopt=False):
    from .communication_template import parameter_format, template_content_revision, validate_template

    previous = stored(doc, 'published_snapshot')
    submitted = stored(doc, 'publication_request')
    first = doc.is_new()
    lifecycle_fields = (*PROTECTED_FIELDS, 'provider_status')
    before = {field: doc.get(field) for field in lifecycle_fields}
    before_draft = None if first else draft(doc)
    observed = _observed(record)
    doc.external_scope = scope
    doc.last_sync_error = None
    formats = definition.get('template_parameter_formats')
    if previous and not adopt:
        if previous['reference'] != record['reference']:
            observed['issue'] = observed['issue'] or 'external_identity_changed'
        elif (formats and observed.get('parameter_format')
                and observed['parameter_format'] != parameter_format(formats, previous['draft']['parameters'])):
            observed['issue'] = observed['issue'] or 'external_parameter_format_changed'
    if observed['issue']:
        if first:
            return 'skipped'
        doc.externally_observed, doc.provider_status = encode(observed), record['status']
        doc.sync_state = _state(doc)
        if any(doc.get(field) != before[field] for field in lifecycle_fields):
            _persist(doc, lifecycle_fields)
        return 'skipped'

    draft_description = None if first else describe(doc, definition)
    target = (bool(submitted.get('pending'))
        and submitted.get('revision') == record['revision']
        and submitted.get('reference') in (None, record['reference'])
        and (not submitted.get('category') or not observed.get('category') or submitted['category'] == observed['category']))
    sendable = definition['template_statuses'].get(record['status'], {}).get('can_send') is True
    published_matches = previous and (previous['reference'], previous['remote_revision']) == (
        record['reference'], record['revision'])
    if not first and not adopt and previous and not published_matches and not sendable:
        doc.externally_observed, doc.provider_status = encode(observed), record['status']
        if target and record['publication'] == 'rejected':
            doc.publication_request = encode(dict(submitted, pending=False, result='rejected'))
            doc.last_sync_error = 'publication_rejected'
        doc.sync_state = _state(doc, definition)
        if any(doc.get(field) != before[field] for field in lifecycle_fields):
            _persist(doc, lifecycle_fields)
            return 'updated'
        return 'unchanged'

    preferred = (submitted['draft'] if target else previous['draft'] if published_matches
        else draft(doc) if draft_description and draft_description['revision'] == record['revision']
        else previous.get('draft') or (draft(doc) if not first else {}))
    projection = operation(definition, 'project_template', doc, dict(record, preferred=preferred))
    if not isinstance(projection, dict) or set(projection) != {'draft', 'delivery'}:
        frappe.throw(_('Invalid external template projection.'))
    content, delivery = projection['draft'], projection['delivery']
    language_field = 'provider_language' if definition.get('template_languages') else 'language'
    if (not isinstance(content, dict) or set(content) != {'template_name', language_field, 'header', 'body', 'footer', 'variables', 'parameters'}
        or not isinstance(delivery, dict) or len(encode(projection).encode()) > 65536
        or not isinstance(content['variables'], list) or not isinstance(content['parameters'], dict)):
        frappe.throw(_('Invalid external template projection.'))
    if first:
        doc.template_name, doc.external_key = content['template_name'], record['key']
        doc.set(language_field, content[language_field])
    if doc.get(language_field) != content[language_field]:
        frappe.throw(_('The external template language does not match the local template.'))
    # Validate the complete projection before storing it as usable evidence.
    projected = frappe.new_doc(DOCTYPE)
    projected.update(dict(template_name=doc.template_name, **{language_field: doc.get(language_field)},
        channel=doc.channel, account=doc.account, status=doc.status,
        published_snapshot=doc.published_snapshot))
    apply_draft(projected, content)
    validate_template(projected, definition=definition, operational=True)
    if describe(projected, definition) != dict(key=record['key'], revision=record['revision']):
        frappe.throw(_('The external template projection does not match the provider revision.'))
    projected_revision = template_content_revision(projected, definition=definition)
    clean = previous and _snapshot_matches_draft(doc, previous, definition)
    matches_draft = (draft_description and draft_description['revision'] == record['revision']
        and set(content['variables']) == {row.parameter_name for row in doc.template_variables}
        and all(content[field] == (doc.get(field) or '') for field in ('header', 'body', 'footer')))
    if first or adopt or clean and not matches_draft:
        apply_draft(doc, content)
    elif matches_draft:
        projected_revision = doc.revision
    elif not target and (not published_matches or stored(doc, 'externally_observed').get('issue') == 'external_content_changed'):
        observed['issue'] = 'external_content_changed'

    if not published_matches or adopt or target and previous.get('local_revision') != projected_revision:
        snapshot = dict(provider=definition['id'], scope=scope, key=record['key'],
            reference=record['reference'], remote_revision=record['revision'],
            draft=content, delivery=delivery, local_revision=projected_revision)
        doc.published_revision = definition_hash(snapshot)
        doc.published_snapshot = encode(snapshot)
    if target and (sendable or record['publication'] == 'rejected'):
        doc.publication_request = encode(dict(submitted, pending=False,
            result='confirmed' if sendable else 'rejected'))
        if not sendable:
            doc.last_sync_error = 'publication_rejected'
    elif adopt and not submitted.get('pending'):
        doc.publication_request = None
    doc.externally_observed, doc.provider_status = encode(observed), record['status']
    doc.revision = template_content_revision(doc, definition=definition)
    doc.sync_state = _state(doc, definition)
    if not first and draft(doc) == before_draft and all(doc.get(field) == before[field] for field in lifecycle_fields):
        return 'unchanged'
    with external_write():
        doc.save(ignore_permissions=True)
    return 'created' if first else 'updated'


def _account_context(doc):
    return dict(channel=doc.channel, account=doc.get('account'))


def _scoped_documents(definition, scope, *, key=None):
    filters = {'external_scope': scope}
    if key:
        filters['external_key'] = key
    names = set(frappe.get_all(DOCTYPE, filters=filters, pluck='name'))
    # Include unverified drafts for the selected binding and for siblings in the scope.
    channels = frappe.get_all('Communication Channel', filters={'provider': definition['id']}, pluck='name')
    draft_filters = {'channel': ['in', channels], 'external_key': key} if key else {'channel': ['in', channels]}
    names.update(frappe.get_all(DOCTYPE, filters=draft_filters, pluck='name'))
    for name in sorted(names):
        doc = frappe.get_doc(DOCTYPE, name, for_update=True)
        if doc.provider != definition['id'] or doc.external_scope and doc.external_scope != scope:
            continue
        binding = _account_context(doc)
        try:
            validate_scope(doc, definition)
            if scope_key(definition, binding) == scope:
                yield doc
        except frappe.ValidationError:
            continue


def _selected(definition, binding, *, permission=True):
    channel, account = binding['channel'], binding['account']
    require_provider_enabled(definition['id'])
    channel_doc = frappe.get_doc('Communication Channel', channel)
    if permission:
        channel_doc.check_permission('read')
    if channel_doc.status != 'Enabled':
        frappe.throw(_('The communication channel is disabled.'))
    if account:
        from messaging.communication.doctype.communication_channel_account.binding import resolve_ingress_binding
        _, account_doc = resolve_ingress_binding(definition['id'], account)
        if permission:
            account_doc.check_permission('read')


def _lock_key(definition, scope):
    return definition_hash(dict(operation='template', provider=definition['id'], scope=scope))


@contextmanager
def scope_lock(definition, scope):
    with connection_lock(_lock_key(definition, scope)) as acquired:
        if not acquired:
            frappe.throw(_('Another operation is in progress for this template scope.'))
        frappe.db.rollback()
        yield


def delivery_lock(definition, channel, account):
    scope = scope_key(definition, dict(channel=channel,
        account=account if definition.get('template_scope') == 'Account' else None))
    return connection_lock(_lock_key(definition, scope), timeout=0)


def _read_one(definition, doc, account):
    reading = operation(definition, 'query_template_status', doc, account)
    return None if reading is None else _reading(reading, expected_key=doc.external_key)


def _refresh(definition, binding, scope, key, preferred_name, *, adopt=False):
    docs = list(_scoped_documents(definition, scope, key=key))
    if not docs:
        return
    source = next((doc for doc in docs if doc.name == preferred_name), docs[0])
    try:
        record = _read_one(definition, source, binding['account'])
    except Exception:
        for doc in docs:
            _fail(doc, 'external_read_failed')
        return
    for doc in docs:
        try:
            with document_savepoint():
                if record is None:
                    _missing(doc, scope)
                else:
                    _apply(doc, definition, scope, record, adopt=adopt and preferred_name == doc.name)
        except (frappe.ValidationError, frappe.DuplicateEntryError):
            _fail(doc, 'external_projection_failed')


@frappe.whitelist(methods=['GET'])
def template_capabilities(channel=None):
    require_system_manager()
    frappe.has_permission(DOCTYPE, 'read', throw=True)
    if channel:
        doc = frappe.get_doc('Communication Channel', channel)
        doc.check_permission('read')
        definition = provider_definition(doc.provider)
        return _capabilities(definition)
    from messaging.communication.doctype.communication_provider.communication_provider import provider_definitions
    result = []
    for provider in provider_definitions():
        try:
            definition = provider_definition(provider)
            if 'list_templates' in definition['operations'] and require_enabled(provider):
                result.append(_capabilities(definition))
        except frappe.ValidationError:
            continue
    return sorted(result, key=lambda row: row['label'])


def require_enabled(provider):
    return frappe.db.get_value('Communication Provider', provider, 'status') == 'Enabled'


def _capabilities(definition):
    operations = definition['operations']
    return dict(provider=definition['id'], label=definition['name'], scope=definition.get('template_scope', 'Channel'),
        synchronize='list_templates' in operations, query='query_template_status' in operations,
        publish='publish_template' in operations, managed=managed(definition),
        fields=definition.get('template_publication_fields', []),
        parameter_formats=definition.get('template_parameter_formats', []),
        languages=[{'code': entry['code'], 'label': entry['label']}
            for entry in definition.get('template_languages', {}).values()])


@frappe.whitelist(methods=['GET'])
def template_language_preview(channel, provider_language):
    require_system_manager()
    frappe.has_permission(DOCTYPE, 'create', throw=True)
    doc = frappe.get_doc('Communication Channel', channel)
    doc.check_permission('read')
    language, origin = resolve_language(provider_definition(doc.provider), provider_language)
    return dict(language=language, origin=origin)


def _diagnostic(record, reason):
    metadata = {key: value for key, value in record.get('metadata', {}).items() if key != 'parameter_format'}
    return dict(metadata, reference=record['reference'], reason=reason)


@frappe.whitelist(methods=['POST'])
def synchronize_templates(provider, channel, account=None):
    require_system_manager()
    for permission in ('create', 'write'):
        frappe.has_permission(DOCTYPE, permission, throw=True)
    definition = provider_definition(provider)
    if 'list_templates' not in definition['operations']:
        frappe.throw(_('This provider does not synchronize templates.'))
    binding = context(definition, channel, account)
    _selected(definition, binding)
    scope = scope_key(definition, binding)
    result = dict(created=0, updated=0, unchanged=0, skipped=[], language_warnings=[], success=True)
    with scope_lock(definition, scope):
        lock_document_types(DOCTYPE)
        _selected(definition, binding)
        if scope_key(definition, binding) != scope:
            frappe.throw(_('The external template scope changed. Reload and try again.'))
        docs = list(_scoped_documents(definition, scope))
        try:
            message_start = len(frappe.local.message_log)
            records = {}
            references = set()
            for value in operation(definition, 'list_templates', binding):
                record = _reading(value)
                if record['key'] in records or record['reference'] in references:
                    frappe.throw(_('The provider returned duplicate template identities.'))
                records[record['key']] = record
                references.add(record['reference'])
        except Exception:
            del frappe.local.message_log[message_start:]
            for doc in docs:
                if doc.external_scope == scope:
                    _fail(doc, 'external_list_failed')
            return dict(result, success=False, error='external_list_failed')
        by_key = {}
        for doc in docs:
            by_key.setdefault(doc.external_key, []).append(doc)
        for key, record in records.items():
            matches = by_key.get(key, [])
            if not record['issue'] and not any(_account_context(doc) == binding for doc in matches):
                matches = [*matches, frappe.new_doc(DOCTYPE).update(dict(binding, status='Inactive'))]
            reason = record['issue']
            for doc in matches:
                new = doc.is_new()
                message_start = len(frappe.local.message_log)
                try:
                    with document_savepoint():
                        applied = _apply(doc, definition, scope, record)
                    if applied != 'skipped':
                        result[applied] += 1
                        if _account_context(doc) == binding and doc.language_mapping in ('Provisional', 'Unassigned'):
                            warning = 'provisional_template_language' if doc.language else 'unassigned_template_language'
                            result['language_warnings'].append(dict(_diagnostic(record, warning), language=doc.language))
                    elif not record['issue']:
                        reason = reason or stored(doc, 'externally_observed').get('issue')
                except (frappe.ValidationError, frappe.DuplicateEntryError):
                    del frappe.local.message_log[message_start:]
                    if not new:
                        _fail(doc, 'external_projection_failed')
                    reason = reason or 'external_projection_failed'
            if reason:
                result['skipped'].append(_diagnostic(record, reason))
        for doc in docs:
            if doc.external_key not in records:
                _missing(doc, scope)
    return result


def refresh_template(name, *, automatic=False, provider=None, channel=None, account=None, adopt=False, modified=None):
    doc = frappe.get_doc(DOCTYPE, name)
    if not automatic:
        require_system_manager()
        doc.check_permission('write')
    definition = provider_definition(doc.provider)
    if provider and provider != doc.provider:
        return
    if not managed(definition):
        frappe.throw(_('This provider does not synchronize external revisions.'))
    if adopt and stored(doc, 'publication_request').get('pending'):
        frappe.throw(_('A template publication is already pending confirmation.'))
    own = context(definition, doc.channel, doc.account)
    binding = context(definition, channel, account) if automatic else own
    _selected(definition, binding, permission=not automatic)
    scope = scope_key(definition, binding)
    if scope_key(definition, own) != scope:
        return
    with scope_lock(definition, scope):
        lock_document_types(DOCTYPE)
        _selected(definition, binding, permission=not automatic)
        if scope_key(definition, binding) != scope or doc.external_scope and doc.external_scope != scope:
            frappe.throw(_('The external template scope changed. Reload and try again.'))
        current = frappe.get_doc(DOCTYPE, name, for_update=True)
        if adopt and str(current.modified) != modified:
            frappe.throw(_('Configuration changed. Reload the template before replacing its draft.'), frappe.TimestampMismatchError)
        if adopt and stored(current, 'publication_request').get('pending'):
            frappe.throw(_('A template publication is already pending confirmation.'))
        _refresh(definition, binding, scope, current.external_key, name, adopt=adopt)


@frappe.whitelist(methods=['POST'])
def adopt_external_template(name, modified):
    refresh_template(name, adopt=True, modified=modified)


@frappe.whitelist(methods=['POST'])
def publish_template(name, modified, options=None):
    from .communication_template import validate_template

    require_system_manager()
    doc = frappe.get_doc(DOCTYPE, name)
    doc.check_permission('write')
    definition = provider_definition(doc.provider)
    if 'publish_template' not in definition['operations']:
        frappe.throw(_('This provider does not publish templates.'))
    binding = context(definition, doc.channel, doc.account)
    _selected(definition, binding)
    scope = scope_key(definition, binding)
    options = frappe.parse_json(options) if isinstance(options, str) else options
    options = {} if options is None else options
    if not isinstance(options, dict) or set(options) - ({field[0] for field in definition.get('template_publication_fields', ())} | {'confirm_conflict'}):
        frappe.throw(_('Invalid template publication options.'))
    with scope_lock(definition, scope):
        lock_document_types(DOCTYPE)
        doc = frappe.get_doc(DOCTYPE, name, for_update=True)
        doc.check_permission('write')
        if str(doc.modified) != modified:
            frappe.throw(_('Configuration changed. Reload the template before publishing.'), frappe.TimestampMismatchError)
        _selected(definition, binding)
        if scope_key(definition, binding) != scope or doc.external_scope and doc.external_scope != scope:
            frappe.throw(_('The external template scope changed. Reload and try again.'))
        validate_template(doc, definition=definition, operational=True)
        described = describe(doc, definition)
        siblings = list(_scoped_documents(definition, scope, key=described['key']))
        if any(stored(sibling, 'publication_request').get('pending') for sibling in siblings):
            frappe.throw(_('A template publication is already pending confirmation.'))
        try:
            remote = _read_one(definition, doc, doc.account)
        except Exception:
            _fail(doc, 'external_read_failed')
            return dict(state='Error', diagnostic='external_read_failed')
        previous = stored(doc, 'published_snapshot')
        if remote and (remote['issue'] or previous and remote['reference'] != previous['reference']):
            frappe.throw(_('Resolve the external template conflict before publishing.'))
        category = options.get('category')
        remote_category = remote.get('metadata', {}).get('category') if remote else None
        if remote and remote['revision'] == described['revision'] and (
            not category or not remote_category or category == remote_category
        ):
            if _allowed(definition, remote['status']):
                doc.publication_request = encode(dict(pending=True, draft=draft(doc),
                    revision=described['revision'], reference=remote['reference'], result='verified'))
                _apply(doc, definition, scope, remote)
                _refresh(definition, binding, scope, described['key'], name)
            else:
                _refresh(definition, binding, scope, described['key'], name)
            return dict(state=frappe.db.get_value(DOCTYPE, name, 'sync_state'))
        conflict = remote and (not previous or remote['revision'] != previous['remote_revision'])
        if conflict and options.get('confirm_conflict') not in (True, 1, '1'):
            frappe.throw(_('Synchronize and confirm replacement of the external content before publishing.'))
        request = operation(definition, 'prepare_template_publication', doc, dict(options, remote=remote))
        if not isinstance(request, dict) or len(encode(request).encode()) > 65536:
            frappe.throw(_('Invalid provider template publication request.'))
        reference = remote['reference'] if remote else None
        submission = dict(pending=True, draft=draft(doc), revision=described['revision'],
            local_revision=doc.revision, reference=reference, result='uncertain')
        if category:
            submission['category'] = category
        doc.external_scope = scope
        doc.publication_request = encode(submission)
        doc.sync_state, doc.last_sync_error = 'Publishing', None
        _persist(doc, ('external_scope', 'publication_request', 'sync_state', 'last_sync_error'))
        # This is the explicit side-effect boundary. A lost result retains the claim.
        frappe.db.commit()
        try:
            result = operation(definition, 'publish_template', binding, request)
        except Exception:
            result = {}
        lock_document_types(DOCTYPE)
        doc = frappe.get_doc(DOCTYPE, name, for_update=True)
        outcome = result.get('outcome') if isinstance(result, dict) else None
        if outcome == 'rejected':
            doc.publication_request = encode(dict(submission, pending=False, result='rejected'))
            doc.sync_state, doc.last_sync_error = 'Error', 'publication_rejected'
        elif outcome == 'submitted':
            remote_reference = result.get('reference')
            if text(remote_reference) and (not reference or reference == remote_reference):
                submission['reference'] = remote_reference
            doc.publication_request = encode(dict(submission, result='submitted'))
        else:
            doc.sync_state, doc.last_sync_error = 'Error', 'publication_result_unknown'
        _persist(doc, ('publication_request', 'sync_state', 'last_sync_error'))
        if outcome == 'submitted':
            _refresh(definition, binding, scope, described['key'], name)
        return dict(state=frappe.db.get_value(DOCTYPE, name, 'sync_state'),
            diagnostic=frappe.db.get_value(DOCTYPE, name, 'last_sync_error'))


def published_revision(doc):
    """Return the delivery identity independently of draft and observations."""
    from .communication_template import template_revision
    definition = provider_definition(doc.provider)
    return doc.published_revision if requires_published(definition) else template_revision(doc)


def _allowed(definition, status):
    return definition['template_statuses'].get(status, {}).get('can_send') is True


def published_block(doc, definition, account, *, expected_revision=None, live=False):
    """Authorize only a compatible snapshot and current evidence for the same revision."""
    validate_scope(doc, definition, account)
    snapshot = stored(doc, 'published_snapshot')
    if not doc.published_revision or not snapshot:
        return 'template_status_unknown'
    if expected_revision is not None and expected_revision != doc.published_revision:
        return 'template_unavailable'
    binding = _account_context(doc)
    if (snapshot['provider'] != definition['id'] or scope_key(definition, binding) != snapshot['scope']
        or doc.external_scope != snapshot['scope'] or snapshot['key'] != doc.external_key):
        return 'template_unavailable'
    if definition.get('template_languages'):
        code = snapshot['draft'].get('provider_language', doc.provider_language)
        if code != doc.provider_language or code not in definition['template_languages']:
            return 'template_unavailable'
    observed = stored(doc, 'externally_observed')
    if (observed.get('issue') and observed['issue'] != 'external_content_changed'
        or (observed.get('reference'), observed.get('revision')) != (snapshot['reference'], snapshot['remote_revision'])
        or not _allowed(definition, observed.get('status'))):
        return 'template_unavailable'
    if doc.last_sync_error and doc.last_sync_error not in ('publication_rejected', 'publication_result_unknown'):
        return 'template_status_unknown'
    if 'template_available' in definition['operations']:
        from messaging.registry.communication_providers import validate_template_availability
        decision = validate_template_availability(operation(definition, 'template_available', snapshot, account))
        if decision['available'] is not True:
            return 'template_unavailable' if decision['available'] is False else 'template_status_unknown'
    if live and 'verify_template_revision' in definition['operations']:
        status = operation(definition, 'verify_template_revision', binding, snapshot)
        if status is False or not isinstance(status, str) or not _allowed(definition, status):
            return 'template_unavailable'
    return None


def published_contract(reference, account, expected_revision=None, *, available=True, document=None):
    doc = document or frappe.get_doc(DOCTYPE, reference, for_update=True)
    definition = provider_definition(doc.provider)
    validate_scope(doc, definition, account)
    if available and (doc.status != 'Active' or published_block(doc, definition, account, expected_revision=expected_revision)):
        frappe.throw(_('The published template revision is unavailable.'))
    if available and definition.get('template_languages'):
        require_language(doc.language)
    snapshot = stored(doc, 'published_snapshot')
    if not snapshot:
        return None
    return dict(reference=reference, account=account, language=doc.language, revision=doc.published_revision,
        variables=snapshot['draft']['variables'],
        external={key: snapshot[key] for key in ('provider', 'scope', 'key', 'reference', 'remote_revision', 'delivery')})


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def notification_template_query(doctype, txt, searchfield, start, page_len, filters):
    """Limit native Link results to the rule's delivery binding."""
    account = filters.get('account') if filters else None
    channel = frappe.db.get_value('Communication Channel Account', account, 'channel') if account else None
    if doctype != DOCTYPE or not channel:
        return []
    definition = provider_definition(frappe.db.get_value('Communication Channel', channel, 'provider'))
    restrictions = dict(channel=channel, status='Active')
    if definition.get('template_scope') == 'Account':
        restrictions['account'] = account
    if managed(definition):
        restrictions['published_revision'] = ['is', 'set']
    return frappe.get_list(DOCTYPE, filters=restrictions,
        or_filters={'name': ['like', f'%{txt}%'], 'template_name': ['like', f'%{txt}%']},
        fields=['name', 'template_name', 'language'], order_by='template_name, name',
        limit_start=start, limit_page_length=page_len, as_list=True)
