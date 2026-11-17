"""Discovery and registration operations for communication providers."""
from messaging.registrations import communication_providers as _contracts
from .operations import _retire, _synchronize, register_communication_providers, validate_registration_schema
from messaging.registry._catalog import register_declaration_type
register_declaration_type('communication_providers', _contracts.CommunicationProvider, validate=validate_registration_schema, synchronize=_synchronize, retire=_retire)
