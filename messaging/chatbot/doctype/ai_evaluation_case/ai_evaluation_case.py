"""Localized native forms, materialization and immutable effective case identities."""
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Count

from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import (
	CONTEXT_LISTS,
	PARAMETER_FIELDS,
	REFERENCE,
	_registry_write,
	canonical,
	case_coverage_pairs,
	case_manifest_revision,
	 references,
	refresh_cases_revision,
	reject,
	revision,
	template_contract,
	validate_child_ownership,
	validate_group,
	validate_language,
)
from messaging.registry import ai_providers as wire
from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	get_handler,
	get_supported_types,
	validate_input_limits,
	validate_input_value,
)
from messaging.common.primitives import identity_key
from messaging.communication.doctype.communication_template.communication_template import (
	_snake_case,
)
from messaging.security.permissions import require_system_manager

CASE_FIELDS = (
	"case_id",
	"case_template",
	"language",
	"variant",
	"input_text",
	"ambiguous",
	"greeting_detected",
	"human_attention_requested",
	"outcome",
)
LOCAL_TABLES = (
	"flow_texts",
	"flows",
	"excluded_flows",
	"expected_parameters",
	"expected_values",
)


def _page(start, page_length):
	if (
		type(start) is not int
		or start < 0
		or type(page_length) is not int
		or not 1 <= page_length <= 500
	):
		reject("Use a nonnegative start and a page length between 1 and 500.")


def _template(name):
	require_system_manager()
	doc = frappe.get_doc("AI Evaluation Case Template", name)
	doc.check_permission("read")
	return doc


def _case_query(doc, language=None, group=None, minimum_parameters=1):
	if language is not None:
		validate_language(language)
	if group is not None:
		validate_group(doc.profile_type, group)
	if type(minimum_parameters) is not int or not 1 <= minimum_parameters <= 30:
		reject("Minimum parameters must be between 1 and 30; Decision uses 1.")
	case = frappe.qb.DocType("AI Evaluation Case")
	query = frappe.qb.from_(case).where(case.case_template == doc.name)
	if language is not None:
		query = query.where(case.language == language)
	if doc.profile_type == "Decision":
		if group is not None:
			query = query.where(case.group == group)
	else:
		parameter = frappe.qb.DocType("AI Evaluation Parameter")
		template = frappe.qb.DocType("AI Evaluation Case Template")
		eligible = (
			frappe.qb.from_(template)
			.select(template.name)
			.where(
				(template.name == doc.name) & (template.parameter_count >= minimum_parameters)
			)
		)
		if group is not None:
			native = (
				frappe.qb.from_(parameter)
				.select(parameter.parent)
				.where(
					(parameter.parenttype == "AI Evaluation Case Template")
					& (parameter.parentfield == "parameters")
					& (parameter.parameter_type == group)
				)
			)
			eligible = eligible.where(template.name.isin(native))
		query = query.where(case.case_template.isin(eligible))
	return query, case


def _count_cases(doc, language=None, group=None, minimum_parameters=1):
	query, case = _case_query(doc, language, group, minimum_parameters)
	return query.select(Count(case.name).as_("count")).run()[0][0]


def count_cases(case_template, *, language=None, group=None, minimum_parameters=1):
	return _count_cases(
		_template(case_template), language, group, minimum_parameters
	)


def list_cases(
	case_template,
	*,
	language=None,
	group=None,
	start=0,
	page_length=100,
	include_data=False,
	minimum_parameters=1,
):
	_page(start, page_length)
	doc = _template(case_template)
	frappe.has_permission("AI Evaluation Case", "read", throw=True)
	query, case = _case_query(doc, language, group, minimum_parameters)
	fields = [
		"name",
		"case_id",
		"case_template",
		"language",
		"group",
		"definition_revision",
		"template_revision",
		"localized_revision",
	]
	if include_data:
		fields += ["input_data", "expected_data"]
	return (
		query.select(*(case[f] for f in fields))
		.orderby(case.name)
		.limit(page_length)
		.offset(start)
		.run(as_dict=True)
	)


def get_case(case_template, case_id):
	_template(case_template)
	name = frappe.db.get_value(
		"AI Evaluation Case", dict(case_template=case_template, case_id=case_id), "name"
	)
	if not name:
		raise frappe.DoesNotExistError(case_id)
	doc = frappe.get_doc("AI Evaluation Case", name)
	doc.check_permission("read")
	return doc


def json_object(value):
	try:
		value = wire.strict_json(value) if isinstance(value, str) else value
		if not isinstance(value, dict) or len(canonical(value).encode()) > 120000:
			raise ValueError
		# Round-trip also rejects objects not representable as JSON, including tuples.
		if json.loads(canonical(value)) != value:
			raise ValueError
		return value
	except (wire.InvalidInterpretation, ValueError, TypeError, RecursionError):
		reject("Case data must be a bounded JSON object with finite JSON values.")


def validate_case(profile_type, group, language, input_data, expected_data):
	source, expected = json_object(input_data), json_object(expected_data)
	if profile_type == "Decision":
		validate_decision(source, expected, language)
	else:
		validate_extraction(source, expected)
	return canonical(source), canonical(expected)


def validate_decision(source, expected, language):
	if set(source) != {"message", "context", "available_flows", "greeting_available", "human_attention_available"}:
		reject("Decision input must contain the public semantic fields.")
	if set(expected) != (wire.DECISION_KEYS - {"confidence"}) | {"ambiguous"}:
		reject("Expected Decision must contain the public decision fields without confidence.")
	if type(expected['ambiguous']) is not bool or expected['ambiguous'] != (expected['outcome'] == 'ambiguous'):
		reject("Expected ambiguity must agree with its outcome.")
	try:
		request = wire.deserialize_request(dict(source, language=language, schema_version=wire.SCHEMA_VERSION,
			request_id='evaluation', allowed_outcomes=sorted(wire.WIRE_OUTCOMES)))
		wire.deserialize_response(dict(schema_version=wire.SCHEMA_VERSION, request_id='evaluation',
			decision={key: value for key, value in dict(expected, confidence=1.0).items() if key != 'ambiguous'}), request)
	except wire.InvalidInterpretation:
		reject("Expected Decision contradicts the AI contract or synthetic context.")


def validate_extraction(source, expected):
	"""Declarative multi-parameter fixture; this is not a remote extraction protocol."""
	if (
		set(source) != {"text", "parameters"}
		or not isinstance(source["text"], str)
		or not source["text"].strip()
		or len(source["text"]) > 16000
	):
		reject("Extraction input requires text and native parameters.")
	specs = source["parameters"]
	if not isinstance(specs, list) or not 1 <= len(specs) <= 30:
		reject("Extraction templates require between 1 and 30 parameters.")
	refs = set()
	for spec in specs:
		if not isinstance(spec, dict) or set(spec) != {
			"ref",
			"type",
			"parameter",
			"cardinality",
		}:
			reject("Invalid extraction parameter contract.")
		ref, kind, definition, row = (
			spec["ref"],
			spec["type"],
			spec["parameter"],
			spec["cardinality"],
		)
		if (
			not isinstance(ref, str)
			or not REFERENCE.fullmatch(ref)
			or ref in refs
			or kind not in PARAMETER_FIELDS
		):
			reject("Extraction parameter references must be unique and use native types.")
		refs.add(ref)
		if (
			not isinstance(definition, dict)
			or set(definition) - ({"label"} | PARAMETER_FIELDS[kind])
			or not isinstance(definition.get("label"), str)
			or not definition["label"].strip()
		):
			reject("Extraction parameter fields must match the native group type.")
		for key, value in definition.items():
			if key in ("min_length", "max_length", "min_value", "max_value") and type(value) is not int:
				reject("Parameter bounds must be whole numbers.")
		if (
			"allow_contextual_name_extraction" in definition
			and type(definition["allow_contextual_name_extraction"]) is not bool
		):
			reject("Contextual name extraction must be a boolean.")
		parameter = frappe.get_doc(
			dict(
				definition,
				doctype="Bot Parameter",
				parameter_type=kind,
				allow_ai_extraction=0,
			)
		)
		get_handler(kind).validate_definition(parameter)
		if (
			not isinstance(row, dict)
			or set(row) != {"allow_multiple_values", "minimum_values", "maximum_values"}
			or type(row["allow_multiple_values"]) is not bool
		):
			reject("Extraction cardinality requires multiple, minimum and maximum values.")
		validate_input_limits(row)
	if (
		set(expected) != {"parameters"}
		or not isinstance(expected["parameters"], dict)
		or set(expected["parameters"]) != refs
	):
		reject("Expected extraction must contain exactly every template parameter reference.")
	for spec in specs:
		result = expected["parameters"][spec["ref"]]
		if (
			not isinstance(result, dict)
			or set(result) != {"status", "values"}
			or result["status"] not in ("EXTRACTED", "NOT_FOUND", "AMBIGUOUS", "NOT_SUPPORTED")
		):
			reject("Expected extraction requires a native extraction status and values.")
		values, row = result["values"], spec["cardinality"]
		if not isinstance(values, list) or (result["status"] != "EXTRACTED" and values):
			reject("Only EXTRACTED results may contain expected values.")
		if result["status"] == "EXTRACTED":
			if not values or (not row["allow_multiple_values"] and len(values) != 1):
				reject("Expected values contradict extraction cardinality.")
			parameter = frappe.get_doc(
				dict(
					spec["parameter"],
					doctype="Bot Parameter",
					parameter_type=spec["type"],
					allow_ai_extraction=0,
				)
			)
			ok, normalized = validate_input_value(
				row, parameter, values if row["allow_multiple_values"] else values[0]
			)
			normalized = normalized if row["allow_multiple_values"] else [normalized]
			if (
				not ok
				or normalized != values
				or any(type(a) is not type(b) for a, b in zip(normalized, values))
			):
				reject("Expected values must be valid canonical native parameter values.")


def _texts(rows, refs):
	if set(references(rows)) != set(refs):
		reject("Localized labels must match every template reference exactly.")
	if any(not isinstance(r.label, str) or not r.label.strip() for r in rows):
		reject("Localized labels are required.")
	return {r.ref: r for r in rows}


def materialize(doc, template=None):
	"""Build the only executable fixture from native fields; submitted JSON is ignored."""
	template = template or frappe.get_doc("AI Evaluation Case Template", doc.case_template)
	scenario = template_contract(template)
	if template.profile_type == "Decision":
		if doc.expected_parameters or doc.expected_values:
			reject("Decision cases cannot contain extraction expectations.")
		texts = _texts(doc.flow_texts, scenario["available_flows"])
		flows = [
			dict(ref=ref, label=texts[ref].label, description=texts[ref].description or "")
			for ref in scenario["available_flows"]
		]
		source = dict(message=dict(text=doc.input_text), context=scenario['context'], available_flows=flows,
			greeting_available=scenario['greeting_available'], human_attention_available=scenario['human_attention_available'])
		if doc.ambiguous not in (0, 1):
			reject("Decision flags must be booleans.")
		expected = dict(
			greeting_detected=bool(doc.greeting_detected),
			human_attention_requested=bool(doc.human_attention_requested),
			ambiguous=bool(doc.ambiguous),
			outcome=doc.outcome,
			flows=references(doc.flows),
			excluded_flows=references(doc.excluded_flows),
		)
	else:
		if (
			doc.flow_texts
			or doc.flows
			or doc.excluded_flows
			or doc.ambiguous
			or doc.greeting_detected
			or doc.human_attention_requested
		):
			reject("Extraction cases cannot contain Decision fields.")
		specs = scenario["parameters"]
		if set(references(doc.expected_parameters)) != {r["ref"] for r in specs}:
			reject("Expected extraction must contain exactly every template parameter reference.")
		types = {r["ref"]: r["type"] for r in specs}
		results = {r.ref: dict(status=r.status, values=[]) for r in doc.expected_parameters}
		for row in doc.expected_values:
			kind = types.get(row.ref)
			if not kind:
				reject("Expected values must reference a template parameter.")
			row.parameter_type = kind
			if (
				(kind != "Int" and row.value_int not in (None, 0))
				or (kind != "Date" and row.value_date)
				or (kind in ("Int", "Date") and row.value_text)
			):
				reject("Expected value fields must match the native parameter type.")
			value = (
				row.value_int
				if kind == "Int"
				else str(row.value_date)
				if kind == "Date"
				else row.value_text
			)
			results[row.ref]["values"].append(value)
		source, expected = dict(text=doc.input_text, parameters=specs), dict(
			parameters=results
		)
	validate_case(template.profile_type, doc.group, doc.language, source, expected)
	return source, expected


def parent_for(doc):
	template = frappe.get_doc("AI Evaluation Case Template", doc.case_template, for_update=True)
	require_system_manager()
	template.check_permission("read")
	return template


class AIEvaluationCase(Document):
	def _protect_ownership(self):
		require_system_manager()
		stored = (
			frappe.db.get_value(self.doctype, self.name, "application")
			if not self.is_new()
			else None
		)
		if (stored or "") != (self.application or "") and not self.is_new():
			raise frappe.PermissionError(_("Evaluation case ownership is immutable."))
		if (stored or self.application) and not _registry_write.active():
			raise frappe.PermissionError(
				_("Registered cases are managed by their owning application.")
			)
		if self.application and self.application not in frappe.get_installed_apps():
			raise frappe.PermissionError(_("Evaluation case owner must be installed."))

	def onload(self):
		self.set_onload("registered", bool(self.application))

	def autoname(self):
		self.name = (
			self.case_id
			if self.application
			else identity_key(self.case_template, self.case_id)
		)

	def before_validate(self):
		self._protect_ownership()
		template = parent_for(self)
		self.profile_type = template.profile_type
		self.group = (
			template.group
			if template.profile_type == "Decision"
			else sorted({r.parameter_type for r in template.parameters})[0]
		)

	def validate(self):
		self.before_validate()
		template = parent_for(self)
		if (
			not isinstance(self.case_id, str)
			or not re.fullmatch(r"[a-z][a-z0-9_]{0,139}", self.case_id)
			or not isinstance(self.variant, str)
			or not REFERENCE.fullmatch(self.variant)
		):
			reject(
				"Case IDs support at most 140 characters and variant IDs at most 64 lowercase identifier characters."
			)
		previous = self.get_doc_before_save()
		if previous and (previous.case_template, previous.case_id) != (
			self.case_template,
			self.case_id,
		):
			reject("Case identity and template are immutable.")
		validate_language(self.language)
		if self.application and self.case_id != default_case_identity(
			self.case_template, self.language
		):
			reject("Registered case identity must match its template and language.")
		groups = (
			{template.group}
			if template.profile_type == "Decision"
			else {r.parameter_type for r in template.parameters}
		)
		if any(
			not any((r.language, r.group) == (self.language, group) for r in template.coverage)
			for group in groups
		):
			reject("Every case requires a configured language and group coverage row.")
		validate_child_ownership(self)
		source, expected = materialize(self, template)
		self.input_data, self.expected_data = canonical(source), canonical(expected)
		localized = dict(
			language=self.language,
			variant=self.variant,
			input=source.get("message", {}).get("text", source.get("text")),
			expected=expected,
		)
		if template.profile_type == "Decision":
			localized.update(
				flow_texts=source["available_flows"],

			)
		self.localized_revision = revision(localized)
		self.template_revision = template.definition_revision
		self.definition_revision = revision(
			[self.case_id, self.case_template, self.template_revision, self.localized_revision]
		)

	def on_update(self):
		if not _registry_write.active():
			refresh_cases_revision(self.case_template)

	def on_trash(self):
		self._protect_ownership()
		parent_for(self)

	def after_delete(self):
		refresh_cases_revision(self.case_template)

	def before_rename(self, *args, **kwargs):
		reject("Case identity and template are immutable.")


@frappe.whitelist()
def template_fields(case_template):
	"""Native Desk form scaffold, containing public definitions and no provider calls."""
	require_system_manager()
	template = frappe.get_doc("AI Evaluation Case Template", case_template)
	template.check_permission("read")
	return dict(
		profile_type=template.profile_type,
		group=template.group,
		flows=references(template.available_flows),
		parameters=[
			dict(ref=r.ref, type=r.parameter_type, label=r.label or r.ref)
			for r in template.parameters
		],
	)


def on_doctype_update():
	frappe.db.add_unique(
		"AI Evaluation Case", ["case_template", "case_id"], "template_case_identity"
	)
	frappe.db.add_unique(
		"AI Evaluation Case",
		["case_template", "language", "variant"],
		"evaluation_localized_identity",
	)
	frappe.db.add_index(
		"AI Evaluation Case",
		["case_template", "language", "`group`", "name"],
		"template_case_selection",
	)


def default_case_identity(template_name, language):
	"""Use the canonical persisted template name and the shared localized naming convention."""
	name = _snake_case(f"{template_name}_{language}")
	if not name or len(name) > 140:
		reject("Normalized evaluation case identity exceeds 140 characters.")
	return name


def sync_default_cases(template, *, app, cases):
	"""Persist only the verified registration's localized cases, preserving manual cases."""
	if not _registry_write.active() or template.application != app:
		raise frappe.PermissionError(
			"Default cases require their owning template registration."
		)
	fields = (
		"variant",
		"flow_texts",
		"ambiguous",
		"greeting_detected",
		"human_attention_requested",
		"outcome",
		"flows",
		"excluded_flows",
		"expected_parameters",
		"expected_values",
	)
	names = {default_case_identity(template.name, case["language"]) for case in cases}
	existing = set(
		frappe.get_all(
			"AI Evaluation Case",
			filters={"case_template": template.name, "application": app},
			pluck="name",
			limit_page_length=0,
		)
	)
	if existing - names:
		reject(
			"Registration cannot remove existing cases; include the complete localized manifest."
		)
	summary = dict(cases_created=0, cases_updated=0, cases_unchanged=0)
	for case in cases:
		name = default_case_identity(template.name, case["language"])
		present = frappe.db.exists("AI Evaluation Case", name)
		item = (
			frappe.get_doc("AI Evaluation Case", name, for_update=True)
			if present
			else frappe.new_doc("AI Evaluation Case")
		)
		localized = frappe.db.get_value(
			"AI Evaluation Case",
			dict(
				case_template=template.name,
				language=case["language"],
				variant=case["variant"],
			),
			"name",
			for_update=True,
		)
		if (present and (item.application != app or item.case_template != template.name)) or (
			localized and localized != name
		):
			raise frappe.PermissionError(
				_("A default case cannot replace another owner or a manual case.")
			)
		before = item.definition_revision
		for key in fields:
			field = item.meta.get_field(key)
			item.set(
				key,
				[]
				if field.fieldtype == "Table"
				else int(field.default or 0)
				if field.fieldtype in ("Int", "Check")
				else field.default,
			)
		item.update(
			dict(case, case_id=name, case_template=template.name, application=app)
		)
		item.before_validate()
		item.validate()
		if present and before == item.definition_revision:
			summary["cases_unchanged"] += 1
		else:
			item.save()
			summary["cases_updated" if present else "cases_created"] += 1
	# Manual cases keep their owner and must remain valid under the updated scenario.
	for row in frappe.get_all(
		"AI Evaluation Case",
		filters={"case_template": template.name},
		fields=["name", "application", "template_revision"],
		limit_page_length=0,
	):
		if not row.application and row.template_revision != template.definition_revision:
			frappe.get_doc("AI Evaluation Case", row.name).save()
	return summary
