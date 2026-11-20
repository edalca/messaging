"""Emulator account and contact fixtures shared by conversation tests.

The helper enables EMULR for one test case. It does not import conversation
test modules, so those modules can prepare an emulator sender without a cycle.
"""
import json
from unittest.mock import patch

import frappe

from messaging.communication.doctype.communication_channel.communication_channel import set_channel_status
from messaging.chatbot.doctype.bot_settings.interpretation import Decision
from messaging.communication.doctype.communication_provider.communication_provider import (
	get_provider_states,
	set_provider_enabled,
	_lock_provider_states,
	_write_provider_states,
)
from messaging.communication.doctype.communication_provider.test_communication_provider import sync_communication_providers
from messaging.emulator.doctype.emulator_contact.emulator_contact import CHANNEL, PROVIDER
from messaging.emulator.page.emulator_console import emulator_console as console


def bind_emulator(case):
	"""Enable the emulator channel and create two accounts plus one contact."""
	case.before = get_provider_states()
	case.contacts = []
	case.bindings = []
	case.users = []
	sync_communication_providers()
	set_provider_enabled(PROVIDER, True)
	case.created_channel = False
	if not frappe.db.exists('Communication Channel', CHANNEL):
		channel = frappe.get_doc(dict(doctype='Communication Channel', provider=PROVIDER, channel_name='Channel Emulator')).insert()
		set_channel_status(channel.name, 'Enabled')
		case.created_channel = True
	frappe.db.set_value('Communication Channel', CHANNEL, case.quota_policy, update_modified=False)
	frappe.clear_document_cache('Communication Channel', CHANNEL)
	case.configured_channels.add(CHANNEL)
	case.http = patch('requests.sessions.Session.request', side_effect=AssertionError('Network forbidden'))
	case.http.start()
	case.addCleanup(case.http.stop)
	case.availability = patch(
		'messaging.chatbot.doctype.ai_configuration.ai_configuration.availability',
		return_value=dict(operational=True, status='operational', diagnostic='AI service operational.'),
	)
	case.availability.start()
	case.addCleanup(case.availability.stop)
	case.interpreter = patch('messaging.conversation.doctype.chat_turn.turn_decision.interpret', return_value=Decision())
	case.interpreter.start()
	case.addCleanup(case.interpreter.stop)
	case.existing_accounts = {row.name for row in console.accounts()['accounts']}
	case.a = emulator_account(case)
	case.b = emulator_account(case)
	contact = emulator_contact(case)
	case.sender = contact.sender_id
	frappe.db.commit()


def release_emulator(case, restore):
	"""Remove emulator fixtures, then restore provider state. `restore` is the caller's own teardown."""
	frappe.db.rollback()
	frappe.set_user('Administrator')
	with patch('frappe.enqueue'):
		for user in case.users:
			frappe.db.delete('Chat Agent', {'user': user})
		frappe.db.commit()
		restore()
		for name in case.contacts:
			frappe.delete_doc('Emulator Contact', name)
		for name in case.bindings:
			frappe.delete_doc('Communication Channel Account', name)
		if getattr(case, 'created_channel', False) and not frappe.db.exists('Communication Channel Account', {'channel': CHANNEL}):
			frappe.delete_doc('Communication Channel', CHANNEL)
		for user in case.users:
			frappe.delete_doc('User', user, force=True)
		_lock_provider_states()
		_write_provider_states(case.before)
		frappe.db.commit()


def emulator_account(case, outcome='Accepted'):
	doc = frappe.get_doc(dict(
		doctype='Communication Channel Account', channel=CHANNEL,
		account_id=case.prefix + '-' + str(len(case.bindings)),
		account_name='Synthetic emulator account', enabled=1,
		parameters=[dict(parameter_key='transport_outcome', value=json.dumps(outcome))],
	)).insert()
	case.bindings.append(doc.name)
	case.accounts.append(doc.name)
	frappe.db.commit()
	return doc.name


def emulator_contact(case, **values):
	doc = frappe.get_doc(dict(doctype='Emulator Contact', **values)).insert()
	case.contacts.append(doc.name)
	frappe.db.commit()
	return doc


def emulator_message(case, account=None, sender=None, text='Hello', client_id=None, **kwargs):
	result = console.send_message(
		account or case.a, sender or case.sender,
		client_id or frappe.generate_hash(length=32), text=text, **kwargs,
	)
	from messaging.conversation.doctype.chat_turn.chat_turn import turn_result
	from messaging.conversation.doctype.chat_turn.inbox import drain_contact
	drain_contact(result['contact'])
	return turn_result(frappe.get_doc('Chat Turn', result['turn']))
