frappe.ui.form.on('AI Evaluation Run', {
    async refresh(frm) {
        frm.disable_save();
        frm.set_intro(frm.doc.invalidated_at ? __('Technical evaluation evidence was invalidated. Run a new evaluation.') : __(frm.doc.diagnostic || frm.doc.status));
        const base = 'messaging.chatbot.doctype.ai_evaluation_run.runtime.';
        const render = async () => {
            if (cur_frm !== frm || cur_frm.doc.name !== frm.doc.name) return;
            const {message: state} = await frappe.call({method: base + 'progress', args: {run_name: frm.doc.name}});
            const escape = frappe.utils.escape_html;
            const technical = state.technical || {};
            const functional = state.functional || {};
            const lines = [
                __('Approval policy: {0}.', [state.policy || '']),
                __('Technical validation: {0}/{1}.', [technical.valid || 0, technical.executed || 0]),
                __('Functional evaluation: {0}/{1}.', [functional.matched || 0, functional.applicable || 0]),
                __('Cases outside the operational route: {0}.', [functional.not_applicable || 0]),
                __('Technical errors: {0}.', [technical.invalid || 0]),
                __('Functional discrepancies: {0}.', [functional.differed || 0]),
                `${__(state.status)}: ${state.executed}/${state.selected}`
            ];
            frm.fields_dict.results_panel.$wrapper.html(`<p>${lines.map(escape).join('<br>')}</p>
                <div class="table-responsive"><table class="table table-bordered"><thead><tr>${['Case Template', 'Case ID', 'Language', 'Group', 'Status', 'Technical Valid', 'Functional Valid', 'Diagnostic'].map(x => `<th>${escape(__(x))}</th>`).join('')}</tr></thead><tbody>
                ${state.results.map(r => `<tr><td>${escape(r.case_template)}</td><td><a href="/desk/ai-evaluation-result/${encodeURIComponent(r.name)}">${escape(r.case_id)}</a></td><td>${escape(r.language)}</td><td>${escape(r.group)}</td><td>${escape(__(r.status))}</td><td>${escape(r.technical_valid ? __('Yes') : __('No'))}</td><td>${escape(r.functional_valid ? __('Yes') : __('No'))}</td><td>${escape(__(r.diagnostic || ''))}</td></tr>`).join('')}</tbody></table></div>`);
            if (['Pending', 'Running'].includes(state.status)) {
                clearTimeout(frm._evaluation_poll);
                frm._evaluation_poll = setTimeout(render, 5000);
            }
        };
        clearTimeout(frm._evaluation_poll);
        await render();
        frm.add_custom_button(__('Refresh results'), () => frm.reload_doc());
        if (['Pending', 'Running'].includes(frm.doc.status)) {
            frm.add_custom_button(__('Close interrupted evaluation'), async () => {
                await frappe.call({method: base + 'interrupt', type: 'POST', args: {run_name: frm.doc.name}});
                frm.reload_doc();
            });
        } else {
            frm.add_custom_button(__('New evaluation'), () => frappe.set_route('Form', 'AI Configuration', frm.doc.configuration));
            frm.add_custom_button(__('Reproduce selection'), () => frappe.confirm(__('Reproduction invokes the configured provider again and may incur usage.'), async () => {
                const manifest = typeof frm.doc.manifest === 'string' ? JSON.parse(frm.doc.manifest) : frm.doc.manifest;
                const {message: run} = await frappe.call({method: base + 'request_evaluation', type: 'POST', args: {
                    configuration: frm.doc.configuration, reproduces: frm.doc.name,
                    optional: JSON.stringify(manifest.templates.filter(r => !r.mandatory).map(r => r.name))}});
                frappe.set_route('Form', 'AI Evaluation Run', run.name);
            }));
        }
    }
});
