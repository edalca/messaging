"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

(async () => {
	let handlers, query, resolve, calls = 0;
	const frappe = {ui: {form: {on: (_doctype, methods) => { handlers = methods; }}},
		call: () => { calls++; return new Promise(done => { resolve = done; }); }, require: async () => {}};
	const parameters = {render: async () => {}};
	vm.runInNewContext(fs.readFileSync(path.join(__dirname, "communication_channel.js"), "utf8"), {
		frappe, messaging: {provider_parameters: parameters}, window: {},
	});
	const frm = {is_new: () => true, set_query: (_field, callback) => { query = callback; }, set_df_property() {}};
	const refreshing = handlers.refresh(frm);
	assert.equal(query().filters.enabled, 1);
	assert.equal(query().filters.name[1].length, 0, "The selector stays restricted while discovery is pending");
	resolve({message: [{id: "OTHER1", available: false}, {id: "OTHER2", available: true}]});
	await refreshing;
	assert.equal(JSON.stringify(query().filters.name), JSON.stringify(["in", ["OTHER1", "OTHER2"]]),
		"Enabled provider discovery is independent of a hardcoded ID or technical availability");
	frm.is_new = () => false;
	await handlers.refresh(frm);
	assert.equal(calls, 1, "Editing an existing channel does not replace its immutable provider");
	console.log("Channel selector uses enabled discovery, accepts other providers and stays restricted during loading.");
})().catch(error => { console.error(error); process.exitCode = 1; });
