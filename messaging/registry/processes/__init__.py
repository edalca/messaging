"""Discovery and registration operations for processes."""
from messaging.registrations import processes as _contracts
from .operations import _retire, _synchronize, decimal_rows, is_process_test, register_processes, validate_registration_schema
from messaging.registry._catalog import register_declaration_type
register_declaration_type('processes', (_contracts.BotProcess, _contracts.NotifyProcess), validate=validate_registration_schema, synchronize=_synchronize, retire=_retire)
