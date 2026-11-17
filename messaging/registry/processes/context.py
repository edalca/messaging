"""Process declaration context shared by discovery and authorized execution."""
from dataclasses import dataclass, replace
import inspect
import frappe
from frappe import _
from messaging.registrations.processes import BotParams, NotifyParams, ProcessOutcomes, validate_definition_signature, _resolve_params, normalize_result


@dataclass(frozen=True, slots=True)
class _ProcessContext:
    process_id: str
    process_type: str
    language: str
    test: bool
    arguments: object
    event: str = ''
    occurrence: str = ''
    _source: object = None
    _execution: object = None
    _invoke: object = None
    _state: object = None
    params: object = None
    outcomes: object = None
    _configured_inputs: object = ()
    _request: object = None
    _finished: bool = False

    def __post_init__(self):
        if type(self.test) is not bool:
            raise frappe.ValidationError(_('Process test mode must be a boolean.'))
        object.__setattr__(self, '_state', dict(callback=None, responded=False))
        params_type = BotParams if self.process_type == 'bot_flow' else NotifyParams
        object.__setattr__(self, 'params', params_type(self))
        object.__setattr__(self, 'outcomes', ProcessOutcomes(self))

    def _bind_handler(self, function):
        """Runtime binds the authorized handler. Declaration cannot register one."""
        if self._state['callback'] is not None or not inspect.isfunction(function):
            raise frappe.ValidationError(_('Bind exactly one business handler.'))
        self._state['callback'] = function

    def declare(self, function):
        validate_definition_signature(function)
        result = function(params=self.params, outcomes=self.outcomes)
        if result is not None or not self.outcomes._definitions:
            raise frappe.ValidationError(_('A Process declaration needs outcomes and no return value.'))
        self.params._finalize()
        self.outcomes._finalize()
        object.__setattr__(self, '_finished', True)

    def response(self):
        if not self._finished:
            raise frappe.ValidationError(_('Incomplete Process declaration.'))
        if self._state['responded']:
            raise frappe.ValidationError(_('The Process handler has already been invoked.'))
        self._state['responded'] = True
        if self.test:
            return dict(version=1, status='test', recipient='', data={}, message='')
        if self._invoke is None or self._request is None or self._state['callback'] is None:
            raise frappe.PermissionError(_('Business execution requires the authorized Process Runtime.'))
        request = replace(self._request, params=_resolve_params(self))
        return normalize_result(self._invoke(self._state['callback'], request),
            outcomes=self.outcomes._definitions, definitions=self.params._definitions,
            process_type=self.process_type)

    def contract(self):
        from copy import deepcopy
        if not self._finished:
            raise frappe.ValidationError(_('Incomplete Process declaration.'))
        contract = dict(parameters=[{k: v for k, v in item.items() if k != 'origin'}
            for item in self.params._definitions if item['origin'] == 'declared'],
            outcomes=deepcopy(self.outcomes._definitions))
        if self.params._configurable:
            contract['configurable_parameters'] = True
        for status, fields in self.outcomes._explicit_fields.items():
            contract['outcomes'][status]['fields'] = dict(fields)
        return contract
