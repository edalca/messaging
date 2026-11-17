frappe.ui.form.on("Bot Dispatch", {
	refresh(frm) {
		frm.disable_save();
		frm.set_read_only();
		if (!frm.is_new()) {
			const statusColor = {
				Accepted: "green",
				Rejected: "red",
				Unknown: "orange",
				Sending: "blue",
				Prepared: "blue",
				Held: "yellow",
				Reserved: "gray",
			}[frm.doc.status] || "gray";
			frm.page.set_indicator(__(frm.doc.status), statusColor);
			const title = [frm.doc.contact, __(frm.doc.message_type)].filter(Boolean).join(" · ");
			if (title) frm.page.set_title(title);
			frm.page.set_title_sub(frm.doc.name);
		}
	},
});
