const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const handlers = {}, requests = [], locals = {};
const frappe = {
    ui: {form: {on(dt, events) {
        handlers[dt] ||= {};
        for (const [key, value] of Object.entries(events)) (handlers[dt][key] ||= []).push(value);
    }}},
    call: args => new Promise((resolve, reject) => requests.push({args, resolve, reject})),
    utils: {escape_html: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('\"', '&quot;')},
    show_alert() {}, throw(message) {throw Error(message);},
};
const context = {frappe, locals, console: {error() {}}, $: {ajax: args => new Promise((resolve, reject) => requests.push({args, resolve, reject}))},
    __: (s, args = []) => s.replace(/\{(\d+)\}/g, (_, i) => args[i])};
vm.runInNewContext(fs.readFileSync(__dirname + '/bot_settings.js', 'utf8'), context);
function form(consumer = 'greeting') {
    const metadata = JSON.parse(fs.readFileSync(__dirname + '/bot_settings.json', 'utf8'));
    const frm = {doc: {doctype: 'Bot Settings', name: 'Bot Settings', human_inactivity_minutes: 5,
        registration_enabled: 0, greeting_enabled: 0, human_attention_enabled: 0},
        fields_dict: Object.fromEntries(metadata.fields.map(df => [df.fieldname, {df: {...df}}])),
        clear_table(table) {this.doc[table] = [];},
        add_child(table, row) {this.doc[table].push({...row});}, refresh_field() {}, refresh_fields() {}, dirty() {},
        set_intro(value, color) {this.intro = value; this.color = color;},
        set_df_property(field, property, value) {this.fields_dict[field].df[property] = value;},
    };
    const config = {registration: ['registration_enabled', 'registration_states'],
        greeting: ['greeting_enabled', 'greeting_states'], human: ['human_attention_enabled', 'human_transfer_states']}[consumer];
    frm.doc[config[0]] = 1; frm.doc[consumer + '_flow'] = consumer;
    frm.doc[config[1]] = [{state: 'success'}];
    return frm;
}
const beforeSave = handlers['Bot Settings'].before_save[0];
const response = (request, state = 'operational', issues = [], fields = []) => {
    const consumer = request.args.data.consumer;
    const doc = JSON.parse(request.args.data.candidate);
    return {message: {consumer, label: {registration: 'Registration', greeting: 'Greeting', human: 'Human Support'}[consumer],
        flow: doc[consumer + '_flow'], state, issues, catalog: {fields, states: []}}};
};
const issue = (field, message = 'Selected state is no longer Complete Flow.') =>
    ({field, message, action: 'Select current Complete Flow states or disable this feature.', severity: 'error'});
const tick = () => new Promise(resolve => setImmediate(resolve));
async function complete(promise, state = 'operational', issues = [], fields = []) {
    requests.at(-1).resolve(response(requests.at(-1), state, issues, fields));
    await promise;
}
// Native controls keep their own presentation, while requests follow the current
// document, selected Flow and input order rather than a stale Link text cache.
async function selectorRaces() {
    const pending = [], events = {};
    const input = {cache: {stale: true}, off() {return this;}, on(name, fn) {events[name] = fn; return this;}, val() {return 'Selected Flow';}};
    const control = {$input: input, df: {}, awesomplete: {list: []},
        get_search_args(txt) {return {txt, filters: {flow: frm.doc.greeting_flow}, page_length: 10};}, merge_duplicates: rows => rows};
    const frm = {doc: {greeting_flow: 'A'}, fields_dict: {greeting_states: control}};
    const context = {frappe: {...frappe, model: {can_create: () => true}, utils: {debounce: fn => fn, add_link_title() {}}, call: args => new Promise(resolve => pending.push({args, resolve}))}, __: s => s};
    vm.runInNewContext(fs.readFileSync(__dirname + '/bot_settings.js', 'utf8'), context);
    context.configure_consumer_selector(frm, 'greeting_states', 'greeting');
    assert.equal(control.df.only_select, true);
    const first = control.on_input({target: {value: ''}});
    frm.doc.greeting_flow = 'B'; const second = control.on_input({target: {value: ''}});
    pending[1].resolve({message: [{value: 'B_complete'}]}); await second;
    pending[0].resolve({message: [{value: 'A_obsolete'}]}); await first;
    assert.equal(control.awesomplete.list[0].value, 'B_complete');
    const oldSearch = control.on_input({target: {value: 'old'}});
    const newSearch = control.on_input({target: {value: 'new'}});
    pending[3].resolve({message: [{value: 'new'}]}); await newSearch;
    pending[2].resolve({message: [{value: 'old'}]}); await oldSearch;
    assert.equal(control.awesomplete.list[0].value, 'new');
    events['focus.flow_options']();
    assert.equal(pending.at(-1).args.args.txt, '');
    assert.equal(pending.at(-1).args.args.page_length, 10);
    pending.at(-1).resolve({message: []});
    control.df.only_select = false;
    const paginated = control.on_input({target: {value: ''}});
    pending.at(-1).resolve({message: [{value: 'eligible'}]}); await paginated;
    assert.equal(control.awesomplete.list.at(-1).value, 'advanced_search__link_option');
    assert.equal(control.awesomplete.list.at(-2).value, 'create_new__link_option');
}
// The native weekly schedule has no effect on custom Flows and survives selection changes.
{
    const metadata = JSON.parse(fs.readFileSync(__dirname + '/bot_settings.json', 'utf8'));
    const field = metadata.fields.find(item => item.fieldname === 'human_support_week');
    const section = metadata.fields.find(item => item.fieldname === 'human_schedule_section');
    assert.equal(field.depends_on, field.mandatory_depends_on);
    assert.equal(field.depends_on, section.depends_on);
    const native = 'messaging_human_availability';
    for (const [enabled, flow, expected] of [[0, native, false], [1, native, true], [1, 'custom_flow', false], [1, native + '_copy', false]]) {
        const doc = {human_attention_enabled: enabled, human_flow: flow};
        assert.equal(Boolean(vm.runInNewContext(field.depends_on.slice(5), {doc})), expected);
    }
    const events = {};
    const context = {frappe: {...frappe, ui: {form: {on(dt, value) {events[dt] = value;}}}}, __: s => s};
    vm.runInNewContext(fs.readFileSync(__dirname + '/bot_settings.js', 'utf8'), context);
    const frm = {doc: {human_attention_enabled: 1, human_flow: native, human_support_week: []},
        add_child(table, row) {this.doc[table].push(row);}, refresh_field() {}, dirty() {}};
    context.prepare_human_schedule(frm);
    assert.equal(frm.doc.human_support_week.length, 7);
    assert.equal(frm.doc.human_support_week[0].weekday, 'Monday');
    frm.doc.human_support_week[0] = {...frm.doc.human_support_week[0], enabled: 1, start_time: '09:00:00', end_time: '17:00:00'};
    const saved = JSON.stringify(frm.doc.human_support_week);
    frm.doc.human_flow = 'custom_flow'; context.prepare_human_schedule(frm);
    assert.equal(JSON.stringify(frm.doc.human_support_week), saved);
    frm.doc.human_flow = native; context.prepare_human_schedule(frm);
    assert.equal(JSON.stringify(frm.doc.human_support_week), saved);
    frm.doc.human_support_week = []; frm.doc.human_flow = 'custom_flow'; context.prepare_human_schedule(frm);
    assert.equal(frm.doc.human_support_week.length, 0);
}
// Each global message owns an independent HTML field and delegates child metadata to the shared editor.
async function localizedMessageFields() {
    const events = {}, rendered = [];
    const api = {...frappe,
        ui: {form: {on(doctype, handlers) {
            events[doctype] ||= {};
            for (const [event, handler] of Object.entries(handlers)) (events[doctype][event] ||= []).push(handler);
        }}},
        require: async () => {}, call: async () => ({message: ['informative_message']}),
    };
    const context = {frappe: api, __: value => value,
        messaging: {translation_cards: {render: async (_frm, config) => rendered.push(config)}}};
    vm.runInNewContext(fs.readFileSync(__dirname + '/bot_settings.js', 'utf8'), context);
    const metadata = JSON.parse(fs.readFileSync(__dirname + '/bot_settings.json', 'utf8'));
    const frm = {doc: {doctype: 'Bot Settings', chatbot_languages: [{language: 'es', enabled: 1, is_default: 1}],
        human_attention_enabled: 1, message_translations: []},
        fields_dict: Object.fromEntries(metadata.fields.map(df => [df.fieldname, {df, $wrapper: {}}]))};
    await events['Bot Settings'].refresh[0](frm);
    assert.equal(rendered.length, 8);
    assert.equal(new Set(rendered.map(config => config.wrapper)).size, 8);
    assert.equal(new Set(rendered.map(config => config.identity.message_key)).size, 8);
    for (const config of rendered) {
        assert.equal(config.table, 'message_translations');
        assert.equal(config.id, config.identity.message_key);
        assert.equal(config.wrapper, frm.fields_dict[config.wrapperField].$wrapper);
        assert.equal(config.fields, undefined);
        assert.equal(config.cards, undefined);
    }
    assert.equal(rendered.find(config => config.id === 'informative_message').requiredFields.join(','), 'message');
    frm.doc.human_attention_enabled = 0;
    await events['Bot Settings'].refresh[0](frm);
    assert.equal(rendered.at(-1).visible, false);
    assert.equal(frm.doc.message_translations.length, 0);
    console.log('PASS: eight independent localized HTML fields, metadata delegation, required hints and conditional visibility without row creation.');
}
async function diagnostics() {
    let frm = form();
    const pending = context.refresh_consumer(frm, 'greeting');
    const request = requests.at(-1);
    assert.equal(request.args.timeout, 20000);
    assert.equal(frm.intro, '');
    assert.ok(!frm.fields_dict.greeting_flow.df.description.includes('Checking'));
    let saved = false;
    const save = beforeSave(frm).then(() => {saved = true;});
    await tick(); assert.equal(saved, false);
    request.resolve(response(request)); await pending; await save;
    assert.equal(saved, true); assert.equal(frm.intro, '');
    assert.match(frm.fields_dict.greeting_flow.df.description, /Operational/);
    // The original preference changes no Flow, state, signature, or existing diagnosis.
    const flowAndStates = JSON.stringify([frm.doc.greeting_flow, frm.doc.greeting_states]);
    for (const value of [0, 1]) {
        const signature = context.consumer_signature(frm, 'greeting');
        frm.doc.show_menu_on_greeting_only = value;
        assert.equal(context.consumer_signature(frm, 'greeting'), signature);
        assert.equal(frm.consumer_diagnostics.greeting.state, 'operational');
        const count = requests.length;
        await beforeSave(frm);
        assert.equal(requests.length, count);
        assert.equal(JSON.stringify([frm.doc.greeting_flow, frm.doc.greeting_states]), flowAndStates);
    }
    // A/B/A and replaced-document responses cannot overwrite the latest diagnosis.
    frm.doc.greeting_flow = 'A'; const first = context.refresh_consumer(frm, 'greeting'); const a1 = requests.at(-1);
    frm.doc.greeting_flow = 'B'; const second = context.refresh_consumer(frm, 'greeting'); const b = requests.at(-1);
    frm.doc.greeting_flow = 'A'; const third = context.refresh_consumer(frm, 'greeting'); const a2 = requests.at(-1);
    a2.resolve(response(a2)); await third;
    b.resolve(response(b, 'technical_required', [issue('greeting_flow', 'Obsolete B')])); await second;
    a1.resolve(response(a1, 'unavailable', [issue('greeting_flow', 'Obsolete A')])); await first;
    assert.equal(frm.consumer_diagnostics.greeting.state, 'operational');
    const old = context.refresh_consumer(frm, 'greeting', false, false, true); const oldRequest = requests.at(-1);
    frm.doc = {...frm.doc};
    const newDoc = context.refresh_consumer(frm, 'greeting'); const newRequest = requests.at(-1);
    newRequest.resolve(response(newRequest)); await newDoc;
    oldRequest.resolve(response(oldRequest, 'internal_error', [issue('greeting_flow', 'Obsolete document')])); await old;
    assert.equal(frm.intro, '');
    // A failed HTTP call becomes a persistent recoverable verification error.
    const failure = context.refresh_consumer(frm, 'greeting', false, false, true);
    requests.at(-1).reject({status: 500}); await failure;
    assert.equal(frm.consumer_diagnostics.greeting.state, 'internal_error');
    assert.match(frm.intro, /request failed/); assert.equal(frm.consumer_checks.greeting.pending, false);
    const retry = beforeSave(frm);
    assert.match(frm.intro, /request failed/);
    assert.ok(!frm.intro.includes('Checking'));
    await complete(retry); assert.equal(frm.intro, '');
    const malformed = context.refresh_consumer(frm, 'greeting', false, false, true);
    requests.at(-1).resolve({message: {state: 'operational'}}); await malformed;
    assert.equal(frm.consumer_diagnostics.greeting.state, 'internal_error');
    await complete(context.refresh_consumer(frm, 'greeting', false, false, true));
    // Backend issues from separate sections are grouped; dynamic values are escaped.
    frm.doc.human_attention_enabled = 1; frm.doc.human_flow = '<script>unsafe</script>'; frm.doc.human_transfer_states = [];
    await complete(context.refresh_consumer(frm, 'human'), 'incomplete', [issue('human_transfer_states', 'Human state missing')]);
    await complete(context.refresh_consumer(frm, 'greeting', false, false, true), 'technical_required', [issue('greeting_flow', 'Process revision changed')]);
    assert.match(frm.intro, /2 configuration problem/); assert.match(frm.intro, /Human Support/); assert.match(frm.intro, /Greeting/);
    assert.match(frm.intro, /Process revision changed/); assert.ok(!frm.intro.includes('<script>'));
    const countBeforeBlockedSave = requests.length;
    await assert.rejects(beforeSave(frm), /Process revision changed|Human state missing/);
    assert.equal(requests.length, countBeforeBlockedSave);
    const retained = JSON.stringify([frm.doc.human_flow, frm.doc.human_transfer_states]);
    frm.doc.human_attention_enabled = 0;
    await context.refresh_consumer(frm, 'human');
    assert.equal(JSON.stringify([frm.doc.human_flow, frm.doc.human_transfer_states]), retained);
    await complete(context.refresh_consumer(frm, 'greeting', false, false, true));
    assert.equal(frm.intro, '');
    // Changes in field storage keep labels and are only reconciled after explicit state edits.
    frm = form('human'); frm.doc.human_fields = [{field_key: 'name', label: 'Edited', keep: 1, states: 'old'}];
    const stateChange = context.refresh_consumer(frm, 'human', false, true);
    requests.at(-1).resolve(response(requests.at(-1), 'incomplete', [issue('human_fields')], [{field_key: 'name', states: ['success']}]));
    await tick();
    requests.at(-1).resolve(response(requests.at(-1))); await stateChange;
    assert.deepEqual(JSON.parse(JSON.stringify(frm.doc.human_fields)), [{field_key: 'name', label: 'Edited', keep: 1, states: 'success'}]);
    assert.equal(frm.intro, '');
    // Disabling while a check is pending preserves its obsolete selections and permits save.
    const stale = context.refresh_consumer(frm, 'human', false, false, true); const staleRequest = requests.at(-1);
    frm.doc.human_attention_enabled = 0; await context.refresh_consumer(frm, 'human'); await beforeSave(frm);
    staleRequest.resolve(response(staleRequest, 'unavailable', [issue('human_flow')])); await stale;
    assert.equal(frm.consumer_diagnostics.human.state, 'disabled'); assert.equal(frm.intro, '');
    console.log('PASS: current pending save, menu toggles, A/B/A and document races, HTTP and malformed-response recovery, grouped escaped native alerts, independent sections, field retention, disabled stale selections.');
}
(async () => {
    if (process.argv.includes('--localized')) return localizedMessageFields();
    await selectorRaces();
    await diagnostics();
})().catch(error => {console.error(error); process.exitCode = 1;});
