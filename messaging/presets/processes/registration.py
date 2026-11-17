"""Default registration Process declaration and handler."""
from messaging.registrations.processes import BotParams, BotProcess, BotRequest, BotResponse, ProcessOutcomes

REGISTRATION_FLOW = 'messaging_registration'


def definition(*, params: BotParams, outcomes: ProcessOutcomes) -> None:
    params.register('name', type='string', required=True)
    params.register_configurable()
    outcomes.register('registered', fields={'name': 'string'}, configurable=True,
        mode=['template', 'message'], action='complete_flow')


def handle(request: BotRequest) -> BotResponse:
    # Capture, required inputs and profile persistence belong to the existing consumers.
    from decimal import Decimal
    # Schema v1 preserves exact decimal captures as text; Boolean stays a JSON boolean.
    return BotResponse('registered', data={'name': request.params['name'],
        **{key: format(value, 'f') if type(value) is Decimal else value
            for key, value in request.params.configurable.items()}})


process = BotProcess(
    id='messaging.registration',
    label='Default Registration',
    definition=definition,
    handler=handle,
)
