"""The native type registry is the sole source of type identities and dispatch."""

from importlib import import_module
from functools import lru_cache

_HANDLERS = {
	"String": ("string", "StringType"),
	"Int": ("integer", "IntegerType"),
	"Date": ("date", "DateType"),
	"Name": ("name", "NameType"),
	"Expression": ("expression", "ExpressionType"),
	"Decimal": ("decimal", "DecimalType"),
	"Boolean": ("boolean", "BooleanType"),
}


def get_supported_types():
	return tuple(_HANDLERS)


@lru_cache(maxsize=None)
def get_handler(parameter_type):
	try:
		module, implementation = _HANDLERS[parameter_type]
	except (KeyError, TypeError):
		raise ValueError("Unregistered native parameter type") from None
	return getattr(import_module(f"{__name__}.{module}"), implementation)()
