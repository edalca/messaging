from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_request_phrase
from messaging.chatbot.doctype.bot_settings.localization import text as settings_text
from messaging.chatbot.doctype.bot_flow.localization import text as localized_text
"""Persistent dialogue orchestration consuming the shared Chatbot engine and neutral transport."""
import json
from copy import deepcopy
from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime

from messaging.common.primitives import utcnow
from messaging.chatbot.doctype.bot_flow.bot_flow import get_flow_effective_internal_error_message
from messaging.chatbot.doctype.bot_flow.execution import invoke
from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_handler, capture
from messaging.chatbot.doctype.bot_settings.bot_settings import (
	compose_informative_response, get_menu_flows, resolve_process_language, get_internal_error_message,
)
from frappe.model.naming import make_autoname
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_contact.chat_contact import lock_contact

EPISODE_MINUTES = 15


class ChatSession(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("SES-.######")

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Conversation identifiers are immutable."))

	def before_save(self):
		if not self.is_new():
			current = frappe.db.get_value('Chat Session', self.name, ['message_sequence','last_incoming_sequence'], as_dict=True)
			for field in ('message_sequence','last_incoming_sequence'):
				self.set(field, max(self.get(field) or 0, current.get(field) or 0))

	def validate(self):
		contact = frappe.db.get_value("Chat Contact", self.contact, ["channel", "account", "identity_key"], as_dict=True)
		if not contact or (self.channel, self.account) != (contact.channel, contact.account):
			frappe.throw(_("The session does not match the contact identity."))
		self.active_key = None if self.status in ("Closed", "Expired") else contact.identity_key

	def read_state(self, field):
		return read_state(self, field)

	def write_state(self, field, state):
		write_state(self, field, state)

	def settle_technical_failure(self, now=None):
		return settle_technical_failure(self, now=now)

	def enqueue(self, state, requested, settings, **kwargs):
		enqueue(state, requested, settings, **kwargs)


def read_state(doc, field):
	return json.loads(doc.get(field) or "{}")


def write_state(doc, field, state):
	from messaging.common.primitives import decimal_json
	doc.set(field, json.dumps(state, ensure_ascii=False, allow_nan=False, default=decimal_json))


def episode_has_pending_work(session):
	state = read_state(session, 'dialogue_state')
	return bool(state.get('queue') or session.active_flow or state.get('registration_pending')
		or state.get('registration_completed') or state.get('registration_request'))


def episode_expired(session, now):
	"""Use the engine's idle TTL; unresolved business work never expires implicitly."""
	return bool(session.expires_at and get_datetime(session.expires_at) <= now
		and not episode_has_pending_work(session))


def notification_blocker(key, now):
	"""Read under the canonical contact lock; do not create or extend an episode."""
	from messaging.conversation.doctype.chat_session.human import HUMAN_STATES
	name = frappe.db.get_value('Chat Session', {'active_key': key}, 'name')
	if not name:
		return None
	session = frappe.get_doc('Chat Session', name)
	if session.status in HUMAN_STATES:
		return 'human_attention'
	if session.status in ('Executing', 'Recovery Required') or frappe.db.exists('Chat Turn',
		{'session': session.name, 'status': 'Processing'}):
		return 'chatbot_processing'
	if session.status not in ('Closed', 'Expired') and not episode_expired(session, now):
		return 'chatbot_episode'
	return None


def get_session(contact, now):
	session = frappe.get_doc("Chat Session", contact.active_session) if contact.active_session else None
	if session:
		settle_technical_failure(session, now)
	if session and session.status not in ("Human Requested", "Human Queued", "Human Active", "Human Paused", "Executing", "Recovery Required", "Closed", "Expired"):
		if episode_expired(session, now):
			session.status = "Expired"
			session.active_key = None
			session.save(ignore_permissions=True)
	if session and session.status not in ("Closed", "Expired"):
		return session
	session = frappe.get_doc(dict(
		doctype="Chat Session", contact=contact.name, channel=contact.channel, account=contact.account,
		active_key=contact.identity_key, status="Active", last_activity=now,
		expires_at=now+timedelta(minutes=EPISODE_MINUTES),
		dialogue_state=json.dumps(dict(queue=[], values={}, greeted=False, completed_count=0)),
	)).insert(ignore_permissions=True)
	contact.active_session = session.name
	contact.save(ignore_permissions=True)
	return session


def settle_technical_failure(session, now=None):
	"""Under the contact lock, retire a failed episode only after definitive handoff evidence."""
	from messaging.conversation.doctype.chat_session.human import HUMAN_STATES
	if session.status in ("Closed", "Expired"):
		return False
	state = read_state(session, "dialogue_state")
	failure = state.get("technical_failure")
	if not failure and session.status == "Recovery Required":
		# Older TypeError receipts were incorrectly marked Unknown despite the
		# executor's completed rollback. Recover from protected durable evidence.
		logs = frappe.get_all("Process Execution Log", filters=dict(session=session.name,
			execution_outcome="Internal Execution Error", diagnostic_error_type="TypeError"),
			fields=["turn", "execution_id"], order_by="creation desc", limit_page_length=1)
		if logs and state.get("queue") and execution_key(session, state["queue"][0]) == logs[0].execution_id:
			owner = frappe.get_doc("Chat Turn", logs[0].turn)
			executions = json.loads(owner.executions or "[]")
			for execution in executions:
				if execution["id"] == logs[0].execution_id and execution["state"] == "Unknown":
					execution["state"] = "Failed"
					owner.executions = json.dumps(executions)
					owner.save(ignore_permissions=True)
					failure = dict(turn=owner.name, execution_id=execution["id"])
					state["technical_failure"] = failure
					write_state(session, "dialogue_state", state)
					session.save(ignore_permissions=True)
	if not failure or failure.get("resolved"):
		return False
	owner = frappe.get_doc("Chat Turn", failure["turn"])
	if not owner.dispatch:
		return False
	dispatch = frappe.get_doc("Bot Dispatch", owner.dispatch)
	if dispatch.status not in ("Accepted", "Rejected"):
		return False
	# All automatic handoffs of this episode must be definitive. TTL is never
	# evidence for an uncertain delivery or permission to resend it.
	if frappe.db.exists("Bot Dispatch", dict(session=session.name, message_type="Automatic",
		status=["in", ["Sending", "Unknown"]])):
		return False
	stamp = now or utcnow()
	for row in frappe.get_all("Bot Dispatch", filters=dict(session=session.name, message_type="Automatic",
		status=["in", ["Reserved", "Prepared", "Held"]]), pluck="name"):
		unsent = frappe.get_doc("Bot Dispatch", row)
		unsent.status, unsent.resolved_at, unsent.diagnostic_code = "Rejected", stamp, "episode_closed_before_handoff"
		unsent.save(ignore_permissions=True)
	decisions = json.loads(owner.decisions or "[]")
	decisions.extend(dict(kind="Flow Cancelled", flow=item["flow"], reason="Technical Error") for item in state.get("queue", []))
	decisions.append(dict(kind="Technical Episode Closed", execution_id=failure["execution_id"],
		flows=[item["flow"] for item in state.get("queue", [])]))
	owner.decisions, owner.status, owner.inbox_state, owner.request_context = json.dumps(decisions), "Complete", "Done", None
	owner.save(ignore_permissions=True)
	if session.status in HUMAN_STATES:
		write_state(session, "dialogue_state", dict(queue=[], values={}, completed_count=0,
			technical_failure=dict(failure, resolved=True)))
		session.active_flow = None
		session.save(ignore_permissions=True)
		return True
	if state.get('registration_request'):
		# Retire the failed invocation only after its safe notice has definitive transport
		# evidence. A fresh input can retry registration without replacing the original request.
		retained = {key: state[key] for key in ('registration_request', 'registration_context', 'registration_language',
			'registration_decision', 'interaction_mode', 'language') if key in state}
		write_state(session, 'dialogue_state', dict(queue=[], values={}, completed_count=0,
			registration_pending=True, **retained))
		session.active_flow, session.status = None, 'Active'
		session.save(ignore_permissions=True)
		return True
	# Preserve the receipt, profile and message history, never captured arguments.
	write_state(session, "dialogue_state", dict(queue=[], values={}, completed_count=0,
		technical_failure=dict(failure, resolved=True)))
	session.active_flow = None
	session.status = "Expired" if session.expires_at and get_datetime(session.expires_at) <= stamp else "Closed"
	session.closed_at, session.close_reason = stamp, "Technical Error"
	session.save(ignore_permissions=True)
	return True


def enqueue(state, requested, settings, *, menu_selection=None, human_attention=False, registration=False):
	if human_attention and state.get('human_attention'):
		return
	if menu_selection:
		menu = get_menu_flows(settings)
		if menu_selection in {item["workflow_code"] for item in menu}:
			requested = [*requested, menu_selection]
	if not state["queue"]:
		state["completed_count"] = 0
		state["values"] = {}
	queued = {item["flow"] for item in state["queue"]}
	for code in requested:
		if (code in queued and not (human_attention or registration)) or not frappe.db.exists("Bot Flow", {"name": code, "status": "Enabled"}):
			continue
		from messaging.communication.doctype.process_definition.validation import test_state
		if test_state(frappe.get_doc('Bot Flow', code), remember_invalid=True) != 'Passed':
			continue
		item = dict(flow=code, request_id=frappe.generate_hash(length=24), prompted=False, invalid_attempts=0, values={})
		if human_attention:
			item['human_attention'] = True
			state['human_attention'] = item['request_id']
			state['active_request'] = item['request_id']
			state.pop('mechanical_menu', None)
		if registration:
			item['registration'] = True
			state['registration_pending'] = True
			state['active_request'] = item['request_id']
		state["queue"].append(item)
		queued.add(code)
	state["queue"].sort(key=lambda item: (item["flow"] != settings.greeting_flow, int(frappe.db.get_value("Bot Flow", item["flow"], "priority") or 0)))
	# Keep an already active capture first; new intentions cannot hijack its input.
	active = state.get("active_request")
	if active:
		state["queue"].sort(key=lambda item: item["request_id"] != active)


def prompt(rows, base, *, invalid=False):
	language = resolve_process_language()
	lines = [_('The value is invalid or ambiguous. Please provide the pending information.', lang=language)] if invalid else []
	if base and base.strip():
		lines.append(base.strip())
	lines.extend(dict.fromkeys(get_request_phrase(row.parameter, language).strip() for row in rows))
	for hint in dict.fromkeys(get_handler(frappe.db.get_value("Bot Parameter", row.parameter, "parameter_type")).input_hint for row in rows):
		if hint:
			lines.append(_(hint, lang=language))
	return "\n".join(lines)


def execution_key(session, item):
	parts = [session.name, item["request_id"]]
	if item.get("process_attempt", 0):
		parts.append(str(item["process_attempt"]))
	return identity_key(*parts)


def finish_flow(session, state, item, decisions, *, cancelled=False):
	decisions.append(dict(kind="Flow Cancelled" if cancelled else "Flow Completed", flow=item["flow"],
		invalid_attempts=item.get("invalid_attempts", 0)))
	state["queue"].remove(item)
	if item.get('human_attention'):
		state.pop('human_attention', None)
	if cancelled:
		for code in item.get("values", {}):
			state.get("values", {}).pop(code, None)
	for key in ("active_request", "prompt_turn"):
		state.pop(key, None)
	if not state["queue"]:
		state["values"] = {}
	state["completed_count"] = state.get("completed_count", 0) + (0 if cancelled else 1)
	session.active_flow = None
	session.status = "Active"


def mechanical_menu_flows(settings):
	"""Filter configured menu Flows by document access and technical availability."""
	installed = set(frappe.get_installed_apps())
	result = []
	for row in get_menu_flows(settings):
		flow = frappe.get_doc("Bot Flow", row["workflow_code"])
		from messaging.communication.doctype.process_definition.validation import test_state
		if test_state(flow) != 'Passed':
			continue
		if not flow.has_permission("read"):
			continue
		if not flow.process_method or flow.process_method.split(".")[0] not in installed:
			continue
		if any(not frappe.get_doc("Bot Parameter", p.parameter).has_permission("read")
			or not frappe.db.get_value("Bot Parameter", p.parameter, "enabled") for p in flow.parameters):
			continue
		result.append(row)
	return result


def mechanical_menu_options(settings):
	"""Compose eligible Flows and the existing native Human capability for display and selection."""
	available = {row["workflow_code"]: dict(flow=row["workflow_code"], label=row["label"])
		for row in mechanical_menu_flows(settings)}
	if settings.human_attention_enabled:
		try:
			settings.human_flow_configuration(operational=True)
		except (frappe.ValidationError, frappe.PermissionError):
			frappe.clear_last_message()
		else:
			available[None] = dict(flow=None, action="Human", label=_("Contact a human agent", lang=resolve_process_language()))
	return available


def main_menu(settings, state=None, dispatch=None, *, bind=True):
	"""Render the same eligible capabilities for native and mechanical menu requests."""
	language = resolve_process_language()
	available = mechanical_menu_options(settings)
	previous = state.get("mechanical_menu") if state is not None else None
	options = ({number: available[option["flow"]]
		for number, option in previous["options"].items() if option["flow"] in available}
		if previous else {str(i): row for i, row in enumerate(available.values(), 1)})
	if bind and state is not None and dispatch is not None:
		state["mechanical_menu"] = dict(options=options, dispatch=dispatch.name)
	lines = [(settings_text(settings, "informative_categories_header") or _("Select an option:", lang=language)).strip()]
	lines.extend(number + ". " + option["label"] for number, option in options.items())
	if not options:
		lines.append(_("No options are currently available.", lang=language))
	lines.append(_("0. Return to the main menu", lang=language))
	return "\n".join(lines)


def complete_request(state):
	"""Retire fully resolved request state; keep the episode and profile."""
	if state.get("queue") or not state.get("completed_count"):
		return
	for key in ("mechanical_menu", "menu_pending", "active_request", "prompt_turn"):
		state.pop(key, None)
	state["greeted"] = False
	state["values"] = {}
	state["completed_count"] = 0


def reject_attempt(item, turn, settings, decisions):
	if item.get("last_invalid_turn") != turn.name:
		item["invalid_attempts"] = item.get("invalid_attempts", 0) + 1
		item["last_invalid_turn"] = turn.name
		decisions.append(dict(kind="Invalid Attempt", flow=item["flow"], count=item["invalid_attempts"]))
	return item["invalid_attempts"] >= int(settings.max_invalid_attempts or 3)


def apply_flow_result(result, flow, item, session, state, turn, settings, decisions):
	"""Apply a functional action once, also used by verified recovery without invoking Process."""
	parts = []
	if result.get("flow_action") == "Retry Parameter" and result.get("functional"):
		code = result["retry_parameter"]
		had_value = code in item.get("values", {})
		item.setdefault("values", {}).pop(code, None)
		state.setdefault("values", {}).pop(code, None)
		item["recapture"] = list(set(item.get("recapture", [])) | {code})
		item["process_attempt"] = item.get("process_attempt", 0) + 1
		if had_value and reject_attempt(item, turn, settings, decisions):
			if flow.name == settings.greeting_flow:
				state.pop('menu_after_greeting', None)
			finish_flow(session, state, item, decisions, cancelled=True)
			parts.append(settings_text(settings, "attempts_exhausted_message"))
			return parts, bool(settings.show_menu_on_attempts_exhausted), False
		item["prompted"] = True
		session.status = "Capturing"
		if not (result.get("text") or "").strip():
			parts.append(prompt([row for row in flow.parameters if row.parameter == code], localized_text(flow, "input_continuation_message")))
		return parts, False, True
	if state.get('menu_after_greeting') and flow.name == settings.greeting_flow:
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		config = configuration(frappe.get_single('Bot Settings'), 'greeting', operational=True, expected_flow=flow.name)
		if not result.get('functional') or result.get('flow_action') != 'Complete Flow' or result.get('status') not in config['states']:
			state.pop('menu_after_greeting', None)
	if item.get('registration') and result.get('functional') and result.get('flow_action') == 'Complete Flow':
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		current = frappe.get_single('Bot Settings')
		config = configuration(current, 'registration', operational=True)
		if config['flow'].name != flow.name:
			raise frappe.ValidationError(_('The registration Flow has changed.'))
		if result['status'] in config['states']:
			from messaging.conversation.doctype.chat_contact.chat_contact import complete_registration
			contact = lock_contact(session.contact)
			complete_registration(contact, current, result, session=session, turn=turn, execution_id=execution_key(session, item))
			state['registration_completed'] = True
			state['registration_pending'] = False
	finish_flow(session, state, item, decisions, cancelled=not result.get("functional") or result.get("flow_action") == "Cancel Flow")
	if item.get('human_attention') and result.get('functional') and result.get('flow_action') == 'Complete Flow':
		current = frappe.get_single('Bot Settings')
		from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
		config = configuration(current, 'human', operational=True)
		if flow.name != config['flow'].name:
			raise frappe.ValidationError(_('The human support Flow has changed.'))
		if result.get('status') in config['states']:
			from messaging.conversation.doctype.chat_session.human import transfer
			parts.append(transfer(session, state, turn=turn.name, flow=flow, result=result, execution_id=execution_key(session, item)))
	return parts, False, False


def advance(contact, session, turn, state, request, settings, dispatch, *, allow_execution=True, prefix=""):
	"""Execute the persistent queue until the next missing input or uncertain business operation."""
	messages = [prefix] if prefix else []
	decisions = json.loads(turn.decisions or "[]")
	executions = json.loads(turn.executions or "[]")
	functional = False
	registering = bool(state.get('registration_pending'))
	text_consumed = False
	show_menu = False
	initial_request = state.get("active_request") if session.active_flow else None
	while state["queue"]:
		item = state["queue"][0]
		flow = frappe.get_doc("Bot Flow", item["flow"])
		consumer = 'registration' if item.get('registration') else 'human' if item.get('human_attention') else None
		if consumer:
			from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration
			try:
				configuration(frappe.get_single('Bot Settings'), consumer, operational=True, expected_flow=flow.name)
			except (frappe.ValidationError, frappe.PermissionError):
				frappe.clear_last_message()
				messages.append(get_internal_error_message())
				finish_flow(session, state, item, decisions, cancelled=True)
				break
		session.active_flow = flow.name
		state["active_request"] = item["request_id"]
		from messaging.communication.doctype.process_definition.validation import test_state
		if flow.status != 'Enabled' or test_state(flow, remember_invalid=True) != 'Passed':
			messages.append(get_flow_effective_internal_error_message(flow))
			decisions.append(dict(kind="Internal Error", flow=flow.name))
			finish_flow(session, state, item, decisions, cancelled=True)
			continue
		# Only registered profile values may cross episodes; session values stay within this contact's episode.
		available = {**read_state(contact, "parameter_values"), **state.get("values", {}), **item.get("values", {})}
		for code in item.get("recapture", []):
			if code not in item.get("values", {}):
				available.pop(code, None)
		continuation = bool(item["prompted"])
		capture_rows = [frappe._dict(row.as_dict(), required=1) if row.parameter in item.get("recapture", []) else row
			for row in flow.parameters]
		values, missing, invalid = capture(
			capture_rows, available, request,
			requested_parameter=next((row.parameter for row in sorted(capture_rows, key=lambda row: row.capture_order)
				if row.required and row.parameter not in available), None) if item["prompted"] else None,
			accept_text=not text_consumed and
				(bool(item["prompted"]) and not request.get("flows") and not request.get("menu_selection")
				 or flow.name in request.get("flows", []) and request.get("text", "").strip() != flow.name),
		)
		if allow_execution and not invalid:
			values = turn.capture_pending(session, capture_rows, values)
			values, missing, invalid = capture(capture_rows, values, {}, accept_text=False)
		text_consumed = True
		item["values"] = values
		for row in flow.parameters:
			state.setdefault("values", {}).pop(row.parameter, None)
		state["values"].update(values)
		if invalid:
			item["recapture"] = list(set(item.get("recapture", [])) | {row.parameter for row in missing})
		if invalid and reject_attempt(item, turn, settings, decisions):
			if flow.name == settings.greeting_flow:
				state.pop('menu_after_greeting', None)
			messages.append(settings_text(settings, "attempts_exhausted_message"))
			show_menu |= bool(settings.show_menu_on_attempts_exhausted)
			finish_flow(session, state, item, decisions, cancelled=True)
			request = {**request, "parameters": {}, "text": ""}
			continue
		if missing:
			base = localized_text(flow, "input_continuation_message") if continuation else localized_text(flow, "input_initial_message")
			messages.append(prompt(missing[:1], base, invalid=invalid))
			if allow_execution:
				item["prompted"] = True
			decisions.append(dict(kind="Parameter Request", flow=flow.name,
								  parameters=[row.parameter for row in missing], input="Continuation" if continuation else "Initial"))
			if allow_execution:
				session.status = "Capturing"
			elif item["request_id"] != initial_request:
				session.active_flow = None
				state.pop("active_request", None)
			break
		if not allow_execution:
			if item["request_id"] != initial_request:
				session.active_flow = None
				state.pop("active_request", None)
			break
		execution_id = execution_key(session, item)
		execution = dict(id=execution_id, flow=flow.name, state="Running")
		executions.append(execution)
		turn.executions = json.dumps(executions)
		session.status = "Executing"
		write_state(session, "dialogue_state", state)
		session.save(ignore_permissions=True)
		turn.save(ignore_permissions=True)
		# Persist the ordered prefix with the one-time invocation marker before business code.
		if messages:
			dispatch.content = "\n\n".join(messages)
			dispatch.save(ignore_permissions=True)
		frappe.db.commit()
		# The connection lock owns conversation state; do not hold the contact row during business code.
		try:
			if consumer:
				from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings
				configuration(get_bot_settings(shared=True), consumer, operational=True, expected_flow=flow.name)
		except (frappe.ValidationError, frappe.PermissionError):
			frappe.clear_last_message()
			from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log
			log = record_process_execution_log(process_method=flow.process_method, workflow=flow.name,
				session=session.name, turn=turn.name, execution_id=execution_id,
				execution_outcome='Internal Execution Error', output_mode_used='Internal Error Fallback',
				diagnostic_error_type='ConsumerConfigurationChanged',
				diagnostic_detail='Consumer configuration changed before business invocation.')
			result = dict(state='Failed', status=None, functional=False, text=get_internal_error_message(), execution_log=log.name)
		else:
			result = invoke(flow, values, session=session, turn=turn, execution_id=execution_id)
		if (item.get('human_attention') or item.get('registration')) and not result['functional']:
			result['text'] = get_internal_error_message()
		parts, menu, waiting = [], False, False
		if result['functional']:
			consumer_state = deepcopy(state)
			if item.get('registration'):
				frappe.db.savepoint('registration_consumer')
			try:
				parts, menu, waiting = apply_flow_result(result, flow, item, session, state, turn, settings, decisions)
				if item.get('registration') and state.get('registration_completed'):
					contact.reload()
			except (frappe.ValidationError, frappe.PermissionError):
				if item.get('registration'):
					frappe.db.rollback(save_point='registration_consumer')
					contact.reload()
				state.clear()
				state.update(consumer_state)
				frappe.clear_last_message()
				# The Process already ran: retain its queue marker for explicit reconciliation.
				result = dict(result, functional=False, state='Unknown', text=get_internal_error_message())
				decisions.append(dict(kind='Consumer Persistence Failed', flow=flow.name, execution_id=execution_id))
				if item not in state['queue']:
					state['queue'].insert(0, item)
				state['active_request'] = item['request_id']
				session.active_flow = flow.name
		execution.update(state=result["state"], status=result["status"])
		turn.executions = json.dumps(executions)
		messages.append(result["text"])
		decisions.append(dict(kind="Functional Result" if result["functional"] else "Internal Error",
							  flow=flow.name, execution_id=execution_id, status=result["status"]))
		functional |= result["functional"]
		if result["state"] == "Unknown":
			session.status = "Recovery Required"
			# Keep this operation and all remaining intentions, without allowing replay.
			break
		if not result["functional"]:
			state["technical_failure"] = dict(turn=turn.name, execution_id=execution_id, notice_stored=True)
			session.status = "Recovery Required"
			write_state(session, "dialogue_state", state)
			session.save(ignore_permissions=True)
			turn.status, turn.decisions = "Recovery Required", json.dumps(decisions)
			turn.save(ignore_permissions=True)
			dispatch.content, dispatch.functional_result = "\n\n".join(messages), int(functional)
			dispatch.save(ignore_permissions=True)
			# A lost response-preparation step can recover this terminal receipt
			# without repeating the Process or duplicating the safe notice.
			frappe.db.commit()
			lock_contact(contact.name)
			# Do not execute the remaining batch after a terminal technical failure.
			break
		if item.get('human_attention') and result.get('flow_action') == 'Complete Flow' and parts:
			# Transfer confirmation is returned only after acceptance of the Human request.
			messages.pop()
		messages.extend(parts)
		show_menu |= menu
		# Persist each completed result with its output before the next Process checkpoint.
		dispatch.content = "\n\n".join(messages)
		dispatch.functional_result = int(functional)
		dispatch.save(ignore_permissions=True)
		write_state(session, "dialogue_state", state)
		session.save(ignore_permissions=True)
		turn.decisions = json.dumps(decisions)
		turn.save(ignore_permissions=True)
		if waiting or item.get("registration"):
			break
	if show_menu:
		state["menu_pending"] = True
	# complete_request clears menu_pending when the batch succeeds. Keep the render decision locally.
	menu_requested = bool(state.get("menu_pending") or state.get("menu_after_greeting"))
	if allow_execution and functional and not state["queue"] and not registering:
		complete_request(state)
	mechanical = state.get("interaction_mode") == "Mechanical"
	if allow_execution and not registering and not state["queue"] and (menu_requested or request.get("_mechanical_menu")):
		if request.get("_invalid_mechanical_menu"):
			messages.append(_("Please select a valid menu option.", lang=resolve_process_language()))
		if request.get("_refresh_mechanical_menu"):
			state.pop("mechanical_menu", None)
		messages.append(main_menu(settings, state, dispatch))
		decisions.append(dict(kind="Main Menu", mode="Mechanical" if mechanical else "Natural"))
		state.pop("menu_pending", None)
		state.pop("menu_after_greeting", None)
	if request.get("_recognition_hold"):
		messages.append(request["_recognition_hold"])
	if not messages:
		messages.append(compose_informative_response(settings))
		decisions.append(dict(kind="Informative"))
		if (allow_execution and settings.informative_show_available_categories
			and not state["queue"] and not registering):
			messages.append(main_menu(settings, state, dispatch))
			decisions.append(dict(kind="Main Menu", mode="Natural"))
	turn.decisions = json.dumps(decisions)
	return "\n\n".join(messages), functional


def on_doctype_update():
	frappe.db.add_index("Chat Session", ["status", "last_activity"], "human_inbox_activity")
	frappe.db.add_index("Chat Session", ["channel", "account", "assigned_agent", "status"], "agent_assignment")
