"""Small coverage configuration owned and validated by AI Evaluation."""
from frappe.model.document import Document


class AIEvaluationCoverage(Document):
    def before_validate(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import reject
        reject('Coverage rows must be saved through their evaluation.')

    validate = before_validate
    on_trash = before_validate


def on_doctype_update():
    import frappe
    frappe.db.add_unique('AI Evaluation Coverage', ['parent', 'language', '`group`', 'minimum_parameters'], 'evaluation_coverage_stratum')
