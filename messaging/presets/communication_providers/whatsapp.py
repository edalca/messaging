"""Code-owned WhatsApp declaration."""
from messaging.registry.communication_providers import (
	CommunicationCapability as Capability,
	CommunicationOperations,
	CommunicationProvider,
	CommunicationProviderParameter as Parameter,
	TemplateScope,
)
from messaging.whatsapp.api import DEFAULT_VERSION, SUPPORTED_VERSIONS
from messaging.whatsapp.configuration import PROVIDER, VERIFY_TOKEN, valid_account
from messaging.whatsapp.languages import TEMPLATE_LANGUAGES
from messaging.whatsapp.provider import mark_incoming_read, notification_destination, send, validate_notification
from messaging.whatsapp.templates import (
    CATEGORIES, FORMAT_CODES, FORMATS, describe_template, list_templates, prepare_provider_template, prepare_template_publication,
    project_template, publish_template, query_template_status, template_scope_key,
    validate_template, verify_template_revision,
)
from messaging.whatsapp.version import __version__
from messaging.whatsapp.webhook import authenticate_ingress


def definition() -> CommunicationProvider:
	return CommunicationProvider(
		id=PROVIDER, name='WhatsApp', version=__version__, app='messaging',
		parameters=(
			Parameter('app_id', 'Meta App ID', 'Data', 'Channel'),
			Parameter('app_secret', 'Meta App Secret', 'Password', 'Channel', required=True),
			Parameter(VERIFY_TOKEN, 'Webhook Verify Token', 'Password', 'Channel', required=True),
			Parameter('graph_version', 'Graph API Version', 'Select', 'Channel', required=True,
				options='\n'.join(SUPPORTED_VERSIONS), default_value=DEFAULT_VERSION),
			Parameter('waba_id', 'Messaging Account ID', 'Data', 'Account', required=True),
			Parameter('access_token', 'Meta Access Token', 'Password', 'Account', required=True),
			Parameter('parameter_format', 'Provider Parameter Format', 'Select', 'Template', options='\n'.join(FORMAT_CODES)),
			Parameter('notification_phone_number', 'Test Destination Number', 'Data', 'Notification Rule', required=True,
				description='International E.164 phone number (for example, +50499999999). Valid format does not guarantee an active WhatsApp account or Meta test-number recipient authorization.'),
		),
		capabilities=(Capability.TEXT, Capability.INTERACTIVE, Capability.DELIVERY_STATUS, Capability.TEMPLATES,
			Capability.TEMPLATE_STATUS, Capability.PROVIDER_TEMPLATES, Capability.NOTIFICATION_TEMPLATE_REQUIRED,
			Capability.RECIPIENT_REDIRECT),
		template_scope=TemplateScope.ACCOUNT,
		template_languages=TEMPLATE_LANGUAGES,
		template_parameter_formats=FORMATS,
		template_statuses={status: {'can_send': status == 'APPROVED'} for status in (
			'APPROVED', 'PENDING', 'IN_APPEAL', 'REJECTED', 'PAUSED', 'DISABLED',
			'PENDING_DELETION', 'DELETED', 'LIMIT_EXCEEDED', 'ARCHIVED',
		)},
		template_publication_fields=(
			('category', 'Meta Template Category', 'Select', CATEGORIES),
			('examples', 'Variable Examples (JSON)', 'Small Text', ()),
		),
		ingress_parameters=(('Channel', ('app_secret',)), ('Account', ('waba_id',))),
		operations=CommunicationOperations(resolve_account=valid_account, send=send,
			authenticate_ingress=authenticate_ingress, validate_notification=validate_notification,
			validate_template=validate_template, query_template_status=query_template_status,
			mark_incoming_read=mark_incoming_read, template_scope_key=template_scope_key,
			describe_template=describe_template, list_templates=list_templates, project_template=project_template,
			prepare_template_publication=prepare_template_publication, publish_template=publish_template,
			prepare_provider_template=prepare_provider_template,
			verify_template_revision=verify_template_revision,
			notification_destination=notification_destination),
	)
