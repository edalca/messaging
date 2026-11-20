"""Bot Flow materialization and applied versions for persisted presets."""
from copy import deepcopy
import json

import frappe
from frappe import _

from .bot_flow import apply_default, existing_default, _preset_write
from .configuration import _available_languages
from .contract import default_contract
from .localization import TEXT_FIELDS, INPUT_FIELDS


def snapshot(flow):
    """Track actual administrative values, including linked template content, per language."""
    common = {key: flow.get(key) for key in ('workflow_name', 'process_definition', 'priority',
        'show_in_menu', 'menu_order', 'use_custom_internal_error_message')}
    common['parameters'] = [{key: row.get(key) for key in ('parameter', 'process_argument', 'required',
        'allow_multiple_values', 'capture_order', 'minimum_values', 'maximum_values')} for row in flow.parameters]
    common['outputs'] = [{key: row.get(key) for key in ('status_code', 'output_mode', 'flow_action', 'retry_parameter')}
        for row in flow.outputs]
    languages = {}
    for table, fields in (('translations', TEXT_FIELDS), ('input_translations', INPUT_FIELDS),
            ('output_translations', ('status_code', 'template_reference'))):
        for row in flow.get(table) or []:
            item = {key: row.get(key) for key in fields}
            if table == 'output_translations' and row.template_reference:
                template = frappe.get_doc('Bot Flow Template', row.template_reference)
                item['template'] = {key: template.get(key) for key in ('template_name', 'language',
                    'header', 'body', 'footer', 'enabled')}
            languages.setdefault(row.language, {}).setdefault(table, []).append(item)
    for index, row in enumerate(flow.parameters):
        if not row.parameter:
            continue
        parameter = frappe.get_doc('Bot Parameter', row.parameter)
        common['parameters'][index]['definition'] = {key: parameter.get(key) for key in (
            'parameter_type', 'label', 'description', 'enabled', 'expression', 'string_mode', 'min_length',
            'max_length', 'min_value', 'max_value', 'allow_contextual_name_extraction', 'allow_ai_extraction')}
        for text in parameter.request_phrases or []:
            languages.setdefault(text.language, {}).setdefault('parameters', {})[row.process_argument or row.parameter] = text.request_phrase
    # JSON projection normalizes Frappe values for durable comparisons.
    return json.loads(json.dumps(dict(common=common, languages=languages), default=str))


def installed_flow_languages(flow):
    """Languages that have a row on the Bot Flow, not preset hashes or active languages."""
    if not flow:
        return []
    found = set()
    for table in ('translations', 'input_translations', 'output_translations'):
        for row in flow.get(table) or []:
            language = row.get('language') if row else None
            if language:
                found.add(language)
    return sorted(found)


def installation_state(preset, definition, active):
    available = _available_languages(definition, active)
    flow = existing_default(definition, app=preset.application)
    versions = json.loads(flow.preset_versions or '{}') if flow else {}
    current = snapshot(flow) if flow else {}
    common_changed = bool(flow and versions.get('common', {}).get('hash') != preset.configuration_hash)
    contributions = {r.language: r for r in preset.languages}
    changed = [language for language in available if flow and language in versions.get('languages', {})
        and versions['languages'][language]['hash'] != contributions[language].configuration_hash]
    pending = [language for language in available if language not in versions.get('languages', {})]
    unavailable = sorted(language for language in versions.get('languages', {}) if language not in
        {r.language for r in preset.languages if r.available and r.application in frappe.get_installed_apps()})
    manual = []
    if flow:
        flow.check_permission('read')
        if not versions or versions.get('common', {}).get('snapshot') != current['common']:
            manual.append('common')
        manual.extend(language for language, version in versions.get('languages', {}).items()
            if version.get('snapshot') != current['languages'].get(language))
    return dict(existing=flow.name if flow else None, available=available,
        installed_languages=installed_flow_languages(flow), pending_languages=pending,
        changed_languages=changed, unavailable_languages=unavailable, common_changed=common_changed,
        manual_changes=manual, requires_reset=bool(flow and (common_changed or changed or manual)))


def apply_preset(preset, definition, active, *, reset=False):
    """Create or explicitly restore one Flow, preserving its identity and unrelated languages."""
    state = installation_state(preset, definition, active)
    if set(state['available']) != set(active):
        frappe.throw(_('Every enabled language requires complete compatible preset content.'))
    discovered = default_contract(definition, preset.application)
    flow = existing_default(definition, app=preset.application, lock=True)
    if state['requires_reset'] and not reset:
        frappe.throw(_('Confirm restoration before replacing changed or manually edited configuration.'))
    versions = json.loads(flow.preset_versions or '{}') if flow else {}
    replace_common = bool(flow and reset and (state['common_changed'] or 'common' in state['manual_changes']))
    reset_languages = set(state['changed_languages']) | (set(state['manual_changes']) - {'common'})
    if replace_common:
        reset_languages.update(active)
    if not reset:
        reset_languages.clear()
    reset_languages &= set(active)
    prepared = deepcopy(definition)
    rebound_parameters = False
    if flow and reset:
        from messaging.chatbot.doctype.bot_parameter.bot_parameter import restore_preset_parameter
        for argument, specification in prepared.get('inputs', {}).items():
            planned = specification['parameter']
            if not isinstance(planned, dict):
                continue
            parameter, changed = restore_preset_parameter(planned, active)
            if parameter.name != planned['code']:
                planned['code'] = parameter.name
                for binding in flow.parameters:
                    if binding.process_argument == argument:
                        old_parameter = binding.parameter
                        binding.parameter = parameter.name
                        for output in flow.outputs:
                            if output.retry_parameter == old_parameter:
                                output.retry_parameter = parameter.name
                rebound_parameters = True
    if flow and reset:
        for key in ('translations', 'input_translations', 'output_translations'):
            flow.set(key, [row for row in flow.get(key) or [] if row.language not in reset_languages])
        # New version identities preserve historical templates and every other consumer.
        for status, output in prepared['outcomes'].items():
            if output['mode'] != 'template':
                continue
            template = prepared['templates'][status]
            template['reuse_existing'] = True
            for language in reset_languages:
                content = template['languages'][language]
                from hashlib import sha256
                suffix = sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:12]
                name = template.get('localized_names', {}).get(language, template['template_name'])
                template.setdefault('localized_names', {})[language] = name + ' ' + suffix
        if replace_common:
            flow.workflow_name = prepared['name']
            flow.update(prepared.get('options', {}))
            flow.set('parameters', [])
            flow.set('outputs', [])
    status, name, warnings = apply_default(prepared, flow, active, discovered, app=preset.application,
        replace_common=replace_common)
    flow = frappe.get_doc('Bot Flow', name)
    flow.check_permission('write')
    actual = snapshot(flow)
    if not versions or replace_common or rebound_parameters:
        versions['common'] = dict(hash=preset.configuration_hash, snapshot=actual['common'])
    contributions = {r.language: r for r in preset.languages}
    language_versions = versions.setdefault('languages', {})
    for language in active:
        if language in language_versions and language not in reset_languages:
            continue
        row = contributions[language]
        language_versions[language] = dict(hash=row.configuration_hash, application=row.application,
            source_method=row.source_method, snapshot=actual['languages'].get(language))
    encoded = json.dumps(versions, sort_keys=True, ensure_ascii=False)
    if flow.preset != preset.name or flow.preset_versions != encoded:
        with _preset_write():
            flow.preset = preset.name
            flow.preset_versions = encoded
            flow.save()
    return ('completed' if reset and status == 'existing' else status), name, warnings
