frappe.ui.form.on("Process Execution Log", {
	refresh(frm) {
		frm.disable_save();
		frm.set_read_only();
		if (!frm.is_new()) {
			const outcomeColor = {
				"Functional Status": "green",
				Running: "blue",
				"No Result": "gray",
				"Internal Execution Error": "red",
				"Invalid Schema Response": "orange",
			}[frm.doc.execution_outcome] || "gray";
			frm.page.set_indicator(__(frm.doc.execution_outcome), outcomeColor);
			frm.page.set_title_sub(frm.doc.name);
		}
	},
});
