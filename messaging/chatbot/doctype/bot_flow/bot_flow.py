import json
import keyword
import re
from typing import Any

import frappe
from frappe import N_, _
from frappe.model.document import Document

from messaging.chatbot.doctype.bot_flow.contract import (
	default_contract,
	synchronize as synchronize_contract,
	validate_bindings,
	validate_process_assignment,
)
from messaging.chatbot.doctype.bot_flow import localization as flow_messages
from messaging.chatbot.doctype.bot_flow.localization import (
	missing_default_language,
	output as localized_output,
	text,
	validate as validate_translations,
)
from messaging.chatbot.doctype.bot_flow_state.bot_flow_state import (
	remove as remove_flow_state,
	synchronize as synchronize_flow_state,
)
from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	get_request_phrase,
	provision_preset_parameter,
	validate_input_limits,
	validate_input_value,
)
from messaging.chatbot.doctype.bot_settings import language as bot_language
from messaging.chatbot.doctype.bot_settings import localization as settings_localization
from messaging.chatbot.doctype.bot_settings.language import (
	get_bot_settings,
	presentation_language,
	validate_chatbot_languages,
)
from messaging.chatbot.doctype.bot_settings.localization import (
	DEFAULT_SAFE_INTERNAL_ERROR_MESSAGE,
	UNSAFE_ERROR_MESSAGE_PATTERNS,
	validate_safe_error_message,
)
from messaging.common.locking import document_savepoint, lock_document_types, shared_document
from messaging.common.write_guards import InternalWriteGuard
from messaging.communication.doctype.communication_template.communication_template import (
	 SIMPLE_VARIABLE_PATTERN,
	TEMPLATE_TOKEN_PATTERN,
	create_preset_template,
	resolve_output,
	template_contract,
	validate_template,
	validate_template_syntax,
)
from messaging.communication.doctype.process_definition import validation
from messaging.registrations.processes import BotProcess, STATUS_CODE_PATTERN, SUPPORTED_PROCESS_SCHEMA_VERSION, validate_captured_process_argument, validate_output_data, validate_process_schema_v1
from messaging.communication.doctype.process_definition.process_definition import (
    bind_definition,
    resolve_definition,
)
from messaging.security.permissions import require_system_manager

_lifecycle_write = InternalWriteGuard("messaging_bot_flow_lifecycle")

VALID_INVOCATION_MODES = ("Internal Call (frappe.call)",)
VALID_OUTPUT_MODES = ("Template", "Message")


def invalidate_process_approval(definition, method=None):
	previous = definition.get_doc_before_save()
	if not previous or (previous.capability_revision, previous.status) == (definition.capability_revision, definition.status):
		return
	for name in frappe.get_all('Bot Flow', filters={'process_definition': definition.name}, pluck='name'):
		flow = frappe.get_doc('Bot Flow', name, for_update=True)
		validation.clear_approval(flow)
		frappe.db.set_value('Bot Flow', name,
			dict(status='Disabled', **{key: flow.get(key) for key in validation.RECEIPT_FIELDS}))

CANONICAL_WORKFLOW_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
PYTHON_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")


class ProcessContractViolationError(frappe.ValidationError):
	"""Raised when a Process response fails Flow Output resolution rules."""


_preset_write = InternalWriteGuard("messaging_bot_flow_provenance")


class BotFlow(Document):
	def _protect_application(self):
		if not _preset_write.active():
			previous = None if self.is_new() else frappe.get_doc('Bot Flow', self.name)
			for field in ('preset', 'preset_versions'):
				if (self.get(field) or '') != ((previous.get(field) if previous else '') or ''):
					raise frappe.PermissionError(_('Applied preset versions are managed by Bot Settings.'))

		stored = frappe.db.get_value(self.doctype, self.name, "application") if not self.is_new() else None
		if (stored or "") != (self.application or ""):
			if not self.is_new() or not _preset_write.active():
				raise frappe.PermissionError(
					_("Application provenance is managed by Code-First registration.")
				)
		if self.is_new() and self.application and self.application not in frappe.get_installed_apps():
			raise frappe.PermissionError(_("Registration owner must be an installed application."))

	def onload(self):
		# The stored Status preserves activation; Desk shows current effective readiness.
		self.status = self.lifecycle_status
		self.set_onload(
			"flow_lifecycle",
			dict(lifecycle_status=self.status, verification_error=self.flags.process_verification_error),
		)

	def test_state(self, *, remember_invalid=False):
		return validation.test_state(self, remember_invalid=remember_invalid)

	def require_approved(self):
		return validation.require_approved(self)

	def runtime_evidence(self, definition, *, administrative_test=False):
		return validation.runtime_evidence(self, definition, administrative_test=administrative_test)

	def resolve_output(self, raw_response):
		return resolve_flow_output(self, raw_response)

	def validate_runtime_arguments(self, arguments):
		return validate_runtime_arguments(self, arguments)

	def _allows_receipt_promotion(self):
		return _lifecycle_write.active()

	def _validate_process_candidate(self):
		self.normalize_configuration()
		self.validate_configuration()
		self._validate_outputs_configuration(require_templates=True)
		if not self.process_method:
			frappe.throw(_("Process Method Path is required."))
		if not self.outputs:
			frappe.throw(_("At least one functional Output must be configured for the Flow."))

	def _consumer_fingerprint(self, *, include_schedule=True):
		settings = get_bot_settings()
		config = dict(validation._fields(self, validation.FLOW_FIELDS))
		config["process_contract"] = self.get("process_contract")
		config["template_bindings"] = sorted(
			[
				validation._fields(row, ("language", "status_code", "template_reference"))
				for row in self.get("output_translations") or []
				if any(
					output.status_code == row.status_code and output.output_mode == "Template"
					for output in self.outputs
				)
			],
			key=lambda row: (row["status_code"], row["language"]),
		)
		config["parameters"] = [
			dict(
				validation._fields(
					row,
					(
						"parameter",
						"process_argument",
						"required",
						"allow_multiple_values",
						"minimum_values",
						"maximum_values",
						"capture_order",
					),
				),
				definition=validation._fields(
					frappe.get_doc("Bot Parameter", row.parameter),
					tuple(
						name
						for name in validation.PARAMETER_FIELDS
						if name not in ("label", "description")
					)
					+ ("allow_contextual_name_extraction", "allow_ai_extraction"),
				),
			)
			for row in self.parameters
		]
		config["outputs"] = [
			validation._fields(
				row,
				(
					"status_code",
					"output_mode",
					"require_non_empty_process_message",
					"flow_action",
					"retry_parameter",
				),
			)
			for row in self.outputs
		]
		config["templates"] = {
			row.template_reference: template_contract(
				shared_document("Bot Flow Template", row.template_reference)
			)
			for row in self.get("output_translations") or []
			if row.get("template_reference")
			and any(
				output.status_code == row.status_code and output.output_mode == "Template"
				for output in self.outputs
			)
		}
		config["transitions"] = validation._fields(
			settings, ("max_invalid_attempts", "show_menu_on_attempts_exhausted")
		)
		config["languages"] = sorted(
			(r.language, int(r.is_default)) for r in settings.chatbot_languages if r.enabled
		)
		return config

	@property
	def lifecycle_status(self):
		tested = self.test_state(remember_invalid=True)
		if tested == "Unavailable":
			return "Unavailable"
		if tested != "Passed":
			return "Untested"
		return "Enabled" if self.status == "Enabled" else "Disabled"

	def _protect_activation(self):
		if _lifecycle_write.active():
			return
		previous = self.get_doc_before_save()
		activation = previous.status if previous else "Disabled"
		if self.status in ("Untested", "Unavailable"):
			self.status = activation
		if self.status != activation:
			raise frappe.PermissionError(_("Use Activate or Deactivate to change the Flow state."))

	def before_validate(self) -> None:
		self._protect_application()
		self._protect_activation()
		self.normalize_configuration()

	def normalize_configuration(self) -> None:
		if self.workflow_code:
			self.workflow_code = self.workflow_code.strip()
		if self.workflow_name:
			self.workflow_name = self.workflow_name.strip()
		if self.process_method:
			self.process_method = self.process_method.strip()
		if self.process_schema_version is None:
			self.process_schema_version = SUPPORTED_PROCESS_SCHEMA_VERSION

		self.has_parameters = 1 if len(self.parameters or []) > 0 else 0
		priority_val = int(self.priority if self.priority is not None else 0)
		self.positive_priority_key = str(priority_val) if priority_val > 0 else None

	def validate(self) -> None:
		self._protect_application()
		self.validate_configuration()
		validation.validate_readiness(self, enabled=self.status == "Enabled")

	def validate_configuration(self) -> None:
		settings = get_bot_settings(shared=True)
		if not validate_chatbot_languages(settings):
			frappe.throw(_("Configure an enabled default language in Bot Settings before creating a Bot Flow."))
		synchronize_contract(self)
		flow_messages.require_default_language(self, settings=settings)
		self._validate_identity_and_semantics()
		param_docs = self._validate_parameters_table()
		validate_bindings(self)
		self._validate_priority_rules(has_params=bool(param_docs))
		self._validate_input_configuration(param_docs=param_docs)
		self._validate_process_configuration()
		self._validate_outputs_configuration()
		self._validate_internal_error_configuration()
		validate_translations(self)
		settings.validate_registration_flow(self)

	def on_update(self):
		synchronize_flow_state(self)

	def on_trash(self):
		remove_flow_state(self)

	def db_insert(self, *args: Any, **kwargs: Any) -> Any:
		try:
			return super().db_insert(*args, **kwargs)
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError) as exc:
			self._handle_unique_constraint_error(exc)
			raise

	def db_update(self, *args: Any, **kwargs: Any) -> Any:
		try:
			return super().db_update(*args, **kwargs)
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError) as exc:
			self._handle_unique_constraint_error(exc)
			raise

	def _handle_unique_constraint_error(self, exc: Exception) -> None:
		message = str(exc)
		if "process_definition" in message:
			try:
				validate_process_assignment(self, current_read=True)
			except frappe.QueryDeadlockError:
				# A competing insert can invalidate MariaDB's locking read after 1062.
				pass
			raise frappe.ValidationError(
				_(
					'Process definition "{0}" is already assigned to another Bot Flow. Reload the document.'
				).format(self.process_definition)
			)
		if "positive_priority_key" in message:
			frappe.throw(
				_("Another Flow already uses positive priority {0}.").format(self.priority),
				frappe.ValidationError,
			)

	def _validate_identity_and_semantics(self) -> None:
		code = (self.workflow_code or "").strip()
		if not code:
			frappe.throw(_("Flow Code is required."), frappe.ValidationError)
		if not CANONICAL_WORKFLOW_CODE_PATTERN.match(code):
			frappe.throw(
				_(
					"Flow Code '{0}' must start with a lowercase letter and contain only lowercase letters, digits, or underscores (2-64 chars)."
				).format(code),
				frappe.ValidationError,
			)
		if keyword.iskeyword(code):
			frappe.throw(
				_("Flow Code '{0}' cannot be a reserved Python keyword.").format(code),
				frappe.ValidationError,
			)

		if not (self.workflow_name or "").strip():
			frappe.throw(_("Flow Name is required."), frappe.ValidationError)

		language = validate_chatbot_languages(get_bot_settings())
		if self.show_in_menu and not text(self, "menu_label", language).strip():
			frappe.throw(
				_(
					"Menu Label is required in the default enabled language when Show in Menu / Catalog is enabled."
				),
				frappe.ValidationError,
			)
		if language and not text(self, "semantic_description", language).strip():
			frappe.throw(_("Semantic Description is required."), frappe.ValidationError)

	def _validate_parameters_table(self) -> list[Document]:
		rows = self.parameters or []
		seen_parameters: set[str] = set()
		seen_orders: set[int] = set()
		seen_arguments: set[str] = set()
		param_docs: list[Document] = []

		for idx, row in enumerate(rows, start=1):
			validate_input_limits(row)
			param_code = (row.parameter or "").strip()
			if not param_code:
				frappe.throw(
					_("Row {0} in Parameters must reference a Bot Parameter.").format(idx),
					frappe.ValidationError,
				)
			if param_code in seen_parameters:
				frappe.throw(
					_(
						"Parameter '{0}' is duplicated in Flow '{1}'. Reuse the single Parameter reference without duplicating rows."
					).format(param_code, self.workflow_code),
					frappe.ValidationError,
				)
			seen_parameters.add(param_code)

			if not frappe.db.exists("Bot Parameter", param_code):
				frappe.throw(
					_("Referenced Parameter '{0}' does not exist.").format(param_code),
					frappe.ValidationError,
				)

			param_doc = frappe.get_doc("Bot Parameter", param_code)
			if not int(param_doc.enabled or 0):
				frappe.throw(
					_("Referenced Parameter '{0}' is disabled and cannot be used in a Flow.").format(
						param_code
					),
					frappe.ValidationError,
				)
			get_request_phrase(param_doc)
			param_docs.append(param_doc)

			order_val = int(row.capture_order if row.capture_order is not None else idx)
			if order_val <= 0:
				frappe.throw(
					_("Capture Order in row {0} must be a positive integer.").format(idx),
					frappe.ValidationError,
				)
			if order_val in seen_orders:
				frappe.throw(
					_("Duplicate Capture Order {0} in Flow Parameters.").format(order_val),
					frappe.ValidationError,
				)
			seen_orders.add(order_val)
			row.capture_order = order_val

			arg_name = (row.process_argument or param_code).strip()
			validate_captured_process_argument(arg_name)
			if not PYTHON_IDENTIFIER_PATTERN.match(arg_name) or keyword.iskeyword(arg_name):
				frappe.throw(
					_(
						"Process Argument '{0}' in row {1} must be a valid Python argument identifier (not starting with '_' and not a keyword)."
					).format(arg_name, idx),
					frappe.ValidationError,
				)
			if arg_name in seen_arguments:
				frappe.throw(
					_("Duplicate Process Argument '{0}' in Flow Parameters.").format(arg_name),
					frappe.ValidationError,
				)
			seen_arguments.add(arg_name)
			row.process_argument = arg_name

		return param_docs

	def _validate_priority_rules(self, *, has_params: bool) -> None:
		if self.priority is None or isinstance(self.priority, bool):
			frappe.throw(_("Priority must be an integer."), frappe.ValidationError)

		try:
			priority_val = int(self.priority)
		except (TypeError, ValueError):
			frappe.throw(_("Priority must be an integer."), frappe.ValidationError)

		if priority_val < 0:
			frappe.throw(_("Flow priority cannot be negative."), frappe.ValidationError)

		if not has_params and priority_val != 0:
			frappe.throw(
				_("A Flow without Parameters must have priority 0 (got {0}).").format(priority_val),
				frappe.ValidationError,
			)

		if has_params and priority_val <= 0:
			frappe.throw(
				_("A Flow with Parameters must have a positive priority (> 0)."),
				frappe.ValidationError,
			)

		self.priority = priority_val
		self.positive_priority_key = str(priority_val) if priority_val > 0 else None

		if priority_val > 0 and frappe.db.table_exists("Bot Flow"):
			existing = frappe.db.sql(
				"""
				SELECT name
				FROM `tabBot Flow`
				WHERE positive_priority_key = %s AND name != %s
				FOR UPDATE
				""",
				(self.positive_priority_key, self.name or ""),
				as_dict=True,
			)
			if existing:
				frappe.throw(
					_(
						"Positive priority {0} is already assigned to Flow '{1}'. Positive priorities must be unique."
					).format(priority_val, existing[0]["name"]),
					frappe.ValidationError,
				)

	def _validate_input_configuration(self, *, param_docs: list[Document]) -> None:
		language = validate_chatbot_languages(get_bot_settings())
		initial_msg = text(self, "input_initial_message", language).strip()
		continuation_msg = text(self, "input_continuation_message", language).strip()

		if not param_docs:
			return

		if not initial_msg:
			frappe.throw(
				_("Initial Input Message is required when the Flow has Parameters."),
				frappe.ValidationError,
			)

		if not continuation_msg:
			frappe.throw(
				_("Continuation Input Message is required when the Flow has Parameters."),
				frappe.ValidationError,
			)

	def _validate_process_configuration(self) -> None:
		bind_definition(self)

		if self.process_invocation_mode not in VALID_INVOCATION_MODES:
			frappe.throw(
				_("Process Invocation Mode must be one of: {0}.").format(
					", ".join(VALID_INVOCATION_MODES)
				),
				frappe.ValidationError,
			)

		if int(self.process_schema_version or 0) != SUPPORTED_PROCESS_SCHEMA_VERSION:
			frappe.throw(
				_("Unsupported Process Schema Version '{0}'. Only version {1} is supported.").format(
					self.process_schema_version, SUPPORTED_PROCESS_SCHEMA_VERSION
				),
				frappe.ValidationError,
			)

	def _validate_outputs_configuration(self, *, require_templates=False) -> None:
		rows = self.outputs or []
		if not rows and self.status != "Enabled":
			return
		if not rows:
			frappe.throw(
				_("At least one functional Output must be configured for the Flow."),
				frappe.ValidationError,
			)

		seen_statuses: set[str] = set()
		for idx, row in enumerate(rows, start=1):
			status_code = (row.status_code or "").strip()
			if not status_code:
				frappe.throw(
					_("Output row {0} must define a non-empty Functional Status.").format(idx),
					frappe.ValidationError,
				)
			if not STATUS_CODE_PATTERN.match(status_code):
				frappe.throw(
					_(
						"Functional Status '{0}' in Output row {1} contains invalid characters."
					).format(status_code, idx),
					frappe.ValidationError,
				)
			normalized_status = status_code.lower()
			if normalized_status == "internal_error":
				frappe.throw(
					_(
						"'internal_error' is not a functional status returned by a Process. Configure the Internal Error section instead."
					),
					frappe.ValidationError,
				)
			if normalized_status in seen_statuses:
				frappe.throw(
					_("Duplicate Output for functional status '{0}' in Flow '{1}'.").format(
						status_code, self.workflow_code
					),
					frappe.ValidationError,
				)
			seen_statuses.add(normalized_status)
			row.status_code = status_code
			row.flow_action = row.flow_action or "Complete Flow"
			validate_output_action(row, self)

			if row.output_mode not in VALID_OUTPUT_MODES:
				frappe.throw(_("Select Message or Template Output."))
			if row.output_mode == "Template":
				language = validate_chatbot_languages(get_bot_settings())
				if (
					self.status != "Enabled"
					and not require_templates
					and not any(
						translation.status_code == status_code and translation.template_reference
						for translation in self.output_translations or []
					)
				):
					row.require_non_empty_process_message = 0
					continue
				reference = localized_output(self, row, language)["template_reference"]
				if not reference:
					frappe.throw(
						_(
							"Select a Bot Flow Template; embedded Output content is not supported."
						)
					)
				template = frappe.get_doc("Bot Flow Template", reference)
				validate_template(template)
				if not template.enabled:
					frappe.throw(_("The bot flow template is disabled."))
				row.require_non_empty_process_message = 0
			else:
				row.require_non_empty_process_message = 1

	def _validate_internal_error_configuration(self) -> None:
		if int(self.use_custom_internal_error_message or 0):
			validate_safe_error_message(
				text(
					self,
					"internal_error_message",
					validate_chatbot_languages(get_bot_settings()),
				),
				field_label=_("Custom Internal Error Message"),
			)
		for row in self.translations or []:
			if (row.internal_error_message or "").strip():
				validate_safe_error_message(
					row.internal_error_message, field_label=_("Custom Internal Error Message")
				)

	def get_process_argument_mapping(self) -> dict[str, str]:
		"""Return canonical Parameter Code -> Process function argument name mapping."""
		return {
			row.parameter: (row.process_argument or row.parameter).strip()
			for row in sorted(self.parameters or [], key=lambda r: int(r.capture_order or 0))
		}


def process_arguments(flow, values):
	"""Validate Flow values for both administrative tests and conversation execution."""
	if not isinstance(values, dict):
		raise frappe.ValidationError(_("Invalid Process argument"))
	if set(values) - {row.parameter for row in flow.parameters}:
		raise frappe.ValidationError(_("Unknown Process argument"))
	for key in values:
		validate_captured_process_argument(key)
	arguments = {}
	for row in flow.parameters:
		validate_captured_process_argument((row.process_argument or row.parameter).strip())
		if row.parameter not in values:
			if row.required:
				raise frappe.ValidationError(_("Required Process argument is missing"))
			continue
		parameter = frappe.get_doc("Bot Parameter", row.parameter)
		valid, value = validate_input_value(row, parameter, values[row.parameter])
		if not parameter.enabled or not valid:
			raise frappe.ValidationError(_("Invalid Process argument"))
		arguments[row.process_argument or row.parameter] = value
	return arguments


def validate_runtime_arguments(flow, arguments):
	"""Direct executor calls must satisfy the persisted Input contract too."""
	mapping = {(row.process_argument or row.parameter): row.parameter for row in flow.parameters}
	if set(arguments) - set(mapping):
		raise frappe.ValidationError(_("Unknown Process argument"))
	return process_arguments(flow, {mapping[key]: value for key, value in arguments.items()})


def get_flow_effective_internal_error_message(flow: Document | str) -> str:
	"""Return the Flow's custom internal error message or inherit the global default from Bot Settings."""
	doc = frappe.get_doc("Bot Flow", flow) if isinstance(flow, str) else flow
	if int(doc.use_custom_internal_error_message or 0):
		try:
			message = flow_messages.text(doc, "internal_error_message")
		except frappe.ValidationError:
			return settings_localization.get_internal_error_message()
		if message.strip():
			return message

	return settings_localization.get_internal_error_message()


def validate_output_action(row, flow):
	if row.flow_action not in ("Complete Flow", "Cancel Flow", "Retry Parameter"):
		frappe.throw(_("Invalid Flow Action."), frappe.ValidationError)
	if row.flow_action == "Retry Parameter":
		if not row.retry_parameter or row.retry_parameter not in {
			p.parameter for p in flow.parameters
		}:
			frappe.throw(_("Parameter to Retry must belong to this Flow."), frappe.ValidationError)
	else:
		row.retry_parameter = None


def resolve_flow_output(
	flow: Document | str,
	raw_response: Any,
) -> dict[str, Any]:
	"""Validate a Process response and resolve the corresponding Flow Output text."""
	doc = frappe.get_doc("Bot Flow", flow) if isinstance(flow, str) else flow
	validated = validate_process_schema_v1(raw_response)
	status = validated["status"]

	matched_output = None
	for row in doc.outputs or []:
		if (row.status_code or "").strip().lower() == status.lower():
			matched_output = row
			break

	if not matched_output:
		raise ProcessContractViolationError(
			_("Flow '{0}' has no Output configured for functional status '{1}'.").format(
				doc.workflow_code, status
			)
		)

	if doc.get("process_contract"):
		fields = json.loads(doc.process_contract)["outcomes"].get(status)
		if fields is None:
			raise ProcessContractViolationError(_("Process returned an undeclared functional status."))
		validate_output_data(fields["fields"], validated["data"])
	validate_output_action(matched_output, doc)
	action = dict(
		flow_action=matched_output.flow_action, retry_parameter=matched_output.retry_parameter
	)
	mode = matched_output.output_mode
	presentation = localized_output(doc, matched_output)
	with presentation_language(presentation["language"]):
		output = resolve_output(
			mode,
			message=validated["message"],
			data=validated["data"],
			template_reference=presentation["template_reference"],
		)
	return dict(
		status=status,
		data=validated["data"],
		output_mode=mode,
		rendered_message=output["text"],
		template_revision=output["revision"],
		template_reference=presentation["template_reference"],
		**action,
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def available_process_definitions(doctype, txt, searchfield, start, page_len, filters):
	require_system_manager()
	frappe.has_permission("Bot Flow", "read", throw=True)
	current = (filters or {}).get("flow")
	if current:
		frappe.get_doc("Bot Flow", current).check_permission("read")
	assigned = frappe.get_all(
		"Bot Flow", filters={"name": ["!=", current or ""]}, pluck="process_definition"
	)
	rows = frappe.get_list(
		"Process Definition",
		filters={
			"process_type": "bot_flow",
			"status": "Enabled",
			"contract_version": BotProcess.contract,
			"application": ["in", frappe.get_installed_apps()],
			"name": ["not in", [name for name in assigned if name] or [""]],
		},
		or_filters={
			"process_id": ["like", "%" + txt + "%"],
			"process_name": ["like", "%" + txt + "%"],
		},
		fields=["name", "process_name", "process_id"],
		order_by="process_name asc",
		limit_start=start,
		limit_page_length=page_len,
	)
	result = []
	for row in rows:
		try:
			resolve_definition(row.name, expected_type="bot_flow")
		except frappe.ValidationError:
			continue
		result.append([row.name, row.process_name, row.process_id])
	return result


@frappe.whitelist(methods=["POST"])
def validate_process(candidate):
	flow = validation._configuration(candidate, for_update=True, persisted_only=True)
	if flow.doctype != "Bot Flow":
		raise frappe.PermissionError(_("Invalid Process source"))
	if validation.test_state(flow, remember_invalid=True) == "Passed":
		raise frappe.ValidationError(_("This Flow already has a current validation."))
	savepoint = "validate_flow_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
		with _lifecycle_write():
			result = validation.test_method(flow.as_dict())
			flow.status = "Enabled" if result["status"] == "Passed" else "Disabled"
			flow.save()
			if result["status"] == "Passed":
				get_bot_settings().require_operational_configuration(flow=flow.name)
				validation.require_approved(flow)
		return dict(
			result,
			lifecycle_status="Enabled" if result["status"] == "Passed" else flow.lifecycle_status,
		)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise


@frappe.whitelist(methods=["POST"])
def set_active(candidate, active):
	if type(active) not in (int, bool, str) or active not in (0, 1, "0", "1"):
		raise frappe.ValidationError(_("Invalid Flow activation."))
	active = int(active)
	flow = validation._configuration(candidate, for_update=True, persisted_only=True)
	if flow.doctype != "Bot Flow":
		raise frappe.PermissionError(_("Invalid Process source"))
	if active:
		flow_messages.require_default_language(flow)
		validation.require_approved(flow)
		from messaging.registry.processes.discovery import verify_registered_revision
		verify_registered_revision(bind_definition(flow))
	savepoint = "activate_flow_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
		with _lifecycle_write():
			# The saved configuration already passed validation. Change only
			# activation after checking its current receipt and availability.
			flow.db_set("status", "Enabled" if active else "Disabled")
			if active:
				get_bot_settings().require_operational_configuration(flow=flow.name)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise
	return dict(lifecycle_status="Enabled" if active else flow.lifecycle_status)


@frappe.whitelist()
def state(name):
	require_system_manager()
	flow = frappe.get_doc("Bot Flow", name)
	flow.check_permission("read")
	tested = validation.test_state(flow, remember_invalid=True)
	return dict(
		lifecycle_status=("Enabled" if flow.status == "Enabled" else "Disabled")
		if tested == "Passed"
		else "Unavailable"
		if tested == "Unavailable"
		else "Untested",
		verification_error=flow.flags.process_verification_error,
	)


@frappe.whitelist(methods=["POST"])
def message_requirements(candidate):
	"""Read-only default-language requirements of the current Flow draft."""
	values = frappe.parse_json(candidate)
	if not isinstance(values, dict) or values.get("doctype") != "Bot Flow":
		raise frappe.PermissionError(_("Invalid message configuration."))
	doc = frappe.get_doc(values)
	doc.check_permission("read")
	return flow_messages.required_text_fields(doc)


def existing_default(definition, *, app, lock=False):
	code = definition["key"].replace(".", "_")
	named = frappe.db.get_value(
		"Bot Flow", code, ["name", "process_definition"], as_dict=True, for_update=lock
	)
	if named and named.process_definition != definition["process"]:
		frappe.throw(_("The Flow identifier belongs to a different Process."))
	name = frappe.db.get_value(
		"Bot Flow", {"process_definition": definition["process"]}, "name", for_update=lock
	)
	doc = frappe.get_doc("Bot Flow", name, for_update=lock) if name else None
	if doc and doc.application != app:
		raise frappe.PermissionError(
			_("The preset identity belongs to a manual document or another application.")
		)
	if doc and doc.get('preset') and doc.preset != definition['key']:
		frappe.throw(_('The Process is already installed from another preset.'))
	return doc


def apply_default(definition, doc, available, discovered, *, app, replace_common=False):
	new = doc is None
	if new:
		doc = frappe.get_doc({
			"doctype": "Bot Flow",
			"__islocal": 1,
			"workflow_code": definition["key"].replace(".", "_"),
			"workflow_name": definition["name"],
			"process_definition": definition["process"],
			"status": "Disabled",
			**(dict(priority=0, show_in_menu=False, menu_order=0, use_custom_internal_error_message=False) | definition.get("options", {})),
		})
	doc.check_permission("create" if new else "write")
	before = doc.as_dict()
	bindings = {}
	parameter_changes = False
	for name, spec in definition.get("inputs", {}).items():
		# Existing Flow bindings remain authoritative, even if administratively changed.
		current = next((row for row in doc.parameters if row.process_argument == name), None)
		if not new and not replace_common and not current:
			continue
		planned = spec["parameter"]
		planned_name = planned["code"] if isinstance(planned, dict) else planned
		if not new and not replace_common and current.parameter != planned_name:
			continue
		bound, changed = provision_preset_parameter(planned, available)
		parameter_changes |= changed
		bindings[name] = bound.name
		if new or replace_common:
			declaration = next(
				(item for item in discovered["parameters"] if item["name"] == name), None
			)
			if declaration is None:
				declaration = dict(
					required=spec.get("required", True),
					multiple=spec.get("allow_multiple_values", False),
				)
			doc.append(
				"parameters",
				dict(
					parameter=bound.name,
					process_argument=name,
					required=int(declaration["required"]),
					allow_multiple_values=int(declaration["multiple"]),
					capture_order=spec.get("capture_order", len(doc.parameters) + 1),
					minimum_values=spec.get("minimum_values", 0),
					maximum_values=spec.get("maximum_values", 0),
				),
			)
	synchronize_contract(doc, contract=discovered)
	if new or replace_common:
		for row in doc.outputs:
			output = definition["outcomes"][row.status_code]
			row.output_mode = output["mode"].title()
			if output.get("retry_parameter"):
				row.retry_parameter = bindings[output["retry_parameter"]]
	references = {}
	for code in available:
		text_values = definition["languages"][code]
		if not any(row.language == code for row in doc.translations):
			doc.append(
				"translations",
				dict(
					language=code,
					**{
						key: value
						for key, value in text_values.items()
						if not key.startswith("input_")
					},
				),
			)
		if doc.parameters and not any(row.language == code for row in doc.input_translations):
			doc.append(
				"input_translations",
				dict(
					language=code,
					**{
						key: value
						for key, value in text_values.items()
						if key.startswith("input_")
					},
				),
			)
		for row in doc.outputs:
			output = definition["outcomes"].get(row.status_code, {})
			if (
				row.output_mode != "Template"
				or not output.get("template")
				or any(
					r.language == code and r.status_code == row.status_code
					for r in doc.output_translations
				)
			):
				continue
			identity = (output["template"], code)
			if identity not in references:
				references[identity] = create_preset_template(
					definition["templates"][output["template"]], code, usage="Bot Flow"
				)
			reference = references[identity]
			doc.append(
				"output_translations",
				dict(language=code, status_code=row.status_code, template_reference=reference),
			)
	if new:
		doc.application = app
		with _preset_write():
			doc.insert()
	elif before != doc.as_dict():
		doc.save()
	elif not parameter_changes:
		return "existing", doc.name, []
	return "created" if new else "completed", doc.name, []


def install_preset(preset, *, restore=False, expected_version=None, languages=None):
	"""Install or restore one persisted preset. The client supplies only its identity."""
	from messaging.security.permissions import require_system_manager
	from messaging.common.locking import lock_document_types
	from messaging.chatbot.doctype.bot_settings.language import configured_languages
	from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import configuration, declaration_versions
	from messaging.chatbot.doctype.bot_flow.preset_application import apply_preset
	require_system_manager()
	frappe.get_single("Bot Settings").check_permission("write")
	frappe.has_permission("Bot Flow Presets", "read", throw=True)
	if type(preset) is not str or not preset:
		frappe.throw(_("Select a unique list of presets."))
	active, _default = configured_languages(lock=True)
	if languages is not None and (type(languages) not in (list, tuple) or any(type(code) is not str for code in languages)
			or set(languages) != set(active)):
		frappe.throw(_("Every enabled language requires complete compatible preset content."))
	lock_document_types("Bot Flow Presets", "Bot Flow", "Bot Parameter", "Bot Flow Template")
	doc = frappe.get_doc("Bot Flow Presets", preset, for_update=True)
	doc.check_permission("read")
	if expected_version is not None and expected_version != declaration_versions(doc):
		frappe.throw(_("Reload the preset catalogue before applying changes."))
	definition = configuration(doc, languages=active)
	return apply_preset(doc, definition, active, reset=bool(restore))


from messaging.chatbot.doctype.bot_flow.configuration import validate_preset_configuration
