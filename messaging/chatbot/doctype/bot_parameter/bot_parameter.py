import contextvars
import hashlib
import hmac
import json
import re

from messaging.communication.doctype.process_definition import approval
from messaging.chatbot.doctype.bot_parameter.parameters import get_handler, get_supported_types
from messaging.chatbot.doctype.bot_parameter.parameters.engine import (
	Occurrence,
	capture,
	capture_context,
	capture_text,
	captured_values,
	public_marker,
	recognize,
	typed_answer,
	valid_values,
	validate_input_limits,
	validate_input_value,
	validate_parameter_value,
	validate_parameter_values,
)
from messaging.chatbot.doctype.bot_parameter.parameters.expression import (
	compile_expression,
	extract_expression_values,
)
from messaging.chatbot.doctype.bot_parameter.parameters.name import extract_name
from messaging.security.permissions import is_system_manager, require_system_manager

import frappe
from frappe import _
from frappe.model.document import Document

from .definition import validate_definition
WRITABLE_FIELDS = (
	"label", "enabled", "parameter_type", "expression", "string_mode",
	"allow_contextual_name_extraction", "allow_ai_extraction", "description",
	"min_length", "max_length", "min_value", "max_value", "request_phrases",
)
IMPACT_FIELDS = (
	"parameter_type", "expression", "enabled", "string_mode", "min_length", "max_length",
	"min_value", "max_value", "allow_contextual_name_extraction", "label", "description",
)
CHECK_FIELDS = {"enabled", "allow_contextual_name_extraction", "allow_ai_extraction"}
INT_FIELDS = {"min_length", "max_length", "min_value", "max_value"}
# Direct saves stay blocked. Only apply_referenced_update sets this token.
_AUTHORIZATION = contextvars.ContextVar("bot_parameter_referenced_update", default=None)


class BotParameter(Document):
	def before_validate(self) -> None:
		if self.label:
			self.label = self.label.strip()

	def validate(self) -> None:
		validate_definition(self)
		self._validate_request_phrases()
		self._protect_referenced_parameter_changes()

	def _validate_request_phrases(self):
		from messaging.chatbot.doctype.bot_settings.language import get_bot_settings, validate_chatbot_languages
		from messaging.communication.doctype.communication_template.communication_template import validate_template_syntax
		settings = get_bot_settings()
		validate_chatbot_languages(settings)
		allowed = {row.language for row in settings.chatbot_languages if row.enabled}
		configured = {row.language for row in settings.chatbot_languages}
		previous = self.get_doc_before_save()
		prior_rows = {row.name: row for row in previous.request_phrases or []} if previous else {}
		seen = set()
		for row in self.request_phrases or []:
			old = prior_rows.get(row.name)
			retained = old and old.language == row.language and old.request_phrase == row.request_phrase
			if row.language not in configured or (row.language not in allowed and not retained):
				frappe.throw(_("Request phrases must use enabled Chatbot Languages from Bot Settings."))
			if row.language in seen:
				frappe.throw(_("Each parameter must have at most one request phrase per language."))
			seen.add(row.language)
			value = row.request_phrase or ''
			if len(value.encode('utf-8')) > 65535:
				frappe.throw(_("Localized message exceeds its maximum length."))
			validate_template_syntax(value, allow_empty=True, allowed_variables=set())
		# A text edit changes presentation only, but cannot remove a live dependency.
		if not self.is_new():
			for consumer in _consumer_rows(self.name):
				owner = frappe.get_doc(consumer['doctype'], consumer['name'])
				if owner.status == 'Enabled':
					get_request_phrase(self, settings=settings)

	def on_trash(self) -> None:
		references = get_parameter_references(self.name)
		if references:
			ref_list = ", ".join(references)
			frappe.throw(
				_("Cannot delete Parameter '{0}' because it is referenced by: {1}.").format(
					self.name, ref_list
				),
				frappe.ValidationError,
			)

	def before_rename(self, old, new, merge=False):
		frappe.throw(_("Parameter Name is immutable."), frappe.ValidationError)

	def _protect_referenced_parameter_changes(self) -> None:
		if self.is_new() or _update_is_authorized(self):
			return
		consumers = _consumer_rows(self.name)
		if not consumers:
			return
		changes = _impact_changes(_value_map(self), _persisted_map(self.name))
		if not changes:
			return
		ref_summary = ", ".join(get_parameter_references(self.name))
		field = changes[0]
		if field == "parameter_type":
			frappe.throw(
				_("Cannot change Parameter Type of '{0}' because it is already referenced by: {1}.").format(
					self.name, ref_summary
				),
				frappe.ValidationError,
			)
		if field == "expression":
			frappe.throw(
				_("Cannot change Expression of '{0}' because it is already referenced by: {1}.").format(
					self.name, ref_summary
				),
				frappe.ValidationError,
			)
		if field == "enabled" and not int(self.enabled or 0):
			frappe.throw(
				_("Cannot disable Parameter '{0}' while it is referenced by: {1}.").format(
					self.name, ref_summary
				),
				frappe.ValidationError,
			)
		label = frappe.get_meta("Bot Parameter").get_label(field)
		frappe.throw(
			_("Cannot change {0} of '{1}' because it is already referenced by: {2}.").format(
				_(label), self.name, ref_summary
			),
			frappe.ValidationError,
		)


def on_doctype_update():
	"""Synchronize installed Select metadata from the native registry at model sync."""
	frappe.db.set_value(
		"DocField",
		{"parent": "Bot Parameter", "fieldname": "parameter_type"},
		"options",
		"\n".join(get_supported_types()),
		update_modified=False,
	)
	frappe.clear_cache(doctype="Bot Parameter")


def get_parameter_references(parameter_name: str) -> list[str]:
	"""Return human-readable references to a Bot Parameter across Bot Flows and Bot Settings."""
	references: list[str] = []
	for row in _consumer_rows(parameter_name):
		if row["doctype"] == "Bot Flow":
			references.append(_("Bot Flow ({0})").format(row["name"]))
		else:
			references.append(_("Bot Settings (Registration)"))
	return references


def _canonical(field: str, value):
	if field == 'request_phrases':
		if not isinstance(value, (list, tuple)) or any(not hasattr(row, 'get')
			or not isinstance(row.get('language'), str) or not isinstance(row.get('request_phrase') or '', str) for row in value):
			frappe.throw(_("Invalid parameter update."))
		return [dict(language=row.get('language'), request_phrase=row.get('request_phrase') or '') for row in value]
	if field in CHECK_FIELDS:
		return int(value or 0)
	if field in INT_FIELDS:
		if value in (None, ""):
			return None
		if isinstance(value, bool):
			frappe.throw(_("Invalid parameter update."), frappe.ValidationError)
		return int(value)
	if value in (None, ""):
		return ""
	return str(value)


def _value_map(doc) -> dict:
	return {field: _canonical(field, doc.get(field)) for field in WRITABLE_FIELDS}


def _persisted_map(name: str) -> dict:
	fields = [field for field in WRITABLE_FIELDS if field != 'request_phrases']
	row = frappe.db.get_value("Bot Parameter", name, fields, as_dict=True) or {}
	row['request_phrases'] = frappe.get_doc('Bot Parameter', name).request_phrases
	return {field: _canonical(field, row.get(field)) for field in WRITABLE_FIELDS}


def _impact_changes(proposed: dict, persisted: dict) -> list[str]:
	return [field for field in IMPACT_FIELDS if proposed.get(field) != persisted.get(field)]


def _linked_names(child_doctype: str, parenttype: str, parameter: str) -> list[str]:
	if not frappe.db.table_exists(child_doctype):
		return []
	names = frappe.db.get_all(
		child_doctype,
		filters={"parameter": parameter, "parenttype": parenttype},
		pluck="parent",
		distinct=True,
	)
	return sorted(set(names))


def _consumer_rows(parameter: str) -> list[dict]:
	"""Real Link rows, including disabled flows. Permission filters cannot hide a consumer."""
	rows = []
	for name in _linked_names("Flow Parameter", "Bot Flow", parameter):
		modified, status, code = frappe.db.get_value(
			"Bot Flow", name, ("modified", "status", "workflow_code")
		) or (None, 0, name)
		rows.append(dict(
			doctype="Bot Flow", name=name, label=code or name,
			modified=str(modified or ""), status=status,
		))
	return rows


def _dependency_token(name: str, consumers: list[dict], values: dict) -> str:
	modified = frappe.db.get_value("Bot Parameter", name, "modified")
	payload = {
		"parameter": name,
		"modified": str(modified or ""),
		"consumers": [
			{"doctype": row["doctype"], "name": row["name"], "modified": str(row["modified"] or "")}
			for row in consumers
		],
		"values": values,
	}
	raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
	return hashlib.sha256(raw).hexdigest()


def _same_token(provided, expected: str) -> bool:
	if not isinstance(provided, str) or len(provided) != len(expected):
		return False
	return hmac.compare_digest(provided, expected)


def _update_is_authorized(doc) -> bool:
	granted = _AUTHORIZATION.get()
	if not granted or granted[0] != doc.name:
		return False
	current = _dependency_token(doc.name, _consumer_rows(doc.name), _value_map(doc))
	return _same_token(granted[1], current)


def _parse_values(values) -> dict:
	if values in (None, ""):
		return {}
	if isinstance(values, str):
		values = frappe.parse_json(values)
	if not isinstance(values, dict) or any(not isinstance(field, str) for field in values):
		frappe.throw(_("Invalid parameter update."), frappe.ValidationError)
	unknown = set(values) - set(WRITABLE_FIELDS)
	if unknown:
		frappe.throw(_("Invalid parameter update."), frappe.ValidationError)
	return values


def _apply_values(doc, values: dict) -> None:
	for field, value in values.items():
		if field == 'request_phrases':
			doc.set(field, _canonical(field, value))
			continue
		if field in INT_FIELDS and value not in (None, "") and isinstance(value, str):
			if not re.fullmatch(r"-?\d+", value):
				frappe.throw(_("Invalid parameter update."), frappe.ValidationError)
		doc.set(field, None if field in INT_FIELDS and value in (None, "") else value)
	if doc.label:
		doc.label = doc.label.strip()


def _require_consumer_access(row: dict) -> None:
	if not frappe.has_permission(row["doctype"], "read", row["name"]) or not frappe.has_permission(
		row["doctype"], "write", row["name"]
	):
		frappe.throw(
			_("You do not have permission to update the related flows."),
			frappe.PermissionError,
		)


def _lock_consumers(parameter: str, consumers: list[dict]) -> None:
	frappe.db.get_value("Bot Parameter", parameter, "name", for_update=True)
	for row in consumers:
		if row["doctype"] == "Bot Flow":
			frappe.db.get_value("Bot Flow", row["name"], "name", for_update=True)
		else:
			frappe.db.get_value("DocType", "Bot Settings", "name", for_update=True)


def _public_consumer(row: dict) -> dict:
	outputs = [
		{"output": item.get("output"), "status_code": item.get("status_code"), "status": item.get("status")}
		for item in row.get("outputs") or []
	]
	return {
		"doctype": row["doctype"],
		"name": row["name"],
		"label": row["label"],
		"status": row.get("status"),
		"modified": str(row.get("modified") or ""),
		"structural": row.get("structural"),
		"structural_message": row.get("structural_message") or "",
		"approval": row.get("approval"),
		"evidence": row.get("evidence"),
		"outputs": outputs,
	}


def _operation_result(name: str, *, applied: bool, unchanged: bool, consumers: list[dict]) -> dict:
	flows = [row for row in consumers if row["doctype"] == "Bot Flow"]
	def accepted(row):
		return row.get("structural") == "valid" and row.get("approval") == "Passed"
	validated = [row["label"] for row in flows if applied and accepted(row)]
	needs = []
	for row in flows:
		if not applied and row.get("structural") == "incompatible":
			needs.append(row["label"])
		elif applied and not accepted(row):
			needs.append(row["label"])
	return {
		"applied": applied,
		"unchanged": unchanged,
		"parameter": name,
		"affected": 0 if unchanged else len(flows),
		"validated": validated,
		"needs_correction": needs,
		"consumers": [_public_consumer(row) for row in consumers],
	}


def _prepare(name: str, values):
	if not isinstance(name, str) or not name:
		frappe.throw(_("Invalid parameter update."), frappe.ValidationError)
	doc = frappe.get_doc("Bot Parameter", name)
	doc.check_permission("read")
	doc.check_permission("write")
	_apply_values(doc, _parse_values(values))
	return doc


@frappe.whitelist(methods=["POST"])
def preview_referenced_update(name, values=None):
	"""Return the real consumers and a confirmation token without writing."""
	doc = _prepare(name, values)
	consumers = _consumer_rows(doc.name)
	for row in consumers:
		_require_consumer_access(row)
	proposed = _value_map(doc)
	changes = _impact_changes(proposed, _persisted_map(doc.name))
	required = bool(changes and consumers)
	return {
		"confirmation_required": required,
		"changes": changes,
		"dependency_token": _dependency_token(doc.name, consumers, proposed) if required else None,
		"consumers": [_public_consumer(row) for row in consumers],
	}


def _assess_structure(consumers: list[dict]) -> list[dict]:
	muted = frappe.flags.mute_messages
	frappe.flags.mute_messages = True
	results = []
	try:
		for row in consumers:
			item = dict(row)
			try:
				if row["doctype"] == "Bot Settings":
					copy = frappe.get_doc(frappe.get_doc("Bot Settings").as_dict())
					copy._validate_registration()
				else:
					flow = frappe.get_doc("Bot Flow", row["name"])
					flow.validate_configuration()
				item["structural"] = "valid"
				item["structural_message"] = ""
			except frappe.ValidationError as exc:
				item["structural"] = "incompatible"
				item["structural_message"] = str(exc)
			finally:
				if row["doctype"] == "Bot Flow":
					frappe.clear_document_cache("Bot Flow", row["name"])
			results.append(item)
	finally:
		frappe.flags.mute_messages = muted
		frappe.clear_last_message()
		if hasattr(frappe.local, "message_log"):
			frappe.local.message_log = []
	return results


def _attach_approval(item: dict) -> None:
	if item["doctype"] != "Bot Flow":
		item["approval"] = None
		item["evidence"] = "not_applicable"
		item["outputs"] = []
		return
	flow = frappe.get_doc("Bot Flow", item["name"])
	item["status"] = flow.status
	item["approval"] = "Passed" if approval.is_current(flow) else "Untested"
	item["evidence"] = "current" if item["approval"] == "Passed" else "pending"
	item["outputs"] = [
		dict(status_code=row.status_code, status=item["approval"]) for row in flow.outputs
	]
	frappe.clear_document_cache("Bot Flow", item["name"])


def _commit_confirmed(doc, consumers: list[dict], token: str) -> dict:
	savepoint = "param_update_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	authorisation = _AUTHORIZATION.set((doc.name, token))
	try:
		doc.save()
		assessed = _assess_structure(consumers)
		if any(item["structural"] == "incompatible" for item in assessed):
			frappe.db.rollback(save_point=savepoint)
			frappe.clear_document_cache("Bot Parameter", doc.name)
			for item in assessed:
				_attach_approval(item)
			return _operation_result(doc.name, applied=False, unchanged=False, consumers=assessed)
		for item in assessed:
			if item["doctype"] == "Bot Flow":
				flow = frappe.get_doc("Bot Flow", item["name"], for_update=True)
				approval.dependency_changed(flow, "parameter_contract_changed")
			_attach_approval(item)
		return _operation_result(doc.name, applied=True, unchanged=False, consumers=assessed)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		frappe.clear_document_cache("Bot Parameter", doc.name)
		raise
	finally:
		_AUTHORIZATION.reset(authorisation)


@frappe.whitelist(methods=["POST"])
def apply_referenced_update(name, values=None, dependency_token=None):
	"""Save one confirmed parameter revision and revalidate its real consumers."""
	doc = _prepare(name, values)
	_lock_consumers(doc.name, _consumer_rows(doc.name))
	proposed = _value_map(doc)
	if proposed == _persisted_map(doc.name):
		return _operation_result(doc.name, applied=True, unchanged=True, consumers=[])
	consumers = _consumer_rows(doc.name)
	for row in consumers:
		_require_consumer_access(row)
	_lock_consumers(doc.name, consumers)
	consumers = _consumer_rows(doc.name)
	for row in consumers:
		_require_consumer_access(row)
	proposed = _value_map(doc)
	if proposed == _persisted_map(doc.name):
		return _operation_result(doc.name, applied=True, unchanged=True, consumers=[])
	changes = _impact_changes(proposed, _persisted_map(doc.name))
	if not changes or not consumers:
		doc.save()
		saved = _consumer_rows(doc.name) if changes else []
		for item in saved:
			_attach_approval(item)
		return _operation_result(doc.name, applied=True, unchanged=False, consumers=saved)
	token = _dependency_token(doc.name, consumers, proposed)
	if not _same_token(dependency_token, token):
		frappe.throw(
			_("The related flows changed. Review the update and confirm it again."),
			frappe.ValidationError,
		)
	return _commit_confirmed(doc, consumers, token)


def get_request_phrase(parameter, language=None, *, settings=None):
	"""Resolve a reusable request in the effective language or the enabled default."""
	from messaging.chatbot.doctype.bot_settings.language import current_language, get_bot_settings, validate_chatbot_languages
	settings = settings if settings is not None else get_bot_settings()
	default = validate_chatbot_languages(settings)
	if not default:
		frappe.throw(_("Configure a default Chatbot Language before using parameters."))
	doc = frappe.get_doc('Bot Parameter', parameter) if isinstance(parameter, str) else parameter
	enabled = {row.language for row in settings.chatbot_languages if row.enabled}
	default_row = next((row for row in doc.get('request_phrases') or [] if row.language == default), None)
	if not default_row or not (default_row.request_phrase or '').strip():
		frappe.throw(_("Configure a request phrase for Bot Parameter {0} in the default language {1}.").format(doc.name, default))
	for code in dict.fromkeys((language or current_language(), default)):
		if code in enabled:
			row = next((row for row in doc.request_phrases if row.language == code), None)
			if row and (row.request_phrase or '').strip():
				return row.request_phrase
	return default_row.request_phrase


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def language_options(doctype, txt, searchfield, start, page_len, filters):
	from messaging.chatbot.doctype.bot_settings.language import get_bot_settings
	require_system_manager()
	codes = [row.language for row in get_bot_settings().chatbot_languages if row.enabled]
	return frappe.get_list('Language', filters={'name': ['in', codes]},
		or_filters={'name': ['like', '%' + txt + '%'], 'language_name': ['like', '%' + txt + '%']},
		fields=['name', 'language_name'], as_list=True, limit_start=start, limit_page_length=page_len)


def provision_preset_parameter(spec, available):
    if isinstance(spec, str):
        doc = frappe.get_doc('Bot Parameter', spec, for_update=True)
        doc.check_permission('read')
        return doc, False
    name = spec['code']
    doc = frappe.get_doc('Bot Parameter', name, for_update=True) if frappe.db.exists('Bot Parameter', name) else None
    new = doc is None
    if new:
        doc = frappe.get_doc(dict(doctype='Bot Parameter', name=name, enabled=1,
            **{key: value for key, value in spec.items() if key not in ('code', 'languages')}))
    else:
        doc.check_permission('read')
    changed = False
    for code in available:
        if code in spec['languages'] and not any(row.language == code for row in doc.request_phrases):
            doc.append('request_phrases', dict(language=code, **spec['languages'][code]))
            changed = True
    if new:
        doc.insert()
    elif changed:
        doc.save()
    return doc, bool(new or changed)




def restore_preset_parameter(spec, available):
    """Restore initial values through a new identity when a shared parameter differs."""
    from copy import deepcopy
    from hashlib import sha256
    import json
    if isinstance(spec, str) or not frappe.db.exists('Bot Parameter', spec['code']):
        return provision_preset_parameter(spec, available)
    doc = frappe.get_doc('Bot Parameter', spec['code'], for_update=True)
    doc.check_permission('read')
    desired = {key: value for key, value in spec.items() if key not in ('code', 'languages')}
    current_languages = {row.language: dict(request_phrase=row.request_phrase) for row in doc.request_phrases}
    different = any(doc.get(key) != value for key, value in desired.items()) or any(
        current_languages.get(language) != spec['languages'].get(language) for language in available)
    if not different:
        return provision_preset_parameter(spec, available)
    planned = deepcopy(spec)
    planned['languages'] = {**current_languages, **planned['languages']}
    suffix = sha256(json.dumps(planned, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    planned['code'] = spec['code'][:50] + '_' + suffix
    # Reusing a previous restored identity never overwrites subsequent administrative edits.
    if frappe.db.exists('Bot Parameter', planned['code']):
        restored = frappe.get_doc('Bot Parameter', planned['code'], for_update=True)
        restored.check_permission('read')
        phrases = {row.language: dict(request_phrase=row.request_phrase) for row in restored.request_phrases}
        if any(restored.get(key) != value for key, value in desired.items()) or any(
                phrases.get(language) != planned['languages'].get(language) for language in available):
            frappe.throw(_('The restored parameter identity contains administrative changes.'))
        return restored, False
    return provision_preset_parameter(planned, tuple(planned['languages']))
