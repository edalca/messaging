"""Bounded Meta Graph API transport.

Adapted from frappe/whatsapp. Copyright (c) 2026, Frappe Technologies Pvt. Ltd.
and contributors. See LICENSE.
"""
import json
import re
from time import monotonic

import requests

# These versions share the request contract used by this adapter, newest first.
SUPPORTED_VERSIONS = ('v27.0', 'v26.0', 'v25.0')
DEFAULT_VERSION = SUPPORTED_VERSIONS[0]
MAX_RESPONSE = 2 * 1024 * 1024
PHONE_FIELDS = 'id,display_phone_number'
PAGE_SIZE = 100


def numeric_id(value):
	return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,29}', value) is not None


def version_valid(value):
	return isinstance(value, str) and value in SUPPORTED_VERSIONS


class GraphError(Exception):
	def __init__(self, *, rejected=False, transport=False, code=None, subcode=None, identity_mismatch=False):
		super().__init__('Meta Graph API request failed')
		self.rejected = rejected
		self.transport = transport
		self.code = code
		self.subcode = subcode
		self.identity_mismatch = identity_mismatch


def _error_number(value):
	return value if type(value) is int and 0 < value <= 2_147_483_647 else None


class Graph:
	def __init__(self, phone, business, token, version):
		if not numeric_id(phone) or not numeric_id(business) or not version_valid(version) or not token:
			raise GraphError(rejected=True)
		self.phone = phone
		self.business = business
		self._token = token
		self.version = version
		self.http_status = None
		self.trace_id = None

	def endpoint(self, node, edge=''):
		return f'https://graph.facebook.com/{self.version}/{node}' + (f'/{edge}' if edge else '')

	def is_credential(self, value):
		return value == self._token

	def _request(self, method, node, edge='', *, params=None, payload=None):
		self.http_status = self.trace_id = None
		deadline = monotonic() + 15
		try:
			with requests.request(method, self.endpoint(node, edge), headers={'Authorization': f'Bearer {self._token}'},
				params=params, json=payload, timeout=(3.05, 10), verify=True,
				allow_redirects=False, stream=True) as response:
				self.http_status = response.status_code
				self._capture_trace(response.headers.get('x-fb-trace-id'))
				raw = bytearray()
				for chunk in response.iter_content(16384):
					raw.extend(chunk)
					if len(raw) > MAX_RESPONSE or monotonic() > deadline:
						raise GraphError()
				data = json.loads(raw)
				if not isinstance(data, dict):
					raise GraphError()
				error = data.get('error')
				if not 200 <= response.status_code < 300 or 'error' in data:
					error = error if isinstance(error, dict) else {}
					self._capture_trace(error.get('fbtrace_id'))
					raise GraphError(rejected=response.status_code in (400, 401, 403, 404)
						and type(error.get('code')) is int and not error.get('is_transient'),
						code=_error_number(error.get('code')), subcode=_error_number(error.get('error_subcode')))
				return data
		except requests.RequestException:
			raise GraphError(transport=True) from None
		except (ValueError, RecursionError):
			raise GraphError() from None

	def send(self, payload):
		return self._request('POST', self.phone, 'messages', payload={'messaging_product': 'whatsapp', **payload})

	def _capture_trace(self, trace):
		if isinstance(trace, str) and not self.is_credential(trace) and re.fullmatch(r'A[A-Za-z0-9_-]{17,31}', trace):
			self.trace_id = trace

	def phone_info(self):
		data = self._request('GET', self.phone, params={'fields': PHONE_FIELDS})
		number = data.get('display_phone_number')
		if data.get('id') != self.phone:
			raise GraphError(identity_mismatch=numeric_id(data.get('id')))
		if (not isinstance(number, str) or self.is_credential(number)
			or not re.fullmatch(r'\+?[0-9][0-9 ().-]{5,31}', number)
			or not 7 <= sum(char.isdigit() for char in number) <= 15):
			raise GraphError()
		return dict(id=self.phone, display_phone_number=number)

	def templates(self, name=None):
		filters = {'name': name} if name else {}
		return self._list('message_templates', fields='id,name,language,status,components,category,parameter_format', **filters)

	def subscription_app_ids(self):
		"""Read every page before using absence as evidence for a configured app."""
		app_ids = {}
		for row in self._list('subscribed_apps'):
			app = row.get('whatsapp_business_api_data')
			app_id = app.get('id') if isinstance(app, dict) else None
			if not numeric_id(app_id) or self.is_credential(app_id):
				raise GraphError()
			app_ids[app_id] = None
		return list(app_ids)

	def _list(self, edge, **parameters):
		seen = set()
		rows = []
		params = dict(limit=PAGE_SIZE, **parameters)
		deadline = monotonic() + 90
		while monotonic() < deadline:
			page = self._request('GET', self.business, edge, params=params)
			data = page.get('data')
			if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
				raise GraphError()
			rows.extend(data)
			paging = page.get('paging')
			if paging is not None and not isinstance(paging, dict):
				raise GraphError()
			if not paging or not paging.get('next'):
				return rows
			cursors = paging.get('cursors')
			cursor = cursors.get('after') if isinstance(cursors, dict) else None
			if not isinstance(cursor, str) or not re.fullmatch(r'[a-zA-Z0-9_+/=-]{1,2048}', cursor) or cursor in seen:
				raise GraphError()
			seen.add(cursor)
			params['after'] = cursor
		raise GraphError()

	def save_template(self, payload, template_id=None):
		if template_id is None:
			return self._request('POST', self.business, 'message_templates', payload=payload)
		if not numeric_id(template_id):
			raise GraphError(rejected=True)
		return self._request('POST', template_id, payload=payload)
