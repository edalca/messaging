"""Public Code-First Process declarations and typed execution contracts.

Process Definition owns document storage; its registry and execution modules own
discovery authorization and callback execution.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import inspect
import json
import keyword
import math
import re
from types import MappingProxyType
import types
from typing import Any, ClassVar, Protocol, Union, get_args, get_origin, get_type_hints

import frappe
from frappe import _
from messaging.common.primitives import _freeze
from messaging.security.execution_context import is_automation, require_automation_process

REGISTRATION_SCHEMA_VERSION = "1"
SUPPORTED_REGISTRATION_SCHEMAS = frozenset({"1"})


class ProcessSchemaValidationError(frappe.ValidationError):
	"""Raised when a Process response violates Process Schema v1."""


def _validate_version(version):
    if type(version) is not str or version not in SUPPORTED_REGISTRATION_SCHEMAS:
        raise ValueError(f"Unsupported Process registration schema version: {version!r}")


def validate_captured_process_argument(argument):
	if argument in ("language", "test", "context"):
		frappe.throw(_("Process Argument '{0}' is reserved for Messaging.").format(argument), frappe.ValidationError)


def _identifier(value):
    if (type(value) is not str or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value)
        or keyword.iskeyword(value) or value in ('test', 'internal_error')):
        raise frappe.ValidationError(_('Invalid Process contract identifier: {0}').format(value))


def _input_definitions(definitions):
    if type(definitions) is not list or len(definitions) > 100:
        raise frappe.ValidationError(_('Register the complete Process inputs exactly once.'))
    seen, result = set(), []
    for item in definitions:
        if (type(item) is not dict or set(item) not in ({'name', 'type', 'multiple', 'required'}, {'name', 'type', 'multiple', 'required', 'origin'})
            or item['type'] not in ('string', 'integer', 'number', 'boolean', 'date') or type(item['multiple']) is not bool
            or type(item['required']) is not bool
            or item.get('origin', 'declared') not in ('declared', 'configurable')):
            raise frappe.ValidationError(_('Invalid Process input declaration.'))
        name = item['name']
        _identifier(name)
        validate_captured_process_argument(name)
        if name in seen:
            raise frappe.ValidationError(_('Duplicate Process input: {0}').format(name))
        seen.add(name)
        result.append(dict(item))
    return result


def _input_value(kind, value):
    if kind == 'string' and type(value) is str:
        return value
    if kind == 'integer' and type(value) is int:
        return value
    if kind == 'number' and type(value) in (str, int, Decimal):
        # Decimal text is the exact intermediate representation, never a float.
        if type(value) is not str or re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', value):
            try:
                number = Decimal(value)
                if number.is_finite():
                    return number
            except InvalidOperation:
                pass
    if kind == 'boolean' and type(value) is bool:
        return value
    if kind == 'date' and type(value) is str:
        try:
            if date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    raise frappe.ValidationError(_('Process input value does not match its declared type.'))


FIELD_TYPES = ('string', 'integer', 'number', 'boolean', 'date', 'nullable_string', 'nullable_integer', 'nullable_number', 'nullable_boolean', 'nullable_date')


def _field_value(kind, value):
    if kind.startswith('nullable_'):
        return value is None or _field_value(kind.removeprefix('nullable_'), value)
    if kind == 'number':
        try:
            _input_value(kind, value)
            return True
        except frappe.ValidationError:
            return type(value) is float and math.isfinite(value)
    if kind == 'date':
        try:
            _input_value(kind, value)
            return True
        except frappe.ValidationError:
            return False
    return (kind == 'string' and type(value) is str
        or kind == 'integer' and type(value) is int
        or kind == 'boolean' and type(value) is bool)


def validate_output_data(fields, data):
    if any(name not in fields or not _field_value(fields[name], value) for name, value in data.items()):
        raise ProcessSchemaValidationError(_('Process result does not match the selected output fields.'))


def _annotation_members(annotation):
    if isinstance(annotation, types.UnionType) or get_origin(annotation) is Union:
        return set(get_args(annotation))
    return {annotation}


def validate_definition_signature(function):
    """Definition accepts only the declarative components and returns nothing."""
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        raise frappe.ValidationError(_('Invalid Process declaration signature.'))
    if (not inspect.isfunction(function) or {item.name for item in parameters} != {'params', 'outcomes'}
        or any(item.kind is not inspect.Parameter.KEYWORD_ONLY or item.default is not inspect.Parameter.empty for item in parameters)
        or inspect.isgeneratorfunction(function) or inspect.iscoroutinefunction(function)):
        raise frappe.ValidationError(_('Invalid Process declaration signature.'))


def validate_handler_signature(function, process_type):
    """Handler signatures are structural and annotated for the process type."""
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        raise frappe.ValidationError(_('Invalid Process handler signature.'))
    if (not inspect.isfunction(function) or len(parameters) != 1
        or parameters[0].kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
        or parameters[0].default is not inspect.Parameter.empty
        or inspect.isgeneratorfunction(function) or inspect.iscoroutinefunction(function)):
        raise frappe.ValidationError(_('Invalid Process handler signature.'))
    try:
        hints = get_type_hints(function)
    except Exception:
        hints = None
    expected_request = BotRequest if process_type == 'bot_flow' else NotifyRequest
    expected_return = {BotResponse} if process_type == 'bot_flow' else {NotifyResponse, NotifyNoResponse}
    if hints is None or hints.get(parameters[0].name) is not expected_request or _annotation_members(hints.get('return')) != expected_return:
        raise frappe.ValidationError(_('Invalid Process handler signature.'))


def configurable_output_fields(definitions):
    """Schema v1 is scalar; exact decimal outputs use text and optional inputs typed nulls."""
    definitions = [item for item in definitions if item.get('origin') == 'configurable']
    if any(item['multiple'] for item in definitions):
        raise frappe.ValidationError(_('Configurable outputs require scalar parameters.'))
    kinds = {'string': 'string', 'integer': 'integer', 'number': 'string', 'boolean': 'boolean', 'date': 'string'}
    return {item['name']: kinds[item['type']] if item['required'] else 'nullable_' + kinds[item['type']]
        for item in definitions}


def project_outcomes(outcomes, definitions, *, configurable):
    """One effective output schema for discovery projections and both runtime consumers."""
    result = {}
    for status, output in outcomes.items():
        enabled = output.get('configurable', False)
        if type(enabled) is not bool:
            raise frappe.ValidationError(_('Output configurable must be a boolean.'))
        fields = dict(output['fields'])
        if enabled:
            if not configurable:
                raise frappe.ValidationError(_('Configurable outputs require configurable Process parameters.'))
            additional = configurable_output_fields(definitions)
            if set(fields) & set(additional):
                raise frappe.ValidationError(_('Explicit and configurable output fields must have unique keys.'))
            fields.update(additional)
        if len(fields) > 100 or any(kind not in FIELD_TYPES for kind in fields.values()):
            raise frappe.ValidationError(_('Invalid Process output fields.'))
        result[status] = {**output, 'fields': fields}
    return result


SUPPORTED_PROCESS_SCHEMA_VERSION = 1
REQUIRED_PROCESS_SCHEMA_KEYS = frozenset({"version", "status", "recipient", "data", "message"})
STATUS_CODE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,63}$")


def validate_process_schema_v1(
	payload: Any,
	*,
	expected_data_fields: list[str] | set[str] | None = None,
) -> dict[str, Any]:
	"""Strictly validate a Process response against Process Schema v1."""
	if isinstance(payload, (str, bytes)):
		try:
			parsed = json.loads(payload)
		except Exception as exc:
			raise ProcessSchemaValidationError(
				_("Process response is not valid JSON: {0}").format(str(exc))
			) from exc
	elif isinstance(payload, dict):
		parsed = payload
	else:
		raise ProcessSchemaValidationError(
			_("Process response must be a JSON object (dict), got {0}.").format(type(payload).__name__)
		)

	if not isinstance(parsed, dict):
		raise ProcessSchemaValidationError(_("Process response root must be a JSON object."))

	if any(not isinstance(key, str) for key in parsed):
		raise ProcessSchemaValidationError(_("Process response root keys must be Strings."))
	present_keys = set(parsed.keys())
	missing_keys = REQUIRED_PROCESS_SCHEMA_KEYS - present_keys
	if missing_keys:
		raise ProcessSchemaValidationError(
			_("Process response is missing mandatory Process Schema v1 keys: {0}.").format(
				", ".join(sorted(missing_keys))
			)
		)

	extra_keys = present_keys - REQUIRED_PROCESS_SCHEMA_KEYS
	if extra_keys:
		raise ProcessSchemaValidationError(
			_("Process response contains unexpected top-level keys: {0}.").format(
				", ".join(sorted(extra_keys))
			)
		)

	version = parsed.get("version")
	if isinstance(version, bool) or not isinstance(version, int):
		raise ProcessSchemaValidationError(
			_("Process response 'version' must be an Integer (got {0}).").format(type(version).__name__)
		)
	if version != SUPPORTED_PROCESS_SCHEMA_VERSION:
		raise ProcessSchemaValidationError(
			_("Unsupported Process Schema version '{0}'. Expected version {1}.").format(
				version, SUPPORTED_PROCESS_SCHEMA_VERSION
			)
		)

	status = parsed.get("status")
	if not isinstance(status, str) or not status.strip():
		raise ProcessSchemaValidationError(
			_("Process response 'status' must be a non-empty String.")
		)
	if status != status.strip() or not STATUS_CODE_PATTERN.match(status):
		raise ProcessSchemaValidationError(
			_("Process response 'status' '{0}' is not a valid status identifier.").format(status)
		)
	if status.lower() == "internal_error":
		raise ProcessSchemaValidationError(
			_("'internal_error' is reserved for technical failures and cannot be returned as a functional Process status.")
		)

	data = parsed.get("data")
	if data is None or not isinstance(data, dict):
		raise ProcessSchemaValidationError(
			_("Process response 'data' must be a JSON Object (dict) and cannot be null.")
		)

	if any(not isinstance(key, str) or type(value) not in (str, int, float, bool, type(None))
		or (type(value) is float and not math.isfinite(value)) for key, value in data.items()):
		raise ProcessSchemaValidationError(_("Process response 'data' must contain only finite JSON scalars."))

	recipient = parsed.get("recipient")
	if not isinstance(recipient, str):
		raise ProcessSchemaValidationError(_("Process response 'recipient' must be a String."))

	message = parsed.get("message")
	if message is None or not isinstance(message, str):
		raise ProcessSchemaValidationError(
			_("Process response 'message' must be a String and cannot be null.")
		)

	if expected_data_fields:
		missing_data = [k for k in expected_data_fields if k not in data or data[k] is None]
		if missing_data:
			raise ProcessSchemaValidationError(
				_("Process response 'data' is missing expected fields: {0}.").format(
					", ".join(missing_data)
				)
			)

	return {
		"version": version,
		"recipient": recipient,
		"status": status,
		"data": data,
		"message": message,
	}


FLOW_ACTIONS = {'complete_flow': 'Complete Flow', 'cancel_flow': 'Cancel Flow', 'retry_parameter': 'Retry Parameter'}


PROCESS_CONTRACT_VERSION = 1


def _validate_process_identity(process_id, label, definition, handler, process_type):
    # The registration schema is internal. Callers cannot select it per instance.
    _validate_version(REGISTRATION_SCHEMA_VERSION)
    if (type(process_id) is not str or len(process_id) > 140
        or not re.fullmatch(r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+', process_id)):
        raise ValueError('Invalid Process ID.')
    if (type(label) is not str or not label.strip() or label != label.strip() or len(label) > 140):
        raise ValueError('Invalid Process label.')
    if not inspect.isfunction(definition) or not inspect.isfunction(handler):
        raise ValueError('Process definition and handler require function references.')
    validate_definition_signature(definition)
    validate_handler_signature(handler, process_type)


def _functional_data(status, data, message):
    normalized = {key: format(value, 'f') if type(value) is Decimal and value.is_finite() else value
        for key, value in data.items()} if type(data) is dict else data
    validate_process_schema_v1(dict(version=1, status=status, recipient='', data=normalized, message=message))
    return dict(normalized)


@dataclass(frozen=True, slots=True)
class BotResponse:
    """Functional Bot Flow result. Messaging supplies version, destination and presentation."""
    status: str
    data: dict = field(default_factory=dict)
    message: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'data', _functional_data(self.status, self.data, self.message))


@dataclass(frozen=True, slots=True)
class NotifyResponse:
    """Functional notification result. Messaging extracts the declared recipient."""
    status: str
    data: dict = field(default_factory=dict)
    message: str = ''

    def __post_init__(self):
        object.__setattr__(self, 'data', _functional_data(self.status, self.data, self.message))


@dataclass(frozen=True, slots=True)
class NotifyNoResponse:
    """Normal notification completion without a business outcome or transport payload."""
    reason: str = 'not_applicable'

    def __post_init__(self):
        _identifier(self.reason)


@dataclass(frozen=True, slots=True)
class ProcessReference:
    """Document identity only. Explicit reads return current data under native permissions."""
    doctype: str
    name: str

    def get_document(self):
        from messaging.communication.doctype.process_definition.execution import read_reference
        return read_reference(self.doctype, self.name)


@dataclass(frozen=True, slots=True)
class ProcessContactReference(ProcessReference):
    def profile_value(self, key):
        """Read one existing profile key through the authorized contact owner."""
        from messaging.communication.doctype.process_definition.execution import process_profile_value
        return process_profile_value(self.name, key)


@dataclass(frozen=True, slots=True)
class ProcessParameterDefinition:
    type: str
    required: bool
    multiple: bool
    origin: str


@dataclass(frozen=True, slots=True, init=False, eq=False)
class ProcessParameterValues(Mapping):
    """Validated values and execution definitions, excluded from business iteration."""
    _values: Mapping
    _definitions: Mapping

    def __init__(self, values, definitions=()):
        object.__setattr__(self, '_values', _freeze(values, preserve_decimal=True))
        object.__setattr__(self, '_definitions', MappingProxyType({item['name']:
            ProcessParameterDefinition(item['type'], item['required'], item['multiple'], item.get('origin', 'declared'))
            for item in definitions}))

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def definition(self, key):
        return self._definitions[key]

    def _group(self, origin):
        return MappingProxyType({key: value for key, value in self.items()
            if (self._definitions[key].origin if key in self._definitions else 'declared') == origin})

    @property
    def declared(self):
        return self._group('declared')

    @property
    def configurable(self):
        return self._group('configurable')


def _request_params(value):
    return value if isinstance(value, ProcessParameterValues) else ProcessParameterValues(value)


@dataclass(frozen=True, slots=True)
class BotRequest:
    """Operational request for a Bot Flow handler."""
    params: Mapping = field(default_factory=dict)
    contact: ProcessContactReference | None = None
    language: str | None = None
    channel: ProcessReference | None = None
    account: ProcessReference | None = None
    session: ProcessReference | None = None
    message: ProcessReference | None = None

    def __post_init__(self):
        object.__setattr__(self, 'params', _request_params(self.params))


@dataclass(frozen=True, slots=True)
class NotifyRequest:
    """Operational request for a Notification Rule handler."""
    params: Mapping = field(default_factory=dict)
    language: str | None = None
    source: ProcessReference | None = None
    event: str | None = None
    channel: ProcessReference | None = None
    account: ProcessReference | None = None

    def __post_init__(self):
        object.__setattr__(self, 'params', _request_params(self.params))


class _ProcessParams:
    """Shared declarative parameter builder. Execution values stay on the typed request."""
    def __init__(self, context):
        self._context, self._definitions, self._configurable = context, [], False

    def register_configurable(self):
        """Allow additional inputs defined by the owning consumer's configuration."""
        if self._context._finished or self._configurable:
            raise frappe.ValidationError(_('Process declaration is finalized or configurable inputs are already registered.'))
        self._configurable = True

    def register(self, name, *, type, required=True, multiple=False):
        if self._context._finished:
            raise frappe.ValidationError(_('Process declaration is finalized.'))
        self._definitions = _input_definitions([*self._definitions,
            dict(name=name, type=type, required=required, multiple=multiple)])

    def _finalize(self):
        declared = [{**item, 'origin': 'declared'} for item in self._definitions]
        configured = [{**dict(item), 'origin': 'configurable'} for item in self._context._configured_inputs]
        if configured and not self._configurable:
            raise frappe.ValidationError(_('The Process does not allow configurable inputs.'))
        self._definitions = _input_definitions([*declared, *configured])


class BotParams(_ProcessParams):
    """Declarative parameters of a Bot Flow."""


class NotifyParams(_ProcessParams):
    """Declarative parameters of a Notification Rule."""


class ProcessOutcomes:
    def __init__(self, context):
        self._context, self._definitions, self._explicit_fields = context, {}, {}

    def _finalize(self):
        self._explicit_fields = {status: dict(output['fields']) for status, output in self._definitions.items()}
        self._definitions = project_outcomes(self._definitions, self._context.params._definitions,
            configurable=self._context.params._configurable)

    def register(self, status, *, fields, mode, action=None, configurable=False):
        if type(configurable) is not bool:
            raise frappe.ValidationError(_('Output configurable must be a boolean.'))
        _identifier(status)
        if self._context._finished or status in self._definitions:
            raise frappe.ValidationError(_('Duplicate or finalized Process output: {0}').format(status))
        if type(fields) is not dict or len(fields) > 100:
            raise frappe.ValidationError(_('Invalid Process output fields.'))
        for name, kind in fields.items():
            _identifier(name)
            if kind not in FIELD_TYPES:
                raise frappe.ValidationError(_('Unsupported Process output field type.'))
        if (type(mode) is not list or not mode or any(type(v) is not str or v not in ('message', 'template') for v in mode)
            or len(set(mode)) != len(mode)):
            raise frappe.ValidationError(_('Declare one or two unique Message or Template modes in default order.'))
        output = dict(fields=dict(fields), mode=list(mode))
        if configurable:
            output['configurable'] = True
        if self._context.process_type == 'bot_flow':
            if type(action) is not str or action not in FLOW_ACTIONS:
                raise frappe.ValidationError(_('Unsupported declared Flow action.'))
            output['action'] = action
        elif 'recipient' in fields and fields['recipient'] != 'string':
            raise frappe.ValidationError(_('Notification recipient must be a string.'))
        self._definitions[status] = output


class BotDefinition(Protocol):
    def __call__(self, *, params: BotParams, outcomes: ProcessOutcomes) -> None: ...


class BotHandler(Protocol):
    def __call__(self, request: BotRequest) -> BotResponse: ...


class NotifyDefinition(Protocol):
    def __call__(self, *, params: NotifyParams, outcomes: ProcessOutcomes) -> None: ...


class NotifyHandler(Protocol):
    def __call__(self, request: NotifyRequest) -> NotifyResponse | NotifyNoResponse: ...


@dataclass(frozen=True, slots=True)
class BotProcess:
    """Bot Flow declaration. The functional type is bot_flow and is not a constructor argument."""
    id: str
    label: str
    definition: BotDefinition
    handler: BotHandler
    contract: ClassVar[int] = PROCESS_CONTRACT_VERSION

    def __post_init__(self):
        _validate_process_identity(self.id, self.label, self.definition, self.handler, self.type)

    @property
    def type(self) -> str:
        return 'bot_flow'


@dataclass(frozen=True, slots=True)
class NotifyProcess:
    """Notification Rule declaration. The functional type is notification and is not a constructor argument."""
    id: str
    label: str
    definition: NotifyDefinition
    handler: NotifyHandler
    contract: ClassVar[int] = PROCESS_CONTRACT_VERSION

    def __post_init__(self):
        _validate_process_identity(self.id, self.label, self.definition, self.handler, self.type)

    @property
    def type(self) -> str:
        return 'notification'


def _resolve_params(context):
    definitions, arguments = context.params._definitions, context.arguments
    if set(arguments) - {item['name'] for item in definitions}:
        raise frappe.ValidationError(_('Unknown Process input.'))
    values = {}
    for item in definitions:
        name = item['name']
        if name not in arguments:
            if item['required']:
                raise frappe.ValidationError(_('Required Process argument is missing.'))
            continue
        value = arguments[name]
        if item['multiple']:
            if not isinstance(value, (list, tuple)) or not value:
                raise frappe.ValidationError(_('A multiple Process input requires a non-empty list.'))
            values[name] = tuple(_input_value(item['type'], v) for v in value)
        else:
            values[name] = _input_value(item['type'], value)
    return ProcessParameterValues(values, definitions)




def normalize_result(value, *, outcomes, definitions, process_type):
    if process_type == 'bot_flow':
        if type(value) is not BotResponse:
            raise frappe.ValidationError(_('A Bot Flow handler must return a declared BotResponse.'))
    elif type(value) is NotifyNoResponse:
        value.__post_init__()
        return value
    elif type(value) is not NotifyResponse:
        raise frappe.ValidationError(_('A notification handler must return NotifyResponse or NotifyNoResponse.'))
    if value.status not in outcomes:
        raise ProcessSchemaValidationError(_('The handler must return a declared functional response.'))
    if len(json.dumps(dict(status=value.status, data=value.data, message=value.message), ensure_ascii=False, allow_nan=False).encode()) > 65536:
        raise frappe.ValidationError(_('Business result exceeds the technical size limit.'))
    data = dict(value.data)
    fields = outcomes[value.status]['fields']
    if outcomes[value.status].get('configurable'):
        # Preserve the existing null representation of omitted optional outputs,
        # without copying any input value or filling explicit/required fields.
        for item in definitions:
            if item['origin'] == 'configurable' and not item['required']:
                data.setdefault(item['name'], None)
    validate_output_data(fields, data)
    recipient = ''
    if process_type == 'notification':
        recipient = data.pop('recipient', '')
    return validate_process_schema_v1(dict(version=1, status=value.status, recipient=recipient, data=data, message=value.message))
