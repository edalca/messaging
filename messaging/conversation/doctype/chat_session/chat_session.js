frappe.ui.form.on("Chat Session", {
	refresh(frm) {
		frm.disable_save();
		frm.set_read_only();
		if (!frm.is_new()) {
			const statusColor = {
				Active: "green",
				Capturing: "blue",
				Executing: "blue",
				"Recovery Required": "red",
				"Human Requested": "orange",
				"Human Queued": "orange",
				"Human Active": "green",
				"Human Paused": "yellow",
				Closed: "gray",
				Expired: "gray",
			}[frm.doc.status] || "gray";
			frm.page.set_indicator(__(frm.doc.status), statusColor);
			frm.page.set_title_sub([frm.doc.contact, __(frm.doc.status)].filter(Boolean).join(" · "));
		}
		if (frm.doc.human_requested_at) frm.add_custom_button(__("Conversation Inbox"), () => frappe.set_route("conversation-inbox"));
		if (frappe.user.has_role("System Manager") && ["Active", "Capturing"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Open Human Conversation"), () => frappe.call({
				method: "messaging.conversation.doctype.chat_session.human.act",
				args: { session_name: frm.doc.name, action: "Open" }, callback: () => frm.reload_doc()
			}));
		}
	}
});
