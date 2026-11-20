"""One bounded, DNS-pinned HTTPS exchange for an external AI profile."""
import ipaddress
import json
import re
import socket
import subprocess
import sys
from urllib.parse import urlsplit

import requests
import urllib3

from messaging.registry.ai_providers import AIServiceError, MAX_RESPONSE_BYTES


def endpoint_target(endpoint):
    """Validate an HTTPS URL and keep the exact authority for TLS verification."""
    try:
        if (type(endpoint) is not str or endpoint[:8].lower() != 'https://'
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 or c in '\\?#' for c in endpoint)):
            raise ValueError
        parts = urlsplit(endpoint)
        host = (parts.hostname or '').encode('idna').decode('ascii').lower()
        if not host or '@' in parts.netloc or '%' in host or parts.netloc.endswith(':'):
            raise ValueError
        if ':' in host:
            ipaddress.IPv6Address(host)
            if not re.fullmatch(r'\[[^\]]+\](?::[0-9]+)?', parts.netloc):
                raise ValueError
        elif len(host.rstrip('.')) > 253 or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in host.removesuffix('.').split('.')
        ):
            raise ValueError
        port = parts.port
        if port == 0 or parts.netloc.startswith('[') and ':' not in host:
            raise ValueError
        return host, 443 if port is None else port, parts.path or '/'
    except (ValueError, TypeError, UnicodeError):
        raise AIServiceError('configuration_incomplete') from None


def _resolve(host, port, allowed_hosts):
    resolved = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = set()
    for item in resolved:
        address = ipaddress.ip_address(item[4][0])
        if isinstance(address, ipaddress.IPv6Address):
            if address.scope_id is not None or item[4][3]:
                raise AIServiceError('destination_blocked')
            address = address.ipv4_mapped or address
        if (address.is_multicast or address.is_unspecified or address.is_link_local
            or address.is_reserved and not address.is_loopback
            or not address.is_global and host not in allowed_hosts):
            raise AIServiceError('destination_blocked')
        addresses.add(str(address))
    if not addresses:
        raise AIServiceError('destination_blocked')
    return min(addresses)


def _exchange(host, port, path, payload, headers, timeout, allowed_hosts, evidence):
    address = _resolve(host, port, allowed_hosts)
    pool = urllib3.HTTPSConnectionPool(address, port=port, server_hostname=host,
        assert_hostname=host, cert_reqs='CERT_REQUIRED', ca_certs=requests.certs.where(), maxsize=1)
    response = None
    try:
        evidence['transmission_state'] = 'Attempted'
        response = pool.urlopen('POST', path, body=payload, headers=headers,
            timeout=urllib3.Timeout(connect=min(5, timeout), read=timeout, total=timeout),
            redirect=False, retries=False, preload_content=False)
        evidence.update(transmission_state='Responded', http_status=response.status)
        if response.status != 200:
            raise AIServiceError({401: 'authentication_error', 403: 'authentication_error', 429: 'rate_limited'}
                .get(response.status, 'unavailable' if response.status >= 500 else 'contract_invalid'))
        if (response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'
            or response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
            raise AIServiceError('contract_invalid')
        body = bytearray()
        while chunk := response.read1(8192, decode_content=False):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise AIServiceError('contract_invalid')
        return body
    finally:
        if response is not None:
            response.close()
        pool.close()


def bounded_exchange(target, payload, headers, timeout, allowed_hosts):
    """Reap the one-shot transport even if DNS or a socket operation stalls."""
    host, port, path = target
    transport = dict(host=host, port=port, path=path, payload=payload.decode('utf-8'),
        headers=headers, timeout=timeout, allowed_hosts=allowed_hosts)
    # Send all connection details through a private pipe, including Authorization.
    # A subprocess deadline also works in Desk threads and bounds libc DNS.
    try:
        completed = subprocess.run([sys.executable, '-m', __name__],
            input=json.dumps(transport).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=True)
    except subprocess.TimeoutExpired:
        raise AIServiceError('timeout') from None
    except (OSError, subprocess.CalledProcessError):
        raise AIServiceError('unavailable') from None
    head, _, body = completed.stdout.partition(b'\n')
    try:
        return json.loads(head), body
    except ValueError:
        raise AIServiceError('unavailable') from None


def _worker():
    request = json.load(sys.stdin.buffer)
    request['payload'] = request['payload'].encode('utf-8')
    evidence = {'transmission_state': 'Prepared'}
    body = b''
    try:
        body = _exchange(**request, evidence=evidence)
    except AIServiceError as exc:
        evidence['error'] = exc.code
    except (TimeoutError, urllib3.exceptions.TimeoutError):
        evidence['error'] = 'timeout'
    except (OSError, urllib3.exceptions.HTTPError):
        evidence['error'] = 'unavailable'
    sys.stdout.buffer.write(json.dumps(evidence).encode() + b'\n' + body)


if __name__ == '__main__':
    _worker()
