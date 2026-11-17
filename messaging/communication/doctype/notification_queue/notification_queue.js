frappe.ui.form.on('Notification Queue', {
	refresh(frm) {
		frm.disable_save();
		frm.set_read_only();
		if (!frm.is_new()) {
			const statusColor = {
				Accepted: "green",
				Pending: "orange",
				Deferred: "orange",
				Processing: "blue",
				Rejected: "red",
				Unknown: "yellow",
				Cancelled: "gray",
			}[frm.doc.status] || "gray";
			frm.page.set_indicator(__(frm.doc.status), statusColor);
			const title = [frm.doc.notification_rule, frm.doc.source_name].filter(Boolean).join(" · ");
			if (title) frm.page.set_title(title);
			frm.page.set_title_sub(frm.doc.name);
		}
	}
});
