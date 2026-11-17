// Native controls over localized child rows. The parent form owns every save.
frappe.provide('messaging.translation_cards');

(() => {
    messaging.translation_cards.help = function (key) {
        return {
            informative_message: __('Shown when the subscriber\'s request is unclear. Write a brief guide that helps them explain what they need or choose an available option.'),
            informative_categories_header: __('Shown before the available categories in the menu. Write a short introduction inviting the subscriber to select an option.'),
            attempts_exhausted_message: __('Shown when the maximum number of invalid capture attempts is reached. Explain that the request could not be completed and guide the subscriber on what to do next.'),
            default_internal_error_message: __('Shown when a technical failure prevents a response. Write a safe, reassuring message without stack traces, internal paths, credentials, or technical details.'),
            internal_error_message: __('Replaces the default safe internal error message for this Flow when custom messaging is enabled. Write a safe response to technical failures without exposing internal details.'),
            quota_courtesy_message: __('Shown as the courtesy response when an automatic response quota is reached. Briefly explain that automatic assistance is temporarily limited and suggest what the subscriber can do next.'),
            human_disabled_message: __('Shown when human support is disabled or cannot be used under the current system conditions. Explain its unavailability and suggest an available way to continue.'),
            human_active_message: __('Shown after the request is assigned to human support. Confirm the assignment and explain that an agent will continue the conversation.'),
            human_completed_message: __('Shown after human support ends manually or due to inactivity. Confirm that attention has ended and explain how the subscriber can contact support again.'),
            input_initial_message: __('Shown when this Flow starts capturing parameters. Introduce the request and explain what information the subscriber should provide; parameter questions are configured separately.'),
            input_continuation_message: __('Shown when this Flow continues capturing pending parameters. Write a short transition asking the subscriber to provide the remaining information.'),
            semantic_description: __('Used to identify requests that belong to this Flow. Describe its purpose and the needs it handles; this context is not sent to the subscriber.'),
            menu_label: __('Shown when this Flow is offered in the category menu. Write a short, recognizable label and, if needed, a brief description of the option.'),
            process_message: __('This output sends the non-empty message returned by the business Process for this functional status. Document the expected content in the notes; the subscriber response is supplied by the Process.'),
        }[key];
    };

    messaging.translation_cards.render = async function (frm, config) {
        const {wrapper, languages, table, identity = {}, requiredFields = []} = config;
        if (!wrapper) return;
        const doc = frm.doc;
        const token = Symbol('localized-editor');
        wrapper.addClass('messaging-localized-host input-max-width').data('translation-render', token).empty();
        if (config.visible === false) return;
        const current = () => frm.doc === doc && wrapper[0].isConnected
            && wrapper.data('translation-render') === token;
        const enabled = languages.filter(row => row.enabled && row.language)
            .filter((row, index, rows) => rows.findIndex(other => other.language === row.language) === index);
        if (!enabled.length) {
            $('<p class="text-muted small">').text(__('Add a Chatbot Language in General before editing messages.')).appendTo(wrapper);
            return;
        }
        const childtype = frm.fields_dict[table].df.options;
        const codes = enabled.map(row => row.language).sort();
        const signature = JSON.stringify(codes);
        if (frm.translation_language_names?.signature !== signature) {
            frm.translation_language_names = {signature, promise: frappe.db.get_list('Language', {
                filters: {name: ['in', codes]}, fields: ['name', 'language_name'], limit: codes.length,
            })};
        }
        const [, records] = await Promise.all([
            frappe.model.with_doctype(childtype), frm.translation_language_names.promise,
        ]);
        if (!current()) return;
        const meta = frappe.get_meta(childtype);
        const fieldnames = config.fields || meta.fields.filter(field =>
            ['Data', 'Small Text'].includes(field.fieldtype) && field.fieldname !== 'language'
            && !(field.fieldname in identity)).map(field => field.fieldname);
        const fields = fieldnames.map(fieldname => meta.fields.find(field => field.fieldname === fieldname));
        if (frm.translation_editor_state?.doc !== doc) frm.translation_editor_state = {doc, languages: {}};
        const selections = frm.translation_editor_state.languages;
        if (!enabled.some(row => row.language === selections[config.id])) {
            selections[config.id] = (enabled.find(row => row.is_default) || enabled[0]).language;
        }
        const editor = $('<div class="messaging-localized-editor" role="group">')
            .attr('data-localized-editor', config.id).appendTo(wrapper);
        // A single keyed message uses its HTML field label without repeating the generic column label.
        const messageLabel = fields.length === 1 && Object.keys(identity).length
            ? __(frm.fields_dict[config.wrapperField].df.label, null, frm.doctype) : null;
        if (messageLabel) editor.attr('aria-label', messageLabel);
        const controls = $('<div class="messaging-localized-controls">').appendTo(editor);
        const selector = $('<div class="messaging-localized-languages" role="group">')
            .attr('aria-label', __('Message language')).appendTo(editor);
        const editable = !frm.read_only && frm.perm.some(perm => perm.write);
        const findRow = language => (doc[table] || []).find(row => row.language === language
            && Object.entries(identity).every(([key, value]) => row[key] === value));

        function paint() {
            const language = selections[config.id];
            const isDefault = enabled.some(row => row.language === language && row.is_default);
            controls.empty();
            selector.empty();
            for (const field of fields) {
                const control = frappe.ui.form.make_control({parent: controls, render_input: true,
                    df: {...field, label: messageLabel || field.label, read_only: field.read_only || !editable,
                        reqd: field.reqd || (isDefault && requiredFields.includes(field.fieldname))}});
                control.set_input(findRow(language)?.[field.fieldname] ?? '');
                // Keep native parsing and validation, and write only actual edits to the child model.
                control.set_model_value = value => {
                    if (!current() || !editable || control.df.read_only) return Promise.resolve();
                    let row = findRow(language);
                    if ((row?.[field.fieldname] ?? '') === value) return Promise.resolve();
                    if (!row) row = frm.add_child(table, {language, ...identity});
                    control.value = value;
                    const changed = frappe.model.set_value(row.doctype, row.name, field.fieldname, value);
                    frm.dirty();
                    return changed;
                };
                control.change = event => {
                    if (!current() || !controls[0].contains(control.wrapper) || control.df.read_only) return;
                    return control.parse_validate_and_set_in_model(control.get_input_value(), event);
                };
                control.$input?.attr('aria-label', `${messageLabel || __(field.label, null, childtype)} · ${language}`)
                    .on('input.localized_editor', event => control.change(event));
                if (field.fieldtype === 'Small Text') control.$input?.attr('rows', 3).css('height', '');
            }
            for (const row of enabled) {
                const label = __(records.find(record => record.name === row.language)?.language_name || row.language);
                const selected = language === row.language;
                const chip = $('<button type="button" class="messaging-localized-language">')
                    .attr({'data-language': row.language, 'aria-pressed': String(selected), title: label})
                    .toggleClass('is-selected', selected).text(label).appendTo(selector);
                if (row.is_default) $('<span>').text(' · ' + __('Default')).appendTo(chip);
                chip.on('click', () => {
                    if (!current() || selections[config.id] === row.language) return;
                    selections[config.id] = row.language;
                    paint();
                    selector.find('button').filter((_, button) => button.dataset.language === row.language).trigger('focus');
                });
            }
        }
        paint();
    };
})();
