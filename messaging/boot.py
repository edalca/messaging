from frappe import _


def boot_session(bootinfo) -> None:
	"""Translate Messaging dock titles in the session payload without changing stored records."""
	# Registered definitions remain searchable but have no ordinary mutation actions.
	for action in ("can_create", "can_write", "can_delete", "can_import"):
		allowed = bootinfo.get("user", {}).get(action)
		if allowed is not None:
			bootinfo["user"][action] = [name for name in allowed
				if name not in ("Process Definition", "Communication Provider", "Bot Flow Presets", "Bot Flow Preset Language", "AI Evaluation Run", "AI Evaluation Result", "Conversation Preference")]
	for app in bootinfo.get("app_data", []):
		if app.get("app_name") == "messaging":
			app["dock"] = _translate_dock_titles(app.get("dock", []))
	if "messaging" in bootinfo.get("dock", {}):
		bootinfo["dock"]["messaging"] = _translate_dock_titles(bootinfo["dock"]["messaging"])


def _translate_dock_titles(entries: list[dict]) -> list[dict]:
	translated = []
	for entry in entries:
		title = entry.get("title")
		translated.append({**entry, "title": _(title)} if title else dict(entry))
	return translated
