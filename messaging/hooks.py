app_name = "messaging"
app_title = "Messaging"
app_publisher = "Edwin Carrillo"
app_description = "Generic messaging and communication engine"
app_email = "edwinalonso162@hotmail.com"
app_license = "mit"
app_logo_url = "/assets/messaging/icons/messaging.svg"

# Apps
# ------------------

required_apps = []

# Each item in the list will be shown as an app in the apps page
add_to_apps_screen = [
	{
		"name": "messaging",
		"logo": app_logo_url,
		"title": "Messaging",
	}
]

# The dock, the rail down the left of the desk, is a document rather than a hook. Author it in
# Manage Dock on a developer-mode site and press Export to App, and it is written to
# `messaging/dock/messaging/messaging.json` for git to carry. An app that ships none has no
# rail: its sidebar gets a switcher in the header instead.
#
# A companion app, one that extends a host app rather than standing on its own, says so with
# `mount_on` on that same record, and its entries are appended to the host's rail. Mounting keeps
# the companion off the apps screen, so it takes precedence over any add_to_apps_screen above.

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/messaging/css/messaging.css"
# app_include_js = "/assets/messaging/js/messaging.js"

# include js, css files in header of web template
# web_include_css = "/assets/messaging/css/messaging.css"
# web_include_js = "/assets/messaging/js/messaging.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "messaging/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_kanban_js = {"doctype" : "public/js/doctype_kanban.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
app_include_icons = ["/assets/messaging/icons/icons.svg"]

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "messaging.utils.jinja_methods",
# 	"filters": "messaging.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "messaging.install.before_install"
# after_install = "messaging.install.after_install"

after_install = "messaging.setup.after_install"

# Migration
# ------------

after_migrate = "messaging.setup.after_migrate"

# Uninstallation
# ------------

# before_uninstall = "messaging.uninstall.before_uninstall"
# after_uninstall = "messaging.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "messaging.utils.before_app_install"
# after_app_install = "messaging.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "messaging.utils.before_app_uninstall"
before_app_uninstall = "messaging.setup.before_app_uninstall"
after_app_uninstall = (
	"messaging.communication.doctype.process_definition.process_definition.after_app_uninstall"
)

# Build
# ------------------
# To hook into the build process

# after_build = "messaging.build.after_build"

# To hook into the build process of other apps
# The list of apps being built is passed as an argument

# after_app_build = "messaging.build.after_app_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "messaging.notifications.get_notification_config"

# Awesome Bar
# -----------
# Extra search results: list of dicts with label, description, route, index.
# route: ["List", "ToDo"], "/desk/docs/some/page", or "https://example.com"
# awesomebar_search = ["messaging.search.awesomebar_results"]

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Human operations share one account scope and assignment policy in Desk and APIs.
permission_query_conditions = {
	"AI Request Log": "messaging.chatbot.doctype.ai_request_log.ai_request_log.get_permission_query_conditions",
	"Chat Session": "messaging.conversation.doctype.chat_session.permissions.session_query",
	"Chat Contact": "messaging.conversation.doctype.chat_session.permissions.contact_query",
	"Chat Message": "messaging.conversation.doctype.chat_session.permissions.message_query",
	"Conversation Preference": "messaging.conversation.doctype.conversation_preference.conversation_preference.preference_query",
}

has_permission = {
	"AI Request Log": "messaging.chatbot.doctype.ai_request_log.ai_request_log.has_permission",
	"Chat Session": "messaging.conversation.doctype.chat_session.permissions.session_permission",
	"Chat Contact": "messaging.conversation.doctype.chat_session.permissions.contact_permission",
	"Chat Message": "messaging.conversation.doctype.chat_session.permissions.message_permission",
	"Conversation Preference": "messaging.conversation.doctype.conversation_preference.conversation_preference.preference_permission",
}

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Observe business events independently of explicit channel ingress.
doc_events = {
	"Process Definition": {
		"on_update": [
			"messaging.chatbot.doctype.bot_flow.bot_flow.invalidate_process_approval",
			"messaging.communication.doctype.notification_rule.notification_rule.invalidate_process_approval",
		],
	},
	"AI Provider": {
		"on_update": [
			"messaging.chatbot.doctype.ai_configuration.ai_configuration.provider_changed",
			"messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
		],
	},
	"Bot Settings": {
		"on_update": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed"
	},
	"Bot Flow": {
		"on_update": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
		"after_delete": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
	},
	"AI Evaluation Case Template": {
		"on_update": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
		"after_delete": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
	},
	"AI Evaluation Case": {
		"on_update": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
		"after_delete": "messaging.chatbot.doctype.ai_evaluation_run.dependencies.dependencies_changed",
	},
	"*": {
		"after_insert": "messaging.communication.doctype.notification_rule.events.document_event",
		"on_update": "messaging.communication.doctype.notification_rule.events.document_event",
		"on_submit": "messaging.communication.doctype.notification_rule.events.document_event",
		"on_cancel": "messaging.communication.doctype.notification_rule.events.document_event",
		"on_update_after_submit": "messaging.communication.doctype.notification_rule.events.document_event",
	},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"messaging.tasks.all"
# 	],
# 	"daily": [
# 		"messaging.tasks.daily"
# 	],
# 	"hourly": [
# 		"messaging.tasks.hourly"
# 	],
# 	"weekly": [
# 		"messaging.tasks.weekly"
# 	],
# 	"monthly": [
# 		"messaging.tasks.monthly"
# 	],
# }

scheduler_events = {
	"cron": {
		"* * * * *": [
			"messaging.communication.doctype.notification_queue.dispatch.dispatch_due",
			"messaging.conversation.doctype.chat_turn.inbox.recover_pending",
		]
	},
	"hourly": ["messaging.conversation.doctype.chat_session.human.close_inactive"],
}

# Testing
# -------

# before_tests = "messaging.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "messaging.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "messaging.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "messaging.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Session Events
# --------------

boot_session = "messaging.boot.boot_session"

# Request Events
# ----------------
# before_request = ["messaging.utils.before_request"]
# after_request = ["messaging.utils.after_request"]

# Job Events
# ----------
# before_job = ["messaging.utils.before_job"]
# after_job = ["messaging.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"messaging.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

# Technical declarations use the single public Registry hook.
messaging_registry = "messaging.presets.register.register"
