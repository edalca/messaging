"""Neutral, transaction-owning backend entry point. This module exposes no HTTP API."""
from messaging.chatbot.doctype.bot_settings.localization import text as settings_text

import json
from datetime import datetime, timedelta, timezone

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime
from messaging.security.execution_context import automatic_operation

from messaging.chatbot.doctype.bot_dispatch.bot_dispatch import prepare, reserve
from messaging.common.primitives import utcnow
from messaging.chatbot.doctype.bot_settings.bot_settings import get_bot_settings, is_registration_complete
from messaging.conversation.doctype.chat_message.chat_message import append_message, dispatch_content
from messaging.conversation.doctype.chat_session.human import HUMAN_OPEN, HUMAN_STATES, request_human
from frappe.model.naming import make_autoname
from messaging.common.locking import contact_lock
from messaging.conversation.doctype.chat_contact.chat_contact import (
	_draining, get_contact, lock_contact, require_operator, resume_pending_inputs, schedule_contact,
)
from messaging.common.primitives import identity_key
from messaging.conversation.doctype.chat_session.chat_session import (
	EPISODE_MINUTES, advance, enqueue, get_session, read_state, write_state,
	execution_key, apply_flow_result, main_menu,
	settle_technical_failure,
	mechanical_menu_flows, complete_request,
)

def pending_input_request(turn):
	"""Reconstruct a held input from its private context and sole canonical message."""
	context = json.loads(turn.request_context or "{}")
	message = frappe.get_doc("Chat Message", {"turn": turn.name, "kind": "Incoming"})
	contact = frappe.get_doc("Chat Contact", turn.contact)
	return dict(context.get("request", {}), channel=contact.channel, account=contact.account,
		sender=contact.external_sender, event_id=context.get("event_id") or message.external_id or turn.event_key,
		event_at=get_datetime(turn.event_at).replace(tzinfo=timezone.utc).isoformat(), text=message.content or "")


def human_request_hold(session, state, request, decision, settings, turn):
	"""Keep a disabled-attention notice only when human support is the whole request."""
	message = request_human(session, state, request, decision, settings, turn=turn)
	if (message and not settings.human_attention_enabled and not state.get('human_attention')
			and (request.get('flows') or request.get('menu_selection'))):
		return None
	return message


def capture_pending(turn, session, rows, values):
	"""Consume only contiguous, unambiguous parameter fragments before invocation.

	No prompt attribution is invented for free text. Semantic prose or an explicit
	new selection stays in receipt order for the normal conversation engine.
	"""
	if not _draining.get() or not turn.receipt_order:
		return values
	import re
	from messaging.chatbot.doctype.bot_parameter.bot_parameter import capture, recognize, validate_input_value
	from messaging.conversation.doctype.chat_message.chat_message import attach_incoming
	from messaging.chatbot.doctype.bot_settings.language import current_language
	known = {row.parameter: frappe.get_doc('Bot Parameter', row.parameter) for row in rows}
	for _index in range(50):
		# READ COMMITTED visibility without committing the partially built current turn.
		pending = frappe.db.sql('''SELECT name FROM `tabChat Turn`
			WHERE contact=%s AND receipt_order>%s AND inbox_state='Pending'
			ORDER BY receipt_order,name LIMIT 1 FOR UPDATE''', (turn.contact, turn.receipt_order), as_dict=True)
		if not pending:
			break
		next_turn = frappe.get_doc('Chat Turn', pending[0].name)
		request = pending_input_request(next_turn)
		if (request.get('flows') or request.get('menu_selection') or request.get('human_requested')
			or request.get('unsupported') or request.get('menu_dispatch')):
			break
		text = request.get('text', '')
		_masked, occurrences = recognize(text, known)
		residue = text
		for occurrence in reversed(occurrences):
			residue = residue[:occurrence.start] + ' ' * (occurrence.end - occurrence.start) + residue[occurrence.end:]
		# Restrict look-ahead to data fragments; never infer a new intent from prose.
		words = re.findall(r'\w+', residue.casefold())
		if any(word not in {'y', 'e', 'and', 'also', 'también', 'tambien', 'el', 'la', 'los', 'las', 'the'} for word in words):
			break
		new, _missing, invalid = capture(rows, {}, request, accept_text=True)
		if invalid or not new or set(request.get('parameters', {})) - set(known):
			break
		merged = dict(values)
		for row in rows:
			key = row.parameter
			if key not in new:
				continue
			if key in merged and not row.allow_multiple_values and merged[key] != new[key]:
				break
			raw = merged[key] + new[key] if key in merged and row.allow_multiple_values else new[key]
			valid, value = validate_input_value(row, known[key], raw)
			if not valid:
				break
			merged[key] = value
		else:
			message = frappe.get_doc('Chat Message', {'turn': next_turn.name, 'kind': 'Incoming'})
			attach_incoming(message, session)
			next_turn.session, next_turn.status = session.name, 'Complete'
			next_turn.inbox_state, next_turn.consumed_by = 'Done', turn.name
			next_turn.input_summary = json.dumps(dict(json.loads(next_turn.input_summary or '{}'), language=current_language()))
			next_turn.decisions = json.dumps([dict(kind='Anticipated Parameters', turn=turn.name, parameters=sorted(new))])
			next_turn.save(ignore_permissions=True)
			values = merged
			continue
		break
	return values


class ChatTurn(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("TRN-.######")

	def capture_pending(self, session, rows, values):
		return capture_pending(self, session, rows, values)

	def before_save(self):
		if self.status in ("Complete", "Ignored", "Human Paused", "Quota Blocked"):
			self.request_context = None

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Conversation identifiers are immutable."))

	def validate(self):
		if not self.session and self.status == "Pending" and self.inbox_state in ("Pending", "Running"):
			return
		if frappe.db.get_value("Chat Session", self.session, "contact") != self.contact:
			frappe.throw(_("The turn does not belong to this contact."))


def validate_request(request):
	allowed = {"channel", "account", "sender", "event_id", "event_at", "text", "flows",
			   "menu_selection", "parameters", "max_text_length", "reply_to", "human_requested",
			   "source_type", "external_reference", "reply_reference", "selection_id", "unsupported", "menu_dispatch"}
	if not isinstance(request, dict) or set(request)-allowed:
		frappe.throw(_("Invalid conversation request."))
	for field in ("channel", "account", "sender", "event_id"):
		if not isinstance(request.get(field), str) or not request[field].strip() or len(request[field]) > 140:
			frappe.throw(_("Invalid conversation identity."))
	request = dict(request)
	# Source event time is mandatory: arrival time cannot identify a delayed continuation.
	try:
		stamp = datetime.fromisoformat(request["event_at"])
		if stamp.tzinfo is None:
			raise ValueError
		request["event_at"] = stamp.astimezone(timezone.utc).replace(tzinfo=None)
		if request["event_at"] > utcnow()+timedelta(minutes=5):
			raise ValueError
	except (KeyError, TypeError, ValueError):
		frappe.throw(_("Event time must be an ISO timestamp with timezone."))
	flows = request.setdefault("flows", [])
	parameters = request.setdefault("parameters", {})
	if (not isinstance(flows, list) or len(flows) > 30 or
		any(not isinstance(code, str) or len(code)>64 for code in flows) or
		not isinstance(parameters, dict) or len(parameters)>100 or
		any(not isinstance(key, str) or not valid_candidate(value) for key, value in parameters.items()) or
		not isinstance(request.get("text", ""), str) or len(request.get("text", "")) > 16000 or
		type(request.get("human_requested", False)) is not bool or
		not isinstance(request.get("menu_selection", ""), str) or
		not isinstance(request.get("reply_to", ""), str) or
		("max_text_length" in request and (type(request["max_text_length"]) is not int or request["max_text_length"] < 1))):
		frappe.throw(_("Invalid conversation request."))
	for field in ("source_type", "external_reference", "reply_reference", "selection_id", "menu_dispatch"):
		if field in request and (not isinstance(request[field], str) or len(request[field]) > 140):
			frappe.throw(_("Invalid conversation source metadata."))
	if type(request.get("unsupported", False)) is not bool:
		frappe.throw(_("Invalid conversation source metadata."))
	if len(json.dumps(parameters, ensure_ascii=False).encode()) > 65536:
		frappe.throw(_("Invalid conversation request."))
	return request


def valid_candidate(value):
	items = value if isinstance(value, list) else [value]
	return bool(items) and len(items) <= 100 and all(type(v) in (str, int, bool) and len(str(v)) <= 4000 for v in items)


def turn_result(turn):
	dispatch = frappe.get_doc("Bot Dispatch", turn.dispatch) if turn.dispatch else None
	return dict(turn=turn.name, session=turn.session, contact=turn.contact, state=turn.status,
				dispatch=turn.dispatch, outbound_id=dispatch.outbound_key if dispatch else None,
				response=dispatch_content(dispatch) if dispatch else None,
				dispatch_state=dispatch.status if dispatch else None)


@automatic_operation
def process_event(request, *, interpret_natural=False, provider=None, _resume_turn=None):
	"""Owns commits/checkpoints. Call on a clean backend transaction as a permitted operator.

	Identity is (channel, account, sender). Flows and candidate keys are canonical catalog names.
	Natural interpretation is opt-in through process_message; execution stays in this engine.
	"""
	require_operator()
	if getattr(frappe.local, "request", None):
		raise frappe.PermissionError(_("Conversation processing requires a background worker."))
	request = validate_request(request)
	key = identity_key(request["channel"], request["account"], request["sender"])
	from messaging.chatbot.doctype.bot_settings.language import contact_language, presentation_language
	with contact_lock(key):
		language_scope = None
		try:
			if provider is not None:
				from messaging.communication.doctype.communication_channel_account.binding import resolve_binding
				resolve_binding(provider, request["account"], incoming=True)
			contact = get_contact(request["channel"], request["account"], request["sender"])
			if not _resume_turn:
				frappe.db.commit()
				resume_pending_inputs(contact.name)
				contact = lock_contact(contact.name)
			event_key = identity_key(key, request["event_id"])
			# The contact connection lock owns this event identity, including duplicates.
			# Avoid range locks on a not-yet-existing event in another contact's index gap.
			existing = frappe.db.get_value("Chat Turn", {"event_key": event_key}, "name")
			if existing and not _resume_turn:
				turn = frappe.get_doc("Chat Turn", existing)
				if turn.status == "Processing":
					session = frappe.get_doc("Chat Session", turn.session)
					if session.status in HUMAN_STATES:
						frappe.db.commit()
						return turn_result(turn)
					session.status = "Recovery Required"
					session.save(ignore_permissions=True)
					turn.status = "Recovery Required"
					turn.save(ignore_permissions=True)
					frappe.db.commit()
				result = turn_result(turn)
				# Release the contact row before another operation acquires its connection lock.
				frappe.db.commit()
				return result
			now = utcnow()
			stale = not _resume_turn and contact.last_event_at and request["event_at"] < get_datetime(contact.last_event_at)
			session = frappe.get_doc("Chat Session", contact.active_session) if stale and contact.active_session else get_session(contact, now)
			state = read_state(session, "dialogue_state")
			settings = get_bot_settings()
			retained_language = None
			if _resume_turn:
				retained = frappe.get_doc("Chat Turn", _resume_turn)
				if retained.contact != contact.name:
					raise frappe.PermissionError(_("The turn does not belong to this contact."))
				retained_language = json.loads(retained.input_summary or "{}").get("language")
			if state.get('registration_request'):
				retained_language = state.get('registration_language')
			enabled_languages = {row.language for row in settings.chatbot_languages if row.enabled}
			context_language = state.get("registration_language") or state.get("language")
			language = (retained_language if retained_language in enabled_languages else
				contact_language(contact, '' if stale else request.get('text', ''), settings, context_language=context_language))
			language_scope = presentation_language(language)
			language_scope.__enter__()
			if _resume_turn:
				turn = frappe.get_doc("Chat Turn", _resume_turn)
				if (turn.contact != contact.name or turn.status not in ("Pending", "Recovery Required", "Processing") or turn.dispatch
					or json.loads(turn.executions or "[]")):
					frappe.throw(_("The pending input cannot be resumed."))
				origin = turn.session
				turn.session, turn.status = session.name, "Processing"
				turn.input_summary = json.dumps(dict(json.loads(turn.input_summary or "{}"), received_session=origin, language=language))
				incoming = frappe.get_doc("Chat Message", {"turn": turn.name, "kind": "Incoming"})
				from messaging.conversation.doctype.chat_message.chat_message import attach_incoming
				attach_incoming(incoming, session)
				turn.save(ignore_permissions=True)
			else:
				turn = frappe.get_doc(dict(
				doctype="Chat Turn", event_key=event_key, contact=contact.name, session=session.name,
				received_at=now, event_at=request["event_at"],
				input_summary=json.dumps(dict(text_length=len(request.get("text", "")),
					candidate_parameters=list(request["parameters"]), language=language)),
				decisions=json.dumps([dict(kind="Flow Request", flows=request["flows"], menu=request.get("menu_selection"))]),
				executions="[]",
					request_context=json.dumps(dict(event_id=request["event_id"], interpret_natural=interpret_natural,
						request={k:v for k,v in request.items() if k not in
							("channel", "account", "sender", "event_id", "event_at", "text")})),
				)).insert(ignore_permissions=True)
			# Incoming text is retained once, even while automation is suspended.
				incoming = append_message(session, event_key, 'Incoming', request.get('text',''), turn=turn.name, event_at=request['event_at'],
					external_id=request.get('external_reference'), source_type=request.get('source_type'),
					reply_reference=request.get('reply_reference'), selection_id=request.get('selection_id'))
			if not _resume_turn and contact.last_event_at and request["event_at"] < get_datetime(contact.last_event_at):
				turn.status, turn.request_context = "Ignored", None
				turn.save(ignore_permissions=True)
				frappe.db.commit()
				return turn_result(turn)
			if session.status in (*HUMAN_OPEN, "Recovery Required", "Executing"):
				turn.status = "Human Paused" if session.status in HUMAN_OPEN else "Recovery Required"
				# A message with no dispatch can move to a new episode after the safe notice is accepted.
				if turn.status == "Recovery Required" and not turn.dispatch and not json.loads(turn.executions or "[]"):
					turn.inbox_state = "Pending"
				contact.last_activity = session.last_activity = now
				if session.status in HUMAN_OPEN:
					contact.last_event_at = session.last_event_at = max(request['event_at'], get_datetime(contact.last_event_at) if contact.last_event_at else request['event_at'])
					turn.request_context = None
				session.save(ignore_permissions=True)
				contact.save(ignore_permissions=True)
				if session.status == "Executing":
					session.status = "Recovery Required"
					session.save(ignore_permissions=True)
				turn.save(ignore_permissions=True)
				frappe.db.commit()
				return turn_result(turn)
			invalid_menu = False
			if request.get('menu_dispatch'):
				from messaging.chatbot.doctype.bot_dispatch.delivery import valid_menu
				invalid_menu = not valid_menu(request, contact, session)
			if request.get('unsupported') or invalid_menu:
				contact.last_activity = session.last_activity = now
				contact.last_event_at = session.last_event_at = max(request["event_at"], get_datetime(contact.last_event_at) if contact.last_event_at else request["event_at"])
				contact.save(ignore_permissions=True)
				turn.status, turn.request_context = "Ignored", None
				turn.save(ignore_permissions=True)
				session.save(ignore_permissions=True)
				frappe.db.commit()
				return turn_result(turn)
			watermark = session.last_event_at if _resume_turn else contact.last_event_at
			if ((watermark and request["event_at"] < get_datetime(watermark))
				or (request.get("reply_to") and request["reply_to"] != state.get("prompt_turn"))):
				turn.status, turn.request_context = "Ignored", None
				turn.save(ignore_permissions=True)
				frappe.db.commit()
				return turn_result(turn)
			settings = get_bot_settings()
			if (interpret_natural and not state.get("queue") and not session.active_flow
				and not state.get("mechanical_menu")
				and not state.get("registration_request") and not state.get("registration_pending")
				and not any((settings.interpretation_enabled, settings.registration_enabled,
					settings.greeting_enabled, settings.human_attention_enabled))
				and not mechanical_menu_flows(settings)):
				# With no configured automation, receive the channel input without a dispatch.
				turn.decisions = json.dumps([dict(kind="Configuration Required")])
				turn.status, turn.request_context = "Complete", None
				# The receipt stays visible as unanswered instead of an ordinary processed input.
				turn.inbox_diagnostic = "automation_not_configured"
				contact.last_activity = session.last_activity = now
				contact.last_event_at = session.last_event_at = request["event_at"]
				contact.save(ignore_permissions=True)
				session.save(ignore_permissions=True)
				turn.save(ignore_permissions=True)
				frappe.db.commit()
				return dict(turn_result(turn), configuration_required=True)
			try:
				settings.require_operational_configuration(channel=contact.channel)
			except frappe.ValidationError as error:
				# Configuration failures never reach capture, AI, quota reservation or Processes.
				# The administrative reason is retained so the unanswered input is never silent.
				turn.status, turn.request_context = "Complete", None
				turn.decisions = json.dumps([dict(kind="Configuration Required", reason=str(error)[:500])])
				turn.inbox_diagnostic = "configuration_required"
				turn.save(ignore_permissions=True)
				frappe.db.commit()
				return dict(turn_result(turn), configuration_required=True)
			if request.get('external_reference'):
				from messaging.conversation.doctype.chat_message.read_receipts import schedule_read_receipts
				schedule_read_receipts(contact.account, [request['external_reference']])
			state["language"] = language
			decision, hold_message = {}, None
			interpreted = False
			registration_required = bool(settings.registration_enabled and not is_registration_complete(contact, settings))
			registering = bool(registration_required)
			if state.get('queue') or session.active_flow:
				request = dict(request, flows=[], menu_selection='', human_requested=False)
			if not registering and (state.get('human_attention') or request.get('human_requested') and not (state.get('queue') or session.active_flow)):
				hold_message = request_human(session, state, request,
					dict(human_requested=request.get('human_requested')), settings, turn=turn.name)

			if registering:
				contact.registration_status = 'Pending'
				if not state.get('registration_request'):
					state['interaction_mode'] = 'Natural' if interpret_natural and settings.interpretation_enabled else 'Mechanical'
					state['registration_request'] = incoming.name
					state['registration_language'] = language
					state['registration_context'] = {key: value for key, value in request.items() if key not in ('text', 'event_at')}
					if interpreted:
						state['registration_decision'] = dict(decision=decision, hold=hold_message)
				flow = settings.registration_flow_configuration(operational=True)
				if not any(item.get('registration') for item in state['queue']):
					enqueue(state, [flow.name], settings, registration=True)
				allowed = {row.parameter for row in flow.parameters}
				request = dict(request, flows=[], menu_selection='',
					parameters={key: value for key, value in request['parameters'].items() if key in allowed})
			else:
				if state.get('registration_request'):
					request, decision, hold_message, interpreted = resume_registration_request(
						request, contact, session, state, settings, turn, interpret_natural)
				elif interpret_natural and not interpreted and not hold_message:
					from messaging.conversation.doctype.chat_turn.turn_decision import interpret_request
					request, decision, hold_message = interpret_request(request, contact, session, state, settings, turn_name=turn.name)
					interpreted = True
				elif not interpreted and not hold_message:
					from messaging.conversation.doctype.chat_turn.turn_decision import assign_configured_flows
					request = assign_configured_flows(request, settings, state, assign_human=False)
					if request.get('_assigned_human'):
						decision = dict(decision, assigned_human=True)
				if decision.get('outcome') not in ('unavailable', 'timeout', 'invalid', 'error') and not hold_message:
					hold_message = human_request_hold(session, state, request, decision, settings, turn.name)
				if not hold_message:
					enqueue(state, request['flows'], settings, menu_selection=request.get('menu_selection'))
			if interpreted:
				turn.decisions = json.dumps([decision])
			session.last_event_at = request['event_at']
			contact.last_event_at = max(request['event_at'], get_datetime(contact.last_event_at) if contact.last_event_at else request['event_at'])
			contact.last_activity = session.last_activity = now
			session.expires_at = now + timedelta(minutes=EPISODE_MINUTES)
			dispatch = reserve(contact, session, turn, settings, registration=registering,
				max_text_length=request.get('max_text_length'), now=now)
			if dispatch is None:
				if not hold_message:
					advance(contact, session, turn, state, request, settings, None, allow_execution=False)
				turn.status = 'Quota Blocked'
			else:
				turn.status, turn.dispatch = 'Processing', dispatch.name
				if dispatch.quota_kind == 'Courtesy':
					content, functional = settings_text(settings, 'quota_courtesy_message'), False
					turn.status = 'Quota Blocked'
				elif hold_message:
					content, functional = hold_message, False
					turn.status = 'Complete'
				else:
					if not registering and state.get('registration_request') and decision.get('outcome') not in ('unavailable', 'timeout', 'invalid', 'error'):
						retire_registration_request(state)
					content, functional = advance(contact, session, turn, state, request, settings, dispatch)
					if registering and state.get('registration_completed'):
						# Persist profile, receipt and confirmation together before interpreting the retained message.
						dispatch.content = content
						dispatch.save(ignore_permissions=True)
						write_state(session, 'dialogue_state', state)
						session.save(ignore_permissions=True)
						frappe.db.commit()
						lock_contact(contact.name)
						resumed_dispatch = reserve(contact, session, turn, settings, registration=False, now=now)
						if resumed_dispatch is not None:
							dispatch = resumed_dispatch
							request, decision, hold_message, interpreted = resume_registration_request(
								request, contact, session, state, settings, turn, interpret_natural)
							turn.decisions = json.dumps([*json.loads(turn.decisions), dict(kind='Registration Completed'), decision])
							if decision.get('outcome') not in ('unavailable', 'timeout', 'invalid', 'error'):
								if not hold_message:
									hold_message = human_request_hold(session, state, request, decision, settings, turn.name)
								if not hold_message:
									enqueue(state, request['flows'], settings, menu_selection=request.get('menu_selection'))
								retire_registration_request(state)
							if hold_message:
								content = '\n\n'.join(filter(None, [content, hold_message]))
							else:
								content, resumed_functional = advance(contact, session, turn, state, request, settings, dispatch, prefix=content)
								functional |= resumed_functional
					turn.status = 'Recovery Required' if session.status == 'Recovery Required' else 'Complete'
				prepare(dispatch, content, functional=functional)
				if session.status == 'Capturing':
					state['prompt_turn'] = turn.name

			write_state(session, "dialogue_state", state)
			session.save(ignore_permissions=True)
			contact.save(ignore_permissions=True)
			turn.save(ignore_permissions=True)
			frappe.db.commit()
			return turn_result(turn)
		except Exception:
			# A prior durable Running marker survives and prevents automatic replay.
			frappe.db.rollback()
			raise
		finally:
			if language_scope is not None:
				language_scope.__exit__(None, None, None)



def retire_registration_request(state):
    for key in ('registration_request', 'registration_context', 'registration_language', 'registration_decision', 'registration_completed', 'registration_pending'):
        state.pop(key, None)


def resume_registration_request(request, contact, session, state, settings, turn, interpret_natural):
    """Restore the sole canonical incoming message and reuse any completed interpretation."""
    original = frappe.get_doc('Chat Message', state['registration_request'])
    if (original.contact, original.session, original.kind) != (contact.name, session.name, 'Incoming'):
        raise frappe.ValidationError(_('The message does not belong to this contact.'))
    restored = dict(request, **{key: value for key, value in state['registration_context'].items()
        if key not in ('event_at', 'event_id', 'reply_to')}, text=original.content or '')
    cached = state.get('registration_decision')
    if cached:
        decision = cached['decision']
        if decision.get('mode') == 'Mechanical' and any(code not in {
            row['workflow_code'] for row in mechanical_menu_flows(settings)
        } for code in restored.get('flows', [])):
            restored = dict(restored, flows=[], parameters={}, text='', _mechanical_menu=True,
                _invalid_mechanical_menu=True)
            decision = dict(decision, flows=[], parameters=[], outcome='unrecognized')
        return restored, decision, cached.get('hold'), True
    if not interpret_natural:
        return restored, {}, None, False
    from messaging.conversation.doctype.chat_turn.turn_decision import interpret_request, mechanical_request
    # A completed registration is not a pending capture competing with the retained request.
    if state.get('interaction_mode') == 'Mechanical':
        restored, decision, hold = mechanical_request(restored, session, state, settings)
    else:
        restored, decision, hold = interpret_request(restored, contact, session, state, settings, turn_name=turn.name)
    if decision.get('outcome') not in ('unavailable', 'timeout', 'invalid', 'error'):
        state['registration_decision'] = dict(decision=decision, hold=hold)
        state['registration_context'] = {key: value for key, value in restored.items() if key not in ('text', 'event_at')}
    return restored, decision, hold, True


def process_message(request, *, provider=None):
	"""Private natural-message boundary; retains the original event and the native M2 transaction."""
	return process_event(request, interpret_natural=True, provider=provider)


@automatic_operation
def reconcile_execution(turn_name, execution_id, response=None, *, terminal_error=False):
	"""Trusted recovery with a verified Process Schema v1 result; never invokes business code.

	The prior response/reservation is retained for independent transport reconciliation.
	Newly queued work resumes only on a subsequent fresh event.
	"""
	from messaging.chatbot.doctype.bot_flow.bot_flow import resolve_flow_output
	from messaging.communication.doctype.process_execution_log.process_execution_log import record_process_execution_log
	from messaging.chatbot.doctype.bot_settings.language import presentation_language

	require_operator()
	contact_name = frappe.db.get_value("Chat Turn", turn_name, "contact")
	key = frappe.db.get_value("Chat Contact", contact_name, "identity_key")
	with contact_lock(key), presentation_language(
		json.loads(frappe.db.get_value("Chat Turn", turn_name, "input_summary") or "{}").get("language")
	):
		try:
			lock_contact(contact_name)
			turn = frappe.get_doc("Chat Turn", turn_name)
			executions = json.loads(turn.executions or "[]")
			execution = next((item for item in executions if item["id"] == execution_id), None)
			if not execution:
				frappe.throw(_("Execution was not found."))
			if execution["state"] not in ("Running", "Unknown"):
				return turn_result(turn)
			session = frappe.get_doc("Chat Session", turn.session)
			from messaging.conversation.doctype.chat_session.human import HUMAN_STATES
			if session.status in (*HUMAN_STATES, "Closed", "Expired"):
				frappe.db.commit()
				return turn_result(turn)
			state = read_state(session, "dialogue_state")
			item = state["queue"][0]
			if execution_key(session, item) != execution_id:
				frappe.throw(_("Execution recovery does not match the active Flow."))
			if terminal_error:
				if response is not None:
					frappe.throw(_("Terminal recovery cannot supply a functional result."))
				execution["state"] = "Failed"
				turn.executions = json.dumps(executions)
				state["technical_failure"] = dict(turn=turn.name, execution_id=execution_id)
				write_state(session, "dialogue_state", state)
				session.status = "Recovery Required"
				session.save(ignore_permissions=True)
				turn.save(ignore_permissions=True)
				if turn.dispatch:
					dispatch = frappe.get_doc("Bot Dispatch", turn.dispatch)
					if dispatch.status == "Reserved":
						from messaging.chatbot.doctype.bot_flow.bot_flow import get_flow_effective_internal_error_message
						prepare(dispatch, "\n\n".join(filter(None, [dispatch_content(dispatch),
							get_flow_effective_internal_error_message(execution["flow"])])))
				decisions = json.loads(turn.decisions or "[]")
				decisions.append(dict(kind="Technical Recovery Exhausted", execution_id=execution_id))
				record_process_execution_log(process_method=frappe.db.get_value("Bot Flow", execution["flow"], "process_method"),
					workflow=execution["flow"], session=session.name, turn=turn.name, execution_id=execution_id,
					execution_outcome="Internal Execution Error", output_mode_used="Internal Error Fallback",
					diagnostic_error_type="RecoveryExhausted", diagnostic_detail="Explicitly verified terminal recovery; no Process replay.")
				turn.decisions = json.dumps(decisions)
				turn.save(ignore_permissions=True)
				settle_technical_failure(session)
				frappe.db.commit()
				resume_pending_inputs(contact_name)
				return turn_result(frappe.get_doc("Chat Turn", turn.name))
			output = resolve_flow_output(execution["flow"], response)
			proof_fields = ['process_definition','definition_revision','runtime_revision','configuration_fingerprint']
			prior = frappe.get_all('Process Execution Log', filters=dict(execution_id=execution_id, session=session.name,
				turn=turn.name, source_type='Bot Flow', source_name=execution['flow']), fields=proof_fields,
				order_by='creation desc', limit_page_length=1)
			proof = {field: prior[0].get(field) for field in proof_fields} if prior else {}
			recovery_log = record_process_execution_log(
				process_method=frappe.db.get_value("Bot Flow", execution["flow"], "process_method"),
				workflow=execution["flow"], source_type="Bot Flow", source_name=execution["flow"],
				session=session.name, turn=turn.name, execution_id=execution_id,
				execution_outcome="Functional Status", functional_status=output["status"],
				output_mode_used=output["output_mode"], raw_response=response,
				diagnostic_detail="Verified result supplied during explicit recovery.", **proof,
			)
			execution.update(state="Complete", status=output["status"])
			flow = frappe.get_doc("Bot Flow", execution["flow"])
			decisions = json.loads(turn.decisions or "[]")
			parts, menu, _waiting = apply_flow_result(
				dict(functional=True, execution_log=recovery_log.name, text=output['rendered_message'], **output), flow, item, session, state, turn, get_bot_settings(), decisions)
			if item.get("registration"):
				contact = frappe.get_doc("Chat Contact", session.contact)
			if menu:
				state["menu_pending"] = True
			dispatch = frappe.get_doc("Bot Dispatch", turn.dispatch) if turn.dispatch else None
			if not state["queue"] and not _waiting and not item.get("registration"):
				complete_request(state)
			mechanical = state.get("interaction_mode") == "Mechanical" and not get_bot_settings().interpretation_enabled
			if not item.get("registration") and not state["queue"] and state.get("menu_pending") and dispatch and dispatch.status == "Reserved":
				parts.append(main_menu(get_bot_settings(), state, dispatch))
				decisions.append(dict(kind="Main Menu", mode="Mechanical" if mechanical else "Natural"))
				state.pop("menu_pending", None)
			turn.decisions = json.dumps(decisions)
			if dispatch:
				if dispatch.status == "Reserved":
					visible_result = '' if item.get('human_attention') and output.get('flow_action') == 'Complete Flow' and parts else output['rendered_message']
					content = "\n\n".join(part for part in [dispatch_content(dispatch), visible_result, *parts] if part)
					prepare(dispatch, content, functional=True)
			turn.status = "Complete"
			turn.executions = json.dumps(executions)
			if turn.dispatch and frappe.db.get_value("Bot Dispatch", turn.dispatch, "status") in ("Sending", "Unknown"):
				state["execution_reconciled"] = dict(turn=turn.name)
				session.status = "Recovery Required"
			write_state(session, "dialogue_state", state)
			session.save(ignore_permissions=True)
			turn.save(ignore_permissions=True)
			frappe.db.commit()
			resume_pending_inputs(contact_name)
			return turn_result(turn)
		except Exception:
			frappe.db.rollback()
			raise


def on_doctype_update():
	frappe.db.add_index("Chat Turn", ["contact", "received_at"], "contact_history")
	frappe.db.add_index("Chat Turn", ["contact", "receipt_order"], "contact_receipt_order")
	frappe.db.add_index("Chat Turn", ["contact", "inbox_state", "receipt_order"], "contact_inbox")
	frappe.db.add_index("Chat Turn", ["inbox_state", "modified", "contact"], "inbox_recovery")
