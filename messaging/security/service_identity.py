"""Administrative provisioning and read-only verification of the canonical service identity."""
import frappe
from frappe import _
from messaging.security.permissions import require_system_manager

SERVICE_USER = "messaging-automation@messaging.invalid"
AUTOMATION_ROLE = "Messaging Automation"
READ_DOCTYPES = ("Chat Turn", "Bot Flow", "Bot Parameter")


def require_setup_manager():
	require_system_manager(_("Messaging automation setup requires System Manager permission."))
	frappe.has_permission("User", "create", throw=True)
	frappe.has_permission("Role", "write", throw=True)


def ensure_messaging_service_user():
	"""Administrative lifecycle/repair API; caller owns the transaction."""
	require_setup_manager()
	# Serialize concurrent setup without committing the caller's transaction.
	frappe.db.get_value("DocType", "User", "name", for_update=True)
	if not frappe.db.exists("Role", AUTOMATION_ROLE):
		frappe.get_doc(dict(doctype="Role", role_name=AUTOMATION_ROLE, desk_access=0)).insert()
	role = frappe.get_doc("Role", AUTOMATION_ROLE)
	# DocType synchronization may create a new permission role with Desk access.
	# Configure that unassigned role before creating the service, never repurpose
	# an already assigned human role silently.
	if role.desk_access and not frappe.db.exists("Has Role", {"role": AUTOMATION_ROLE}):
		role.desk_access = 0
		role.save()
	if role.disabled or role.desk_access:
		_unavailable()
	if not frappe.db.exists("User", SERVICE_USER):
		user = frappe.get_doc(dict(doctype="User", email=SERVICE_USER, first_name="Messaging Automation",
			enabled=1, send_welcome_email=0, roles=[dict(role=AUTOMATION_ROLE)],
			user_type="Website User", time_zone="America/Tegucigalpa"))
		user.flags.no_welcome_mail = True
		user.insert()
	# Installed consumers own their narrowly scoped business permissions.
	for app in frappe.get_installed_apps():
		for path in frappe.get_hooks("messaging_automation_setup", app_name=app) or []:
			if not path.startswith(app + "."):
				raise frappe.PermissionError(_("Invalid automation setup owner"))
			frappe.get_attr(path)()
	identity = require_messaging_service_user()
	frappe.logger("messaging").info("Messaging automation identity verified by administrative setup.")
	return identity


def _unavailable():
	frappe.logger("messaging").error("Messaging automation unavailable; authorized setup or repair is required.")
	frappe.throw(_("Messaging automation is unavailable. Ask a System Manager to run the authorized setup or repair procedure."), frappe.PermissionError)


def require_messaging_service_user():
	"""Fail closed without provisioning, credentials, fallback users or mutations."""
	user = frappe.db.get_value("User", SERVICE_USER,
		["enabled", "user_type", "send_welcome_email", "api_key", "api_secret", "role_profile_name"], as_dict=True)
	roles = set(frappe.get_all("Has Role", filters={"parent": SERVICE_USER, "parenttype": "User"}, pluck="role"))
	# Native business roles are administered by their owners, independently of Messaging.
	if (not user or not user.enabled or user.user_type != "Website User" or user.send_welcome_email
		or user.api_key or user.api_secret or user.role_profile_name or AUTOMATION_ROLE not in roles
		or frappe.db.exists("User Role Profile", {"parent": SERVICE_USER})
		or frappe.db.exists("__Auth", {"doctype": "User", "name": SERVICE_USER, "encrypted": 0})):
		_unavailable()
	if any(not frappe.db.get_value("Role", role, "name") or frappe.db.get_value("Role", role, "disabled")
		or frappe.db.get_value("Role", role, "desk_access") for role in roles):
		_unavailable()
	for doctype in READ_DOCTYPES:
		if not frappe.has_permission(doctype, "read", user=SERVICE_USER):
			_unavailable()
	# Frappe automatically shares a User's own profile with that User. That
	# native self-profile permission is not user administration.
	if any(frappe.has_permission("User", ptype, user=SERVICE_USER) for ptype in ("create", "delete")):
		_unavailable()
	for doctype in ("Role", "Bot Settings", "Bot Flow", "Communication Channel Account"):
		if any(frappe.has_permission(doctype, ptype, user=SERVICE_USER) for ptype in ("write", "create", "delete")):
			_unavailable()
	return SERVICE_USER
