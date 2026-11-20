frappe.provide('messaging.notification_conditions');
(() => {
    const api = messaging.notification_conditions;
    const decode = row => { try { return JSON.parse(row.value || 'null'); } catch { return null; } };
    const definition = (frm, row) => (frm.notification_fields || []).find(f => f.fieldname === row.field_name);
    function store(row, control) {
        let value = control.get_value();
        if (control.df.fieldtype === 'Check') value = !!Number(value);
        const encoded = value === '' || value == null ? '' : JSON.stringify(value);
        return frappe.model.set_value(row.doctype, row.name, 'value', encoded);
    }
    function editor(frm, gridRow, parent, onlyInput) {
        const row = gridRow.doc, field = definition(frm, row);
        const kind = field?.fieldtype || 'Data';
        let control;
        const df = {
            fieldname: 'condition_value', label: __('Value'),
            fieldtype: ['Email', 'Phone', 'URL'].includes(kind) ? 'Data' : kind,
            options: kind === 'Data' ? field?.options : ['Email', 'Phone', 'URL'].includes(kind) ? kind : field?.options,
            length: field?.length, precision: field?.precision,
            read_only: (!frm.is_new() && frm.doc.status === 'Enabled') || !field || !frm.perm?.[0]?.write,
            change: () => { if (!control._condition_loading) return store(row, control); }
        };
        parent.empty();
        control = frappe.ui.form.make_control({parent, df, only_input: onlyInput, render_input: true});
        const nativeRefresh = control.refresh.bind(control);
        control.refresh = () => {
            control._condition_loading = true;
            control.value = decode(row);
            nativeRefresh();
            control.set_input(control.value);
            control._condition_loading = false;
        };
        control.refresh();
        control.$input?.addClass('input-sm');
        control._condition_row = row;
        return control;
    }
    api.render_row = function(frm, gridRow) {
        if (gridRow.grid.df.fieldname !== 'events') return;
        const row = gridRow.doc;
        // Row-owned copies only. Shared DocField metadata is never changed.
        const eventDf = gridRow.docfields.find(f => f.fieldname === 'document_event');
        eventDf.options = (frm.notification_events || []).join('\n');
        if (!gridRow._condition_make_control) {
            const nativeMake = gridRow.make_control.bind(gridRow);
            gridRow.make_control = column => {
                if (column.df.fieldname !== 'value') {
                    nativeMake(column);
                    if (column.df.fieldname === 'field_name') column.field?.set_data((frm.notification_fields || []).map(f => ({label: __(f.label) + ' (' + f.fieldname + ')', value: f.fieldname})));
                    return;
                }
                if (!column.field) {
                    column.field = editor(frm, gridRow, column.field_area, true);
                    gridRow.on_grid_fields_dict.value = column.field;
                }
            };
            const nativeRefresh = gridRow.refresh_field.bind(gridRow);
            gridRow.refresh_field = (fieldname, text) => {
                nativeRefresh(fieldname, text);
                if (fieldname === 'value') gridRow.columns.value?.static_area.text(decode(row) == null ? '' : String(decode(row)));
            };
            gridRow._condition_make_control = true;
        }
        const validation = gridRow.columns.validation_status;
        if (validation) {
            const approved = ['Disabled', 'Enabled'].includes(frm.doc.status) && frm.doc.approval_hash;
            validation.static_area.text(approved ? __('Validated') : __('Not validated'));
        }
        const valueColumn = gridRow.columns.value;
        const signature = JSON.stringify([definition(frm, row) || null, frm.doc.status === 'Enabled']);
        if (valueColumn?.field && valueColumn.field._condition_signature !== signature) {
            valueColumn.field.$wrapper.remove();
            delete valueColumn.field;
            gridRow.make_control(valueColumn);
        }
        if (valueColumn?.field) valueColumn.field._condition_signature = signature;
        if (valueColumn) valueColumn.static_area.text(decode(row) == null ? '' : String(decode(row)));
        const fieldControl = gridRow.on_grid_fields_dict.field_name;
        fieldControl?.set_data((frm.notification_fields || []).map(f => ({label: __(f.label) + ' (' + f.fieldname + ')', value: f.fieldname})));
        if (gridRow.grid_form?.fields_dict.value) {
            const original = gridRow.grid_form.fields_dict.value;
            const parent = original.$wrapper.parent();
            original.$wrapper.remove();
            gridRow.grid_form.fields_dict.value = editor(frm, gridRow, parent, false);
            gridRow.grid_form.fields_dict.field_name?.set_data((frm.notification_fields || []).map(f => ({label: __(f.label), value: f.fieldname})));
        }
    };
    api.refresh = frm => frm.fields_dict.events.grid.grid_rows.forEach(row => api.render_row(frm, row));
    api.flush = async frm => {
        for (const row of frm.fields_dict.events.grid.grid_rows) {
            const control = row.grid_form?.wrapper.is(':visible') ? row.grid_form.fields_dict.value : row.columns.value?.field;
            if ((frm.is_new() || frm.doc.status !== 'Enabled') && control?._condition_row && definition(frm, row.doc)) {
                await control.parse_validate_and_set_in_model(control.get_input_value());
                await store(row.doc, control);
            }
        }
    };
})();

frappe.provide('messaging.notification_parameters');
(() => {
    const api = messaging.notification_parameters;
    const decode = row => row.fixed_value == null || row.fixed_value === '' ? undefined : JSON.parse(row.fixed_value);
    const fieldtype = row => ({string: 'Data', integer: 'Int', number: 'Float', boolean: 'Check', date: 'Date'}[row.internal_type] || 'Data');
    function value(row, control) {
        const raw = control.get_input_value();
        if (row.internal_type === 'boolean') return Boolean(Number(raw));
        if (row.internal_type === 'integer') {
            if (!/^[+-]?[0-9]+$/.test(String(raw)) || !Number.isSafeInteger(Number(raw))) frappe.throw(__('Supply a valid integer.'));
            return Number(raw);
        }
        if (row.internal_type === 'number') {
            if (!/^[+-]?[0-9]+(?:\.[0-9]+)?$/.test(String(raw))) frappe.throw(__('Supply an exact decimal value without grouping.'));
            return String(raw);
        }
        return raw ?? '';
    }
    function scalar(row, parent, initial, changed, editable = true) {
        const control = frappe.ui.form.make_control({parent, df: {
            fieldname: 'typed_value', label: __('Value'), fieldtype: fieldtype(row), read_only: !editable,
            change: () => { if (!control._loading) changed(); }
        }, only_input: true, render_input: true});
        // Preserve exact decimal text through the native Float control.
        if (row.internal_type === 'number') {
            control.parse = raw => raw;
            control.format_for_input = raw => raw == null ? '' : String(raw);
        }
        control._loading = true;
        control.set_input(initial === undefined ? '' : initial);
        control._loading = false;
        return control;
    }
    function store(row, values) {
        return frappe.model.set_value(row.doctype, row.name, 'fixed_value', values === undefined ? '' : JSON.stringify(values));
    }
    function multipleEditor(frm, row, parent) {
        const button = $('<button type="button" class="btn btn-default btn-sm">').text(__('Edit values')).prop('disabled', !frm.perm?.[0]?.write).appendTo(parent);
        button.on('click', () => {
            const dialog = new frappe.ui.Dialog({title: __('Fixed Values'), fields: [{fieldname: 'values', fieldtype: 'HTML'}],
                primary_action_label: __('Apply'), primary_action: async () => {
                    const values = editors.map(item => value(row, item.control));
                    await store(row, values.length ? values : undefined);
                    dialog.hide(); frm.refresh_field('process_parameters');
                }});
            const editors = [];
            const area = dialog.fields_dict.values.$wrapper;
            const add = initial => {
                const wrapper = $('<div class="d-flex align-items-center mb-2" style="gap:8px">').appendTo(area);
                const input = $('<div class="flex-grow-1">').appendTo(wrapper);
                const item = {control: scalar(row, input, initial, () => {})};
                editors.push(item);
                $('<button type="button" class="btn btn-default btn-sm">').text(__('Remove')).appendTo(wrapper).on('click', () => {
                    editors.splice(editors.indexOf(item), 1); wrapper.remove();
                });
            };
            for (const initial of decode(row) || []) add(initial);
            dialog.add_custom_action(__('Add value'), () => add(undefined));
            dialog.show();
        });
    }
    function editor(frm, gridRow, parent) {
        const row = gridRow.doc;
        parent.empty();
        if (row.value_source !== 'Fixed Value') return;
        const initial = decode(row);
        if (row.multiple) {
            multipleEditor(frm, row, parent);
            $('<span class="small text-muted ml-2">').text(initial === undefined ? __('Not configured') : String(initial.length)).appendTo(parent);
            return;
        }
        const control = scalar(row, parent, initial, () => store(row, value(row, control)), Boolean(frm.perm?.[0]?.write));
        const refresh = control.refresh.bind(control);
        control.refresh = () => {
            control._loading = true;
            const current = decode(row);
            control.value = current === undefined ? '' : current;
            refresh();
            control.set_input(control.value);
            control._loading = false;
        };
        if (initial === undefined) $('<span class="small text-muted">').text(__('Not configured')).appendTo(parent);
        $('<button type="button" class="btn btn-link btn-xs">').text(__('Set value')).prop('disabled', !frm.perm?.[0]?.write).appendTo(parent).on('click', () => store(row, value(row, control)));
        $('<button type="button" class="btn btn-link btn-xs">').text(__('Clear')).prop('disabled', !frm.perm?.[0]?.write).appendTo(parent).on('click', async () => {
            await store(row, undefined); frm.refresh_field('process_parameters');
        });
        control._param_row = row;
        control.$input?.addClass('input-sm');
        return control;
    }
    api.render_row = function(frm, gridRow) {
        if (gridRow.grid.df.fieldname !== 'process_parameters') return;
        const row = gridRow.doc;
        if (!gridRow._typed_parameters) {
            const make = gridRow.make_control.bind(gridRow);
            gridRow.make_control = column => {
                if (column.df.fieldname !== 'fixed_value') return make(column);
                if (!column.field) {
                    const control = editor(frm, gridRow, column.field_area);
                    // Native grids require a control-shaped object for dialog/list editors.
                    column.field = control || {df: column.df, refresh() {}, get_value: () => row.fixed_value};
                    gridRow.on_grid_fields_dict.fixed_value = column.field;
                }
            };
            gridRow._typed_parameters = true;
        }
        const signature = JSON.stringify([row.internal_type, row.multiple, row.value_source]);
        const column = gridRow.columns.fixed_value;
        if (column?.field && column.field._signature !== signature) {
            column.field_area.empty(); delete column.field; gridRow.make_control(column);
        }
        if (column?.field) column.field._signature = signature;
        if (column?.static_area) {
            const initial = decode(row);
            column.static_area.text(initial === undefined ? __('Not configured') : Array.isArray(initial) ? initial.join(', ') : String(initial));
        }
        const original = gridRow.grid_form?.fields_dict.fixed_value;
        if (original && !original._param_row) {
            const area = original.$wrapper || gridRow.grid_form.wrapper;
            const control = editor(frm, gridRow, area);
            if (control) gridRow.grid_form.fields_dict.fixed_value = control;
        }
    };
    api.refresh = frm => frm.fields_dict.process_parameters?.grid?.grid_rows?.forEach(row => api.render_row(frm, row));
    api.flush = async frm => {
        // Controls persist their changes; untouched controls preserve absence and exact JSON.
        for (const gridRow of frm.fields_dict.process_parameters?.grid?.grid_rows || []) {
            const control = gridRow.grid_form?.wrapper.is(':visible') ? gridRow.grid_form.fields_dict.fixed_value : gridRow.columns.fixed_value?.field;
            if (control?._param_row && control.$input?.is(':focus')) {
                await store(gridRow.doc, value(gridRow.doc, control));
            }
        }
    };
})();

frappe.ui.form.on('Notification Rule', {
    setup(frm) {
        frm.set_query('process_definition', () => ({filters: {process_type: 'notification', status: 'Enabled'}}));
        frm.set_query('template_reference', 'outputs', () => ({
            query: 'messaging.communication.doctype.communication_template.external.notification_template_query',
            filters: {account: frm.doc.account},
        }));
        frm.set_query('account', () => ({query: 'messaging.communication.doctype.notification_rule.notification_rule.account_query', filters: {channel: null}}));
        frm.set_query('source_doctype', () => ({filters: {
            module: ['in', frm.notification_modules || []], istable: 0, issingle: 0, is_virtual: 0,
            name: ['not in', ['Notification Rule', 'Notification Queue', 'Process Definition', 'Process Execution Log', 'Error Log']]
        }}));
        $(frm.wrapper).on('grid-row-render.notification_conditions', (event, row) => messaging.notification_conditions.render_row(frm, row));
        $(frm.wrapper).on('grid-row-render.notification_parameters', (event, row) => messaging.notification_parameters.render_row(frm, row));
    },
    async refresh(frm) {
        await frappe.require('/assets/messaging/js/process_test.js');
        await refresh_notification_rule(frm);
        lock_rule_structure(frm);
        messaging.process_approval.refresh(frm, 'messaging.communication.doctype.notification_rule.notification_rule.');
    },
    async redirect_recipient(frm) {
        if (frm.notification_loading) return;
        if (!frm.doc.redirect_recipient && !frm.notification_redirect_required && (frm.doc.parameters || []).length) {
            frm.clear_table('parameters');
        }
        await refresh_notification_rule(frm);
    },
    async validate(frm) {
        await messaging.notification_conditions.flush(frm);
        await messaging.notification_parameters.flush(frm);
        messaging.provider_parameters?.flush(frm);
    },
    async source_application(frm) {
        if (frm.notification_loading) return;
        await frm.set_value({source_doctype: '', process_method: ''});
        await refresh_notification_rule(frm);
    },
    async source_doctype(frm) {
        await refresh_notification_rule(frm);
        const invalid = (frm.doc.events || []).filter(row => row.field_name && !(frm.notification_fields || []).some(f => f.fieldname === row.field_name));
        // Any source change invalidates typed values, even for equally named fields.
        const changed = frm.notification_source && frm.notification_source !== frm.doc.source_doctype;
        for (const row of frm.doc.events || []) {
            if (changed || invalid.includes(row)) await frappe.model.set_value(row.doctype, row.name, {field_name: '', value: ''});
        }
        if (invalid.length || changed) frappe.msgprint(__('Source DocType changed. Review the event conditions; incompatible fields and values were cleared.'));
        frm.notification_source = frm.doc.source_doctype;
        messaging.notification_conditions.refresh(frm);
    },
    async account(frm) {
        if (frm.notification_loading) return;
        await frappe.require('/assets/messaging/js/provider_parameters.js');
        if (!await messaging.provider_parameters.selection_changed(frm, 'account')) return;
        await frm.set_value('redirect_recipient', 0);
        await refresh_notification_rule(frm);

    },

});
frappe.ui.form.on('Notification Rule Event', {
    async field_name(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        const field = (frm.notification_fields || []).find(f => f.fieldname === row.field_name);
        await frappe.model.set_value(cdt, cdn, 'value', field?.fieldtype === 'Check' ? 'false' : '');
        frm.refresh_field('events');
    },
    events_add(frm) { messaging.notification_conditions.refresh(frm); },
    form_render(frm, cdt, cdn) { messaging.notification_conditions.render_row(frm, frm.fields_dict.events.grid.grid_rows_by_docname[cdn]); }
});
function lock_rule_structure(frm) {
    const locked = !frm.is_new();
    const eventsLocked = locked && frm.doc.status === 'Enabled';
    frm.set_df_property('account', 'read_only', locked && !!frm.doc.__onload?.account_bound);
    for (const field of ['source_application', 'source_doctype']) frm.set_df_property(field, 'read_only', locked);
    frm.set_df_property('events', 'read_only', eventsLocked);
    const grid = frm.fields_dict.events.grid;
    grid.cannot_add_rows = eventsLocked;
    grid.cannot_delete_rows = eventsLocked;
    for (const field of ['document_event', 'field_name', 'value']) grid.update_docfield_property(field, 'read_only', eventsLocked);
}

async function refresh_notification_rule(frm) {
    const revision = frm.notification_revision = (frm.notification_revision || 0) + 1;
    const {message: data} = await frappe.call({
        method: 'messaging.communication.doctype.notification_rule.notification_rule.configuration',
        args: {source_application: frm.doc.source_application, source_doctype: frm.doc.source_doctype, account: frm.doc.account}
    });
    if (revision !== frm.notification_revision) return;
    frm.notification_loading = true;
    try {
        frm.notification_modules = data.modules;
        frm.notification_fields = data.condition_fields;
        frm.notification_process_fields = data.process_fields;
        frm.notification_events = data.events;
        frm.notification_source ??= frm.doc.source_doctype;
        frm.set_df_property('source_application', 'options', ['', ...data.applications]);
        Object.assign(frm.doc, {channel_context: data.channel || null, provider_context: data.provider || null});
        frm.refresh_fields(['channel_context', 'provider_context']);
        frm.toggle_display('redirect_recipient', data.redirect_supported);
        const info = frm.fields_dict.delivery_information.$wrapper.empty();
        if (data.delivery_information) $('<p>').text(data.delivery_information).appendTo(info);
        frm.notification_redirect_required = data.redirect_required;
        await frappe.require('/assets/messaging/js/provider_parameters.js');
        await messaging.provider_parameters.render(frm);
        lock_rule_structure(frm);
        messaging.notification_conditions.refresh(frm);
    } finally { frm.notification_loading = false; }
}

async function synchronize_notification_process(frm) {
    const selected = frm.doc.process_definition;
    const document = frm.doc;
    if (!selected) return;
    const signature = () => JSON.stringify({contract: frm.doc.process_contract, method: frm.doc.process_method,
        parameters: (frm.doc.process_parameters || []).map(row => [row.parameter_name, row.internal_type, row.required, row.multiple, row.value_source, row.fixed_value || '', row.source_field || '']),
        outputs: (frm.doc.outputs || []).map(row => [row.status_code, row.template_reference || '', row.send, row.output_fields])});
    const before = signature();
    const revision = frm.notification_contract_revision = (frm.notification_contract_revision || 0) + 1;
    const {message} = await frappe.call({method: 'messaging.communication.doctype.notification_rule.contract.discover',
        type: 'POST', args: {candidate: JSON.parse(JSON.stringify(frm.doc))}});
    if (frm.doc !== document || revision !== frm.notification_contract_revision || frm.doc.process_definition !== selected || signature() !== before) return;
    for (const field of ['process_parameters', 'outputs']) {
        const identity = field === 'outputs' ? 'status_code' : 'parameter_name';
        const existing = new Map((frm.doc[field] || []).map(row => [row[identity], row]));
        frm.doc[field] = message[field].map((row, index) => {
            const retained = existing.get(row[identity]);
            if (retained) {
                const values = {...row};
                for (const key of ['name', 'parent', 'parenttype', 'parentfield', 'doctype']) delete values[key];
                Object.assign(retained, values); return retained;
            }
            const values = {...row};
            for (const key of ['name', 'parent', 'parenttype', 'parentfield', 'doctype']) delete values[key];
            return frm.add_child(field, {...values, idx: index + 1});
        });
    }
    Object.assign(frm.doc, {process_contract: message.process_contract, process_method: message.process_method});
    const hasParams = Boolean(message.process_parameters.length) || Boolean(JSON.parse(message.process_contract).configurable_parameters);
    frm.toggle_display('section_break_parameters', hasParams);
    frm.toggle_display('process_parameters', hasParams);
    for (const field of ['process_parameters', 'outputs']) {
        const grid = frm.fields_dict[field].grid;
        grid.cannot_add_rows = field !== 'process_parameters' || !JSON.parse(message.process_contract).configurable_parameters;
        frm.refresh_field(field);
    }
    if (signature() !== before) frm.dirty();
}
function notification_process_row(frm, row) {
    const table = row.grid.df.fieldname;
    if (table === 'process_parameters') {
        const options = (frm.notification_process_fields || []).map(field => ({value: field.fieldname, label: __(field.label)}));
        const contract = JSON.parse(frm.doc.process_contract || '{}');
        const declared = contract.parameters?.find(item => item.name === row.doc.parameter_name && item.origin === 'declared');
        const editable = Boolean(contract.configurable_parameters) && !declared;
        for (const name of ['parameter_name', 'internal_type', 'required', 'multiple']) {
            const field = row.docfields.find(item => item.fieldname === name);
            if (field) field.read_only = !editable;
            row.toggle_editable(name, editable);
            const control = row.grid_form?.fields_dict[name];
            if (control) { control.df.read_only = !editable; control.refresh(); }
        }
        row.on_grid_fields_dict.source_field?.set_data(options);
        row.grid_form?.fields_dict.source_field?.set_data(options);
        messaging.notification_parameters?.render_row(frm, row);
    }

}
frappe.ui.form.on('Notification Rule', {
    setup(frm) {
        $(frm.wrapper).on('grid-row-render.notification_process', (_event, row) => notification_process_row(frm, row));
    },
    async refresh(frm) {
        const contract = JSON.parse(frm.doc.process_contract || '{}');
        const hasParams = Boolean(frm.doc.process_parameters?.length) || Boolean(contract.configurable_parameters);
        frm.toggle_display('section_break_parameters', hasParams);
        frm.toggle_display('process_parameters', hasParams);
        const configurable = Boolean(contract.configurable_parameters);
        const grid = frm.fields_dict.process_parameters.grid;
        grid.cannot_add_rows = !configurable;
        frm.fields_dict.outputs.grid.cannot_add_rows = true;
    },
    process_definition: synchronize_notification_process
});
frappe.ui.form.on('Notification Outcome', {
    form_render(frm, _cdt, cdn) { notification_process_row(frm, frm.fields_dict.outputs.grid.grid_rows_by_docname[cdn]); }
});
frappe.ui.form.on('Notification Process Parameter', {
    value_source(frm, cdt, cdn) {
        const row = locals[cdt][cdn];
        if (row.value_source === 'Fixed Value') {
            frappe.model.set_value(cdt, cdn, 'source_field', '');
        } else {
            frappe.model.set_value(cdt, cdn, 'fixed_value', '');
        }
        frm.fields_dict.process_parameters?.grid?.refresh();
    },
    internal_type(frm) { frm.refresh_field('process_parameters'); },
    multiple(frm) { frm.refresh_field('process_parameters'); },
    form_render(frm, _cdt, cdn) { notification_process_row(frm, frm.fields_dict.process_parameters.grid.grid_rows_by_docname[cdn]); }
});
