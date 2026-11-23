"""Authorized Code-First execution and transaction boundaries."""

import hashlib
import inspect
from dataclasses import dataclass, replace
import json
import time
from contextlib import nullcontext

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_settings import language as bot_language
from messaging.chatbot.doctype.bot_settings.localization import get_internal_error_message
from messaging.common.primitives import _freeze, decimal_json, identity_key
from messaging.registry.processes import BotRequest, NotifyNoResponse, NotifyRequest, ProcessContactReference, ProcessReference, ProcessSchemaValidationError, validate_captured_process_argument, validate_process_schema_v1
from messaging.communication.doctype.process_definition.process_definition import (
    DOTTED_METHOD_PATH_PATTERN,
    FORBIDDEN_PROCESS_PREFIXES,
    FORBIDDEN_PROCESS_ROOT_MODULES,
    _decimal_values,
    _reading,
    _require_write_execution,
    _testing,
    bind_definition,
    deadline_scope,
    effect_boundary,
    is_process_test,
    process_messages,
    process_profile_value,
    read_boundary,
    validate_process_method_path,
)
from messaging.communication.doctype.process_execution_log.process_execution_log import (
	record_process_execution_log,
)
from messaging.security.execution_context import (
	authorized_process,
	is_automation,
	require_automation_process,
)
from messaging.security.permissions import (
	is_system_manager,
	require_system_manager,
	verify_loaded_method,
)



from messaging.registry.processes.context import _ProcessContext


def read_reference(doctype, name):
    """Explicit document read with native permission and field checks."""
    doc = frappe.get_doc(doctype, name)
    doc.check_permission('read')
    doc.apply_fieldlevel_read_permissions()
    return doc


PROCESS_TIMEOUT_SECONDS = 30

# This fixed operation is reserved for the native human availability Process.
# Receipts outlive a reset and expire independently; the period never slides.
HUMAN_REQUEST_TTL = 86400
HUMAN_REQUEST_INCREMENT = """
local generation = redis.call('HGET', KEYS[1], 'generation')
local receipt = redis.call('GET', KEYS[2])
if receipt then
    if receipt == generation then
        return tonumber(redis.call('HGET', KEYS[1], 'count'))
    end
    return 0
end
if not generation then
    generation = ARGV[1]
    redis.call('HSET', KEYS[1], 'generation', generation, 'count', 0)
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
local count = redis.call('HINCRBY', KEYS[1], 'count', 1)
redis.call('SET', KEYS[2], generation, 'EX', ARGV[2])
return count
"""
HUMAN_REQUEST_RESET = """
local receipt = redis.call('GET', KEYS[2])
if receipt and receipt == redis.call('HGET', KEYS[1], 'generation') then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _human_request_keys(contact, message):
    """Hash every identity before passing site-scoped keys to Frappe Redis."""
    digest = identity_key(frappe.local.site, 'messaging.human_availability', contact)
    base = 'messaging:human_requests:' + digest
    return (frappe.cache.make_key(base + ':count'),
        frappe.cache.make_key(base + ':message:' + identity_key(message)))


def record_human_request(request):
    """Bounded Redis capability; no caller-supplied keys, commands, TTL or scripts."""
    execution = _require_write_execution()
    if (
        is_process_test()
        or execution['path'] != 'messaging.presets.processes.human_availability.handle'
        or not isinstance(request, BotRequest)
        or not request.contact
        or not request.session
        or not request.message
        or request.contact.doctype != 'Chat Contact'
        or request.session.doctype != 'Chat Session'
        or request.message.doctype != 'Chat Message'
        or not request.contact.name
        or request.session.name != execution['session']
        or not request.message.name
    ):
        raise frappe.PermissionError(_('An authorized operational execution is required.'))
    session = frappe.db.get_value(
        'Chat Session',
        execution['session'],
        ['contact', 'active_flow', 'status', 'dialogue_state'],
        as_dict=True,
    )
    turn = frappe.db.get_value(
        'Chat Turn',
        execution['turn'],
        ['contact', 'session'],
        as_dict=True,
    )
    message = frappe.db.get_value(
        'Chat Message',
        request.message.name,
        ['contact', 'session', 'turn', 'kind'],
        as_dict=True,
    )
    dialogue_state = json.loads(session.dialogue_state or '{}') if session else {}
    if (
        not session
        or session.contact != request.contact.name
        or session.active_flow != execution['source_name']
        or session.status != 'Executing'
        or not isinstance(dialogue_state, dict)
        or not dialogue_state.get('human_attention')
        or not turn
        or turn.contact != session.contact
        or turn.session != execution['session']
        or not message
        or message.contact != session.contact
        or message.session != execution['session']
        or message.turn != execution['turn']
        or message.kind != 'Incoming'
    ):
        raise frappe.PermissionError(_('An authorized operational execution is required.'))
    keys = _human_request_keys(request.contact.name, request.message.name)
    # Suspend only the socket audit for this fixed, already authorized operation.
    # All document, SQL, callback, deadline and transaction guards remain active.
    token = _reading.set(False)
    try:
        return int(frappe.cache.eval(HUMAN_REQUEST_INCREMENT, 2, *keys,
            identity_key(request.message.name), HUMAN_REQUEST_TTL))
    finally:
        _reading.reset(token)


def _reset_human_request_count(contact, message):
    """Transfer coordinator calls only after its successful database commit."""
    return frappe.cache.eval(HUMAN_REQUEST_RESET, 2, *_human_request_keys(contact, message))


def execution_deadline():
	"""Enforce a thread-isolated deadline without mutating process-global SIGALRM state."""
	return deadline_scope(PROCESS_TIMEOUT_SECONDS)


@process_messages()
def execute_process(
	*,
	process_method,
	arguments,
	source_type,
	source_name,
	workflow=None,
	session=None,
	turn=None,
	execution_id=None,
	invocation_mode="Internal Call (frappe.call)",
	prepare=None,
	render_result=None,
	public_error=None,
	log_context=None,
	administrative_test=False,
	candidate=None,
):
	"""Internal shared boundary: authorization, deadline, Schema v1 and exactly one execution log.

	Only trusted consumers call this function. It is deliberately not whitelisted;
	Operational calls use persisted sources. Only administrative tests accept a candidate.
	"""
	started = time.monotonic()
	raw = None
	called = False
	phase = "authorization"
	runtime_evidence = {}
	savepoint = "process_" + frappe.generate_hash(length=12)
	frappe.db.savepoint(savepoint)
	presentation_scope = None
	try:
		if type(administrative_test) is not bool:
			raise frappe.ValidationError(_("Process test mode must be a boolean."))
		if administrative_test:
			require_system_manager()
		if candidate is not None and not administrative_test:
			raise frappe.PermissionError(_("Invalid Process source"))
		arguments = dict(arguments)
		definition = None
		if source_type not in ("Bot Flow", "Notification Rule") or not source_name:
			raise frappe.ValidationError(_("Invalid Process source"))
		if candidate is not None:
			expected_type = source_type
			if candidate.doctype != expected_type:
				raise frappe.PermissionError(_("Invalid Process source"))
			candidate.check_permission("create" if candidate.is_new() else "write")
		from messaging.communication.doctype.process_definition import approval

		persisted = candidate or (frappe.get_doc(source_type, source_name) if administrative_test
			else approval.lock_execution(source_type, source_name))
		if source_type == "Bot Flow":
			workflow = persisted
			if workflow.status != "Enabled" and not administrative_test:
				raise frappe.PermissionError(_("Process authorization is unavailable"))
			if not workflow.is_new():
				workflow.check_permission("read")
			if render_result is None:
				render_result = workflow.resolve_output
			process_method = workflow.process_method
			invocation_mode = workflow.process_invocation_mode
		else:
			configured = persisted
			if source_type == "Notification Rule" and not administrative_test:
				if configured.status != "Enabled" or not (is_automation() or is_system_manager()):
					raise frappe.PermissionError(_("Process authorization is unavailable"))
			if source_type == "Notification Rule" and render_result is None:
				render_result = lambda response: configured._render_process_result(
					response, administrative_test=administrative_test
				)
			if administrative_test and not configured.is_new():
				configured.check_permission("read")
			process_method = configured.process_method
			invocation_mode = configured.process_invocation_mode
		owner = workflow if source_type == "Bot Flow" else configured
		definition = bind_definition(owner)
		runtime_evidence.update(approval.runtime_evidence(owner, definition, administrative_test=administrative_test))
		process_method = definition.method
		invocation_mode = "Internal Call (frappe.call)"
		if not administrative_test:
			approval.require_approved(owner, definition)
		path = validate_process_method_path(process_method)
		if path.split(".")[0] not in frappe.get_installed_apps():
			raise frappe.PermissionError(_("Process application is unavailable"))
		for key in arguments:
			validate_captured_process_argument(key)
		if prepare:
			phase = "arguments"
			prepare(arguments)
		for key in arguments:
			validate_captured_process_argument(key)
		if source_type == "Bot Flow" and not administrative_test:
			phase = "arguments"
			arguments = workflow.validate_runtime_arguments(arguments)
		phase = "method"
		method = frappe.get_attr(path)
		definition_path = validate_process_method_path(definition.definition_method)
		phase = "static_policy"
		from messaging.registry.processes.discovery import (
			verify_registered_revision,
			require_compliant,
			validate_registered_method,
		)

		definition_inspection = require_compliant(definition_path)
		inspection = require_compliant(path)
		verify_loaded_method(frappe.get_attr(definition_path), inspection=definition_inspection)
		verify_loaded_method(method, inspection=inspection)
		phase = "signature"
		validate_registered_method(definition_path, definition.application, process_type=definition.process_type, role='definition')
		validate_registered_method(path, definition.application, process_type=definition.process_type, role='handler')
		phase = "capability_contract"
		verify_registered_revision(definition)

		if (
			len(
				json.dumps(
					arguments, ensure_ascii=False, allow_nan=False, default=decimal_json
				).encode()
			)
			> 65536
		):
			raise frappe.ValidationError(_("Process arguments exceed the technical size limit"))
		phase = "context"
		context = build_context(
			definition,
			owner,
			arguments,
			administrative_test=administrative_test,
			execution=dict(
				source_type=source_type,
				source_name=source_name,
				session=session,
				turn=turn,
				execution_id=execution_id,
			),
		)
		presentation_scope = bot_language.presentation_language(context.language)
		presentation_scope.__enter__()
		if source_type == "Notification Rule" and not administrative_test:
			runtime_evidence.update(
				condition_identity=context.occurrence, source_revision=context._source.get("modified")
			)
		phase = "invocation"
		# Administrative validation only discovers declarations under its own
		# bounded discovery boundary. Business execution retains the worker timer.
		with (
			nullcontext() if administrative_test else execution_deadline()
		), authorized_process(
			path,
			source_type=source_type,
			source_name=source_name,
			session=session,
			turn=turn,
			execution_id=execution_id,
		):
			previous_control = frappe.db._disable_transaction_control
			frappe.db._disable_transaction_control = True
			try:
				phase = "invocation"
				called = True
				test_token = _testing.set(administrative_test)
				try:
					execution = dict(
						source_type=source_type,
						source_name=source_name,
						session=session,
						turn=turn,
						execution_id=execution_id,
					)
					object.__setattr__(context, "_execution", _freeze(execution))
					raw = run_handler(definition, context, execution)
				finally:
					_testing.reset(test_token)
			finally:
				frappe.db._disable_transaction_control = previous_control
		phase = "schema"
		no_result = type(raw) is NotifyNoResponse
		if no_result:
			if source_type != 'Notification Rule':
				raise frappe.ValidationError(_('Only notification Processes may complete without a functional result.'))
			raw.__post_init__()
			validated = None
		else:
			validated = validate_process_schema_v1(raw)
		phase = "response"
		if no_result:
			output = dict(status=None, output_mode='', rendered_message='',
				diagnostic='No notifiable result: ' + raw.reason)
		elif administrative_test and definition:
			output = dict(status="test", output_mode="", rendered_message="")
		else:
			output = (
				render_result(validated)
				if render_result
				else dict(
					status=validated["status"], output_mode="", rendered_message=validated["message"]
				)
			)
		phase = "audit"
		runtime_evidence.update(
			output_template=output.get("template_reference"),
			output_template_revision=output.get("template_revision"),
		)
		log = record_process_execution_log(
			process_method=path,
			workflow=workflow.name if workflow and not workflow.is_new() else None,
			session=session,
			turn=turn,
			source_type=source_type,
			source_name=source_name,
			execution_id=execution_id,
			execution_outcome="No Result" if no_result else "Functional Status",
			functional_status=output["status"],
			output_mode_used=output["output_mode"],
			diagnostic_detail="Static inspection: "
			+ inspection["status"]
			+ "; policy="
			+ inspection["policy"]
			+ "; unresolved="
			+ str(len(inspection["limitations"]))
			+ ("; " + output["diagnostic"] if output.get("diagnostic") else ""),
			**runtime_evidence,
			arguments=arguments,
			raw_response=validated,
			duration_ms=int((time.monotonic() - started) * 1000),
			**(log_context or {}),
		)
		return dict(
			state="Complete",
			status=output["status"],
			text=output["rendered_message"],
			functional=not no_result,
			no_result=no_result,
			execution_log=log.name,
			**(
				{
					"inspection": dict(
						status=inspection["status"],
						limitations=inspection["limitations"],
						functions=len(inspection["functions"]),
					)
				}
				if administrative_test
				else {}
			),
		)
	except Exception as exc:
		frappe.db.rollback(save_point=savepoint)
		invalid_schema = isinstance(exc, ProcessSchemaValidationError)
		log = record_process_execution_log(
			process_method=process_method or _("Unconfigured Process"),
			workflow=workflow.name if workflow and not workflow.is_new() else None,
			session=session,
			turn=turn,
			execution_id=execution_id,
			source_type=source_type,
			source_name=source_name,
			execution_outcome="Invalid Schema Response"
			if invalid_schema
			else "Internal Execution Error",
			output_mode_used="Internal Error Fallback",
			arguments=arguments,
			diagnostic_error_type=type(exc).__name__,
			diagnostic_detail=f"Process execution failed during {phase}; values and exception text were omitted.",
			**runtime_evidence,
			exception=exc,
			duration_ms=int((time.monotonic() - started) * 1000),
			**(log_context or {}),
		)
		# A synchronous local exception with a completed rollback is a terminal
		# failure. Timeouts/connectivity failures can still hide an external result.
		from requests.exceptions import ConnectionError as TransportConnectionError, Timeout

		uncertain = (
			called
			and raw is None
			and isinstance(exc, (TimeoutError, ConnectionError, Timeout, TransportConnectionError))
		)
		return dict(
			state="Unknown" if uncertain else "Failed",
			status=None,
			text=public_error() if public_error else get_internal_error_message(),
			functional=False,
			execution_log=log.name,
			**(
				{
					"diagnostic": phase + ": " + type(exc).__name__,
					"policy_diagnostic": str(exc) if phase == "static_policy" else None,
				}
				if administrative_test is True
				else {}
			),
		)
	finally:
		if presentation_scope is not None:
			presentation_scope.__exit__(None, None, None)


def run_handler(definition, context, execution):
	"""One handler, one delegated callback, unchanged native read/write boundaries."""
	from messaging.registry.processes.discovery import _method
	from messaging.communication.doctype.process_definition.process_definition import registered_snapshot
	from frappe.translate import get_all_translations

	spec = registered_snapshot(definition)['declaration']
	get_all_translations(context.language)
	get_all_translations(frappe.local.lang)
	if context.test:
		return dict(version=1, status='test', recipient='', data={}, message='')
	with read_boundary() as suspend:
		def invoke(function, request):
			if not is_system_manager():
				require_automation_process(definition.method)
			with suspend(), effect_boundary(dict(execution, path=definition.method)):
				return function(request)

		# Construct execution values using the public validators and the registered
		# declaration. Owner declaration functions never run on ordinary messages.
		for item in spec['contract']['parameters']:
			context.params.register(item['name'], type=item['type'], required=item['required'], multiple=item['multiple'])
		if spec['contract'].get('configurable_parameters'):
			context.params.register_configurable()
		for status, output in spec['contract']['outcomes'].items():
			context.outcomes.register(status, fields=output['fields'], mode=output['mode'],
				action=output.get('action'), configurable=output.get('configurable', False))
		context._bind_handler(_method(spec['functions']['handler'], definition, role='handler'))
		context.params._finalize()
		context.outcomes._finalize()
		object.__setattr__(context, '_finished', True)
		object.__setattr__(context, '_invoke', invoke)
		return context.response()


def source_document(rule, arguments, *, administrative_test):
	"""Load the persisted source and verify the exact configured condition."""
	if (
		set(arguments) != {"doctype", "name", "event", "occurrence"}
		or arguments["doctype"] != rule.source_doctype
		or not isinstance(arguments["name"], str)
		or not arguments["name"]
	):
		raise frappe.ValidationError(_("Invalid Process source document."))
	meta = frappe.get_meta(rule.source_doctype)
	if meta.istable or meta.issingle or meta.is_virtual:
		raise frappe.ValidationError(_("Invalid Process source document."))
	doc = frappe.get_doc(rule.source_doctype, arguments["name"])
	if not rule._match_source_condition(doc, arguments["event"], arguments["occurrence"]):
		raise frappe.ValidationError(_("Select a configured event condition matching the source document."))
	if administrative_test:
		if (
			arguments["event"] == "on_submit"
			and doc.docstatus != 1
			or arguments["event"] == "on_cancel"
			and doc.docstatus != 2
		):
			raise frappe.ValidationError(_("Source document state does not match the selected event."))
	# Field permissions still apply to the snapshot used for parameter projection.
	doc.apply_fieldlevel_read_permissions()
	return _freeze(doc.as_dict())


def _decimal_source_value(source, fieldname):
	"""Read an authorized current source field as decimal text before float coercion."""
	rows = _decimal_values(source["doctype"], [source["name"]], [fieldname])
	if len(rows) != 1:
		raise frappe.ValidationError(_("Process source document is unavailable."))
	return rows[0][fieldname]


def build_context(definition, owner, arguments, *, administrative_test, execution=None, invoke=None):
	if type(administrative_test) is not bool:
		raise frappe.ValidationError(_("Process test mode must be a boolean."))
	source = (
		source_document(owner, arguments, administrative_test=False)
		if definition.process_type == "notification" and not administrative_test
		else None
	)
	values = arguments if definition.process_type == "bot_flow" else {}
	if source is not None:
		values = owner.resolve_parameters(source)
	language = bot_language.resolve_process_language()
	execution = execution or {}
	session = None
	contact = None
	channel = None
	account = None
	message = None
	if not administrative_test and execution.get("session"):
		session = frappe.db.get_value(
			"Chat Session", execution["session"], ["name", "contact", "channel", "account"], as_dict=True
		)
		if not session:
			raise frappe.ValidationError(_("Process session is unavailable."))
		contact = session.contact
		channel, account = session.channel, session.account
		if execution.get("turn"):
			turn = frappe.db.get_value("Chat Turn", execution["turn"],
				["contact", "session", "input_summary"], as_dict=True)
			if not turn or turn.contact != contact or turn.session != session.name:
				raise frappe.PermissionError(_("Process turn does not belong to its session."))
			if bot_language.current_language() is None:
				retained = json.loads(turn.input_summary or "{}").get("language")
				with bot_language.presentation_language(retained):
					language = bot_language.resolve_process_language()
			message = frappe.db.get_value(
				"Chat Message", {"turn": execution["turn"], "kind": "Incoming", "contact": contact}, "name"
			)
	if definition.process_type == "notification":
		account = owner.account
		channel = frappe.db.get_value("Communication Channel Account", account, "channel") if account else None
	reference = lambda doctype, name: ProcessReference(doctype, name) if name else None
	if definition.process_type == "notification":
		request = NotifyRequest(
			language=language or None,
			source=reference(source["doctype"], source["name"]) if source else None,
			event=arguments.get("event") or None,
			channel=reference("Communication Channel", channel),
			account=reference("Communication Channel Account", account),
		)
	else:
		request = BotRequest(
			language=language or None,
			contact=ProcessContactReference("Chat Contact", contact) if contact else None,
			session=reference("Chat Session", session.name) if session else None,
			channel=reference("Communication Channel", channel),
			account=reference("Communication Channel Account", account),
			message=reference("Chat Message", message),
		)
	return _ProcessContext(
		definition.process_id,
		definition.process_type,
		language,
		administrative_test,
		_freeze(values, preserve_decimal=True),
		arguments.get("event", ""),
		arguments.get("occurrence", ""),
		source,
		_freeze(execution),
		invoke,
		_configured_inputs=tuple(
			item
			for item in json.loads(owner.process_contract or "{}").get("parameters", [])
			if item.get("origin") == "configurable"
		),
		_request=request,
	)
