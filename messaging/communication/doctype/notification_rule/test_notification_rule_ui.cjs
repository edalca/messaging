const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../../..');
const processTestSrc = fs.readFileSync(path.join(root, 'public/js/process_test.js'), 'utf8');
const providerParamsSrc = fs.readFileSync(path.join(root, 'public/js/provider_parameters.js'), 'utf8');
const ruleSrc = fs.readFileSync(path.join(root, 'communication/doctype/notification_rule/notification_rule.js'), 'utf8');

function makeElement(tag = 'div') {
    const el = {
        tag,
        children: [],
        classes: new Set(),
        attrs: {},
        props: {},
        handlers: {},
        textValue: '',
        parentEl: null,
        addClass(cls) { String(cls).split(/\s+/).filter(Boolean).forEach(c => el.classes.add(c)); return el; },
        removeClass(cls) { String(cls).split(/\s+/).filter(Boolean).forEach(c => el.classes.delete(c)); return el; },
        attr(k, v) { if (v === undefined) return el.attrs[k]; el.attrs[k] = v; return el; },
        prop(k, v) { if (v === undefined) return el.props[k]; el.props[k] = v; return el; },
        text(v) { if (v === undefined) return el.textValue; el.textValue = String(v); return el; },
        appendTo(parent) { parent.children.push(el); el.parentEl = parent; return el; },
        empty() { el.children = []; el.textValue = ''; return el; },
        remove() {
            if (el.parentEl) el.parentEl.children = el.parentEl.children.filter(c => c !== el);
            return el;
        },
        find(selector) {
            const matches = [];
            const walk = node => {
                for (const child of node.children) {
                    if ((selector === 'button' && child.tag === 'button')
                        || (selector.startsWith('.') && child.classes.has(selector.slice(1)))) {
                        matches.push(child);
                    }
                    walk(child);
                }
            };
            walk(el);
            return {
                length: matches.length,
                items: matches,
                remove() { matches.forEach(m => m.remove()); },
                closest() { return { remove() {} }; },
            };
        },
        on(evt, fn) { (el.handlers[evt] ||= []).push(fn); return el; },
        off(evt) { delete el.handlers[evt]; return el; },
        is() { return false; },
    };
    return el;
}

function makeJq() {
    const jq = input => {
        if (typeof input === 'string') {
            const match = input.match(/^<(\w+)/);
            const el = makeElement(match ? match[1] : 'div');
            const cls = input.match(/class="([^"]+)"/);
            if (cls) el.addClass(cls[1]);
            return el;
        }
        return input;
    };
    jq.ajax = async () => ({ message: {} });
    return jq;
}

async function runTests() {
    const formHandlers = {};
    const calls = [];
    const madeControls = [];
    let currentConfig = {};
    let currentSchema = { parameters: [] };

    const $ = makeJq();
    const sandbox = {
        console,
        setTimeout,
        clearTimeout,
        JSON,
        Map,
        Set,
        Array,
        Object,
        Boolean,
        Number,
        String,
        Symbol,
        Promise,
        Error,
        encodeURIComponent,
        $: $,
        __: (s) => s,
        locals: {},
        frappe: {
            csrf_token: 'test-token',
            user: { has_role: role => role === 'System Manager' },
            get_route_str: () => 'Form/Notification Rule/NRL-00194',
            provide(ns) {
                const parts = ns.split('.');
                let cur = sandbox;
                for (const p of parts) cur = (cur[p] ||= {});
            },
            require: async () => {},
            utils: { escape_html: s => String(s ?? '') },
            msgprint() {},
            confirm: (_msg, yes) => yes(),
            call: async ({ method, args }) => {
                calls.push({ method, args });
                if (method.endsWith('.describe')) {
                    return { message: { status: 'Untested', conditions: [] } };
                }
                if (method.endsWith('.configuration')) {
                    return { message: currentConfig };
                }
                if (method.endsWith('.parameter_schema')) {
                    return { message: currentSchema };
                }
                return { message: {} };
            },
            model: {
                set_value: async () => {},
                remove_from_locals() {},
                sync() {},
            },
            ui: {
                form: {
                    on(doctype, handlers) {
                        (formHandlers[doctype] ||= []).push(handlers);
                    },
                    make_control({ parent, df }) {
                        const ctrl = {
                            df,
                            value: '',
                            $wrapper: makeElement('div').appendTo(parent),
                            set_value: async v => { ctrl.value = v; },
                            get_value: () => ctrl.value,
                            disable_password_checks() {},
                        };
                        madeControls.push(ctrl);
                        return ctrl;
                    },
                },
            },
        },
    };
    vm.createContext(sandbox);
    vm.runInContext(processTestSrc, sandbox);
    vm.runInContext(providerParamsSrc, sandbox);
    vm.runInContext(ruleSrc, sandbox);

    function makeFrm({ lifecycle_status, account = '1325812677287836', redirect_recipient = 0, parameters = [], status = lifecycle_status, isNew = false }) {
        const buttons = [];
        const dfProps = {};
        const toggled = {};
        const doc = {
            doctype: 'Notification Rule',
            name: isNew ? 'new-rule-1' : 'NRL-00194',
            status,
            account,
            redirect_recipient,
            parameters,
            events: [],
            process_parameters: [],
            outputs: [],
            process_contract: '{}',
            modified: '2026-11-18 17:00:00',
            __onload: {
                account_bound: !isNew && !!account,
            },
        };
        const frm = {
            doctype: 'Notification Rule',
            doc,
            perm: [{ write: 1 }],
            wrapper: makeElement('div'),
            $wrapper: makeElement('div'),
            is_new: () => isNew,
            is_dirty: () => false,
            dirty() {},
            set_query() {},
            set_df_property(field, prop, val) { (dfProps[field] ||= {})[prop] = val; },
            toggle_display(field, show) { toggled[field] = show; },
            refresh_field() {},
            refresh_fields() {},
            set_intro() {},
            set_value: async (field, val) => { doc[field] = val; },
            clear_table(field) { doc[field] = []; },
            add_child(field, row) { const item = { ...row }; (doc[field] ||= []).push(item); return item; },
            fields_dict: {
                process_method: { $wrapper: makeElement('div') },
                delivery_information: { $wrapper: makeElement('div') },
                parameter_controls: { $wrapper: makeElement('div') },
                events: {
                    grid: {
                        grid_rows: [],
                        docfieldProps: {},
                        update_docfield_property(field, prop, val) {
                            (this.docfieldProps[field] ||= {})[prop] = val;
                        },
                    },
                },
                process_parameters: { grid: { grid_rows: [] } },
                outputs: { grid: { grid_rows: [] } },
            },
            page: { set_indicator() {} },
            add_custom_button(label, fn) {
                const btn = makeElement('button');
                btn.text(label);
                btn._fn = fn;
                buttons.push(btn);
                return btn;
            },
            remove_custom_button(label) {
                const index = buttons.findIndex(button => button.textValue === label);
                if (index >= 0) buttons.splice(index, 1);
            },
            _buttons: buttons,
            _dfProps: dfProps,
            _toggled: toggled,
        };
        return frm;
    }

    const triggerRefresh = async frm => {
        for (const h of formHandlers['Notification Rule']) {
            if (h.refresh) await h.refresh(frm);
        }
    };

    // Untested rules expose a single top action and no inline test button.
    currentConfig = {
        applications: ['qota'], modules: ['Qota'], condition_fields: [], process_fields: [],
        events: ['on_submit'], channel: 'whatsapp_qota', provider: 'WHATSAPP',
        redirect_supported: true, redirect_required: false,
        delivery_information: 'Optional test recipient redirection is available.',
    };
    currentSchema = {
        parameters: [{
            parameter_key: 'notification_phone_number', label: 'Test Destination Number',
            field_type: 'Data', required: 1, options: '', default_value: '', sort_order: 0,
            description: 'International E.164 phone number (for example, +50499999999).',
        }],
    };
    madeControls.length = 0;
    const untestedFrm = makeFrm({ lifecycle_status: 'Untested', redirect_recipient: 0 });
    await triggerRefresh(untestedFrm);
    assert.equal(untestedFrm.fields_dict.process_method.$wrapper.find('button').length, 0, 'Bottom Validate method button must be removed');
    assert.deepEqual(untestedFrm._buttons.map(b => b.textValue), ['Validate'], 'Top Validate button must appear for Untested rule');
    const meta = JSON.parse(fs.readFileSync(path.join(__dirname, 'notification_rule.json'), 'utf8'));
    assert.equal(meta.fields.find(field => field.fieldname === 'status').read_only, 1);
    assert.equal(untestedFrm._dfProps.redirect_recipient, undefined, 'redirect_recipient must be editable on bound rule');
    assert.equal(untestedFrm._toggled.redirect_recipient, true, 'WHATSAPP must show redirect_recipient checkbox');
    assert.equal(madeControls.length, 0, 'Test Destination Number must be hidden when redirect_recipient is unchecked');

    // 2) Checking redirect_recipient renders Test Destination Number as editable required Data control
    untestedFrm.doc.redirect_recipient = 1;
    for (const h of formHandlers['Notification Rule']) {
        if (h.redirect_recipient) await h.redirect_recipient(untestedFrm);
    }
    assert.equal(madeControls.length, 1);
    assert.equal(madeControls[0].df.fieldname, 'notification_phone_number');
    assert.equal(madeControls[0].df.label, 'Test Destination Number');
    assert.equal(madeControls[0].df.fieldtype, 'Data');
    assert.equal(madeControls[0].df.reqd, true);
    assert.equal(madeControls[0].df.read_only, false, 'Test Destination Number must be editable on bound rule');

    // Unchecking redirect_recipient clears parameters
    madeControls[0].value = '+50499999999';
    sandbox.messaging.provider_parameters.store(untestedFrm, currentSchema.parameters[0], madeControls[0]);
    assert.equal(untestedFrm.doc.parameters.length, 1);
    untestedFrm.doc.redirect_recipient = 0;
    for (const h of formHandlers['Notification Rule']) {
        if (h.redirect_recipient) await h.redirect_recipient(untestedFrm);
    }
    assert.equal(untestedFrm.doc.parameters.length, 0, 'Unchecking redirect_recipient must clear stale parameters');

    for (const [state, action] of [['Disabled', 'Enable'], ['Enabled', 'Disable'], ['Unavailable', 'Validate']]) {
        const frm = makeFrm({status: state});
        await triggerRefresh(frm);
        assert.deepEqual(frm._buttons.map(b => b.textValue), [action]);
    }
    assert(!calls.some(call => /describe|test_method|validate_process|set_active/.test(call.method)));

    // Events table lock by status: only Enabled locks events; Untested, Disabled, Unavailable allow editing.
    for (const [state, expectedLocked] of [['Untested', false], ['Disabled', false], ['Enabled', true], ['Unavailable', false]]) {
        const frm = makeFrm({ status: state });
        await triggerRefresh(frm);
        assert.equal(frm._dfProps.source_application.read_only, true, `${state}: source_application must stay locked`);
        assert.equal(frm._dfProps.source_doctype.read_only, true, `${state}: source_doctype must stay locked`);
        assert.equal(frm._dfProps.events.read_only, expectedLocked, `${state}: events read_only mismatch`);
        assert.equal(frm.fields_dict.events.grid.cannot_add_rows, expectedLocked, `${state}: cannot_add_rows mismatch`);
        assert.equal(frm.fields_dict.events.grid.cannot_delete_rows, expectedLocked, `${state}: cannot_delete_rows mismatch`);
        for (const f of ['document_event', 'field_name', 'value']) {
            assert.equal(frm.fields_dict.events.grid.docfieldProps[f].read_only, expectedLocked, `${state}: ${f} read_only mismatch`);
        }
    }

    // 4) EMULR account: hides redirect_recipient, shows required notification_contact Link
    currentConfig = {
        applications: ['qota'], modules: ['Qota'], condition_fields: [], process_fields: [],
        events: ['on_submit'], channel: 'emulator', provider: 'EMULR',
        redirect_supported: false, redirect_required: true,
        delivery_information: 'Configure the required test destination on this notification rule.',
    };
    currentSchema = {
        parameters: [{
            parameter_key: 'notification_contact', label: 'Notification Emulated Contact',
            field_type: 'Link', required: 1, options: 'Emulator Contact', default_value: '', sort_order: 0,
            description: 'Select an enabled fictitious contact.',
        }],
    };
    madeControls.length = 0;
    const emulrFrm = makeFrm({ lifecycle_status: 'Disabled', account: 'EMU-001', redirect_recipient: 0 });
    await triggerRefresh(emulrFrm);
    assert.equal(emulrFrm._toggled.redirect_recipient, false);
    assert.equal(madeControls.length, 1);
    assert.equal(madeControls[0].df.fieldname, 'notification_contact');
    assert.equal(madeControls[0].df.fieldtype, 'Link');
    assert.equal(madeControls[0].df.reqd, true);

    console.log('ALL Notification Rule UI assertions passed.');
}

runTests().catch(err => {
    console.error(err);
    process.exit(1);
});
