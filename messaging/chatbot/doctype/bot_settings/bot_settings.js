frappe.ui.form.on("Bot Settings", {
	setup(frm) {
		frm.set_query("active_ai_configuration", () => ({ filters: { enabled: 1, profile_type: "Decision", validation_status: "operational" } }));
		frm.set_query("language", "chatbot_languages", () => ({ filters: { enabled: 1 } }));
	},
});

frappe.ui.form.on("Bot Language", {
	language(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		const enabled = (frm.doc.chatbot_languages || []).filter(language => language.enabled);
		if (row.language && row.enabled && enabled.length === 1 && !enabled.some(language => language.is_default)) {
			row.is_default = 1;
			frm.refresh_field("chatbot_languages");
		}
	},
	is_default(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.is_default) return;
		// Update all rows synchronously so repeated events cannot retain two defaults.
		for (const language of frm.doc.chatbot_languages || []) {
			language.is_default = language.name === cdn ? 1 : 0;
		}
		row.enabled = 1;
		frm.dirty();
		frm.refresh_field("chatbot_languages");
	},
	enabled(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (row.is_default && !row.enabled) {
			row.enabled = 1;
			frm.refresh_field("chatbot_languages");
			frappe.msgprint(__("Select another default Chatbot Language before disabling this language."));
		}
	},
});

// Each HTML field edits one message in the existing flat child table.
const SETTINGS_MESSAGE_CARDS = {
    informative: [
        {field: 'informative_message_cards', key: 'informative_message'},
        {field: 'informative_categories_header_card', key: 'informative_categories_header'},
    ],
    attempts: [{field: 'attempts_exhausted_message_card', key: 'attempts_exhausted_message'}],
    security: [{field: 'general_message_cards', key: 'default_internal_error_message'}],
    courtesy: [{field: 'quota_courtesy_message_card', key: 'quota_courtesy_message'}],
    human: [
        {field: 'human_message_cards', key: 'human_disabled_message'},
        {field: 'human_active_message_card', key: 'human_active_message'},
        {field: 'human_completed_message_card', key: 'human_completed_message'},
    ],
};

async function render_settings_cards(frm, sections) {
    const doc = frm.doc;
    const request = Symbol('settings_cards');
    frm.settings_cards_requests ||= {};
    for (const section of sections) frm.settings_cards_requests[section] = request;
    const [, {message: required}] = await Promise.all([
        frappe.require(['/assets/messaging/js/translation_cards.js', '/assets/messaging/css/translation_cards.css']),
        frappe.call({method: 'messaging.chatbot.doctype.bot_settings.bot_settings.message_requirements', type: 'POST',
            args: {candidate: JSON.stringify(doc)}}),
    ]);
    if (frm.doc !== doc) return;
    return Promise.all(sections.map(section => {
        if (frm.settings_cards_requests[section] !== request) return;
        return Promise.all(SETTINGS_MESSAGE_CARDS[section].map(({field, key}) => messaging.translation_cards.render(frm, {
            wrapper: frm.fields_dict[field]?.$wrapper, wrapperField: field, id: key, table: 'message_translations',
            languages: doc.chatbot_languages || [], identity: {message_key: key},
            requiredFields: required.includes(key) ? ['message'] : [],
            visible: !['human_active_message', 'human_completed_message'].includes(key) || Boolean(doc.human_attention_enabled),
        })));
    }));
}
function refresh_settings_cards(frm) {
    return render_settings_cards(frm, Object.keys(SETTINGS_MESSAGE_CARDS));
}
frappe.ui.form.on('Bot Settings', {
    refresh: refresh_settings_cards,
    informative_show_available_categories: frm => render_settings_cards(frm, ['informative']),
});
frappe.ui.form.on('Bot Language', {
    language: refresh_settings_cards,
    enabled: refresh_settings_cards,
    is_default: refresh_settings_cards,
    chatbot_languages_add: refresh_settings_cards,
    chatbot_languages_remove: refresh_settings_cards,
});

const FLOW_CONSUMERS = {
    registration: {enabled: 'registration_enabled', states: 'registration_states', fields: 'registration_fields'},
    greeting: {enabled: 'greeting_enabled', states: 'greeting_states'},
    human: {enabled: 'human_attention_enabled', states: 'human_transfer_states', fields: 'human_fields'},
};

const CONSUMER_STATUS = {
    disabled: 'Disabled', operational: 'Operational', incomplete: 'Incomplete configuration',
    unavailable: 'Dependency unavailable', technical_required: 'Technical validation required',
    internal_error: 'Verification error', checking: 'Checking',
};

function consumer_signature(frm, consumer) {
    const config = FLOW_CONSUMERS[consumer];
    const keys = [config.enabled, consumer + '_flow', config.states, config.fields,
        'chatbot_languages', 'max_invalid_attempts', 'show_menu_on_attempts_exhausted'];
    if (consumer === 'registration') keys.push('exempt_registration_from_quotas', 'max_registration_exempt_messages');
    if (consumer === 'greeting') keys.push('registration_flow', 'human_flow');
    if (consumer === 'human') keys.push('human_support_week', 'human_inactivity_minutes', 'human_requests_required');
    const tables = {
        [config.states]: ['state'],
        chatbot_languages: ['language', 'enabled', 'is_default'],
        human_support_week: ['weekday', 'enabled', 'start_time', 'end_time'],
    };
    if (config.fields) tables[config.fields] = consumer === 'human' ? ['field_key', 'label', 'keep', 'states'] : ['field_key', 'label', 'keep'];
    return JSON.stringify(keys.filter(Boolean).map(key => tables[key] ?
        (frm.doc[key] || []).map(row => tables[key].map(field => row[field] ?? null)) : frm.doc[key]));
}

function render_consumer_diagnostics(frm) {
    const escape = value => frappe.utils.escape_html(String(value ?? ''));
    const problems = [];
    for (const [consumer, config] of Object.entries(FLOW_CONSUMERS)) {
        const diagnostic = frm.consumer_diagnostics?.[consumer];
        if (!diagnostic) continue;
        const label = diagnostic.label || __(consumer === 'human' ? 'Human Support' : consumer === 'greeting' ? 'Greeting' : 'Registration');
        const status = diagnostic.state === 'checking' ? '' : __(CONSUMER_STATUS[diagnostic.state]);
        frm.consumer_descriptions ||= {};
        const field = consumer + '_flow';
        if (!(field in frm.consumer_descriptions)) frm.consumer_descriptions[field] = frm.fields_dict[field]?.df.description || '';
        frm.set_df_property(field, 'description', [frm.consumer_descriptions[field], escape(status)].filter(Boolean).join('<br>'));
        if (!frm.doc[config.enabled]) continue;
        const issues = diagnostic.issues || [];
        if (issues.length) problems.push(`<p><strong>${escape(label)} — ${escape(diagnostic.flow_label || diagnostic.flow || '')}</strong></p>` +
            issues.map(issue => {
                const fieldLabel = frm.fields_dict[issue.field]?.df.label || issue.field;
                return `<p>${escape(__(fieldLabel))}: ${escape(issue.message)}<br>${escape(__('Action'))}: ${escape(issue.action)}` +
                    (issue.reference ? `<br>${escape(__('Error reference'))}: ${escape(issue.reference)}` : '') + '</p>';
            }).join(''));
    }
    const count = Object.values(frm.consumer_diagnostics || {}).reduce((total, diagnostic) => total + (diagnostic.issues || []).length, 0);
    const header = count ? `<strong>${escape(count === 1 ? __('Bot Settings has one configuration problem.') :
        __('Bot Settings has {0} configuration problem(s).', [count]))}</strong>` : '';
    frm.set_intro(header + problems.join(''), 'red');
}

function selector_input(control) {
    const input = control?.$input;
    return input ? input[0] || input : null;
}

function reveal_flow_selector(control) {
    const awesomplete = control?.awesomplete;
    if (!awesomplete || typeof awesomplete.evaluate !== 'function') return;
    let focused = false;
    if (typeof control.$input?.is === 'function') focused = Boolean(control.$input.is(':focus'));
    if (!focused && typeof document !== 'undefined') {
        const node = control.input || control.$input?.[0];
        focused = Boolean(node && document.activeElement === node);
    }
    if (focused) awesomplete.evaluate();
}

function configure_consumer_selector(frm, fieldname, consumer) {
    const control = frm.fields_dict[fieldname];
    const input = selector_input(control);
    if (!input || control.flow_selector_input === input) return;
    control.flow_selector_input = input;
    control.flow_selector_request = Symbol(fieldname);
    if (fieldname !== consumer + '_flow') control.df.only_select = true;
    control.on_input = async function (event) {
        const term = event ? event.target.value : this.$input.val();
        const args = this.get_search_args(term);
        if (!args) return;
        const doc = frm.doc;
        const selected = doc[consumer + '_flow'];
        const request = Symbol(fieldname);
        this.flow_selector_request = request;
        const current = () => frm.doc === doc && this.flow_selector_request === request &&
            doc[consumer + '_flow'] === selected;
        try {
            const {message} = await frappe.call({method: 'frappe.desk.search.search_link',
                type: 'POST', no_spinner: true, args});
            if (!current()) return;
            const entries = this.merge_duplicates(message);
            if (!this.df.only_select && frappe.model.can_create(args.doctype)) entries.push({
                label: __('Create a new {0}', [__(args.doctype)]), value: 'create_new__link_option', action: this.new_doc});
            if (!this.df.only_select) entries.push({label: __('Advanced Search'),
                value: 'advanced_search__link_option', action: this.open_advanced_search});
            this.awesomplete.list = entries;
            reveal_flow_selector(this);
            for (const item of message) frappe.utils.add_link_title(args.doctype, item.value, item.label);
        } catch (_) {
            if (current()) {
                this.awesomplete.list = [];
                reveal_flow_selector(this);
            }
        }
    };
    // Native Link binds its original debounced method when creating the input.
    control.$input.off('input', control._debounced_input_handler);
    control.$input.off('focus.flow_options');
    control._debounced_input_handler = frappe.utils.debounce(control.on_input.bind(control), 500);
    control.$input.on('input', control._debounced_input_handler);
    control.$input.on('focus.flow_options', () => {
        // Native Link already searches immediately when the input is empty.
        // A filled control still searches the full list on click, without waiting for typing.
        if (control.$input.val()) control.on_input({target: {value: ''}});
    });
}

function refresh_consumer(frm, consumer, changedFlow = false, reconcileFields = false, force = false) {
    const config = FLOW_CONSUMERS[consumer];
    frm.consumer_checks ||= {};
    frm.consumer_diagnostics ||= {};
    if (changedFlow) {
        const selector = frm.fields_dict?.[config.states];
        if (selector) {
            selector.flow_selector_request = Symbol('flow_changed');
            if (selector.$input) selector.$input.cache = {};
            if (selector.awesomplete) selector.awesomplete.list = [];
        }
        frm.clear_table(config.states);
        if (config.fields) frm.clear_table(config.fields);
        frm.refresh_fields([config.states, config.fields].filter(Boolean));
        frm.dirty();
    }
    const doc = frm.doc;
    const signature = consumer_signature(frm, consumer);
    const previous = frm.consumer_checks[consumer];
    if (!force && previous?.doc === doc && previous.signature === signature) return previous.promise;
    const check = {doc, signature};
    frm.consumer_checks[consumer] = check;
    const current = () => frm.doc === doc && frm.consumer_checks[consumer] === check &&
        consumer_signature(frm, consumer) === signature;
    if (!doc[config.enabled]) {
        frm.consumer_diagnostics[consumer] = {state: 'disabled', issues: []};
        check.promise = Promise.resolve();
        render_consumer_diagnostics(frm);
        return check.promise;
    }
    const prior = previous?.doc === doc && previous.signature === signature ? frm.consumer_diagnostics[consumer] : null;
    frm.consumer_diagnostics[consumer] = {...(prior || {}), state: 'checking',
        flow: doc[consumer + '_flow'], issues: prior?.issues || []};
    render_consumer_diagnostics(frm);
    check.pending = true;
    check.promise = (async () => {
        try {
            // Use the same bounded, local AJAX pattern as Bot Flow lifecycle operations.
            // Generic Frappe HTTP handlers otherwise open raw diagnostic modals.
            const response = await $.ajax({
                url: '/api/method/messaging.chatbot.doctype.bot_settings.flow_consumers.diagnose', type: 'POST',
                data: {candidate: JSON.stringify(doc), consumer}, dataType: 'json',
                headers: {'X-Frappe-CSRF-Token': frappe.csrf_token}, timeout: 20000,
            });
            const message = response?.message;
            if (response?.exc || response?.exception) throw Error('Bot Settings diagnostic request failed');
            if (!current()) return;
            if (!message || message.consumer !== consumer || !Array.isArray(message.issues) ||
                !Object.hasOwn(CONSUMER_STATUS, message.state) || ['checking', 'disabled'].includes(message.state) || message.flow !== doc[consumer + '_flow'] ||
                message.issues.some(issue => !issue || typeof issue.field !== 'string' ||
                    typeof issue.message !== 'string' || typeof issue.action !== 'string') ||
                (message.state === 'operational' && message.issues.length) ||
                (!['operational', 'disabled'].includes(message.state) && !message.issues.length)) {
                throw Error('Invalid Bot Settings diagnostic response');
            }
            frm.consumer_diagnostics[consumer] = message;
            if (reconcileFields && config.fields && Array.isArray(message.catalog?.fields)) {
                const prior = new Map((doc[config.fields] || []).map(row => [row.field_key, row]));
                const fields = message.catalog.fields.map(item => ({field_key: item.field_key,
                    label: prior.get(item.field_key)?.label ?? item.field_key.replaceAll('_', ' '),
                    ...(consumer === 'human' ? {states: item.states.join(', ')} : {}), keep: prior.get(item.field_key)?.keep || 0}));
                const values = rows => JSON.stringify(rows.map(row => [row.field_key, row.label, row.states, row.keep || 0]));
                if (values(fields) !== values(doc[config.fields] || [])) {
                    frm.clear_table(config.fields);
                    for (const row of fields) frm.add_child(config.fields, row);
                    frm.refresh_field(config.fields);
                    frm.dirty();
                    return refresh_consumer(frm, consumer);
                }
            }
        } catch (error) {
            if (!current()) return;
            frm.consumer_diagnostics[consumer] = {state: 'internal_error', flow: doc[consumer + '_flow'],
                issues: [{field: consumer + '_flow', kind: 'internal_error', severity: 'error',
                    message: __('The verification request failed. Flow availability cannot be confirmed.'),
                    action: __('Check the connection and server error log, then reload the form to retry.')}]};
            console.error('Bot Settings verification request failed:', consumer, error?.status || error?.name || 'Error');
        } finally {
            check.pending = false;
            if (frm.doc === doc && frm.consumer_checks[consumer] === check) render_consumer_diagnostics(frm);
        }
    })();
    return check.promise;
}

async function check_consumers_before_save(frm) {
    validate_human_timeout(frm);
    const consumers = Object.keys(FLOW_CONSUMERS);
    for (let attempt = 0; attempt < 3; attempt++) {
        const doc = frm.doc;
        await Promise.all(consumers.map(async consumer => {
            const check = frm.consumer_checks?.[consumer];
            if (check?.pending && check.doc === doc && check.signature === consumer_signature(frm, consumer)) {
                await check.promise;
            } else {
                await refresh_consumer(frm, consumer, false, false,
                    frm.consumer_diagnostics?.[consumer]?.state === 'internal_error');
            }
        }));
        const current = frm.doc === doc && consumers.every(consumer => {
            const check = frm.consumer_checks?.[consumer];
            return check?.doc === doc && check.signature === consumer_signature(frm, consumer) && !check.pending;
        });
        if (!current) continue;
        render_consumer_diagnostics(frm);
        const invalid = Object.entries(FLOW_CONSUMERS).filter(([consumer, config]) =>
            frm.doc[config.enabled] && frm.consumer_diagnostics?.[consumer]?.state !== 'operational');
        if (invalid.length) {
            const escape = value => frappe.utils.escape_html(String(value ?? ''));
            frappe.throw(invalid.map(([consumer]) => {
                const diagnostic = frm.consumer_diagnostics?.[consumer];
                return escape(diagnostic?.label || consumer) + ': ' +
                    escape(diagnostic?.issues?.[0]?.message || __('The verification request failed. Flow availability cannot be confirmed.'));
            }).join('<br>'));
        }
        return;
    }
    frappe.throw(__('Flow configuration changed during verification. Check again before saving.'));
}

function validate_human_timeout(frm) {
    if (!frm.doc.human_attention_enabled) return;
    const raw = frm.human_timeout_input ?? frm.doc.human_inactivity_minutes;
    if (!/^[0-9]+$/.test(String(raw ?? '')) || Number(raw) < 1) {
        frappe.throw(__('Human inactivity timeout must be an integer of at least one minute.'));
    }
}

function prepare_human_schedule(frm) {
    if (!frm.doc.human_attention_enabled || frm.doc.human_flow !== 'messaging_human_availability' ||
        (frm.doc.human_support_week || []).length) return;
    for (const weekday of ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']) {
        frm.add_child('human_support_week', {weekday, enabled: 0});
    }
    frm.refresh_field('human_support_week');
    frm.dirty();
}

frappe.ui.form.on('Bot Settings', {
    setup(frm) {
        for (const [consumer, config] of Object.entries(FLOW_CONSUMERS)) {
            frm.set_query(consumer + '_flow', () => ({query: 'messaging.chatbot.doctype.bot_settings.flow_consumers.flow_options'}));
            frm.fields_dict[config.states].df.only_select = true;
            frm.set_query(config.states, () => ({
                query: 'messaging.chatbot.doctype.bot_settings.flow_consumers.state_options',
                filters: {flow: frm.doc[consumer + '_flow'] || ''},
            }));
        }
    },
    refresh(frm) {
        if (frappe.user.has_role('System Manager')) frm.add_custom_button(__('Add Default Flows'), async () => {
            await frappe.call({
                method: 'messaging.chatbot.doctype.bot_settings.bot_settings.validate_flow_preset_languages',
                type: 'POST',
                args: {pending_languages: frm.is_dirty() && (frm.doc.chatbot_languages || []).some(row => row.language)},
            });
            await frappe.require(['/assets/messaging/js/presets.js', '/assets/messaging/css/presets.css']);
            return messaging.presets.open(() => frm.reload_doc());
        });
        for (const [consumer, config] of Object.entries(FLOW_CONSUMERS)) {
            configure_consumer_selector(frm, consumer + '_flow', consumer);
            configure_consumer_selector(frm, config.states, consumer);
        }
        for (const config of Object.values(FLOW_CONSUMERS)) {
            if (!config.fields) continue;
            const grid = frm.fields_dict[config.fields].grid;
            grid.cannot_add_rows = true;
            grid.df.cannot_add_rows = true;
            grid.df.cannot_delete_rows = true;
            grid.refresh();
        }
        frm.fields_dict.human_inactivity_minutes.$input?.off('input.human_timeout').on('input.human_timeout', function () {
            frm.human_timeout_input = this.value;
        });
        frm.human_timeout_input = null;
        return Promise.all(Object.keys(FLOW_CONSUMERS).map(key => refresh_consumer(frm, key, false, false, true)));
    },
    registration_flow: frm => Promise.all([refresh_consumer(frm, 'registration', true), refresh_consumer(frm, 'greeting')]),
    greeting_flow: frm => refresh_consumer(frm, 'greeting', true),
    human_flow(frm) {
        prepare_human_schedule(frm);
        return Promise.all([refresh_consumer(frm, 'human', true), refresh_consumer(frm, 'greeting')]);
    },
    registration_states: frm => refresh_consumer(frm, 'registration', false, true),
    greeting_states: frm => refresh_consumer(frm, 'greeting', false, true),
    human_transfer_states: frm => refresh_consumer(frm, 'human', false, true),
    registration_enabled: frm => refresh_consumer(frm, 'registration'),
    greeting_enabled: frm => refresh_consumer(frm, 'greeting'),
    async human_attention_enabled(frm) {
        prepare_human_schedule(frm);
        if (frm.doc.human_attention_enabled && !frm.doc.human_inactivity_minutes) {
            frm.human_timeout_input = null;
            await frm.set_value('human_inactivity_minutes', 1);
        }
        return refresh_consumer(frm, 'human');
    },
    before_save: check_consumers_before_save,
});
frappe.ui.form.on('Flow Consumer State', {
    state(frm, cdt, cdn) {
        const parentfield = locals[cdt][cdn].parentfield;
        const entry = Object.entries(FLOW_CONSUMERS).find(([, config]) => config.states === parentfield);
        if (entry) return refresh_consumer(frm, entry[0], false, true);
    },
    registration_states_add: frm => refresh_consumer(frm, 'registration'),
    registration_states_remove: frm => refresh_consumer(frm, 'registration', false, true),
    greeting_states_add: frm => refresh_consumer(frm, 'greeting'),
    greeting_states_remove: frm => refresh_consumer(frm, 'greeting'),
    human_transfer_states_add: frm => refresh_consumer(frm, 'human'),
    human_transfer_states_remove: frm => refresh_consumer(frm, 'human', false, true),
});
frappe.ui.form.on('Registration Field', {
    keep: frm => refresh_consumer(frm, 'registration'),
    label: frm => refresh_consumer(frm, 'registration'),
});

frappe.ui.form.on('Bot Settings', {
    interpretation_enabled: refresh_settings_cards,
    registration_enabled: refresh_settings_cards,
    greeting_enabled: refresh_settings_cards,
    human_attention_enabled: refresh_settings_cards,
    show_menu_on_attempts_exhausted: refresh_settings_cards,
    exempt_registration_from_quotas: refresh_settings_cards,
});

frappe.ui.form.on('Human Request Field', {
    keep: frm => refresh_consumer(frm, 'human'),
    label: frm => refresh_consumer(frm, 'human'),
});

function refresh_all_consumers(frm) {
    return Promise.all(Object.keys(FLOW_CONSUMERS).map(consumer => refresh_consumer(frm, consumer)));
}
frappe.ui.form.on('Bot Language', {
    language: refresh_all_consumers, enabled: refresh_all_consumers, is_default: refresh_all_consumers,
    chatbot_languages_add: refresh_all_consumers, chatbot_languages_remove: refresh_all_consumers,
});
frappe.ui.form.on('Bot Settings', {
    max_invalid_attempts: refresh_all_consumers,
    show_menu_on_attempts_exhausted: refresh_all_consumers,
    exempt_registration_from_quotas: frm => refresh_consumer(frm, 'registration'),
    max_registration_exempt_messages: frm => refresh_consumer(frm, 'registration'),
    human_inactivity_minutes: frm => refresh_consumer(frm, 'human'),
    human_requests_required: frm => refresh_consumer(frm, 'human'),
});
frappe.ui.form.on('Human Support Day', {
    enabled: frm => refresh_consumer(frm, 'human'),
    start_time: frm => refresh_consumer(frm, 'human'),
    end_time: frm => refresh_consumer(frm, 'human'),
});
