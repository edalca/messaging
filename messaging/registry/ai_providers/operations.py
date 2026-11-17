import inspect
from dataclasses import asdict
import frappe
from frappe import _
from messaging.registry import _catalog
from messaging.security.permissions import installed_applications
from messaging.registrations.ai_providers import AIProvider, AIProviderParameter, AIServiceError

def register_ai_providers(*, app: str, providers: tuple[AIProvider, ...]):
    """Register the owner's typed definitions through the storage-only DocType writer."""
    frame = inspect.currentframe()
    try:
        return _register_ai_providers(app=app, definitions=providers, caller=frame.f_back)
    finally:
        del frame


def _register_ai_providers(*, app, definitions, caller):
    from messaging.security.permissions import require_registration_owner
    from messaging.chatbot.doctype.ai_provider.ai_provider import _sync_ai_providers
    require_registration_owner(app, caller)
    records = _declared(app, definitions)
    for definition in definitions:
        if definition != internal_provider(definition.provider_id, app):
            frappe.throw(_('AI Provider operations must match the installed application hook.'))
    from messaging.common.locking import document_savepoint
    summary = dict(created=0, updated=0, unchanged=0)
    with document_savepoint():
        for key, record in records.items():
            try:
                result = _sync_ai_providers(app=app, definitions={key: record})
            except (frappe.ValidationError, frappe.PermissionError):
                if not _catalog.reject_declaration('ai_providers', key):
                    raise
                continue
            for action, count in result.items():
                summary[action] += count
    return summary


def _declared(app, definitions):
    if not isinstance(definitions, (list, tuple)):
        frappe.throw(_('Invalid AI Provider declaration.'))
    normalized = {}
    from messaging.security.permissions import require_provider_function
    for raw in definitions:
        if type(raw) is not AIProvider or raw.app != app or any(type(p) is not AIProviderParameter for p in raw.parameters):
            frappe.throw(_('Invalid AI Provider declaration.'))
        raw.__post_init__()
        for capability in (raw.decision, raw.parameter_extraction):
            if capability is not None:
                require_provider_function(capability.execute, app, None, None)
        require_provider_function(raw.validate_configuration, app, None)
        raw = {key: getattr(raw, key) for key in ('provider_id', 'provider_name', 'description', 'enabled', 'schema_version', 'parameters', 'decision', 'parameter_extraction')}
        raw['parameters'] = [asdict(p) for p in raw['parameters']]
        raw['decision'] = int(raw['decision'] is not None)
        raw['parameter_extraction'] = int(raw['parameter_extraction'] is not None)
        provider_id = raw['provider_id']
        if provider_id in normalized:
            frappe.throw(_('Invalid AI Provider declaration.'))
        normalized[provider_id] = dict(raw, application=app, provider_type='Internal')
    return normalized


def internal_provider(provider_id, app=None):
    """Resolve operations from verified installed declarations, never from document paths."""
    matches = [definition for owner in ([app] if app in installed_applications() else [] if app else installed_applications())
        for definition in _catalog.application_declarations(owner, kinds=('ai_providers',), tolerate_errors=True)['ai_providers']
        if type(definition) is AIProvider and definition.app == owner
        and definition.provider_id == provider_id and (app is None or owner == app)]
    if len(matches) != 1:
        raise AIServiceError('configuration_incomplete')
    return matches[0]


def retire_providers(app, identifiers):
    """Invalidate profiles of missing or invalid declarations, preserving providers."""
    from messaging.security.permissions import require_system_manager
    require_system_manager()
    from messaging.chatbot.doctype.ai_configuration.ai_configuration import invalidate_provider_configurations
    unavailable = []
    for name in frappe.get_all('AI Provider', filters={'application': app, 'provider_type': 'Internal'}, pluck='name'):
        if name not in identifiers:
            unavailable.append(name)
            invalidate_provider_configurations(name)
    return unavailable


def validate_registration_schema(app, definitions):
    return _declared(app, definitions)
