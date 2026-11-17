"""Discovery and registration operations for ai providers."""
from messaging.registrations import ai_providers as _contracts
from .operations import retire_providers, _declared, _register_ai_providers, internal_provider, register_ai_providers, validate_registration_schema
from messaging.registry._catalog import register_declaration_type
register_declaration_type('ai_providers', _contracts.AIProvider, validate=validate_registration_schema, synchronize=_register_ai_providers, retire=retire_providers)
