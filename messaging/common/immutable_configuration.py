"""Immutable typed document configuration values and their structural validation."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from functools import lru_cache
from types import MappingProxyType, UnionType
from typing import Literal, Union, get_args, get_origin, get_type_hints
import re
import frappe

@lru_cache(maxsize=None)
def _hints(cls):
    return get_type_hints(cls)


def _matches(value, annotation):
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(_matches(value, option) for option in arguments)
    if origin is Literal:
        return any(type(value) is type(option) and value == option for option in arguments)
    if origin is Mapping:
        return isinstance(value, Mapping) and all(_matches(k, arguments[0]) and _matches(v, arguments[1]) for k, v in value.items())
    if origin is tuple:
        return type(value) is tuple and all(_matches(v, arguments[0]) for v in value)
    return type(value) is annotation


def _invalid(detail):
    # Developer contract errors are not user interface messages.
    raise frappe.ValidationError('Invalid preset declaration: ' + detail)


def _text(value, *, required=False):
    if '\x00' in value or (required and not value.strip()):
        _invalid('A non-empty text value without null characters is required.')


def _identifier(value, *, dotted=False):
    pattern = r'[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*' if dotted else r'[a-z][a-z0-9_]*'
    if not re.fullmatch(pattern, value):
        _invalid('Invalid technical identifier: ' + value)


def _languages(values):
    if not values:
        _invalid('Localized content is required.')
    for code in values:
        if not re.fullmatch(r'[a-zA-Z0-9]+(?:[-_][a-zA-Z0-9]+)*', code):
            _invalid('Invalid language code: ' + code)


def _plain(value):
    """Project value objects into ordinary Frappe initial field values, without serialization."""
    if is_dataclass(value):
        return {f.name: _plain(item) for f in fields(value) if (item := getattr(value, f.name)) is not None}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


class _Value:
    __slots__ = ()

    def __post_init__(self):
        for name, annotation in _hints(type(self)).items():
            value = getattr(self, name)
            if not _matches(value, annotation):
                _invalid(type(self).__name__ + '.' + name + ' has an invalid type or value.')
            if isinstance(value, str):
                _text(value)
            nested = value.values() if isinstance(value, Mapping) else value if isinstance(value, tuple) else (value,)
            for item in nested:
                if isinstance(item, _Value):
                    item.__post_init__()
            if isinstance(value, Mapping):
                object.__setattr__(self, name, MappingProxyType(dict(value)))
        self.validate()

    def validate(self):
        pass

    def __deepcopy__(self, memo):
        return self  # All nested declarations and collections are immutable.
