"""Protected administrative evaluation evidence; only the evaluator can write it."""
import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.write_guards import InternalWriteGuard

_writing = InternalWriteGuard('messaging_ai_functional_evaluation')


def require_writer():
    if not _writing.active():
        frappe.throw(_('Evaluation evidence is managed exclusively by the evaluator.'), frappe.PermissionError)


class AIEvaluationRun(Document):
    def before_validate(self):
        require_writer()

    def validate(self):
        require_writer()
        old = self.get_doc_before_save()
        if old and old.status in ('Passed', 'Failed', 'Incomplete', 'Unavailable'):
            frappe.throw(_('Completed evaluation evidence is immutable.'), frappe.PermissionError)
        if old and any(old.get(k) != self.get(k) for k in (
            'configuration', 'provider', 'profile_type', 'evidence_kind', 'requested_by', 'requested_at',
            'seed', 'policy_version', 'configuration_generation', 'dependency_fingerprint', 'manifest',
            'required_count', 'selected_count', 'reproduces')):
            frappe.throw(_('Evaluation manifest identity is immutable.'), frappe.PermissionError)

    def on_trash(self):
        frappe.throw(_('Evaluation evidence cannot be deleted.'), frappe.PermissionError)

    def before_rename(self, *args, **kwargs):
        self.on_trash()


def on_doctype_update():
    frappe.db.add_index('AI Evaluation Run', ['configuration', 'status', 'creation'], 'configuration_evaluations')
