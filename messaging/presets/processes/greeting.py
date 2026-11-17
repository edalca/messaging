"""Default greeting Process declaration and handler."""
from messaging.registrations.processes import BotParams, BotProcess, BotRequest, BotResponse, ProcessOutcomes

GREETING_FLOW = 'messaging_greeting'


def definition(*, params: BotParams, outcomes: ProcessOutcomes) -> None:
    outcomes.register('greeted', fields={}, mode=['template', 'message'], action='complete_flow')


def handle(request: BotRequest) -> BotResponse:
    return BotResponse('greeted')


process = BotProcess(
    id='messaging.greeting',
    label='Default Greeting',
    definition=definition,
    handler=handle,
)
