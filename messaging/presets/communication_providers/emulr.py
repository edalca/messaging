"""Code-owned emulator declaration; operations remain with Emulator."""
from messaging.emulator.provider import (
    PROVIDER, CHANNEL, resolve_account, send, available, validate_notification,
    validate_template, authenticate_ingress, prepare_account_id, notification_destination,
)
from messaging.emulator.version import __version__
from messaging.registry.communication_providers import CommunicationCapability, CommunicationProvider, CommunicationProviderParameter, CommunicationOperations, TemplateScope


def definition() -> CommunicationProvider:
    return CommunicationProvider(
        id=PROVIDER, name='Channel Emulator', version=__version__, app='messaging',
        single_channel=True, fixed_channel_id=CHANNEL, template_scope=TemplateScope.CHANNEL,
        parameters=(
            CommunicationProviderParameter('notification_contact', 'Notification Emulated Contact', 'Link',
                'Notification Rule', required=True, options='Emulator Contact',
                description='Select an enabled fictitious contact for notifications from this rule.'),
            CommunicationProviderParameter('transport_outcome', 'Simulated Transport Outcome', 'Select',
                'Account', required=True, options='Accepted\nRejected\nUnknown', default_value='Accepted',
                description='Simulated outcome for new Bot Dispatch and Notification Queue handoffs on this account.'),
        ),
        capabilities=(CommunicationCapability.TEXT, CommunicationCapability.INTERACTIVE,
            CommunicationCapability.TEMPLATES, CommunicationCapability.DELIVERY_STATUS,
            CommunicationCapability.RECIPIENT_REDIRECT, CommunicationCapability.NOTIFICATION_REDIRECT_REQUIRED),
        operations=CommunicationOperations(resolve_account=resolve_account, send=send, available=available,
            validate_notification=validate_notification, validate_template=validate_template,
            authenticate_ingress=authenticate_ingress, prepare_account_id=prepare_account_id,
            notification_destination=notification_destination),
    )
