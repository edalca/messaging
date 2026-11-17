"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const dir = __dirname;
const html = fs.readFileSync(path.join(dir, "my_conversations.html"), "utf8");
const css = fs.readFileSync(path.join(dir, "my_conversations.css"), "utf8");
const js = fs.readFileSync(path.join(dir, "my_conversations.js"), "utf8");
const api = require(path.join(dir, "my_conversations.js"));

// 1. HTML contract: Frappe microtemplate single-quote safety & simplified Addendum layout
assert.ok(!html.includes("'"), "my_conversations.html must not contain literal single quotes (breaks Frappe template wrapping)");
assert.ok(html.includes('data-testid="my-conversations-root"'));
assert.ok(!html.includes("mc-topbar"), "Must use native Frappe page header instead of duplicate topbar");
assert.ok(html.includes('frappe-control" data-fieldtype="Data"'), "Search box must use native Frappe frappe-control markup");
assert.ok(html.includes("input-with-feedback form-control input-xs"), "Search input must use native Frappe input classes");
assert.ok(html.includes("filter-button btn-mc-channel-filter") && html.includes("filter-x-button"), "Filter controls must match Conversation Inbox native Frappe filter group");
assert.ok(!html.includes("mc-kpi-paused"), "Paused summary counter must NOT exist");
assert.ok(!html.includes("mc-kpi-waiting"), "Waiting summary counter must NOT exist");
assert.ok(!html.includes("data-status="), "Paused/status tabs must NOT exist");
assert.ok(!html.includes("mc-btn-pause"), "Pause button must NOT exist in HTML");
assert.ok(!html.includes("mc-btn-resume"), "Resume button must NOT exist in HTML");
assert.ok(html.includes("mc-compose-input"), "Integrated composer textarea must exist");
assert.ok(html.includes("mc-send-btn"), "Circular send button must exist");

// 2. JS contract: Native Frappe header & filter controls, no Pause/Resume action dispatching, Close attention & origin labels present
assert.ok(js.includes("page.set_indicator"), "JS must use native Frappe page indicator");
assert.ok(js.includes("page.set_secondary_action"), "JS must use native Frappe page secondary action for Refresh");
assert.ok(js.includes("page.set_title_sub"), "JS must use native Frappe page subtitle");
assert.ok(js.includes("frappe.ui.Dropdown"), "JS must use native Frappe Dropdown for filter selector matching Conversation Inbox");
assert.ok(!js.includes("mc-btn-pause"), "JS must NOT render Pause button");
assert.ok(!js.includes("mc-btn-resume"), "JS must NOT render Resume button");
assert.ok(!js.includes('action: "Pause"'), "JS must NOT call Pause action");
assert.ok(!js.includes('action: "Resume"'), "JS must NOT call Resume action");
assert.ok(js.includes('action: "Close"'), "JS must call Close action");
assert.ok(js.includes("Close attention"), "JS must use Close attention label");
assert.ok(js.includes("Active human attention"), "JS must display Active human attention status");
assert.ok(js.includes("m.origin_label"), "JS must use the backend administrative origin projection");

// 3. CSS contract: Canonical side-based bubble colors (left=gray, right=green) & red Close button
assert.ok(css.includes("--mc-in-bg: var(--surface-gray-2"), "CSS must use neutral gray token for left/incoming subscriber messages");
assert.ok(css.includes("--mc-in-border: var(--outline-gray-1"), "CSS must use gray outline token for left/incoming messages");
assert.ok(css.includes("--mc-out-bg: var(--surface-green-2"), "CSS must use soft green token for right/outgoing bot and human replies");
assert.ok(css.includes("--mc-out-border: var(--outline-green-2"), "CSS must use green outline token for right/outgoing replies");
assert.ok(css.includes("--surface-red-7") && css.includes(".mc-close-btn"), "Close attention button must use solid red tokens");
assert.ok(css.includes("--ink-blue-7"), "CSS must use Emulator Console read icon blue token");
assert.ok(css.includes("--ink-red-7"), "CSS must use Emulator Console error icon red token");
assert.ok(css.includes(".mc-msg.is-incoming") && css.includes("align-self: flex-start"), "Incoming messages must align left");
assert.ok(css.includes(".mc-msg.is-outgoing") && css.includes("align-self: flex-end"), "Automatic/Human replies must align right");
assert.ok(css.includes(".mc-event-row") && css.includes("align-self: center"), "Events must align center");
assert.ok(css.includes("width: fit-content"), "Bubbles must fit message content");
assert.ok(css.includes(".mc-compose") && css.includes("border-radius: 20px"), "Composer must match Emulator Console pill style");
assert.ok(css.includes(".mc-send-btn") && css.includes("border-radius: 50%"), "Send button must match circular Emulator send button");

// 4. Unit assertions on exported helpers
assert.strictEqual(api.statusLabel("Human Active"), "Active human attention");
assert.strictEqual(api.statusLabel("Closed"), "Closed");

assert.strictEqual(api.closeReasonLabel({close_reason: "Manual"}), "Closed manually");
assert.strictEqual(api.closeReasonLabel({close_reason: "Inactivity"}), "Closed due to inactivity");
assert.strictEqual(api.closeReasonLabel({close_reason: "Manual", close_reason_label: "Finalizada manualmente"}), "Finalizada manualmente");

assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Delivered"}).map(s => [s.icon, s.tone]),
	[["check-check", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Accepted", delivery_status: "Read"}).map(s => [s.icon, s.tone]),
	[["check-check", "read"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Accepted", delivery_status: "Sent"}).map(s => [s.icon, s.tone]),
	[["check", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Sending"}).map(s => [s.icon, s.tone]),
	[["loader-circle", "muted"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs({status: "Failed"}).map(s => [s.icon, s.tone]),
	[["circle-alert", "error"]]
);
assert.deepStrictEqual(
	api.getTransportStatusSpecs(null, "sending").map(s => [s.icon, s.tone]),
	[["clock", "muted"]]
);

// Registration deduplication skips keys already shown in profile_fields
const deduped = api.collectDeduplicatedRegistration(
	[{key: "name", label: "Name", value: "Ana"}, {key: "phone", label: "Phone", value: "999"}],
	{name: "Ana", phone: "999", city: "Tegucigalpa", account_code: "ACC-1"}
);
assert.deepStrictEqual(deduped.map(r => r.key), ["city", "account_code"]);

// Pending optimistic message deduplication
const merged = api.mergeTimelineMessages(
	[{name: "M1", client_id: "c1", kind: "Human", content: "Hola"}],
	[{client_id: "c1", kind: "Human", content: "Hola"}, {client_id: "c2", kind: "Human", content: "Pendiente"}]
);
assert.strictEqual(merged.length, 2);
assert.strictEqual(merged[1].client_id, "c2");

// Origin aria labels
assert.strictEqual(api.buildMessageAriaLabel({kind: "Incoming", origin_label: "Subscriber", content: "Hola"}), "Subscriber: Hola");
assert.strictEqual(api.buildMessageAriaLabel({kind: "Automatic", origin_label: "Bot", content: "Hello"}), "Bot: Hello");
assert.strictEqual(api.buildMessageAriaLabel({kind: "Human", origin_label: "Agent", agent: "Administrator", agent_name: "Internal name", content: "Ok"}), "Agent: Ok");
assert.strictEqual(api.buildMessageAriaLabel({kind: "Human", agent: "Administrator", content: "Historical body"}), "Historical body");

// Session card HTML uses canonical display name & unread badge without Pause/Resume badges
const cardHtml = api.renderSessionCardHtml({
	name: "SESS-1",
	contact_name: "Carlos Ruiz",
	channel_name: "EMULR",
	account_name: "Emulador",
	unread: 2,
	last_message: "Necesito ayuda",
	last_message_time: new Date().toISOString(),
}, "SESS-1");
assert.ok(cardHtml.includes("is-selected"));
assert.ok(cardHtml.includes("mc-item-avatar"));
assert.ok(cardHtml.includes("tag-channel"));
assert.ok(cardHtml.includes("Carlos Ruiz"));
assert.ok(cardHtml.includes("mc-unread-badge"));

console.log("test_my_conversations_ui.cjs: ALL PASSED");
