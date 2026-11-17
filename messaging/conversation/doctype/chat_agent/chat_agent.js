frappe.ui.form.on("Chat Agent", {
	refresh(frm) {
		if (frm.is_new()) {
			frm.page.clear_indicator();
			return;
		}
		const display = frm.doc.__onload?.scope_display;
		const title = display?.title || [frm.doc.user, frm.doc.channel, frm.doc.account].filter(Boolean).join(" · ") || frm.doc.name;
		frm.page.set_title(title);
		frm.page.set_title_sub(frm.doc.name);
		if (!frm.doc.enabled) {
			frm.page.set_indicator(__("Disabled"), "gray");
		} else if (frm.doc.available) {
			frm.page.set_indicator(__("Available"), "green");
		} else {
			frm.page.set_indicator(__("Unavailable"), "orange");
		}
	},
});
