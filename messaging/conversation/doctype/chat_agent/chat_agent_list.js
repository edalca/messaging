frappe.listview_settings["Chat Agent"] = {
	add_fields: ["enabled", "available", "capacity", "user", "channel", "account"],
	get_indicator(doc) {
		if (!doc.enabled) return [__("Disabled"), "gray", "enabled,=,0"];
		if (doc.available) return [__("Available"), "green", "enabled,=,1|available,=,1"];
		return [__("Unavailable"), "orange", "enabled,=,1|available,=,0"];
	},
};
