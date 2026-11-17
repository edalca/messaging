"""Stable UTC clock and identity digest; formats are part of the storage contract."""
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from types import MappingProxyType

import frappe
from frappe import _
from frappe.model.docstatus import DocStatus


def utcnow():
	"""Quota timestamps are naive UTC in MariaDB, independent of the site's display timezone."""
	return datetime.now(timezone.utc).replace(tzinfo=None)


def identity_key(*parts):
	return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def decimal_json(value):
	"""Encode finite Decimal values as exact text; consumers restore by input type."""
	if type(value) is Decimal and value.is_finite():
		return format(value, "f")
	raise TypeError('Unsupported JSON value')


def _freeze(value, *, preserve_decimal=False):
	if isinstance(value, DocStatus):
		return int(value)
	if isinstance(value, Mapping):
		return MappingProxyType({key: _freeze(item, preserve_decimal=preserve_decimal) for key, item in value.items()})
	if isinstance(value, (list, tuple)):
		return tuple(_freeze(item, preserve_decimal=preserve_decimal) for item in value)
	if isinstance(value, (datetime, date, time)):
		return value.isoformat()
	if isinstance(value, Decimal):
		return value if preserve_decimal else format(value, "f")
	if type(value) not in (str, int, float, bool, type(None)):
		raise frappe.ValidationError(_('Unsupported Process context value.'))
	return value
