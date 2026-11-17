/* Focused async regression checks over the actual Desk controller. Run with node. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const handlers = {};
const requests = [];
const messages = [];
const rendered = [];
const settings = {chatbot_languages: [{language: 'es', enabled: 1, is_default: 1}, {language: 'en', enabled: 1}]};
const shared = {frappe: {provide() {}}, messaging: {translation_cards: {}}, __: s => s};
vm.runInNewContext(fs.readFileSync(__dirname + '/../../../public/js/translation_cards.js', 'utf8'), shared);
const messageKeys = ['informative_message', 'informative_categories_header', 'attempts_exhausted_message',
    'default_internal_error_message', 'quota_courtesy_message', 'human_disabled_message', 'human_active_message',
    'internal_error_message', 'input_initial_message', 'input_continuation_message', 'semantic_description', 'menu_label', 'process_message'];
assert(messageKeys.every(key => shared.messaging.translation_cards.help(key)));
assert.equal(new Set(messageKeys.map(key => shared.messaging.translation_cards.help(key))).size, messageKeys.length);
const frappe = {
    ui: {form: {on: (dt, events) => {
        handlers[dt] ||= {};
        for (const [key, value] of Object.entries(events)) (handlers[dt][key] ||= []).push(value);
    }}},
    call: args => args.method.endsWith('.message_requirements') ? Promise.resolve({message: ['semantic_description',
        ...((JSON.parse(args.args.candidate).parameters || []).length ? ['input_initial_message', 'input_continuation_message'] : [])]})
        : new Promise((resolve, reject) => requests.push({args, resolve, reject})),
    db: {get_doc: async () => settings},
    require: async () => {},
    msgprint: value => messages.push(value),
    throw: value => {throw Error(value);},
};
vm.runInNewContext(fs.readFileSync(__dirname + '/bot_flow.js', 'utf8'), {frappe, messaging: {translation_cards: {render: async (_frm, config) => {rendered.push(config);}}}, __: s => s, console});
const change = handlers['Bot Flow'].process_definition[0];
const beforeSave = handlers['Bot Flow'].before_save[0];
function form() {
    return {
        doc: {name: 'draft', process_definition: 'old', parameters: [{parameter: 'old'}], outputs: [{status_code: 'old'}],
            output_translations: [{status_code: 'old'}], process_method: 'old.handler', process_contract: 'old', priority: 1},
        fields_dict: {},
        page: {set_indicator() {}},
        clear_table(table) {this.doc[table] = [];},
        add_child(table, row) {const child = {name: table + this.doc[table].length, parent: this.doc.name, ...row}; this.doc[table].push(child); return child;},
        refresh_fields() {}, refresh_field() {}, dirty() {}, toggle_display(names, visible) {
            this.display = {names, visible};
            this.visibility ||= {};
            for (const name of names) this.visibility[name] = visible;
        },
        set_value(key, value) {this.doc[key] = value;},
    };
}
function result(definition, inputs = ['input'], outputs = ['success']) {
    return {message: {definition, parameters: inputs.map(process_argument => ({process_argument, parent: 'candidate-other-document'})),
        outputs: outputs.map(status_code => ({status_code})), process_method: definition + '.handler',
        process_contract: JSON.stringify({definition}), process_contract_fingerprint: definition + '-fingerprint'}};
}
// A Process that declares Flow captures can add ordinary Parameter rows.
{
    const context = {frappe: {...frappe, ui: {form: {on() {}}}}, __: s => s};
    vm.runInNewContext(fs.readFileSync(__dirname + '/bot_flow.js', 'utf8'), context);
    const frm = form();
    frm.fields_dict.parameters = {grid: {cannot_add_rows: true}};
    frm.doc.process_contract = JSON.stringify({configurable_parameters: true});
    context.sync_flow_parameter_state(frm);
    assert.equal(frm.fields_dict.parameters.grid.cannot_add_rows, false);
    frm.doc.process_contract = JSON.stringify({inputs: []});
    context.sync_flow_parameter_state(frm);
    assert.equal(frm.fields_dict.parameters.grid.cannot_add_rows, true);
    frm.doc.input_translations = [{language: 'es', input_initial_message: 'Keep start', input_continuation_message: 'Keep next'}];
    const retained = JSON.stringify(frm.doc.input_translations);
    frm.doc.parameters = []; frm.doc.process_contract = '{}'; frm.doc.priority = 0;
    context.sync_flow_parameter_state(frm);
    assert.equal(frm.visibility.initial_message_card, false);
    assert.equal(frm.visibility.continuation_message_card, false);
    frm.doc.process_contract = JSON.stringify({parameters: [{name: 'required_capture'}]});
    context.sync_flow_parameter_state(frm);
    assert.equal(frm.visibility.initial_message_card, true);
    frm.doc.parameters = [{parameter: 'capture'}]; frm.doc.priority = 0;
    context.sync_flow_parameter_state(frm);
    assert.equal(frm.visibility.continuation_message_card, true);
    assert.equal(frm.doc.priority, 1);
    assert.equal(JSON.stringify(frm.doc.input_translations), retained);
}
(async () => {
    const frm = form();
    if (process.argv.includes('--general')) {
        const validate = handlers['Bot Flow'].validate[0];
        const toggle = handlers['Bot Flow'].show_in_menu[0];
        frm.doc.translations = [{language: 'es', semantic_description: 'Meaning'}, {language: 'en', menu_label: 'Secondary'}];
        frm.doc.show_in_menu = 1;
        for (const label of [undefined, '', '   ']) {
            frm.doc.translations[0].menu_label = label;
            await assert.rejects(validate(frm), /Menu Label is required in the default enabled language/);
        }
        frm.doc.show_in_menu = 0;
        await validate(frm);
        frm.doc.translations[0].menu_label = 'Default';
        frm.doc.menu_order = 37;
        const values = JSON.stringify(frm.doc);
        const hiding = toggle(frm);
        assert.equal(frm.display.visible, false);
        await hiding;
        assert.equal(JSON.stringify(frm.doc), values);
        frm.doc.show_in_menu = 1;
        const showing = toggle(frm);
        assert.equal(frm.display.visible, true);
        await showing;
        await validate(frm);
        assert.equal(frm.doc.menu_order, 37);
        assert.equal(frm.doc.translations[0].menu_label, 'Default');
        const editors = rendered.slice(-5);
        assert.deepEqual(editors.map(editor => editor.id).join(','), 'semantic,initial,continuation,menu,internal_error_message');
        assert.equal(editors[3].fields.join(','), 'menu_label,menu_description');
        assert(editors.every(editor => editor.fields.every(field => typeof field === 'string')));
        assert.equal(editors[3].requiredFields.join(','), 'menu_label');
        assert.equal(editors[3].visible, true);
        assert.equal(editors[0].requiredFields.join(','), 'semantic_description');
        frm.doc.show_in_menu = 0;
        await toggle(frm);
        assert.equal(rendered.at(-2).requiredFields.length, 0);
        assert.equal(rendered.at(-2).visible, false);
        assert.equal(rendered.at(-5).visible, undefined);
        assert.equal(editors[4].table, 'translations');
        assert.equal(editors[4].fields.join(','), 'internal_error_message');
        const original = JSON.stringify(frm.doc);
        await handlers['Bot Flow'].use_custom_internal_error_message[0](frm);
        assert.equal(JSON.stringify(frm.doc), original);
        frm.doc.use_custom_internal_error_message = 1;
        await handlers['Bot Flow'].use_custom_internal_error_message[0](frm);
        assert.equal(rendered.at(-1).visible, true);
        console.log('PASS: independent localized HTML fields, metadata field references, immediate visibility, default-language validation and unchanged values.');
        return;
    }
    frm.doc.process_definition = 'A'; const a1 = change(frm);
    assert.equal(frm.doc.parameters.length, 0); assert.equal(frm.doc.outputs.length, 0);
    assert.equal(frm.doc.output_translations.length, 0); assert.equal(frm.doc.process_method, '');
    assert.equal(frm.doc.process_test_status, 'Untested');
    assert.throws(() => beforeSave(frm), /successful Process discovery/);
    frm.doc.process_definition = 'B'; const b = change(frm);
    frm.doc.process_definition = 'A'; const a2 = change(frm);
    requests[2].resolve(result('A', [], ['updated'])); await a2;
    requests[1].resolve(result('B')); await b;
    requests[0].resolve(result('A', ['obsolete'], ['obsolete'])); await a1;
    assert.equal(frm.doc.process_method, 'A.handler');
    assert.equal(frm.doc.parameters.length, 0);
    assert.equal(frm.doc.outputs[0].status_code, 'updated');
    beforeSave(frm);
    frm.doc.process_definition = 'broken'; const broken = change(frm);
    requests[3].reject(Error('Discovery failed')); await broken;
    assert.equal(frm.doc.outputs.length, 0); assert.equal(frm.doc.process_method, '');
    assert.equal(messages.length, 1); assert.throws(() => beforeSave(frm));
    frm.doc.process_definition = 'restored'; const restored = change(frm);
    requests[4].resolve(result('restored')); await restored;
    beforeSave(frm);
    assert.equal(frm.doc.parameters[0].parent, 'draft');
    frm.doc.parameters[0].parameter = 'manual-binding';
    await change(frm); // An event with the same definition must preserve manual edits.
    assert.equal(frm.doc.parameters[0].parameter, 'manual-binding');
    frm.doc.process_definition = 'pending'; const pending = change(frm);
    frm.doc = {...form().doc, process_definition: 'reloaded'};
    requests[5].resolve(result('pending')); await pending;
    assert.equal(frm.doc.process_method, 'old.handler');
    console.log('PASS: immediate invalidation, A/B/A ordering, empty inputs, replacement outputs, failure, save guard, manual edits and document identity.');
})().catch(error => {console.error(error); process.exitCode = 1;});

// Both owners enforce the same per-row ordered mode contract in their Desk controllers.
for (const [filename, apply, table] of [
    [__dirname + '/bot_flow.js', 'apply_flow_outcome_contract', 'outputs'],
]) {
    const context = {frappe: {...frappe, provide() {}, ui: {form: {on() {}}}},
        messaging: {notification_conditions: {}}, __: s => s};
    vm.runInNewContext(fs.readFileSync(filename, 'utf8'), context);
    for (const modes of [['message'], ['template'], ['message', 'template'], ['template', 'message']]) {
        const edits = {};
        const control = {df: {}, refresh() {}};
        const modeField = {fieldname: 'output_mode'};
        const row = {grid: {df: {fieldname: table}}, doc: {status_code: 'done'}, docfields: [modeField],
            toggle_editable(field, editable) {edits[field] = editable;}, grid_form: {fields_dict: {output_mode: control}}};
        const frm = {doc: {process_contract: JSON.stringify({outcomes: {done: {mode: modes}}})}};
        context[apply](frm, row);
        assert.equal(modeField.options, modes.map(v => v[0].toUpperCase() + v.slice(1)).join('\n'));
        assert.equal(edits.output_mode, modes.length === 2);
        assert.equal(control.df.read_only, modes.length === 1);
        if (apply === 'apply_flow_outcome_contract') assert.equal(edits.flow_action, false);
    }
}
console.log('PASS: ordered single/dual modes, per-row editability, fixed Flow action in Bot Flow.');

(async function lifecycle() {
    const calls = [], notifications = [];
    const button = {prop() {return this;}, attr() {return this;}, text() {return this;}, remove() {}};
    const wrapper = {off() {return this;}, on() {return this;}};
    let action;
    const frm = {doc: {doctype: 'Bot Flow', name: 'saved', modified: 'before', process_definition: 'definition',
            parameters: [], process_contract: '{}', __onload: {flow_lifecycle: {lifecycle_status: 'Untested'}}},
        doctype: 'Bot Flow', fields_dict: {}, $wrapper: wrapper,
        page: {set_primary_action() {}, set_indicator() {}, menu: {find: () => ({closest: () => ({remove() {}})})},
            add_button(_label, callback) {action = callback; return button;}},
        is_new: () => false, is_dirty: () => false, refresh_field() {}, set_intro() {},
        async refresh() {await context.refresh_flow_lifecycle(this);}};
    const api = {...frappe, get_route_str: () => 'Form/Bot Flow/saved', user: {has_role: () => true},
        utils: {escape_html: value => value}, msgprint: value => notifications.push(value),
        model: {remove_from_locals() {}, sync(response) {frm.doc = response.docs[0];}},
        call: async () => ({message: {process_contract: '{}'}})};
    const context = {frappe: api, __: s => s, $: {ajax: async request => {
        calls.push(request.url);
        if (request.url.endsWith('validate_process')) return {message: {status: 'Passed', lifecycle_status: 'Enabled'}};
        if (request.url.endsWith('getdoc')) return {docs: [{doctype: 'Bot Flow', name: 'saved', modified: 'after',
            __onload: {flow_lifecycle: {lifecycle_status: 'Enabled'}}, status: 'Enabled'}]};
        throw Error('Unexpected repeated state request');
    }}};
    vm.runInNewContext(fs.readFileSync(__dirname + '/bot_flow.js', 'utf8'), context);
    await context.refresh_flow_lifecycle(frm);
    assert.equal(calls.length, 0);
    await action();
    assert.equal(frm.doc.status, 'Enabled');
    assert.equal(calls.length, 2);
    assert(notifications.some(value => value.message?.includes('validated and enabled')));
    assert.equal(frm.flow_lifecycle_operation, null);
    console.log('PASS: load evidence reuse, successful tester enables, document reload and no repeated state request.');
})().catch(error => {console.error(error); process.exitCode = 1;});
