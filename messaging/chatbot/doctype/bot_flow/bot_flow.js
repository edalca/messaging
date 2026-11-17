function invalidate_process_contract(frm) {
    // Invalidate the draft synchronously, before discovery can yield to another selection.
    frm.flow_lifecycle_request = Symbol('definition_changed');
    for (const table of ['parameters', 'outputs', 'output_translations']) frm.clear_table(table);
    Object.assign(frm.doc, {process_method: '', process_contract: '', process_contract_fingerprint: '',
        process_test_status: 'Untested', process_test_fingerprint: '', process_test_evidence: '',
        process_test_checked_at: null, process_test_checked_by: null, process_test_log: null});
    frm.flow_lifecycle_button?.prop('disabled', true);
    show_flow_lifecycle_state(frm, 'Untested');
    frm.dirty();
    frm.refresh_fields(['parameters', 'outputs', 'process_method', 'process_contract']);
    sync_flow_parameter_state(frm);
}

async function synchronize_process_contract(frm, discovered) {
    const selected = frm.doc.process_definition;
    const doc = frm.doc;
    const request = Symbol('process_contract');
    frm.flow_contract_request = request;
    frm.flow_contract_error = false;
    frm.flow_contract_pending = true;
    const current = () => frm.doc === doc && frm.doc.process_definition === selected && frm.flow_contract_request === request;
    try {
        const message = discovered || (await frappe.call({
            method: 'messaging.chatbot.doctype.bot_flow.contract.discover', type: 'POST',
            args: {candidate: JSON.parse(JSON.stringify(frm.doc))}
        })).message;
        if (!current()) return;
        if (message.definition !== selected) throw new Error('Invalid discovery definition');
        // Apply one response without asynchronous field events interleaving its rows.
        for (const table of ['parameters', 'outputs']) {
            const existing = new Map((frm.doc[table] || []).map(row => [row.name, row]));
            const projected = message[table].map((row, index) => {
                const retained = row.name && existing.get(row.name);
                const values = {...row};
                for (const key of ['name', 'idx', 'doctype', 'parent', 'parenttype', 'parentfield', '__islocal', '__unsaved']) delete values[key];
                const target = retained ? Object.assign(retained, values) : frm.add_child(table, values);
                target.idx = index + 1;
                return target;
            });
            frm.doc[table] = projected;
        }
        Object.assign(frm.doc, {process_method: message.process_method, process_contract: message.process_contract,
            process_contract_fingerprint: message.process_contract_fingerprint});
        frm.dirty();
        frm.refresh_fields(['parameters', 'outputs', 'process_method', 'process_contract']);
        sync_flow_parameter_state(frm);
    } catch (error) {
        if (!current()) return;
        frm.flow_contract_error = true;
        frappe.msgprint({title: __('Process discovery failed'), indicator: 'red',
            message: __('The selected Process contract could not be discovered. Select the definition again before saving.')});
    } finally {
        if (current()) frm.flow_contract_pending = false;
    }
}

function flow_has_captures(doc) {
    if ((doc.parameters || []).length) return true;
    try { return (JSON.parse(doc.process_contract || '{}').parameters || []).length > 0; }
    catch { return true; }
}

function sync_flow_parameter_state(frm) {
	const has_params = Array.isArray(frm.doc.parameters) && frm.doc.parameters.length > 0;
	if (Boolean(frm.doc.has_parameters) !== has_params) {
		frm.set_value("has_parameters", has_params ? 1 : 0);
	}
	if (!has_params && frm.doc.priority !== 0) {
		frm.set_value("priority", 0);
	} else if (has_params && (!frm.doc.priority || frm.doc.priority <= 0)) {
		frm.set_value("priority", 1);
	}
    const grid = frm.fields_dict.parameters?.grid;
    if (grid) grid.cannot_add_rows = !uses_configurable_parameters(frm);
    const captures = has_params || flow_has_captures(frm.doc);
    frm.toggle_display(['initial_message_card', 'continuation_message_card'], captures);
}

function uses_configurable_parameters(frm) {
    try { return Boolean(JSON.parse(frm.doc.process_contract || '{}').configurable_parameters); }
    catch { return false; }
}

frappe.ui.form.on("Bot Flow", {
	setup(frm) {
        $(frm.wrapper).on('grid-row-render.process_inputs', (_event, row) => configure_flow_input_row(frm, row));
		frm.set_query("process_definition", () => ({
            query: "messaging.chatbot.doctype.bot_flow.bot_flow.available_process_definitions",
            filters: {flow: frm.is_new() ? null : frm.doc.name},
        }));
		frm.set_query("retry_parameter", "outputs", () => ({
			filters: { name: ["in", (frm.doc.parameters || []).map(row => row.parameter)] },
		}));
		frm.set_query("parameter", "parameters", (_doc, cdt, cdn) => {
            const row = locals[cdt][cdn];
            const types = {string: ['String', 'Name', 'Expression'], integer: ['Int'], number: ['Decimal'], boolean: ['Boolean'], date: ['Date']};
            if (uses_configurable_parameters(frm) && !declared_flow_input(frm, row)) return {filters: {enabled: 1}};
            return {filters: {enabled: 1, parameter_type: ['in', types[row.internal_type] || []]}};
        });
	},
	async process_definition(frm) {
        if (frm.flow_contract_owner?.doc === frm.doc && frm.flow_contract_owner.definition === frm.doc.process_definition && !frm.flow_contract_error) return;
        frm.flow_contract_owner = {doc: frm.doc, definition: frm.doc.process_definition};
        frm.flow_contract_request = Symbol('selection_changed');
        frm.flow_contract_pending = false;
        frm.flow_contract_error = false;
        invalidate_process_contract(frm);
        if (frm.doc.process_definition) await synchronize_process_contract(frm);
        await render_flow_translation_cards(frm);
    },
    async validate(frm) {
        if (!frm.doc.show_in_menu) return;
        const settings = await frappe.db.get_doc('Bot Settings');
        const language = (settings.chatbot_languages || []).find(row => row.enabled && row.is_default)?.language;
        const label = (frm.doc.translations || []).find(row => row.language === language)?.menu_label;
        if (!language || !label?.trim()) {
            frappe.throw(__('Menu Label is required in the default enabled language when Show in Menu / Catalog is enabled.'));
        }
    },
    before_save(frm) {
        if (frm.flow_contract_pending || frm.flow_contract_error) {
            frappe.throw(__('Wait for successful Process discovery before saving.'));
        }
    },
	refresh(frm) {
        if (frm.flow_contract_owner?.doc !== frm.doc) {
            frm.flow_contract_owner = {doc: frm.doc, definition: frm.doc.process_definition};
            frm.flow_contract_pending = false;
            frm.flow_contract_error = false;
        }
		sync_flow_parameter_state(frm);
		for (const table of ['parameters', 'outputs']) {
			frm.fields_dict[table].grid.cannot_add_rows = table !== 'parameters' || !uses_configurable_parameters(frm);
			if (table === 'outputs') frm.fields_dict[table].grid.update_docfield_property('validation_status', 'hidden', 1);
			frm.refresh_field(table);
		}
		render_flow_translation_cards(frm);
		refresh_flow_lifecycle(frm);
	},
    show_in_menu: render_flow_translation_cards,
	priority(frm) {
		sync_flow_parameter_state(frm);
	},
    use_custom_internal_error_message: render_flow_translation_cards,
});

frappe.ui.form.on("Flow Parameter", {
	parameters_add(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const count = (frm.doc.parameters || []).length;
		if (!row.capture_order || (row.capture_order === 1 && count > 1)) {
			frappe.model.set_value(cdt, cdn, "capture_order", count);
		}
		sync_flow_parameter_state(frm);
	},
	parameters_remove(frm) {
		sync_flow_parameter_state(frm);
	},
    form_render(frm, cdt, cdn) { configure_flow_input_row(frm, frm.fields_dict.parameters.grid.grid_rows_by_docname[cdn]); },
	parameter(frm) {
		if (uses_configurable_parameters(frm)) return synchronize_process_contract(frm);
	},

});

frappe.ui.form.on("Flow Output", {
	flow_action(frm, cdt, cdn) {
		if (locals[cdt][cdn].flow_action !== "Retry Parameter") {
			frappe.model.set_value(cdt, cdn, "retry_parameter", null);
		}
	},
	output_mode(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.output_mode === "Message") {
			frappe.model.set_value(cdt, cdn, "require_non_empty_process_message", 1);
		} else {
			frappe.model.set_value(cdt, cdn, "require_non_empty_process_message", 0);
		}
	},
});

frappe.ui.form.on('Bot Flow', {
    setup(frm) {
        frm.set_query('language', 'translations', language_query);
        frm.set_query('language', 'output_translations', language_query);
    }
});
function language_query() {
    return {query: 'messaging.chatbot.doctype.bot_parameter.bot_parameter.language_options'};
}

// These controls edit the parent's child tables; only the parent document saves.
async function render_translation_editor(frm, cdt, cdn, kind) {
    const row = locals[cdt][cdn];
    const config = {
        source: 'outputs', table: 'output_translations', key: 'status_code',
        field: 'language_templates_editor', value: 'template_reference', label: 'Bot Flow Template', type: 'Link'
    };
    const gridRow = frm.fields_dict[config.source].grid.grid_rows_by_docname[cdn];
    const wrapper = gridRow?.grid_form?.fields_dict[config.field]?.$wrapper;
    if (!wrapper) return;
    const notes = gridRow.grid_form.fields_dict.process_message_notes;
    if (notes && globalThis.messaging?.translation_cards) {
        notes.set_description(messaging.translation_cards.help('process_message'));
    }
    const renderToken = Symbol();
    wrapper.data('translation-render', renderToken).empty();
    if (kind === 'output' && row.output_mode !== 'Template') return;
    const settings = await frappe.db.get_doc('Bot Settings');
    if (!wrapper[0].isConnected || locals[cdt][cdn] !== row || wrapper.data('translation-render') !== renderToken) return;
    const enabled = settings.chatbot_languages.filter(item => item.enabled).map(item => item.language);
    const editable = !frm.read_only && frm.perm.some(perm => perm.write);
    const table = $('<table class="table table-bordered">').appendTo(wrapper);
    const head = $('<tr>').appendTo($('<thead>').appendTo(table));
    $('<th>').text(__('Language')).css('width', '30%').appendTo(head);
    $('<th>').text(__(config.label)).appendTo(head);
    const body = $('<tbody>').appendTo(table);
    const translations = (frm.doc[config.table] || []).filter(item => item[config.key] === row[config.key]);
    for (const translation of translations) {
        const tr = $('<tr>').attr('data-translation', translation.name).appendTo(body);
        const languageCell = $('<td>').appendTo(tr);
        const valueCell = $('<td>').appendTo(tr);
        const retained = Boolean(translation.language && !enabled.includes(translation.language));
        const languageControl = frappe.ui.form.make_control({
            parent: languageCell,
            df: {fieldname: 'language', fieldtype: 'Link', options: 'Language', label: __('Language'),
                read_only: !editable || retained, only_select: true,
                get_query: language_query,
                change: async () => {
                    const value = languageControl.get_value();
                    if (value === translation.language) return;
                    if (value && (!enabled.includes(value) || (frm.doc[config.table] || []).some(item =>
                        item.name !== translation.name && item[config.key] === row[config.key] && item.language === value))) {
                        frappe.show_alert({message: __('Select a unique enabled language for each translation.'), indicator: 'orange'});
                        await languageControl.set_value(translation.language || '');
                        return;
                    }
                    await frappe.model.set_value(translation.doctype, translation.name, 'language', value);
                    valueControl.df.read_only = !editable || !value;
                    valueControl.refresh();
                }}, render_input: true
        });
        const valueControl = frappe.ui.form.make_control({
            parent: valueCell,
            df: {fieldname: config.value, fieldtype: config.type, options: config.type === 'Link' ? 'Bot Flow Template' : undefined,
                label: __(config.label), read_only: !editable || !translation.language, only_select: true,
                get_query: () => ({filters: {enabled: 1, language: translation.language || ''}}),
                change: () => {
                    const value = valueControl.get_value();
                    if (value !== (translation[config.value] || '')) {
                        return frappe.model.set_value(translation.doctype, translation.name, config.value, value);
                    }
                }}, render_input: true
        });
        languageControl.set_value(translation.language || '');
        valueControl.set_value(translation[config.value] || '');
        languageCell.find('.control-label').hide();
        valueCell.find('.control-label').hide();
        if (retained) $('<small class="text-muted">').text(__('Disabled language: retained for reference.')).appendTo(languageCell);
        if (editable) $('<button type="button" class="btn btn-xs btn-default">').text(__('Remove'))
            .appendTo(languageCell).on('click', () => {
                frappe.model.clear_doc(translation.doctype, translation.name);
                frm.dirty();
                render_translation_editor(frm, cdt, cdn, kind);
            });
    }
    if (editable) $('<button type="button" class="btn btn-xs btn-default">').text(__('Add Row'))
        .appendTo(wrapper).on('click', () => {
            if (translations.some(item => !item.language)) return;
            frm.add_child(config.table, {[config.key]: row[config.key], language: ''});
            frm.dirty();
            render_translation_editor(frm, cdt, cdn, kind);
        });
}

frappe.ui.form.on('Flow Output', {
    form_render: (frm, cdt, cdn) => render_translation_editor(frm, cdt, cdn, 'output'),
    output_mode: (frm, cdt, cdn) => render_translation_editor(frm, cdt, cdn, 'output')
});


function flow_lifecycle_error(error) {
    const response = error?.responseJSON || error;
    const administrative = ['ValidationError', 'PermissionError', 'TimestampMismatchError',
        'MandatoryError', 'LinkValidationError'].includes(response?.exc_type);
    if (administrative) {
        try {
            const messages = JSON.parse(response._server_messages || '[]');
            if (messages.length) return JSON.parse(messages[messages.length - 1]).message;
        } catch (_) { /* Fall back to the administrative exception text. */ }
        if (typeof response.exception === 'string') return response.exception.replace(/^[^:]+:\s*/, '');
    }
    return __('The operation could not be completed. Reload the Flow to check its current state, then try again.');
}

function show_flow_lifecycle_error(error) {
    frappe.msgprint({title: __('Flow operation failed'), indicator: 'red',
        message: frappe.utils.escape_html(flow_lifecycle_error(error))});
}

async function flow_lifecycle_call(method, args, load_document = false) {
    // Frappe's generic 500 handler opens raw diagnostic details. Handle this
    // request locally so technical failures always receive a safe message.
    const response = await $.ajax({url: '/api/method/' + method, type: 'POST',
        data: Object.fromEntries(Object.entries(args).map(([key, value]) =>
            [key, typeof value === 'object' ? JSON.stringify(value) : value])),
        dataType: 'json', headers: {'X-Frappe-CSRF-Token': frappe.csrf_token}, timeout: 30000});
    if (!response || response.exc || response.exception ||
        (load_document ? !Array.isArray(response.docs) : !response.message)) throw response;
    return load_document ? response : response.message;
}

function show_flow_lifecycle_state(frm, state) {
    if (!['Untested', 'Disabled', 'Enabled', 'Unavailable'].includes(state)) throw new Error('Invalid Flow state');
    frm.doc.status = state;
    frm.refresh_field('status');
    frm.page.set_indicator(__(state, null, 'Bot Flow'),
        state === 'Enabled' ? 'green' : state === 'Disabled' ? 'gray' : 'orange');
}

async function refresh_flow_lifecycle(frm) {
    const owner = 'messaging.chatbot.doctype.bot_flow.bot_flow.';
    const request = Symbol('flow_lifecycle');
    frm.flow_lifecycle_request = request;
    frm.flow_lifecycle_button?.remove();
    if (frm.flow_lifecycle_label) {
        frm.page.menu.find(`[data-label="${encodeURIComponent(frm.flow_lifecycle_label)}"]`).closest('li').remove();
    }
    frm.flow_lifecycle_button = null;
    frm.page.set_primary_action(__('Save'), () => frm.save(), 'save');
    frm.page.btn_primary?.prop('disabled', !frm.is_dirty() || frm.flow_lifecycle_operation?.doc === frm.doc);
    if (frm.is_new() || !frappe.user.has_role('System Manager')) return;
    const name = frm.doc.name;
    const doc = frm.doc;
    const route = frappe.get_route_str();
    const current = () => frm.doc === doc && frappe.get_route_str() === route;
    let message;
    try {
        message = doc.__onload?.flow_lifecycle || await flow_lifecycle_call(owner + 'state', {name});
        // Reuse only this load's response. A later refresh must verify again.
        // The current document load already includes its effective approval state.
        if (!current() || frm.flow_lifecycle_request !== request) return;
        show_flow_lifecycle_state(frm, message.lifecycle_status);
        if (message.verification_error) frm.set_intro(message.verification_error, 'orange');
    } catch (error) {
        if (current() && frm.flow_lifecycle_request === request) show_flow_lifecycle_error(error);
        return;
    }
    if (frm.doc.name !== name || frm.flow_lifecycle_request !== request) return;
    const state = message.lifecycle_status;
    if (state === 'Unavailable') return;
    const label = state === 'Untested' ? __('Validate') : state === 'Disabled' ? __('Activate') : __('Deactivate');
    const act = async () => {
        if (!current() || frm.flow_lifecycle_operation?.doc === doc) return;
        if (frm.is_new() || frm.is_dirty()) {
            frappe.msgprint(__('Save pending changes before validating or changing activation.'));
            return;
        }
        const args = {candidate: frm.doc};
        if (state !== 'Untested') args.active = state === 'Disabled' ? 1 : 0;
        const operation = {name, doc};
        frm.flow_lifecycle_operation = operation;
        const modified = frm.doc.modified;
        frm.flow_lifecycle_button?.prop('disabled', true).attr('aria-busy', 'true')
            .text(state === 'Untested' ? __('Validating...') : __('Updating state...'));
        frm.page.btn_primary?.prop('disabled', true);
        frm.set_intro(state === 'Untested' ? __('Validating...') : __('Updating state...'), 'blue');
        try {
            const result = await flow_lifecycle_call(
                owner + (state === 'Untested' ? 'validate_process' : 'set_active'), args);
            if (!current()) return;
            if (frm.is_dirty() || frm.doc.modified !== modified) {
                frappe.msgprint(__('The document changed during the operation. Reload it to check its current state.'));
                return;
            }
            if (state === 'Untested' && !['Passed', 'Failed'].includes(result.status)) throw new Error('Invalid validation response');
            if (!['Untested', 'Disabled', 'Enabled', 'Unavailable'].includes(result.lifecycle_status)) throw new Error('Invalid Flow state');
            if (result.status === 'Passed' && result.lifecycle_status !== 'Enabled') throw new Error('Invalid validation state');
            if (result.status === 'Failed') {
                frappe.msgprint({title: __('Flow validation failed.'), indicator: 'red',
                    message: frappe.utils.escape_html(result.execution_log
                        ? __('The Process could not be validated. Review Process Execution Log {0} for the technical diagnosis.', [result.execution_log])
                        : result.message || __('Flow validation failed.'))});
            }
            const saved = await flow_lifecycle_call('frappe.desk.form.load.getdoc', {doctype: frm.doctype, name}, true);
            if (!current()) return;
            if (frm.is_dirty() || frm.doc.modified !== modified) {
                frappe.msgprint(__('The document changed during the operation. Reload it to check its current state.'));
                return;
            }
            if (!saved.docs.some(item => item.doctype === frm.doctype && item.name === name)) throw new Error('Invalid Flow document');
            frappe.model.remove_from_locals(frm.doctype, name);
            frappe.model.sync(saved);
            await frm.refresh(name);
            if (frappe.get_route_str() !== route || frm.doc.name !== name) return;
            if (state === 'Untested' && result.status === 'Passed' && frm.doc.status === 'Enabled') {
                frappe.msgprint({title: __('Flow validated'), indicator: 'green',
                    message: __('The Flow was validated and enabled. Business logic was not executed.')});
            } else if (state === 'Untested' && result.status === 'Passed') {
                frappe.msgprint(__('The Flow is no longer validated. Review its configuration and validate it again.'));
            }
        } catch (error) {
            if (frappe.get_route_str() === route && frm.doc.name === name) show_flow_lifecycle_error(error);
        } finally {
            if (frm.flow_lifecycle_operation === operation) {
                frm.flow_lifecycle_operation = null;
                if (frappe.get_route_str() === route && frm.doc.name === name) {
                    frm.set_intro(frm.is_dirty() ? __('Save pending changes before validating or changing activation.') : '', 'orange');
                    if (frm.doc === doc) await refresh_flow_lifecycle(frm);
                }
            }
        }
    };
    frm.flow_lifecycle_label = label;
    frm.flow_lifecycle_button = frm.page.add_button(label, act, {btn_class: 'btn-default'});
    const dirty = () => {
        frm.flow_lifecycle_button?.prop('disabled', frm.is_dirty() || frm.flow_lifecycle_operation?.doc === frm.doc);
        if (frm.is_dirty()) frm.set_intro(__('Save pending changes before validating or changing activation.'), 'orange');
        else frm.set_intro(message.verification_error || '', 'orange');
    };
    frm.$wrapper.off('dirty.flow_lifecycle').on('dirty.flow_lifecycle', dirty);
    dirty();
}

async function render_flow_translation_cards(frm) {
    const doc = frm.doc;
    const captures = flow_has_captures(doc);
    frm.toggle_display(['initial_message_card', 'continuation_message_card'], captures);
    frm.toggle_display(['menu_order', 'menu_translation_card'], Boolean(doc.show_in_menu));
    const request = Symbol('flow_cards');
    frm.flow_cards_request = request;
    const [settings, , {message: required}] = await Promise.all([frappe.db.get_doc('Bot Settings'),
        frappe.require(['/assets/messaging/js/translation_cards.js', '/assets/messaging/css/translation_cards.css']),
        frappe.call({method: 'messaging.chatbot.doctype.bot_flow.bot_flow.message_requirements', type: 'POST',
            args: {candidate: JSON.stringify(doc)}})]);
    if (frm.doc !== doc || frm.flow_cards_request !== request) return;
    const requiredFields = keys => keys.filter(key => required.includes(key));
    const editors = [
        {id: 'semantic', wrapperField: 'translation_cards', table: 'translations',
            requiredFields: requiredFields(['semantic_description']), fields: ['semantic_description']},
        {id: 'initial', wrapperField: 'initial_message_card', table: 'input_translations',
            requiredFields: requiredFields(['input_initial_message']), fields: ['input_initial_message']},
        {id: 'continuation', wrapperField: 'continuation_message_card', table: 'input_translations',
            requiredFields: requiredFields(['input_continuation_message']), fields: ['input_continuation_message']},
        {id: 'menu', wrapperField: 'menu_translation_card', table: 'translations',
            requiredFields: doc.show_in_menu ? ['menu_label'] : [], visible: Boolean(doc.show_in_menu),
            fields: ['menu_label', 'menu_description']},
        {id: 'internal_error_message', wrapperField: 'internal_error_message', table: 'translations',
            requiredFields: requiredFields(['internal_error_message']),
            visible: Boolean(doc.use_custom_internal_error_message), fields: ['internal_error_message']},
    ];
    return Promise.all(editors.map(editor => messaging.translation_cards.render(frm, {
        ...editor, wrapper: frm.fields_dict[editor.wrapperField]?.$wrapper, languages: settings.chatbot_languages || [],
    })));
}


function apply_flow_outcome_contract(frm, gridRow) {
    if (gridRow.grid.df.fieldname !== 'outputs') return;
    const outcome = JSON.parse(frm.doc.process_contract || '{}').outcomes?.[gridRow.doc.status_code];
    if (!outcome) return;
    const options = outcome.mode.map(mode => mode[0].toUpperCase() + mode.slice(1));
    gridRow.docfields.find(field => field.fieldname === 'output_mode').options = options.join('\n');
    gridRow.toggle_editable('output_mode', options.length > 1);
    gridRow.toggle_editable('flow_action', false);
    const control = gridRow.grid_form?.fields_dict.output_mode;
    if (control) { control.df.options = options.join('\n'); control.df.read_only = options.length === 1; control.refresh(); }
}
frappe.ui.form.on('Bot Flow', {
    setup(frm) {
        $(frm.wrapper).on('grid-row-render.process_outcomes', (_event, row) => apply_flow_outcome_contract(frm, row));
    }
});


function declared_flow_input(frm, row) {
    return (JSON.parse(frm.doc.process_contract || '{}').parameters || [])
        .find(item => item.name === row.process_argument && item.origin === 'declared');
}
function configure_flow_input_row(frm, row) {
    if (row.grid.df.fieldname !== 'parameters') return;
    const editable = uses_configurable_parameters(frm) && !declared_flow_input(frm, row.doc);
    for (const name of ['process_argument', 'required', 'allow_multiple_values']) {
        const field = row.docfields.find(item => item.fieldname === name);
        if (field) field.read_only = !editable;
        row.toggle_editable(name, editable);
        const control = row.grid_form?.fields_dict[name];
        if (control) { control.df.read_only = !editable; control.refresh(); }
    }
}
