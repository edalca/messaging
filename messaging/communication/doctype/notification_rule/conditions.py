"""Canonical scalar equality conditions owned by Notification Rule."""
import json
from datetime import time as native_time
import re
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _
from frappe.utils import getdate, get_datetime, get_time

from messaging.common.configuration import normalize
from messaging.common.primitives import identity_key

EVENTS = ('after_insert', 'on_update', 'on_submit', 'on_cancel', 'on_update_after_submit')
FIELD_TYPES = frozenset(('Data', 'Small Text', 'Select', 'Link', 'Int', 'Float', 'Currency',
    'Percent', 'Check', 'Date', 'Datetime', 'Time', 'Email', 'Phone', 'URL'))
NUMERIC_TYPES = frozenset(('Float', 'Currency', 'Percent'))


def condition_fields(meta, *, field_types=FIELD_TYPES):
    """Only public persisted scalar DocFields; metadata remains the authority."""
    columns = set(meta.get_valid_columns())
    return [f for f in meta.fields if f.fieldtype in field_types and f.fieldname in columns
            and not f.is_virtual and not f.hidden and not f.permlevel
            and not (f.fieldtype == 'Data' and f.options == 'Password')]


def field_definition(meta, fieldname):
    field = next((f for f in condition_fields(meta) if f.fieldname == fieldname), None)
    if not field:
        frappe.throw(_('Select a public stored scalar field for the notification condition.'))
    return field


def normalize_value(field, raw, *, check_reference=False):
    """Return canonical JSON, preserving explicit false and exact decimal equality."""
    kind = field.fieldtype
    try:
        if raw is None or raw == '' or isinstance(raw, (dict, list, tuple)):
            raise ValueError
        if kind in NUMERIC_TYPES:
            if isinstance(raw, bool) or not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', str(raw)):
                raise ValueError
            value = Decimal(str(raw))
            if not value.is_finite() or abs(value) > Decimal('1e100') or value.as_tuple().exponent < -100:
                raise ValueError
            return format(value.normalize(), 'f') if value else '0'
        if kind in ('Date', 'Datetime', 'Time'):
            # Native conversions use site-local stored values; no browser date parsing.
            text = str(raw)
            if kind == 'Date':
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', text): raise ValueError
                value = getdate(text).isoformat()
            elif kind == 'Datetime':
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?', text): raise ValueError
                value = get_datetime(text).isoformat(sep=' ', timespec='microseconds')
            else:
                if not re.fullmatch(r'\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?', text): raise ValueError
                native_time.fromisoformat(text)
                value = get_time(text).isoformat(timespec='microseconds')
        elif kind in ('Int', 'Check'):
            value = normalize(dict(field_type=kind), raw)
        else:
            if not isinstance(raw, str) or '\x00' in raw or len(raw) > (field.length or (65535 if kind == 'Small Text' else 140)):
                raise ValueError
            value = raw
            if kind == 'Select' and value not in (field.options or '').split('\n'):
                raise ValueError
            if kind == 'Link' and check_reference:
                linked = frappe.get_doc(field.options, value)
                linked.check_permission('read')
            if kind == 'Email' or (kind == 'Data' and field.options == 'Email'):
                from frappe.utils import validate_email_address
                if validate_email_address(value, throw=False) != value: raise ValueError
            if kind == 'URL' or (kind == 'Data' and field.options == 'URL'):
                from frappe.utils import validate_url
                if not validate_url(value, throw=False): raise ValueError
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        frappe.throw(_('Invalid notification condition value.'))


def canonical_condition(row, meta, *, check_reference=False):
    event, fieldname = row.get('document_event'), row.get('field_name') or ''
    if event not in EVENTS or (event in ('on_submit', 'on_cancel', 'on_update_after_submit') and not meta.is_submittable):
        frappe.throw(_('The selected event is not supported by this DocType.'))
    stored = row.get('value')
    if not fieldname:
        if stored not in (None, ''):
            frappe.throw(_('A notification condition without Field must have an empty Value.'))
        return dict(document_event=event, field_name='', value='')
    field = field_definition(meta, fieldname)
    try:
        raw = json.loads(stored, parse_float=Decimal) if isinstance(stored, str) else None
    except (ValueError, TypeError):
        frappe.throw(_('Invalid notification condition value.'))
    value = normalize_value(field, raw, check_reference=check_reference)
    return dict(document_event=event, field_name=fieldname, value=value)


def canonical_conditions(rule, *, check_reference=False):
    meta = frappe.get_meta(rule.source_doctype)
    result, seen = [], set()
    if not rule.get('events'):
        frappe.throw(_('Configure at least one notification event.'))
    for row in rule.events:
        condition = canonical_condition(row, meta, check_reference=check_reference)
        key = tuple(condition.values())
        if key in seen:
            frappe.throw(_('Duplicate notification event condition.'))
        seen.add(key)
        result.append(condition)
    return sorted(result, key=lambda c: tuple(c.values()))


def validate_conditions(rule):
    canonical_conditions(rule, check_reference=True)
    meta = frappe.get_meta(rule.source_doctype)
    for row in rule.events:
        if row.name:
            stored = frappe.db.get_value('Notification Rule Event', row.name, ['parent', 'parenttype', 'parentfield'], as_dict=True)
            if stored and (stored.parent, stored.parenttype, stored.parentfield) != (rule.name, rule.doctype, 'events'):
                frappe.throw(_('Event condition ownership is immutable.'), frappe.PermissionError)
        condition = canonical_condition(row, meta)
        row.field_name, row.value = condition['field_name'], condition['value']


def matches(condition, source):
    if not condition['field_name']:
        return True
    field = field_definition(frappe.get_meta(source.doctype), condition['field_name'])
    # Observe the actual value written by Frappe, including its native conversions.
    raw = frappe.db.get_value(source.doctype, source.name, field.fieldname)
    if raw is None or raw == '':
        return False
    return normalize_value(field, raw) == condition['value']


def condition_token(condition):
    """Short opaque token for filtered Process arguments; bare events retain their contract."""
    return (identity_key(condition['document_event'], condition['field_name'], condition['value'])
            if condition['field_name'] else condition['document_event'])


def occurrence_key(rule, source, condition):
    return identity_key(rule.name, rule.source_application, source.doctype, source.name,
                        condition['document_event'], condition['field_name'], condition['value'])
