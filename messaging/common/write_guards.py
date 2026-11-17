"""Code-owned write scopes. REST fields and document flags never grant these capabilities."""
from contextlib import contextmanager
from contextvars import ContextVar


class InternalWriteGuard:
	def __init__(self, name):
		self._scope = ContextVar(name, default=False)

	def active(self):
		return self._scope.get()

	@contextmanager
	def __call__(self):
		token = self._scope.set(True)
		try:
			yield
		finally:
			self._scope.reset(token)
