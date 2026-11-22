"""Drafts, publication actions and public routes owned by Communication Channel."""
import json
import re
from html import unescape
from urllib.parse import quote, urlsplit

import frappe
import nh3
from frappe import _
from frappe.utils import cstr, nowdate, strip_html, validate_email_address
from frappe.website.page_renderers.base_renderer import BaseRenderer
from werkzeug.wrappers import Response

from messaging.common.public_origin import configured_public_origin
from messaging.security.permissions import require_system_manager

DOCTYPE = "Communication Channel"
DOCUMENTS = {
	"privacy-policy": ("public_privacy_draft", "Privacy Policy"),
	"terms-of-service": ("public_terms_draft", "Terms of Service"),
	"data-deletion": ("public_deletion_draft", "Data Deletion Instructions"),
}
ROUTE = re.compile(r"messaging/([A-Za-z0-9_-]{1,140})/(privacy-policy|terms-of-service|data-deletion)")
SNAPSHOTS = "public_document_snapshots"
PAGE = "templates/public_information/document.html"
TAGS = {"p", "br", "div", "h2", "h3", "h4", "ol", "ul", "li", "blockquote", "b", "strong", "i", "em", "u", "a"}
NO_CACHE = {"Cache-Control": "no-store"}
PUBLIC_HEADERS = {
	**NO_CACHE,
	"Content-Security-Policy": "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
	"X-Content-Type-Options": "nosniff",
	"Referrer-Policy": "no-referrer",
}


def _clean_html(body):
	return nh3.clean(
		body or "", tags=TAGS, attributes={"a": {"href"}}, url_schemes={"https", "http", "mailto"},
		clean_content_tags={"script", "style", "svg", "math", "iframe", "object", "template"},
	)


def _text(body):
	return unescape(strip_html(body or "")).strip()


def _website(website):
	try:
		parsed = urlsplit(website)
		if (parsed.scheme in ("https", "http") and parsed.hostname and parsed.username is None
			and parsed.password is None and not any(c.isspace() or c == "\\" for c in website)):
			return website
	except ValueError:
		pass
	return ""


def validate_public_information(doc, previous):
	if (doc.get(SNAPSHOTS) or "") != ((previous.get(SNAPSHOTS) or "") if previous else ""):
		frappe.throw(_("Use the public document actions to change publication."), frappe.PermissionError)
	for field, _title in DOCUMENTS.values():
		body = doc.get(field)
		if body:
			doc.set(field, _clean_html(body))


def _channel(channel, permission, *, for_update=False):
	require_system_manager()
	doc = frappe.get_doc(DOCTYPE, channel, for_update=for_update)
	doc.check_permission(permission)
	return doc


def _path(channel, document):
	return f"/messaging/{channel}/{document}"


def _snapshots(value):
	return json.loads(value) if value else {}


def _document(document):
	if not isinstance(document, str) or document not in DOCUMENTS:
		frappe.throw(_("Select a public document."))
	return DOCUMENTS[document]


def _draft(doc, field):
	return dict(
		organization=cstr(doc.public_organization).strip(),
		service=cstr(doc.public_service).strip(),
		email=cstr(doc.public_email).strip(),
		website=_website(cstr(doc.public_website).strip()),
		contact=cstr(doc.public_contact).strip(),
		body=_clean_html(doc.get(field)),
	)


def _render(document, content, *, links=(), preview=False):
	return frappe.render_template(PAGE, dict(
		title=_(DOCUMENTS[document][1]), content=content, links=links, preview=preview,
		mail_link="mailto:" + quote(content["email"], safe="@"),
		language=frappe.local.lang or "es",
	))


@frappe.whitelist(methods=["GET"])
def publication_status(channel):
	doc = _channel(channel, "read")
	snapshots = _snapshots(doc.get(SNAPSHOTS))
	origin = configured_public_origin()
	return {
		document: dict(
			published=document in snapshots,
			url=origin + _path(doc.name, document) if origin and document in snapshots else None,
		)
		for document in DOCUMENTS
	}


@frappe.whitelist(methods=["POST"])
def standard_draft(channel, document):
	doc = _channel(channel, "write")
	_document(document)
	return frappe.render_template("templates/public_information/default_es.html", dict(
		document=document, description=cstr(doc.public_description).strip(),
	)).strip()


@frappe.whitelist(methods=["POST"])
def preview_draft(channel, document):
	doc = _channel(channel, "read")
	field, _title = _document(document)
	if not _text(doc.get(field)):
		frappe.throw(_("Save a draft before previewing it."))
	return _render(document, _draft(doc, field), preview=True)


@frappe.whitelist(methods=["POST"])
def set_publication(channel, document, action, expected_modified):
	"""Publish a reviewed saved draft or withdraw the exact saved publication."""
	doc = _channel(channel, "write", for_update=True)
	field, _title = _document(document)
	if cstr(doc.modified) != expected_modified:
		frappe.throw(_("The channel changed. Reload it and review the document again."), frappe.TimestampMismatchError)
	if action not in ("publish", "withdraw"):
		frappe.throw(_("Select a publication action."))

	snapshots = _snapshots(doc.get(SNAPSHOTS))
	if action == "publish":
		content = _draft(doc, field)
		if not content["organization"] or not content["service"] or not content["email"]:
			frappe.throw(_("Enter a public organization, service name, and contact email before publishing."))
		if "," in content["email"] or validate_email_address(content["email"]) != content["email"]:
			frappe.throw(_("Enter one valid public contact email."))
		if doc.public_website and not content["website"]:
			frappe.throw(_("Enter a valid HTTP or HTTPS address for the official website."))
		text = _text(content["body"])
		if not text or "[Por completar:" in text:
			frappe.throw(_("Complete all draft sections before publishing."))
		content["updated"] = nowdate()
		snapshots[document] = content
	elif document in snapshots:
		del snapshots[document]
	else:
		return

	frappe.db.set_value(DOCTYPE, doc.name, SNAPSHOTS, json.dumps(snapshots, ensure_ascii=False))


class PublicDocumentPage(BaseRenderer):
	def can_render(self):
		return ROUTE.fullmatch(self.path) is not None

	def render(self):
		if frappe.request.method not in ("GET", "HEAD"):
			return Response(status=405, headers={**NO_CACHE, "Allow": "GET, HEAD"})
		channel, document = ROUTE.fullmatch(self.path).groups()
		row = frappe.db.get_value(DOCTYPE, channel, ["name", SNAPSHOTS], as_dict=True)
		snapshots = _snapshots(row.get(SNAPSHOTS)) if row and row.name == channel else {}
		if document not in snapshots:
			return Response(_("Page not found"), status=404, headers=PUBLIC_HEADERS, mimetype="text/plain")
		links = [
			dict(path=_path(channel, key), title=_(title))
			for key, (_field, title) in DOCUMENTS.items() if key in snapshots and key != document
		]
		return Response(_render(document, snapshots[document], links=links), headers=PUBLIC_HEADERS, mimetype="text/html")
