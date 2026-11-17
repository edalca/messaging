"""MariaDB connection locking shared by reception, Human and notification handoff."""
from contextlib import contextmanager
import frappe
from frappe import _


@contextmanager
def contact_lock(key, *, timeout=10):
	"""A connection lock survives the durable execution checkpoints and is released on disconnect."""
	name = "messaging:" + key[:54]
	if frappe.db.sql("SELECT GET_LOCK(%s, %s)", (name, timeout))[0][0] != 1:
		if timeout == 0:
			yield False
			return
		frappe.throw(_("This contact is currently being processed. Please retry later."))
	try:
		# Entry points own the transaction. Drop any read snapshot opened during permission checks.
		frappe.db.rollback()
		yield True
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (name,))


def shared_document(doctype, name):
	"""Protect operational configuration from edits while allowing concurrent readers.

	Unlike a conversation's exclusive connection lock, this transactional row lock
	protects a shared source definition. The caller retains authorization checks.
	"""
	table = frappe.qb.DocType(doctype)
	query, values = frappe.qb.from_(table).select(table.name).where(table.name == name).walk()
	frappe.db.sql(query + ' LOCK IN SHARE MODE', values)
	return frappe.get_doc(doctype, name)



def lock_document_types(*doctypes):
    """Serialize creation of absent documents using stable native metadata rows.

    Callers choose one consistent lock order and retain their transaction.
    """
    for doctype in doctypes:
        frappe.db.get_value('DocType', doctype, 'name', for_update=True)


@contextmanager
def document_savepoint():
    """Rollback one document change and its dependencies, propagating every error.

    Frappe's savepoint context suppresses caught errors; administrative owners
    need the error back to build their own diagnostics. Never commit here.
    """
    name = 'messaging_' + frappe.generate_hash(length=12)
    frappe.db.savepoint(name)
    try:
        yield
    except Exception:
        frappe.db.rollback(save_point=name)
        raise
    finally:
        frappe.db.release_savepoint(name)
