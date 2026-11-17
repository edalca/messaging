frappe.ui.form.on("Chat Message", {
	refresh(frm) {
		frm.disable_save();
		frm.set_read_only();
		if (!frm.is_new()) {
			const kindColor = {
				Incoming: "blue",
				Automatic: "green",
				Human: "purple",
				Event: "gray",
			}[frm.doc.kind] || "gray";
			frm.page.set_indicator(__(frm.doc.kind), kindColor);
			frm.page.set_title_sub([frm.doc.session, frm.doc.contact].filter(Boolean).join(" · "));
		}
	},
});
