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
from .languages import declared_language, require_language, set_language_mapping
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
	require_language(language)
	return _descriptive_identity(template_name, _snake_case(language))


def _descriptive_identity(template_name, language_token):
	if not isinstance(template_name, str):
		frappe.throw(_("Template Name is required."))
	base = _snake_case(template_name)
	if not base:
		frappe.throw(_("Template Name must contain letters or digits after normalization."))
	name = base + '_' + language_token
	if len(name) > 140:
		frappe.throw(_("Normalized template identity exceeds 140 characters."))
	return name


_IDENTITY_MIGRATION = InternalWriteGuard("messaging_template_identity_migration")


def scoped_template_identity(template_name, language, channel, account=None, *, provider_language=None):
    if provider_language:
        token = _snake_case(provider_language)
        # Retain readable conventional locales. Distinguish opaque codes whose normalized spellings can coincide.
        if not re.fullmatch(r'[a-z0-9]+(?:_[A-Z0-9]+)*', provider_language):
            digest = hashlib.sha256(provider_language.encode()).hexdigest()[:16]
            token = f'{token}_{digest}' if token else digest
        descriptive = _descriptive_identity(template_name, token)
    else:
        descriptive = template_identity(template_name, language)
    prefix = _snake_case(channel or '')
    if not prefix or account and (not isinstance(account, str) or not account.isprintable()
            or account != account.strip()):
        frappe.throw(_("Invalid template channel or account identity."))
    name = '_'.join(part for part in (prefix, account, descriptive) if part)
    if len(name) > 140:
        frappe.throw(_("Normalized template identity exceeds 140 characters."))
    from frappe.model.naming import validate_name
    return validate_name('Communication Template', name)


class CommunicationTemplate(Document):
    @property
    def provider(self):
        """Resolve technical operations exclusively through the owning channel."""
        return frappe.db.get_value('Communication Channel', self.channel, 'provider') if self.channel else None

    def onload(self):
        from .external import _capabilities, provider_definition, stored

        if self.provider:
            try:
                capabilities = _capabilities(provider_definition(self.provider))
            except frappe.ValidationError:
                return
            self.set_onload('template_capabilities', capabilities)
            self.set_onload('template_status_supported', capabilities['query'])
            if capabilities['managed']:
                self.set_onload('template_sync_issue', stored(self, 'externally_observed').get('issue'))
                self.set_onload('template_publication_pending', bool(stored(self, 'publication_request').get('pending')))

    def autoname(self):
        from .external import provider_definition
        if not self.channel:
            frappe.throw(_("Select a Communication Channel."))
        self.name = self._identity(provider_definition(self.provider))

    def _set_defaults(self):
        language = self.language
        super()._set_defaults()
        if self.is_new() and self.provider_language:
            self.language = language

    def _identity(self, definition):
        if definition.get('template_languages'):
            declared_language(definition, self.provider_language)
        return scoped_template_identity(self.template_name, self.language, self.channel, self.account,
            provider_language=self.provider_language)

    def validate(self):
        from .external import external_write, managed, protect, provider_definition
        from .status import protect_status_rows, provider_status_summary

        if not self.channel:
            frappe.throw(_("Select a Communication Channel."))
        definition = provider_definition(self.provider)
        previous = None
        if self.is_new():
            self.status = self.status or 'Inactive'
            lock_document_types('Communication Template')
            identity = self._identity(definition)
            for field in ('name', 'previous_name'):
                other = frappe.db.get_value(self.doctype, {field: identity}, 'name', for_update=True)
                if other:
                    frappe.throw(_("Template identity {0} already belongs to {1}. Choose a different Template Name.").format(identity, other))
            if self.name != identity:
                frappe.throw(_("Template identity is assigned automatically."))
        else:
            previous = frappe.get_doc(self.doctype, self.name, for_update=True)
            self._check_immutable(previous)
        set_language_mapping(self, previous, definition, synchronizing=external_write.active())
        for index, row in enumerate(self.get('template_variables') or [], 1):
            row.idx = index
        validate_template(self, definition=definition)
        self.revision = (
            template_content_revision(self, definition=definition)
            if managed(definition) or self.provider_language
            else template_revision(self)
        )
        protect(self, previous, definition)
        if managed(definition) and (not previous or not previous.external_key):
            filters = dict(channel=self.channel, external_key=self.external_key, name=['!=', self.name])
            filters['account'] = self.account or ['is', 'not set']
            if frappe.db.get_value(self.doctype, filters, 'name', for_update=True):
                frappe.throw(_("The external template identity is already bound to this account or channel."))
        protect_status_rows(self)
        if managed(definition):
            if not previous:
                self.provider_status = self.provider_status if self.published_revision else 'not_synchronized'
            else:
                if not external_write.active():
                    self.provider_status = previous.provider_status
        else:
            self.provider_status = provider_status_summary(self)
        self._validated_language_mapping = (self.language, self.language_mapping)

    def _check_immutable(self, previous):
        if (previous.provider_language or '') != (self.provider_language or ''):
            frappe.throw(_("The provider template language is immutable."))
        if not previous.provider_language and previous.language != self.language:
            frappe.throw(_("The template language is immutable."))
        for field in ('template_name', 'channel', 'account'):
            if (previous.get(field) or '') != (self.get(field) or ''):
                frappe.throw(_("Template name, language, channel and account are immutable."))
        if previous.status != self.status and not _STATUS_TRANSITION_WRITE.active():
            frappe.throw(_("Use Activate or Deactivate to change template status."))

    def on_update(self):
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import cleanup_values
        cleanup_values(self)

    def on_trash(self):
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import cleanup_values
        from .external import stored
        if stored(self, 'publication_request').get('pending'):
            frappe.throw(_("A template publication is already pending confirmation."))
        cleanup_values(self, deleting=True)

    def db_set(self, fieldname, value=None, **kwargs):
        from .status import template_status_write_active
        values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
        if not kwargs.get('commit') and template_status_write_active() and set(values) <= {'provider_status'}:
            return super().db_set(fieldname, value, **kwargs)
        frappe.throw(_("Save Communication Template changes through its validated controller."))

    def db_update(self, *args, **kwargs):
        from .external import PROTECTED_FIELDS, external_write
        validated_mapping = self.__dict__.pop('_validated_language_mapping', None)
        fields = ('template_name', 'channel', 'language', 'provider_language', 'language_mapping', 'account',
            'status', 'provider_status', *PROTECTED_FIELDS)
        stored = frappe.db.get_value(self.doctype, self.name, fields, as_dict=True)
        if stored:
            self._check_immutable(stored)
            mapping = (self.language, self.language_mapping)
            if mapping != validated_mapping and tuple(value or '' for value in mapping) != (
                stored.language or '', stored.language_mapping or ''
            ):
                frappe.throw(_("Save Communication Template changes through its validated controller."))
            if not external_write.active() and any((self.get(key) or '') != (stored.get(key) or '')
                    for key in PROTECTED_FIELDS if key != 'sync_state'):
                frappe.throw(_("External template evidence is managed internally."), frappe.PermissionError)
            if not external_write.active() and self.sync_state != stored.sync_state and not self.get_doc_before_save():
                frappe.throw(_("External template evidence is managed internally."), frappe.PermissionError)
            if not external_write.active() and self.provider_status != stored.provider_status:
                from .status import template_status_write_active
                if not template_status_write_active() and stored.provider_status:
                    frappe.throw(_("External template evidence is managed internally."), frappe.PermissionError)
        return super().db_update(*args, **kwargs)

    def before_rename(self, *args, **kwargs):
        if not _IDENTITY_MIGRATION.active():
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


def parameter_format(formats, values):
	"""Resolve an explicit external format or the provider's first declared option."""
	return values.get('parameter_format') or formats[0][0]


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
	formats = definition.get('template_parameter_formats') if definition else None
	repeat = False
	if formats:
		values = {row.parameter_key: json.loads(row.value or 'null') for row in doc.parameters
			if row.parameter_key == 'parameter_format'}
		repeat = dict(formats).get(parameter_format(formats, values), False)
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
				if repeat:
					continue
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


def validate_template(doc, *, account=None, variables=None, definition=None, operational=False):
	"""Validate notification content, administrative status and the owning channel."""
	if doc.doctype == 'Bot Flow Template':
		return _validate_bot_flow_document(doc, variables)
	if doc.status not in ('Active', 'Inactive'):
		frappe.throw(_("Select Active or Inactive."))
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
	definition = definition or provider_definition(doc.provider)
	if definition.get('template_languages'):
		declared_language(definition, doc.provider_language)
		if variables is not None:
			require_language(doc.language)
	else:
		if not doc.language or not frappe.db.exists('Language', doc.language):
			frappe.throw(_('Select an existing Language.'))
		if doc.status == 'Active':
			require_language(doc.language)
	from .external import validate_scope
	validate_scope(doc, definition, account)
	validate_values(doc, operational=variables is not None or operational)
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


def _ordered_component_variables(template):
    order = [row.parameter_name for row in template.get('template_variables') or [] if row.get('parameter_name')]
    result = {}
    for field in COMPONENT_FIELDS:
        used = set(TEMPLATE_PLACEHOLDER_PATTERN.findall(template.get(field) or ''))
        if used:
            result[field] = [name for name in order if name in used]
    return result


def template_content_revision(template, *, definition=None):
    """Content and provider-parameter identity independent of administrative status."""
    if template.doctype == 'Bot Flow Template':
        values = {name: template.get(name) for name in ('language', 'header', 'body', 'footer')}
    else:
        language_field = 'provider_language' if template.get('provider_language') else 'language'
        values = {name: template.get(name) for name in (language_field, 'header', 'body', 'footer', 'channel')}
        from messaging.communication.doctype.communication_provider_value.communication_provider_value import value_fingerprint
        values['provider_values'] = value_fingerprint(template)
        if template.get('account'):
            values['account'] = template.account
        formats = definition.get('template_parameter_formats') if definition is not None else None
        if formats is None and definition is None and template.get('channel'):
            provider = (
                template.provider
                if hasattr(template, 'provider')
                else frappe.db.get_value('Communication Channel', template.channel, 'provider')
            )
            if provider:
                from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
                try:
                    formats = provider_definition(provider).get('template_parameter_formats')
                except Exception:
                    formats = None
        if formats:
            param_values = {
                row.parameter_key: json.loads(row.value or 'null')
                for row in template.get('parameters') or []
                if row.parameter_key == 'parameter_format'
            }
            if not dict(formats).get(parameter_format(formats, param_values), False):
                component_vars = _ordered_component_variables(template)
                if component_vars:
                    values['component_variables'] = component_vars
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
        if template.get('account'):
            values['account'] = template.account
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
        WHERE r.status = 'Enabled'
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
    """Return the verified external delivery contract for the exact account."""
    from .external import managed, provider_definition, published_contract
    from messaging.communication.doctype.communication_channel_account.communication_channel_account import account_provider

    definition = provider_definition(account_provider(account))
    if not managed(definition) or not isinstance(reference, str) or not reference.strip() or len(reference) > 140:
        raise frappe.ValidationError(_('This provider cannot resolve external templates.'))
    return published_contract(reference, account)



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
    names = [row.parameter_name for row in template.template_variables or []]
    if usage == 'Notification':
        from .external import managed, provider_definition, published_contract, requires_published, validate_scope
        definition = provider_definition(template.provider)
        validate_scope(template, definition, account)
        if active and (template.status != 'Active' or not account):
            raise frappe.ValidationError(_('The output template must be active and match its channel.'))
        if managed(definition):
            contract = published_contract(reference, account, available=active, document=template)
            names = contract['variables'] if contract else []
        else:
            if active and requires_published(definition):
                raise frappe.ValidationError(_('The published template revision is unavailable.'))
            validate_template(template, account=account) if active else validate_template_content(template)
    else:
        validate_template(template) if active else validate_template_content(template)
    if (active and usage == 'Bot Flow' and not template.enabled) or (language and template.language != language):
        raise frappe.ValidationError(_('The output template must be enabled and match its language.'))
    for key in names:
        if (key not in fields if usage == 'Notification' else fields.get(key) not in ('string', 'nullable_string')):
            raise frappe.ValidationError(_('Template field {0} must be a declared string field of this output.').format(key))


def resolve_output(mode, *, message='', variables=None, data=None, template_reference=None, account=None,
                   provider_template_reference=None, expected_revision=None, usage='Bot Flow'):
    """Return validated text and the exact revision used by tests and runtime."""
    if data is not None:
        if variables is not None:
            raise frappe.ValidationError(_('Supply the Schema data once for Output validation.'))
        from messaging.registry.processes import validate_process_schema_v1
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
        if usage == 'Notification':
            from .external import managed, provider_definition, published_contract, requires_published
            definition = provider_definition(template.provider)
            if managed(definition):
                descriptor = published_contract(template_reference, account, expected_revision, document=template)
                if data is not None:
                    variables = {key: represent_output_value(data[key])
                        for key in descriptor['variables'] if key in data}
                validate_output_variables(variables, descriptor['variables'])
                return dict(text='', revision=descriptor['revision'], variables=variables,
                    external_contract=dict(descriptor['external'], variables=descriptor['variables']))
            if requires_published(definition):
                raise frappe.ValidationError(_('The published template revision is unavailable.'))
        if data is not None:
            required = [row.parameter_name for row in template.get('template_variables') or []]
            variables = {key: represent_output_value(data[key]) if usage == 'Notification' else data[key]
                for key in required if key in data}
        validate_template(template, account=account if usage == 'Notification' else None, variables=variables)
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
    doc = preset_template_document(definition, code, usage=usage, account=account, channel=channel)
    name = preset_identity(doc)
    if frappe.db.exists(doc.doctype, name):
        frappe.throw(_('Template identity {0} already exists without this consumer binding. Review the collision in Desk.').format(name))
    return doc


def preset_identity(doc):
    if doc.doctype == 'Communication Template':
        from .external import provider_definition
        return doc._identity(provider_definition(doc.provider))
    return template_identity(doc.template_name, doc.language)


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
        if account and selected_channel != frappe.db.get_value('Communication Channel Account', account, 'channel'):
            frappe.throw(_('The template channel does not match the channel account.'))
        from .external import provider_definition
        provider = frappe.db.get_value('Communication Channel', selected_channel, 'provider')
        if provider:
            provider_contract = provider_definition(provider)
            if provider_contract.get('template_scope', 'Channel') == 'Account':
                values['account'] = account
            if provider_contract.get('template_languages'):
                values['provider_language'] = definition.get('provider_languages', {}).get(code)
        values['status'] = 'Inactive'
    doc.update(values)
    return doc


def create_preset_template(definition, code, *, usage, account=None, channel=None):
    doctype = _preset_doctype(usage)
    candidate = preset_template_document(definition, code, usage=usage, account=account, channel=channel)
    name = preset_identity(candidate)
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


def materialize_registered_template(name, *, channel, account=None):
    """Materialize an inactive template for the selected persisted scope."""
    from messaging.security.permissions import require_system_manager
    from .external import provider_definition, validate_scope

    require_system_manager()
    frappe.has_permission('Communication Template', 'create', throw=True)
    if not channel or not frappe.db.exists('Communication Channel', channel):
        frappe.throw(_('Select an existing Communication Channel.'))
    entry = registered_template_definition(name)
    if entry:
        candidate = preset_template_document(entry['definition'], entry['language'],
            usage='Notification', channel=channel, account=account)
        name = preset_identity(candidate)
    if frappe.db.exists('Communication Template', name):
        doc = frappe.get_doc('Communication Template', name)
        doc.check_permission('read')
        if doc.channel != channel:
            frappe.throw(_('The template channel does not match the channel account.'))
        validate_scope(doc, provider_definition(doc.provider), account)
        return doc.name
    if not entry:
        frappe.throw(_('Registered template definition {0} was not found.').format(name))
    candidate.insert()
    return candidate.name


def save_registered_template(*, definition, language, usage='Notification', channel=None, account=None):
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
    selected_channel = channel or definition.get('channel')
    if account and not selected_channel:
        selected_channel = frappe.db.get_value('Communication Channel Account', account, 'channel')
    name = (preset_identity(preset_template_document(definition, language, usage=usage,
        account=account, channel=selected_channel)) if selected_channel or usage == 'Bot Flow'
        else template_identity(definition['template_name'], language))
    if (selected_channel or usage == 'Bot Flow') and frappe.db.exists(doctype, name):
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
    return create_preset_template(definition, language, usage=usage, channel=selected_channel, account=account)


def validate_preset_content(content, language, *, usage='Bot Flow'):
    """Validate localized initial content using native template fields without Bot Settings."""
    definition = dict(template_name=content['template_name'], languages={language: {
        key: value for key, value in content.items() if key != 'template_name'}})
    doc = preset_template_document(definition, language, usage='Bot Flow' if usage == 'Bot Flow' else 'Notification', channel='unbound-preset')
    validate_template_content(doc, notification=usage == 'Notification')
    doc._validate_length()
    return doc
