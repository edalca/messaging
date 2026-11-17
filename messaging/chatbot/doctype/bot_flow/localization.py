"""Localized administrative text over one technical Flow contract."""
import json

import frappe
from frappe import _

from messaging.chatbot.doctype.bot_settings.language import (
	get_bot_settings,
	resolve_process_language,
	validate_chatbot_languages,
)
from messaging.communication.doctype.communication_template.communication_template import (
	validate_template_fields,
)

TEXT_FIELDS = ("semantic_description", "menu_label", "menu_description", "internal_error_message")
INPUT_FIELDS = ("input_initial_message", "input_continuation_message")
TRANSLATION_TABLES = {
	"translations": (None, TEXT_FIELDS),
	"input_translations": (None, INPUT_FIELDS),
	"output_translations": ("status_code", ("template_reference",)),
}


def required_text_fields(flow):
	"""Use the same conditional text contract for saves, activation and Desk hints."""
	fields = ["semantic_description"]
	if flow.show_in_menu:
		fields.append("menu_label")
	contract = json.loads(flow.get("process_contract") or "{}")
	if flow.parameters or contract.get("parameters"):
		fields.extend(INPUT_FIELDS)
	if flow.use_custom_internal_error_message:
		fields.append("internal_error_message")
	return fields


def default_language_issues(flow, language, *, settings=None):
	"""Inspect exact default-language rows, without runtime presentation fallback."""
	from messaging.chatbot.doctype.bot_parameter.bot_parameter import get_request_phrase

	if not language:
		return [_("Configure an enabled default language in Bot Settings before creating a Bot Flow.")]
	issues = []
	sections = {
		"semantic_description": "Flow Information",
		"menu_label": "Menu or Catalog",
		"input_initial_message": "Input Parameters",
		"input_continuation_message": "Input Parameters",
		"internal_error_message": "Internal Error Response",
	}
	for field in required_text_fields(flow):
		table = "input_translations" if field in INPUT_FIELDS else "translations"
		row = next((r for r in flow.get(table) or [] if r.language == language), None)
		if not row or not (row.get(field) or "").strip():
			meta = frappe.get_meta("Flow Input Translation" if field in INPUT_FIELDS else "Flow Translation")
			issues.append(_("Flow {0}, language {1}: {2} / {3} is required.").format(
				flow.workflow_name or flow.name, language, _(sections[field]), _(meta.get_field(field).label)))
	for output_row in flow.outputs or []:
		if output_row.output_mode != "Template":
			continue
		translation = next((r for r in flow.output_translations or []
			if r.language == language and r.status_code == output_row.status_code), None)
		try:
			if not translation or not translation.template_reference:
				raise frappe.ValidationError(_("Bot Flow Template is required."))
			validate_template_fields(translation.template_reference, json.loads(output_row.output_fields),
				language=language, active=True, usage="Bot Flow", account=flow.get("account"))
		except frappe.ValidationError as error:
			issues.append(_("Flow {0}, language {1}: Functional Outputs / {2}: {3}").format(
				flow.workflow_name or flow.name, language, output_row.status_code, str(error)))
	for row in flow.parameters or []:
		if not row.parameter:
			continue
		try:
			get_request_phrase(row.parameter, language, settings=settings)
		except frappe.ValidationError as error:
			issues.append(_("Flow {0}, language {1}: Input Parameters / {2}: {3}").format(
				flow.workflow_name or flow.name, language, row.parameter, str(error)))
	return issues


def require_default_language(flow, *, settings=None):
	settings = settings if settings is not None else get_bot_settings(shared=True)
	language = validate_chatbot_languages(settings)
	issues = default_language_issues(flow, language, settings=settings)
	if issues:
		frappe.throw("\n".join(issues))


def text(flow, field, language=None):
	settings = get_bot_settings()
	default = validate_chatbot_languages(settings)
	if not default:
		return ""
	language = language or resolve_process_language()
	enabled = {r.language for r in settings.chatbot_languages if r.enabled}
	table = "input_translations" if field in INPUT_FIELDS else "translations"
	for code in dict.fromkeys((language, default)):
		if code in enabled:
			row = next((r for r in flow.get(table) or [] if r.language == code), None)
			value = row.get(field) if row else None
			if value and value.strip():
				return value
	return ""


def output(flow, row, language=None):
	settings = get_bot_settings()
	default = validate_chatbot_languages(settings)
	language = language or resolve_process_language()
	if row.output_mode == "Message":
		return dict(language=language, template_reference=None)
	enabled = {r.language for r in settings.chatbot_languages if r.enabled}
	for code in dict.fromkeys((language, default)):
		if code not in enabled:
			continue
		translated = next(
			(
				r
				for r in flow.get("output_translations") or []
				if r.language == code and r.status_code == row.status_code
			),
			None,
		)
		if translated and translated.template_reference:
			template = frappe.get_doc("Bot Flow Template", translated.template_reference)
			if template.enabled and template.language == code:
				return dict(language=code, template_reference=translated.template_reference)
	raise frappe.ValidationError(
		_("Configure an enabled output template for the contact language or the default language.")
	)


def validate(flow):
	settings = get_bot_settings()
	validate_chatbot_languages(settings)
	enabled = {r.language for r in settings.chatbot_languages if r.enabled}
	outputs = {r.status_code: r for r in flow.outputs}
	previous = None if flow.is_new() else frappe.get_doc(flow.doctype, flow.name)
	for table, (key, fields) in TRANSLATION_TABLES.items():
		history = {r.name: r for r in previous.get(table) or []} if previous else {}
		seen = set()
		for row in flow.get(table) or []:
			identity = (row.language, row.get(key) if key else None)
			old = history.get(row.name)
			retained = old and old.language == row.language and (not key or old.get(key) == row.get(key))
			if not row.language or identity in seen or (row.language not in enabled and not retained):
				raise frappe.ValidationError(_("Select a unique enabled language for each translation."))
			seen.add(identity)
			if table == "output_translations":
				if row.status_code not in outputs:
					raise frappe.ValidationError(_("The translated output must reference a declared status."))
				base = outputs[row.status_code]
				if base.output_mode == "Template":
					if not row.template_reference:
						# Additional languages may remain incomplete; runtime falls back.
						continue
					validate_template_fields(
						row.template_reference,
						json.loads(base.output_fields),
						language=row.language,
						active=(row.language in enabled),
						usage="Bot Flow",
						account=flow.get("account"),
					)
				elif not retained or any(row.get(f) != old.get(f) for f in fields):
					raise frappe.ValidationError(_("Message outputs use only the business Process message."))


def missing_default_language(definition, doc, code):
	if not doc:
		return True
	if not any(row.language == code for row in doc.translations):
		return True
	if set(definition['outcomes']) - {row.status_code for row in doc.outputs}:
		return True
	if doc.parameters and not any(row.language == code for row in doc.input_translations):
		return True
	for row in doc.outputs:
		if (
			row.output_mode == "Template"
			and definition["outcomes"].get(row.status_code, {}).get("template")
			and not any(
				r.language == code and r.status_code == row.status_code for r in doc.output_translations
			)
		):
			return True
	for item in definition.get("inputs", {}).values():
		parameter = item["parameter"]
		name = parameter["code"] if isinstance(parameter, dict) else parameter
		current = next((r for r in doc.parameters if r.parameter == name), None)
		if (
			current
			and isinstance(parameter, dict)
			and code in parameter["languages"]
			and not frappe.db.exists(
				"Bot Parameter Translation",
				{"parent": name, "parenttype": "Bot Parameter", "language": code},
			)
		):
			return True
	return False
