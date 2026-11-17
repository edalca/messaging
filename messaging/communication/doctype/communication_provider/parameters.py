"""Communication provider parameter document schema, reused by declarations."""
import re
import frappe
from frappe import _
from messaging.common.configuration import normalize

TYPES = frozenset(('Data', 'Small Text', 'Password', 'Check', 'Int', 'Float', 'Select', 'Link'))
PARAMETER_FIELDS = ('parameter_key', 'label', 'field_type', 'scope', 'required', 'options', 'default_value', 'sort_order', 'description')


def validate_parameter_schema(parameters):
	if not isinstance(parameters, (list, tuple)):
		frappe.throw(_("Invalid provider parameter definition."))
	seen = set()
	result = []
	for raw in parameters:
		if not isinstance(raw, dict) or set(raw) - set(PARAMETER_FIELDS):
			frappe.throw(_("Invalid provider parameter definition."))
		p = {k: raw.get(k) for k in PARAMETER_FIELDS}
		key = p['parameter_key']
		if (not isinstance(key, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) or key in seen
			or not isinstance(p['field_type'], str) or p['field_type'] not in TYPES or p['scope'] not in ('Channel', 'Account', 'Template', 'Notification Rule')
			or not isinstance(p['label'], str) or not p['label'].strip() or len(p['label']) > 140
			or type(p['required']) not in (type(None), bool, int) or p['required'] not in (None, 0, 1)):
			frappe.throw(_("Invalid provider parameter definition."))
		seen.add(key)
		p['required'] = int(bool(p['required']))
		p['sort_order'] = raw.get('sort_order', 0)
		if type(p['sort_order']) is not int or p['sort_order'] < 0:
			frappe.throw(_("Invalid provider parameter definition."))
		for field in ('options', 'default_value', 'description'):
			p[field] = '' if p[field] is None else p[field]
			if not isinstance(p[field], str) or len(p[field]) > 4000:
				frappe.throw(_("Invalid provider parameter definition."))
		if p['field_type'] == 'Select':
			items = p['options'].split('\n')
			if not items or any(not item.strip() or item != item.strip() or len(item)>140 for item in items) or len(set(items)) != len(items):
				frappe.throw(_("Invalid provider parameter options."))
		elif p['field_type'] == 'Link':
			if not p['options'].strip():
				frappe.throw(_("Invalid provider parameter options."))
		elif p['options']:
			frappe.throw(_("Invalid provider parameter options."))
		if p['field_type'] == 'Password' and p['default_value']:
			frappe.throw(_("Password parameters cannot have defaults."))
		if p['default_value'] and p['field_type'] != 'Link':
			normalize(p, p['default_value'])
		result.append(p)
	return result
