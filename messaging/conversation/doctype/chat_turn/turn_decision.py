"""Adapt validated Chatbot proposals to the existing conversation engine."""

from messaging.chatbot.doctype.bot_settings.localization import text as settings_text

import json
from dataclasses import asdict

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_settings.bot_settings import (
	compose_informative_response,
	get_menu_flows,
	get_internal_error_message,
)
from messaging.chatbot.doctype.bot_settings.interpretation import (
	TECHNICAL_OUTCOMES,
	Decision,
	InvalidInterpretation,
	ProviderUnavailable,
	catalog,
	interpret,
	validate_decision,
)
from messaging.conversation.doctype.chat_session.chat_session import main_menu, read_state, mechanical_menu_options
from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	capture, capture_context, valid_values, recognize, typed_answer, get_handler,
)


def pending_capture(contact, session, state, settings):
	"""Return only the current owner's capture definition, reusing native capture semantics."""
	if session.active_flow and state.get("queue"):
		flow = frappe.get_doc("Bot Flow", session.active_flow)
		item = state["queue"][0]
		available = {**read_state(contact, "parameter_values"), **state.get("values", {}), **item.get("values", {})}
		for code in item.get("recapture", []):
			if code not in item.get("values", {}):
				available.pop(code, None)
		return (
			[frappe._dict(row.as_dict(), required=1) if row.parameter in item.get("recapture", []) else row for row in flow.parameters],
			available,
			False,
		)
	return [], {}, False


def last_recognized_flows(session, flows):
	"""Use one completed turn in this episode, without retrieving text, Outputs or other contacts."""
	previous = frappe.get_all(
		"Chat Turn",
		filters={"session": session.name, "status": "Complete"},
		fields=["decisions"],
		order_by="received_at desc",
		limit_page_length=1,
	)
	if not previous:
		return []
	result = []
	for item in json.loads(previous[0].decisions or "[]"):
		for code in item.get("flows", []):
			if code in flows and code not in result:
				result.append(code)
	return result


def interpret_request(request, contact, session, state, settings, *, turn_name=None):
	"""Run under the existing contact lock, after duplicate, paused and delayed event checks.

	Return a native request and an optional controlled hold message. No Process is run here.
	Raw provider responses, input text and candidate values never enter turn diagnostics.
	"""
	# Assigned work owns subsequent text, regardless of its type or validity.
	# Every continuation belongs to capture, including incidental selections.
	if state.get("queue") or session.active_flow:
		decision = Decision(outcome="continuation")
		summary = asdict(decision)
		summary.update(kind="Interpretation", parameters=[], error_log=None)
		return dict(request, flows=[], menu_selection="", human_requested=False), summary, None
	from messaging.chatbot.doctype.ai_configuration.ai_configuration import availability
	ai_available = bool(settings.interpretation_enabled and availability(settings)["operational"])
	if (not ai_available or state.get("mechanical_menu") and request.get("text", "").strip().isascii()
		and request.get("text", "").strip().isdecimal()) and not request.get("flows") and not request.get("menu_selection"):
		return mechanical_request(request, session, state, settings, ai_available=ai_available)
	if not state.get("queue") and not session.active_flow:
		state["interaction_mode"] = "Natural"
		state.pop("mechanical_menu", None)
	text = request.get("text", "")
	available = {}
	decision = Decision()
	error_log = None
	explicit_capture_text = ""
	registering = False
	try:
		rows, available, registering = pending_capture(contact, session, state, settings)
		if state.get('registration_request') and state.get('registration_completed') and not rows:
			# Keep the actual Flow input definitions available for local redaction of
			# the retained message, without reopening capture or storing another profile.
			flow = settings.registration_flow_configuration(operational=True)
			rows = flow.parameters
			available = read_state(contact, 'parameter_values')
		allowed_active = {}
		for row in rows:
			parameter = frappe.get_doc("Bot Parameter", row.parameter)
			parameter.check_permission("read")
			if parameter.enabled:
				allowed_active[parameter.name] = parameter
		flows = catalog()
		explicit = list(request["flows"])
		selection = request.get("menu_selection")
		if selection:
			if selection not in {item["workflow_code"] for item in get_menu_flows(settings)}:
				raise InvalidInterpretation("Invalid explicit menu selection")
			explicit.append(selection)
		# Exact canonical identifiers are explicit selections, even when hidden from the menu.
		if not explicit and text.strip() in flows:
			explicit = [text.strip()]
		if explicit:
			if any(code not in flows for code in explicit):
				raise InvalidInterpretation("Unavailable explicit Flow")
			decision.flows = list(dict.fromkeys(explicit))
			decision.outcome = "explicit"
			allowed = dict(allowed_active)
			for code in decision.flows:
				allowed.update(flows[code]["parameters"])
			if set(request["parameters"]) - set(allowed):
				raise InvalidInterpretation("Unauthorized explicit Parameter")
			decision.parameters = valid_values(request["parameters"], allowed)
			labels = {flows[code]["name"] for code in decision.flows} | set(decision.flows)
			labels.update(row["label"] for row in get_menu_flows(settings) if row["workflow_code"] in decision.flows)
			if text.strip() not in labels and (typed_answer(text) or recognize(text, allowed)[1]):
				explicit_capture_text = text
			# Structured selections are already explicit; candidates still pass the native validators.
			validate_decision(
				asdict(decision),
				flows,
				allowed,
				text + " " + " ".join(str(v) for value in decision.parameters.values() for v in (value if isinstance(value, list) else [value])),
				multiple_parameters={row.parameter for row in rows if row.get("allow_multiple_values")},
			)
		else:
			if request["parameters"]:
				raise InvalidInterpretation("Natural input cannot predeclare a semantic decision")

			if decision.outcome != "continuation":
				context = dict(previously_recognized_flows=last_recognized_flows(session, flows))
				decision = interpret(text, context, flows, allowed_active, settings, turn_name=turn_name)
				if decision.flows and not decision.ambiguous:
					allowed = {key: parameter for code in decision.flows
						for key, parameter in flows[code]["parameters"].items()}
					known = {**allowed_active, **{key: parameter for flow in flows.values()
						for key, parameter in flow["parameters"].items()}}
					multiple = {key for code in decision.flows for key in flows[code]["multiple_parameters"]}
					_values, rejected, _occurrences = capture_context(
						text, allowed, known_parameters=known, multiple=multiple)
					# Native syntax and validators also apply to optional values in
					# an initial request; a malformed token cannot mean omission.
					invalid_tokens = any(not get_handler(parameter.parameter_type).validate(parameter, value)[0]
						for parameter in allowed.values()
						for _start, _end, value in get_handler(parameter.parameter_type).extract(parameter, text).spans)
					if rejected or invalid_tokens:
						decision = Decision(ambiguous=True, outcome="ambiguous", metrics=decision.metrics,
							greeting_detected=decision.greeting_detected, human_attention_requested=decision.human_attention_requested)
				# An old validated value must not silently answer a request for another entity.
				if any(
					code in available and available[code] != value
					for code, value in decision.parameters.items()
				):
						decision = Decision(ambiguous=True, outcome="ambiguous", metrics=decision.metrics,
							greeting_detected=decision.greeting_detected, human_attention_requested=decision.human_attention_requested)
	except Exception as exc:
		outcome = (
			"timeout"
			if isinstance(exc, TimeoutError)
			else "unavailable"
			if isinstance(exc, ProviderUnavailable)
			else "invalid"
			if isinstance(exc, InvalidInterpretation)
			else "error"
		)
		decision = Decision(outcome=outcome)
		# Insert a native Error Log without collecting request/job arguments or exception context.
		log = frappe.get_doc(
			dict(
				doctype="Error Log",
				method="Messaging Interpretation Error",
				error="Interpretation failed: "
				+ outcome
				+ ". Input, credentials and provider details omitted.",
				metadata="{}",
				reference_doctype="Chat Turn",
				reference_name=turn_name,
			)
		).insert(ignore_permissions=True)
		error_log = log.name if log else None
	queued = {item["flow"] for item in state.get("queue", [])}
	suspended = (set(state.get("suspended_flows", [])) | (queued & set(decision.excluded_flows))) - set(
		decision.flows
	)
	if suspended:
		state["suspended_flows"] = sorted(suspended)
	else:
		state.pop("suspended_flows", None)
	selected_flows = list(decision.flows)
	human_requested = decision.human_attention_requested and bool(settings.human_attention_enabled)
	if decision.greeting_detected and settings.greeting_enabled:
		selected_flows.insert(0, settings.greeting_flow)
	adapted = assign_configured_flows({
		**request,
		"_assigned_human": human_requested,
		"flows": selected_flows,
		"parameters": decision.parameters,
		"menu_selection": "",
		"text": text if decision.outcome == "continuation" else explicit_capture_text,
	}, settings, state)

	hold = None
	if decision.outcome in TECHNICAL_OUTCOMES:
		hold = get_internal_error_message()
	elif not human_requested and (decision.ambiguous or state.get("suspended_flows")):
		parts = [_("Please clarify your request or choose an available option."), compose_informative_response(settings)]
		if settings.informative_show_available_categories:
			parts.append(main_menu(settings, bind=False))
		hold = "\n\n".join(part for part in parts if part)
	elif (
		decision.outcome not in ("continuation", "explicit")
		and not decision.flows
		and not decision.parameters
		and state.get("queue")
	):
		hold = compose_informative_response(settings)
	if hold and decision.greeting_detected and settings.greeting_enabled:
		adapted["_recognition_hold"] = hold
		state.pop("menu_after_greeting", None)
		hold = None
	summary = asdict(decision)
	summary["parameters"] = list(decision.parameters)
	summary.update(kind="Interpretation", error_log=error_log)
	if adapted.get("_assigned_human"):
		summary["assigned_human"] = True
	return adapted, summary, hold


def assign_configured_flows(request, settings, state, *, assign_human=True):
	"""Map configured assignments, preserving ordinary Human Flow calls in structured requests."""
	request = dict(request)
	selected = [code for code in list(request.get("flows") or []) + [request.get("menu_selection") or ""] if code]
	human = settings.get("human_flow") if assign_human else None
	human_selected = bool(request.get("_assigned_human") or (human and human in selected))
	if human and human in (request.get("flows") or []):
		request["flows"] = [code for code in request["flows"] if code != human]
	if human and request.get("menu_selection") == human:
		request["menu_selection"] = ""
	if human_selected:
		request["_assigned_human"] = True
	greeting = settings.get("greeting_flow") if settings.greeting_enabled else None
	sole_greeting = (
		bool(greeting) and not human_selected and not request.get("human_requested")
		and int(settings.show_menu_on_greeting_only or 0) and set(selected) == {greeting}
		and not any(item['flow'] != greeting for item in state.get('queue', []))
	)
	if sole_greeting:
		state["menu_after_greeting"] = True
	else:
		state.pop("menu_after_greeting", None)
	return request


def mechanical_request(request, session, state, settings, *, ai_available=False):
	"""Resolve only numbers from this episode's prepared and accepted text menu."""
	state["interaction_mode"] = "Natural" if ai_available else "Mechanical"
	menu = state.get("mechanical_menu")
	text = request.get("text", "").strip()
	option = menu["options"].get(text) if menu else None
	selected = None
	human = False
	if option:
		receipt = frappe.db.get_value("Bot Dispatch", menu["dispatch"], ["session", "status"], as_dict=True)
		if receipt and receipt.session == session.name and receipt.status == "Accepted":
			if option["flow"] in mechanical_menu_options(settings):
				if option.get("action") == "Human":
					human = True
				else:
					selected = option["flow"]
	decision = Decision(flows=[selected] if selected else [], outcome="explicit" if selected or human else "menu")
	summary = dict(asdict(decision), kind="Interpretation", mode=state["interaction_mode"], parameters=[], error_log=None)
	adapted = dict(request, flows=decision.flows, parameters={}, text="", menu_selection="")
	if human or request.get("human_requested"):
		adapted["_assigned_human"] = True
		summary["assigned_human"] = True
	if selected or human:
		state.pop("mechanical_menu", None)
	else:
		if not menu and settings.greeting_enabled:
			adapted["flows"] = [settings.greeting_flow]
		adapted.update(_mechanical_menu=True, _invalid_mechanical_menu=bool(menu) and text != "0",
			_refresh_mechanical_menu=text == "0")
	return adapted, summary, None
