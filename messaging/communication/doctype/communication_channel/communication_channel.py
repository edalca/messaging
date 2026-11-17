"""Configured provider instances with immutable identity and scoped values."""
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import frappe
from frappe import _
from frappe.model.document import Document

from messaging.communication.doctype.communication_provider_value.communication_provider_value import validate_values, cleanup_values

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


class CommunicationChannel(Document):
	def autoname(self):
		fixed = frappe.db.get_value('Communication Provider', self.provider, 'fixed_channel_id')
		if fixed and not frappe.db.exists(self.doctype, fixed):
			self.name = fixed

	def before_validate(self):
		if self.quotas_enabled:
			self.quota_consumption_basis = "Provider Accepted"
			self.composite_response_single_unit = 1
			self.reconcile_quota_on_acceptance = 1

	def validate(self):
		old = self.get_doc_before_save()
		if old and self.provider != old.provider:
			frappe.throw(_("Channel provider identity is immutable."))
		frappe.db.get_value('Communication Provider', self.provider, 'name', for_update=True)
		from messaging.communication.doctype.communication_provider.communication_provider import provider_definition
		provider_definition(self.provider)
		if self.is_new():
			from messaging.communication.doctype.communication_provider.communication_provider import require_provider_enabled
			require_provider_enabled(self.provider)
		catalog = frappe.get_doc('Communication Provider', self.provider)
		self.single_provider_key = self.provider if catalog.single_channel else None
		if self.is_new() and catalog.fixed_channel_id:
			owner = frappe.db.get_value(self.doctype, catalog.fixed_channel_id, 'provider')
			if owner and owner != self.provider:
				frappe.throw(_("The fixed channel ID belongs to another provider."))
			if self.name != catalog.fixed_channel_id and owner != self.provider:
				frappe.throw(_("The channel identity must match the provider's fixed channel ID."))
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


def get_channel_quota_policy(channel_name: str, *, settings=None, operational: bool = False) -> CommunicationChannel:
	"""Resolve and validate the quota policy owned by the given Communication Channel."""
	if not isinstance(channel_name, str) or not channel_name.strip() or not frappe.db.exists("Communication Channel", channel_name):
		frappe.throw(_("A valid Communication Channel is required to resolve the quota policy."), frappe.ValidationError)
	channel = frappe.get_doc("Communication Channel", channel_name)
	channel._validate_quotas(operational=operational, settings=settings)
	return channel
