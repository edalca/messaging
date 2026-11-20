import inspect
import frappe
from frappe import _
from .contracts import CommunicationProvider, normalize_definitions

def validate_registration_schema(app, definitions):
    return normalize_definitions(app, definitions)


def _synchronize(*, app, definitions, caller):
    from messaging.security.permissions import require_registration_owner
    from messaging.registry import _installed_declarations
    from messaging.communication.doctype.communication_provider.communication_provider import _sync_provider_catalog
    require_registration_owner(app, caller)
    installed = normalize_definitions(app, _installed_declarations(app, 'communication_providers'))
    declared = normalize_definitions(app, definitions)
    if declared != installed:
        frappe.throw(_("Communication provider declarations must match the installed application hook."))
    from messaging.registry._catalog import tolerates_errors, reject_declaration
    result = _sync_provider_catalog(declared, tolerate_errors=tolerates_errors())
    for name in result['rejected']:
        reject_declaration('communication_providers', name)
    from messaging.communication.doctype.communication_provider_value.values import incompatible_configurations
    result['configurations'] = {name: incompatible_configurations(name) for name in declared if name not in result['rejected']}
    return result


def _retire(app, identifiers):
    from messaging.communication.doctype.communication_provider.communication_provider import retire_providers
    return retire_providers(app, identifiers)


def register_communication_providers(*, app: str, providers: tuple[CommunicationProvider, ...]):
    """Register installed owner declarations without provisioning channels."""
    frame = inspect.currentframe()
    try:
        return _synchronize(app=app, definitions=providers, caller=frame.f_back)
    finally:
        del frame
