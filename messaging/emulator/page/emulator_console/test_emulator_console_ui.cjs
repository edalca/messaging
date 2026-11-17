"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "emulator_console.js"), "utf8");
const scan = source.slice(source.indexOf("\tconst scanVisible ="), source.indexOf("\tconst syncEvidence ="));
const sendSlice = source.slice(source.indexOf("\tconst enqueueSend ="), source.indexOf("\tconst renderNotification ="));
const reconcileSlice = source.slice(source.indexOf("\tconst destroyNode ="), source.indexOf("\tconst updatePreferenceRow ="));
const statusAndInfoSlice = source.slice(source.indexOf("\tconst diagnosticLabel ="), source.indexOf("\tconst closeInfoPanel ="));

// The simulated receiver renders the canonical body without internal identity headings.
{
	const nodes = [];
	const render = vm.runInNewContext(
		source.slice(source.indexOf("\tconst renderMessage ="), source.indexOf("\tconst render =")) + "\nrenderMessage;",
		{
			$: markup => {
				const node = {markup, value: "", attr() { return this; }, data() { return this; },
					text(value) { this.value = value; return this; }, appendTo() { return this; }};
				nodes.push(node);
				return node;
			},
			getIncomingStatusIcons: () => [], getTransportStatusIcons: () => [],
			appendBubbleMeta() {}, diagnostics: false,
		}
	);
	for (const kind of ["Human", "Automatic", "Incoming"]) {
		nodes.length = 0;
		render({kind, agent: "Administrator", origin_label: "Agent", content: "Public body"});
		assert.deepEqual(nodes.filter(n => n.value).map(n => n.value), ["Public body"]);
		assert.ok(!nodes.some(n => n.markup.includes("emulator-message-sender")));
	}
}

async function observation() {
	let now = 1000, visible = true;
	const timers = [], calls = [], observations = new Map(), confirmedReads = new Set();
	const item = {source_type: "Bot Dispatch", name: "synthetic", transport: {delivery_status: "Delivered"}};
	const rescan = vm.runInNewContext(scan + "\nscanVisible(); scanVisible;", {
		evidenceItems: () => [item], canObserve: () => visible,
		observations, confirmedReads, evidenceEpoch: 1, key: () => "same-selection",
		evidenceArgs: () => ({name: item.name}), performance: {now: () => now},
		setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
		clearTimeout() {},
		call: async (method, args) => { calls.push({method, args}); return {receipt: "synthetic-proof"}; },
		$: () => ({find: () => ({text() {}})}), __: value => value,
	});
	await Promise.resolve();
	return {timers, calls, confirmedReads, item, observations, rescan,
		advance: value => { now = 1000 + value; }, hide: () => { visible = false; }};
}

async function optimisticSendAndRetry() {
	let identity = {account: "account-a", sender: "same-sender"}, draft = "First optimistic", serial = 0;
	let renderCount = 0;
	const calls = [], sending = new Set(), retries = new Map();
	const pendingMessages = new Map(), sendQueues = new Map();
	const compose = {val(value) { if (arguments.length) { draft = value; return this; } return draft; }, trigger() {}};
	const banner = {textVal: "", hidden: true, removeClass() { this.hidden = false; return this; }, addClass() { this.hidden = true; return this; }, text(v) { if (arguments.length) { this.textVal = v; return this; } return this.textVal; }};
	const context = {
		selection: () => ({...identity}),
		key: () => JSON.stringify([identity.account, identity.sender]),
		compose, sending, retries, pendingMessages, sendQueues,
		localSerial: 0,
		data: {messages: [], notifications: [], preference: {}},
		drafts: new Map(), enabled: true, generation: 0, busy: false,
		crypto: {randomUUID: () => "receipt-" + ++serial},
		setBusy() {}, resizeCompose() {}, applyLocalActivity() {}, renderContacts() {},
		render: () => { renderCount++; },
		refresh: async () => {}, banner, __: value => value,
		call: (method, args) => new Promise((resolve, reject) => calls.push({method, args, resolve, reject})),
	};
	vm.runInNewContext(sendSlice + "\nglobalThis.submit = submit; globalThis.retrySend = retrySend;", context);

	// 1. Optimistic item appears immediately with send_state="sending" and composer clears synchronously.
	const p1 = context.submit();
	const keyA = JSON.stringify(["account-a", "same-sender"]);
	assert.equal(draft, "", "Composer must clear synchronously on submit");
	assert.equal(renderCount, 1, "Thread must render immediately before HTTP resolves");
	assert.equal(pendingMessages.get(keyA).length, 1);
	assert.equal(pendingMessages.get(keyA)[0].send_state, "sending");

	// 2. Second message while first is in flight queues sequentially without blocking.
	draft = "Second queued";
	const p2 = context.submit();
	assert.equal(pendingMessages.get(keyA).length, 2);
	assert.equal(calls.length, 1, "Second HTTP request waits cleanly behind first for same conversation");

	// 3. First resolves and reconciles to persisted Chat Message without duplicates.
	calls[0].resolve({
		turn: "TRN-1",
		session: "SES-1",
		message: {name: "MSG-1", kind: "Incoming", content: "First optimistic", timeline_at: "2026-11-15T00:00:01.000000+00:00", processing: {state: "Pending", status: "Pending"}},
	});
	await new Promise(r => setImmediate(r));
	assert.equal(pendingMessages.get(keyA).length, 1, "First provisional removed upon reconciliation");
	assert.equal(context.data.messages.map(m => m.name).join(","), "MSG-1");
	assert.equal(calls.length, 2, "Second message starts automatically after first");

	// 4. Switching conversations isolates in-flight send and preserves failed message for retry.
	identity = {account: "account-b", sender: "same-sender"};
	context.generation++;
	context.data = {messages: [], notifications: [], preference: {}};
	draft = "Independent B";
	const p3 = context.submit();
	assert.equal(calls.length, 3, "Other account sends immediately without waiting for account-a");
	draft = "Unsent draft in B";

	calls[1].reject(new Error("Simulated network failure"));
	await p1; await p2;
	assert.equal(pendingMessages.get(keyA).length, 1);
	const failedItem = pendingMessages.get(keyA)[0];
	assert.equal(failedItem.send_state, "failed", "Failed message remains visible");
	assert.equal(draft, "Unsent draft in B", "Background failure must not wipe active conversation draft");
	assert.equal(banner.hidden, true, "Background failure must not show error banner in another conversation");

	calls[2].resolve({
		turn: "TRN-2", session: "SES-2",
		message: {name: "MSG-B", kind: "Incoming", content: "Independent B", timeline_at: "2026-11-15T00:00:02.000000+00:00"},
	});
	await p3;

	// 5. Switch back and retry failed item: preserves client_id and avoids duplicates.
	identity = {account: "account-a", sender: "same-sender"};
	context.generation++;
	context.data = {messages: [{name: "MSG-1", kind: "Incoming", content: "First optimistic", timeline_at: "2026-11-15T00:00:01.000000+00:00"}], notifications: [], preference: {}};
	const retryPromise = context.retrySend(failedItem);
	assert.equal(failedItem.send_state, "sending");
	assert.equal(calls[3].args.client_id, calls[1].args.client_id, "Retry preserves exact ingress client_id");
	await context.retrySend(failedItem); // Double-click while retrying must no-op.
	assert.equal(calls.length, 4);
	calls[3].resolve({
		turn: "TRN-3", session: "SES-1",
		message: {name: "MSG-2", kind: "Incoming", content: "Second queued", timeline_at: "2026-11-15T00:00:03.000000+00:00"},
	});
	await retryPromise;
	assert.equal(pendingMessages.has(keyA), false);
	assert.equal(context.data.messages.map(m => m.name).join(","), "MSG-1,MSG-2");

	// 6. Empty input does nothing.
	draft = "   ";
	await context.submit();
	assert.equal(calls.length, 4, "Empty messages never reach ingress");
}

(async () => {
	const early = await observation();
	assert.equal(early.timers[0].delay, 700);
	early.advance(699.2);
	await early.timers.shift().callback();
	assert.deepEqual(early.calls.map(c => c.method), ["observe_message"]);
	assert.equal(early.timers[0].delay, 1);
	early.advance(700);
	await early.timers.shift().callback();
	assert.deepEqual(early.calls.map(c => c.method), ["observe_message", "client_status"]);
	assert.equal(early.calls[1].args.receipt, "synthetic-proof");
	assert.equal(early.calls[1].args.status, "Read");
	assert.equal(early.item.transport.delivery_status, "Read");

	for (const invalidate of [o => o.hide(), o => o.observations.clear()]) {
		const cancelled = await observation();
		cancelled.advance(699);
		await cancelled.timers.shift().callback();
		invalidate(cancelled);
		cancelled.advance(701);
		await cancelled.timers.shift().callback();
		assert.deepEqual(cancelled.calls.map(c => c.method), ["observe_message"]);
		assert.equal(cancelled.confirmedReads.size, 0);
	}
	const replaced = await observation();
	replaced.item.element = {};
	replaced.rescan();
	await Promise.resolve();
	assert.equal(replaced.calls.filter(c => c.method === "observe_message").length, 2,
		"Replacing a rendered bubble starts a fresh dwell instead of retaining a dead observation");

	await optimisticSendAndRetry();
	reconcileAndRapidSelectTests();
	statusIconsAndInfoPanelTests();
	console.log("UI tests passed: 700ms read evidence, optimistic send, queued ordering, conversation isolation, idempotent retry, DOM reuse, rapid selection/cache, truthful status icons, and info panel metadata.");
})().catch(error => { console.error(error); process.exitCode = 1; });

function makeMiniDom() {
	function wrap(el) {
		const jq = {
			0: el,
			length: el ? 1 : 0,
			data(k, v) {
				if (typeof k === "object") { Object.assign(el._data, k); return jq; }
				if (arguments.length === 1) return el._data[k];
				el._data[k] = v;
				return jq;
			},
			replaceWith(otherJq) {
				const otherEl = otherJq[0];
				const parent = el.parentNode;
				const idx = parent.children.indexOf(el);
				parent.children.splice(idx, 1, otherEl);
				otherEl.parentNode = parent;
				el.parentNode = null;
				return jq;
			},
			insertAfter(prevJq) {
				const parent = prevJq[0].parentNode;
				if (el.parentNode) {
					const oldIdx = el.parentNode.children.indexOf(el);
					if (oldIdx >= 0) el.parentNode.children.splice(oldIdx, 1);
				}
				const idx = parent.children.indexOf(prevJq[0]);
				parent.children.splice(idx + 1, 0, el);
				el.parentNode = parent;
				return jq;
			},
			prepend(childJq) {
				const childEl = childJq[0];
				if (childEl.parentNode) {
					const oldIdx = childEl.parentNode.children.indexOf(childEl);
					if (oldIdx >= 0) childEl.parentNode.children.splice(oldIdx, 1);
				}
				el.children.unshift(childEl);
				childEl.parentNode = el;
				return jq;
			},
			remove() {
				if (el.parentNode) {
					const idx = el.parentNode.children.indexOf(el);
					if (idx >= 0) el.parentNode.children.splice(idx, 1);
					el.parentNode = null;
				}
				return jq;
			},
		};
		return jq;
	}
	function createNode(id) {
		const node = {
			id,
			parentNode: null,
			children: [],
			_data: {},
			get firstElementChild() { return this.children[0] || null; },
			get nextElementSibling() {
				if (!this.parentNode) return null;
				const i = this.parentNode.children.indexOf(this);
				return this.parentNode.children[i + 1] || null;
			},
		};
		return node;
	}
	const $ = el => wrap(el && el[0] ? el[0] : el);
	return {$, createNode};
}

function reconcileAndRapidSelectTests() {
	const {$, createNode} = makeMiniDom();
	const historyCache = new Map();
	const pendingMessages = new Map();
	const drafts = new Map();
	const calls = [];
	const aborted = [];
	let renderCalls = 0, emptyCalls = [];
	const root = {addClass() { return this; }};
	const compose = {val() { return ""; }};
	const historyEl = createNode("history");
	const historyTarget = $(historyEl);
	const context = {
		$, historyCache, HISTORY_CACHE_LIMIT: 20, pendingMessages, drafts,
		identityKey: c => JSON.stringify([c.account || "", c.sender_id || ""]),
		key: () => JSON.stringify([context.accountName || "", context.sender || ""]),
		sender: "", accountName: "", selectedContact: null, data: null,
		providerEnabled: true, accountRows: [{name: "ACC-1", enabled: 1}, {name: "ACC-2", enabled: 1}],
		generation: 0, busy: false, enabled: true, activeHistoryXhr: null,
		cancelObservations() {},
		compose, resizeCompose() {}, root, renderHeading() {}, renderContacts() {},
		setBusy() {}, fitHeight() {}, scanVisible() {},
		empty: (_el, msg) => { emptyCalls.push(msg); },
		render: () => { renderCalls++; },
		openHistory: (scroll, opts) => {
			const xhr = {abort() { aborted.push(calls.length); }};
			context.activeHistoryXhr = xhr;
			calls.push({sender: context.sender, account: context.accountName, gen: context.generation, scroll, opts});
		},
		history: historyTarget,
	};
	const api = vm.runInNewContext(
		reconcileSlice + "\n({reconcile, storeHistoryCache, selectContact, invalidateHistoryCacheByAccount});",
		context
	);

	// 1. reconcile DOM node reuse, reorder, selective replace, and cleanup
	let builds = 0, destroyed = [];
	const makeRow = item => {
		builds++;
		const node = createNode(item.key + ":" + item.signature);
		return $(node).data({cleanup: () => destroyed.push(node.id)});
	};
	api.reconcile(historyTarget, [
		{key: "a", signature: "1"},
		{key: "b", signature: "1"},
		{key: "c", signature: "1"},
	], makeRow);
	assert.equal(builds, 3);
	const nodeA = historyEl.children[0], nodeB = historyEl.children[1], nodeC = historyEl.children[2];

	// Reorder only -> zero rebuilds, exact node identity preserved
	api.reconcile(historyTarget, [
		{key: "c", signature: "1"},
		{key: "a", signature: "1"},
		{key: "b", signature: "1"},
	], makeRow);
	assert.equal(builds, 3);
	assert.deepEqual(historyEl.children, [nodeC, nodeA, nodeB]);

	// Change signature of "b" and remove "c" -> only "b" rebuilt, "b" and "c" cleaned up
	api.reconcile(historyTarget, [
		{key: "a", signature: "1"},
		{key: "b", signature: "2"},
	], makeRow);
	assert.equal(builds, 4);
	assert.equal(historyEl.children[0], nodeA);
	assert.notEqual(historyEl.children[1], nodeB);
	assert.deepEqual(destroyed.sort(), ["b:1", "c:1"]);

	// 2. Rapid selectContact aborts in-flight XHR and hydrates cache while honoring cleared_before
	api.selectContact({account: "ACC-1", sender_id: "001", enabled: 1});
	api.selectContact({account: "ACC-2", sender_id: "002", enabled: 1});
	assert.equal(aborted.length, 1, "Previous XHR aborted when rapidly switching conversations");
	assert.equal(calls.length, 2);
	assert.equal(context.sender, "002");
	assert.equal(context.generation, 2);

	api.storeHistoryCache(JSON.stringify(["ACC-1", "001"]), {
		enabled: true,
		messages: [{name: "M1", timeline_at: "2026-11-14 10:00:00"}, {name: "M2", timeline_at: "2026-11-14 10:05:00"}],
		notifications: [{name: "N1", timeline_at: "2026-11-14 10:02:00"}],
		preference: {},
	});
	api.selectContact({account: "ACC-1", sender_id: "001", enabled: 1, cleared_before: "2026-11-14 10:03:00"});
	assert.equal(context.data.messages.map(m => m.name).join(","), "M2");
	assert.equal(context.data.notifications.length, 0);
	assert.equal(Boolean(calls[2].opts && calls[2].opts.background), true);
}

function statusIconsAndInfoPanelTests() {
	const api = vm.runInNewContext(
		statusAndInfoSlice + "\n({getIncomingStatusIcons, getTransportStatusIcons, buildInfoSections});",
		{
			accountRows: [{name: "ACC-EMULR", account_id: "emu-1", account_name: "Emulator 1"}],
			accountName: "ACC-EMULR",
			accountLabel: a => a ? (a.account_name || a.account_id || a.name) : "",
			formatFullDate: () => "2026-11-15",
			formatLocalTime: () => "02:05",
			__: v => v,
		}
	);

	const norm = v => JSON.parse(JSON.stringify(v));

	// 1. Incoming status dimensions (must never fabricate processing success when diagnostic/blocked/error is present)
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({send_state: "sending"})).map(s => [s.icon, s.tone]),
		[["clock", "muted"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({send_state: "failed"})).map(s => [s.icon, s.tone]),
		[["circle-alert", "error"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Pending", status: "Pending"}})).map(s => [s.icon, s.tone]),
		[["check", "muted"], ["clock", "muted"]]
	);
	const running = norm(api.getIncomingStatusIcons({processing: {state: "Running", status: "Running"}}));
	assert.deepEqual(running.map(s => [s.icon, s.tone, Boolean(s.spinning)]), [["check", "muted", false], ["loader-circle", "muted", true]]);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Done", status: "Complete"}})).map(s => [s.icon, s.tone]),
		[["check-check", "muted"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Done", status: "Complete", diagnostic: "configuration_required"}})).map(s => [s.icon, s.tone]),
		[["check", "muted"], ["circle-alert", "error"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Blocked", status: "Recovery Required"}})).map(s => [s.icon, s.tone]),
		[["check", "muted"], ["circle-alert", "error"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Blocked", status: "Quota Blocked"}})).map(s => [s.icon, s.tone]),
		[["check", "muted"], ["circle-alert", "error"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Done", status: "Human Paused"}})).map(s => [s.icon, s.tone]),
		[["check", "muted"], ["clock", "muted"]]
	);
	assert.deepEqual(
		norm(api.getIncomingStatusIcons({processing: {state: "Done", status: "Ignored"}})).map(s => [s.icon, s.tone]),
		[["check", "muted"]]
	);

	// 2. Outgoing transport status dimensions (never show blue double-check without Read evidence)
	assert.deepEqual(
		norm(api.getTransportStatusIcons(null)).map(s => [s.icon, s.tone]),
		[["clock", "muted"]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Pending"})).map(s => [s.icon, s.tone]),
		[["clock", "muted"]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Sending"})).map(s => [s.icon, s.tone, Boolean(s.spinning)]),
		[["loader-circle", "muted", true]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Accepted", delivery_status: "Sent"})).map(s => [s.icon, s.tone]),
		[["check", "muted"]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Accepted", delivery_status: "Delivered"})).map(s => [s.icon, s.tone]),
		[["check-check", "muted"]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Accepted", delivery_status: "Read"})).map(s => [s.icon, s.tone]),
		[["check-check", "read"]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Accepted", delivery_status: "Failed"})).map(s => [s.icon, s.tone]),
		[["circle-alert", "error"]]
	);
	assert.deepEqual(
		norm(api.getTransportStatusIcons({status: "Rejected"})).map(s => [s.icon, s.tone]),
		[["circle-alert", "error"]]
	);

	// 3. Read-only info sections: conditional metadata, no empty values, no sensitive client tokens
	const incomingSections = api.buildInfoSections("message", {
		name: "MSG-001",
		kind: "Incoming",
		turn: "TURN-001",
		session: "SESS-001",
		timeline_at: "2026-11-15 02:05:00",
		client_id: "secret-client-id",
		processing: {state: "Done", status: "Complete"},
	});
	const incomingFlat = incomingSections.flat();
	assert.ok(incomingFlat.some(r => r.label === "Origin" && r.value === "Simulated subscriber"));
	assert.ok(incomingFlat.some(r => r.label === "Chat Turn" && r.value === "TURN-001"));
	assert.ok(incomingFlat.some(r => r.label === "Chat Session" && r.value === "SESS-001"));
	assert.ok(!incomingFlat.some(r => r.value === "secret-client-id"));
	assert.ok(!incomingFlat.some(r => r.label === "Bot Dispatch"));

	const provisionalSections = api.buildInfoSections("message", {
		name: "pending:1",
		kind: "Incoming",
		send_state: "sending",
		timeline_at: "2026-11-15 02:05:00",
	}).flat();
	assert.ok(!provisionalSections.some(r => String(r.value).startsWith("pending:")));
	assert.ok(provisionalSections.some(r => r.label === "Reception" && r.value === "Sending..."));

	const outgoingSections = api.buildInfoSections("message", {
		name: "MSG-002",
		kind: "Automatic",
		session: "SESS-001",
		dispatch: "DISP-001",
		client_receipt: "emu-DISP-001",
		timeline_at: "2026-11-15 02:05:05",
		transport: {
			status: "Accepted",
			delivery_status: "Failed",
			transport_reference: "emu-OUT-001",
			diagnostic_code: "provider_delivery_failed",
		},
	}).flat();
	assert.ok(outgoingSections.some(r => r.label === "Origin" && r.value === "Bot"));
	assert.ok(outgoingSections.some(r => r.label === "Acceptance" && r.value === "Accepted"));
	assert.ok(outgoingSections.some(r => r.label === "Delivery Status" && r.value === "Failed" && r.tone === "error"));
	assert.ok(outgoingSections.some(r => r.label === "Failure" && r.value === "Provider delivery failed" && r.tone === "error"));
	assert.ok(outgoingSections.some(r => r.label === "Transport Reference" && r.value === "emu-OUT-001"));
	assert.ok(!outgoingSections.some(r => r.value === "emu-DISP-001"));
	assert.ok(!outgoingSections.some(r => r.label === "Read status"));

	const notificationSections = api.buildInfoSections("notification", {
		name: "NOTIF-001",
		status: "Accepted",
		delivery_status: "Read",
		recipient: "cust-99",
		transport_reference: "emu-N-001",
		timeline_at: "2026-11-15 02:06:00",
	}).flat();
	assert.ok(notificationSections.some(r => r.label === "Origin" && r.value === "Simulated notification"));
	assert.ok(notificationSections.some(r => r.label === "Original Recipient" && r.value === "cust-99"));
	assert.ok(notificationSections.some(r => r.label === "Delivery Status" && r.value === "Delivered"));
	assert.ok(notificationSections.some(r => r.label === "Read status" && r.value === "Read" && r.tone === "read"));
	assert.ok(notificationSections.some(r => r.label === "Notification Queue" && r.value === "NOTIF-001"));
}
