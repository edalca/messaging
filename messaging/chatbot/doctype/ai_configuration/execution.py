"""Execute configured AI providers through their installed operation or pinned HTTPS."""
from dataclasses import asdict
import base64
import hashlib
import hmac
import json
import time
from types import MappingProxyType

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password, get_encryption_key

from messaging.chatbot.doctype.ai_provider.ai_provider import DEFINITION_FIELDS, validate_scalar, RESERVED_REQUEST_KEYS, REST_TRANSPORT_KEYS
from messaging.registry.ai_providers import internal_provider
from .policy import DecisionPolicy, profile_policy, RESERVED_KEYS
from .rest_transport import bounded_exchange, endpoint_target
from messaging.registry.ai_providers import validate_decision_evidence, validate_extraction_request, validate_extraction_response
from messaging.registry.ai_providers import AIServiceError, InvalidInterpretation, MAX_REQUEST_BYTES, SCHEMA_VERSION, ProviderTimeout, ProviderUnavailable, AICapability, DecisionRequest, DecisionClassification, DecisionSignal, validate_request, to_json, serialize

from messaging.registry.ai_providers.rest import WIRE_VERSIONS, deserialize_rest_response, serialize_rest_request


def provider_for(doc, *, lock=False):
    return frappe.get_doc('AI Provider', doc.provider, for_update=lock)


def set_profile_status(doc, status, *, reason=None):
    """Write a server-owned transition while the profile row is locked."""
    data = dict(status=status)
    if reason:
        data.update(validation_status=reason, diagnostic=_(DIAGNOSTICS[reason]))
    if all(doc.get(key) == value for key, value in data.items()):
        return
    frappe.db.set_value('AI Configuration', doc.name, data)
    doc.update(data)


def invalidate_configuration(doc, reason='evaluation_invalidated'):
    """Withdraw operating authority while retaining historical evidence and secrets."""
    if doc.status in ('Enabled', 'Disabled', 'Unavailable'):
        set_profile_status(doc, 'Unavailable', reason=reason)


def require_provider_available(provider):
    if provider.status != 'Enabled':
        raise AIServiceError('provider_unavailable' if provider.provider_type == 'Internal' else 'provider_disabled')


def configuration_values(doc, provider, *, required=False):
    definitions = {row.parameter_key: row.as_dict() for row in provider.parameters}
    values = {}
    for row in doc.parameters:
        definition = definitions.get(row.parameter_key)
        if not definition or row.parameter_key in values or row.parameter_key in RESERVED_KEYS:
            frappe.throw(_('Invalid or duplicate provider parameter.'))
        if definition['field_type'] == 'Password':
            value = row.secret_value
            if value and row.is_dummy_password(value):
                value = get_decrypted_password(row.doctype, row.name, 'secret_value', raise_exception=False)
        else:
            try:
                value = json.loads(row.value) if row.value else None
            except (ValueError, TypeError):
                frappe.throw(_('Invalid provider parameter value.'))
        values[row.parameter_key] = validate_scalar(definition, value)
    for key, definition in definitions.items():
        if key in RESERVED_KEYS:
            raise AIServiceError('configuration_incomplete')
        if key not in values:
            default = None if provider.provider_type == 'External' and key == 'endpoint' else definition['default_value']
            values[key] = validate_scalar(definition, default)
        if required and definition['required'] and values[key] in (None, ''):
            raise AIServiceError('configuration_incomplete')
    return values


def fingerprint(provider, values, *, behavior_only=True, policy=None, rest_wire=None):
    definition = {key: provider.get(key) for key in (
        'name', 'provider_type', 'schema_version', 'decision', 'parameter_extraction', 'application')}
    # Keep the digest format used by prior evaluations of available providers.
    # Temporary withdrawal changes eligibility independently of evaluated behavior.
    definition['enabled'] = 1
    definition['parameters'] = [{key: row.get(key) for key in DEFINITION_FIELDS} for row in provider.parameters]
    if provider.provider_type == 'External':
        for row, fields in zip(provider.parameters, definition['parameters'], strict=True):
            fields['send_to_rest'] = bool(row.send_to_rest)
    if behavior_only:
        definition['parameters'] = sorted([{k: v for k, v in row.items()
            if k not in ('label', 'description', 'sort_order')} for row in definition['parameters']], key=lambda r: r['parameter_key'])
    # A keyed digest detects secret changes without publishing a reusable credential digest.
    data = dict(provider=definition, values=values)
    if rest_wire is not None:
        data['rest_wire'] = rest_wire
    if policy is not None:
        data['decision_policy'] = asdict(policy)
    body = json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hmac.new(get_encryption_key().encode(), body, hashlib.sha256).hexdigest()


def profile_fingerprint(doc, provider, values, *, behavior_only=True):
    rest_wire = WIRE_VERSIONS[AICapability(doc.profile_type)] if provider.provider_type == 'External' else None
    return fingerprint(provider, values, behavior_only=behavior_only, policy=profile_policy(doc, provider), rest_wire=rest_wire)


def error_code(exc):
    if isinstance(exc, AIServiceError):
        return exc.code
    if isinstance(exc, (TimeoutError, ProviderTimeout)):
        return 'timeout'
    if isinstance(exc, InvalidInterpretation):
        return 'contract_invalid'
    if isinstance(exc, (ProviderUnavailable, ConnectionError)):
        return 'unavailable'
    return 'internal_error'


def validate_configuration(provider, values, *, capability=AICapability.DECISION, policy=None):
    if provider.schema_version != SCHEMA_VERSION:
        raise AIServiceError('contract_invalid')
    if type(capability) is not AICapability or not provider.get('decision' if capability is AICapability.DECISION else 'parameter_extraction'):
        raise AIServiceError('capability_unavailable')
    require_provider_available(provider)
    if type(values.get('timeout')) is not int or not 1 <= values['timeout'] <= 60:
        raise AIServiceError('configuration_incomplete')
    if provider.provider_type == 'Internal':
        definition = internal_provider(provider.provider_id, provider.application)
        from messaging.security.permissions import require_provider_function
        if policy is not None and (capability is not AICapability.DECISION or type(policy) is not DecisionPolicy):
            raise AIServiceError('configuration_incomplete')
        options = dict(values)
        if RESERVED_KEYS.intersection(options):
            raise AIServiceError('configuration_incomplete')
        options = MappingProxyType(options)
        require_provider_function(definition.validate_configuration, definition.app, options)
        definition.validate_configuration(options)
        return definition, options
    else:
        endpoint_target(values.get('endpoint'))
        if values.get('auth_type') not in ('None', 'Bearer', 'Basic'):
            raise AIServiceError('configuration_incomplete')
        if values['auth_type'] != 'None' and not values.get('auth_secret'):
            raise AIServiceError('configuration_incomplete')
        if values['auth_type'] == 'Basic' and not values.get('username'):
            raise AIServiceError('configuration_incomplete')
        for key in ('auth_secret', 'username'):
            if any(c in (values.get(key) or '') for c in ('\r', '\n', '\x00')):
                raise AIServiceError('configuration_incomplete')
        provider_parameters(provider, values)


def _present(value):
    return value is not None and value != ''


def provider_parameters(provider, values):
    """Project only declared scalar values expressly authorized for the REST body."""
    definitions = {row.parameter_key: row for row in provider.parameters}
    if values.keys() - definitions.keys() - REST_TRANSPORT_KEYS:
        raise AIServiceError('configuration_incomplete')
    payload = {}
    for key, row in sorted(definitions.items()):
        if key in REST_TRANSPORT_KEYS:
            if row.send_to_rest:
                raise AIServiceError('configuration_incomplete')
            continue
        if row.field_type == 'Password' or key in RESERVED_REQUEST_KEYS:
            raise AIServiceError('configuration_incomplete')
        value = validate_scalar(row.as_dict(), values.get(key))
        if not _present(value):
            if row.required:
                raise AIServiceError('configuration_incomplete')
        elif row.send_to_rest:
            if value == values.get('auth_secret'):
                raise AIServiceError('configuration_incomplete')
            payload[key] = value
    return payload


def outbound_request(provider, values, request):
    """Build the provider-facing request for the selected capability."""
    if provider.provider_type == 'Internal':
        return to_json(validate_request(request))
    outbound = serialize_rest_request(request)
    parameters = provider_parameters(provider, values)
    if parameters:
        outbound['provider_parameters'] = parameters
    return outbound


def rest_interpret(request, values, *, _request_evidence=None):
    target = endpoint_target(values.get('endpoint'))
    host, port, _path = target
    payload = serialize(request)
    if len(payload) > MAX_REQUEST_BYTES:
        raise InvalidInterpretation('Oversized interpretation request')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
        'Host': ('[' + host + ']' if ':' in host else host) + (':' + str(port) if port != 443 else '')}
    if values['auth_type'] == 'Bearer':
        headers['Authorization'] = 'Bearer ' + values['auth_secret']
    elif values['auth_type'] == 'Basic':
        token = (values['username'] + ':' + values['auth_secret']).encode()
        headers['Authorization'] = 'Basic ' + base64.b64encode(token).decode('ascii')
    configured_hosts = frappe.conf.get('messaging_ai_allowed_hosts')
    allowed_hosts = configured_hosts if isinstance(configured_hosts, list) else []
    # The subprocess may have transmitted before a deadline interrupts the exchange.
    if _request_evidence is not None:
        _request_evidence['transmission_state'] = 'Attempted'
    evidence, body = bounded_exchange(target, payload, headers, values['timeout'], allowed_hosts)
    error = evidence.pop('error', None)
    if _request_evidence is not None:
        _request_evidence.update(evidence)
    if error:
        raise AIServiceError(error)
    from messaging.chatbot.doctype.ai_request_log.ai_request_log import capture_response
    capture_response(body, public_values=(*DecisionClassification, *DecisionSignal, request['wire_version']),
        protect_evaluation=True)
    return body


def run(provider, values, request, *, policy=None):
    """Execute exactly the requested declared capability with the same trust boundary."""
    capability = AICapability.DECISION if type(request) is DecisionRequest else AICapability.PARAMETER_EXTRACTION
    if provider.provider_type == 'Internal' and capability is AICapability.PARAMETER_EXTRACTION:
        validate_extraction_request(request)
    else:
        validate_request(request)
    internal = validate_configuration(provider, values, capability=capability, policy=policy)
    from messaging.chatbot.doctype.ai_request_log.ai_request_log import provider_call, record_call
    with provider_call(provider, values, request) as diagnostic:
        try:
            if provider.provider_type == 'Internal':
                definition, options = internal
                decision = capability is AICapability.DECISION
                operation = definition.decision if decision else definition.parameter_extraction
                if operation is None:
                    raise AIServiceError('capability_unavailable')
                from messaging.security.permissions import require_provider_function
                require_provider_function(operation.execute, definition.app, request, options)
                response = operation.execute(request, options)
                validator = validate_decision_evidence if decision else validate_extraction_response
                result = validator(response, request)
            else:
                started, error = time.monotonic(), None
                outbound = outbound_request(provider, values, request)
                evidence = dict(request_text=request.message.text, transmission_state='Prepared')
                try:
                    result = deserialize_rest_response(rest_interpret(outbound, values, _request_evidence=evidence), request)
                except Exception as exc:
                    error = error_code(exc)
                    raise
                finally:
                    record_call(None, None, {}, (time.monotonic()-started)*1000, error,
                        provider=provider.name, requested_model=WIRE_VERSIONS[capability], operation=capability, **evidence)
            diagnostic.update(normalized=result, contract_validation='Passed')
            return result
        except Exception as exc:
            diagnostic.update(error=error_code(exc), exception_type=type(exc).__name__, contract_validation='Failed')
            raise


DIAGNOSTICS = {
    'evaluation_missing': 'This AI profile has no technical evaluation.',
    'evaluation_pending': 'The technical evaluation is pending.',
    'evaluation_running': 'The technical evaluation is running.',
    'evaluation_failed': 'The mandatory technical evaluation failed.',
    'evaluation_incomplete': 'The technical evaluation was interrupted or incomplete.',
    'evaluation_invalidated': 'Technical evaluation evidence was invalidated. Run a new evaluation.',
    'evaluation_context': 'The technical evaluation does not cover the current languages or applications.',
    'evaluation_unavailable': 'No compatible evaluation executor is available.',
    'evaluation_policy': 'The stored evaluation uses a policy that is no longer current.',
    'evaluation_test': 'Test evidence cannot approve an operational AI profile.',
    'capability_unavailable': 'The AI provider does not declare the requested capability.',
    'extraction_inactive': 'Parameter extraction is not available. This profile stores configuration only.',
    'decision_required': 'Select a Decision AI profile.',
    'operational': 'AI service operational.',
    'not_validated': 'The AI configuration has not passed connection validation.',
    'configuration_incomplete': 'The AI configuration is incomplete.',
    'authentication_error': 'The AI credential was rejected.',
    'unavailable': 'The AI service is temporarily unreachable.',
    'timeout': 'The AI connection timed out.',
    'internal_error': 'An unexpected error prevented the AI connection check. Consult the server error log.',
    'rate_limited': 'The AI service rate limit was reached.',
    'contract_invalid': 'The AI service returned an incompatible contract.',
    'destination_blocked': 'The AI destination is blocked by the network policy.',
    'provider_disabled': 'The AI provider is disabled.',
    'provider_unavailable': 'The registered AI provider is unavailable.',
    'configuration_untested': 'Run a technical evaluation before using this AI profile.',
    'configuration_unavailable': 'This AI profile is unavailable. Review its diagnostic before evaluating it.',
    'configuration_disabled': 'The AI configuration is disabled.',
    'no_active_configuration': 'No active AI configuration is selected.',
    'no_provider': 'No AI provider is configured.',
    'interpretation_disabled': 'Chatbot AI interpretation is disabled.',
}


def diagnose(doc, code, current_hash=None, *, checked=False):
    from messaging.common.primitives import utcnow
    current = frappe.db.get_value(doc.doctype, doc.name, ('status', 'evaluation_generation', 'validation_status'),
        as_dict=True, for_update=True)
    data = dict(validation_status=code, diagnostic=_(DIAGNOSTICS[code]))
    if checked:
        data.update(last_checked=utcnow(), attempted_hash=current_hash)
        if code == 'operational':
            data['validated_hash'] = current_hash
    confirmed = code in ('authentication_error', 'configuration_incomplete', 'contract_invalid', 'destination_blocked')
    if current and current.evaluation_generation == doc.evaluation_generation and current.status == doc.status:
        if confirmed and doc.status in ('Enabled', 'Disabled'):
            data['status'] = 'Unavailable'
        if doc.status == 'Unavailable' and (code in ('operational', 'timeout', 'unavailable', 'rate_limited')
                or current.validation_status in ('provider_unavailable', 'provider_disabled')):
            data.pop('validation_status')
            data.pop('diagnostic')
        if data:
            frappe.db.set_value(doc.doctype, doc.name, data, update_modified='status' in data)
            doc.update(data)
    return dict(status=code, operational=code == 'operational', diagnostic=_(DIAGNOSTICS[code]),
        configuration=doc.name, provider=doc.provider, last_checked=doc.last_checked, profile_status=doc.status)
