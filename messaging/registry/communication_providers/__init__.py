"""Public communication providers contracts and registration operations."""
from .contracts import (
    REGISTRATION_SCHEMA_VERSION,
    SUPPORTED_REGISTRATION_SCHEMAS,
    CommunicationCapability,
    CommunicationProviderParameter,
    CommunicationOperations,
    CommunicationProvider,
    TemplateScope,
    TemplateLanguage,
    TemplateBinding,
    TemplateDescription,
    ExternalTemplateReading,
    TemplateDraft,
    TemplateProjection,
    PublishedTemplateSnapshot,
    TemplatePublicationResult,
    _definition_values,
    normalize_definitions,
    validate_template_status_result,
    validate_template_availability,
    CAPABILITIES,
    OPERATION_ARGUMENTS,
    OPERATIONS,
    TYPES,
    PARAMETER_FIELDS,
    validate_parameter_schema
)

from .operations import _retire, _synchronize, register_communication_providers, validate_registration_schema
from messaging.registry._catalog import register_declaration_type

register_declaration_type('communication_providers', CommunicationProvider, validate=validate_registration_schema,
    synchronize=_synchronize, retire=_retire)
