"""Document event configuration; executable paths use the shared Process authorization."""

from contextlib import contextmanager, nullcontext
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.modules.utils import get_module_app
from frappe.utils import cint

from messaging.chatbot.doctype.bot_settings.language import (
	presentation_language,
)
from messaging.common.locking import document_savepoint, lock_document_types
from messaging.common.write_guards import InternalWriteGuard
from messaging.communication.doctype.communication_provider.communication_provider import (
	definition_hash,
	operation,
	provider_definition,
	provider_registry,
)
from messaging.communication.doctype.communication_provider_value.communication_provider_value import (
	cleanup_values,
	validate_values,
	value_fingerprint,
)
from messaging.communication.doctype.communication_template.communication_template import (
	provider_template_contract,
	registered_template_definition,
	template_identity,
	template_revision,
)
from messaging.communication.doctype.notification_rule.conditions import (
	EVENTS,
	canonical_conditions,
	condition_fields,
	condition_token,
	matches,
	validate_conditions,
)
from messaging.communication.doctype.notification_rule.contract import (
	default_contract,
	parameter_fields,
	prepared_data,
	resolve_parameters,
	selected_output,
	synchronize,
	validate_notification_contract,
	validate_output_templates,
	require_notifiable_outcomes,
	validate_parameters,
)
from messaging.communication.doctype.process_definition import validation
from messaging.communication.doctype.process_definition.process_definition import (
	bind_definition,
	validate_process_method_path,
)
from messaging.registrations.processes import ProcessSchemaValidationError
from messaging.security.permissions import require_system_manager

EXCLUDED = frozenset(
	(
		"Notification Rule",
		"Notification Queue",
		"Process Definition",
		"Process Execution Log",
		"Error Log",
	)
)


def require_manager():
	require_system_manager()
	frappe.has_permission("Notification Rule", "write", throw=True)


def invalidate_process_approval(definition, method=None):
	previous = definition.get_doc_before_save()
	if not previous or (previous.capability_revision, previous.status) == (definition.capability_revision, definition.status):
		return
	for name in frappe.get_all('Notification Rule', filters={'process_definition': definition.name}, pluck='name'):
		rule = frappe.get_doc('Notification Rule', name, for_update=True)
		validation.clear_approval(rule)
		frappe.db.set_value('Notification Rule', name,
			dict(enabled=0, **{key: rule.get(key) for key in validation.RECEIPT_FIELDS}))
	invalidate_rules()


def source_app(doctype):
	return get_module_app(frappe.get_meta(doctype).module)


def content_types(definition):
	if "validate_notification" not in definition["operations"]:
		return []
	capabilities = set(definition["capabilities"])
	result = []
	if (
		"templates" in capabilities
		or ("text" in capabilities and "notification_template_required" not in capabilities)
		or (
			"provider_templates" in capabilities
			and {"resolve_provider_template", "prepare_provider_template"} <= set(definition["operations"])
		)
	):
		result.append("Template")
	return result


STRUCTURAL_FIELDS = (
	"rule_name",
	"source_application",
	"source_doctype",
	"account",
	"redirect_recipient",
)


def validate_immutable_structure(rule):
	"""Compare with a locked persisted owner; disabled rules have the same identity."""
	if rule.is_new():
		return
	previous = frappe.get_doc("Notification Rule", rule.name, for_update=True)

	def signature(doc):
		values = {field: doc.get(field) or None for field in STRUCTURAL_FIELDS}
		values["events"] = sorted(
			(r.document_event, r.field_name or "", r.value or "") for r in doc.events
		)
		values["parameters"] = sorted(
			(r.parameter_key, r.value or "", r.secret_value or "") for r in doc.parameters
		)
		return values

	before, after = signature(previous), signature(rule)
	# An account-less draft can bind its first real account and provider values.
	# Once bound, the existing immutable operational identity applies.
	if not previous.account and not previous.enabled and not rule.enabled:
		for field in ("account", "parameters", "redirect_recipient"):
			before.pop(field)
			after.pop(field)
	if _preset_write.active():
		# Authorized documentary synchronization may update registered defaults only.
		for field in ('rule_name', 'events'):
			before.pop(field, None)
			after.pop(field, None)
	if after != before:
		frappe.throw(
			_("Notification Rule structure is immutable. Disable this rule and create a replacement.")
		)


_preset_write = InternalWriteGuard("messaging_notification_rule_provenance")


class NotificationRule(Document):
	def _protect_application(self):
		if not _preset_write.active():
			previous = None if self.is_new() else frappe.get_doc('Notification Rule', self.name)
			for field in ('registered_hash', 'registered_snapshot'):
				if (self.get(field) or '') != ((previous.get(field) if previous else '') or ''):
					raise frappe.PermissionError(_('Registered configuration versions are managed by the owning application.'))

		stored = frappe.db.get_value(self.doctype, self.name, "application") if not self.is_new() else None
		if (stored or "") != (self.application or ""):
			if not self.is_new() or not _preset_write.active():
				raise frappe.PermissionError(
					_("Application provenance is managed by Code-First registration.")
				)
		if self.is_new() and self.application and self.application not in frappe.get_installed_apps():
			raise frappe.PermissionError(_("Registration owner must be an installed application."))

	@property
	def lifecycle_status(self):
		tested = self.test_state(remember_invalid=True)
		if tested == "Unavailable":
			return "Unavailable"
		if tested != "Passed":
			return "Untested"
		return "Enabled" if self.enabled else "Disabled"

	def onload(self):
		self.set_onload("account_bound", bool(self.account))
		self.set_onload(
			"rule_lifecycle",
			dict(
				lifecycle_status=self.lifecycle_status,
				verification_error=self.flags.process_verification_error,
			),
		)

	def test_state(self, *, remember_invalid=False):
		return validation.test_state(self, remember_invalid=remember_invalid)

	def require_approved(self):
		return validation.require_approved(self)

	def runtime_evidence(self, definition, *, administrative_test=False):
		return validation.runtime_evidence(self, definition, administrative_test=administrative_test)

	def validate_notification_contract(self):
		return validate_notification_contract(self)

	def resolve_parameters(self, source):
		return resolve_parameters(self, source)

	def process_language(self):
		"""Use selected template content for business presentation, independently of Bot Settings."""
		return next((frappe.db.get_value('Communication Template', row.template_reference, 'language')
			for row in self.outputs if row.send and row.template_reference), None)

	def _match_source_condition(self, doc, event, occurrence):
		selected = [
			c
			for c in canonical_conditions(self)
			if c["document_event"] == event and condition_token(c) == occurrence
		]
		return len(selected) == 1 and matches(selected[0], doc)

	def _canonical_conditions_with_tokens(self):
		return [(condition_token(c), c) for c in canonical_conditions(self)]

	def _render_process_result(self, response, *, administrative_test=False):
		prepared_data(response, self, administrative_test=administrative_test)
		row = selected_output(self, response["status"])
		return dict(status=response["status"], output_mode="Template", rendered_message="")

	def _validate_process_candidate(self):
		from messaging.communication.doctype.process_definition.process_definition import registered_contract
		require_notifiable_outcomes(registered_contract(bind_definition(self)))
		validate_rule(self)
		validate_notification_contract(self)

	def _consumer_fingerprint(self, *, include_schedule=True):
		config = dict(validation._fields(self, validation.RULE_FIELDS))
		config["events"] = canonical_conditions(self)
		config["process_contract"] = self.get("process_contract")
		config["process_parameters"] = [
			validation._fields(
				r,
				(
					"parameter_name",
					"internal_type",
					"required",
					"multiple",
					"value_source",
					"fixed_value",
					"source_field",
				),
			)
			for r in self.process_parameters
		]
		config["outcomes"] = [
			validation._fields(r, ("status_code", "template_reference", "send"))
			for r in self.outputs
		]
		lock_templates = bool(
			self.flags.get("_lock_templates")
			or getattr(frappe.flags, "_lock_notification_rule_templates", False)
		)
		templates_map = {}
		for r in self.outputs:
			if r.send and r.template_reference:
				if frappe.db.exists("Communication Template", r.template_reference):
					base = frappe.get_doc(
						"Communication Template",
						r.template_reference,
						for_update=lock_templates,
					)
					templates_map[r.template_reference] = template_revision(base)

				else:
					templates_map[r.template_reference] = None

		config["templates"] = templates_map
		config["delivery"] = delivery_contract(self)
		if include_schedule:
			config["notification_delay_minutes"] = self.get("notification_delay_minutes")
		return config

	def before_validate(self):
		self._protect_application()
		enabled = bool(cint(self.enabled))
		if enabled:
			lock_document_types("Communication Template", "Notification Rule")
		with _lock_template_reads() if enabled else nullcontext():
			validate_rule(self)
			validation.validate_readiness(self, enabled=enabled)

	def validate(self):
		self._protect_application()
		enabled = bool(cint(self.enabled))
		with _lock_template_reads() if enabled else nullcontext():
			validate_rule(self)
			validation.validate_readiness(self, enabled=enabled)

	def db_set(self, fieldname, value=None, **kwargs):
		# Use save so readiness and immutable structure are checked on backend actions.
		if kwargs.get("commit"):
			frappe.throw(_("Notification Rule updates cannot commit implicitly."))
		values = fieldname if isinstance(fieldname, dict) else {fieldname: value}
		if set(values) != {"enabled"}:
			frappe.throw(_("Save Notification Rule changes through its validated controller."))
		self.update(values)
		return self.save()

	def db_update(self, *args, **kwargs):
		validate_immutable_structure(self)
		previous = frappe.get_doc("Notification Rule", self.name)
		if cint(self.enabled):
			with _lock_template_reads():
				validate_notification_contract(self)
				if not cint(previous.enabled):
					validation.require_approved(self)
		return super().db_update(*args, **kwargs)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Notification rule identity is immutable."))

	def on_update(self):
		cleanup_values(self)
		invalidate_rules()

	def on_trash(self):
		cleanup_values(self, deleting=True)
		invalidate_rules()


def validate_notification_delay(value):
	"""Accept only canonical whole minutes within the existing one-year limit."""
	if type(value) is int:
		valid = 1 <= value <= 525600
	elif type(value) is str:
		valid = bool(re.fullmatch(r"[1-9][0-9]{0,5}", value)) and int(value) <= 525600
	else:
		valid = False
	if not valid:
		frappe.throw(_("Notification delay must be between 1 and 525600 whole minutes."))
	return int(value)


def validate_rule(rule):
	validate_immutable_structure(rule)
	rule.notification_delay_minutes = validate_notification_delay(
		rule.get("notification_delay_minutes")
	)
	if rule.source_application not in frappe.get_installed_apps():
		frappe.throw(_("Select an installed source application."))
	meta = frappe.get_meta(rule.source_doctype)
	if (
		source_app(rule.source_doctype) != rule.source_application
		or meta.istable
		or meta.issingle
		or meta.is_virtual
		or rule.source_doctype in EXCLUDED
	):
		frappe.throw(
			_("The DocType does not belong to this source application or cannot be observed.")
		)
	validate_conditions(rule)
	bind_definition(rule)
	if rule.process_invocation_mode not in ("Internal Call (frappe.call)", "Whitelisted API"):
		frappe.throw(_("Unsupported Process invocation mode."))
	enabled = bool(cint(rule.enabled))
	if enabled and not rule.outputs:
		frappe.throw(_("Configure at least one output before enabling or validating this Notification Rule."))
	contract = synchronize(rule)
	validate_parameters(rule, contract)
	validate_output_templates(rule, contract)
	if not rule.account:
		if enabled or rule.parameters or rule.redirect_recipient:
			frappe.throw(
				_(
					"Configure a Communication Channel Account before enabling the rule or provider settings."
				)
			)
		rule.channel_context = rule.provider_context = None
		return
	account = frappe.get_doc("Communication Channel Account", rule.account)
	channel = frappe.get_doc("Communication Channel", account.channel)
	definition = provider_definition(channel.provider)
	if not account.account_id:
		frappe.throw(_("The provider does not support this notification content type."))
	rule.channel_context, rule.provider_context = channel.name, channel.provider
	if rule.redirect_recipient and (
		"recipient_redirect" not in definition["capabilities"]
		or "notification_redirect_required" in definition["capabilities"]
	):
		frappe.throw(_("This provider does not allow optional recipient redirection."))
	required = "notification_redirect_required" in definition["capabilities"]
	old = rule.get_doc_before_save()
	if old and old.account != rule.account:
		old_channel = frappe.db.get_value("Communication Channel Account", old.account, "channel")
		old_provider = frappe.db.get_value("Communication Channel", old_channel, "provider")
		if old_provider != channel.provider and {r.name for r in old.parameters} & {
			r.name for r in rule.parameters
		}:
			frappe.throw(
				_("Reconfigure notification provider parameters after changing the provider.")
			)
	first_binding = bool(old and not old.account)
	validate_values(
		rule,
		check_required=bool(
			first_binding
			or ((required or rule.redirect_recipient) and (enabled or rule.redirect_recipient))
		),
	)
	for row in rule.outputs:
		if row.send and "Template" not in content_types(definition):
			frappe.throw(_("The provider does not support this notification content type."))
	if enabled:
		validate_notification_contract(rule)
	if first_binding or enabled or rule.redirect_recipient or (required and rule.parameters):
		notification_destination(rule, account)


def invalidate_rules():
	# Invalidate now and after commit: an uncommitted reader must not repopulate an old map.
	frappe.cache.delete_value("messaging_notification_rules")
	frappe.local.notification_rules_changed = True
	frappe.db.after_commit.add(lambda: frappe.cache.delete_value("messaging_notification_rules"))
	frappe.db.after_rollback.add(lambda: frappe.cache.delete_value("messaging_notification_rules"))


def rule_index():
	def load():
		result = {}
		for row in frappe.get_all(
			"Notification Rule",
			filters={"enabled": 1},
			fields=["name", "source_doctype"],
			order_by="name",
		):
			for event in sorted(
				set(
					frappe.get_all(
						"Notification Rule Event",
						filters={
							"parent": row.name,
							"parenttype": "Notification Rule",
							"parentfield": "events",
						},
						pluck="document_event",
					)
				)
			):
				result.setdefault(row.source_doctype, {}).setdefault(event, []).append(row.name)
		return result

	# Do not cache uncommitted rule changes in another request's cache.
	return (
		load()
		if getattr(frappe.local, "notification_rules_changed", False)
		else frappe.cache.get_value("messaging_notification_rules", generator=load)
	)


@frappe.whitelist()
def configuration(source_application=None, source_doctype=None, account=None):
	require_manager()
	result = dict(
		applications=frappe.get_installed_apps(),
		events=[],
		condition_fields=[],
		process_fields=[],
		modules=[],
		content_types=[],
		channel="",
		provider="",
		redirect_supported=False,
		redirect_required=False,
		delivery_information="",
	)
	if source_application:
		result["modules"] = [
			m
			for m in frappe.get_all("Module Def", pluck="name")
			if get_module_app(m) == source_application
		]
	if source_doctype:
		meta = frappe.get_meta(source_doctype)
		frappe.has_permission(source_doctype, "read", throw=True)
		result["process_fields"] = [
			{k: field.get(k) for k in ("fieldname", "label", "fieldtype", "options")}
			for field in parameter_fields(meta)
		]
		result.update(
			events=list(EVENTS if meta.is_submittable else EVENTS[:2]),
			condition_fields=[
				{
					k: field.get(k)
					for k in (
						"fieldname",
						"label",
						"fieldtype",
						"options",
						"length",
						"precision",
					)
				}
				for field in condition_fields(meta)
			],
		)
	if account:
		binding = frappe.get_doc("Communication Channel Account", account)
		channel = frappe.get_doc("Communication Channel", binding.channel)
		definition = provider_definition(channel.provider)
		required = "notification_redirect_required" in definition["capabilities"]
		result.update(
			channel=channel.name,
			provider=channel.provider,
			content_types=content_types(definition),
			redirect_supported="recipient_redirect" in definition["capabilities"] and not required,
			redirect_required=required,
		)
		if required:
			result["delivery_information"] = _(
				"Configure the required test destination on this notification rule."
			)
	return result


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def account_query(doctype, txt, searchfield, start, page_len, filters):
	require_manager()
	providers = [
		r["id"]
		for r in provider_registry()
		if not (filters or {}).get("templates") or "templates" in r["capabilities"]
	]
	if not providers:
		return []
	channel = (filters or {}).get("channel")
	return frappe.db.sql(
		"""SELECT a.name, a.account_name, a.account_id, a.channel, c.provider
		FROM `tabCommunication Channel Account` a JOIN `tabCommunication Channel` c ON c.name=a.channel
		WHERE c.provider IN %(providers)s AND (%(channel)s IS NULL OR a.channel=%(channel)s) AND (a.name LIKE %(txt)s OR a.account_name LIKE %(txt)s OR a.account_id LIKE %(txt)s)
		ORDER BY a.account_name, a.name LIMIT %(limit)s OFFSET %(start)s""",
		{
			"providers": providers,
			"channel": channel,
			"txt": "%" + txt + "%",
			"limit": min(page_len, 100),
			"start": start,
		},
	)


def on_doctype_update():
	frappe.db.add_index("Notification Rule", ["enabled", "source_doctype"], "notification_source_rules")


def notification_destination(rule, account, recipient=None):
	"""Resolve only the rule destination explicitly authorized by the provider."""
	if rule.account != account.name:
		frappe.throw(_("The notification rule does not match the channel account."))
	provider = frappe.db.get_value("Communication Channel", account.channel, "provider")
	definition = provider_definition(provider)
	required = "notification_redirect_required" in definition["capabilities"]
	redirect = bool(rule.get("redirect_recipient"))
	if not required and not redirect:
		return dict(effective_recipient=recipient, emulator_contact=None, delivery_mode="Normal")
	if (
		"recipient_redirect" not in definition["capabilities"]
		or "notification_destination" not in definition["operations"]
	):
		frappe.throw(_("This provider does not support recipient redirection."))
	validate_values(rule, operational=True)
	result = operation(definition, "notification_destination", rule, account, recipient)
	if (
		not isinstance(result, dict)
		or set(result) != {"effective_recipient", "emulator_contact"}
		or not isinstance(result["effective_recipient"], str)
		or not result["effective_recipient"].strip()
		or len(result["effective_recipient"]) > 140
		or result["effective_recipient"] != result["effective_recipient"].strip()
		or any(ord(c) < 32 or ord(c) == 127 for c in result["effective_recipient"])
	):
		frappe.throw(_("Invalid notification destination policy result."))
	return dict(result, delivery_mode="Required" if required else "Redirected")


def delivery_contract(rule):
	"""Contractual delivery configuration without free-text template wording hashes."""
	if not rule.account:
		return None
	account = frappe.get_doc("Communication Channel Account", rule.account)
	channel = frappe.get_doc("Communication Channel", account.channel)
	definition = provider_definition(channel.provider)
	return definition_hash(
		dict(
			provider=definition,
			parameters=value_fingerprint(rule),
			account=dict(name=account.name, provider=channel.provider),
			channel=dict(name=channel.name),
		)
	)


def delivery_configuration(rule):
	"""Relevant revisions only; hashes never expose configuration values or credentials."""
	if not rule.account:
		return None
	account = frappe.get_doc("Communication Channel Account", rule.account)
	channel = frappe.get_doc("Communication Channel", account.channel)
	definition = provider_definition(channel.provider)
	parameters = value_fingerprint(rule)
	templates = {}
	for row in rule.outputs:
		if row.template_reference and row.send:
			base = frappe.get_doc("Communication Template", row.template_reference, for_update=True)
			templates[row.template_reference] = template_revision(base)

	external = {}
	if "provider_templates" in definition["capabilities"]:
		for reference in templates:
			with presentation_language(
				frappe.db.get_value("Communication Template", reference, "language")
			):
				external[reference] = provider_template_contract(rule.account, reference)
	return definition_hash(
		dict(
			provider=definition,
			external=external,
			parameters=parameters,
			account=dict(name=account.name, revision=str(account.modified)),
			channel=dict(name=channel.name, revision=str(channel.modified)),
			templates=templates,
		)
	)


def delivery_fingerprint(rule, destination):
	return definition_hash(
		dict(rule=validation.fingerprint(rule, include_schedule=False), destination=destination)
	)


@contextmanager
def _lock_template_reads():
	previous = getattr(frappe.flags, "_lock_notification_rule_templates", False)
	frappe.flags._lock_notification_rule_templates = True
	try:
		yield
	finally:
		frappe.flags._lock_notification_rule_templates = previous


@frappe.whitelist(methods=["POST"])
def validate_process(candidate):
	require_system_manager()
	frappe.has_permission("Notification Rule", "write", throw=True)
	lock_document_types("Communication Template", "Notification Rule")
	with _lock_template_reads():
		rule = validation._configuration(candidate, for_update=True, persisted_only=True)
		if rule.doctype != "Notification Rule":
			raise frappe.PermissionError(_("Invalid Process source"))
		if validation.test_state(rule, remember_invalid=True) == "Passed":
			raise frappe.ValidationError(_("This Notification Rule already has a current validation."))
		savepoint = "validate_rule_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(savepoint)
		try:
			result = validation.test_method(rule.as_dict())
			rule.enabled = 1 if result["status"] == "Passed" else 0
			rule.save()
			if result["status"] == "Passed":
				validation.require_approved(rule)
			return dict(
				result,
				lifecycle_status="Enabled" if result["status"] == "Passed" else rule.lifecycle_status,
			)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise


@frappe.whitelist(methods=["POST"])
def set_active(candidate, active):
	require_system_manager()
	frappe.has_permission("Notification Rule", "write", throw=True)
	if type(active) not in (int, bool, str) or active not in (0, 1, "0", "1"):
		raise frappe.ValidationError(_("Invalid rule activation."))
	active = int(active)
	if active:
		lock_document_types("Communication Template", "Notification Rule")
	with _lock_template_reads() if active else nullcontext():
		rule = validation._configuration(candidate, for_update=True, persisted_only=True)
		if rule.doctype != "Notification Rule":
			raise frappe.PermissionError(_("Invalid Process source"))
		if active:
			validate_rule(rule)
			validate_notification_contract(rule)
			validation.require_approved(rule)
			from messaging.registry.processes.discovery import verify_registered_revision
			verify_registered_revision(bind_definition(rule))
		savepoint = "activate_rule_" + frappe.generate_hash(length=8)
		frappe.db.savepoint(savepoint)
		try:
			rule.db_set("enabled", active)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		return dict(lifecycle_status="Enabled" if active else "Disabled")


@frappe.whitelist()
def state(name):
	require_system_manager()
	rule = frappe.get_doc("Notification Rule", name)
	rule.check_permission("read")
	tested = validation.test_state(rule, remember_invalid=True)
	return dict(
		lifecycle_status=("Enabled" if rule.enabled else "Disabled")
		if tested == "Passed"
		else "Unavailable"
		if tested == "Unavailable"
		else "Untested",
		verification_error=rule.flags.process_verification_error,
	)


def existing_default(definition, *, app, lock=False):
	name = definition["key"]
	if not frappe.db.exists("Notification Rule", name):
		return None
	doc = frappe.get_doc("Notification Rule", name, for_update=lock)
	if doc.application != app:
		raise frappe.PermissionError(
			_("The preset identity belongs to a manual document or another application.")
		)
	if doc.process_definition != definition["process"]:
		frappe.throw(_("The rule identifier belongs to a different Process."))
	return doc


def _registered_projection(doc):
    fields = ('rule_name', 'process_definition', 'source_application', 'source_doctype', 'notification_delay_minutes')
    values = {field: doc.get(field) for field in fields}
    for table, columns in (
        ('events', ('document_event', 'field_name', 'value')),
        ('process_parameters', ('parameter_name', 'value_source', 'fixed_value', 'source_field', 'internal_type', 'required', 'multiple')),
        ('outputs', ('status_code', 'output_mode', 'send', 'template_reference')),
    ):
        values[table] = [{column: row.get(column) for column in columns} for row in doc.get(table) or []]
    return values


def save_rule(*, key, name, process, source_application, source_doctype, events, outcomes,
        notification_delay_minutes=60, inputs=None):
    """Create or synchronize one Notification Rule from explicit Code-First arguments."""
    import inspect
    import json
    from copy import deepcopy
    from hashlib import sha256
    from messaging.security.permissions import installed_applications, require_registration_owner
    from messaging.communication.doctype.notification_rule.contract import validate_default_contract
    from messaging.communication.doctype.process_definition.process_definition import resolve_definition, registered_contract
    frame = inspect.currentframe()
    try:
        caller = frame.f_back
        module = caller.f_globals.get('__name__', '')
        matches = [item for item in installed_applications() if module == item or module.startswith(item + '.')]
        app = max(matches, key=len) if matches else None
        require_registration_owner(app, caller)
    finally:
        del frame
    inputs = {} if inputs is None else inputs
    outcomes = deepcopy(outcomes)
    events = [dict(event) for event in events]
    definition = resolve_definition(process, expected_type='notification')
    definition.check_permission('read')
    if definition.application != app:
        frappe.throw(_('The Process must belong to the preset application.'))
    contract = registered_contract(definition)
    technical_outcomes = deepcopy(outcomes)
    for output in technical_outcomes.values():
        output.pop('template', None)
    validate_default_contract(dict(inputs=inputs, outcomes=technical_outcomes, options=dict(
        source_application=source_application, source_doctype=source_doctype, events=events,
        notification_delay_minutes=notification_delay_minutes)), contract)
    with document_savepoint():
        lock_document_types('Notification Rule')
        doc = existing_default(dict(key=key, process=process), app=app, lock=True)
        new = doc is None
        candidate = frappe.get_doc(dict(doctype='Notification Rule', __islocal=1, rule_name=name,
            process_definition=process, enabled=0, source_application=source_application,
            source_doctype=source_doctype, events=events,
            notification_delay_minutes=notification_delay_minutes))
        declared = {item['name'] for item in contract['parameters']}
        for parameter_name, specification in inputs.items():
            if parameter_name not in declared:
                candidate.append('process_parameters', dict(parameter_name=parameter_name, **specification))
        synchronize(candidate, contract=contract)
        for row in candidate.process_parameters:
            if row.parameter_name in inputs:
                row.update(inputs[row.parameter_name])
        for row in candidate.outputs:
            output = outcomes[row.status_code]
            template_ref = output.get('template')
            if template_ref and not frappe.db.exists('Communication Template', template_ref):
                registered = registered_template_definition(template_ref)
                if registered:
                    expected_fields = {
                        field_name: kind
                        for field_name, kind in contract['outcomes'][row.status_code]['fields'].items()
                        if field_name != 'recipient'
                    }
                    for var_name in registered['variables']:
                        if var_name not in expected_fields:
                            frappe.throw(
                                _('Template field {0} must be a declared string field of this output.').format(var_name)
                            )
                    row.send = 0
                    row.template_reference = None
                    continue
            row.send = int(output['send'])
            row.template_reference = template_ref
        candidate.before_validate()
        desired = _registered_projection(candidate)
        digest = sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
        if doc:
            current = _registered_projection(doc)
            baseline = json.loads(doc.registered_snapshot) if doc.registered_snapshot else desired
            if current != baseline:
                return dict(status='administrative_changes', name=doc.name)
            if doc.registered_hash == digest:
                return dict(status='existing', name=doc.name)
            for field in ('rule_name', 'notification_delay_minutes', 'events', 'process_parameters', 'outputs'):
                value = candidate.get(field)
                doc.set(field, [row.as_dict() for row in value] if isinstance(value, list) else value)
            if current != desired:
                doc.enabled = 0
        else:
            doc = candidate
            doc.application = app
        doc.registered_hash = digest
        doc.registered_snapshot = json.dumps(desired, sort_keys=True)
        with _preset_write():
            doc.insert(set_name=key) if new else doc.save()
        return dict(status='created' if new else 'updated', name=doc.name)
