"""Independent interpreter configurations, encrypted values and validated operational state."""
import json
import traceback
from uuid import uuid4

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils.password import remove_encrypted_password

from messaging.common.configuration import validate_value_owner, prepare_secret
from messaging.common.primitives import utcnow
from messaging.security.permissions import require_system_manager
from messaging.chatbot.doctype.ai_provider.ai_provider import validate_scalar, DEFINITION_FIELDS
from messaging.chatbot.doctype.ai_configuration.execution import (
    AIServiceError, DIAGNOSTICS, configuration_values, diagnose, error_code, fingerprint,
    provider_for, run, validate_configuration,
)
from messaging.registrations.ai_providers import InvalidInterpretation, ProviderTimeout, ProviderUnavailable, DecisionRequest, Message, validate_response

STATE_FIELDS = ('validation_status', 'last_checked', 'diagnostic', 'validated_hash', 'attempted_hash',
    'evaluation_generation', 'evaluation_basis')


class AIConfiguration(Document):
    def onload(self):
        try:
            resolve_configuration(self.name)
            code = 'operational'
        except Exception as exc:
            code = error_code(exc)
        self.set_onload('ai_operational_status', _(DIAGNOSTICS[code]))
        from messaging.chatbot.doctype.ai_evaluation_run.runtime import approval
        self.set_onload('functional_evaluation', approval(self))

    def before_validate(self):
        require_system_manager()
        old = self.get_doc_before_save()
        for key in STATE_FIELDS:
            self.set(key, old.get(key) if old else None)

    def validate(self):
        if self.profile_type not in ('Decision', 'Parameter Extraction'):
            frappe.throw(_('Select a valid AI profile purpose.'))
        if self.profile_type != 'Decision' and frappe.db.get_single_value('Bot Settings', 'active_ai_configuration') == self.name:
            frappe.throw(_('The active Decision AI profile cannot be changed to Parameter Extraction.'))
        provider = provider_for(self, lock=True)
        if not provider.get('decision' if self.profile_type == 'Decision' else 'parameter_extraction'):
            frappe.throw(_('The AI profile purpose must match the provider functional type.'))
        if self.enabled and not provider.enabled:
            frappe.throw(_('Select an enabled AI provider.'))
        old = self.get_doc_before_save()
        if old and (old.provider != self.provider or old.configuration_name != self.configuration_name):
            frappe.throw(_('AI Configuration identity and provider are immutable.'))
        old_rows = {row.name: row for row in old.parameters} if old else {}
        definitions = {row.parameter_key: row for row in provider.parameters}
        for row in self.parameters:
            stored = validate_value_owner(row, self)
            definition = definitions.get(row.parameter_key)
            if not definition:
                frappe.throw(_('Invalid or duplicate provider parameter.'))
            if definition.field_type == 'Password':
                prepare_secret(row, old_rows, stored)
            elif row.secret_value:
                frappe.throw(_('Secrets must use encrypted Password storage.'))
        present = {r.parameter_key for r in self.parameters}
        for definition in provider.parameters:
            if definition.parameter_key not in present and definition.default_value not in (None, ''):
                self.append('parameters', dict(parameter_key=definition.parameter_key,
                    value=json.dumps(validate_scalar(definition.as_dict(), definition.default_value))))
        values = configuration_values(self, provider)
        for row in self.parameters:
            if definitions[row.parameter_key].field_type != 'Password':
                row.value = json.dumps(values[row.parameter_key], ensure_ascii=False, allow_nan=False)
        current_hash = fingerprint(provider, values)
        basis = fingerprint(provider, values, behavior_only=True)
        if not old or basis != old.evaluation_basis or self.enabled != old.enabled:
            self.evaluation_generation = uuid4().hex
        self.evaluation_basis = basis
        if current_hash != self.attempted_hash:
            self.validation_status = 'not_validated'
            self.diagnostic = _(DIAGNOSTICS['not_validated'])
            self.validated_hash = None
        self.flags.ai_probe_needed = current_hash != self.attempted_hash

    def on_update(self):
        old = self.get_doc_before_save()
        keep = {r.name for r in self.parameters}
        for row in old.parameters if old else []:
            if row.name not in keep:
                remove_encrypted_password('AI Configuration Value', row.name, 'secret_value')
        if self.profile_type == 'Parameter Extraction':
            diagnose(self, 'extraction_inactive')
        elif self.flags.ai_probe_needed or (old and old.profile_type != self.profile_type):
            # Saving validates local structure only; provider consumption is explicit.
            provider = provider_for(self)
            try:
                values = configuration_values(self, provider, required=True)
                validate_configuration(provider, values)
                code = 'not_validated'
            except Exception as exc:
                code = error_code(exc)
            diagnose(self, code)
        from messaging.chatbot.doctype.ai_evaluation_run.dependencies import dependencies_changed
        dependencies_changed(self)

    def before_rename(self, *args, **kwargs):
        frappe.throw(_('AI Configuration identity and provider are immutable.'))

    def on_trash(self):
        require_system_manager()
        for row in self.parameters:
            remove_encrypted_password('AI Configuration Value', row.name, 'secret_value')


def check_connection(doc):
    if doc.profile_type != 'Decision':
        result = diagnose(doc, 'extraction_inactive')
        return dict(result, message=result['diagnostic'])
    provider = None
    current_hash = None
    try:
        provider = provider_for(doc, lock=True)
        values = configuration_values(doc, provider)
        current_hash = fingerprint(provider, values)
        configuration_values(doc, provider, required=True)
        validate_configuration(provider, values)
        # A synthetic classification exchange checks authentication and the same contract;
        # it never loads or invokes a business Flow.
        from messaging.chatbot.doctype.bot_settings.language import resolve_process_language
        from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope
        request = DecisionRequest(uuid4().hex, Message('Connection check.'), resolve_process_language(),
            greeting_available=True)
        with evaluation_scope(None, configuration=doc.name):
            validate_response(run(provider, values, request), request)
        code = 'operational'
    except Exception as exc:
        code = error_code(exc)
        if code == 'internal_error':
            # Omit exception text, source lines and locals: any of them may contain credentials.
            frames = [dict(file=frame.filename, line=frame.lineno, function=frame.name)
                for frame in traceback.extract_tb(exc.__traceback__)]
            frappe.get_doc(dict(doctype='Error Log', method='AI connection check failed',
                error=json.dumps(dict(code=code, exception_type=type(exc).__name__, frames=frames)),
                metadata='{}', reference_doctype=doc.doctype, reference_name=doc.name)).insert(ignore_permissions=True)
    result = diagnose(doc, code, current_hash, checked=True)
    if code == 'operational':
        result['message'] = _('Connection verified successfully. The AI provider is operational.')
    elif code == 'authentication_error':
        result['message'] = _('{0} rejected authentication. Check the configured API key.').format(provider.provider_name)
    else:
        result['message'] = result['diagnostic']
    return result


@frappe.whitelist(methods=['POST'])
def test_connection(configuration):
    require_system_manager()
    doc = frappe.get_doc('AI Configuration', configuration, for_update=True)
    doc.check_permission('write')
    return check_connection(doc)


def resolve_configuration(name, *, for_update=False, settings=None):
    if not name or not frappe.db.exists('AI Configuration', name):
        raise AIServiceError('no_active_configuration')
    doc = frappe.get_doc('AI Configuration', name, for_update=for_update)
    if doc.profile_type != 'Decision':
        raise AIServiceError('decision_required' if doc.profile_type != 'Parameter Extraction' else 'extraction_inactive')
    if not doc.enabled:
        raise AIServiceError('configuration_disabled')
    provider = provider_for(doc, lock=for_update)
    values = configuration_values(doc, provider, required=True)
    validate_configuration(provider, values)
    if doc.validation_status != 'operational':
        raise AIServiceError(doc.validation_status if doc.validation_status in DIAGNOSTICS else 'not_validated')
    if fingerprint(provider, values) != doc.validated_hash:
        raise AIServiceError('not_validated')
    from messaging.chatbot.doctype.ai_evaluation_run.runtime import approval
    state = approval(doc, settings=settings)
    if not state['approved']:
        raise AIServiceError(state['status'])
    return doc, provider, values


def availability(settings=None):
    settings = settings or frappe.get_single('Bot Settings')
    try:
        if not frappe.db.count('AI Provider'):
            raise AIServiceError('no_provider')
        if not settings.interpretation_enabled:
            raise AIServiceError('interpretation_disabled')
        doc, provider, values = resolve_configuration(settings.active_ai_configuration, settings=settings)
        return dict(status='operational', operational=True, diagnostic=_(DIAGNOSTICS['operational']),
            configuration=doc.name, provider=provider.provider_name, last_checked=doc.last_checked)
    except Exception as exc:
        code = error_code(exc)
        return dict(status=code, operational=False, diagnostic=_(DIAGNOSTICS[code]),
            configuration=settings.active_ai_configuration)


@frappe.whitelist()
def dashboard():
    require_system_manager()
    configurations = frappe.get_list('AI Configuration', fields=['name','provider','profile_type','enabled','validation_status','last_checked','diagnostic'], limit_page_length=0)
    for row in configurations:
        try:
            resolve_configuration(row.name)
            row.operational = True
        except Exception as exc:
            row.operational = False
            row.validation_status = error_code(exc)
            row.diagnostic = _(DIAGNOSTICS[row.validation_status])
    return dict(availability=availability(),
        providers=frappe.get_list('AI Provider', fields=['name','provider_name','provider_type','decision','parameter_extraction','enabled'], limit_page_length=0),
        configurations=configurations)


def on_doctype_update():
    """Initialize only the additive purpose field; preserve all existing connection state."""
    profile = frappe.qb.DocType('AI Configuration')
    (frappe.qb.update(profile).set(profile.profile_type, 'Decision')
        .where(profile.profile_type.isnull() | (profile.profile_type == ''))).run()


def invalidate_provider_configurations(provider):
    """Retain values and encrypted secrets while revoking operational approval."""
    from messaging.chatbot.doctype.ai_configuration.execution import invalidate_configuration
    from messaging.chatbot.doctype.ai_evaluation_run.dependencies import dependencies_changed
    for name in frappe.get_all('AI Configuration', filters={'provider': provider}, pluck='name'):
        doc = frappe.get_doc('AI Configuration', name, for_update=True)
        invalidate_configuration(doc)
        dependencies_changed(doc)


def provider_changed(provider, method=None):
    previous = provider.get_doc_before_save()
    if previous and fingerprint(previous, {}) != fingerprint(provider, {}):
        invalidate_provider_configurations(provider.name)
