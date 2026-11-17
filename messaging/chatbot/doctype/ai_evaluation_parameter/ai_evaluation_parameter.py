"""Native rows validated and persisted exclusively through their owning document."""
import frappe
from frappe.model.document import Document


class AIEvaluationParameter(Document):
    def before_validate(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import reject
        reject("Evaluation rows must be saved through their owning document.")

    validate = before_validate
    on_trash = before_validate


def on_doctype_update():
    frappe.db.add_index('AI Evaluation Parameter', ['parent', 'parentfield', 'parameter_type'], 'evaluation_parameter_type')
