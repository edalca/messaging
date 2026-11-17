"""Shared scalar validation and encrypted child storage for configuration owners."""
import math
import re

import frappe
from frappe import _
from frappe.utils.password import set_encrypted_password

MASK = '********'


class EncryptedConfigurationValue:
    def _save_passwords(self):
        value = self.secret_value
        if value and not self.is_dummy_password(value):
            set_encrypted_password(self.doctype, self.name, value, 'secret_value')
        self.secret_value = MASK if value else ''


def normalize(parameter, raw):
	"""Return one typed scalar; None means empty, distinct from False, zero and text."""
	if raw is None or raw == '':
		return None
	kind = parameter['field_type']
	try:
		if kind == 'Check':
			if type(raw) not in (bool, int, str) or raw not in (True, False, 0, 1, '0', '1'):
				raise ValueError
			return raw in (True, 1, '1')
		if kind == 'Int':
			if isinstance(raw, bool) or not re.fullmatch(r'[+-]?\d+', str(raw)):
				raise ValueError
			value = int(raw)
			if not -(2**53 - 1) <= value <= 2**53 - 1: raise ValueError
			return value
		if kind == 'Float':
			if isinstance(raw, bool): raise ValueError
			value = float(raw)
			if not math.isfinite(value): raise ValueError
			return value
		if not isinstance(raw, str) or len(raw) > 4000 or '\x00' in raw:
			raise ValueError
		if kind == 'Link' and not frappe.db.exists(parameter['options'], raw):
			raise ValueError
		if kind == 'Select' and raw not in parameter['options'].split('\n'):
			raise ValueError
		return raw
	except (ValueError, TypeError, OverflowError):
		# Never interpolate input values, especially credentials, into errors.
		frappe.throw(_("Invalid provider parameter value."))


def validate_value_owner(row, doc):
    stored = frappe.db.get_value(row.doctype, row.name,
        ['parent', 'parenttype', 'parentfield', 'parameter_key'], as_dict=True) if row.name else None
    expected = (doc.name, doc.doctype, 'parameters', row.parameter_key)
    if stored and (stored.parent, stored.parenttype, stored.parentfield, stored.parameter_key) != expected:
        frappe.throw(_('Provider value ownership is immutable.'), frappe.PermissionError)
    if (row.parent, row.parenttype, row.parentfield) != expected[:3]:
        frappe.throw(_('Provider value ownership is immutable.'), frappe.PermissionError)
    return stored


def prepare_secret(row, old_rows, stored):
    if row.value not in (None, '', 'null'):
        frappe.throw(_('Secrets must use encrypted Password storage.'))
    row.value = None
    if not row.secret_value and row.name in old_rows:
        row.secret_value = old_rows[row.name].secret_value or ''
    if row.secret_value and row.is_dummy_password(row.secret_value):
        if not stored or not frappe.db.get_value('__Auth',
            {'doctype':row.doctype,'name':row.name,'fieldname':'secret_value','encrypted':1},'name',order_by='name'):
            frappe.throw(_('Invalid saved secret reference.'))
