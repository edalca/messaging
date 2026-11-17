"""Messaging-owned accounts; provider identity is derived exclusively through channel."""
from copy import deepcopy

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.communication.doctype.communication_channel_account.binding import (
	account_provider,
	resolve_binding,
	resolve_ingress_binding,
)
from messaging.communication.doctype.communication_provider.communication_provider import (
	operation,
	provider_definition,
	validate_result,
)
from messaging.communication.doctype.communication_provider_value.values import (
	_authentication_parameters,
	cleanup_values,
	validate_values,
)
from messaging.security.execution_context import _authenticated_adapter, is_automation


class CommunicationChannelAccount(Document):
	def insert(self, *args, **kwargs):
		# Frappe encrypts child passwords during validation, before the parent INSERT.
		savepoint = "channel_account_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(savepoint)
		try:
			return super().insert(*args, **kwargs)
		except Exception:
			frappe.db.rollback(save_point=savepoint)
			raise
		finally:
			frappe.db.release_savepoint(savepoint)

	def before_insert(self):
		self._generated_account_id = None
		provider = frappe.db.get_value("Communication Channel", self.channel, "provider")
		definition = provider_definition(provider)
		self._account_id_definition = definition if 'prepare_account_id' in definition['operations'] else None
		if self._account_id_definition:
			self._prepare_account_id()
			self._generated_account_id = self.account_id

	def _prepare_account_id(self):
		# Pass a snapshot of this account only; no route or callback comes from the form.
		self.account_id = operation(self._account_id_definition, 'prepare_account_id',
			frappe._dict(channel=self.channel, account=self.name, account_id=self.account_id))

	def db_insert(self, ignore_if_duplicate=False):
		# MariaDB rolls back a failed INSERT statement, before children or hooks run.
		# Generated field-based names must be retried together with their account ID.
		for attempt in range(5):
			generated = (self.is_new() and self.meta.autoname == "field:account_id"
				and self.account_id == self.name == getattr(self, "_generated_account_id", None))
			try:
				return super().db_insert(ignore_if_duplicate=False if generated else ignore_if_duplicate)
			except (frappe.DuplicateEntryError, frappe.UniqueValidationError) as error:
				if not generated or not self._generated_identity_collision(error) or attempt == 4:
					raise
				frappe.clear_last_message()
				self._prepare_account_id()
				self._generated_account_id = self.account_id
				self.name = self.account_id
				self.set_parent_in_children()
				self.validate()

	def _generated_identity_collision(self, error):
		# Frappe wraps the actual database error as (doctype, name, error).
		if len(error.args) != 3 or error.args[:2] != (self.doctype, self.name):
			return False
		database_error = error.args[2]
		if isinstance(error, frappe.DuplicateEntryError):
			if not frappe.db.is_primary_key_violation(database_error):
				return False
			key = database_error.args[1].rpartition(" for key ")[2].strip("'`\"").split(".")[-1]
			if key != "PRIMARY":
				return False
			filters = {"name": self.name}
		else:
			if not frappe.db.is_unique_key_violation(database_error):
				return False
			# Match the exact index, never an arbitrary substring of the error.
			key = database_error.args[1].rpartition(" for key ")[2].strip("'`\"").split(".")[-1]
			if key not in ("account_id", "unique_channel_account_id"):
				return False
			filters = {"account_id": self.account_id}
			if key == "unique_channel_account_id":
				filters["channel"] = self.channel
		# A locking read sees the conflicting row even under REPEATABLE READ.
		return bool(frappe.db.get_value(self.doctype, filters, "name", for_update=True))

	def validate(self):
		if not self.is_new() and any(self.has_value_changed(f) for f in ('channel', 'account_id')):
			frappe.throw(_("Channel account identity is immutable."))
		if not isinstance(self.account_id, str) or not self.account_id.strip() or self.account_id != self.account_id.strip() or len(self.account_id) > 140:
			frappe.throw(_("Invalid channel account."))
		channel = frappe.get_doc('Communication Channel', self.channel)
		if self.is_new():
			provider_definition(channel.provider)
		validate_values(self)

	def on_update(self):
		cleanup_values(self)

	def on_trash(self):
		if any(frappe.db.exists(dt, {'account': self.name}) for dt in ('Chat Contact', 'Chat Session', 'Chat Turn', 'Bot Dispatch', 'Chat Agent')):
			frappe.throw(_("A channel account with conversation history or references cannot be deleted."))
		cleanup_values(self, deleting=True)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Channel account identity is immutable."))


def on_doctype_update():
	frappe.db.add_unique('Communication Channel Account', ['channel', 'account_id'], constraint_name='unique_channel_account_id')


def _report_owner(source_type):
	if source_type == 'Bot Dispatch':
		from messaging.chatbot.doctype.bot_dispatch import delivery
	elif source_type == 'Notification Queue':
		from messaging.communication.doctype.notification_queue import dispatch as delivery
	else:
		frappe.throw(_('Invalid outbound source.'))
	return delivery


def report_result(provider, account, dispatch_name, result, *, source_type='Bot Dispatch'):
	"""Route authenticated transport evidence to its source machine, without writes here."""
	return _report_owner(source_type).report_result(provider, account, dispatch_name, result)


def report_status(provider, account, dispatch_name, status, reference, *, source_type='Bot Dispatch'):
	"""Both outbound sources share provider reports; each owner enforces its transitions."""
	return _report_owner(source_type).report_status(provider, account, dispatch_name, status, reference)


def dispatch_ingress(provider, account, request):
	"""Authenticate using the registered operation, then dispatch its normalized event.

	The verifier receives (binding snapshot, raw request, selected credentials). It
	must validate protocol authenticity AND the request's association to that binding,
	and return exactly {action, payload}, or reject. No identity is granted on failure.
	For external providers this includes cryptographic verification of the raw body.
	EMULR authenticates the administrative session instead of an external protocol.
	"""
	if is_automation():
		frappe.throw(_("Channel authentication must precede automatic execution."), frappe.PermissionError)
	definition, binding = resolve_ingress_binding(provider, account)
	path = definition['operations'].get('authenticate_ingress')
	if not path:
		frappe.throw(_("The channel provider has no ingress authenticator."), frappe.PermissionError)
	identity = frappe._dict(provider=provider, channel=binding.channel, account=binding.name, account_id=binding.account_id)
	credentials = _authentication_parameters(definition, binding)
	failed = False
	try:
		verified = frappe.get_attr(path)(deepcopy(identity), request, credentials)
	except Exception:
		failed = True
	finally:
		# Do not retain material in the dispatcher or return it to a caller.
		credentials.clear()
	if failed:
		frappe.throw(_("Channel ingress authentication failed."), frappe.PermissionError)
	if not isinstance(verified, dict) or set(verified) != {'action', 'payload'} or not isinstance(verified['payload'], dict):
		frappe.throw(_("Invalid authenticated channel event."), frappe.PermissionError)
	action, payload = verified['action'], deepcopy(verified['payload'])
	allowed = {'receive': {'event'}, 'report_result': {'dispatch_name', 'result', 'source_type'},
		'report_status': {'dispatch_name', 'status', 'reference', 'source_type'}}
	if not isinstance(action, str) or action not in allowed or set(payload) != allowed[action]:
		frappe.throw(_("Invalid authenticated channel event."), frappe.PermissionError)
	# Recheck association and configuration after authentication, before entering context.
	_current, current = resolve_ingress_binding(provider, account)
	if (current.channel, current.account_id) != (identity.channel, identity.account_id):
		frappe.throw(_("Invalid or disabled channel account."), frappe.PermissionError)
	from messaging.conversation.doctype.chat_turn.ingress import receive, normalize_event
	if action == 'receive':
		from messaging.conversation.doctype.chat_turn.chat_turn import validate_request
		validate_request(normalize_event(definition, binding, payload['event']))
	else:
		if payload['source_type'] not in ('Bot Dispatch', 'Notification Queue'):
			frappe.throw(_("Invalid outbound source."))
		if action == 'report_result':
			validate_result(payload['result'])
		elif (payload['status'] not in ('Sent', 'Delivered', 'Read', 'Failed')
			or not isinstance(payload['reference'], str) or not payload['reference'].strip() or len(payload['reference']) > 140):
			frappe.throw(_("Invalid delivery status."))
	with _authenticated_adapter(provider, account):
		resolve_binding(provider, account, incoming=True)
		if action == 'receive':
			return receive(provider, account, payload['event'])
		if action == 'report_result':
			return report_result(provider, account, **payload)
		return report_status(provider, account, **payload)
