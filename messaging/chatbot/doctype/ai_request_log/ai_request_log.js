frappe.ui.form.on('AI Request Log', {
    refresh(frm) {
        frm.disable_save();
        frm.set_read_only();
        if (!frm.is_new()) {
            const title = [frm.doc.provider, __(frm.doc.operation)].filter(Boolean).join(' · ');
            if (title) frm.page.set_title(title);
            frm.page.set_title_sub(frm.doc.name);
            frm.page.set_indicator(__(frm.doc.status), frm.doc.status === 'Completed' ? 'green' : 'red');
        }
        const wrapper = frm.fields_dict.diagnostics_html?.$wrapper;
        if (!wrapper) return;
        wrapper.empty();
        // Restricted fields are removed by Frappe before this form reaches the browser.
        if (!frappe.perm.has_perm(frm.doctype, 1, 'read') || !frm.doc.result_summary) return;
        let evidence;
        try { evidence = JSON.parse(frm.doc.result_summary); }
        catch { evidence = {}; }
        const section = title => $('<section class="mb-5">').append($('<h4>').text(__(title))).appendTo(wrapper);
        const text = (parent, value) => $('<p class="text-muted">').text(__(value)).appendTo(parent);
        const json = (parent, title, value, open = false) => {
            const details = $('<details class="mb-3">').prop('open', open).appendTo(parent);
            $('<summary class="mb-2">').text(__(title)).appendTo(details);
            const shown = JSON.stringify(value, null, 2);
            $('<button type="button" class="btn btn-xs btn-default mb-2">').text(__('Copy JSON'))
                .on('click', () => frappe.utils.copy_to_clipboard(shown)).appendTo(details);
            const code = $('<code class="language-json">').text(shown);
            $('<pre>').css({maxHeight: '440px', overflow: 'auto', whiteSpace: 'pre'})
                .append(code).appendTo(details);
        };
        const rows = (parent, entries) => {
            const wrap = $('<div class="table-responsive">').appendTo(parent);
            const table = $('<table class="table table-bordered table-sm">').appendTo(wrap);
            const body = $('<tbody>').appendTo(table);
            for (const [key, value] of entries) {
                const row = $('<tr>').appendTo(body);
                $('<th>').text(__(key)).appendTo(row);
                $('<td>').text(value === undefined || value === null ? '—' :
                    (typeof value === 'object' ? JSON.stringify(value) : String(value))).appendTo(row);
            }
        };
        const inspect = (parent, value) => {
            const details = $('<details class="mb-3">').appendTo(parent);
            $('<summary class="mb-2">').text(__('Individual results and technical references')).appendTo(details);
            const search = $('<input type="search" class="form-control mb-2">')
                .attr('placeholder', __('Filter by JSON path or value')).appendTo(details);
            const scroll = $('<div>').css({maxHeight: '400px', overflow: 'auto'}).appendTo(details);
            const entries = [];
            const walk = (item, path) => {
                if (entries.length >= 1000) return;
                if (item !== null && typeof item === 'object') {
                    for (const [key, child] of Object.entries(item)) walk(child, `${path}[${JSON.stringify(key)}]`);
                } else entries.push([path, item]);
            };
            walk(value, '$');
            rows(scroll, entries);
            if (entries.length >= 1000) text(details, 'Table limited to 1000 values. The available JSON remains accessible above.');
            search.on('input', () => {
                const query = search.val().toLowerCase();
                scroll.find('tr').each(function () { $(this).toggle($(this).text().toLowerCase().includes(query)); });
            });
        };
        const provider = section('Provider JSON response');
        text(provider, 'Protected evidence: [redacted] marks private fields, free text or secret reflections. JSON paths and numeric values do not imply an intent or a per-Flow confidence.');
        if (evidence.provider_response !== undefined && evidence.provider_response !== null) {
            json(provider, 'Available protected JSON', evidence.provider_response, true);
            inspect(provider, {response: evidence.provider_response, context: evidence.provider_context});
        } else text(provider, 'No original provider response was retained for this request.');
        if (evidence.response_status) rows(provider, [['Response retention', evidence.response_status]]);
        if (evidence.provider_context) json(provider, 'Provider question context', evidence.provider_context);
        if (evidence.context_status) rows(provider, [['Context retention', evidence.context_status]]);
        const normalized = section('Normalized decision');
        const result = evidence.normalized_response;
        if (result) {
            const decision = result.decision;
            if (decision) rows(normalized, [
                ['Outcome', decision.outcome], ['Global confidence', decision.confidence],
                ['Recognized Flows', decision.flows], ['Excluded Flows', decision.excluded_flows],
                ['Greeting detected', decision.greeting_detected], ['Human attention requested', decision.human_attention_requested],
                ['Schema version', result.schema_version], ['Request ID', result.request_id],
            ]);
            json(normalized, 'Normalized response JSON', result, true);
        } else text(normalized, 'No validated normalized response is available.');
        if (evidence.normalized_status) rows(normalized, [['Normalized response retention', evidence.normalized_status]]);
        if (evidence.contract_validation) rows(normalized, [['Contract validation', evidence.contract_validation]]);
        const technical = section('Technical diagnostics');
        rows(technical, [
            ['AI Provider', frm.doc.provider], ['AI Configuration', frm.doc.configuration],
            ['Requested Model', frm.doc.requested_model], ['Actual Model', frm.doc.actual_model],
            ['Operation', frm.doc.operation], ['Status', frm.doc.status], ['HTTP status', evidence.http_status],
            ['Latency (ms)', frm.doc.latency_ms], ['Request Transmission', frm.doc.transmission_state],
            ['Evaluation ID', frm.doc.evaluation_id], ['Request Key', frm.doc.request_key],
            ['Request Sequence', frm.doc.request_sequence], ['Chat Turn', frm.doc.turn],
            ['Safe Error Code', frm.doc.error_code], ['Exception type', evidence.exception_type],
        ]);
        if (frm.doc.error_log) $('<a>').attr('href', '/app/error-log/' + encodeURIComponent(frm.doc.error_log))
            .text(__('Error Log')).appendTo(technical);
        if (frm.doc.request_text) json(technical, 'Text Sent to AI', frm.doc.request_text);
        const metadata = Object.fromEntries(Object.entries(evidence).filter(([key]) => ![
            'provider_response', 'response_status', 'provider_context', 'context_status', 'normalized_response',
            'contract_validation', 'normalized_status', 'request_id', 'exception_type', 'http_status',
        ].includes(key)));
        if (Object.keys(metadata).length) json(technical, 'Additional provider metadata', metadata);
        // Frappe ships the JavaScript grammar, which also highlights JSON literals.
        frappe.require('syntax_highlighting.bundle.js').then(() => {
            wrapper.find('code.language-json').each(function () {
                $(this).html(window.hljs.highlight($(this).text(), {language: 'javascript'}).value);
            });
        });
    },
});
