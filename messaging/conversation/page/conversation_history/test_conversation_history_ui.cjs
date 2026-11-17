"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, "conversation_history.html"), "utf8");
const css = fs.readFileSync(path.join(dir, "conversation_history.css"), "utf8");
const js = fs.readFileSync(path.join(dir, "conversation_history.js"), "utf8");
const api = require(path.join(dir, "conversation_history.js"));

// 1. HTML contract: Frappe microtemplate safety, 3-pane layout, filters, and strict read-only UI
assert.ok(!html.includes("'"), "conversation_history.html must not contain literal single quotes");
assert.ok(html.includes('data-testid="conversation-history-root"'));
assert.ok(html.includes('data-testid="ch-list-pane"'));
assert.ok(html.includes('data-testid="ch-chat-pane"'));
assert.ok(html.includes('data-testid="ch-info-pane"'));

assert.ok(html.includes('data-testid="ch-search-input"'));
assert.ok(html.includes('data-testid="ch-filter-toggle"'));
assert.ok(html.includes('data-testid="ch-filter-clear"'));
assert.ok(html.includes('data-testid="ch-filter-mine"'));
assert.ok(html.includes('data-testid="ch-filter-date-from"'));
assert.ok(html.includes('data-testid="ch-filter-date-to"'));
assert.ok(html.includes('data-testid="ch-filter-channel"'));
assert.ok(html.includes('data-testid="ch-filter-account"'));
assert.ok(html.includes('data-testid="ch-filter-attention-type"'));
assert.ok(html.includes('data-testid="ch-filter-status"'));
assert.ok(html.includes('data-testid="ch-filter-close-reason"'));
assert.ok(html.includes('data-testid="ch-filter-agent"'));
assert.ok(html.includes('data-testid="ch-quick-closed"'));
assert.ok(html.includes('data-testid="ch-quick-expired"'));

// Strictly read-only: no composer, no textarea, no operational actions
assert.ok(!html.includes("<textarea"), "History must NOT include any textarea composer");
assert.ok(!html.includes("composer"), "History must NOT include a message composer");
assert.ok(!html.includes("send-btn"), "History must NOT include a send button");
assert.ok(!html.includes("Take Attention"), "History must NOT include Take Attention");
assert.ok(!html.includes("Close attention"), "History must NOT include Close attention");
assert.ok(!html.includes("Pause"), "History must NOT include Pause");
assert.ok(!html.includes("Resume"), "History must NOT include Resume");
assert.ok(!html.includes("Reassign"), "History must NOT include Reassign");

// 2. JS contract: read-only GET APIs, origin_label, race guards, deep linking, no write endpoints
assert.ok(js.includes("historical_conversations"), "JS must call historical_conversations");
assert.ok(js.includes("historical: 1"), "JS must pass historical: 1 to history");
assert.ok(js.includes('type: "GET"'), "JS must use GET requests only");
assert.ok(!js.includes('type: "POST"'), "JS must NOT execute POST requests");
assert.ok(!js.includes(".send"), "JS must NOT reference inbox.send");
assert.ok(!js.includes("human.act"), "JS must NOT reference human.act");
assert.ok(!js.includes("mark_read"), "JS must NOT reference mark_read");
assert.ok(!js.includes("client_status"), "JS must NOT send client read receipts");
assert.ok(js.includes("m.origin_label"), "JS must use backend origin_label");
assert.ok(js.includes("listRequestSeq") && js.includes("detailRequestSeq"), "JS must guard against stale HTTP responses");
assert.ok(js.includes("consumeRequestedSession"), "JS must support ?session= deep links");
assert.ok(js.includes("getFrappeTimeZone"), "JS must format timestamps in Frappe configured timezone");

// 3. CSS contract: 3px black left border, 36px avatar, canonical bubble tokens & responsive layout
assert.ok(css.includes(".ch-session-item.is-selected") && css.includes("border-left-color"));
assert.ok(css.includes("border-left: 3px solid transparent"));
assert.ok(css.includes("width: 36px") && css.includes("height: 36px"));
assert.ok(css.includes("--ch-in-bg: var(--surface-gray-2"));
assert.ok(css.includes("--ch-out-bg: var(--surface-green-2"));
assert.ok(css.includes(".ch-msg.is-incoming") && css.includes("align-self: flex-start"));
assert.ok(css.includes(".ch-msg.is-outgoing") && css.includes("align-self: flex-end"));
assert.ok(css.includes(".ch-event-row") && css.includes("align-self: center"));
assert.ok(css.includes("@media (max-width: 767px)"));

// 4. Unit tests on helpers
assert.strictEqual(api.initialsOf("Diego Cuatro"), "DC");
assert.strictEqual(api.initialsOf("000000000000000004"), "#");
assert.strictEqual(api.escapeHtml("<script>alert(1)</script>"), "&lt;script&gt;alert(1)&lt;/script&gt;");

assert.strictEqual(api.formatDuration(null), "");
assert.strictEqual(api.formatDuration(-5), "");
assert.strictEqual(api.formatDuration(42), "42s");
assert.strictEqual(api.formatDuration(125), "2m 5s");
assert.strictEqual(api.formatDuration(3660), "1h 1m");
assert.strictEqual(api.formatDuration(90000), "1d 1h");

assert.strictEqual(api.closeReasonLabel({close_reason: "Manual"}), "Closed manually");
assert.strictEqual(api.closeReasonLabel({close_reason: "Inactivity"}), "Closed due to inactivity");
assert.strictEqual(api.closeReasonLabel({close_reason: "Technical Error"}), "Technical error");
assert.strictEqual(api.attentionTypeLabel({attention_type: "Human"}), "Human attention");
assert.strictEqual(api.attentionTypeLabel({attention_type: "Automatic"}), "Automatic attention");

// Transport evidence icons: empty when no evidence, accurate when dispatch exists
assert.deepStrictEqual(api.getTransportStatusSpecs(null), []);
assert.deepStrictEqual(api.getTransportStatusSpecs({}), []);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Prepared"}).map(s => [s.icon, s.tone]),
	[["clock", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Sending"}).map(s => [s.icon, s.tone]),
	[["loader-circle", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Accepted", delivery_status: "Sent"}).map(s => [s.icon, s.tone]),
	[["check", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Accepted", delivery_status: "Delivered"}).map(s => [s.icon, s.tone]),
	[["check-check", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Accepted", delivery_status: "Read"}).map(s => [s.icon, s.tone]),
	[["check-check", "read"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Rejected"}).map(s => [s.icon, s.tone]),
	[["circle-alert", "error"]]
);

// Multiple distinct episodes for the same contact keep independent identity, status & reason
const cardClosed = api.renderSessionCardHtml({
	name: "SES-002825",
	contact_name: "Diego Cuatro",
	channel_name: "Canal Emulado",
	account_name: "Cuenta #1",
	status: "Closed",
	close_reason: "Manual",
	attention_type: "Human",
	last_message: "Consulta resuelta",
	activity_at: "2026-11-16T03:32:01",
}, "SES-002825", "America/Tegucigalpa");

const cardExpired = api.renderSessionCardHtml({
	name: "SES-002416",
	contact_name: "Diego Cuatro",
	channel_name: "Canal Emulado",
	account_name: "Cuenta #1",
	status: "Expired",
	close_reason: "",
	attention_type: "Automatic",
	last_message: "Saldo consultado",
	activity_at: "2026-11-15T08:20:05",
}, "SES-002825", "America/Tegucigalpa");

assert.ok(cardClosed.includes("is-selected"));
assert.ok(cardClosed.includes("SES-002825"));
assert.ok(cardClosed.includes("Closed manually"));
assert.ok(cardClosed.includes("Human attention"));

assert.ok(!cardExpired.includes("is-selected"));
assert.ok(cardExpired.includes("SES-002416"));
assert.ok(cardExpired.includes("Expired"));
assert.ok(cardExpired.includes("Automatic attention"));
assert.ok(!cardExpired.includes("Closed manually"));
assert.ok(!cardExpired.includes("Closed due to inactivity"));

// UTC to Frappe presentation timezone conversion (America/Tegucigalpa = UTC-6)
const formattedTime = api.formatTime("2026-11-16T03:32:01", "America/Tegucigalpa");
assert.ok(formattedTime.includes("21:32") || formattedTime.includes("9:32"), `Expected UTC 03:32 -> 21:32/9:32 PM in Tegucigalpa, got ${formattedTime}`);

console.log("test_conversation_history_ui.cjs passed");
