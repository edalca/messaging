/* Native control wiring for per-template provider parameters; no browser or network. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const calls = [];
const context = {messaging: {}, __: value => value};
context.frappe = {
    provide() {}, utils: {escape_html: value => value},
    confirm(_message, yes) {yes();},
    call: async args => {
        calls.push(args);
        return {message: {parameters: args.args.channel === 'channel-one' ? [
            {parameter_key: 'remote_name', label: 'Remote name', field_type: 'Data', required: true, sort_order: 0},
            {parameter_key: 'counter', label: 'Counter', field_type: 'Int', sort_order: 1},
            {parameter_key: 'secret', label: 'Secret', field_type: 'Password', sort_order: 2},
            {parameter_key: 'parameter_format', label: 'Provider Parameter Format', field_type: 'Select',
                sort_order: 3, options: 'POSITIONAL\nNAMED'},
        ] : args.args.channel === 'whatsapp-channel' ? [
            {parameter_key: 'parameter_format', label: 'Provider Parameter Format', field_type: 'Select',
                sort_order: 0, options: 'POSITIONAL\nNAMED'},
        ] : []}};
    },
    ui: {form: {make_control: ({df}) => ({df, value: '',
        async set_value(value) {this.value = value;}, get_value() {return this.value;}, disable_password_checks() {}})}},
};
vm.runInNewContext(fs.readFileSync(__dirname + '/../../../public/js/provider_parameters.js', 'utf8'), context);
const display = {};
const frm = {doctype: 'Communication Template', perm: [{write: 1}],
    doc: {channel: 'channel-one', status: 'Active',
        parameters: [{parameter_key: 'remote_name', value: '"stored-name"'}]},
    fields_dict: {parameter_controls: {$wrapper: {empty() {return this;}}}, provider_section: {}},
    toggle_display(field, visible) {display[field] = visible;}, dirty() {},
    clear_table(field) {this.doc[field] = [];},
    add_child(field, row) {this.doc[field].push(row);return row;},
    async set_value(field, value) {this.doc[field] = value;},
};
(async () => {
    const api = context.messaging.provider_parameters;
    const formats = [['POSITIONAL', false], ['NAMED', true]];
    frm._template_capabilities = {parameter_formats: formats};
    await api.render(frm);
    assert.equal(calls[0].args.channel, 'channel-one');
    assert.equal(calls[0].args.account, undefined);
    assert.equal(frm._parameter_controls[0].control.get_value(), 'stored-name');
    assert.equal(frm._parameter_controls[0].control.df.reqd, true);
    assert.equal(display.provider_section, true);
    frm._parameter_controls[1].control.value = 7;
    api.flush(frm);
    assert.equal(frm.doc.parameters.find(row => row.parameter_key === 'counter').value, '7');
    const field = frm._parameter_controls[3].control;
    assert.equal(field.df.fieldtype, 'Select');
    assert.equal(field.get_value(), 'POSITIONAL');
    assert(!frm.doc.parameters.some(row => row.parameter_key === 'parameter_format'));
    let event;
    frm.script_manager = {trigger: name => {event = name;}};
    field.value = 'NAMED';
    field.df.change();
    assert.equal(event, 'provider_parameter_format');
    assert.equal(frm.doc.parameters.find(row => row.parameter_key === 'parameter_format').value, '"NAMED"');
    frm.doc.published_revision = 'verified';
    await api.render(frm);
    assert.equal(frm._parameter_controls[3].control.get_value(), 'NAMED');
    assert.equal(frm._parameter_controls[3].control.df.read_only, true);
    context.messaging.communication_template = {};
    context.frappe.ui.form.on = () => {};
    vm.runInNewContext(fs.readFileSync(__dirname + '/communication_template.js', 'utf8'), context);
    const doc = {...frm.doc, body: 'Use {{reference}} twice: {{reference}}.',
        template_variables: [{parameter_name: 'reference'}]};
    assert.equal(context.messaging.communication_template.inspect_template(doc, formats).valid, true);
    assert.equal(context.messaging.communication_template.inspect_template(doc).valid, false);
    frm.doc.channel = 'whatsapp-channel';
    frm.doc.published_revision = null;
    await api.selection_changed(frm, 'channel');
    assert.deepEqual(Array.from(frm._parameter_controls, ({parameter}) => parameter.parameter_key), ['parameter_format']);
    assert.equal(frm._parameter_controls[0].control.df.fieldtype, 'Select');
    assert.equal(frm._parameter_controls[0].control.df.read_only, false);
    assert.equal(frm._parameter_controls[0].control.get_value(), 'POSITIONAL');
    frm.doc.channel = 'channel-two';
    await api.selection_changed(frm, 'channel');
    assert.equal(calls.at(-1).args.channel, 'channel-two');
    assert.equal(frm.doc.parameters.length, 0);
    assert.equal(frm._parameter_controls.length, 0);
    assert.equal(display.provider_section, false);
    const count = calls.length;
    frm.doc.channel = null;
    await api.render(frm);
    assert.equal(calls.length, count);
    const schema = JSON.parse(fs.readFileSync(__dirname + '/communication_template.json', 'utf8'));
    assert.equal(schema.allow_rename || 0, 0);
    assert(!schema.fields.some(field => ['external_reference', 'category', 'approval_status'].includes(field.fieldname)));
    assert.equal(schema.fields.find(field => field.fieldname === 'parameters').options, 'Communication Provider Value');
    assert.equal(schema.fields.find(field => field.fieldname === 'channel').reqd, 1);
    assert.equal(schema.fields.find(field => field.fieldname === 'account').options, 'Communication Channel Account');
    assert.equal(schema.fields.find(field => field.fieldname === 'provider_language').fieldtype, 'Select');
    assert.equal(schema.fields.find(field => field.fieldname === 'language').options, 'Language');
    assert.equal(schema.fields.find(field => field.fieldname === 'language_mapping').read_only, 1);
    assert.equal(schema.fields.find(field => field.fieldname === 'published_revision').read_only, 1);
    assert.equal(schema.fields.find(field => field.fieldname === 'status').options, 'Inactive\nActive');
    assert.equal(schema.fields.find(field => field.fieldname === 'status').default, 'Inactive');
    assert.equal(schema.fields.find(field => field.fieldname === 'status').read_only, 1);
    assert.equal(schema.fields.some(field => field.fieldname === 'usage_type'), false);
    const statusSchema = JSON.parse(fs.readFileSync(__dirname + '/../communication_template_status/communication_template_status.json', 'utf8'));
    assert.equal(statusSchema.fields.find(field => field.fieldname === 'template_revision').read_only, 1);
    context.frappe.user = {has_role: () => true};
    context.frappe.listview_settings = {};
    context.frappe.call = async () => ({message: [
        {label: 'Synthetic account provider', provider: 'ACCTP', scope: 'Account'},
        {label: 'Synthetic channel provider', provider: 'CHANN', scope: 'Channel'},
    ]});
    const buttons = [];
    const dialogs = [];
    context.frappe.ui.Dialog = class {
        constructor(config) {this.config = config;}
        hide() {}
        show() {dialogs.push(this.config);}
    };
    const list = {refresh: async () => {}, page: {
        add_inner_button: (label, action, group) => buttons.push({label, action, group}),
    }};
    vm.runInNewContext(fs.readFileSync(__dirname + '/communication_template_list.js', 'utf8'), context);
    await context.frappe.listview_settings['Communication Template'].onload(list);
    assert.deepEqual(buttons.map(({label, group}) => [label, group]), [
        ['Synthetic account provider', 'Synchronize templates'], ['Synthetic channel provider', 'Synchronize templates'],
    ]);
    buttons.forEach(button => button.action());
    assert.deepEqual(Array.from(dialogs[0].fields, field => field.fieldname), ['channel', 'account']);
    assert.deepEqual(Array.from(dialogs[1].fields, field => field.fieldname), ['channel']);
    context.frappe.call = async () => ({message: {
        success: true, created: 1, updated: 0, skipped: [
            {name: '<external name>', provider_language: 'external:code', category: 'MARKETING', reason: 'unsupported_buttons'},
        ], language_warnings: [
            {name: 'Imported name', provider_language: 'en_AE', language: 'es', reason: 'provisional_template_language'},
        ],
    }});
    context.frappe.utils.escape_html = value => value.replaceAll('<', '&lt;').replaceAll('>', '&gt;');
    let diagnostics;
    context.frappe.msgprint = value => {diagnostics = value;};
    await dialogs[0].primary_action({channel: 'channel-one', account: 'account-one'});
    assert.match(diagnostics, /&lt;external name&gt; · external:code · MARKETING — unsupported_buttons/);
    assert.match(diagnostics, /<\/ul><p>Language Mapping<\/p><ul><li>Imported name · en_AE/);
    console.log('PASS: format selector and repeat policy, provider scope and language diagnostics.');
})().catch(error => {console.error(error);process.exitCode = 1;});
