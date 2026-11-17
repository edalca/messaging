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
        return {message: {parameters: args.args.provider === 'account-one' ? [
            {parameter_key: 'remote_name', label: 'Remote name', field_type: 'Data', required: true, sort_order: 0},
            {parameter_key: 'counter', label: 'Counter', field_type: 'Int', sort_order: 1},
            {parameter_key: 'secret', label: 'Secret', field_type: 'Password', sort_order: 2},
        ] : []}};
    },
    ui: {form: {make_control: ({df}) => ({df, value: '',
        async set_value(value) {this.value = value;}, get_value() {return this.value;}, disable_password_checks() {}})}},
};
vm.runInNewContext(fs.readFileSync(__dirname + '/../../../public/js/provider_parameters.js', 'utf8'), context);
const display = {};
const frm = {doctype: 'Communication Template', perm: [{write: 1}],
    doc: {provider: 'account-one', status: 'Active',
        parameters: [{parameter_key: 'remote_name', value: '"stored-name"'}]},
    fields_dict: {parameter_controls: {$wrapper: {empty() {return this;}}}, provider_section: {}},
    toggle_display(field, visible) {display[field] = visible;}, dirty() {},
    clear_table(field) {this.doc[field] = [];},
    add_child(field, row) {this.doc[field].push(row);return row;},
    async set_value(field, value) {this.doc[field] = value;},
};
(async () => {
    const api = context.messaging.provider_parameters;
    await api.render(frm);
    assert.equal(calls[0].args.provider, 'account-one');
    assert.equal(calls[0].args.account, undefined);
    assert.equal(frm._parameter_controls[0].control.get_value(), 'stored-name');
    assert.equal(frm._parameter_controls[0].control.df.reqd, true);
    assert.equal(display.provider_section, true);
    frm._parameter_controls[1].control.value = 7;
    api.flush(frm);
    assert.equal(frm.doc.parameters.find(row => row.parameter_key === 'counter').value, '7');
    frm.doc.provider = 'account-two';
    await api.selection_changed(frm, 'provider');
    assert.equal(calls.at(-1).args.provider, 'account-two');
    assert.equal(frm.doc.parameters.length, 0);
    assert.equal(frm._parameter_controls.length, 0);
    assert.equal(display.provider_section, false);
    const count = calls.length;
    frm.doc.provider = null;
    await api.render(frm);
    assert.equal(calls.length, count);
    const schema = JSON.parse(fs.readFileSync(__dirname + '/communication_template.json', 'utf8'));
    assert.equal(schema.allow_rename || 0, 0);
    assert(!schema.fields.some(field => ['external_reference', 'category', 'approval_status'].includes(field.fieldname)));
    assert.equal(schema.fields.find(field => field.fieldname === 'parameters').options, 'Communication Provider Value');
    assert.equal(schema.fields.find(field => field.fieldname === 'provider').reqd, 1);
    assert.equal(schema.fields.find(field => field.fieldname === 'status').options, 'Inactive\nActive');
    assert.equal(schema.fields.find(field => field.fieldname === 'status').default, 'Inactive');
    assert.equal(schema.fields.find(field => field.fieldname === 'status').read_only, 1);
    assert.equal(schema.fields.some(field => field.fieldname === 'usage_type'), false);
    const statusSchema = JSON.parse(fs.readFileSync(__dirname + '/../communication_template_status/communication_template_status.json', 'utf8'));
    assert.equal(statusSchema.fields.find(field => field.fieldname === 'template_revision').read_only, 1);
    console.log('PASS: dynamic Template scope, provider selection, stored values, typed edits, explicit clearing, empty providers and generic schema.');
})().catch(error => {console.error(error);process.exitCode = 1;});
