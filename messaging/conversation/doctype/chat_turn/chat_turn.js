frappe.ui.form.on("Chat Turn", {
	refresh(frm) {
		frm.disable_save();
		frm.set_read_only();
		if (!frm.is_new()) {
			const statusColor = {
				Complete: "green",
				Pending: "orange",
				Processing: "blue",
				Ignored: "gray",
				"Quota Blocked": "yellow",
				"Recovery Required": "red",
				"Human Paused": "yellow",
			}[frm.doc.status] || "gray";
			frm.page.set_indicator(__(frm.doc.status), statusColor);
			frm.page.set_title_sub([frm.doc.contact, frm.doc.session].filter(Boolean).join(" · "));
		}
	},
});
