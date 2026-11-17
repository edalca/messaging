"""Pure AI provider parameter schema and scalar rules shared with registrations."""
import math
import re
import frappe
from frappe import _
from messaging.common.configuration import normalize

TYPES = ('Data', 'Password', 'Check', 'Int', 'Float', 'Select')
PROVIDER_ID = re.compile(r'[a-z][a-z0-9_-]{1,63}')

def validate_scalar(definition, value):
    value = normalize(definition, value)
    if value is None:
        return value
    if isinstance(value, str) and len(value) > int(definition.get('max_length') or 4000):
        frappe.throw(_('Invalid provider parameter value.'))
    if type(value) in (int, float):
        if ((definition.get('minimum') is not None and value < definition['minimum'])
            or (definition.get('maximum') is not None and value > definition['maximum'])):
            frappe.throw(_('Invalid provider parameter value.'))
    return value


def validate_parameter_schema(rows):
    seen = set()
    for row in rows:
        if (not all(isinstance(getattr(row, key), str) for key in ('parameter_key', 'label', 'field_type', 'options', 'description'))
            or type(row.required) not in (bool, int) or row.required not in (0, 1)
            or type(row.sort_order) is not int or row.sort_order < 0
            or type(row.max_length) is not int or not 1 <= row.max_length <= 4000
            or row.default_value is not None and not isinstance(row.default_value, str)
            or any(bound is not None and (type(bound) not in (int, float) or not math.isfinite(bound))
                for bound in (row.minimum, row.maximum))):
            frappe.throw(_('Invalid provider parameter definition.'))
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', row.parameter_key or '')
            or row.parameter_key in seen or row.field_type not in TYPES or not (row.label or '').strip()):
            frappe.throw(_('Invalid provider parameter definition.'))
        seen.add(row.parameter_key)
        if row.minimum is not None and row.maximum is not None and row.minimum > row.maximum:
            frappe.throw(_('Invalid provider parameter definition.'))
        if row.field_type == 'Select':
            options = (row.options or '').splitlines()
            if not options or len(options) != len(set(options)) or any(not x.strip() or x.strip() != x for x in options):
                frappe.throw(_('Invalid provider parameter options.'))
        elif row.options:
            frappe.throw(_('Invalid provider parameter options.'))
        if row.field_type == 'Password' and row.default_value:
            frappe.throw(_('Password parameters cannot have defaults.'))
        if row.default_value not in (None, ''):
            validate_scalar(dict(row), row.default_value)
