"""Internal content templates for Bot Flow. They do not belong to a provider, channel, or account."""

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.communication.doctype.communication_template.communication_template import (
	template_identity,
	template_revision,
	validate_template_content,
)


class BotFlowTemplate(Document):
	def autoname(self):
		if self.name:
			return
		if not self.language:
			from messaging.chatbot.doctype.bot_settings.language import resolve_process_language
			self.language = resolve_process_language()
		self.name = template_identity(self.template_name, self.language)

	def validate(self):
		from messaging.chatbot.doctype.bot_settings.language import resolve_process_language
		if not self.language:
			self.language = resolve_process_language()
		previous = self.get_doc_before_save()
		if previous and previous.language != self.language:
			frappe.throw(_("Template language is immutable. Create a new template for another language."))
		identity = template_identity(self.template_name, self.language)
		if not self.name:
			self.name = identity
		if self.is_new() or (previous and previous.template_name != self.template_name):
			frappe.db.get_value('DocType', 'Bot Flow Template', 'name', for_update=True)
			for other in frappe.db.sql(
				'SELECT name, template_name FROM `tabBot Flow Template` WHERE language=%s FOR UPDATE',
				(self.language,), as_dict=True,
			):
				from messaging.communication.doctype.communication_template.communication_template import _snake_case
				other_identity = _snake_case(other.template_name or '') + '_' + _snake_case(self.language)
				if (self.is_new() or other.name != self.name) and (other.name == identity or other_identity == identity):
					frappe.throw(_("Template identity {0} already belongs to {1}. Choose a different Template Name.").format(identity, other.name))
		_authorize_language(self)
		validate_template_content(self)
		self.revision = template_revision(self)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Template identity is immutable."))


def _authorize_language(doc):
	from messaging.chatbot.doctype.bot_settings.language import get_bot_settings
	if not doc.language or not frappe.db.exists('Language', doc.language):
		frappe.throw(_('Select an existing Language.'))
	if frappe.flags.in_template_migration or not doc.enabled:
		return
	allowed = {row.language for row in get_bot_settings().chatbot_languages if row.enabled}
	if doc.language not in allowed:
		frappe.throw(_("Template language is not authorized for this Output."))
