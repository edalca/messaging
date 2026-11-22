(() => {
    const method = "messaging.communication.doctype.communication_channel.public_information.";
    const documents = [
        { key: "privacy-policy", field: "public_privacy_draft", title: __("Privacy Policy") },
        { key: "terms-of-service", field: "public_terms_draft", title: __("Terms of Service") },
        { key: "data-deletion", field: "public_deletion_draft", title: __("Data Deletion Instructions") },
    ];

    function call(frm, action, args = {}, type = "POST") {
        return frappe.call({ method: method + action, type, args: { channel: frm.doc.name, ...args } });
    }

    function saved(frm) {
        if (!frm.is_dirty()) return true;
        frappe.msgprint(__("Save pending channel changes before continuing."));
        return false;
    }

    function editDraft(frm, { key, field, title }) {
        const dialog = new frappe.ui.Dialog({
            title,
            size: "large",
            fields: [{
                fieldname: "draft", fieldtype: "Text Editor", label: __("Draft Content"),
                get_toolbar_options: () => [
                    [{ header: [2, 3, 4, false] }],
                    ["bold", "italic", "underline", "blockquote", "link", "clean"],
                    [{ list: "ordered" }, { list: "bullet" }],
                ],
            }],
            primary_action_label: __("Save Draft"),
            async primary_action() {
                await frm.set_value(field, dialog.get_value("draft"));
                if (frm.is_dirty()) await frm.save();
                if (!frm.is_dirty()) dialog.hide();
            },
            secondary_action_label: __("Use Standard Template"),
            secondary_action() {
                if (!saved(frm)) return;
                const apply = async () => {
                    const { message } = await call(frm, "standard_draft", { document: key });
                    await dialog.set_value("draft", message);
                };
                if (dialog.fields_dict.draft.quill.getText().trim()) {
                    frappe.confirm(__("Replace the editor content with the standard template?"), apply);
                } else {
                    return apply();
                }
            },
        });
        dialog.set_value("draft", frm.doc[field] || "");
        dialog.show();
    }

    async function previewDraft(frm, { key, title }) {
        if (!saved(frm)) return;
        const { message } = await call(frm, "preview_draft", { document: key });
        const dialog = new frappe.ui.Dialog({ title: __("Draft Preview"), size: "extra-large" });
        $("<iframe>", {
            title, sandbox: "", srcdoc: message, class: "w-100 border-0",
        }).css("height", "65vh").appendTo(dialog.$body);
        dialog.show();
    }

    function changePublication(frm, { key }, action, prompt) {
        if (!saved(frm)) return;
        const expected_modified = frm.doc.modified;
        frappe.confirm(prompt, async () => {
            if (!saved(frm)) return;
            await call(frm, "set_publication", { document: key, action, expected_modified });
            await frm.reload_doc();
        });
    }

    function button(parent, label, run, enabled = true) {
        return $("<button>", {
            type: "button", class: "btn btn-sm btn-default", text: label, disabled: !enabled,
        }).on("click", run).appendTo(parent);
    }

    function renderDocument(frm, parent, document, status, canWrite) {
        const { field, title } = document;
        const { published, url } = status;
        const card = $("<div>", { class: "border rounded px-3 py-2 mb-2" }).appendTo(parent);
        const heading = $("<div>", { class: "d-flex flex-wrap align-items-center mb-2" }).appendTo(card);
        $("<strong>", { text: title }).appendTo(heading);
        $("<span>", {
            class: "indicator-pill ml-2 " + (published ? "green" : "gray"),
            text: published ? __("Published") : __("Draft"),
        }).appendTo(heading);
        const actions = $("<div>", { class: "d-flex flex-wrap gap-2" }).appendTo(card);
        if (canWrite) button(actions, __("Edit Draft"), () => editDraft(frm, document));
        button(actions, __("Preview Draft"), () => previewDraft(frm, document), Boolean(frm.doc[field]));
        if (canWrite) {
            button(actions, published ? __("Publish Updated Draft") : __("Publish"), () => changePublication(
                frm, document, "publish", __("Have you reviewed the saved draft and contact details for public release?")
            ), Boolean(frm.doc[field]));
            if (published) button(actions, __("Withdraw Publication"), () => changePublication(
                frm, document, "withdraw", __("Withdraw this document from its public URL?")
            ));
        }
        if (url) {
            $("<a>", { href: url, text: url, target: "_blank", rel: "noopener noreferrer", class: "d-block text-break mt-2" })
                .appendTo(card);
            button(actions, __("Copy Public URL"), () => frappe.utils.copy_to_clipboard(url));
        } else if (published) {
            $("<p>", { class: "text-muted small mt-2 mb-0", text: __("Configure a public HTTPS site origin to copy the public URL.") })
                .appendTo(card);
        }
    }

    frappe.ui.form.on("Communication Channel", {
        async refresh(frm) {
            const parent = frm.fields_dict.public_documents_controls.$wrapper.empty();
            if (!frappe.user.has_role("System Manager")) return;
            if (frm.is_new()) {
                parent.text(__("Save the channel to manage its public documents."));
                return;
            }
            const channel = frm.doc.name;
            const { message } = await call(frm, "publication_status", {}, "GET");
            if (frm.doc.name !== channel) return;
            parent.empty();
            if (frm.doc.provider === "WHATSAPP") {
                $("<p>", {
                    class: "text-muted small",
                    text: __("Use each published URL in its corresponding field in Meta Developers. The fields are Privacy Policy URL, Terms of Service URL, and Data Deletion Instructions URL."),
                }).appendTo(parent);
            }
            for (const document of documents) {
                renderDocument(frm, parent, document, message[document.key], Boolean(frm.perm?.[0]?.write));
            }
        },
    });
})();
