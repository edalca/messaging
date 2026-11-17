"""Public immutable channel provider declarations and their registration schema."""
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
import inspect
import re
from typing import Any, Callable, Literal, Mapping

import frappe
from frappe import _
from messaging.common.configuration import normalize
from messaging.security.permissions import require_provider_function

REGISTRATION_SCHEMA_VERSION = "1"
SUPPORTED_REGISTRATION_SCHEMAS = frozenset({"1"})

from messaging.communication.doctype.communication_provider.parameters import TYPES, PARAMETER_FIELDS, validate_parameter_schema


class CommunicationCapability(StrEnum):
	TEXT = 'text'
	INTERACTIVE = 'interactive'
	TEMPLATES = 'templates'
	DELIVERY_STATUS = 'delivery_status'
	RECEIVE_FILES = 'receive_files'
	SEND_FILES = 'send_files'
	NOTIFICATION_TEMPLATE_REQUIRED = 'notification_template_required'
	RECIPIENT_REDIRECT = 'recipient_redirect'
	NOTIFICATION_REDIRECT_REQUIRED = 'notification_redirect_required'
	PROVIDER_TEMPLATES = 'provider_templates'
	TEMPLATE_STATUS = 'template_status'



@dataclass(frozen=True, slots=True)
class CommunicationProviderParameter:
	parameter_key: str
	label: str
	field_type: Literal['Data', 'Small Text', 'Password', 'Check', 'Int', 'Float', 'Select', 'Link']
	scope: Literal['Channel', 'Account', 'Template', 'Notification Rule']
	required: bool = False
	options: str = ''
	default_value: str = ''
	sort_order: int = 0
	description: str = ''

	def __post_init__(self):
		validate_parameter_schema([asdict(self)])


@dataclass(frozen=True, slots=True)
class CommunicationOperations:
	resolve_account: Callable[[Any], bool]
	send: Callable[[dict[str, Any]], dict[str, Any]]
	available: Callable[[], bool] | None = None
	validate_notification: Callable[[dict[str, Any]], bool] | None = None
	authenticate_ingress: Callable[[Mapping[str, Any], Any, dict[str, Any]], dict[str, Any]] | None = None
	prepare_account_id: Callable[[Any], str] | None = None
	notification_destination: Callable[[Any, Any, str | None], dict[str, Any]] | None = None
	validate_template: Callable[[Any, dict[str, Any] | None], bool] | None = None
	template_variable_limits: Callable[[Any], dict[str, int | None]] | None = None
	resolve_provider_template: Callable[[dict[str, Any]], dict[str, Any]] | None = None
	prepare_provider_template: Callable[[dict[str, Any]], dict[str, Any]] | None = None
	query_template_status: Callable[[Any, Any], dict[str, Any]] | None = None
	template_available: Callable[[Any, Any], dict[str, Any]] | None = None
	apply_template_status_update: Callable[[Any], dict[str, Any]] | None = None

	def __post_init__(self):
		if any(value is not None and not callable(value) for value in (getattr(self, f.name) for f in fields(self))):
			raise ValueError('Communication operations require function references.')
		if not callable(self.resolve_account) or not callable(self.send):
			raise ValueError('Communication operations require resolve_account and send.')


@dataclass(frozen=True, slots=True)
class CommunicationProvider:
	id: str
	name: str
	version: str
	app: str
	operations: CommunicationOperations
	capabilities: tuple[CommunicationCapability, ...] = (CommunicationCapability.TEXT,)
	parameters: tuple[CommunicationProviderParameter, ...] = ()
	single_channel: bool = False
	fixed_channel_id: str | None = None
	ingress_parameters: tuple[tuple[str, tuple[str, ...]], ...] = ()
	registration_schema_version: str = field(default=REGISTRATION_SCHEMA_VERSION, kw_only=True)

	def __post_init__(self):
		try:
			capabilities = tuple(CommunicationCapability(value) for value in self.capabilities)
		except (TypeError, ValueError):
			raise ValueError('Unknown communication capability') from None
		object.__setattr__(self, 'capabilities', capabilities)
		object.__setattr__(self, 'parameters', tuple(self.parameters))
		object.__setattr__(self, 'ingress_parameters', tuple((scope, tuple(keys)) for scope, keys in self.ingress_parameters))
		_definition_values(self)


def _definition_values(raw):
	if type(raw.registration_schema_version) is not str or raw.registration_schema_version not in SUPPORTED_REGISTRATION_SCHEMAS:
		raise ValueError(f'Unsupported Communication Provider registration schema version: {raw.registration_schema_version!r}')
	if type(raw) is not CommunicationProvider or type(raw.operations) is not CommunicationOperations or any(type(p) is not CommunicationProviderParameter for p in raw.parameters):
		frappe.throw(_("Invalid channel provider registration."))
	raw.operations.__post_init__()
	row = {f.name: getattr(raw, f.name) for f in fields(raw) if f.name != 'registration_schema_version'}
	row['parameters'] = [asdict(p) for p in raw.parameters]
	row['capabilities'] = [capability.value for capability in raw.capabilities]
	row['operations'] = {field.name: getattr(raw.operations, field.name) for field in fields(raw.operations) if getattr(raw.operations, field.name) is not None}
	selection = raw.ingress_parameters
	if any(not isinstance(scope, str) for scope, keys in selection) or len({scope for scope, keys in selection}) != len(selection):
		frappe.throw(_("Invalid ingress authentication parameter declaration."))
	row['ingress_parameters'] = {scope: list(keys) for scope, keys in selection}
	if (not isinstance(row.get('app'), str) or not re.fullmatch(r'[a-z][a-z0-9_]*', row['app']) or any(not isinstance(row.get(k), str) or not row[k].strip() or len(row[k]) > 140 for k in ('id', 'name'))
		or not re.fullmatch(r'[A-Za-z0-9]{5,10}', row['id']) ):
		frappe.throw(_("Invalid channel provider registration."))
	caps, ops = row.get('capabilities'), row.get('operations')
	if (not isinstance(caps, (list, tuple)) or any(not isinstance(c, str) for c in caps) or not set(caps) <= CAPABILITIES
		or len(set(caps)) != len(caps) or 'text' not in caps or not isinstance(ops, dict) or not {'resolve_account', 'send'} <= set(ops) or not set(ops) <= OPERATIONS):
		frappe.throw(_("Invalid channel provider registration."))
	if (('provider_templates' in caps and not {'resolve_provider_template', 'prepare_provider_template'} <= set(ops))
		or (set(ops) & {'resolve_provider_template', 'prepare_provider_template'} and 'provider_templates' not in caps)
		or ('notification_template_required' in caps and not {'templates', 'provider_templates'} & set(caps))
		or ('templates' in caps and 'validate_template' not in ops)
		or ('template_variable_limits' in ops and 'templates' not in caps)
		or ('template_status' in caps and not {'query_template_status', 'template_available'} <= set(ops))
		or (set(ops) & {'query_template_status', 'template_available', 'apply_template_status_update'} and 'template_status' not in caps)
		or ('notification_redirect_required' in caps and 'recipient_redirect' not in caps)
		or ('recipient_redirect' in caps and 'notification_destination' not in ops)):
		frappe.throw(_("Invalid channel provider registration."))
	if not isinstance(row.get('version'), str) or not row['version'].strip() or len(row['version']) > 140:
		frappe.throw(_("Invalid channel provider registration."))
	row['parameters'] = validate_parameter_schema(row.get('parameters', []))
	if 'templates' not in caps and any(p['scope'] == 'Template' for p in row['parameters']):
		frappe.throw(_("The provider does not support templates."))
	if any(p['scope'] == 'Notification Rule' for p in row['parameters']) and 'notification_destination' not in ops:
		frappe.throw(_("Notification rule parameters require a provider destination policy."))
	if row['ingress_parameters']:
		selection = row['ingress_parameters']
		if not isinstance(selection, dict) or set(selection) - {'Channel', 'Account'} or 'authenticate_ingress' not in ops:
			frappe.throw(_("Invalid ingress authentication parameter declaration."))
		for scope, keys in selection.items():
			allowed = {p['parameter_key'] for p in row['parameters'] if p['scope'] == scope}
			if not isinstance(keys, (list, tuple)) or any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys) or not set(keys) <= allowed:
				frappe.throw(_("Invalid ingress authentication parameter declaration."))

	if type(row.get('single_channel', False)) is not bool:
		frappe.throw(_("Invalid channel provider registration."))
	row['single_channel'] = row.get('single_channel', False)
	fixed = row.get('fixed_channel_id')
	if fixed is not None and (not isinstance(fixed, str) or not re.fullmatch(r'[a-zA-Z0-9_-]+', fixed) or len(fixed) > 140):
		frappe.throw(_("Invalid fixed channel ID."))
	row['fixed_channel_id'] = fixed or None
	return row


_STATUS_OUTCOMES = frozenset(('reported', 'unknown', 'error'))
_SAFE_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')


def _scope_text(value, label):
	if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 140 or any(ord(char) < 32 or ord(char) == 127 for char in value):
		raise frappe.ValidationError(_('Invalid template status {0}.').format(label))
	return value


def validate_template_status_result(value):
	"""Normalize one provider status reading. Error and unknown are not disapprovals."""
	if not isinstance(value, dict):
		raise frappe.ValidationError(_('Invalid template status result.'))
	outcome = value.get('outcome')
	if outcome not in _STATUS_OUTCOMES:
		raise frappe.ValidationError(_('Invalid template status result.'))
	template = _scope_text(value.get('template'), 'template')
	scope_type = _scope_text(value.get('scope_type'), 'scope')
	scope_key = _scope_text(value.get('scope_key'), 'scope')
	reference = value.get('reference')
	if reference is not None:
		reference = _scope_text(reference, 'reference')
	template_revision = value.get('template_revision')
	if template_revision is not None:
		template_revision = _scope_text(template_revision, 'revision')
	error_code = value.get('error_code')
	if outcome == 'reported':
		provider_status = _scope_text(value.get('provider_status'), 'status')
		available = value.get('available')
		if type(available) is not bool:
			raise frappe.ValidationError(_('Invalid template status result.'))
		if error_code is not None:
			raise frappe.ValidationError(_('Invalid template status result.'))
	else:
		provider_status = None
		available = None
		if not isinstance(error_code, str) or not _SAFE_CODE.fullmatch(error_code):
			raise frappe.ValidationError(_('Invalid template status result.'))
	return dict(outcome=outcome, template=template, scope_type=scope_type, scope_key=scope_key,
		provider_status=provider_status, available=available, reference=reference,
		template_revision=template_revision, error_code=error_code)


def validate_template_availability(value):
	"""Provider interpretation of whether one account may send. None is undetermined, not a rejection."""
	if not isinstance(value, dict) or set(value) != {'available', 'authorization_required', 'reason'}:
		raise frappe.ValidationError(_('Invalid template availability result.'))
	available = value['available']
	if available is not None and type(available) is not bool:
		raise frappe.ValidationError(_('Invalid template availability result.'))
	if type(value['authorization_required']) is not bool or not isinstance(value['reason'], str) or not _SAFE_CODE.fullmatch(value['reason']):
		raise frappe.ValidationError(_('Invalid template availability result.'))
	return dict(available=available, authorization_required=value['authorization_required'], reason=value['reason'])


def normalize_definitions(app, providers):
	"""Validate owner declarations and derive routes without document construction."""
	if not isinstance(providers, (list, tuple)):
		frappe.throw(_("Invalid channel provider registration."))
	result = {}
	for raw in providers:
		if type(raw) is not CommunicationProvider:
			frappe.throw(_("Invalid channel provider registration."))
		raw.__post_init__()
		row = _definition_values(raw)
		if raw.app != app or raw.id in result:
			frappe.throw(_("Invalid channel provider registration."))
		row['operations'] = {op: require_provider_function(function, app, *OPERATION_ARGUMENTS[op])
			for op, function in row['operations'].items()}
		result[raw.id] = row
	return result


CAPABILITIES = frozenset(capability.value for capability in CommunicationCapability)
OPERATION_ARGUMENTS = {
	'resolve_account': (None,), 'send': (None,), 'available': (),
	'validate_notification': (None,), 'authenticate_ingress': (None, None, None),
	'prepare_account_id': (None,), 'notification_destination': (None, None, None),
	'validate_template': (None, None), 'template_variable_limits': (None,),
	'resolve_provider_template': (None,), 'prepare_provider_template': (None,),
	'query_template_status': (None, None), 'template_available': (None, None),
	'apply_template_status_update': (None,),
}
OPERATIONS = frozenset(('resolve_account', 'send', 'available', 'validate_notification', 'authenticate_ingress', 'prepare_account_id', 'notification_destination', 'validate_template', 'template_variable_limits', 'resolve_provider_template', 'prepare_provider_template', 'query_template_status', 'template_available', 'apply_template_status_update'))
