"""Permission-checked, bounded catalogue reads for evaluators and Desk."""
import frappe

from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import (
	_case_query,
	_count_cases,
	_page,
	_template,
	case_coverage_pairs,
	case_manifest_revision,
	count_cases,
	get_case,
	list_cases,
)
from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import (
	FUNCTIONAL_TYPES,
	reject,
	validate_group,
	validate_language,
)
from messaging.security.permissions import require_system_manager


def resolve_templates(
	*, profile_type, provider=None, applications=(), mandatory=None, start=0, page_length=100
):
	"""Return additive applicable definitions; mandatory core suites cannot be replaced.

	applications is the explicit set of integrations in the case_template context, not
	every installed app. The provider's owning app is included when it has one.
	"""
	require_system_manager()
	if provider:
		frappe.get_doc("AI Provider", provider).check_permission("read")
	return _resolve_templates(
		profile_type=profile_type,
		provider=provider,
		applications=applications,
		mandatory=mandatory,
		start=start,
		page_length=page_length,
	)


def _resolve_templates(
	*, profile_type, provider=None, applications=(), mandatory=None, start=0, page_length=100
):
	"""Server-owned operational reads share applicability without elevating a runtime user."""
	_page(start, page_length)
	if profile_type not in FUNCTIONAL_TYPES or (
		mandatory is not None and type(mandatory) is not bool
	):
		reject("Select a functional type and an optional boolean mandatory filter.")
	if not isinstance(applications, (list, tuple)) or any(
		a not in frappe.get_installed_apps() for a in applications
	):
		reject(
			"Applicable applications must be an explicit list of installed applications."
		)
	applications = set(applications)
	if provider:
		doc = frappe.get_doc("AI Provider", provider)
		if not doc.get('decision' if profile_type == 'Decision' else 'parameter_extraction'):
			reject("Template and provider functional types must match.")
		if doc.application:
			applications.add(doc.application)
	filters = {"profile_type": profile_type}
	if mandatory is not None:
		filters["mandatory"] = int(mandatory)
	# Exclude uninstalled owners without hiding custom documents.
	filters["application"] = ["in", ["", *frappe.get_installed_apps()]]
	scopes = [["AI Evaluation Case Template", "scope", "=", "General"]]
	if applications:
		scopes.append(
			[
				"AI Evaluation Case Template",
				"applicable_application",
				"in",
				sorted(applications),
			]
		)
	if provider:
		scopes.append(["AI Evaluation Case Template", "provider", "=", provider])
	return frappe.get_all(
		"AI Evaluation Case Template",
		filters=filters,
		or_filters=scopes,
		fields=[
			"name",
			"template_name",
			"profile_type",
			"origin",
			"application",
			"mandatory",
			"scope",
			"applicable_application",
			"provider",
			"metadata_revision",
			"cases_revision",
			"definition_revision",
		],
		order_by="mandatory desc, name",
		start=start,
		page_length=page_length,
	)


def coverage_status(case_template):
	"""Return groups, languages, configured quantities and explicit bank sufficiency.

	Custom definitions may be assembled in Desk before their bank is complete.
	Insufficient coverage is reported, never silently reduced or treated as optional.
	"""
	doc = _template(case_template)
	return _coverage_status(doc)


def _coverage_status(doc):
	counts = {
		(r.language, r.group, r.minimum_parameters or 1): _count_cases(
			doc, r.language, r.group, r.minimum_parameters or 1
		)
		for r in doc.coverage
	}
	return [
		dict(
			language=r.language,
			group=r.group,
			sample_count=r.sample_count,
			minimum_parameters=r.minimum_parameters or 1,
			required=bool(r.required),
			available=counts[(r.language, r.group, r.minimum_parameters or 1)],
			sufficient=counts[(r.language, r.group, r.minimum_parameters or 1)]
			>= r.sample_count,
		)
		for r in doc.coverage
	]
