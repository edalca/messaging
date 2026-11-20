"""Optional provider acknowledgements scheduled by the owners of read evidence."""
import frappe

from messaging.communication.doctype.communication_channel_account.binding import account_provider, resolve_binding
from messaging.communication.doctype.communication_provider.communication_provider import operation, provider_definition
from messaging.security.execution_context import automatic_job


def schedule_read_receipts(account, references):
	references = list(dict.fromkeys(reference for reference in references if reference))
	provider = account_provider(account) if references else None
	if not provider:
		return
	try:
		definition = provider_definition(provider)
	except frappe.ValidationError:
		return
	if 'mark_incoming_read' in definition['operations']:
		for offset in range(0, len(references), 20):
			frappe.enqueue(send_read_receipts, enqueue_after_commit=True, provider=provider,
				account=account, references=references[offset:offset + 20])


@automatic_job
def send_read_receipts(provider, account, references):
	definition, _binding = resolve_binding(provider, account)
	if 'mark_incoming_read' not in definition['operations']:
		return
	for reference in references:
		operation(definition, 'mark_incoming_read', dict(account=account, reference=reference))
