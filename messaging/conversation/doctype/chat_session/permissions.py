"""Shared Desk, document and inbox authorization; grants never imply System Manager."""
import frappe
from frappe import _
from messaging.security.permissions import is_system_manager as is_manager




def require_agent():
	if not is_manager() and (frappe.session.user == 'Guest' or 'Messaging Agent' not in frappe.get_roles()):
		frappe.throw(_("Messaging agent permission is required."), frappe.PermissionError)


def scope_sql(alias, user):
	return (f"EXISTS (SELECT 1 FROM `tabChat Agent` g WHERE g.user={frappe.db.escape(user)} "
		f"AND g.channel={alias}.channel AND g.account={alias}.account AND g.enabled=1)")


def session_sql(alias, user):
	return (
		f"({alias}.human_requested_at IS NOT NULL AND {scope_sql(alias, user)} AND "
		f"({alias}.assigned_agent={frappe.db.escape(user)} OR "
		f"(IFNULL({alias}.assigned_agent,'')='' AND {alias}.status NOT IN ('Closed','Expired'))))"
	)


def session_query(user=None):
	user = user or frappe.session.user
	return '' if is_manager(user) else session_sql('`tabChat Session`', user)


def contact_query(user=None):
	user = user or frappe.session.user
	return '' if is_manager(user) else scope_sql('`tabChat Contact`', user)


def message_query(user=None):
	user = user or frappe.session.user
	return '' if is_manager(user) else ('EXISTS (SELECT 1 FROM `tabChat Session` s WHERE s.name=`tabChat Message`.session AND ' + session_sql('s', user) + ')')


def has_scope(channel, account, user=None):
	user = user or frappe.session.user
	return is_manager(user) or bool(frappe.db.exists('Chat Agent', dict(user=user, channel=channel, account=account, enabled=1)))


def session_permission(doc, user=None, permission_type=None):
	user = user or frappe.session.user
	if permission_type not in (None, 'read', 'select'):
		return False
	if is_manager(user):
		return True
	if not bool(doc.human_requested_at) or not has_scope(doc.channel, doc.account, user):
		return False
	if doc.status in ('Closed', 'Expired'):
		return bool(doc.assigned_agent and doc.assigned_agent == user)
	return not doc.assigned_agent or doc.assigned_agent == user


def contact_permission(doc, user=None, permission_type=None):
	return permission_type in (None, 'read', 'select') and has_scope(doc.channel, doc.account, user)


def message_permission(doc, user=None, permission_type=None):
	if permission_type not in (None, 'read', 'select'):
		return False
	if is_manager(user):
		return True
	return bool(doc.session) and session_permission(frappe.get_doc('Chat Session', doc.session), user, permission_type)


def check_session(session, *, responding=False):
	require_agent()
	if not session_permission(session):
		frappe.throw(_("This conversation is outside your assignment or account scope."), frappe.PermissionError)
	if responding and (session.status != 'Human Active' or session.assigned_agent != frappe.session.user):
		frappe.throw(_("Only the assigned agent can reply to an active human conversation."), frappe.PermissionError)
