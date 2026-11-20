"""Independent interpreter configurations, encrypted values and validated operational state."""
from dataclasses import asdict
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
    invalidate_configuration, profile_fingerprint, provider_for, require_provider_available, run, set_profile_status, validate_configuration,
)
from messaging.registry.ai_providers import InvalidInterpretation, ProviderTimeout, ProviderUnavailable, DecisionRequest, Message
from .policy import profile_policy, RESERVED_KEYS

STATE_FIELDS = ('validation_status', 'last_checked', 'diagnostic', 'validated_hash', 'attempted_hash',
    'evaluation_generation', 'evaluation_basis', 'evaluation_compatibility')


class AIConfiguration(Document):
    def db_set(self, fieldname, *args, **kwargs):
        if {'status', *STATE_FIELDS}.intersection([fieldname] if isinstance(fieldname, str) else fieldname):
            frappe.throw(_('Use the available actions to change Status.'), frappe.PermissionError)
        return super().db_set(fieldname, *args, **kwargs)

    def onload(self):
        from messaging.chatbot.doctype.ai_evaluation_run.runtime import approval
        provider = provider_for(self)
        self.set_onload('provider_available', provider.status == 'Enabled')
        self.set_onload('provider_type', provider.provider_type)
        self.set_onload('functional_evaluation', approval(self))

    def before_validate(self):
        require_system_manager()
        old = self.get_doc_before_save()
        status = old.status if old else 'Untested'
        if (self.status or status) != status:
            frappe.throw(_('Use the available actions to change Status.'), frappe.PermissionError)
        self.status = status
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
        old = self.get_doc_before_save()
        if not old and provider.status != 'Enabled':
            frappe.throw(_('Select an enabled AI provider.'))
        if old and (old.provider != self.provider or old.configuration_name != self.configuration_name):
            frappe.throw(_('AI Configuration identity and provider are immutable.'))
        policy = profile_policy(self, provider, validate_inactive=True)
        if policy is not None:
            self.update(asdict(policy))
        old_rows = {row.name: row for row in old.parameters} if old else {}
        definitions = {row.parameter_key: row for row in provider.parameters}
        for row in self.parameters:
            stored = validate_value_owner(row, self)
            definition = definitions.get(row.parameter_key)
            if not definition or row.parameter_key in RESERVED_KEYS:
                frappe.throw(_('Invalid or duplicate provider parameter.'))
            if definition.field_type == 'Password':
                prepare_secret(row, old_rows, stored)
            elif row.secret_value:
                frappe.throw(_('Secrets must use encrypted Password storage.'))
        present = {r.parameter_key for r in self.parameters}
        for definition in provider.parameters:
            if provider.provider_type == 'External' and definition.parameter_key == 'endpoint':
                continue
            if definition.parameter_key not in present and definition.default_value not in (None, ''):
                self.append('parameters', dict(parameter_key=definition.parameter_key,
                    value=json.dumps(validate_scalar(definition.as_dict(), definition.default_value))))
        values = configuration_values(self, provider)
        for row in self.parameters:
            if definitions[row.parameter_key].field_type != 'Password':
                row.value = json.dumps(values[row.parameter_key], ensure_ascii=False, allow_nan=False)
        basis = profile_fingerprint(self, provider, values)
        changed = not old or basis != old.evaluation_basis or old.profile_type != self.profile_type
        if changed:
            self.evaluation_generation = uuid4().hex
            self.evaluation_compatibility = None
            self.attempted_hash = None
            self.validated_hash = None
            if self.status in ('Enabled', 'Disabled', 'Unavailable'):
                self.status = 'Unavailable'
            code = 'evaluation_invalidated' if self.status == 'Unavailable' else 'not_validated'
            self.validation_status = code
            self.diagnostic = _(DIAGNOSTICS[code])
        self.evaluation_basis = basis
        self.flags.check_local_configuration = changed

    def on_update(self):
        old = self.get_doc_before_save()
        keep = {r.name for r in self.parameters}
        for row in old.parameters if old else []:
            if row.name not in keep:
                remove_encrypted_password('AI Configuration Value', row.name, 'secret_value')
        if self.profile_type == 'Parameter Extraction' and self.status == 'Untested':
            diagnose(self, 'extraction_inactive')
        elif self.flags.check_local_configuration and self.status == 'Untested':
            provider = provider_for(self)
            try:
                validate_configuration(provider, configuration_values(self, provider, required=True),
                    policy=profile_policy(self, provider))
            except AIServiceError as exc:
                diagnose(self, exc.code)
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
        current_hash = profile_fingerprint(doc, provider, values)
        configuration_values(doc, provider, required=True)
        policy = profile_policy(doc, provider)
        validate_configuration(provider, values, policy=policy)
        # A synthetic classification exchange checks authentication and the same contract;
        # it never loads or invokes a business Flow.
        from messaging.chatbot.doctype.bot_settings.language import resolve_process_language
        from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope
        request = DecisionRequest(uuid4().hex, Message('Connection check.'), resolve_process_language(),
            greeting_available=True)
        with evaluation_scope(None, configuration=doc.name):
            if policy is None:
                run(provider, values, request)
            else:
                run(provider, values, request, policy=policy)
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
    doc.check_permission('read')
    doc.check_permission('write')
    return check_connection(doc)


@frappe.whitelist(methods=['POST'])
def set_active(configuration, active, modified):
    require_system_manager()
    doc = frappe.get_doc('AI Configuration', configuration, for_update=True)
    doc.check_permission('read')
    doc.check_permission('write')
    if str(doc.modified) != modified:
        frappe.throw(_('The document changed. Reload it before changing Status.'), frappe.TimestampMismatchError)
    if type(active) not in (bool, int, str) or active not in (True, False, '0', '1'):
        frappe.throw(_('Invalid AI profile action.'))
    if active in (True, '1'):
        if doc.status != 'Disabled':
            frappe.throw(_('This action is unavailable for the current Status.'))
        try:
            _approved_configuration(doc)
        except AIServiceError as exc:
            return _activation_failed(doc, exc.code)
        set_profile_status(doc, 'Enabled')
    else:
        if doc.status != 'Enabled':
            frappe.throw(_('This action is unavailable for the current Status.'))
        set_profile_status(doc, 'Disabled')
    return dict(status=doc.status)


def _activation_failed(doc, code):
    invalidate_configuration(doc, code)
    return dict(status=doc.status, diagnostic=_(DIAGNOSTICS[code]))


def resolve_configuration(name, *, for_update=False, settings=None):
    if not name or not frappe.db.exists('AI Configuration', name):
        raise AIServiceError('no_active_configuration')
    doc = frappe.get_doc('AI Configuration', name, for_update=for_update)
    if doc.profile_type != 'Decision':
        raise AIServiceError('decision_required' if doc.profile_type != 'Parameter Extraction' else 'extraction_inactive')
    if doc.status != 'Enabled':
        code = {'Disabled': 'configuration_disabled', 'Untested': 'configuration_untested'}.get(doc.status)
        if not code:
            code = doc.validation_status if doc.validation_status in DIAGNOSTICS and doc.validation_status != 'operational' else 'configuration_unavailable'
        raise AIServiceError(code)
    return _approved_configuration(doc, settings=settings)


def _approved_configuration(doc, *, settings=None):
    provider = provider_for(doc)
    values = configuration_values(doc, provider, required=True)
    validate_configuration(provider, values, policy=profile_policy(doc, provider))
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
    configurations = frappe.get_list('AI Configuration', fields=['name','provider','profile_type','status','validation_status','last_checked','diagnostic'], limit_page_length=0)
    for row in configurations:
        try:
            resolve_configuration(row.name)
            row.operational = True
        except Exception as exc:
            row.operational = False
            row.validation_status = error_code(exc)
            row.diagnostic = _(DIAGNOSTICS[row.validation_status])
    return dict(availability=availability(),
        providers=frappe.get_list('AI Provider', fields=['name','provider_name','provider_type','decision','parameter_extraction','status'], limit_page_length=0),
        configurations=configurations)


def on_doctype_update():
    """Initialize only the additive purpose field; preserve all existing connection state."""
    profile = frappe.qb.DocType('AI Configuration')
    (frappe.qb.update(profile).set(profile.profile_type, 'Decision')
        .where(profile.profile_type.isnull() | (profile.profile_type == ''))).run()


def provider_changed(provider, method=None):
    from messaging.chatbot.doctype.ai_evaluation_run.dependencies import dependencies_changed
    from messaging.chatbot.doctype.ai_evaluation_run.runtime import approval

    previous = provider.get_doc_before_save()
    if not previous:
        return
    changed = fingerprint(previous, {}) != fingerprint(provider, {})
    if not changed and previous.status == provider.status:
        return
    for name in frappe.get_all('AI Configuration', filters={'provider': provider.name}, pluck='name', order_by='name'):
        doc = frappe.get_doc('AI Configuration', name, for_update=True)
        if changed:
            invalidate_configuration(doc)
        elif provider.status != 'Enabled' and doc.status in ('Enabled', 'Disabled'):
            invalidate_configuration(doc, 'provider_unavailable' if provider.provider_type == 'Internal' else 'provider_disabled')
        elif (provider.status == 'Enabled' and doc.status == 'Unavailable'
              and doc.validation_status in ('provider_unavailable', 'provider_disabled')):
            if approval(doc)['approved']:
                set_profile_status(doc, 'Disabled', reason='operational')
            else:
                invalidate_configuration(doc)
    dependencies_changed(provider)
