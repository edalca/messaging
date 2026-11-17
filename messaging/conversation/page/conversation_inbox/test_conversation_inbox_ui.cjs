"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "conversation_inbox.html"), "utf8");
const js = fs.readFileSync(path.join(__dirname, "conversation_inbox.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "conversation_inbox.css"), "utf8");

// Administrative labels do not become part of the message or reveal internal agent identities.
{
	const rendered = [];
	const stream = {empty() { rendered.length = 0; return this; }, append(value) { rendered.push(value); }};
	const render = vm.runInNewContext(
		js.slice(js.indexOf("\tconst renderMessagesTimeline ="), js.indexOf("\n\tconst renderContactView =")) + "\nrenderMessagesTimeline;",
		{root: {find: selector => selector === ".detail-messages-stream" ? stream : {toggleClass() {}}},
			state: {hasMoreMessages: false}, escapeHtml: value => value, renderStatusIconsHtml: () => ""}
	);
	render([{kind: "Human", agent: "Administrator", origin_label: "Agent", content: "Public body"},
		{kind: "Automatic", agent: "Internal user", origin_label: "Bot", content: "Bot body"}]);
	assert.ok(rendered[0].includes('msg-sender-tag">Agent</div>'));
	assert.ok(rendered[0].includes('msg-text">Public body</div>'));
	assert.ok(!rendered.join("").includes("Administrator"));
	assert.ok(rendered[1].includes('msg-sender-tag">Bot</div>'));
	assert.ok(rendered[1].includes("outgoing-bot"));
}

// 1. Strict unassigned pending requests queue scope (no workspace tabs, no composer, no active/paused/closed controls)
assert.ok(!html.includes("queue-workspace-tabs"), "HTML must NOT include workspace switcher tabs");
assert.ok(!html.includes("workspace-tab-btn"), "HTML must NOT include workspace tab buttons");
assert.ok(!html.includes('data-workspace='), "HTML must NOT include workspace attributes");
assert.ok(!html.includes("detail-composer-shell"), "HTML must NOT include a message reply composer");
assert.ok(!html.includes("detail-reply-input"), "HTML must NOT include a reply textarea");
assert.ok(!html.includes("btn-send-reply"), "HTML must NOT include a send reply button");
assert.ok(!html.includes("btn-pause-attention"), "HTML must NOT include Pause attention action");
assert.ok(!html.includes("btn-resume-attention"), "HTML must NOT include Resume attention action");
assert.ok(!html.includes("btn-assign-agent"), "HTML must NOT include Assign other agent action");
assert.ok(!html.includes("btn-close-attention"), "HTML must NOT include Close attention action");
assert.ok(!js.includes('call("send"'), "JS must NOT expose message sending from Conversation Inbox");
assert.ok(!js.includes("Human Active") || !html.includes('data-status="Human Active"'), "Inbox must NOT include Active/Paused/Closed queue filters");
assert.ok(!html.includes('data-status="Human Paused"'), "HTML must NOT include Paused status filter");
assert.ok(!html.includes('data-status="Closed"'), "HTML must NOT include Closed status filter");

// 2. Three-panel structure verification
assert.ok(html.includes("inbox-column-queue"), "HTML must contain left panel (queue)");
assert.ok(html.includes("inbox-column-detail"), "HTML must contain center panel (detail)");
assert.ok(html.includes("inbox-column-contact"), "HTML must contain right panel (contact)");

// 3. Original three operational summary cards ONLY (To Attend, Waiting for Agent, Pending)
assert.ok(html.includes("inbox-summary-strip"), "HTML must contain summary strip");
assert.ok(html.includes("metric-total"), "HTML must contain total counter");
assert.ok(html.includes("metric-waiting"), "HTML must contain waiting counter");
assert.ok(html.includes("metric-pending"), "HTML must contain pending counter");
assert.ok(!html.includes("metric-mine"), "HTML must NOT include My Attentions counter");
assert.ok(!html.includes("metric-others"), "HTML must NOT include Other Agents counter");

// 4. Search input, Channel Filter button (ListView style), queue status filters, and Active Tag
assert.ok(html.includes("queue-search-input"), "HTML must contain search input");
assert.ok(html.includes("btn-channel-filter"), "HTML must contain channel filter button");
assert.ok(html.includes("filter-button"), "HTML must contain filter-button class");
assert.ok(html.includes("filter-x-button"), "HTML must contain filter-x-button beside filter-button");
assert.ok(html.includes('data-status=""') && html.includes('data-status="Waiting"') && html.includes('data-status="Pending"'), "HTML must preserve All/Waiting/Pending filters");
assert.ok(js.includes("unassigned_only: 1"), "JS must query unassigned pending requests strictly");
assert.ok(js.includes("frappe.ui.Dropdown"), "JS must open channels with the Desk view-switcher menu");
assert.ok(!html.includes("channel-filter-menu"), "HTML must not keep a custom channel menu");
assert.ok(html.includes("queue-active-filter-bar"), "HTML must contain queue-active-filter-bar");
assert.ok(
	html.indexOf("queue-status-filter-group") < html.indexOf("queue-active-filter-bar"),
	"Active tag bar must be positioned below the status filter group (All/Waiting/Pending)"
);
assert.ok(js.includes("active-channel-tag"), "JS must create active channel tag");
assert.ok(js.includes("getTagTheme"), "JS must use theme hashing matching Form tags");
assert.ok(!js.includes('val("").text(__("All Channels"))'), "JS must NOT add All Channels to dropdown menu");
assert.ok(html.includes("Search by contact..."), "HTML placeholder must be Search by contact...");
assert.ok(!html.includes("select-channel-filter"), "HTML must NOT contain old channel select");
assert.ok(!html.includes("select-account-filter"), "HTML must NOT contain old account select");
assert.ok(!html.includes("btn-queue-prev"), "HTML must NOT contain pagination prev button");
assert.ok(!html.includes("btn-queue-next"), "HTML must NOT contain pagination next button");

// 5. Take Attention action, eligibility banner, Realtime synchronization (no excessive polling)
assert.ok(html.includes("btn-take-attention"), "HTML must contain 'Take Attention' button");
assert.ok(js.includes('action: "Take"'), "JS must call Take action via human.act");
assert.ok(js.includes("clearDetailView()"), "JS must clear selection once request is taken and leaves queue");
assert.ok(js.includes("agent_status"), "JS must reflect backend agent eligibility and capacity");
assert.ok(js.includes('frappe.realtime.on("messaging_conversation_update"'), "JS must subscribe to Realtime updates");
assert.ok(js.includes('frappe.realtime.off("messaging_conversation_update"'), "JS must unsubscribe Realtime on cleanup");
assert.ok(js.includes("(!payload?.session || payload.session === state.selectedSession)"), "JS must avoid redundant history calls when another session changes");
assert.ok(js.includes("preservePagination: true"), "JS must preserve queue pagination/scroll during sync updates");
assert.ok(!js.includes("setInterval") || js.includes("60000"), "JS must NOT run aggressive polling");

// 6. Right panel provenance section
assert.ok(html.includes("provenance-val-channel"), "Right panel must display channel");
assert.ok(html.includes("provenance-val-account"), "Right panel must display account");
assert.ok(html.includes("btn-open-account-document"), "Right panel must have link to account document when authorized");
assert.ok(html.includes("btn-open-contact-history"), "Right panel must have button to view contact history");

// 7. CSS verification
assert.ok(css.includes(".inbox-workspace-grid"), "CSS must contain grid layout");
assert.ok(css.includes(".summary-metric-card"), "CSS must contain summary card styling");
assert.ok(css.includes(".active-channel-tag"), "CSS must contain active channel tag styling");
assert.ok(css.includes('[data-theme="dark"] .active-channel-tag'), "CSS must contain dark mode styling for active channel tag");
assert.ok(css.includes(".channel-filter-dropdown"), "CSS must contain channel filter dropdown styling");
assert.ok(css.includes("@media (max-width: 767px)"), "CSS must contain responsive mobile breakpoint");
assert.ok(!css.includes(".queue-workspace-tabs"), "CSS must NOT keep removed workspace tabs styling");
assert.ok(!css.includes(".detail-composer-shell"), "CSS must NOT keep removed composer styling");

console.log("UI structural tests passed successfully! All contract requirements verified.");
