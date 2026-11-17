"""Effective dependencies, exact operational context and irreversible evidence invalidation."""
import ast
import importlib
import inspect
from pathlib import Path

import frappe

from messaging.chatbot.doctype.ai_configuration.execution import (
	configuration_values,
	fingerprint,
	provider_for,
	invalidate_configuration,
)
from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import (
	case_manifest_revision,
)
from messaging.chatbot.doctype.ai_evaluation_case_template import queries
from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import (
	_registry_write,
	reject,
	revision,
)
from messaging.registry.ai_providers import internal_provider
from messaging.chatbot.doctype.bot_settings.language import validate_chatbot_languages
from messaging.common.primitives import utcnow
from messaging.security.permissions import verify_loaded_method

POLICY = {
	"version": "technical-required-v1",
	"succeeds": "all-required-v1",
	"algorithm": "sha256-reservoir-union-v2",
	"required": "Every mandatory executed sample must pass technical validation. Functional comparison is informative.",
	"functional": "informative",
	"optional_blocks": False,
	"max_calls": 256,
	"global_seconds": 900,
	"call_seconds": 30,
	"retries": 0,
}


def operational_context(settings=None):
	"""The only current bot is a Single; installed Process ownership supplies integrations."""
	settings = settings or frappe.get_single("Bot Settings")
	validate_chatbot_languages(settings)
	definitions = frappe.get_all(
		"Bot Flow", filters={"status": "Enabled"}, pluck="process_definition"
	)
	applications = set(
		frappe.get_all(
			"Process Definition",
			filters={"name": ["in", [d for d in definitions if d] or [""]]},
			pluck="application",
		)
	)
	return dict(
		kind="Bot Settings",
		languages=sorted(
			r.language for r in settings.chatbot_languages if r.enabled
		),
		applications=sorted(
			a for a in applications if a in frappe.get_installed_apps()
		),
	)


def applicable(doc, context, *, administrative=False):
	rows, start = [], 0
	resolver = (
		queries.resolve_templates if administrative else queries._resolve_templates
	)
	while True:
		page = resolver(
			profile_type=doc.profile_type,
			provider=doc.provider,
			applications=context["applications"],
			start=start,
			page_length=500,
		)
		rows.extend(page)
		if len(page) < 500:
			return rows
		start += len(page)


def template_dependency(doc, *, languages=None):
	# Metadata labels and descriptions are audit revisions, not behavioral dependencies.
	cases_revision = (
		doc.cases_revision
		if languages is None
		else case_manifest_revision(doc.name, languages=languages)
	)
	return dict(
		name=doc.name,
		profile_type=doc.profile_type,
		mandatory=bool(doc.mandatory),
		scope=doc.scope,
		applicable_application=doc.applicable_application or "",
		provider=doc.provider or "",
		application=doc.application or "",
		cases_revision=cases_revision,
		coverage=sorted(
			[
				dict(
					language=r.language,
					group=r.group,
					sample_count=r.sample_count,
					required=bool(r.required),
					minimum_parameters=r.minimum_parameters or 1,
				)
				for r in doc.coverage
				if languages is None or r.language in languages
			],
			key=lambda r: (r["language"], r["group"], r["minimum_parameters"]),
		),
	)


def implementation_revision(provider, *, verify=False):
	modules = [
		"messaging.chatbot.doctype.ai_configuration.execution",
		"messaging.chatbot.doctype.ai_provider.ai_provider",
		"messaging.registry.ai_providers",
        "messaging.registrations.ai_providers.contracts",
        "messaging.registry.ai_providers.operations",
        "messaging.registrations.ai_providers.wire",
		"messaging.registry._catalog",
		"messaging.registry",
		"messaging.chatbot.doctype.bot_settings.interpretation",
		"messaging.chatbot.doctype.ai_configuration.ai_configuration",
		"messaging.chatbot.doctype.ai_evaluation_case_template.queries",
		"messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case",
		"messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template",
        "messaging.chatbot.doctype.ai_evaluation_case_template.parameters",
		"messaging.chatbot.doctype.ai_request_log.ai_request_log",
		"messaging.chatbot.doctype.ai_evaluation_run.dependencies",
		"messaging.chatbot.doctype.ai_evaluation_run.sampling",
		"messaging.chatbot.doctype.ai_evaluation_run.comparison",
		"messaging.chatbot.doctype.ai_evaluation_run.runtime",
	]
	modules.extend(
		[
			"messaging.chatbot.doctype.ai_evaluation_run.ai_evaluation_run",
			"messaging.chatbot.doctype.ai_evaluation_result.ai_evaluation_result",
			"messaging.common.configuration",
			"messaging.common.write_guards",
			"messaging.security.permissions",
			"messaging.communication.doctype.process_definition.execution",
		]
	)
	if provider.provider_type == 'Internal':
		definition = internal_provider(provider.provider_id, provider.application)
		modules.extend(
			(
				*[capability.execute.__module__ for capability in (definition.decision, definition.parameter_extraction) if capability is not None],
				definition.validate_configuration.__module__,
			)
		)
	if provider.parameter_extraction:
		modules.extend(
			"messaging.chatbot.doctype.bot_parameter.parameters." + name
			for name in (
				"engine",
				"base",
				"string",
				"integer",
				"date",
				"name",
				"expression",
			)
		)
	sources = {}
	for name in sorted(set(modules)):
		module = importlib.import_module(name)
		sources[name] = revision(Path(module.__file__).read_bytes().hex())
		if verify:
			targets = list(vars(module).values())
			tree = ast.parse(Path(module.__file__).read_bytes())
			for node in tree.body:
				if isinstance(node, ast.ClassDef):
					cls = vars(module).get(node.name)
					if inspect.isclass(cls) and cls.__module__ == name:
						targets.extend(
							vars(cls).get(item.name)
							for item in node.body
							if isinstance(item, ast.FunctionDef)
						)
				if (
					isinstance(node, ast.Assign)
					and len(node.targets) == 1
					and isinstance(node.targets[0], ast.Name)
					and node.targets[0].id.isupper()
					and not node.targets[0].id.startswith("_")
				):
					try:
						literal = ast.literal_eval(node.value)
					except (ValueError, TypeError):
						continue
					if vars(module).get(node.targets[0].id) != literal:
						reject(
							"AI evaluation code changed. Reload the application process before evaluating."
						)
			for value in targets:
				if isinstance(value, (classmethod, staticmethod)):
					value = value.__func__
				if inspect.isfunction(value) and value.__module__ == name:
					verify_loaded_method(value)
	return revision(sources)


def snapshot(doc, *, settings=None, verify=False, administrative=False):
	provider = provider_for(doc)
	values = configuration_values(doc, provider)
	context = operational_context(settings)
	rows = applicable(doc, context, administrative=administrative)
	mandatory = [
		template_dependency(
			frappe.get_doc("AI Evaluation Case Template", r.name),
			languages=context["languages"],
		)
		for r in rows
		if r.mandatory
	]
	if not mandatory:
		reject("No mandatory evaluations are available for this AI profile.")
	body = dict(
		configuration=doc.name,
		generation=doc.evaluation_generation,
		configuration_revision=fingerprint(provider, values, behavior_only=True),
		context=context,
		mandatory=mandatory,
		policy=POLICY,
		implementation=implementation_revision(provider, verify=verify),
	)
	return body, revision(body), rows


def evidence_invalidated(run):
	return bool(
		run.invalidated_at or frappe.cache.get_value("ai-evaluation-revoked:" + run.name)
	)


def invalidate(run, reason, *, observed=False):
	if (
		frappe.in_test or frappe.flags.in_test
	) and frappe.db.get_value("AI Evaluation Run", run.name, "evidence_kind") == "Real":
		return
	if not run.invalidated_at:
		at = utcnow()
		frappe.db.set_value(
			"AI Evaluation Run",
			run.name,
			dict(invalidated_at=at, invalidation_reason=reason),
			update_modified=False,
		)
		run.update(dict(invalidated_at=at, invalidation_reason=reason))

	# A runtime read may be followed by an enclosing rollback. Keep an irreversible
	# rejection marker; this cache never grants approval. Transactional edits publish
	# it only after commit, so rejected edits do not revoke valid evidence.
	def remember():
		frappe.cache.set_value("ai-evaluation-revoked:" + run.name, True)

	if observed:
		remember()
	else:
		frappe.db.after_commit.add(remember)


def dependencies_changed(doc=None, method=None):
	"""Called in the owner's transaction, including changes that are later reverted."""
	if method is not None and _registry_write.active():
		return
	if not frappe.db.table_exists("AI Evaluation Run"):
		return
	filters = {
		"invalidated_at": ["is", "not set"],
		"status": ["in", ["Pending", "Running", "Passed"]],
	}
	if frappe.in_test or frappe.flags.in_test:
		filters["evidence_kind"] = "Test"
	if doc and doc.doctype == "AI Configuration":
		filters["configuration"] = doc.name
	elif doc and doc.doctype == "AI Provider":
		filters["provider"] = doc.name
	for row in frappe.get_all(
		"AI Evaluation Run",
		filters=filters,
		fields=["name", "configuration"],
		limit_page_length=0,
	):
		run = frappe.get_doc("AI Evaluation Run", row.name)
		try:
			_body, current, _rows = snapshot(
				frappe.get_doc("AI Configuration", row.configuration)
			)
			changed = run.dependency_fingerprint != current
		except Exception:
			changed = True
		if changed:
			invalidate(
				run,
				"Operational dependencies changed: "
				+ (doc.doctype if doc else "AI evaluation runtime"),
			)
			invalidate_configuration(frappe.get_doc('AI Configuration', row.configuration, for_update=True))
