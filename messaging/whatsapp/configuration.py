"""WhatsApp configuration on Messaging channel, account and template documents."""
import json
from urllib.parse import quote, urlencode

import frappe
from frappe.utils.password import get_decrypted_password

from messaging.common.public_origin import configured_public_origin
from messaging.common.write_guards import InternalWriteGuard
from messaging.whatsapp.api import DEFAULT_VERSION, Graph, numeric_id, version_valid

PROVIDER = 'WHATSAPP'
VERIFY_TOKEN = 'verify_token'
WEBHOOK_PATH = '/api/method/messaging.whatsapp.webhook.handler'
webhook_token_write = InternalWriteGuard('messaging_whatsapp_webhook_token_write')


def callback_url(channel):
	"""Build the Meta callback from the configured public origin and saved channel ID."""
	origin = configured_public_origin()
	if origin:
		query = urlencode({'channel': channel}, quote_via=quote)
		return f'{origin}{WEBHOOK_PATH}?{query}'


def value(doc, key, default=None):
	row = next((row for row in doc.parameters if row.parameter_key == key), None)
	return json.loads(row.value) if row and row.value else default


def graph_version(channel):
	return value(channel, 'graph_version', DEFAULT_VERSION)


def secret(doc, key):
	row = next((row for row in doc.parameters if row.parameter_key == key), None)
	return get_decrypted_password(row.doctype, row.name, 'secret_value', raise_exception=False) if row else None


def verify_token_row(doc):
	return next((row for row in doc.parameters if row.parameter_key == VERIFY_TOKEN), None) if doc else None


def valid_account(binding):
	channel = frappe.get_doc('Communication Channel', binding.channel)
	return (channel.provider == PROVIDER and numeric_id(binding.account_id)
		and numeric_id(value(binding, 'waba_id')) and version_valid(graph_version(channel)))


def client(account):
	from messaging.communication.doctype.communication_provider_value.communication_provider_value import transport_parameters
	parameters = transport_parameters(PROVIDER, account)
	return Graph(account, parameters['Account']['waba_id'], parameters['Account']['access_token'],
		parameters['Channel']['graph_version'])


def validate_channel(doc, method=None):
	if doc.provider != PROVIDER:
		return
	previous_doc = doc.get_doc_before_save()
	app_id = value(doc, 'app_id')
	# Existing enabled channels can retain their credentials until the administrator supplies the ID.
	if app_id is None and doc.status == 'Enabled' and (
		not previous_doc or previous_doc.status != 'Enabled' or value(previous_doc, 'app_id') is not None
	):
		frappe.throw(frappe._('Enter the Meta App ID before enabling the WhatsApp channel.'))
	if app_id is not None:
		if not numeric_id(app_id):
			frappe.throw(frappe._('Use a numeric Meta App ID.'))
		# Channel validation already holds the provider row lock. Use a current read
		# so concurrent saves see the preceding transaction's configuration.
		param = frappe.qb.DocType('Communication Provider Value')
		channel = frappe.qb.DocType('Communication Channel')
		duplicate = (frappe.qb.from_(param).join(channel).on(param.parent == channel.name)
			.select(param.name).where(
				(param.parenttype == doc.doctype) & (param.parentfield == 'parameters')
				& (param.parameter_key == 'app_id') & (param.value == json.dumps(app_id))
				& (channel.provider == PROVIDER) & (channel.name != doc.name)
			).limit(1).for_update().run())
		if duplicate:
			frappe.throw(frappe._('This Meta App ID is already configured on another WhatsApp channel.'))
	if not version_valid(graph_version(doc)):
		frappe.throw(frappe._('Invalid WhatsApp Graph API version.'))
	if doc.status == 'Enabled' and (not previous_doc or previous_doc.status != 'Enabled'):
		if not callback_url(doc.name):
			frappe.throw(frappe._('Configure a public HTTPS site origin before enabling the WhatsApp channel.'))
		if not all(secret(doc, key) for key in ('app_secret', VERIFY_TOKEN)):
			frappe.throw(frappe._('Configure the Meta App Secret and webhook verify token before enabling the WhatsApp channel.'))
	if not webhook_token_write.active():
		row = verify_token_row(doc)
		previous = verify_token_row(previous_doc)
		if (row and (row.name, row.secret_value or '')) != (previous and (previous.name, previous.secret_value or '')):
			frappe.throw(frappe._('Change the webhook verify token using its administrative actions.'), frappe.PermissionError)


def validate_account(doc, method=None):
	previous = doc.get_doc_before_save()
	if (doc.whatsapp_display_phone_number or '') != ((previous.whatsapp_display_phone_number or '') if previous else ''):
		frappe.throw(frappe._('Only a successful Meta lookup can change the WhatsApp number.'), frappe.PermissionError)
	if frappe.db.get_value('Communication Channel', doc.channel, 'provider') != PROVIDER:
		return
	if bool(doc.enabled) != bool(previous and previous.enabled):
		frappe.throw(frappe._('Change the WhatsApp account status using its administrative actions.'), frappe.PermissionError)
	business = value(doc, 'waba_id')
	if not numeric_id(doc.account_id) or (business is not None and not numeric_id(business)):
		frappe.throw(frappe._('Use numeric values for the Phone Number ID and Messaging Account ID.'))
