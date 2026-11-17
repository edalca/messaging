frappe.provide('messaging.presets');

messaging.presets.action = function(row) {
    if (!row.selectable && (row.status === 'Unavailable' || row.diagnostic)) return 'Unavailable';
    if (row.requires_reset) return 'Restore';
    if (!row.existing || messaging.presets._list(row, 'pending_languages').length) return 'Add';
    return 'Updated';
};

messaging.presets.details = function(row) {
    const parts = [];
    const missing = messaging.presets._list(row, 'missing_content');
    if (messaging.presets._list(row, 'unavailable_languages').length) {
        parts.push({kind: 'unavailable', languages: row.unavailable_languages});
    }
    if (missing.length) parts.push({kind: 'missing', languages: missing});
    else if (row.diagnostic) parts.push({kind: 'diagnostic', text: row.diagnostic});
    return parts;
};

messaging.presets.visible = function(presets, filters) {
    const query = String((filters && filters.query) || '').trim().toLowerCase();
    const app = filters && filters.app || '';
    const action = filters && filters.action || '';
    return (presets || []).filter(row => {
        if (app && row.app !== app) return false;
        if (action && messaging.presets.action(row) !== action) return false;
        if (!query) return true;
        return String(row.name || '').toLowerCase().includes(query)
            || String(row.process || '').toLowerCase().includes(query);
    });
};

messaging.presets.groups = function(rows) {
    const order = [];
    const grouped = new Map();
    for (const row of rows || []) {
        if (!grouped.has(row.app)) {
            grouped.set(row.app, []);
            order.push(row.app);
        }
        grouped.get(row.app).push(row);
    }
    return order.map(app => ({app, rows: grouped.get(app)}));
};

messaging.presets.createSelection = function() {
    const keys = new Set();
    return {
        has(key) { return keys.has(key); },
        size() { return keys.size; },
        list() { return [...keys]; },
        toggle(row, on) {
            if (!row || !row.selectable) return false;
            if (on) keys.add(row.key);
            else keys.delete(row.key);
            return true;
        },
        setRows(rows, on) {
            for (const row of rows || []) this.toggle(row, on);
        },
    };
};

messaging.presets._list = function(row, key) {
    const value = row && row[key];
    return Array.isArray(value) ? value : [];
};

messaging.presets.open = async function(afterApply) {
    const {message: data} = await frappe.call({
        method: 'messaging.chatbot.doctype.bot_flow_presets.bot_flow_presets.list_presets',
        type: 'GET',
    });
    const esc = frappe.utils.escape_html;
    const manualLabel = value => value === 'common' ? __('Common configuration') : value;
    const actionLabel = {
        Add: __('Add preset'),
        Restore: __('Restore preset'),
        Updated: __('Preset updated'),
        Unavailable: __('Unavailable'),
    };
    const selected = messaging.presets.createSelection();
    const collapsed = new Set();
    const dialog = new frappe.ui.Dialog({
        title: __('Add or Restore Default Flows'),
        size: 'extra-large',
        fields: [],
        no_submit_on_enter: true,
        primary_action_label: __('Add or restore selected'),
        secondary_action_label: __('Cancel'),
        secondary_action() { dialog.hide(); },
        async primary_action() {
            const keys = selected.list().filter(key => data.presets.some(row => row.key === key && row.selectable));
            if (!keys.length) {
                frappe.msgprint(__('Select at least one preset.'));
                return;
            }
            const chosen = data.presets.filter(row => keys.includes(row.key));
            const restoring = chosen.filter(row => row.requires_reset);
            if (restoring.length) {
                const confirmed = await new Promise(resolve => frappe.confirm(
                    __('Restore the selected preset versions? This replaces the listed changed configuration and administrative edits and requires validation again.') +
                    '<ul>' + restoring.map(row => '<li>' + esc(row.name) + ': ' +
                        esc([row.common_changed ? __('Common configuration') : '', ...row.changed_languages,
                            ...row.manual_changes.map(manualLabel)].filter(Boolean).join(', ')) + '</li>').join('') + '</ul>',
                    () => resolve(true), () => resolve(false)));
                if (!confirmed) return;
            }
            dialog.get_primary_btn().prop('disabled', true);
            try {
                const {message: result} = await frappe.call({
                    method: 'messaging.chatbot.doctype.bot_settings.bot_settings.apply_flow_presets',
                    type: 'POST',
                    args: {
                        keys,
                        reset_keys: restoring.map(row => row.key),
                        expected_versions: Object.fromEntries(chosen.map(row => [row.key, row.version])),
                    },
                    freeze: true,
                });
                dialog.hide();
                const groups = [['created', __('Created')], ['completed', __('Completed')],
                    ['existing', __('Existing')], ['not_applied', __('Not applied')]];
                frappe.msgprint({title: __('Preset provisioning result'), message: groups.map(([key, label]) =>
                    `<h5>${esc(label)} (${result[key].length})</h5>` + result[key].map(row =>
                        `<p>${esc(row.key)}: ${esc(row.message || row.name || '')}` +
                        (row.warnings || []).map(text => `<br>${esc(text)}`).join('') +
                        (row.unavailable_languages?.length ? `<br>${esc(__('Default content unavailable'))}: ${esc(row.unavailable_languages.join(', '))}` : '') + '</p>'
                    ).join('')).join('')});
                await afterApply?.();
            } finally {
                dialog.get_primary_btn().prop('disabled', false);
            }
        },
    });
    dialog.$wrapper.addClass('messaging-preset-dialog').css('overflow', 'hidden');
    dialog.$body.addClass('hide');
    const intro = $('<div class="messaging-preset-intro">').appendTo(dialog.header);
    $('<p class="messaging-preset-description">').text(
        __('Select default flows to add or restore for the active languages.')).appendTo(intro);
    const active = (data.languages || []).join(', ') || '—';
    $('<p class="messaging-preset-languages">').text(
        __('Active languages') + ': ' + active + ' · ' + __('Default language') + ': ' + (data.default_language || '—')
    ).appendTo(intro);
    const count = $('<span class="messaging-preset-selection">').appendTo(dialog.custom_actions);
    const catalogue = $('<div class="messaging-preset-catalogue">').appendTo(dialog.modal_body);
    const toolbar = $('<div class="messaging-preset-toolbar">').appendTo(catalogue);
    const searchSlot = filterSlot(__('Search')).appendTo(toolbar);
    const appSlot = filterSlot(__('Application')).appendTo(toolbar);
    const statusSlot = filterSlot(__('Status')).appendTo(toolbar);
    const scroll = $('<div class="messaging-preset-scroll">').appendTo(catalogue);
    const apps = [...new Set((data.presets || []).map(row => row.app))];
    const search = frappe.ui.form.make_control({
        parent: searchSlot.find('.messaging-preset-filter-control'),
        only_input: true,
        df: {fieldtype: 'Data', fieldname: 'preset_search', placeholder: 'Name or Process'},
        render_input: true,
    });
    search.$input.attr('aria-label', __('Search'));
    const appFilter = frappe.ui.form.make_control({
        parent: appSlot.find('.messaging-preset-filter-control'),
        only_input: true,
        df: {
            fieldtype: 'Select', fieldname: 'preset_app',
            options: [{value: '', label: 'All'}, ...apps.map(app => ({value: app, label: app}))],
        },
        render_input: true,
    });
    appFilter.$input.attr('aria-label', __('Application'));
    const statusFilter = frappe.ui.form.make_control({
        parent: statusSlot.find('.messaging-preset-filter-control'),
        only_input: true,
        df: {
            fieldtype: 'Select', fieldname: 'preset_status',
            options: [
                {value: '', label: 'All'},
                {value: 'Add', label: 'Add preset'},
                {value: 'Restore', label: 'Restore preset'},
                {value: 'Updated', label: 'Preset updated'},
                {value: 'Unavailable', label: 'Unavailable'},
            ],
        },
        render_input: true,
    });
    statusFilter.$input.attr('aria-label', __('Status'));
    search.$input.on('input', render);
    appFilter.$input.on('change', render);
    statusFilter.$input.on('change', render);

    function filterSlot(label) {
        const slot = $('<label class="messaging-preset-filter">');
        $('<span>').text(label).appendTo(slot);
        $('<div class="messaging-preset-filter-control">').appendTo(slot);
        return slot;
    }

    function updateCount() {
        count.text(__('{0} selected', [selected.size()]));
    }

    function languageText(codes) {
        return codes && codes.length ? codes.join(', ') : '—';
    }

    function detailText(parts) {
        return parts.map(part => {
            if (part.kind === 'unavailable') return __('Unavailable contributions') + ': ' + part.languages.join(', ');
            if (part.kind === 'missing') return __('Default content unavailable') + ': ' + part.languages.join(', ');
            return part.text || '';
        }).filter(Boolean).join(' · ');
    }

    function syncGroup(group, rows) {
        const box = group.find('.messaging-preset-group-check');
        const selectable = rows.filter(row => row.selectable);
        const chosen = selectable.filter(row => selected.has(row.key)).length;
        box.prop('checked', selectable.length > 0 && chosen === selectable.length);
        box.prop('indeterminate', chosen > 0 && chosen < selectable.length);
        box.prop('disabled', !selectable.length);
    }

    function render() {
        const rows = messaging.presets.visible(data.presets || [], {
            query: search.get_value(),
            app: appFilter.get_value(),
            action: statusFilter.get_value(),
        });
        const groups = messaging.presets.groups(rows);
        scroll.empty();
        if (!groups.length) {
            $('<p class="messaging-preset-empty-state">').text(data.presets && data.presets.length
                ? __('No presets match this search.')
                : __('No presets are available from installed applications.')).appendTo(scroll);
            return;
        }
        const columns = [
            ['name', __('Flow name')],
            ['process', __('Process')],
            ['action', __('Action')],
            ['installed', __('Installed languages')],
            ['available', __('Available languages')],
            ['existing', __('Existing Bot Flow')],
        ];
        for (const group of groups) {
            const details = $('<details class="messaging-preset-group">').attr('data-app', group.app).appendTo(scroll);
            if (!collapsed.has(group.app)) details.prop('open', true);
            const summary = $('<summary>').appendTo(details);
            const heading = $('<span class="messaging-preset-summary">').appendTo(summary);
            $('<span class="messaging-preset-app">').text(group.app).appendTo(heading);
            $('<span class="messaging-preset-count">').text(String(group.rows.length))
                .attr('aria-label', __('{0} flows', [group.rows.length])).appendTo(heading);
            details[0].addEventListener('toggle', () => {
                if (details.prop('open')) collapsed.delete(group.app);
                else collapsed.add(group.app);
            });
            const table = $('<table class="messaging-preset-table">').appendTo(details);
            const head = $('<tr>').appendTo($('<thead>').appendTo(table));
            const groupCheck = $('<input type="checkbox" class="messaging-preset-group-check">')
                .attr('aria-label', __('Select visible presets'));
            $('<th class="messaging-preset-check">').append(groupCheck).appendTo(head);
            for (const [, label] of columns) $('<th>').text(label).appendTo(head);
            const body = $('<tbody>').appendTo(table);
            for (const row of group.rows) {
                const action = messaging.presets.action(row);
                const line = $('<tr>').attr('data-key', row.key).appendTo(body);
                const checkCell = $('<td class="messaging-preset-check" data-label="">').appendTo(line);
                const input = $('<input type="checkbox">').val(row.key).attr('aria-label', row.name)
                    .prop('checked', selected.has(row.key)).prop('disabled', !row.selectable).appendTo(checkCell);
                input.on('change', () => {
                    selected.toggle(row, input.prop('checked'));
                    if (!row.selectable) input.prop('checked', false);
                    updateCount();
                    syncGroup(details, group.rows);
                });
                $('<td data-label="' + esc(__('Flow name')) + '">').append($('<span class="messaging-preset-name">').text(row.name)).appendTo(line);
                $('<td class="messaging-preset-process">').attr('data-label', __('Process')).text(row.process).appendTo(line);
                $('<td>').attr('data-label', __('Action')).append(
                    $('<span class="messaging-preset-action">').toggleClass('is-updated', action === 'Updated').text(actionLabel[action] || action)
                ).appendTo(line);
                $('<td>').attr('data-label', __('Installed languages')).text(languageText(row.installed_languages)).appendTo(line);
                $('<td>').attr('data-label', __('Available languages')).text(languageText(row.declared_languages)).appendTo(line);
                const existing = $('<td>').attr('data-label', __('Existing Bot Flow')).appendTo(line);
                if (row.existing) {
                    const path = frappe.router.slug('Bot Flow');
                    $('<a class="messaging-preset-link">')
                        .attr('href', '/app/' + path + '/' + encodeURIComponent(row.existing))
                        .text(row.existing).appendTo(existing);
                } else {
                    $('<span class="messaging-preset-muted">').text('—').appendTo(existing);
                }
                const extra = detailText(messaging.presets.details(row));
                if (extra) {
                    $('<tr class="messaging-preset-detail-row">').append(
                        $('<td class="messaging-preset-detail" colspan="7">').text(extra)
                    ).appendTo(body);
                }
            }
            groupCheck.on('change', () => {
                const on = groupCheck.prop('checked');
                selected.setRows(group.rows, on);
                body.find('input[type="checkbox"]').each(function() {
                    const row = group.rows.find(item => item.key === this.value);
                    if (row && row.selectable) this.checked = selected.has(row.key);
                });
                updateCount();
                syncGroup(details, group.rows);
            });
            syncGroup(details, group.rows);
        }
    }

    updateCount();
    render();
    dialog.show();
};
