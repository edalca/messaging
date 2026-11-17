"""Idempotent application lifecycle; setup imports never write or execute providers."""


def after_install():
    from messaging.conversation.doctype.chat_agent.chat_agent import setup_roles
    setup_roles()
    from messaging.security.service_identity import ensure_messaging_service_user
    ensure_messaging_service_user()
    from messaging.registry import synchronize_registry
    synchronize_registry()
    from messaging.presets.evaluations import register as register_evaluations
    register_evaluations()
    from messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets import provision_presets
    from messaging.presets.bot_flows import greeting, human_availability, registration
    provision_presets(app='messaging', declarations=(greeting.register, human_availability.register, registration.register))
    from messaging.chatbot.doctype.ai_evaluation_run.dependencies import dependencies_changed
    dependencies_changed()


def after_migrate():
    from messaging.registry import synchronize_registry
    synchronize_registry()


def before_app_uninstall(app_name: str) -> None:
    """Clean app-scoped technical metadata and protect source pages during uninstall."""
    import frappe

    target_app = str(app_name or "").strip()
    if target_app != "messaging":
        return

    modules = frappe.get_module_list("messaging")
    prev_dev_mode = frappe.conf.get("developer_mode")
    prev_in_migrate = getattr(frappe.flags, "in_migrate", False)
    try:
        frappe.conf.developer_mode = 0
        frappe.flags.in_migrate = True
        if modules and frappe.db.table_exists("Page"):
            for page_name in frappe.get_all("Page", filters={"module": ("in", modules)}, pluck="name"):
                frappe.delete_doc("Page", page_name, ignore_permissions=True, ignore_on_trash=True, force=True)
    finally:
        frappe.conf.developer_mode = prev_dev_mode
        frappe.flags.in_migrate = prev_in_migrate

    if frappe.db.table_exists("Dock Item"):
        frappe.db.delete("Dock Item", {"parent": "messaging"})
    if frappe.db.table_exists("Dock"):
        frappe.db.delete("Dock", {"app": "messaging"})
    if frappe.db.table_exists("Scheduled Job Type"):
        frappe.db.delete("Scheduled Job Type", {"method": ("like", "messaging.%")})
