from contextvars import ContextVar
from frappe.model.naming import make_autoname
from messaging.common.primitives import identity_key
from messaging.common.write_guards import InternalWriteGuard

_language_write = InternalWriteGuard("messaging_contact_language")
_registration_write = InternalWriteGuard("messaging_contact_registration")
INBOX_METHOD = "messaging.conversation.doctype.chat_turn.inbox.drain_contact"
JOB_TIMEOUT = 1200
_draining = ContextVar("messaging_inbox_draining", default=False)

import hashlib
import json
import frappe
from frappe import _
from frappe.model.document import Document


class ChatContact(Document):
	def autoname(self):
		if not self.name:
			self.name = make_autoname("CTC-.######")

	def check_permission(self, permtype='read', *args, **kwargs):
		if permtype == 'write' and _language_write.active():
			require_operator()
			return
		return super().check_permission(permtype, *args, **kwargs)

	def remember_language(self, language, confidence):
		remember_language(self, language, confidence)

	def onload(self):
		self.check_permission("read")
		self.set_onload("profile_fields", None)
		if frappe.session.user == "Administrator" or self.has_permlevel_access_to("parameter_values"):
			self.set_onload("profile_fields", self.profile_fields())

	def profile_fields(self):
		"""Describe stored canonical values without changing the profile or active configuration."""
		values = frappe.parse_json(self.parameter_values or "{}")
		labels = frappe.parse_json(self.profile_labels or "{}")
		parameters = {p.name: p for p in frappe.get_all("Bot Parameter", fields=[
			"name", "label", "parameter_type", "string_mode",
		]) if p.name in values}
		fields = []
		for key, value in values.items():
			parameter = parameters.get(key) if key not in labels else None
			fieldtype = {"String": "Small Text", "Int": "Int", "Date": "Date",
				"Name": "Data", "Expression": "Data"}.get(parameter.parameter_type) if parameter else None
			if parameter and parameter.parameter_type == "String" and parameter.string_mode == "Single Word":
				fieldtype = "Data"
			raw_label = labels.get(key) or (parameter.label if parameter else None)
			fields.append(dict(key=key, label=_(raw_label) if raw_label else
				_("Historical profile value: {0}").format(key),
				parameter_type=parameter.parameter_type if parameter else None,
				string_mode=parameter.string_mode if parameter else None,
				fieldtype=fieldtype or ({bool: "Check", int: "Int", float: "Float"}.get(type(value), "Small Text")), value=value, historical=not bool(parameter or key in labels)))
		return fields

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Conversation identifiers are immutable."))

	def validate(self):
		if not self.is_new() and any(self.has_value_changed(field) for field in ("channel", "account", "external_sender", "identity_key")):
			frappe.throw(_("Contact identity is immutable."))
		if self.has_value_changed('registration_receipt') and self.registration_receipt and not _registration_write.active():
			raise frappe.PermissionError(_('Registration receipts are managed by the Flow consumer.'))
		if self.identity_key != identity_key(self.channel, self.account, self.external_sender):
			frappe.throw(_("Invalid contact identity."))




def require_operator():
	"""Internal entry points require operator permission; automation uses a scoped service identity."""
	if frappe.session.user == "Guest" or not frappe.has_permission("Chat Turn", "read"):
		frappe.throw(_("Messaging operator permission is required."), frappe.PermissionError)


def profile_digest(values):
	return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
		allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def complete_registration(contact, settings, result, *, session, turn, execution_id):
	"""Merge only authorized fields, preserving every unrelated historical profile value."""
	from messaging.chatbot.doctype.bot_settings.flow_consumers import configuration, selected_values, revision
	from messaging.chatbot.doctype.bot_flow.execution import require_execution_result
	require_operator()
	config = configuration(settings, 'registration', operational=True)
	flow = config['flow']
	require_execution_result(flow, result, session=session.name, turn=turn.name, execution_id=execution_id)
	selected = selected_values(config, result['status'], result['data'])
	values = frappe.parse_json(contact.parameter_values or '{}')
	labels = frappe.parse_json(contact.profile_labels or '{}')
	if not isinstance(values, dict) or not isinstance(labels, dict):
		raise frappe.ValidationError(_('The stored contact profile is invalid.'))
	updates = {key: item['value'] for key, item in selected.items()}
	values.update(updates)
	labels.update({key: item['label'] for key, item in selected.items()})
	contact.parameter_values = json.dumps(values, ensure_ascii=False, allow_nan=False)
	contact.profile_labels = json.dumps(labels, ensure_ascii=False)
	contact.registration_receipt = json.dumps(dict(flow=flow.name, status=result['status'],
		revision=revision(config), fields=sorted(updates), digest=profile_digest(updates),
		execution_log=result['execution_log'], session=session.name, turn=turn.name, execution_id=execution_id))
	contact.registration_status = 'Complete'
	with _registration_write():
		contact.save(ignore_permissions=True)
	if not registration_complete(contact, settings):
		raise frappe.ValidationError(_('The contact registration could not be completed.'))


def registration_complete(contact, settings):
	"""Retain completed registration independently of subsequent configuration revisions."""
	from messaging.communication.doctype.process_execution_log.process_execution_log import require_execution_receipt
	if contact.registration_status != 'Complete' or not contact.get('registration_receipt'):
		return False
	try:
		receipt = frappe.parse_json(contact.registration_receipt)
		fields = receipt['fields']
		if (not isinstance(fields, list) or not fields or any(type(key) is not str or not key for key in fields)
			or fields != sorted(set(fields))):
			return False
		require_execution_receipt(receipt['execution_log'], source_type='Bot Flow', source_name=receipt['flow'],
			session=receipt['session'], turn=receipt['turn'], execution_id=receipt['execution_id'], status=receipt['status'])
		if frappe.db.get_value('Chat Session', receipt['session'], 'contact') != contact.name:
			return False
		if frappe.db.get_value('Chat Turn', receipt['turn'], ['contact', 'session']) != (contact.name, receipt['session']):
			return False
		values = frappe.parse_json(contact.parameter_values or '{}')
		stored = {key: values[key] for key in fields}
		return receipt['digest'] == profile_digest(stored)
	except (frappe.ValidationError, frappe.PermissionError, KeyError, TypeError, ValueError):
		frappe.clear_last_message()
		return False




def lock_contact(name):
	frappe.db.get_value("Chat Contact", name, "name", for_update=True)
	return frappe.get_doc("Chat Contact", name)


def get_contact(channel, account, sender):
	key = identity_key(channel, account, sender)
	# The connection lock already serializes this exact identity. Locking a missing
	# secondary-index key would hold a gap shared with unrelated new contacts.
	name = frappe.db.get_value("Chat Contact", {"identity_key": key}, "name")
	if name:
		return lock_contact(name)
	return frappe.get_doc(dict(doctype="Chat Contact", identity_key=key, channel=channel,
							   account=account, external_sender=sender, parameter_values="{}")).insert(ignore_permissions=True)


def remember_language(contact, language, confidence):
    """Persist only detected preference on the existing locked contact."""
    require_operator()
    from messaging.security.execution_context import is_automation
    from messaging.security.permissions import is_system_manager
    from messaging.chatbot.doctype.bot_settings.language import get_bot_settings, MIN_LANGUAGE_CONFIDENCE
    if not (is_automation() or is_system_manager()):
        raise frappe.PermissionError(_('Messaging operator permission is required.'))
    if language not in {r.language for r in get_bot_settings().chatbot_languages if r.enabled} or not MIN_LANGUAGE_CONFIDENCE <= confidence <= 1:
        raise frappe.ValidationError(_('Invalid detected contact language.'))
    current = frappe.get_doc('Chat Contact', contact.name, for_update=True)
    if current.preferred_language == language and current.language_confidence == confidence:
        return
    current.preferred_language, current.language_confidence = language, confidence
    with _language_write():
        current.save()
    contact.preferred_language, contact.language_confidence = language, confidence
    # Keep the caller's locked document current for the engine's later save.
    contact.modified, contact.modified_by = current.modified, current.modified_by


def process_profile_value(name, key):
    """Read one profile value of the active Process contact without granting catalogue access."""
    from messaging.communication.doctype.process_definition.process_definition import (
        _effect,
        _native,
        _scope,
    )
    from messaging.common.primitives import _freeze
    execution = _effect.get()
    if not execution or not execution.get('session') or type(key) is not str:
        raise frappe.PermissionError(_('An authorized operational execution is required.'))
    token = _scope.set({**(_scope.get() or {}), 'Chat Session': None, 'Chat Contact': None})
    try:
        with _native():
            if frappe.db.get_value('Chat Session', execution['session'], 'contact') != name:
                raise frappe.PermissionError(_('The contact does not belong to the Process session.'))
            doc = frappe.get_doc('Chat Contact', name)
            doc.check_permission('read')
            doc.apply_fieldlevel_read_permissions()
            if not doc.has_permlevel_access_to('parameter_values'):
                raise frappe.PermissionError(_('Contact profile read permission is required.'))
            return _freeze(frappe.parse_json(doc.parameter_values or '{}').get(key))
    finally:
        _scope.reset(token)


def schedule_contact(contact):
    """Use Frappe's transaction callback; a Redis failure cannot undo a durable receipt."""
    if _draining.get():
        return
    frappe.db.after_commit.add(lambda: _enqueue(contact))


def _enqueue(contact):
    try:
        return frappe.enqueue(INBOX_METHOD, queue='long', timeout=JOB_TIMEOUT, contact_name=contact,
            job_id='messaging-inbox-' + contact, deduplicate=True)
    except Exception:
        # Do not leak payloads or turn a successful receipt into a failed HTTP acknowledgement.
        frappe.logger('messaging.inbox', allow_site=True).exception('Conversation enqueue failed: %s', contact)


def resume_pending_inputs(contact_name):
    """Wake the durable inbox after reconciliation; HTTP never drains conversations."""
    schedule_contact(contact_name)
