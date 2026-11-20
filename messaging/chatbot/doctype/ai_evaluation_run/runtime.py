"""Bounded administrative evaluation jobs, durable samples and derived approval."""
import base64
import json
import time
from datetime import timedelta
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import get_datetime

from messaging.chatbot.doctype.ai_configuration import execution as ai_providers
from messaging.chatbot.doctype.ai_evaluation_case.ai_evaluation_case import (
	json_object,
	materialize,
)
from messaging.chatbot.doctype.ai_evaluation_case_template import queries
from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import (
	canonical,
	reject,
	revision,
)
from messaging.chatbot.doctype.ai_evaluation_result.ai_evaluation_result import (
	create_selected_result,
	get_run_result_rows,
	list_run_results,
	mark_unfinished_results_incomplete,
	update_result,
)
from messaging.chatbot.doctype.ai_request_log.ai_request_log import evaluation_scope
from messaging.common.primitives import utcnow
from messaging.communication.doctype.process_definition.execution import execution_deadline
from messaging.security.permissions import require_system_manager

from .ai_evaluation_run import _writing
from .comparison import (
	compare_decision,
	contract_defect,
	decision_request,
	functional_scope,
	protected_response,
)
from .dependencies import (
	POLICY,
	evidence_invalidated,
	invalidate,
	matches_evaluation,
	snapshot,
)
from .sampling import ALGORITHM, select_strata

configurations = ai_providers

TERMINAL = ("Passed", "Failed", "Incomplete", "Unavailable")
APPROVAL_DIAGNOSTICS = {
	**{
		k: v
		for k, v in configurations.DIAGNOSTICS.items()
		if k.startswith("evaluation_")
	},
	"evaluation_passed": "The technical evaluation is approved and current.",
	"provider_unavailable": configurations.DIAGNOSTICS["provider_unavailable"],
	"provider_disabled": configurations.DIAGNOSTICS["provider_disabled"],
}


def _write(doc, **values):
	if doc.doctype == "AI Evaluation Result":
		return update_result(doc, **values)
	doc.update(values)
	with _writing():
		doc.save(ignore_permissions=True)
	return doc


def _configuration(name, *, lock=False):
	require_system_manager()
	doc = frappe.get_doc("AI Configuration", name, for_update=lock)
	doc.check_permission("read")
	doc.check_permission("write")
	return doc


def _options(value):
	value = json.loads(value) if isinstance(value, str) else (value or [])
	if (
		not isinstance(value, list)
		or any(not isinstance(x, str) for x in value)
		or len(set(value)) != len(value)
	):
		reject("Optional templates must be a unique list of applicable template IDs.")
	return value


def _plan(doc, optional=(), *, seed=None):
	body, fingerprint, rows = snapshot(doc, administrative=True)
	optional = _options(optional)
	eligible = {r.name: r for r in rows}
	if any(name not in eligible or eligible[name].mandatory for name in optional):
		reject("Select only optional templates applicable to this AI profile.")
	templates = [r for r in rows if r.mandatory or r.name in optional]
	strata, missing, total = [], [], 0
	for row in templates:
		coverage = queries.coverage_status(row.name)
		covered_languages = {c["language"] for c in coverage}
		for language in body["context"]["languages"]:
			if language not in covered_languages:
				missing.append(
					dict(
						case_template=row.name,
						language=language,
						group="*",
						required=1,
						available=0,
						mandatory=bool(row.mandatory),
					)
				)
		for c in coverage:
			if c["language"] not in body["context"]["languages"]:
				continue
			stratum = dict(case_template=row.name, mandatory=bool(row.mandatory), **c)
			strata.append(stratum)
			total += c["sample_count"]
			if not c["sufficient"]:
				missing.append(
					dict(
						case_template=row.name,
						language=c["language"],
						group=c["group"],
						required=c["sample_count"],
						available=c["available"],
						mandatory=bool(row.mandatory),
					)
				)
	seed = seed or uuid4().hex
	if (
		not isinstance(seed, str)
		or not 1 <= len(seed) <= 64
		or not seed.isascii()
		or not all(c.isalnum() or c in "_-" for c in seed)
	):
		reject(
			"Use a seed of 1 to 64 ASCII letters, numbers, underscores or hyphens."
		)
	if any(s["sample_count"] > POLICY["max_calls"] for s in strata):
		reject(
			"Evaluation sample count exceeds the administrative call limit of {0}.".format(
				POLICY["max_calls"]
			)
		)
	selected = select_strata(strata, seed)
	if len(selected) > POLICY["max_calls"]:
		reject(
			"Evaluation sample count exceeds the administrative call limit of {0}.".format(
				POLICY["max_calls"]
			)
		)
	return dict(
		dependencies=body,
		fingerprint=fingerprint,
		templates=[dict(r) for r in templates],
		strata=strata,
		insufficient=missing,
		selected_count=len(selected),
		coverage_count=total,
		selected=selected,
		seed=seed,
		algorithm=ALGORITHM,
	)


@frappe.whitelist()
def preview(configuration, optional=None):
	plan = _plan(_configuration(configuration), _options(optional))
	return dict(
		configuration=configuration,
		context=plan["dependencies"]["context"],
		templates=plan["templates"],
		strata=plan["strata"],
		insufficient=plan["insufficient"],
		optional_templates=[
			dict(r)
			for r in snapshot(_configuration(configuration), administrative=True)[2]
			if not r.mandatory
		],
		selected_count=plan["selected_count"],
		limits=POLICY,
		executor_available=plan["dependencies"]["mandatory"][0]["profile_type"]
		== "Decision",
	)


def _coverage_error(missing):
	return _("Insufficient evaluation coverage: {0}.").format(
		"; ".join(
			"{case_template}/{language}/{group}: {available}/{required}".format(**r)
			for r in missing
		)
	)


def _create(configuration, optional=None, seed=None, reproduces=None):
	doc = _configuration(configuration, lock=True)
	if doc.status not in ("Untested", "Unavailable"):
		reject("Only untested or unavailable AI profiles can start an evaluation.")
	provider = configurations.provider_for(doc)
	configurations.require_provider_available(provider)
	# This row lock also serializes simultaneous job requests for the same profile.
	if frappe.db.get_value(
		"AI Evaluation Run",
		{"configuration": doc.name, "status": ["in", ["Pending", "Running"]]},
		"name",
		for_update=True,
	):
		reject("An evaluation is already pending or running for this AI profile.")
	if not doc.evaluation_generation:
		# Additive initialization for profiles created before functional evaluation existed.
		doc.evaluation_generation = uuid4().hex
		doc.evaluation_basis = configurations.profile_fingerprint(
			doc, provider,
			configurations.configuration_values(doc, provider),
			behavior_only=True,
		)
		frappe.db.set_value(
			doc.doctype,
			doc.name,
			dict(
				evaluation_generation=doc.evaluation_generation,
				evaluation_basis=doc.evaluation_basis,
			),
			update_modified=False,
		)
	plan = _plan(doc, _options(optional), seed=seed)
	mandatory_missing = [r for r in plan["insufficient"] if r.get("mandatory", True)]
	if mandatory_missing:
		frappe.throw(_coverage_error(mandatory_missing))
	values = configurations.configuration_values(
		doc, provider, required=doc.profile_type == "Decision"
	)
	if doc.profile_type == "Decision":
		ai_providers.validate_configuration(provider, values, policy=configurations.profile_policy(doc, provider))
	selected, fixtures = [], []
	if reproduces:
		old = frappe.get_doc("AI Evaluation Run", reproduces)
		old.check_permission("read")
		if (
			old.configuration != configuration
			or not matches_evaluation(old, doc, plan["fingerprint"])
			or evidence_invalidated(old)
		):
			reject(
				"Historical dependencies are no longer current. Inspect the original manifest or request a new selection."
			)
		original = json.loads(old.manifest)
		if sorted(r["name"] for r in plan["templates"]) != sorted(
			r["name"] for r in original["templates"]
		):
			reject("Historical reproduction requires the same optional templates.")
		plan["seed"] = old.seed
		plan["templates"] = original["templates"]
		plan["strata"] = original["strata"]
		plan["insufficient"] = original["insufficient"]
		for result in _results(old.name):
			fixtures.append(
				dict(
					case_template=result.case_template,
					name=result.evaluation_case,
					case_id=result.case_id,
					language=result.language,
					group=result.group,
					definition_revision=result.case_revision,
					template_revision=result.template_revision,
					localized_revision=result.localized_revision,
					input_data=result.input_data,
					expected_data=result.expected_data,
				)
			)
		selected = original["selected"]
	else:
		selected = plan["selected"]
		for item in selected:
			fixture = queries.get_case(item["case_template"], item["case_id"])
			if fixture.definition_revision != item["definition_revision"]:
				reject(
					"The evaluation catalogue changed during selection. Request a new evaluation."
				)
			source, expected = materialize(fixture)
			values = fixture.as_dict()
			values.update(
				input_data=canonical(source), expected_data=canonical(expected)
			)
			fixtures.append(values)
	plan["selected"] = selected
	plan["selected_count"] = len(selected)
	plan["manifest_revision"] = revision(plan)
	template_rows = {r["name"]: r for r in plan["templates"]}
	run = frappe.get_doc(
		dict(
			doctype="AI Evaluation Run",
			configuration=doc.name,
			provider=doc.provider,
			profile_type=doc.profile_type,
			status="Pending",
			evidence_kind="Test" if frappe.in_test or frappe.flags.in_test else "Real",
			requested_by=frappe.session.user,
			requested_at=utcnow(),
			seed=plan["seed"],
			policy_version=POLICY["version"],
			configuration_generation=doc.evaluation_generation,
			dependency_fingerprint=plan["fingerprint"],
			required_count=sum(
				bool(template_rows[item["case_template"]]["mandatory"])
				for item in selected
			),
			selected_count=len(selected),
			manifest=canonical(plan),
			reproduces=reproduces,
		)
	)
	with _writing():
		run.insert(ignore_permissions=True)
	for index, fixture in enumerate(fixtures, 1):
		template = template_rows[fixture["case_template"]]
		create_selected_result(
			run.name, index, fixture, mandatory=template["mandatory"]
		)
	return run


@frappe.whitelist(methods=["POST"])
def request_evaluation(configuration, optional=None, seed=None, reproduces=None):
	"""An explicit administrative action; the normal request owns creation's commit."""
	try:
		run = _create(configuration, optional, seed, reproduces)
	except ai_providers.AIServiceError as exc:
		if exc.code in (
			"provider_disabled",
			"provider_unavailable",
			"configuration_incomplete",
			"contract_invalid",
			"extraction_inactive",
		):
			frappe.throw(_(configurations.DIAGNOSTICS[exc.code]))
		raise
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		frappe.throw(
			_(
				"Another administrative operation changed this AI profile. Reload its evaluation status."
			),
			frappe.TimestampMismatchError,
		)
	if run.profile_type == "Parameter Extraction":
		for result in _results(run.name):
			_write(
				result,
				status="Unavailable",
				finished_at=utcnow(),
				diagnostic="extraction_inactive",
			)
		_write(
			run,
			status="Unavailable",
			finished_at=utcnow(),
			diagnostic="extraction_inactive",
			summary=canonical(_aggregate(run)),
		)
	else:
		frappe.enqueue(
			"messaging.chatbot.doctype.ai_evaluation_run.runtime.execute",
			queue="long",
			timeout=POLICY["global_seconds"] + 60,
			enqueue_after_commit=True,
			job_id="ai-evaluation-" + run.name,
			run_name=run.name,
		)
	return dict(
		name=run.name,
		status=run.status,
		selected_count=run.selected_count,
		seed=run.seed,
	)


def _results(name):
	return list_run_results(name, limit=POLICY["max_calls"] + 1)


def _operational(row):
	return row.group != "continuation" and not str(row.diagnostic or "").startswith(
		"contract_only"
	)


def _quality(run):
	"""Separate technical approval counts from informative functional comparison."""
	if run is None:
		return dict(
			policy=None,
			technical=dict(
				executed=0, valid=0, invalid=0, incomplete=0, unavailable=0
			),
			functional=dict(
				applicable=0, matched=0, differed=0, not_applicable=0
			),
		)
	rows = get_run_result_rows(
		run.name,
		fields=[
			"group",
			"status",
			"attempt_count",
			"technical_valid",
			"functional_valid",
			"diagnostic",
		],
		limit=POLICY["max_calls"] + 1,
	)
	applicable = [r for r in rows if _operational(r) and r.technical_valid]
	return dict(
		policy=run.policy_version,
		technical=dict(
			executed=sum(bool(r.attempt_count) for r in rows),
			valid=sum(bool(r.technical_valid) for r in rows),
			invalid=sum(
				r.status == "Failed" and not r.technical_valid for r in rows
			),
			incomplete=sum(r.status == "Incomplete" for r in rows),
			unavailable=sum(r.status == "Unavailable" for r in rows),
		),
		functional=dict(
			applicable=len(applicable),
			matched=sum(bool(r.functional_valid) for r in applicable),
			differed=sum(not r.functional_valid for r in applicable),
			not_applicable=sum(
				not _operational(r) and r.status in TERMINAL for r in rows
			),
		),
	)


def _aggregate(run):
	manifest = json.loads(run.manifest)
	results = get_run_result_rows(
		run.name,
		fields=[
			"evaluation_case",
			"case_template",
			"language",
			"group",
			"status",
			"attempt_count",
			"technical_valid",
			"functional_valid",
			"diagnostic",
		],
		limit=POLICY["max_calls"] + 1,
	)
	by_case = {r.evaluation_case: r for r in results}
	summary = {}
	for template in manifest["templates"]:
		name = template["name"]
		selected = [r for r in manifest["selected"] if r["case_template"] == name]
		rows = [r for r in results if r.case_template == name]
		coverage = []
		for stratum in manifest["strata"]:
			if stratum["case_template"] != name:
				continue
			key = [stratum["group"], stratum.get("minimum_parameters", 1)]
			members = [
				by_case.get(r["name"])
				for r in selected
				if r["language"] == stratum["language"]
				and key in r["coverage_strata"]
			]
			passed = sum(
				bool(r and r.status == "Passed" and r.technical_valid)
				for r in members
			)
			coverage.append(
				dict(
					language=stratum["language"],
					group=stratum["group"],
					minimum_parameters=key[1],
					required=stratum["sample_count"],
					selected=len(members),
					passed=passed,
					complete=stratum["sufficient"]
					and len(members) == stratum["sample_count"]
					and passed == stratum["sample_count"],
				)
			)
		row = dict(
			mandatory=bool(template["mandatory"]),
			required=len(selected),
			coverage_required=sum(c["required"] for c in coverage),
			selected=len(rows),
			executed=sum(bool(r.attempt_count) for r in rows),
			passed=sum(
				r.status == "Passed" and bool(r.technical_valid) for r in rows
			),
			functional_matched=sum(
				bool(r.functional_valid) and _operational(r) for r in rows
			),
			functional_differed=sum(
				bool(r.technical_valid)
				and not r.functional_valid
				and _operational(r)
				for r in rows
			),
			functional_not_applicable=sum(
				not _operational(r) and r.status in TERMINAL for r in rows
			),
			technical_invalid=sum(
				r.status == "Failed" and not r.technical_valid for r in rows
			),
			failed=sum(r.status == "Failed" for r in rows),
			unavailable=sum(r.status == "Unavailable" for r in rows),
			coverage=coverage,
		)
		missing = any(
			r["case_template"] == name for r in manifest["insufficient"]
		)
		row["status"] = (
			"Passed"
			if coverage and not missing and all(c["complete"] for c in coverage)
			else "Failed"
			if row["failed"]
			else (
				"Unavailable"
				if not missing
				and row["required"]
				and row["unavailable"] == row["required"]
				else "Incomplete"
			)
		)
		summary[name] = row
	return summary


def _current(run, *, verify=False):
	doc = _configuration(run.configuration, lock=True)
	configurations.require_provider_available(configurations.provider_for(doc))
	body, fingerprint, _rows = snapshot(doc, verify=verify)
	if evidence_invalidated(run) or fingerprint != run.dependency_fingerprint:
		invalidate(run, "Evaluation dependencies are no longer current.")
		return None
	return doc


def execute(run_name):
	"""Worker entry; markers commit before network and each sample commits independently."""
	require_system_manager()
	if getattr(frappe.local, "request", None) is not None:
		raise frappe.PermissionError(
			_("Evaluation execution requires a background job.")
		)
	configuration = frappe.db.get_value(
		"AI Evaluation Run", run_name, "configuration"
	)
	frappe.db.get_value("AI Configuration", configuration, "name", for_update=True)
	run = frappe.get_doc("AI Evaluation Run", run_name, for_update=True)
	if run.status != "Pending":
		return
	try:
		doc = _current(run, verify=True)
	except Exception as exc:
		_finish_incomplete(run, configurations.error_code(exc))
		frappe.db.commit()
		return
	if doc is None:
		_finish_incomplete(run, "evaluation_invalidated")
		frappe.db.commit()
		return
	if run.profile_type != "Decision":
		_write(
			run,
			status="Unavailable",
			finished_at=utcnow(),
			diagnostic="extraction_inactive",
		)
		frappe.db.commit()
		return
	started = time.monotonic()
	_write(
		run,
		status="Running",
		started_at=utcnow(),
		deadline_at=utcnow() + timedelta(seconds=POLICY["global_seconds"]),
	)
	frappe.db.commit()
	try:
		for selected in _results(run.name):
			run.reload()
			if (
				time.monotonic() - started + POLICY["call_seconds"]
				> POLICY["global_seconds"]
			):
				raise TimeoutError()
			doc = _current(run)
			if doc is None or run.call_count >= POLICY["max_calls"]:
				break
			provider = configurations.provider_for(doc)
			values = configurations.configuration_values(
				doc, provider, required=True
			)
			policy = configurations.profile_policy(doc, provider)
			secrets = [
				values.get(r.parameter_key)
				for r in provider.parameters
				if r.field_type == "Password"
			]
			# Basic credentials can be reflected in encoded headers by an untrusted peer.
			if values.get("auth_type") == "Basic":
				secrets.append(
					base64.b64encode(
						(values["username"] + ":" + values["auth_secret"]).encode()
					).decode()
				)
			source, expected = (
				json_object(selected.input_data),
				json_object(selected.expected_data),
			)
			request, refs = decision_request(source, selected.language)
			outbound = ai_providers.outbound_request(provider, values, request)
			_write(
				selected,
				status="Running",
				started_at=utcnow(),
				attempt_count=1,
				wire_request=canonical(outbound),
			)
			_write(run, call_count=run.call_count + 1)
			# No database transaction or configuration lock remains open during the call.
			frappe.db.commit()
			evidence, raw, normalized, differences = {}, None, None, []
			technical, functional, diagnostic = False, False, ""
			call_started = time.monotonic()
			try:
				with evaluation_scope(
					None, configuration=doc.name, evidence=evidence
				), execution_deadline():
					raw = (ai_providers.run(provider, values, request, policy=policy) if policy is not None
						else ai_providers.run(provider, values, request))
				normalized, differences = compare_decision(
					raw, request, refs, expected, values.get("minimum_confidence"), policy=policy
				)
				technical, functional = True, not differences
				diagnostic = (
					"passed"
					if functional
					else "; ".join(
						d["field"] + ": " + d["reason"] for d in differences
					)
				)
				if functional_scope(selected.group, expected) == "contract_only":
					diagnostic = (
						"contract_only"
						if functional
						else "contract_only; " + diagnostic
					)
			except Exception as exc:
				diagnostic = configurations.error_code(exc)
				# Exception messages and remote error bodies are deliberately excluded.
				differences = [
					dict(code=diagnostic, exception_type=type(exc).__name__)
				]
				defect = contract_defect(exc)
				if defect:
					differences[0]["defect"] = defect
					diagnostic += ": " + defect
			log_ids = frappe.get_all(
				"AI Request Log",
				filters={"evaluation_id": evidence.get("evaluation_id", "")},
				pluck="name",
				limit_page_length=100,
			)
			raw_evidence = evidence.get("response", raw)
			_write(
				selected,
				status="Passed" if technical else "Failed",
				technical_valid=int(technical),
				functional_valid=int(technical and functional),
				finished_at=utcnow(),
				latency_ms=round((time.monotonic() - call_started) * 1000, 3),
				diagnostic=diagnostic,
				raw_response=protected_response(raw_evidence, secrets),
				normalized_response=canonical(normalized or {}),
				differences=canonical(differences),
				request_log_ids=canonical(log_ids),
			)
			_write(
				run,
				executed_count=run.executed_count + 1,
				passed_count=run.passed_count + int(technical),
			)
			frappe.db.commit()
			if not technical and diagnostic in (
				"authentication_error",
				"rate_limited",
				"provider_disabled",
				"configuration_disabled",
			):
				break
		run.reload()
		doc = _current(run, verify=True)
		summary = _aggregate(run)
		mandatory = [s for s in summary.values() if s["mandatory"]]
		status = (
			"Passed"
			if mandatory and all(s["status"] == "Passed" for s in mandatory)
			else (
				"Failed"
				if any(s["failed"] for s in mandatory)
				else "Incomplete"
			)
		)
		if doc is None:
			status = "Incomplete"
		if any(r.status in ("Selected", "Running") for r in _results(run.name)):
			status = "Incomplete"
		# Every selected case must be terminal, including optional cases.
		mark_unfinished_results_incomplete(
			run.name, "evaluation_interrupted", limit=POLICY["max_calls"] + 1
		)
		_write(
			run,
			status=status,
			summary=canonical(summary),
			finished_at=utcnow(),
			diagnostic="evaluation_passed"
			if status == "Passed"
			else "evaluation_invalidated"
			if doc is None
			else "evaluation_" + status.lower(),
		)
		if status == "Passed" and run.evidence_kind == "Real" and doc.status in ("Untested", "Unavailable"):
			provider = configurations.provider_for(doc)
			values = configurations.configuration_values(doc, provider, required=True)
			configurations.set_profile_status(doc, "Enabled", reason="operational")
			configurations.diagnose(doc, "operational", configurations.profile_fingerprint(doc, provider, values), checked=True)
		frappe.db.commit()
	except BaseException:
		# Hard process death is reconciled explicitly by the administrative status endpoint.
		frappe.db.rollback()
		run.reload()
		_finish_incomplete(run, "evaluation_interrupted")
		frappe.db.commit()
		raise


def approval(doc, *, settings=None):
	"""Check persisted technical evidence without writes or provider calls."""
	rows = frappe.get_all(
		"AI Evaluation Run",
		filters={"configuration": doc.name},
		fields=["name"],
		order_by="creation desc, name desc",
		limit_page_length=1,
	)
	code, run = "evaluation_missing", None
	if rows:
		run = frappe.get_doc("AI Evaluation Run", rows[0].name)
		try:
			configurations.require_provider_available(configurations.provider_for(doc))
			body, current, _rows = snapshot(doc, settings=settings)
			if run.policy_version != POLICY["version"]:
				code = "evaluation_policy"
			elif (
				evidence_invalidated(run)
				or not matches_evaluation(run, doc, current)
			):
				stored = json.loads(run.manifest)["dependencies"]["context"]
				code = (
					"evaluation_context"
					if stored != body["context"]
					else "evaluation_invalidated"
				)
			elif run.evidence_kind != "Real":
				code = "evaluation_test"
			elif run.status == "Passed":
				summary = _aggregate(run)
				mandatory = [r for r in summary.values() if r["mandatory"]]
				code = (
					"evaluation_passed"
					if mandatory
					and all(r["status"] == "Passed" for r in mandatory)
					else "evaluation_incomplete"
				)
			else:
				code = "evaluation_" + run.status.lower()
		except configurations.AIServiceError as exc:
			code = exc.code if exc.code in ("provider_disabled", "provider_unavailable") else "evaluation_invalidated"
		except Exception:
			code = "evaluation_invalidated"
	return dict(
		approved=code == "evaluation_passed",
		status=code,
		diagnostic=_(APPROVAL_DIAGNOSTICS[code]),
		run=run.name if run else None,
		invalidated_at=run.invalidated_at if run else None,
		**_quality(run),
	)


@frappe.whitelist(methods=["POST"])
def approval_status(configuration):
	return approval(_configuration(configuration))


@frappe.whitelist(methods=["POST"])
def interrupt(run_name):
	"""Explicitly terminate abandoned/queued work; samples are never resumed or replayed."""
	require_system_manager()
	run = frappe.get_doc("AI Evaluation Run", run_name, for_update=True)
	run.check_permission("read")
	if run.status not in ("Pending", "Running"):
		return dict(name=run.name, status=run.status)
	if (
		run.status == "Running"
		and run.deadline_at
		and utcnow() <= get_datetime(run.deadline_at) + timedelta(seconds=60)
	):
		reject("The evaluation worker is still within its execution limit.")
	_finish_incomplete(run, "evaluation_interrupted")
	return dict(name=run.name, status=run.status)


def _finish_incomplete(run, diagnostic):
	mark_unfinished_results_incomplete(
		run.name, diagnostic, limit=POLICY["max_calls"] + 1
	)
	_write(
		run,
		status="Incomplete",
		finished_at=utcnow(),
		diagnostic=diagnostic,
		summary=canonical(_aggregate(run)),
	)


@frappe.whitelist()
def progress(run_name):
	require_system_manager()
	run = frappe.get_doc("AI Evaluation Run", run_name)
	run.check_permission("read")
	return dict(
		name=run.name,
		status=run.status,
		invalidated_at=run.invalidated_at,
		diagnostic=run.diagnostic,
		selected=run.selected_count,
		executed=run.executed_count,
		calls=run.call_count,
		summary=_aggregate(run),
		**_quality(run),
		results=get_run_result_rows(
			run.name,
			fields=[
				"name",
				"case_template",
				"case_id",
				"language",
				"group",
				"status",
				"technical_valid",
				"functional_valid",
				"diagnostic",
			],
			order_by="sequence",
			limit=POLICY["max_calls"] + 1,
		),
	)
