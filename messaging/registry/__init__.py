"""Discover and synchronize the three technical registries; DocTypes own persistence."""
from .processes import register_processes
from .ai_providers import register_ai_providers
from .communication_providers import register_communication_providers


def _installed_declarations(app, kind):
    """Read the installed owner's verified hook without materializing any category."""
    from messaging.registry._catalog import application_declarations
    return application_declarations(app, kinds=(kind,))[kind]


def load_declarations(register_file, kind):
    """Read conventional declarations relative to a verified owner module."""
    from messaging.registry import _catalog
    return _catalog.load_declarations(register_file, kind)


def register_registry(*, app: str, discover: bool = False, definitions=()):
    """Declare technical capabilities through the installed owner's verified hook."""
    import inspect
    from messaging.registry._catalog import register_application
    frame = inspect.currentframe()
    try:
        return register_application(app=app, discover=discover, definitions=definitions, caller=frame.f_back)
    finally:
        del frame


def synchronize_registry():
    """Synchronize functional capabilities before consumers resolve cross-application metadata."""
    import frappe
    from messaging.common.locking import document_savepoint
    from messaging.security.permissions import require_system_manager, installed_applications
    from messaging.registry._catalog import synchronize_application
    require_system_manager()
    if getattr(frappe.local, 'request', None) is not None:
        raise frappe.PermissionError('Registry synchronization requires a server lifecycle or Bench call.')
    result = {}
    with document_savepoint():
        for app in sorted(installed_applications()):
            result[app] = synchronize_application(app)
        from messaging.registry.ai_providers.operations import retire_uninstalled_providers
        retire_uninstalled_providers()
        from messaging.chatbot.doctype.ai_evaluation_run.dependencies import dependencies_changed
        dependencies_changed()
    return result
