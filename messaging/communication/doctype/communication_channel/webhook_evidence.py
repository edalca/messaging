"""Private receipt evidence for the shared Meta webhook."""
import json
from functools import partial

import frappe

DOCTYPE = 'Communication Channel'
FIELD = 'meta_webhook_evidence'
TURN_STATES = {
	'Pending': 'pending',
	'Running': 'running',
	'Blocked': 'blocked',
}
FINISHED_STATES = {
	'Complete': 'complete',
	'Ignored': 'ignored',
	'Human Paused': 'human_paused',
	'Quota Blocked': 'quota_blocked',
}


def after_response(request, channel, method, receipt):
	"""Persist only normalized evidence once Frappe has finished the request transaction."""
	if callbacks := getattr(request, 'after_response', None):
		callbacks.add(partial(_record, channel.name, channel.creation, method, receipt))


def _stored(value):
	return json.loads(value) if isinstance(value, str) else value or {}


def _record(channel, creation, method, receipt):
	try:
		current = frappe.db.get_value(DOCTYPE, channel, ['name', 'creation', FIELD], for_update=True, as_dict=True)
		if current and (current.name, current.creation) == (channel, creation):
			evidence = _stored(current[FIELD])
			if not evidence.get(method) or evidence[method]['received_at'] < receipt['received_at']:
				evidence[method] = receipt
				frappe.db.set_value(DOCTYPE, channel, FIELD, json.dumps(evidence), update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.logger('messaging').warning('Could not record Meta webhook evidence.')


def read(channel):
	"""Project receipt evidence without exposing internal turn references."""
	evidence = _stored(channel.get(FIELD))
	post = evidence.get('post')
	if post:
		post = dict(post)
		turn = post.pop('turn', None)
		if turn:
			state = frappe.db.get_value('Chat Turn', turn, ['inbox_state', 'status'], as_dict=True)
			if state:
				states, key = (FINISHED_STATES, state.status) if state.inbox_state == 'Done' else (TURN_STATES, state.inbox_state)
				post['processing'] = states.get(key, 'unconfirmed')
		evidence = {**evidence, 'post': post}
	return evidence
