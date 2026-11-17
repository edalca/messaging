"""Scenario definitions, direct functional type, policy and application ownership."""
import hashlib
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.registrations.ai_providers import FUNCTIONAL_TYPES
from .parameters import PARAMETER_FIELDS, parameter_contract
from messaging.chatbot.doctype.bot_parameter.bot_parameter import (
	get_handler,
	get_supported_types,
	validate_input_limits,
)
from messaging.common.write_guards import InternalWriteGuard
from messaging.security.permissions import require_system_manager

_registry_write = InternalWriteGuard("messaging_evaluation_registry")
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
GROUP = re.compile(r"[a-z][a-z0-9_]{0,63}")
METADATA = (
	"template_id",
	"template_name",
	"description",
	"origin",
	"application",
	"profile_type",
	"mandatory",
	"scope",
	"applicable_application",
	"provider",
)


def reject(message):
	frappe.throw(_(message), frappe.ValidationError)


def canonical(value):
	return json.dumps(
		value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")
	)


def revision(value):
	return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_language(language):
	if not isinstance(language, str) or not frappe.db.exists("Language", language):
		reject("Select an existing Frappe language.")


def validate_group(profile_type, group):
	if profile_type == "Parameter Extraction":
		if group not in get_supported_types():
			reject("Extraction groups must be registered native parameter types.")
	elif not isinstance(group, str) or not GROUP.fullmatch(group):
		reject("Decision groups must be lowercase identifiers.")


def metadata(doc):
	values = {key: doc.get(key) or "" for key in METADATA}
	values["mandatory"] = bool(doc.mandatory)
	values["coverage"] = sorted(
		[
			dict(
				language=r.language,
				group=r.group,
				sample_count=r.sample_count,
				required=bool(r.required),
				minimum_parameters=r.minimum_parameters or 1,
			)
			for r in doc.coverage
		],
		key=lambda r: (r["language"], r["group"], r["minimum_parameters"]),
	)
	return values


def protect(doc):
	require_system_manager()
	persisted = (
		frappe.db.get_value(doc.doctype, doc.name, ["origin", "application"], as_dict=True)
		if not doc.is_new()
		else None
	)
	if not _registry_write.active() and (
		doc.origin == "Registered" or (persisted and persisted.origin == "Registered")
	):
		raise frappe.PermissionError(
			_("Registered templates are managed by their owning application.")
		)


def validate_child_ownership(doc):
	"""Reject cross-parent row adoption for native evaluation tables."""
	for field in doc.meta.get_table_fields():
		for row in doc.get(field.fieldname) or []:
			expected = (doc.name, doc.doctype, field.fieldname)
			stored = (
				frappe.db.get_value(
					row.doctype,
					row.name,
					["parent", "parenttype", "parentfield"],
					as_dict=True,
				)
				if row.name
				else None
			)
			if (row.parent, row.parenttype, row.parentfield) != expected or (
				stored and (stored.parent, stored.parenttype, stored.parentfield) != expected
			):
				raise frappe.PermissionError(_("Evaluation row ownership is immutable."))


REFERENCE = re.compile(r"[a-z][a-z0-9_]{0,63}")
CONTEXT_LISTS = ("previously_recognized_flows",)


def references(rows):
	refs = [r.ref for r in rows]
	if (
		len(refs) > 30
		or any(not isinstance(r, str) or not REFERENCE.fullmatch(r) for r in refs)
		or len(set(refs)) != len(refs)
	):
		reject("Scenario references must be unique lowercase identifiers (at most 30 rows).")
	return refs


def template_contract(doc):
	if doc.profile_type == "Decision":
		return dict(available_flows=sorted(references(doc.available_flows)),
			greeting_available=bool(doc.greeting_available),
			human_attention_available=bool(doc.human_attention_available),
			context=dict(previously_recognized_flows=references(doc.previously_recognized_flows)))
	return dict(
		parameters=sorted([parameter_contract(r) for r in doc.parameters], key=lambda r: r["ref"])
	)


def case_coverage_pairs(doc):
	case = frappe.qb.DocType("AI Evaluation Case")
	if doc.profile_type == "Decision":
		return set(
			frappe.qb.from_(case)
			.select(case.language, case.group)
			.distinct()
			.where(case.case_template == doc.name)
			.run()
		)
	parameter = frappe.qb.DocType("AI Evaluation Parameter")
	return set(
		frappe.qb.from_(case)
		.join(parameter)
		.on(case.case_template == parameter.parent)
		.select(case.language, parameter.parameter_type)
		.distinct()
		.where(
			(case.case_template == doc.name)
			& (parameter.parentfield == "parameters")
			& (parameter.parenttype == "AI Evaluation Case Template")
		)
		.run()
	)


class AIEvaluationCaseTemplate(Document):
	def autoname(self):
		self.name = self.template_id

	def before_validate(self):
		protect(self)
		if not _registry_write.active():
			if self.origin not in (None, "", "Custom") or self.application:
				raise frappe.PermissionError(
					_("Custom templates cannot claim an application owner.")
				)
			self.origin, self.application = "Custom", ""

	def validate(self):
		self.before_validate()
		if (
			not isinstance(self.template_id, str)
			or len(self.template_id) > 120
			or not IDENTIFIER.fullmatch(self.template_id)
		):
			reject(
				"Template ID must be a namespaced lowercase identifier (at most 120 characters)."
			)
		namespace = self.application if self.origin == "Registered" else "custom"
		if not namespace or not self.template_id.startswith(namespace + "."):
			reject("Template ID must use its owner namespace; custom templates use custom.")
		if self.origin == "Registered" and (
			not _registry_write.active() or self.application not in frappe.get_installed_apps()
		):
			raise frappe.PermissionError(
				_("Registered templates are managed by their owning application.")
			)
		if self.profile_type not in FUNCTIONAL_TYPES:
			reject("Select a valid AI provider functional type.")
		if not isinstance(self.template_name, str) or not self.template_name.strip():
			reject("Template Name is required.")
		previous = self.get_doc_before_save()
		if previous and any(
			previous.get(k) != self.get(k)
			for k in ("template_id", "origin", "application", "profile_type")
		):
			reject("Template identity, ownership and functional type are immutable.")
		validate_child_ownership(self)
		if self.profile_type == "Decision":
			validate_group(self.profile_type, self.group)
			refs = references(self.available_flows)
			if self.parameters:
				reject("Decision templates cannot define extraction parameters.")
			for key in CONTEXT_LISTS:
				if set(references(self.get(key))) - set(refs):
					reject("Decision context must reference offered synthetic Flows.")

			self.parameter_count = 0
		else:
			if (
				self.available_flows
				or self.greeting_available
			or self.human_attention_available
				or any(self.get(k) for k in CONTEXT_LISTS)
			):
				reject("Extraction templates cannot define Decision context or Flows.")
			if not self.parameters:
				reject("Extraction templates require at least one parameter.")
			references(self.parameters)
			self.group = ""
			self.parameter_count = len(self.parameters)
		self.definition_revision = revision(
			dict(
				template_id=self.template_id,
				profile_type=self.profile_type,
				group=self.group,
				scenario=template_contract(self),
			)
		)

		self._validate_applicability()
		self._validate_coverage()
		if not _registry_write.active() and not self.is_new():
			if case_coverage_pairs(self) - {(r.language, r.group) for r in self.coverage}:
				reject("Coverage cannot omit a language and group with existing cases.")
		self.metadata_revision = revision(metadata(self))
		self.cases_revision = (
			previous.cases_revision if previous else hashlib.sha256().hexdigest()
		)

	def _validate_applicability(self):
		if self.scope == "General":
			if self.applicable_application or self.provider:
				reject("General evaluations cannot restrict an application or provider.")
		elif self.scope == "Application":
			if (
				self.applicable_application not in frappe.get_installed_apps()
				or self.provider
			):
				reject(
					"Application scope requires an installed application and no provider."
				)
		elif self.scope == "Provider":
			if self.applicable_application or not self.provider:
				reject(
					"Provider scope requires a provider and no application restriction."
				)
			if (
				not frappe.db.get_value("AI Provider", self.provider, "decision" if self.profile_type == "Decision" else "parameter_extraction")
			):
				reject("Evaluation and provider functional types must match.")
		else:
			reject("Select General, Application or Provider applicability.")

	def _validate_coverage(self):
		if self.mandatory not in (0, 1):
			reject("Mandatory must be a boolean.")
		if not self.coverage:
			reject("Configure at least one language and group coverage row.")
		validate_child_ownership(self)
		seen = set()
		for row in self.coverage:
			validate_language(row.language)
			validate_group(self.profile_type, row.group)
			groups = (
				{self.group}
				if self.profile_type == "Decision"
				else {r.parameter_type for r in self.parameters}
			)
			if row.group not in groups:
				reject("Coverage groups must belong to this template scenario.")
			row.minimum_parameters = row.minimum_parameters or 1
			if (
				type(row.minimum_parameters) is not int
				or not 1 <= row.minimum_parameters <= 30
				or (self.profile_type == "Decision" and row.minimum_parameters != 1)
			):
				reject("Minimum parameters must be between 1 and 30; Decision uses 1.")
			if (row.language, row.group, row.minimum_parameters) in seen:
				reject("Coverage language and group combinations must be unique.")
			seen.add((row.language, row.group, row.minimum_parameters))
			if type(row.sample_count) is not int or not 1 <= row.sample_count <= 1000000:
				reject(
					"Coverage sample count must be a positive whole number up to 1000000."
				)
			if row.required not in (0, 1):
				reject("Coverage required must be a boolean.")
			if self.mandatory and not row.required:
				reject("Every coverage row of a mandatory evaluation must be required.")

	def on_update(self):
		previous = self.get_doc_before_save()
		if (
			previous
			and previous.definition_revision != self.definition_revision
			and not _registry_write.active()
		):
			# Case snapshots and effective revisions change in the same transaction.
			for name in frappe.get_all(
				"AI Evaluation Case",
				filters={"case_template": self.name},
				pluck="name",
				limit_page_length=0,
			):
				frappe.get_doc("AI Evaluation Case", name).save(ignore_permissions=True)
		if not _registry_write.active():
			refresh_cases_revision(self.name)

	def on_trash(self):
		self.before_validate()
		if frappe.db.exists("AI Evaluation Case", {"case_template": self.name}):
			reject("Delete localized cases before deleting their template.")

	def before_rename(self, *args, **kwargs):
		reject("Template identity is immutable.")


def case_manifest_revision(case_template, *, languages=None):
	"""Hash a paged revision manifest; payloads are never loaded to refresh a suite."""
	digest = hashlib.sha256()
	after = ""
	while True:
		filters = {"case_template": case_template, "name": [">", after]}
		if languages is not None:
			filters["language"] = ["in", languages]
		rows = frappe.get_all(
			"AI Evaluation Case",
			filters=filters,
			fields=["name", "definition_revision"],
			order_by="name",
			limit_page_length=500,
		)
		if not rows:
			break
		for row in rows:
			digest.update(canonical([row.name, row.definition_revision]).encode() + b"\n")
		after = rows[-1].name
	return digest.hexdigest()


def refresh_cases_revision(case_template):
	doc = frappe.get_doc("AI Evaluation Case Template", case_template, for_update=True)
	cases_revision = case_manifest_revision(case_template)
	if doc.cases_revision != cases_revision:
		frappe.db.set_value(
			doc.doctype, doc.name, "cases_revision", cases_revision, update_modified=False
		)
	return cases_revision


def on_doctype_update():
	frappe.db.add_index(
		"AI Evaluation Case Template",
		["profile_type", "mandatory", "scope"],
		"template_applicability",
	)
