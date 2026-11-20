"""Default human availability Process declaration and handler."""
from messaging.registry.processes import BotParams, BotProcess, BotRequest, BotResponse, ProcessOutcomes

HUMAN_FLOW = 'messaging_human_availability'


def definition(*, params: BotParams, outcomes: ProcessOutcomes) -> None:
    outcomes.register('open', fields={}, mode=['template', 'message'], action='complete_flow')
    outcomes.register('closed', fields={}, mode=['template', 'message'], action='cancel_flow')
    outcomes.register('filtered', fields={}, mode=['template', 'message'], action='cancel_flow')


def handle(request: BotRequest) -> BotResponse:
    import frappe
    from messaging.communication.doctype.process_definition.execution import record_human_request
    settings = frappe.get_doc('Bot Settings', 'Bot Settings')
    if not settings.human_attention_enabled:
        return BotResponse('closed')
    if not settings.human_support_available():
        return BotResponse('closed')
    count = record_human_request(request)
    return BotResponse('open' if count >= settings.human_requests_required else 'filtered')


process = BotProcess(
    id='messaging.human_availability',
    label='Default Human Support',
    definition=definition,
    handler=handle,
)
