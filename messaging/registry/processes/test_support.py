"""Module-level process fixtures with a small, stable source closure."""
from messaging.registrations.processes import BotParams, BotRequest, BotResponse, NotifyNoResponse, NotifyParams, NotifyRequest, NotifyResponse, ProcessOutcomes


def bot_definition(*, params: BotParams, outcomes: ProcessOutcomes) -> None:
    params.register('contract_number', type='string', required=True)
    outcomes.register('success', fields={'balance': 'string'}, mode=['message'], action='complete_flow')


BOT_CALLS = []


def bot_handle(request: BotRequest) -> BotResponse:
    BOT_CALLS.append(request.params['contract_number'])
    return BotResponse('success', data={'balance': request.params['contract_number']}, message='Prepared')


def notify_definition(*, params: NotifyParams, outcomes: ProcessOutcomes) -> None:
    params.register('payment_id', type='string', required=True)
    outcomes.register('payment_received', fields={'recipient': 'string', 'amount': 'string'}, mode=['template'])


def notify_handle(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    payment_id = request.params['payment_id']
    if payment_id == 'ineligible':
        return NotifyNoResponse('payment_not_eligible')
    if payment_id == 'broken':
        raise RuntimeError('technical failure')
    return NotifyResponse('payment_received', data={'recipient': '50400000000', 'amount': payment_id})


def positional_definition(params, outcomes):
    outcomes.register('success', fields={}, mode=['message'], action='complete_flow')


def wrong_bot_handler(request: NotifyRequest) -> NotifyResponse | NotifyNoResponse:
    return NotifyNoResponse('payment_not_eligible')
