"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const dir = __dirname;
const pageJson = JSON.parse(fs.readFileSync(path.join(dir, "conversation_supervision.json"), "utf8"));
const html = fs.readFileSync(path.join(dir, "conversation_supervision.html"), "utf8");
const css = fs.readFileSync(path.join(dir, "conversation_supervision.css"), "utf8");
const js = fs.readFileSync(path.join(dir, "conversation_supervision.js"), "utf8");
const api = require(path.join(dir, "conversation_supervision.js"));

// 1. Page metadata: System Manager ONLY
assert.strictEqual(pageJson.name, "conversation-supervision");
assert.strictEqual(pageJson.module, "Conversation");
assert.deepStrictEqual(pageJson.roles, [{role: "System Manager"}]);

// 2. HTML contract: Frappe microtemplate safety, KPIs, Agent Load, 3-pane layout, strict read-only thread
assert.ok(!html.includes("'"), "conversation_supervision.html must not contain literal single quotes");
assert.ok(html.includes('data-testid="conversation-supervision-root"'));
assert.ok(html.includes('data-testid="cs-kpi-active"'));
assert.ok(html.includes('data-testid="cs-kpi-waiting"'));
assert.ok(html.includes('data-testid="cs-kpi-agents"'));
assert.ok(html.includes('data-testid="cs-kpi-avg-wait"'));
assert.ok(html.includes('data-testid="cs-agent-strip"'));
assert.ok(html.includes('data-testid="cs-tab-active"'));
assert.ok(html.includes('data-testid="cs-tab-queued"'));
assert.ok(html.includes('data-testid="cs-tab-all"'));
assert.ok(html.includes('data-testid="cs-list-pane"'));
assert.ok(html.includes('data-testid="cs-chat-pane"'));
assert.ok(html.includes('data-testid="cs-info-pane"'));
assert.ok(html.includes('data-testid="cs-detail-tab-session"'));
assert.ok(html.includes('data-testid="cs-detail-tab-agent"'));
assert.ok(html.includes('data-testid="cs-detail-tab-contact"'));
assert.ok(html.includes('data-testid="cs-pagination"'));

// Supervisor must NEVER have composer, reply, take, close, or pause controls
assert.ok(!html.includes("<textarea"), "Supervision must NOT include any textarea composer");
assert.ok(!html.includes("composer"), "Supervision must NOT include a message composer");
assert.ok(!html.includes("send-btn"), "Supervision must NOT include a send button");
assert.ok(!html.includes("Take Attention"), "Supervision must NOT include Take Attention");
assert.ok(!html.includes("Close attention"), "Supervision must NOT include Close attention");
assert.ok(!html.includes("Pause"), "Supervision must NOT include Pause");
assert.ok(!html.includes("Resume"), "Supervision must NOT include Resume");

// 3. JS contract: supervision endpoints, read-only GETs, POST only for reassign, realtime lifecycle cleanup
assert.ok(js.includes("messaging.conversation.doctype.chat_session.supervision"));
assert.ok(js.includes('callGet("overview"'));
assert.ok(js.includes('callGet("conversations"'));
assert.ok(js.includes('callGet("detail"'));
assert.ok(js.includes('callGet("reassignment_candidates"'));
assert.ok(js.includes('callPost("reassign"'));
assert.ok(!js.includes("mark_read"), "Supervision must NOT call mark_read");
assert.ok(!js.includes("client_status"), "Supervision must NOT send client read receipts");
assert.ok(!js.includes('inbox.send'), "Supervision must NOT call inbox.send");
assert.ok(!js.includes('action: "Take"'), "Supervision must NOT take conversations");
assert.ok(!js.includes('action: "Close"'), "Supervision must NOT close conversations");
assert.ok(js.includes("m.origin_label"), "JS must use backend origin_label");
assert.ok(js.includes("overviewRequestSeq") && js.includes("listRequestSeq") && js.includes("detailRequestSeq"));
assert.ok(js.includes('frappe.realtime.on(REALTIME_EVENT'));
assert.ok(js.includes('frappe.realtime.off(REALTIME_EVENT'));

// 4. CSS contract: 3px left border, 36px avatar, bubble tokens, dark mode, responsive mobile 4-step navigation
assert.ok(css.includes(".cs-session-item.is-selected") && css.includes("border-left-color"));
assert.ok(css.includes("border-left: 3px solid transparent"));
assert.ok(css.includes("width: 36px") && css.includes("height: 36px"));
assert.ok(css.includes("--cs-in-bg: var(--surface-gray-2"));
assert.ok(css.includes("--cs-out-bg: var(--surface-green-2"));
assert.ok(css.includes(".cs-msg.is-incoming") && css.includes("align-self: flex-start"));
assert.ok(css.includes(".cs-msg.is-outgoing") && css.includes("align-self: flex-end"));
assert.ok(css.includes(".cs-event-row") && css.includes("align-self: center"));
assert.ok(css.includes('[data-theme="dark"]'));
assert.ok(css.includes("@media (max-width: 767px)"));
assert.ok(css.includes('data-mobile-view="summary"'));
assert.ok(css.includes('data-mobile-view="list"'));
assert.ok(css.includes('data-mobile-view="chat"'));
assert.ok(css.includes('data-mobile-view="details"'));

// 5. Unit tests on helpers
assert.strictEqual(api.initialsOf("María López"), "ML");
assert.strictEqual(api.initialsOf("000000000000000004"), "#");
assert.strictEqual(api.escapeHtml("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;");
assert.strictEqual(api.formatDuration(null), "—");
assert.strictEqual(api.formatDuration(45), "45s");
assert.strictEqual(api.formatDuration(125), "2m 5s");
assert.strictEqual(api.formatDuration(3660), "1h 1m");

assert.deepStrictEqual(api.allowedOrderKeysForState("all", ""), ["last_activity"]);
assert.deepStrictEqual(api.allowedOrderKeysForState("active", ""), ["last_activity", "oldest_attention"]);
assert.deepStrictEqual(api.allowedOrderKeysForState("queued", ""), ["last_activity", "longest_wait"]);
assert.deepStrictEqual(api.allowedOrderKeysForState("all", "Human Queued"), ["last_activity", "longest_wait"]);
assert.deepStrictEqual(api.allowedOrderKeysForState("all", "Human Active"), ["last_activity", "oldest_attention"]);

// Transport icons only when evidence exists
assert.deepStrictEqual(api.getTransportStatusSpecs(null), []);
assert.deepStrictEqual(api.getTransportStatusSpecs({}), []);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Accepted", delivery_status: "Read"}).map(s => [s.icon, s.tone]),
	[["check-check", "read"]]
);

// Consolidated compact user card rendering: selector button, no expandable details, no per-account lists or bars
assert.ok(!js.includes("View account details"), "JS must NOT include View account details");
assert.ok(!js.includes("Hide account details"), "JS must NOT include Hide account details");
assert.ok(!js.includes("expandedAgents"), "JS must NOT retain expansion state");
assert.ok(!css.includes(".cs-agent-detail"), "CSS must NOT retain unused detail panel styles");

const mixedUserCard = api.renderAgentUserCardHtml({
	user: "admin@example.invalid",
	full_name: "Administrator",
	account_count: 3,
	active_conversations: 2,
	can_receive: true,
	status_code: "available",
	reason_code: "available",
	grants: [
		{account: "ACC-1", account_name: "EMULR-TEST-01"},
		{account: "ACC-2", account_name: "EMULR-TEST-02"},
		{account: "EMU-OLD", account_name: "EMU-OLD"},
	],
}, "admin@example.invalid");
assert.ok(mixedUserCard.startsWith('<button type="button"'));
assert.ok(mixedUserCard.includes("is-selected"));
assert.ok(mixedUserCard.includes('aria-pressed="true"'));
assert.ok(mixedUserCard.includes("AD"));
assert.ok(mixedUserCard.includes("Administrator"));
assert.ok(mixedUserCard.includes("3 linked accounts"));
assert.ok(mixedUserCard.includes("2 active conversations"));
assert.ok(mixedUserCard.includes("Available"));
assert.ok(!mixedUserCard.includes("EMULR-TEST-01"), "Compact card must NOT render account list");
assert.ok(!mixedUserCard.includes("cs-agent-detail"), "Compact card must NOT render expandable detail");
assert.ok(!mixedUserCard.includes("cs-agent-bar"), "Compact card must NOT render capacity bars");
assert.ok(!mixedUserCard.includes("cs-agent-expand-btn"), "Compact card must NOT render expand button");

const disabledUserCard = api.renderAgentUserCardHtml({
	user: "norole@example.invalid",
	full_name: "Ana Perez",
	account_count: 1,
	active_conversations: 0,
	can_receive: false,
	status_code: "not_enabled",
	reason_code: "missing_role",
}, "");
assert.ok(disabledUserCard.includes("is-disabled"));
assert.ok(disabledUserCard.includes('aria-pressed="false"'));
assert.ok(disabledUserCard.includes("1 linked account"));
assert.ok(disabledUserCard.includes("0 active conversations"));
assert.ok(disabledUserCard.includes("Not enabled"));

const noCapacityCard = api.renderAgentUserCardHtml({
	user: "full@example.invalid",
	full_name: "Luis Vega",
	account_count: 2,
	active_conversations: 4,
	can_receive: false,
	status_code: "full",
}, "");
assert.ok(noCapacityCard.includes("No capacity"));
assert.ok(noCapacityCard.includes("4 active conversations"));

// JS consolidation helper preserves distinct users with identical display names
const sameNameUsers = api.consolidateAgentWorkload([
	{
		user: "maria.1@example.invalid",
		full_name: "María López",
		channel: "emulator",
		account: "ACC-1",
		effective_enabled: true,
		available: true,
		can_receive: true,
		capacity: 3,
		occupied: 1,
		active_count: 1,
		status_code: "available",
		reason_code: "available",
	},
	{
		user: "maria.2@example.invalid",
		full_name: "María López",
		channel: "emulator",
		account: "ACC-1",
		effective_enabled: true,
		available: true,
		can_receive: false,
		capacity: 2,
		occupied: 2,
		active_count: 2,
		status_code: "full",
		reason_code: "full",
	},
]);
assert.strictEqual(sameNameUsers.length, 2);
assert.strictEqual(sameNameUsers[0].user, "maria.1@example.invalid");
assert.strictEqual(sameNameUsers[1].user, "maria.2@example.invalid");
assert.strictEqual(sameNameUsers[0].status_code, "available");
assert.strictEqual(sameNameUsers[1].status_code, "full");

// Session card rendering distinguishes Human Active vs Human Queued
const activeSessionCard = api.renderSessionCardHtml({
	name: "SES-0001",
	contact_name: "Julio Diez",
	channel_name: "EMULR",
	account_name: "Cuenta 1",
	status: "Human Active",
	assigned_agent: "maria@example.invalid",
	assigned_agent_name: "María López",
	last_message: "Hola agente",
	last_activity: new Date().toISOString(),
	unread: 1,
}, "SES-0001");
assert.ok(activeSessionCard.includes("is-selected"));
assert.ok(activeSessionCard.includes("cs-badge-in-attention"));
assert.ok(activeSessionCard.includes("María López"));

const queuedSessionCard = api.renderSessionCardHtml({
	name: "SES-0002",
	contact_name: "Elena Centro",
	channel_name: "EMULR",
	account_name: "Cuenta 1",
	status: "Human Queued",
	assigned_agent: "",
	last_message: "Necesito ayuda",
	supervision_wait_seconds: 185,
}, "SES-0001");
assert.ok(!queuedSessionCard.includes("is-selected"));
assert.ok(queuedSessionCard.includes("cs-badge-waiting"));
assert.ok(queuedSessionCard.includes("Unassigned"));
assert.ok(queuedSessionCard.includes("3m 5s"));

console.log("test_conversation_supervision_ui.cjs passed");
