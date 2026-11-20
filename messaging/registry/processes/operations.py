import inspect
from .contracts import BotProcess, NotifyProcess

def validate_registration_schema(app, definitions):
    """Validate declarations and authorized function references before synchronization."""
    from messaging.registry.processes.discovery import function_reference
    for definition in definitions:
        if type(definition) not in (BotProcess, NotifyProcess):
            raise ValueError("Use a typed BotProcess or NotifyProcess declaration.")
        definition.__post_init__()
        function_reference(definition.definition, app, process_type=definition.type, role='definition')
        function_reference(definition.handler, app, process_type=definition.type, role='handler')


def _synchronize(*, app, definitions, caller):
    from messaging.registry.processes.discovery import _register_processes
    return _register_processes(app=app, processes=definitions, caller=caller)


def _retire(app, identifiers):
    from messaging.communication.doctype.process_definition.process_definition import retire_definitions
    return retire_definitions(app, identifiers)


def register_processes(*, app, processes):
    """Register the owner's complete catalogue without executing business callbacks."""
    frame = inspect.currentframe()
    try:
        return _synchronize(app=app, definitions=processes, caller=frame.f_back)
    finally:
        del frame


def decimal_rows(doctype, names, fields):
    """Read exact decimals through Process Definition's authorized document access."""
    from messaging.communication.doctype.process_definition.execution import decimal_rows as read
    return read(doctype, names, fields)


def is_process_test():
    from messaging.communication.doctype.process_definition.execution import is_process_test as active
    return active()
