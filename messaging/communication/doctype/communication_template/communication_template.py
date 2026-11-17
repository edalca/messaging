"""General Messaging content templates with strict variables and revision tracking."""

import hashlib
import json
import re
import unicodedata

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from messaging.common.locking import lock_document_types
from messaging.common.write_guards import InternalWriteGuard
from .syntax import (
    TEMPLATE_VARIABLE_PATTERN, TEMPLATE_PLACEHOLDER_PATTERN, RAW_TOKEN_PATTERN,
    TEMPLATE_TOKEN_PATTERN, SIMPLE_VARIABLE_PATTERN, RESERVED_TEMPLATE_KEYS,
    validate_template_syntax, validate_template_variable_name, extract_component_variables,
)

COMPONENT_FIELDS = ("header", "body", "footer")
COMPONENT_LABELS = {"header": "Header", "body": "Body", "footer": "Footer"}
DEFAULT_COMPONENT_VARIABLE_LIMITS = {"header": 1, "body": None, "footer": 1}
_STATUS_TRANSITION_WRITE = InternalWriteGuard("messaging_template_status_transition")
_REGISTERED_TEMPLATE_DEFINITIONS = {}


def _snake_case(value):
	value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
	return re.sub(r'[^a-z0-9]+', '_', value).strip('_')


def template_identity(template_name, language):
	"""Normalize the descriptive name and real Language code into a stable identity."""
	from messaging.chatbot.doctype.bot_settings.language import validate_language
	language = validate_language(language)
	if not isinstance(template_name, str):
		frappe.throw(_("Template Name is required."))
	base = _snake_case(template_name)
	if not base:
		frappe.throw(_("Template Name must contain letters or digits after normalization."))
	name = base + '_' + _snake_case(language)
	if len(name) > 140:
		frappe.throw(_("Normalized template identity exceeds 140 characters."))
	return name


class CommunicationTemplate(Document):
	@property
	def provider(self):
		"""Resolve technical operations exclusively through the owning channel."""
		return frappe.db.get_value('Communication Channel', self.channel, 'provider') if self.channel else None

	def onload(self):
		supported = False
		if self.provider:
			from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
			try:
				supported = 'template_status' in provider_definition(self.provider).get('capabilities', [])
			except Exception:
				supported = False
		self.set_onload('template_status_supported', supported)

	def autoname(self):
		self.name = template_identity(self.template_name, self.language)

	def validate(self):
		if self.is_new():
			if not self.status:
				self.status = "Inactive"
			if not self.channel:
				frappe.throw(_("Select a Communication Channel."))
			identity = template_identity(self.template_name, self.language)
			# One stable native row serializes normalization checks, including older names.
			frappe.db.get_value('DocType', 'Communication Template', 'name', for_update=True)
			for other in frappe.db.sql('SELECT name, template_name FROM `tabCommunication Template` WHERE language=%s FOR UPDATE',
				(self.language,), as_dict=True):
				other_identity = _snake_case(other.template_name or '') + '_' + _snake_case(self.language)
				if other.name == identity or other_identity == identity:
					frappe.throw(_("Template identity {0} already belongs to {1}. Choose a different Template Name.").format(identity, other.name))
		else:
			previous = frappe.get_doc("Communication Template", self.name, for_update=True)
			if (previous.template_name or "") != (self.template_name or ""):
				frappe.throw(_("Template name is immutable after creation."))
			if (previous.channel or "") != (self.channel or ""):
				frappe.throw(_("Communication channel is immutable. Create a new template for another channel."))
			if (previous.language or "") != (self.language or ""):
				frappe.throw(_("Template language is immutable. Create a new template for another language."))
			if (previous.status or "") != (self.status or "") and not _STATUS_TRANSITION_WRITE.active():
				frappe.throw(_("Use Activate or Deactivate to change template status."))
		validate_template(self)
		from messaging.communication.doctype.communication_template.status import protect_status_rows, provider_status_summary
		protect_status_rows(self)
		self.provider_status = provider_status_summary(self)
		self.revision = template_revision(self)

	def on_update(self):
		from messaging.communication.doctype.communication_provider_value.communication_provider_value import cleanup_values
		cleanup_values(self)

	def on_trash(self):
		from messaging.communication.doctype.communication_provider_value.communication_provider_value import cleanup_values
		cleanup_values(self, deleting=True)

	def db_set(self, fieldname, value=None, **kwargs):
		if kwargs.get("commit"):
			frappe.throw(_("Communication Template updates cannot commit implicitly."))
		from messaging.communication.doctype.communication_template.status import template_status_write_active
		values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		if _STATUS_TRANSITION_WRITE.active() and set(values) <= {"status", "revision"}:
			return super().db_set(fieldname, value, **kwargs)
		if template_status_write_active() and set(values) <= {"provider_status"}:
			return super().db_set(fieldname, value, **kwargs)
		frappe.throw(_("Save Communication Template changes through its validated controller."))

	def db_update(self, *args, **kwargs):
		stored = frappe.db.get_value(
			"Communication Template",
			self.name,
			["template_name", "channel", "language", "status"],
			as_dict=True,
		)
		if stored:
			if (stored.template_name or "") != (self.template_name or ""):
				frappe.throw(_("Template name is immutable after creation."))
			if (stored.channel or "") != (self.channel or ""):
				frappe.throw(_("Communication channel is immutable. Create a new template for another channel."))
			if (stored.language or "") != (self.language or ""):
				frappe.throw(_("Template language is immutable. Create a new template for another language."))
			if (stored.status or "") != (self.status or "") and not _STATUS_TRANSITION_WRITE.active():
				frappe.throw(_("Use Activate or Deactivate to change template status."))
		return super().db_update(*args, **kwargs)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Template identity is immutable."))


def resolve_component_variable_limits(definition, doc):
	"""Merge optional provider variable count limits onto the universal defaults."""
	limits = dict(DEFAULT_COMPONENT_VARIABLE_LIMITS)
	if definition and "template_variable_limits" in definition.get("operations", {}):
		from messaging.communication.doctype.communication_provider.communication_provider import operation
		custom = operation(definition, "template_variable_limits", doc)
		if (
			not isinstance(custom, dict)
			or not set(custom) <= set(COMPONENT_FIELDS)
			or any(limit is not None and (type(limit) is not int or limit < 0) for limit in custom.values())
		):
			frappe.throw(_("Invalid provider template variable limits."))
		limits.update(custom)
	return limits


def validate_template_content(doc, *, definition=None, notification=None):
	"""Validate header, body, footer and declared variables for either template document."""
	if notification is None:
		notification = doc.doctype == 'Communication Template'
	declared = []
	declared_seen = set()
	for row in doc.get("template_variables") or []:
		row_name = row.get("name")
		if row_name:
			child_doctype = doc.meta.get_field("template_variables").options if doc.meta else "Communication Template Parameter"
			stored = frappe.db.get_value(
				child_doctype,
				row_name,
				["parent", "parenttype", "parentfield"],
				as_dict=True,
			)
			if stored and (stored.parent, stored.parenttype, stored.parentfield) != (
				doc.name,
				doc.doctype,
				"template_variables",
			):
				frappe.throw(_("Template parameter ownership is immutable."), frappe.PermissionError)
		param_name = row.get("parameter_name")
		if not isinstance(param_name, str) or not param_name:
			frappe.throw(_("Each template parameter row must define a parameter name."))
		validate_template_variable_name(param_name, notification=notification)
		if param_name in declared_seen:
			frappe.throw(_("Duplicate template parameter declaration: '{0}'.").format(param_name))
		declared_seen.add(param_name)
		declared.append(param_name)

	if len(declared) > 100:
		frappe.throw(_("Templates support at most 100 variables."))
	limits = resolve_component_variable_limits(definition, doc)
	used_occurrences = []
	used_seen = set()
	for field in COMPONENT_FIELDS:
		label = COMPONENT_LABELS[field]
		component_vars = extract_component_variables(doc.get(field), label, notification=notification)
		limit = limits[field]
		if limit is not None and len(component_vars) > limit:
			if limit == 0:
				frappe.throw(_("Template {0} cannot contain variables for this provider.").format(_(label)))
			frappe.throw(_("Template {0} allows at most {1} variable(s).").format(_(label), limit))
		for var_name in component_vars:
			if var_name in used_seen:
				frappe.throw(
					_(
						"Template variable '{0}' cannot be used more than once across Header, Body, and Footer."
					).format(var_name)
				)
			used_seen.add(var_name)
			used_occurrences.append(var_name)

	undeclared = [var_name for var_name in used_occurrences if var_name not in declared_seen]
	if undeclared:
		frappe.throw(_("Template uses undeclared parameter(s): {0}.").format(", ".join(undeclared)))

	unused = [param_name for param_name in declared if param_name not in used_seen]
	if unused:
		frappe.throw(
			_("Declared template parameter(s) are not used in Header, Body, or Footer: {0}.").format(
				", ".join(unused)
			)
		)
	return declared


def validate_template_structure(doc, *, definition=None):
	"""Compatibility name for the shared content validator."""
	return validate_template_content(doc, definition=definition)


def validate_template_variables(doc, variables):
	"""Use the same exact string contract for every Output consumer."""
	return validate_output_variables(variables, [row.parameter_name for row in doc.get('template_variables') or []])


def render_template_components(doc, variables, *, definition=None):
	"""Render non-empty Header, Body, and Footer in a single safe pass without extra separators."""
	validate_template_structure(doc, definition=definition)
	validate_template_variables(doc, variables)
	parts = []
	for field in COMPONENT_FIELDS:
		raw = doc.get(field)
		if isinstance(raw, str) and raw != "":
			rendered = TEMPLATE_PLACEHOLDER_PATTERN.sub(lambda match: variables[match.group(1)], raw)
			if len(rendered) > (16000 if field == 'body' else 140) or (
				field != 'body' and ('\n' in rendered or '\r' in rendered)
			):
				frappe.throw(_("Rendered template content exceeds the component limits."))
			parts.append(rendered)
	return "\n\n".join(parts)


def validate_template(doc, *, account=None, variables=None):
	"""Validate notification content, administrative status and the owning channel."""
	if doc.doctype == 'Bot Flow Template':
		return _validate_bot_flow_document(doc, variables)
	if doc.status not in ('Active', 'Inactive'):
		frappe.throw(_("Select Active or Inactive."))
	if not doc.language or not frappe.db.exists('Language', doc.language):
		frappe.throw(_('Select an existing Language.'))
	if doc.status == 'Active':
		from messaging.chatbot.doctype.bot_settings.language import validate_language
		validate_language(doc.language)
	if variables is not None and doc.status != 'Active':
		frappe.throw(_("The communication template is not active."))
	if not any(doc.get(field) for field in COMPONENT_FIELDS):
		frappe.throw(_("Communication Template requires content."))
	if not doc.channel or not frappe.db.exists("Communication Channel", doc.channel):
		frappe.throw(_("Select an existing Communication Channel."))
	if not frappe.db.exists('Communication Provider', doc.provider):
		frappe.throw(_("Select an existing communication provider."))
	from messaging.communication.doctype.communication_provider.communication_provider import provider_definition, operation
	from messaging.communication.doctype.communication_provider_value.communication_provider_value import validate_values
	definition = provider_definition(doc.provider)
	validate_values(doc, operational=variables is not None)
	if account and doc.channel != frappe.db.get_value('Communication Channel Account', account, 'channel'):
		frappe.throw(_("The template channel does not match the channel account."))
	validate_template_content(doc, definition=definition)
	if definition and 'validate_template' in definition.get('operations', {}):
		if operation(definition, 'validate_template', doc, variables) is not True:
			frappe.throw(_("The provider rejected the communication template."))
	if variables is not None:
		validate_template_variables(doc, variables)
	return doc


def _validate_bot_flow_document(doc, variables):
	from messaging.chatbot.doctype.bot_settings.language import get_bot_settings
	if variables is not None and not doc.enabled:
		frappe.throw(_("The bot flow template is disabled."))
	if doc.enabled:
		allowed = {row.language for row in get_bot_settings().chatbot_languages if row.enabled}
		if doc.language not in allowed:
			frappe.throw(_("Template language is not authorized for this Output."))
	elif not doc.language or not frappe.db.exists('Language', doc.language):
		frappe.throw(_('Select an existing Language.'))
	if not any(doc.get(field) for field in COMPONENT_FIELDS):
		frappe.throw(_("Communication Template requires content."))
	validate_template_content(doc)
	if variables is not None:
		validate_template_variables(doc, variables)
	return doc


def validate_output_variables(variables, required=None):
    """No formatting, defaults or coercion may cross the substitution boundary."""
    if not isinstance(variables, dict) or len(variables) > 100 or any(
        type(key) is not str or type(value) is not str for key, value in variables.items()
    ):
        raise frappe.ValidationError(_('Output variables must be an object of string values.'))
    if any(not value.strip() or '\x00' in value or len(value) > 16000 for value in variables.values()):
        raise frappe.ValidationError(_('Output variables must contain non-empty strings.'))
    if required is not None and set(variables) != set(required):
        raise frappe.ValidationError(_('Output variables must exactly match the template contract.'))
    if len(json.dumps(variables, ensure_ascii=False).encode()) > 64000:
        raise frappe.ValidationError(_('Output variables exceed the maximum allowed size.'))
    return variables



def template_contract(template):
    """Structural template identity used by Flow approval. Text with the same variables stays valid."""
    return dict(enabled=int(template.enabled or 0), language=template.language,
        variables=sorted(row.parameter_name for row in template.template_variables or []))


def template_content_revision(template):
    """Content and provider-parameter identity independent of administrative status."""
    if template.doctype == 'Bot Flow Template':
        values = {name: template.get(name) for name in ('language', 'header', 'body', 'footer')}
    else:
        values = {name: template.get(name) for name in ('language', 'header', 'body', 'footer', 'channel')}
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import value_fingerprint
        values['provider_values'] = value_fingerprint(template)
    values['variables'] = sorted(row.parameter_name for row in template.get('template_variables') or [])
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def template_revision(template):
    """Content identity is independent of transport, document labels and save times."""
    if template.doctype == 'Bot Flow Template':
        values = {name: template.get(name) for name in ('language', 'header', 'body', 'footer')}
        values['enabled'] = cint(template.get('enabled'))
    else:
        values = {name: template.get(name) for name in ('status', 'language', 'header', 'body', 'footer', 'channel')}
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import value_fingerprint
        values['provider_values'] = value_fingerprint(template)
    values['variables'] = sorted(row.parameter_name for row in template.get('template_variables') or [])
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _blocking_active_notification_rules(template_doc):
    """Lock and return administratively active Notification Rules consuming this template."""
    template_names = [template_doc.name]
    return frappe.db.sql(
        """
        SELECT DISTINCT r.name, r.rule_name
        FROM `tabNotification Rule` r
        INNER JOIN `tabNotification Outcome` o
            ON o.parent = r.name
            AND o.parenttype = 'Notification Rule'
        WHERE r.enabled = 1
            AND o.send = 1
            AND o.template_reference IN %s
        ORDER BY r.name
        FOR UPDATE
        """,
        (tuple(template_names),),
        as_dict=True,
    )


@frappe.whitelist(methods=["POST"])
def set_active(candidate, active):
    """Transition Communication Template administrative status via validated server action."""
    from messaging.security.permissions import require_system_manager
    require_system_manager()
    frappe.has_permission("Communication Template", "write", throw=True)
    if isinstance(candidate, str):
        text = candidate.strip()
        candidate = json.loads(text) if text.startswith("{") else {"name": candidate}
    if not isinstance(candidate, dict):
        frappe.throw(_("Invalid Communication Template selection."), frappe.PermissionError)
    if candidate.get("__islocal") or not candidate.get("name"):
        frappe.throw(_("Save the template before changing its status."))
    if type(active) not in (int, bool, str) or active not in (0, 1, "0", "1"):
        frappe.throw(_("Invalid template activation."))
    target_status = "Active" if int(active) else "Inactive"
    name = candidate["name"]
    expected_modified = candidate.get("modified")
    if target_status == "Inactive":
        lock_document_types("Communication Template", "Notification Rule")
    doc = frappe.get_doc("Communication Template", name, for_update=True)
    doc.check_permission("read")
    doc.check_permission("write")
    if expected_modified is not None and str(expected_modified) != str(doc.modified or ""):
        frappe.throw(
            _("Configuration changed. Reload the template before changing its status."),
            frappe.TimestampMismatchError,
        )
    savepoint = "activate_template_" + frappe.generate_hash(length=8)
    frappe.db.savepoint(savepoint)
    try:
        if target_status == "Inactive":
            blocking = _blocking_active_notification_rules(doc)
            if blocking:
                labels = [
                    f"{row.rule_name} ({row.name})" if row.rule_name and row.rule_name != row.name else row.name
                    for row in blocking
                ]
                frappe.throw(
                    _(
                        "Cannot deactivate this template because it is used by active Notification Rules: {0}. "
                        "Disable those rules or remove the template from their sending outputs first."
                    ).format(", ".join(labels))
                )
        with _STATUS_TRANSITION_WRITE():
            doc.status = target_status
            doc.save()
    except Exception:
        frappe.db.rollback(save_point=savepoint)
        raise
    return dict(
        status=doc.status,
        revision=doc.revision,
        provider_status=doc.provider_status,
    )



def provider_template_contract(account, reference):
    """Installed adapters own external identities, account checks and approval state.

    The registered resolver is read-only: it must not send or write business data.
    Mapping transport parameters remains in prepare_provider_template immediately
    before handoff; both callbacks are owned by the existing provider registry.
    """
    from messaging.communication.doctype.communication_provider.communication_provider import provider_definition, operation
    from messaging.communication.doctype.communication_channel_account.communication_channel_account import account_provider
    definition = provider_definition(account_provider(account))
    required = {'resolve_provider_template', 'prepare_provider_template'}
    if ('provider_templates' not in definition['capabilities'] or not required <= set(definition['operations'])
        or type(reference) is not str or not reference.strip() or len(reference) > 140):
        raise frappe.ValidationError(_('This provider cannot resolve external templates.'))
    template = frappe.get_doc('Communication Template', reference)
    validate_template(template, account=account)
    language = template.language
    from messaging.communication.doctype.process_definition.execution import read_boundary
    with read_boundary():
        descriptor = operation(definition, 'resolve_provider_template', dict(account=account, reference=reference, language=language))
    if (not isinstance(descriptor, dict) or set(descriptor) != {'reference', 'account', 'language', 'revision', 'available', 'approved', 'variables'}
        or descriptor['reference'] != reference or descriptor['account'] != account or descriptor['language'] != language
        or descriptor['available'] is not True or descriptor['approved'] is not True
        or type(descriptor['revision']) is not str or not descriptor['revision'] or len(descriptor['revision']) > 140
        or type(descriptor['variables']) is not list or len(descriptor['variables']) > 100
        or any(type(key) is not str for key in descriptor['variables']) or len(set(descriptor['variables'])) != len(descriptor['variables'])):
        raise frappe.ValidationError(_('Invalid or unavailable provider template contract.'))
    for key in descriptor['variables']:
        validate_template_variable_name(key, notification=True)
    return descriptor



def represent_output_value(value):
    """Safe scalar text after the Process contract has validated the business type."""
    from decimal import Decimal
    from datetime import date
    import math
    if type(value) is str:
        return value
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        return str(value)
    if type(value) is Decimal and value.is_finite():
        return format(value, 'f')
    if type(value) is date:
        return value.isoformat()
    if type(value) is float and math.isfinite(value):
        return str(value)
    raise frappe.ValidationError(_('Template variables require a present scalar output value.'))


def validate_template_fields(reference, fields, *, language=None, active=True, usage='Bot Flow', account=None):
    doctype = 'Communication Template' if usage == 'Notification' else 'Bot Flow Template'
    template = frappe.get_doc(doctype, reference, for_update=bool(usage == 'Notification' and active))
    from messaging.security.execution_context import is_automation
    if usage != 'Notification' or not is_automation():
        template.check_permission('read')
    if usage == 'Notification':
        channel = frappe.db.get_value('Communication Channel Account', account, 'channel') if account else None
        if channel and template.channel != channel:
            raise frappe.ValidationError(_('The template channel does not match the channel account.'))
        if active and (template.status != 'Active' or not channel):
            raise frappe.ValidationError(_('The output template must be active and match its channel.'))
    validate_template(template, account=account if usage == 'Notification' else None) if active else validate_template_content(template)
    if (active and usage == 'Bot Flow' and not template.enabled) or (language and template.language != language):
        raise frappe.ValidationError(_('The output template must be enabled and match its language.'))
    from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
    from messaging.communication.doctype.communication_channel_account.communication_channel_account import account_provider
    if usage == 'Notification' and active and account and 'provider_templates' in provider_definition(account_provider(account))['capabilities']:
        from messaging.chatbot.doctype.bot_settings.language import presentation_language
        with presentation_language(language or template.language):
            external = provider_template_contract(account, template.name)
        if set(external['variables']) != {r.parameter_name for r in template.template_variables or []}:
            raise frappe.ValidationError(_('The external template variables must match the communication template.'))
    for row in template.template_variables or []:
        if (row.parameter_name not in fields if usage == 'Notification'
            else fields.get(row.parameter_name) not in ('string', 'nullable_string')):
            raise frappe.ValidationError(_('Template field {0} must be a declared string field of this output.').format(row.parameter_name))


def resolve_output(mode, *, message='', variables=None, data=None, template_reference=None, account=None,
                   provider_template_reference=None, expected_revision=None, usage='Bot Flow'):
    """Return validated text and the exact revision used by tests and runtime."""
    if data is not None:
        if variables is not None:
            raise frappe.ValidationError(_('Supply the Schema data once for Output validation.'))
        from messaging.registrations.processes import validate_process_schema_v1
        validate_process_schema_v1(dict(version=1, status='success', recipient='', data=data, message=message))
    variables = {} if variables is None else variables
    if mode == 'Message':
        if template_reference or provider_template_reference or type(message) is not str or not message.strip() or '\x00' in message or len(message) > 16000:
            raise frappe.ValidationError(_('Message Output requires a non-empty message and no template.'))
        validate_output_variables(variables, ())
        return dict(text=message, revision='', variables=variables)
    if mode == 'Template':
        if not template_reference or provider_template_reference:
            raise frappe.ValidationError(_('Template Output requires exactly one communication template.'))
        doctype = 'Communication Template' if usage == 'Notification' else 'Bot Flow Template'
        template = frappe.get_doc(doctype, template_reference, for_update=True)
        if usage == 'Notification':
            if template.status != 'Active' or not account:
                raise frappe.ValidationError(_('The output template must be active and match its channel.'))
        elif not template.enabled:
            raise frappe.ValidationError(_('The bot flow template is disabled.'))
        if data is not None:
            required = [row.parameter_name for row in template.get('template_variables') or []]
            variables = {key: represent_output_value(data[key]) if usage == 'Notification' else data[key]
                for key in required if key in data}
        validate_template(template, account=account if usage == 'Notification' else None, variables=variables)
        from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
        from messaging.communication.doctype.communication_channel_account.communication_channel_account import account_provider
        external = usage == 'Notification' and account and 'provider_templates' in provider_definition(account_provider(account))['capabilities']
        if external:
            descriptor = provider_template_contract(account, template.name)
            validate_output_variables(variables, descriptor['variables'])
            if expected_revision is not None and expected_revision != descriptor['revision']:
                raise frappe.ValidationError(_('Output template revision changed. Validate the Output again.'))
            return dict(text='', revision=descriptor['revision'], variables=variables)
        revision = template_revision(template)
        if expected_revision is not None and expected_revision != revision:
            raise frappe.ValidationError(_('Output template revision changed. Validate the Output again.'))
        return dict(text=render_template_components(template, variables), revision=revision, variables=variables)
    if mode == 'Provider Template':
        if not account or template_reference or not provider_template_reference:
            raise frappe.ValidationError(_('Provider Template Output requires an account and an external reference.'))
        descriptor = provider_template_contract(account, provider_template_reference)
        if data is not None:
            variables = {key: data[key] for key in descriptor['variables'] if key in data}
        validate_output_variables(variables, descriptor['variables'])
        if expected_revision is not None and expected_revision != descriptor['revision']:
            raise frappe.ValidationError(_('Output template revision changed. Validate the Output again.'))
        return dict(text='', revision=descriptor['revision'], variables=variables)
    raise frappe.ValidationError(_('Unsupported Output mode.'))


def _preset_doctype(usage):
    return 'Bot Flow Template' if usage == 'Bot Flow' else 'Communication Template'


def build_preset_template(definition, code, *, usage, account=None, channel=None):
    """Create a new identity; an unrelated existing identity is an explicit conflict."""
    template_name = definition.get('localized_names', {}).get(code, definition['template_name'])
    name = template_identity(template_name, code)
    if frappe.db.exists(_preset_doctype(usage), name):
        frappe.throw(_('Template identity {0} already exists without this consumer binding. Review the collision in Desk.').format(name))
    return preset_template_document(definition, code, usage=usage, account=account, channel=channel)


def preset_template_document(definition, code, *, usage, account=None, channel=None):
    """Build native fields without checking installation identity or writing documents."""
    template_name = definition.get('localized_names', {}).get(code, definition['template_name'])
    content = definition['languages'][code]
    variables = list(dict.fromkeys(variable for component, text in content.items()
        for variable in extract_component_variables(text, component.title(), notification=usage == "Notification")))
    doc = frappe.new_doc(_preset_doctype(usage))
    values = dict(template_name=template_name, language=code,
        template_variables=[dict(parameter_name=value) for value in variables], **content)
    if usage == 'Bot Flow':
        values['enabled'] = 1
    else:
        selected_channel = channel or definition.get('channel')
        if not selected_channel and account:
            selected_channel = frappe.db.get_value('Communication Channel Account', account, 'channel')
        if not selected_channel:
            frappe.throw(_('Select a Communication Channel.'))
        values['channel'] = selected_channel
        values['status'] = 'Inactive'
    doc.update(values)
    return doc


def create_preset_template(definition, code, *, usage, account=None, channel=None):
    doctype = _preset_doctype(usage)
    name = template_identity(definition.get('localized_names', {}).get(code, definition['template_name']), code)
    if definition.get('reuse_existing') and frappe.db.exists(doctype, name):
        doc = frappe.get_doc(doctype, name)
        doc.check_permission('read')
        content = definition['languages'][code]
        if any((doc.get(key) or '') != (content.get(key) or '') for key in COMPONENT_FIELDS):
            frappe.throw(_('The restored template identity contains administrative changes.'))
        if usage == 'Notification' and channel and doc.channel != channel:
            frappe.throw(_('The template channel does not match the channel account.'))
        validate_template(doc, account=account if usage == 'Notification' else None)
        return doc.name
    doc = build_preset_template(definition, code, usage=usage, account=account, channel=channel)
    doc.insert()
    return doc.name


def registered_template_definition(name):
    """Return a copy of a Code-First generic template definition when registered without a channel."""
    from copy import deepcopy
    entry = _REGISTERED_TEMPLATE_DEFINITIONS.get(name)
    return deepcopy(entry) if entry else None


def materialize_registered_template(name, *, channel):
    """Materialize an Inactive Communication Template from a Code-First generic definition once a channel is chosen."""
    from messaging.security.permissions import require_system_manager
    require_system_manager()
    frappe.has_permission('Communication Template', 'create', throw=True)
    if not channel or not frappe.db.exists('Communication Channel', channel):
        frappe.throw(_('Select an existing Communication Channel.'))
    if frappe.db.exists('Communication Template', name):
        doc = frappe.get_doc('Communication Template', name)
        doc.check_permission('read')
        if doc.channel != channel:
            frappe.throw(_('The template channel does not match the channel account.'))
        return doc.name
    entry = registered_template_definition(name)
    if not entry:
        frappe.throw(_('Registered template definition {0} was not found.').format(name))
    return create_preset_template(entry['definition'], entry['language'], usage='Notification', channel=channel)


def save_registered_template(*, definition, language, usage='Notification', channel=None):
    """Create a native template when channel is known, or register the Code-First definition without partial DB docs."""
    import inspect
    from copy import deepcopy
    from messaging.security.permissions import require_registration_owner
    frame = inspect.currentframe()
    try:
        app = frame.f_back.f_globals.get('__name__', '').split('.')[0]
        require_registration_owner(app, frame.f_back)
    finally:
        del frame
    doctype = _preset_doctype(usage)
    name = template_identity(definition['template_name'], language)
    if frappe.db.exists(doctype, name):
        doc = frappe.get_doc(doctype, name)
        doc.check_permission('read')
        selected_channel = channel or definition.get('channel')
        if usage == 'Notification' and selected_channel and doc.channel != selected_channel:
            frappe.throw(_('The template channel does not match the channel account.'))
        if doc.language != language or doc.template_name != definition['template_name']:
            frappe.throw(_('Registered template identity belongs to incompatible content.'))
        return name
    selected_channel = channel or definition.get('channel')
    if usage == 'Notification' and not selected_channel:
        content = definition['languages'][language]
        doc = validate_preset_content(dict(template_name=definition['template_name'], **content), language, usage=usage)
        _REGISTERED_TEMPLATE_DEFINITIONS[name] = dict(
            app=app,
            template_name=definition['template_name'],
            language=language,
            definition=deepcopy(definition),
            variables=[row.parameter_name for row in doc.template_variables],
        )
        return name
    return create_preset_template(definition, language, usage=usage, channel=selected_channel)


def validate_preset_content(content, language, *, usage='Bot Flow'):
    """Validate localized initial content using native template fields without Bot Settings."""
    definition = dict(template_name=content['template_name'], languages={language: {
        key: value for key, value in content.items() if key != 'template_name'}})
    doc = preset_template_document(definition, language, usage='Bot Flow' if usage == 'Bot Flow' else 'Notification', channel='unbound-preset')
    validate_template_content(doc, notification=usage == 'Notification')
    doc._validate_length()
    return doc
