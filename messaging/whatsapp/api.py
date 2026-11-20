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


def numeric_id(value):
	return isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,29}', value) is not None


def version_valid(value):
	return isinstance(value, str) and value in SUPPORTED_VERSIONS


class GraphError(Exception):
	def __init__(self, *, rejected=False):
		super().__init__('Meta Graph API request failed')
		self.rejected = rejected


class Graph:
	def __init__(self, phone, business, token, version):
		if not numeric_id(phone) or not numeric_id(business) or not version_valid(version) or not token:
			raise GraphError(rejected=True)
		self.phone = phone
		self.business = business
		self._token = token
		self.version = version

	def _request(self, method, node, edge='', *, params=None, payload=None):
		url = f'https://graph.facebook.com/{self.version}/{node}'
		if edge:
			url += f'/{edge}'
		deadline = monotonic() + 15
		try:
			with requests.request(method, url, headers={'Authorization': f'Bearer {self._token}'},
				params=params, json=payload, timeout=(3.05, 10), verify=True,
				allow_redirects=False, stream=True) as response:
				raw = bytearray()
				for chunk in response.iter_content(16384):
					raw.extend(chunk)
					if len(raw) > MAX_RESPONSE or monotonic() > deadline:
						raise GraphError()
				data = json.loads(raw)
				if not isinstance(data, dict):
					raise GraphError()
				error = data.get('error')
				if not 200 <= response.status_code < 300 or error:
					raise GraphError(rejected=response.status_code in (400, 401, 403, 404)
						and isinstance(error, dict) and type(error.get('code')) is int
						and not error.get('is_transient'))
				return data
		except (requests.RequestException, ValueError):
			raise GraphError() from None

	def send(self, payload):
		return self._request('POST', self.phone, 'messages', payload={'messaging_product': 'whatsapp', **payload})

	def phone_info(self):
		return self._list('phone_numbers', fields='id,display_phone_number')

	def subscription(self, method):
		if method not in ('GET', 'POST', 'DELETE'):
			raise ValueError
		return self._request(method, self.business, 'subscribed_apps')

	def templates(self, name=None):
		filters = {'name': name} if name else {}
		return self._list('message_templates', fields='id,name,language,status,components,category,parameter_format', **filters)

	def subscriptions(self):
		return self._list('subscribed_apps')

	def _list(self, edge, **parameters):
		seen = set()
		rows = []
		params = dict(limit=100, **parameters)
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
