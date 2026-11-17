"""Native rows validated and persisted exclusively through their owning document."""
from frappe.model.document import Document


class AIEvaluationExpectedParameter(Document):
    def before_validate(self):
        from messaging.chatbot.doctype.ai_evaluation_case_template.ai_evaluation_case_template import reject
        reject("Evaluation rows must be saved through their owning document.")

    validate = before_validate
    on_trash = before_validate
