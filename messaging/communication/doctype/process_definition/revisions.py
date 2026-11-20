"""Persisted Process metadata integrity independent of functional discovery."""
import hashlib
import json
from pathlib import Path
import frappe

def canonical_json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _registration_identity(definition):
    """Include both function references once the definition reference is persisted."""
    keys = ['process_id', 'application', 'process_type', 'method', 'contract_version']
    if definition.get('definition_method'):
        keys.insert(3, 'definition_method')
    return {key: definition.get(key) for key in keys}


def registration_revisions(definition, snapshot):
    """Stable identities exclude deployment paths, labels and file timestamps."""
    identity = _registration_identity(definition)
    structural = dict(identity, contract=snapshot['declaration']['contract'])
    effective = dict(structural, declaration=snapshot['declaration'],
        sources={module: snapshot['sources'][module]['digest'] for module in snapshot['implementation_sources']},
        policy=snapshot['policy'])
    return {name: hashlib.sha256(canonical_json(value).encode()).hexdigest() for name, value in
        (('contract_revision', structural), ('capability_revision', effective))}




def runtime_sources():
	"""The same bounded runtime manifest governs registration and loaded-code checks."""
	root = Path(frappe.get_app_path('messaging'))
	return {'messaging.' + name.removesuffix('.py').replace('/', '.').removesuffix('.__init__'):
		dict(path=str(root / name), digest=hashlib.sha256((root / name).read_bytes()).hexdigest())
		for name in (
				"communication/doctype/process_definition/execution.py",
				"communication/doctype/process_definition/approval.py",
				"registry/processes/discovery.py",
                "registry/processes/context.py",
                "registry/processes/inspection.py",
				"communication/doctype/process_definition/validation.py",
				"communication/doctype/process_definition/process_definition.py",
				"registry/processes/__init__.py",
				"registry/processes/contracts.py",
                "registry/processes/operations.py",
                "communication/doctype/process_definition/revisions.py",
				"registry/_catalog.py",
				"security/permissions.py",
				"security/execution_context.py",
				"communication/doctype/notification_rule/notification_rule.py",
				"communication/doctype/notification_rule/conditions.py",
				"communication/doctype/notification_rule/contract.py",
				"communication/doctype/communication_provider_value/communication_provider_value.py",
				"communication/doctype/notification_rule/events.py",
				"communication/doctype/communication_template/communication_template.py",
				"communication/doctype/communication_template/external.py",
				"communication/doctype/communication_template/languages.py",
				"conversation/doctype/chat_turn/chat_turn.py",
				"conversation/doctype/chat_turn/inbox.py",
				"common/locking.py",
				"common/primitives.py",
				"conversation/doctype/chat_contact/chat_contact.py",
				"chatbot/doctype/bot_flow/contract.py",
				"chatbot/doctype/bot_flow/localization.py",
				"chatbot/doctype/bot_settings/language.py",
				"chatbot/doctype/bot_flow/execution.py",
				"chatbot/doctype/bot_flow/bot_flow.py",
				"conversation/doctype/chat_session/chat_session.py",
				"conversation/doctype/chat_session/human.py",
				"chatbot/doctype/bot_parameter/parameters/engine.py",
				"chatbot/doctype/bot_parameter/parameters/decimal.py",
				"chatbot/doctype/bot_parameter/parameters/boolean.py",
				"chatbot/doctype/bot_parameter/parameters/__init__.py",
				"chatbot/doctype/bot_settings/bot_settings.py",
				"chatbot/doctype/bot_settings/flow_consumers.py",
				"chatbot/doctype/bot_flow_state/bot_flow_state.py",
			)}


def runtime_revision():
	values = {module: source['digest'] for module, source in runtime_sources().items()}
	return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
