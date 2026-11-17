"""Execute configured AI providers through their installed operation or pinned HTTPS."""
import base64
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from uuid import uuid4
from types import MappingProxyType
from urllib.parse import urlsplit

import frappe
from frappe import _
from frappe.utils.password import get_decrypted_password, get_encryption_key
import requests
import urllib3

from messaging.chatbot.doctype.ai_provider.ai_provider import DEFINITION_FIELDS, validate_scalar, RESERVED_REQUEST_KEYS, REST_TRANSPORT_KEYS
from messaging.registry.ai_providers import internal_provider
from messaging.registrations.ai_providers import AIServiceError, InvalidInterpretation, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, SCHEMA_VERSION, ProviderTimeout, ProviderUnavailable, AICapability, DecisionRequest, ParameterExtractionRequest, validate_request, validate_response, deserialize_response, to_json, serialize, strict_json


def provider_for(doc, *, lock=False):
    return frappe.get_doc('AI Provider', doc.provider, for_update=lock)


def invalidate_configuration(doc):
    """Require manual validation after a relevant provider or approval change."""
    if not doc.enabled and doc.validation_status == 'not_validated' and not doc.validated_hash:
        return
    data = dict(enabled=0, validated_hash=None, attempted_hash=None,
        validation_status='not_validated', diagnostic=_(DIAGNOSTICS['not_validated']),
        evaluation_generation=uuid4().hex)
    frappe.db.set_value('AI Configuration', doc.name, data)
    doc.update(data)


def configuration_values(doc, provider, *, required=False):
    definitions = {row.parameter_key: row.as_dict() for row in provider.parameters}
    values = {}
    for row in doc.parameters:
        definition = definitions.get(row.parameter_key)
        if not definition or row.parameter_key in values:
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
        if key not in values:
            values[key] = validate_scalar(definition, definition['default_value'])
        if required and definition['required'] and values[key] in (None, ''):
            raise AIServiceError('configuration_incomplete')
    return values


def fingerprint(provider, values, *, behavior_only=True):
    definition = {key: provider.get(key) for key in (
        'name', 'enabled', 'provider_type', 'schema_version', 'decision', 'parameter_extraction', 'application')}
    definition['parameters'] = [{key: row.get(key) for key in DEFINITION_FIELDS} for row in provider.parameters]
    if behavior_only:
        definition['parameters'] = sorted([{k: v for k, v in row.items()
            if k not in ('label', 'description', 'sort_order')} for row in definition['parameters']], key=lambda r: r['parameter_key'])
    # A keyed digest detects secret changes without publishing a reusable credential digest.
    body = json.dumps(dict(provider=definition, values=values), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    return hmac.new(get_encryption_key().encode(), body, hashlib.sha256).hexdigest()


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


def validate_configuration(provider, values, *, capability=AICapability.DECISION):
    if provider.schema_version != SCHEMA_VERSION:
        raise AIServiceError('contract_invalid')
    if type(capability) is not AICapability or not provider.get('decision' if capability is AICapability.DECISION else 'parameter_extraction'):
        raise AIServiceError('capability_unavailable')
    if not provider.enabled:
        raise AIServiceError('provider_disabled')
    if type(values.get('timeout')) is not int or not 1 <= values['timeout'] <= 60:
        raise AIServiceError('configuration_incomplete')
    for key in ('minimum_confidence',) if capability is AICapability.DECISION else ():
        value = values.get(key)
        if type(value) not in (int, float) or not 0.5 < value <= 1:
            raise AIServiceError('configuration_incomplete')
    if provider.provider_type == 'Internal':
        definition = internal_provider(provider.provider_id, provider.application)
        from messaging.security.permissions import require_provider_function
        require_provider_function(definition.validate_configuration, definition.app, MappingProxyType(dict(values)))
        definition.validate_configuration(MappingProxyType(dict(values)))
    else:
        endpoint_target(values.get('endpoint'), resolve=False)
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


def endpoint_target(endpoint, *, resolve=True):
    """Resolve once, validate every address, then connect to that exact address with TLS SNI.

    Private destinations require an exact hostname allowlist in trusted site configuration.
    Neither provider documents nor API callers can relax the network policy.
    """
    try:
        parts = urlsplit(endpoint or '')
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        if (parts.scheme != 'https' or not host or parts.username or parts.password
            or parts.fragment or parts.query or '\\' in endpoint
            or any(ord(c) < 33 for c in endpoint)):
            raise ValueError
        port = parts.port or 443
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise AIServiceError('configuration_incomplete') from None
    if not resolve:
        return host, port, parts.path or '/', None
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    except OSError:
        raise AIServiceError('unavailable') from None
    allowed_hosts = frappe.conf.get('messaging_ai_allowed_hosts') or []
    allow_private = isinstance(allowed_hosts, list) and host in allowed_hosts
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses) and not allow_private:
        raise AIServiceError('destination_blocked')
    return host, port, parts.path or '/', addresses[0]


def _present(value):
    return value is not None and value != ''


def provider_parameters(provider, values):
    """Additional non-secret external values, in stable key order.

    An empty result means the request omits provider_parameters. Internal adapters
    do not use this object. Password values and transport settings are rejected
    here instead of being copied into ordinary JSON.
    """
    if provider.provider_type != 'External':
        return {}
    payload = {}
    for row in provider.parameters:
        key = row.parameter_key
        if key in REST_TRANSPORT_KEYS:
            continue
        if row.field_type == 'Password' or key in RESERVED_REQUEST_KEYS:
            raise AIServiceError('configuration_incomplete')
        value = values.get(key)
        if not _present(value):
            if row.required:
                raise AIServiceError('configuration_incomplete')
            continue
        payload[key] = value
    return {key: payload[key] for key in sorted(payload)}


def outbound_request(provider, values, request):
    """Return the JSON document sent to an external endpoint. Internal requests stay unchanged."""
    parameters = provider_parameters(provider, values)
    outbound = to_json(validate_request(request))
    if not parameters:
        return outbound
    outbound['provider_parameters'] = parameters
    return outbound


def rest_interpret(request, values, *, _request_evidence=None):
    host, port, path, address = endpoint_target(values['endpoint'])
    payload = serialize(request)
    if _request_evidence is not None:
        _request_evidence['request_text'] = strict_json(payload)['message']['text']
    if len(payload) > MAX_REQUEST_BYTES:
        raise InvalidInterpretation('Oversized interpretation request')
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
        'Host': ('[' + host + ']' if ':' in host else host) + (':' + str(port) if port != 443 else '')}
    if values['auth_type'] == 'Bearer':
        headers['Authorization'] = 'Bearer ' + values['auth_secret']
    elif values['auth_type'] == 'Basic':
        token = (values['username'] + ':' + values['auth_secret']).encode()
        headers['Authorization'] = 'Basic ' + base64.b64encode(token).decode('ascii')
    timeout = values['timeout']
    pool = urllib3.HTTPSConnectionPool(address, port=port, server_hostname=host,
        assert_hostname=host, cert_reqs='CERT_REQUIRED', ca_certs=requests.certs.where(), maxsize=1)
    response = None
    started = time.monotonic()
    try:
        if _request_evidence is not None:
            _request_evidence['transmission_state'] = 'Attempted'
        response = pool.urlopen('POST', path, body=payload, headers=headers,
            timeout=urllib3.Timeout(connect=min(5, timeout), read=timeout, total=timeout),
            redirect=False, retries=False, preload_content=False)
        if _request_evidence is not None:
            _request_evidence['transmission_state'] = 'Responded'
            _request_evidence['http_status'] = response.status
        if response.status != 200:
            raise AIServiceError({401:'authentication_error', 403:'authentication_error',
                429:'rate_limited'}.get(response.status, 'unavailable' if response.status >= 500 else 'contract_invalid'))
        if response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise AIServiceError('contract_invalid')
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise AIServiceError('contract_invalid')
        body = bytearray()
        while True:
            chunk = response.read1(8192, decode_content=False)
            if time.monotonic() - started > timeout:
                raise AIServiceError('timeout')
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                from messaging.chatbot.doctype.ai_request_log.ai_request_log import capture_response
                capture_response(body)
                raise AIServiceError('contract_invalid')
        from messaging.chatbot.doctype.ai_request_log.ai_request_log import capture_response
        capture_response(body)
        return strict_json(body)
    except urllib3.exceptions.TimeoutError:
        raise AIServiceError('timeout') from None
    except (urllib3.exceptions.HTTPError, OSError):
        raise AIServiceError('unavailable') from None
    finally:
        if response:
            response.close()
        pool.close()


def run(provider, values, request):
    """Execute exactly the requested declared capability with the same trust boundary."""
    validate_request(request)
    capability = AICapability.DECISION if type(request) is DecisionRequest else AICapability.PARAMETER_EXTRACTION
    validate_configuration(provider, values, capability=capability)
    from messaging.chatbot.doctype.ai_request_log.ai_request_log import provider_call, record_call
    with provider_call(provider, values, request) as diagnostic:
        try:
            if provider.provider_type == 'Internal':
                definition = internal_provider(provider.provider_id, provider.application)
                operation = definition.decision if capability is AICapability.DECISION else definition.parameter_extraction
                if operation is None:
                    raise AIServiceError('capability_unavailable')
                from messaging.security.permissions import require_provider_function
                require_provider_function(operation.execute, definition.app, request, MappingProxyType(dict(values)))
                result = validate_response(operation.execute(request, MappingProxyType(dict(values))), request)
            else:
                started, error = time.monotonic(), None
                outbound = outbound_request(provider, values, request)
                evidence = dict(request_text=request.message.text, transmission_state='Prepared')
                try:
                    result = deserialize_response(rest_interpret(outbound, values, _request_evidence=evidence), request)
                except Exception as exc:
                    error = error_code(exc)
                    raise
                finally:
                    record_call(None, None, {}, (time.monotonic()-started)*1000, error,
                        provider=provider.name, requested_model=f'contract-v{SCHEMA_VERSION}', operation=capability, **evidence)
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
    'configuration_disabled': 'The AI configuration is disabled.',
    'no_active_configuration': 'No active AI configuration is selected.',
    'no_provider': 'No AI provider is configured.',
    'interpretation_disabled': 'Chatbot AI interpretation is disabled.',
}


def diagnose(doc, code, current_hash=None, *, checked=False):
    from messaging.common.primitives import utcnow
    data = dict(validation_status=code, diagnostic=_(DIAGNOSTICS[code]))
    if checked:
        data.update(last_checked=utcnow(), attempted_hash=current_hash,
            validated_hash=current_hash if code == 'operational' else None)
    elif code != 'operational':
        data['validated_hash'] = None
    frappe.db.set_value(doc.doctype, doc.name, data, update_modified=False)
    doc.update(data)
    return dict(status=code, operational=code == 'operational', diagnostic=_(DIAGNOSTICS[code]),
        configuration=doc.name, provider=doc.provider, last_checked=doc.last_checked)
