"""Configured provider instances with immutable identity and scoped values."""
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.common.locking import document_savepoint, lock_document_types
from messaging.common.write_guards import InternalWriteGuard
from messaging.communication.doctype.communication_provider_value.communication_provider_value import validate_values, cleanup_values
from messaging.security.permissions import require_system_manager

channel_status_write = InternalWriteGuard('messaging_channel_status_write')

QUOTA_POLICY_FIELDS = (
	"quotas_enabled",
	"daily_quota_limit",
	"monthly_quota_limit",
	"quota_timezone",
	"quota_consumption_basis",
	"composite_response_single_unit",
	"quota_reservation_ttl_seconds",
	"reconcile_quota_on_acceptance",
	"quota_courtesy_enabled",
	"quota_courtesy_max_per_cycle",
)


def _normalize_channel_name(value):
	label = unicodedata.normalize('NFC', re.sub(r' +', ' ', (value or '').strip(' ')))
	if not label:
		frappe.throw(_("Channel Name is required."))
	if not re.fullmatch(r'[A-Za-z](?:[A-Za-z\u0300-\u036f]| [A-Za-z])*', unicodedata.normalize('NFD', label)):
		frappe.throw(_("Channel Name may contain only letters and spaces."))
	return label


class CommunicationChannel(Document):
	def before_insert(self):
		from messaging.communication.doctype.communication_provider.communication_provider import operation, provider_definition

		self.channel_name = _normalize_channel_name(self.channel_name)
		# Share the catalog lock so fixed IDs cannot be reserved during a channel insert.
		lock_document_types('Communication Provider')
		definition = provider_definition(self.provider)
		if 'initialize_channel' in definition['operations']:
			operation(definition, 'initialize_channel', self)

	def autoname(self):
		fixed = frappe.db.get_value('Communication Provider', self.provider, 'fixed_channel_id', for_update=True)
		self.name = self._initial_name(fixed)

	def _initial_name(self, fixed):
		if fixed:
			owner = frappe.db.get_value(self.doctype, fixed, 'provider', for_update=True)
			if owner and owner != self.provider:
				frappe.throw(_("The fixed channel ID belongs to another provider."))
			if not owner:
				return fixed
		letters = unicodedata.normalize('NFD', self.channel_name).encode('ascii', 'ignore').decode('ascii')
		return letters.lower().replace(' ', '_')

	def before_validate(self):
		if self.quotas_enabled:
			self.quota_consumption_basis = "Provider Accepted"
			self.composite_response_single_unit = 1
			self.reconcile_quota_on_acceptance = 1

	def validate(self):
		from messaging.communication.doctype.communication_provider.communication_provider import (
			is_available, provider_definition,
		)

		old = self.get_doc_before_save()
		self.channel_name = _normalize_channel_name(self.channel_name)
		if old and self.provider != old.provider:
			frappe.throw(_("Channel provider identity is immutable."))
		if self.status != (old.status if old else 'Disabled') and not channel_status_write.active():
			frappe.throw(_("Change the channel status using its enable or disable action."), frappe.PermissionError)
		catalog = frappe.db.get_value('Communication Provider', self.provider,
			['status', 'single_channel', 'fixed_channel_id'], as_dict=True, for_update=True)
		definition = provider_definition(self.provider)
		if self.is_new() or channel_status_write.active():
			if catalog.status != 'Enabled':
				frappe.throw(_("The channel provider is disabled."), frappe.PermissionError)
		if channel_status_write.active() and not is_available(definition):
			frappe.throw(_("The channel provider is unavailable and the channel cannot be enabled."))
		self.single_provider_key = self.provider if catalog.single_channel else None
		if self.is_new():
			expected_name = self._initial_name(catalog.fixed_channel_id)
			if self.name != expected_name:
				if expected_name == catalog.fixed_channel_id:
					frappe.throw(_("The channel identity must match the provider's fixed channel ID."))
				frappe.throw(_("The channel identity must match Channel Name."))
			if frappe.db.get_value('Communication Provider',
				{'fixed_channel_id': self.name, 'name': ['!=', self.provider]}, 'name', for_update=True):
				frappe.throw(_("The fixed channel ID belongs to another provider."))
			if frappe.db.exists(self.doctype, self.name):
				frappe.throw(_("A channel with this ID already exists."))
		if catalog.single_channel and frappe.db.exists(self.doctype, {'provider': self.provider, 'name': ['!=', self.name]}):
			frappe.throw(_("This provider permits only one channel."))
		validate_values(self)
		self._validate_quotas()

	def _validate_quotas(self, *, operational=False, settings=None) -> None:
		daily_limit = int(self.daily_quota_limit or 0)
		monthly_limit = int(self.monthly_quota_limit or 0)
		reservation_ttl = int(self.quota_reservation_ttl_seconds or 0)

		if self.quotas_enabled and daily_limit < 1:
			frappe.throw(_("Daily Quota Limit must be at least 1."), frappe.ValidationError)
		if self.quotas_enabled and monthly_limit < daily_limit:
			frappe.throw(
				_("Monthly Quota Limit ({0}) cannot be less than Daily Quota Limit ({1}).").format(
					monthly_limit, daily_limit
				),
				frappe.ValidationError,
			)
		if ((operational and self.quotas_enabled) or self.quota_reservation_ttl_seconds) and not 10 <= reservation_ttl <= 3600:
			frappe.throw(
				_("Quota Reservation TTL must be between 10 and 3600 seconds."),
				frappe.ValidationError,
			)

		if int(self.quota_courtesy_enabled or 0):
			if int(self.quota_courtesy_max_per_cycle or 0) < 1:
				frappe.throw(
					_("Max Courtesy Notices Per Quota Cycle must be at least 1 when Quota Courtesy is enabled."),
					frappe.ValidationError,
				)
			if self.quotas_enabled or operational:
				from messaging.chatbot.doctype.bot_settings.language import get_bot_settings, validate_chatbot_languages
				from messaging.chatbot.doctype.bot_settings.localization import text as localized_text
				bot_settings = settings or get_bot_settings()
				default_lang = validate_chatbot_languages(bot_settings)
				if (operational or default_lang) and not (localized_text(bot_settings, "quota_courtesy_message", default_lang) or "").strip():
					frappe.throw(
						_("Quota Courtesy Message is required when Quota Courtesy is enabled."),
						frappe.ValidationError,
					)

		if not self.quotas_enabled:
			return
		tz_name = (self.quota_timezone or "").strip()
		if not tz_name:
			frappe.throw(_("Applicable Quota Timezone is required."), frappe.ValidationError)
		try:
			ZoneInfo(tz_name)
		except (ZoneInfoNotFoundError, KeyError, ValueError):
			frappe.throw(
				_("Invalid IANA Timezone '{0}'.").format(tz_name),
				frappe.ValidationError,
			)
		self.quota_timezone = tz_name

	def insert(self, *args, **kwargs):
		try:
			# Frappe saves encrypted child values before inserting the parent.
			with document_savepoint():
				return super().insert(*args, **kwargs)
		except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
			if frappe.db.get_value('Communication Provider', self.provider, 'single_channel'):
				frappe.throw(_("This provider permits only one channel."))
			raise

	def on_update(self):
		cleanup_values(self)

	def on_trash(self):
		if frappe.db.exists('Communication Channel Account', {'channel': self.name}):
			frappe.throw(_("A channel with accounts cannot be deleted."))
		cleanup_values(self, deleting=True)

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Channel identity is immutable."))


@frappe.whitelist(methods=['POST'])
def set_channel_status(channel, status):
	"""Enable a validated saved configuration or disable an existing channel."""
	require_system_manager()
	if status not in ('Disabled', 'Enabled'):
		frappe.throw(_("Invalid channel status."))
	try:
		doc = frappe.get_doc('Communication Channel', channel, for_update=True)
		doc.check_permission('write')
		if doc.status != status:
			if status == 'Enabled':
				with channel_status_write():
					doc.status = status
					doc.save()
			else:
				frappe.db.set_value(doc.doctype, doc.name, 'status', status)
		return dict(channel=doc.name, status=status)
	except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
		frappe.throw(_("Channel status changed concurrently. Reload the channel and retry."), frappe.TimestampMismatchError)


def get_channel_quota_policy(channel_name: str, *, settings=None, operational: bool = False) -> CommunicationChannel:
	"""Resolve and validate the quota policy owned by the given Communication Channel."""
	if not isinstance(channel_name, str) or not channel_name.strip() or not frappe.db.exists("Communication Channel", channel_name):
		frappe.throw(_("A valid Communication Channel is required to resolve the quota policy."), frappe.ValidationError)
	channel = frappe.get_doc("Communication Channel", channel_name)
	channel._validate_quotas(operational=operational, settings=settings)
	return channel
