"""Trusted public HTTPS origins for external integrations."""
import re
from urllib.parse import urlsplit

import frappe

DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?')
LOCAL_SUFFIXES = ('.localhost', '.local', '.internal', '.lan', '.home.arpa', '.test', '.invalid', '.example')


def configured_public_origin():
	"""Read the canonical origin from site configuration."""
	host_name = frappe.conf.host_name or frappe.conf.hostname or ''
	if not isinstance(host_name, str) or any(ord(char) < 32 for char in host_name):
		return None
	try:
		origin = urlsplit(host_name.strip())
		domain = (origin.hostname or '').rstrip('.').encode('idna').decode('ascii')
		port = origin.port
	except (ValueError, UnicodeError):
		return None
	if (origin.scheme != 'https' or not DOMAIN.fullmatch(domain) or domain.endswith(LOCAL_SUFFIXES)
		or origin.username is not None or origin.password is not None or origin.path not in ('', '/') or origin.query or origin.fragment):
		return None
	authority = f'{domain}:{port}' if port is not None else domain
	return f'https://{authority}'
