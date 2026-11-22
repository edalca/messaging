"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

(async () => {
	let handlers, query, resolve, secondary, confirm, copied, reloads = 0, dirty = false, admin = true;
	const calls = [];
	const buttons = {};
	const groups = [];
	const controls = [];
	const warnings = [];
	const statusText = [];
	let evidence = {};
	let callbackUrl = "https://wa.example.com/api/method/messaging.whatsapp.webhook.handler?channel=fixture";
	const element = {
		empty: () => element,
		appendTo: () => element,
	};
	const frappe = {
		ui: {form: {on: (_doctype, methods) => { handlers = methods; }, make_control: control => controls.push(control)}},
		user: {has_role: () => admin},
		datetime: {str_to_user: value => value},
		call(options) {
			calls.push(options);
			if (options.method.endsWith("channel_providers")) return new Promise(done => { resolve = done; });
			if (options.method.endsWith("get_webhook_url")) return Promise.resolve({message: {url: callbackUrl, evidence}});
			return Promise.resolve({message: {token: "synthetic"}});
		},
		confirm: (message, callback) => { confirm = {message, callback}; },
		utils: {copy_to_clipboard: token => { copied = token; }},
		require: async () => {},
		msgprint: message => warnings.push(message),
		throw(message) { throw new Error(message); },
	};
	const parameters = {render: async () => {}, flush() {}};
	vm.runInNewContext(fs.readFileSync(path.join(__dirname, "communication_channel.js"), "utf8"), {
		frappe, messaging: {provider_parameters: parameters}, __: value => value,
	});
	const frm = {
		doc: {name: "fixture", status: "Disabled", provider: "EMULR"}, perm: [{write: 1}],
		is_new: () => true, is_dirty: () => dirty, set_query: (_field, callback) => { query = callback; },
		fields_dict: {meta_webhook_details: {$wrapper: element}},
		add_custom_button: (label, click, group) => { buttons[label] = click; groups.push(group); },
		set_df_property() {}, page: {set_secondary_action: (label, callback) => { secondary = {label, callback}; }},
		reload_doc: async () => { reloads++; },
		set_value: async (field, value) => { frm.doc[field] = value; },
	};
	frm.doc.channel_name = "  Atencio\u0301n   Ñandú Ünico  ";
	await handlers.validate(frm);
	assert.equal(frm.doc.channel_name, "Atención Ñandú Ünico");
	assert.equal(frm.doc.name, "fixture");
	for (const value of ["", "   ", "Phone 2", "Manual_ID", "A-B", "Phone 📞", "Two\twords", "Channel\n"]) {
		frm.doc.channel_name = value;
		await assert.rejects(handlers.validate(frm), /Channel Name/);
	}
	const refreshing = handlers.refresh(frm);
	assert.equal(query().filters.status, "Enabled");
	assert.equal(query().filters.name[1].length, 0, "The selector stays restricted while discovery is pending");
	resolve({message: [{id: "OTHER1", available: false}, {id: "OTHER2", available: true}]});
	await refreshing;
	assert.equal(JSON.stringify(query().filters.name), JSON.stringify(["in", ["OTHER1", "OTHER2"]]),
		"Enabled provider discovery is independent of a hardcoded ID or technical availability");
	frm.is_new = () => false;
	await handlers.refresh(frm);
	assert.equal(calls.length, 1, "Editing an existing channel does not replace its immutable provider");
	assert.equal(secondary.label, "Enable");
	dirty = true;
	await secondary.callback();
	assert.equal(calls.length, 1);
	dirty = false;
	await secondary.callback();
	assert.equal(calls.at(-1).type, "POST");
	assert.equal(calls.at(-1).args.status, "Enabled");
	assert.equal(reloads, 1);
	frm.doc.status = "Enabled";
	await handlers.refresh(frm);
	assert.equal(secondary.label, "Disable");
	await secondary.callback();
	assert.equal(calls.at(-1).args.status, "Disabled");
	assert.equal(reloads, 2);

	const appRoot = path.resolve(__dirname, "../../..");
	vm.runInNewContext(fs.readFileSync(path.join(appRoot, "public/js/whatsapp_channel.js"), "utf8"), {
		frappe, __: value => value, $: (_tag, attrs) => {
			if (attrs.role === "status") warnings.push(attrs.text);
			else if (attrs.text) statusText.push(attrs.text);
			return element;
		},
	});
	await handlers.refresh(frm);
	assert.equal(Object.keys(buttons).length, 0, "EMULR has no WhatsApp token actions");
	assert.equal(controls.length, 0, "EMULR has no callback controls");
	frm.doc.provider = "WHATSAPP";
	admin = false;
	await handlers.refresh(frm);
	assert.equal(Object.keys(buttons).length, 0, "Non-administrators have no token actions");
	admin = true;
	await handlers.refresh(frm);
	assert.equal(calls.at(-1).type, "GET");
	assert.equal(calls.at(-1).args.channel, frm.doc.name);
	assert.equal(controls[0].doc.webhook_callback_url, callbackUrl);
	assert.equal(controls[0].df.label, "Webhook URL");
	assert.equal(controls[0].df.read_only, 1);
	assert.equal(controls.length, 1, "The webhook section has no action controls");
	assert.deepEqual(statusText, ["Webhook Status", "URL Verification", "Unchecked", "Event Reception", "Unchecked"]);
	buttons["Copy webhook URL"]();
	assert.equal(copied, callbackUrl);
	assert.equal(frm.doc.webhook_callback_url, undefined);
	assert.equal(frm.is_dirty(), false);
	assert.deepEqual(Object.keys(buttons), ["Copy webhook URL", "Copy verification token", "Generate verification token"]);
	assert.deepEqual(groups, ["Webhook Actions", "Webhook Actions", "Webhook Actions"]);
	frm.doc.modified = "2026-11-17 12:00:00.000001";
	await buttons["Generate verification token"]();
	assert.match(calls.at(-1).method, /generate_webhook_token$/);
	assert.equal(calls.at(-1).type, "POST");
	assert.equal(calls.at(-1).args.expected_modified, frm.doc.modified);
	for (const label of Object.keys(buttons)) delete buttons[label];
	evidence = {
		get: {received_at: "2026-11-21 14:37:18", token_valid: true, challenge_answered: true, http_status: 200},
		post: {received_at: "2026-11-21 14:38:19", hmac_valid: true, event_type: "messages",
			routing: "routed", processing: "pending"},
	};
	statusText.length = 0;
	frm.doc.parameters = [{parameter_key: "verify_token", secret_value: "********"}];
	callbackUrl = null;
	await handlers.refresh(frm);
	assert.match(warnings.at(-1), /public HTTPS URL/);
	assert.ok(statusText.includes("Last challenge answered successfully"));
	assert.ok(statusText.includes(evidence.get.received_at));
	assert.ok(statusText.includes("HTTP 200"));
	assert.ok(statusText.includes("Signed webhook received"));
	assert.ok(statusText.includes(evidence.post.received_at));
	assert.ok(statusText.includes("HMAC signature"));
	assert.ok(statusText.includes("Routed to an account"));
	assert.ok(statusText.includes("Pending worker processing"));
	assert.ok(!statusText.includes("Message processed"));
	assert.equal(controls.length, 1, "A local site does not get a callback control");
	assert.deepEqual(Object.keys(buttons), ["Copy webhook URL", "Copy verification token", "Regenerate verification token"]);
	await buttons["Copy verification token"]();
	assert.equal(copied, "synthetic");
	assert.equal(frm.doc.token, undefined);
	const requests = calls.length;
	await buttons["Regenerate verification token"]();
	assert.equal(calls.length, requests, "Regeneration waits for confirmation");
	assert.match(confirm.message, /update it in Meta Developers/);
	await confirm.callback();
	assert.match(calls.at(-1).method, /regenerate_webhook_token$/);
	assert.equal(calls.at(-1).type, "POST");
	assert.equal(reloads, 4);
	const schema = JSON.parse(fs.readFileSync(path.join(__dirname, "communication_channel.json"), "utf8"));
	assert.deepEqual(schema.fields.filter(field => field.fieldtype === "Tab Break").map(field => field.label),
		["General", "Public Information"]);
	console.log("Channel selector, status action, webhook evidence, and token menu passed.");
})().catch(error => { console.error(error); process.exitCode = 1; });
