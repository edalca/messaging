"""Functional capabilities declared by the installed owner."""
from messaging.registry import register_registry


def register():
    return register_registry(app='messaging', discover=True)
