"""Global fictitious senders; the engine scopes each conversation by the selected account."""
from datetime import datetime
import re
from secrets import randbelow
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.model.document import Document
from faker import Faker

from messaging.security.permissions import require_system_manager


PROVIDER = 'EMULR'
CHANNEL = 'emulator'
NUMBER_PATTERN = re.compile(r'000[0-9]{15}')


def require_manager():
	require_system_manager(_("Channel Emulator requires System Manager permission."))


def generate_emulator_account_id():
	return datetime.now(ZoneInfo("America/Tegucigalpa")).strftime("EMU-%Y%m%d%H%M%S%f")


def validate_sender(sender):
	# E.164 permits at most 15 digits. This closed 18-digit namespace also has
	# an invalid zero country-code prefix; it never represents an international number.
	if not isinstance(sender, str) or not NUMBER_PATTERN.fullmatch(sender) or frappe.db.exists('User', sender):
		frappe.throw(_("Use an emulated number of 18 digits starting with 000."))
	return sender


def generate_number():
	for _attempt in range(20):
		number = '000' + f'{randbelow(10**15):015d}'
		if not frappe.db.exists('Emulator Contact', {'sender_id': number}) and not frappe.db.exists('User', number):
			return number
	frappe.throw(_("An emulated number could not be generated. Please retry."))


@frappe.whitelist(methods=['POST'])
def new_number():
	require_manager()
	frappe.has_permission('Emulator Contact', 'create', throw=True)
	return generate_number()


class EmulatorContact(Document):
	def before_insert(self):
		# Field-based naming runs before before_validate in Frappe.
		if not self.sender_id:
			self.sender_id = generate_number()

	def before_validate(self):
		if self.is_new() and not self.sender_id:
			self.sender_id = generate_number()
		if not (self.display_name or '').strip():
			self.display_name = Faker('es_ES').name()

	def validate(self):
		require_manager()
		validate_sender(self.sender_id)
		if not self.is_new() and self.has_value_changed('sender_id'):
			frappe.throw(_("Emulator contact identity is immutable."))

	def on_trash(self):
		if (frappe.db.exists('Chat Contact', {'channel': CHANNEL, 'external_sender': self.sender_id})
			or frappe.db.exists('Notification Queue', {'channel': CHANNEL, 'effective_recipient': self.sender_id})):
			frappe.throw(_("An emulator contact with conversation history cannot be deleted."))

	def before_rename(self, *args, **kwargs):
		frappe.throw(_("Emulator contact identity is immutable."))
