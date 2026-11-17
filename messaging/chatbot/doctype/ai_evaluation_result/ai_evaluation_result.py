"""Independent, protected sample evidence; fixtures remain historical snapshots."""
import frappe
from frappe import _
from frappe.model.document import Document

from messaging.chatbot.doctype.ai_evaluation_run.ai_evaluation_run import _writing, require_writer
from messaging.common.primitives import utcnow


class AIEvaluationResult(Document):
	def before_validate(self):
		require_writer()

	def validate(self):
		require_writer()
		old = self.get_doc_before_save()
		if old and old.status not in ("Selected", "Running"):
			frappe.throw(_("Completed sample evidence is immutable."), frappe.PermissionError)
		if old and any(
			old.get(k) != self.get(k)
			for k in (
				"evaluation_run",
				"case_template",
				"evaluation_case",
				"case_id",
				"language",
				"group",
				"mandatory",
				"case_revision",
				"template_revision",
				"localized_revision",
				"sequence",
				"input_data",
				"expected_data",
			)
		):
			frappe.throw(_("Selected sample identity is immutable."), frappe.PermissionError)

	def on_trash(self):
		frappe.throw(_("Evaluation evidence cannot be deleted."), frappe.PermissionError)

	def before_rename(self, *args, **kwargs):
		self.on_trash()


def create_selected_result(run_name, sequence, fixture, *, mandatory):
	doc = frappe.get_doc(
		dict(
			doctype="AI Evaluation Result",
			evaluation_run=run_name,
			case_template=fixture["case_template"],
			evaluation_case=fixture["name"],
			case_id=fixture["case_id"],
			language=fixture["language"],
			group=fixture["group"],
			mandatory=mandatory,
			case_revision=fixture["definition_revision"],
			template_revision=fixture.get("template_revision"),
			localized_revision=fixture.get("localized_revision"),
			sequence=sequence,
			status="Selected",
			input_data=fixture["input_data"],
			expected_data=fixture["expected_data"],
		)
	)
	with _writing():
		doc.insert(ignore_permissions=True)
	return doc


def list_run_results(run_name, *, limit=257):
	return [
		frappe.get_doc("AI Evaluation Result", r)
		for r in frappe.get_all(
			"AI Evaluation Result",
			filters={"evaluation_run": run_name},
			order_by="sequence",
			pluck="name",
			limit_page_length=limit,
		)
	]


def get_run_result_rows(run_name, *, fields, order_by=None, limit=257):
	return frappe.get_all(
		"AI Evaluation Result",
		filters={"evaluation_run": run_name},
		fields=fields,
		order_by=order_by,
		limit_page_length=limit,
	)


def update_result(result, **values):
	result.update(values)
	with _writing():
		result.save(ignore_permissions=True)
	return result


def mark_unfinished_results_incomplete(run_name, diagnostic, *, limit=257):
	for result in list_run_results(run_name, limit=limit):
		if result.status in ("Selected", "Running"):
			update_result(result, status="Incomplete", finished_at=utcnow(), diagnostic=diagnostic)


def on_doctype_update():
	frappe.db.add_unique(
		"AI Evaluation Result",
		["evaluation_run", "evaluation_case"],
		"evaluation_sample_identity",
	)
	frappe.db.add_index(
		"AI Evaluation Result",
		["evaluation_run", "sequence"],
		"evaluation_sample_order",
	)
