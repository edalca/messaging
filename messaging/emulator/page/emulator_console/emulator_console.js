frappe.pages["emulator-console"].on_page_load = wrapper => {
	const page = frappe.ui.make_app_page({parent: wrapper, title: __("Chat Emulator"), single_column: true});
	$(wrapper).addClass("emulator-page");
	const api = "messaging.emulator.page.emulator_console.emulator_console.";
	const call = async (method, args = {}) => (await frappe.call({method: api + method, args,
		type: ["conversations", "available_pairs", "history"].includes(method) ? "GET" : "POST"})).message;
	const root = $(frappe.render_template("emulator_console")).appendTo(page.body);
	const banner = root.find(".emulator-state"), list = root.find(".emulator-contacts"), history = root.find(".emulator-history");
	const search = root.find(".emulator-search input"), compose = root.find(".emulator-compose textarea"), send = root.find(".emulator-send");
	const tabChats = root.find(".emulator-tab-chats"), tabArchived = root.find(".emulator-tab-archived");
	const earlierNotifications = root.find(".emulator-earlier-notifications").text(__("Earlier notifications"));
	root.find(".emulator-list-title").text(__("Conversations"));
	root.find(".emulator-add").attr({"aria-label": __("Add conversation"), title: __("Add conversation")}).html(frappe.utils.icon("plus", "sm"));
	search.attr({placeholder: __("Search conversations"), "aria-label": __("Search conversations")});
	tabChats.text(__("Chats"));
	tabArchived.find(".emulator-tab-archived-label").text(__("Archived"));
	compose.attr({placeholder: __("Type an incoming message"), "aria-label": __("Incoming message")});
	root.find(".emulator-compose-hint").text(__("Enter to send · Shift+Enter for new line"));
	send.attr({"aria-label": __("Send"), title: __("Send")}).html(frappe.utils.icon("arrow-up", "sm", "", "", "", true));

	let automaticReceipts = true, diagnostics = false, evidenceSyncing = false, evidenceEpoch = 0;
	const observations = new Map(), confirmedReads = new Set(), sending = new Set(), retries = new Map(), drafts = new Map();
	const pendingMessages = new Map(), sendQueues = new Map(), prefPending = new Set(), historyCache = new Map();
	const HISTORY_CACHE_LIMIT = 20;
	let generation = 0, localSerial = 0, mutationEpoch = 0, enabled = false, busy = false, data = null;
	let accountName = "", sender = "", selectedContact = null, activeFolder = "chats";
	let accountRows = [], conversationRows = [], providerEnabled = false, refreshing = false, refreshAgain = false, refreshScroll = false;
	let loadingHistory = 0, activeHistoryXhr = null, initialLoadStarted = false, conversationsRefreshing = false;
	let timer = null, realtimeTimer = null, scanRaf = null, active = true, paging = false, dialog = null, confirmDialog = null, openingDialog = false;
	let activeInfoPopover = null, activeInfoBubble = null, activeInfoTrigger = null, longPressTimer = null, suppressLongPressClick = false;

	const identityKey = row => JSON.stringify([row.account || "", row.sender_id || row.sender || ""]);
	const selection = () => ({account: accountName, sender});
	const key = () => JSON.stringify([accountName, sender]);
	const accountLabel = row => row ? (row.account_name ? `${row.account_name} (${row.account_id})` : row.account_id || row.name) : accountName;
	const contactInitials = value => {
		const raw = String(value || "").trim();
		if (!raw) return "?";
		if (/^\+?\d+$/.test(raw)) return "#";
		const parts = raw.split(/\s+/).filter(Boolean);
		return parts.length === 1 ? parts[0].slice(0, 2).toUpperCase() : (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
	};
	const formatDate = value => frappe.datetime.str_to_user(frappe.datetime.convert_to_system_tz(moment(value).utc().format("YYYY-MM-DD HH:mm:ss")));
	const formatFullDate = value => new Date(value).toLocaleDateString(frappe.boot.lang || undefined, {year: "numeric", month: "long", day: "numeric"});
	const formatLocalTime = value => new Date(value).toLocaleTimeString(frappe.boot.lang || undefined, {hour: "2-digit", minute: "2-digit"});
	const empty = (target, text) => target.empty().append($("<div class='emulator-empty'>").text(__(text)));
	const resizeCompose = () => { compose.css("height", "auto").css("height", Math.min(180, compose[0].scrollHeight)); };
	const fitHeight = () => {
		if (active && root[0].getClientRects().length) root.height(Math.max(0, window.innerHeight - root[0].getBoundingClientRect().top - 16));
	};
	const hasInFlightOption = id => (pendingMessages.get(id) || []).some(p => p.send_state === "sending" && p.selection_id);
	const setBusy = value => {
		busy = value;
		send.prop("disabled", !enabled || !sender || !compose.val().trim());
		compose.prop("disabled", !enabled || !sender);
		root.find(".emulator-operation").prop("disabled", busy || !enabled || !sender || hasInFlightOption(key()));
	};
	const sortConversationRows = rows => [...rows].sort((a, b) => {
		const pinDiff = (Number(b.is_pinned) ? 1 : 0) - (Number(a.is_pinned) ? 1 : 0);
		if (pinDiff) return pinDiff;
		if (Number(a.is_pinned) && Number(b.is_pinned)) {
			const pinAtDiff = String(b.pinned_at || "").localeCompare(String(a.pinned_at || ""));
			if (pinAtDiff) return pinAtDiff;
		}
		const actDiff = String(b.last_activity || "").localeCompare(String(a.last_activity || ""));
		if (actDiff) return actDiff;
		const nameDiff = String(a.display_name || "").localeCompare(String(b.display_name || ""));
		if (nameDiff) return nameDiff;
		return String(a.account || "").localeCompare(String(b.account || "")) || String(a.sender_id || "").localeCompare(String(b.sender_id || ""));
	});
	const applyLocalActivity = (identity, preview, stamp, kind = "Incoming") => {
		const id = JSON.stringify([identity.account || "", identity.sender || ""]);
		const existing = conversationRows.find(c => identityKey(c) === id);
		if (existing) {
			existing.preview = String(preview || "").slice(0, 250);
			existing.last_activity = stamp;
			existing.activity_kind = kind;
		} else if (selectedContact && identity.sender === selectedContact.sender_id) {
			conversationRows.push({
				...selectedContact,
				account: identity.account,
				sender_id: identity.sender,
				preview: String(preview || "").slice(0, 250),
				last_activity: stamp,
				activity_kind: kind,
				is_pinned: 0,
				pinned_at: null,
				is_archived: 0,
				archived_at: null,
				cleared_before: null,
			});
		}
		conversationRows = sortConversationRows(conversationRows);
	};
	const renderHeading = () => {
		const target = root.find(".emulator-contact-heading").empty();
		$("<strong>").text(sender || __("Chat Emulator")).appendTo(target);
		if (sender && selectedContact?.display_name) $("<div class='small text-muted'>").text(selectedContact.display_name).appendTo(target);
		$("<div class='small text-muted'>").text(sender ? accountLabel(accountRows.find(a => a.name === accountName)) : __("Select or start a conversation.")).appendTo(target);
		root.find(".emulator-composer-area").toggleClass("hidden", !sender);
		const readOnlyText = sender && !enabled ? __("This conversation is read-only. Enable its provider, channel, account and contact to send messages.") : "";
		root.find(".emulator-compose-state").text(readOnlyText).toggleClass("hidden", !readOnlyText);
	};
	const destroyNode = element => { $(element).data("cleanup")?.(); };
	// Keep unchanged nodes and focus in place during polling and activity reordering.
	const reconcile = (target, rows, makeRow) => {
		const existing = new Map();
		const targetEl = target[0];
		for (let el = targetEl.firstElementChild; el; el = el.nextElementSibling) {
			const k = $(el).data("key");
			if (k !== undefined) existing.set(k, $(el));
		}
		const retained = new Set(); let previous = null;
		for (const item of rows) {
			let node = existing.get(item.key);
			if (!node || !node.length || node.data("signature") !== item.signature) {
				const replacement = makeRow(item).data({key: item.key, signature: item.signature});
				if (node && node.length) {
					destroyNode(node[0]);
					node.replaceWith(replacement);
				}
				node = replacement;
			}
			const expected = previous ? previous[0].nextElementSibling : targetEl.firstElementChild;
			if (expected !== node[0]) { if (previous) node.insertAfter(previous); else target.prepend(node); }
			previous = node; retained.add(item.key);
		}
		for (let el = targetEl.firstElementChild; el;) {
			const next = el.nextElementSibling;
			if (!retained.has($(el).data("key"))) {
				destroyNode(el);
				$(el).remove();
			}
			el = next;
		}
	};
	const cloneHistoryPayload = payload => payload ? {
		...payload,
		sessions: [...(payload.sessions || [])],
		preference: {...(payload.preference || {})},
		messages: (payload.messages || []).map(m => ({...m})),
		notifications: (payload.notifications || []).map(n => ({...n})),
	} : null;
	const storeHistoryCache = (idKey, payload) => {
		if (!idKey || !payload) return;
		historyCache.delete(idKey);
		historyCache.set(idKey, cloneHistoryPayload(payload));
		if (historyCache.size > HISTORY_CACHE_LIMIT) {
			const oldestKey = historyCache.keys().next().value;
			if (oldestKey !== undefined) historyCache.delete(oldestKey);
		}
	};
	const invalidateHistoryCacheByAccount = targetAccount => {
		for (const idKey of [...historyCache.keys()]) {
			try {
				if (JSON.parse(idKey)[0] === targetAccount) historyCache.delete(idKey);
			} catch { /* ignore */ }
		}
	};
	const selectContact = c => {
		const nextId = identityKey(c);
		if (key() !== nextId) {
			if (typeof closeInfoPanel === "function") closeInfoPanel();
			if (sender) drafts.set(key(), compose.val());
			cancelObservations();
			if (activeHistoryXhr) { try { activeHistoryXhr.abort(); } catch { /* ignore */ } activeHistoryXhr = null; }
			generation++; sender = c.sender_id; accountName = c.account; selectedContact = c; data = null;
			enabled = providerEnabled && !!accountRows.find(a => a.name === accountName)?.enabled && c.enabled !== 0;
			compose.val(drafts.get(key()) || ""); resizeCompose();
			root.addClass("emulator-chat-open"); renderHeading(); renderContacts(); setBusy(busy); fitHeight();
			const cached = historyCache.get(nextId);
			const cutoff = c.cleared_before || cached?.preference?.cleared_before || null;
			const pending = pendingMessages.get(nextId) || [];
			if (cached) {
				const hydrated = cloneHistoryPayload(cached);
				if (cutoff) {
					hydrated.messages = hydrated.messages.filter(m => String(m.timeline_at || "") > cutoff);
					hydrated.notifications = hydrated.notifications.filter(n => String(n.timeline_at || "") > cutoff);
					hydrated.preference = {...(hydrated.preference || {}), cleared_before: cutoff};
				}
				data = hydrated;
				if (typeof hydrated.enabled === "boolean") enabled = hydrated.enabled && c.enabled !== 0;
				renderHeading();
				render(true);
				openHistory(false, {background: true});
			} else if (pending.length) {
				data = {sessions: [], session: null, preference: {...c}, more_messages: false, more_notifications: false, messages: [], notifications: []};
				render(true);
				openHistory(true);
			} else {
				empty(history, "Loading history...");
				openHistory(true);
			}
			return;
		}
		root.addClass("emulator-chat-open"); renderHeading(); renderContacts(); setBusy(busy); fitHeight(); scanVisible();
	};
	const updatePreferenceRow = (idKey, patch) => {
		const row = conversationRows.find(c => identityKey(c) === idKey);
		if (row) Object.assign(row, patch);
		if (selectedContact && identityKey(selectedContact) === idKey) Object.assign(selectedContact, patch);
		if (key() === idKey && data) data.preference = {...(data.preference || {}), ...patch};
		conversationRows = sortConversationRows(conversationRows);
		renderContacts();
	};
	const togglePin = async row => {
		const idKey = identityKey(row);
		if (prefPending.has(idKey)) return;
		const prev = {is_pinned: row.is_pinned || 0, pinned_at: row.pinned_at || null};
		const nextPinned = prev.is_pinned ? 0 : 1;
		mutationEpoch++;
		prefPending.add(idKey);
		updatePreferenceRow(idKey, {is_pinned: nextPinned, pinned_at: nextPinned ? new Date().toISOString() : null});
		try {
			const res = await call("set_pinned", {account: row.account, sender: row.sender_id, pinned: nextPinned});
			mutationEpoch++;
			updatePreferenceRow(idKey, res);
			refresh();
		} catch {
			updatePreferenceRow(idKey, prev);
			frappe.show_alert({message: __("Could not update conversation preference."), indicator: "red"});
		} finally {
			prefPending.delete(idKey);
		}
	};
	const toggleArchive = async row => {
		const idKey = identityKey(row);
		if (prefPending.has(idKey)) return;
		const prev = {is_archived: row.is_archived || 0, archived_at: row.archived_at || null};
		const nextArchived = prev.is_archived ? 0 : 1;
		mutationEpoch++;
		prefPending.add(idKey);
		updatePreferenceRow(idKey, {is_archived: nextArchived, archived_at: nextArchived ? new Date().toISOString() : null});
		try {
			const res = await call("set_archived", {account: row.account, sender: row.sender_id, archived: nextArchived});
			mutationEpoch++;
			updatePreferenceRow(idKey, res);
			refresh();
		} catch {
			updatePreferenceRow(idKey, prev);
			frappe.show_alert({message: __("Could not update conversation preference."), indicator: "red"});
		} finally {
			prefPending.delete(idKey);
		}
	};
	const performClearChat = async row => {
		const idKey = identityKey(row);
		if (prefPending.has(idKey)) return;
		prefPending.add(idKey);
		mutationEpoch++;
		try {
			const res = await call("clear_chat", {account: row.account, sender: row.sender_id});
			mutationEpoch++;
			const cutoff = res.cleared_before;
			const pending = pendingMessages.get(idKey);
			if (pending && cutoff) {
				const kept = pending.filter(p => p.send_state === "sending" || String(p.timeline_at || "") > cutoff);
				if (kept.length) pendingMessages.set(idKey, kept);
				else pendingMessages.delete(idKey);
			}
			updatePreferenceRow(idKey, {
				...res,
				preview: "",
				last_activity: null,
				activity_kind: null,
			});
			if (key() === idKey && data) {
				closeInfoPanel();
				cancelObservations();
				data.preference = res;
				data.messages = cutoff ? (data.messages || []).filter(m => String(m.timeline_at || "") > cutoff) : [];
				data.notifications = cutoff ? (data.notifications || []).filter(n => String(n.timeline_at || "") > cutoff) : [];
				data.more_messages = false;
				data.more_notifications = false;
				render();
			}
			historyCache.delete(idKey);
			refresh();
		} catch {
			frappe.show_alert({message: __("Could not update conversation preference."), indicator: "red"});
		} finally {
			prefPending.delete(idKey);
		}
	};
	const confirmClearChat = row => {
		confirmDialog?.hide();
		confirmDialog = new frappe.ui.Dialog({
			title: __("Clear visible chat history?"),
			fields: [{
				fieldtype: "HTML",
				fieldname: "notice",
				options: $("<p class='text-muted mb-0'>").text(__("This will clear the visible messages and notifications in this conversation for your view. Operational and audit records will be preserved."))[0].outerHTML,
			}],
			primary_action_label: __("Clear chat"),
			primary_action: () => {
				confirmDialog?.hide();
				performClearChat(row);
			},
			secondary_action_label: __("Cancel"),
			secondary_action: () => confirmDialog?.hide(),
			onhide: () => { confirmDialog = null; },
		});
		confirmDialog.get_primary_btn().removeClass("btn-primary").addClass("btn-danger");
		confirmDialog.show();
	};
	const rowMenuOptions = c => {
		const latest = conversationRows.find(r => identityKey(r) === identityKey(c)) || c;
		return [
			{
				label: Number(latest.is_pinned) ? __("Unpin chat") : __("Pin chat"),
				icon: Number(latest.is_pinned) ? "pin-off" : "pin",
				onclick: () => togglePin(latest),
			},
			{
				label: Number(latest.is_archived) ? __("Unarchive chat") : __("Archive chat"),
				icon: Number(latest.is_archived) ? "archive-restore" : "archive",
				onclick: () => toggleArchive(latest),
			},
			{
				label: __("Clear chat"),
				icon: "eraser",
				theme: "red",
				onclick: () => confirmClearChat(latest),
			},
		];
	};
	const renderFolderTabs = () => {
		const archivedCount = conversationRows.filter(c => Number(c.is_archived)).length;
		const isArchived = activeFolder === "archived";
		tabChats.toggleClass("active", !isArchived).attr("aria-selected", String(!isArchived));
		tabArchived.toggleClass("active", isArchived).attr("aria-selected", String(isArchived));
		tabArchived.find(".emulator-archived-badge").text(String(archivedCount)).toggleClass("hidden", !archivedCount);
	};
	const renderContacts = () => {
		renderFolderTabs();
		const query = search.val().trim().toLocaleLowerCase();
		const inFolder = conversationRows.filter(c => activeFolder === "archived" ? Number(c.is_archived) : !Number(c.is_archived));
		const rows = inFolder.filter(c => !query || `${c.sender_id} ${c.display_name} ${accountLabel(accountRows.find(a => a.name === c.account))}`.toLocaleLowerCase().includes(query));
		reconcile(list, rows.map(c => {
			const account = accountRows.find(a => a.name === c.account);
			return {
				key: identityKey(c),
				signature: JSON.stringify([c.sender_id, c.display_name, c.enabled, Number(c.is_pinned), Number(c.is_archived), c.last_activity, c.preview, c.activity_kind, accountLabel(account), account?.enabled]),
				c,
			};
		}), ({c}) => {
			const item = $("<div class='emulator-contact-item'>").attr({"data-account": c.account, "data-sender": c.sender_id});
			const row = $("<button type='button' class='emulator-contact'>").on("click", () => selectContact(c)).appendTo(item);
			const displayName = c.display_name || c.sender_id;
			$("<div class='emulator-contact-avatar'>").text(contactInitials(displayName)).appendTo(row);
			const body = $("<div class='emulator-contact-body'>").appendTo(row);
			const top = $("<div class='emulator-contact-top'>").appendTo(body);
			$("<strong>").text(displayName).appendTo(top);
			const badges = $("<span class='emulator-contact-badges'>").appendTo(top);
			if (Number(c.is_pinned)) {
				$("<span class='emulator-pin-indicator'>").attr({"aria-label": __("Pinned"), title: __("Pinned")}).html(frappe.utils.icon("pin", "xs")).appendTo(badges);
			}
			if (c.last_activity) {
				$("<time>").attr({datetime: c.last_activity, title: formatDate(c.last_activity)}).text(new Date(c.last_activity).toLocaleDateString(frappe.boot.lang || undefined, {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"})).appendTo(badges);
			}
			const meta = $("<div class='emulator-contact-meta'>").appendTo(body);
			$("<span class='emulator-tag-channel'>").text("EMULR").appendTo(meta);
			if (c.display_name) $("<span class='emulator-sender-id'>").text(c.sender_id).appendTo(meta);
			$("<span class='emulator-account-label'>").text(accountLabel(accountRows.find(a => a.name === c.account))).appendTo(meta);
			$("<div class='emulator-preview'>").text(c.preview || "").appendTo(body);
			const menuBtn = $("<button type='button' class='emulator-contact-menu-btn'>")
				.attr({"aria-label": __("Conversation options"), title: __("Conversation options")})
				.html(frappe.utils.icon("chevron-down", "xs"))
				.on("click", event => event.stopPropagation())
				.appendTo(item);
			const dropdown = new frappe.ui.Dropdown({
				trigger: menuBtn[0],
				align: "end",
				options: () => rowMenuOptions(c),
				on_open: () => item.attr("data-state", "open"),
				on_close: () => item.removeAttr("data-state"),
			});
			const contextMenu = new frappe.ui.ContextMenu({
				target: item[0],
				options: () => rowMenuOptions(c),
			});
			item.data("cleanup", () => {
				dropdown.destroy();
				contextMenu.destroy();
			});
			return item;
		});
		list.children(".emulator-contact-item").each((_, row) => {
			const isCurrent = $(row).data("key") === key();
			$(row).attr("data-current", String(isCurrent));
			$(row).find(".emulator-contact").attr("aria-current", String(isCurrent));
		});
		if (!rows.length) {
			const message = !accountRows.length
				? "No EMULR accounts are configured. Configure an emulator channel account first."
				: query
					? "No conversations match this search."
					: activeFolder === "archived"
						? "No archived conversations."
						: "No conversations.";
			empty(list, message);
		}
	};
	const addConversation = async () => {
		if (dialog) { dialog.show(); return; }
		if (openingDialog) return;
		openingDialog = true;
		const options = await call("available_pairs").catch(() => { showError(); return null; }).finally(() => { openingDialog = false; });
		if (!options || !active) return;
		let available = [];
		const valid = () => available.some(a => a.name === dialog.get_value("account"));
		const updateAccounts = () => {
			const contact = dialog.get_value("contact");
			available = options.accounts.filter(a => !options.used.some(pair => pair.sender_id === contact && pair.account === a.name));
			dialog.fields_dict.account.set_data(available.map(a => ({value: a.name, label: a.account_name || a.account_id})));
			if (!valid()) dialog.set_value("account", "");
			dialog.fields_dict.availability.$wrapper.text(!options.accounts.length ? __("No EMULR accounts are configured. Configure an emulator channel account first.") : !options.contacts.length ? __("No fictitious contacts are available. Create an Emulator Contact first.") : contact && !available.length ? __("All accounts already have a conversation with this contact. Open it from the inbox.") : "");
			updateButton();
		};
		const updateButton = () => dialog.get_primary_btn().prop("disabled", !options.contacts.some(c => c.sender_id === dialog.get_value("contact")) || !valid());
		dialog = new frappe.ui.Dialog({title: __("Add fictitious contact"), fields: [
			{fieldtype: "HTML", fieldname: "description", options: $("<p class='text-muted'>").text(__("Select a contact and an emulator account to start a conversation."))[0].outerHTML},
			{fieldtype: "Autocomplete", fieldname: "contact", label: __("Fictitious contact"), reqd: 1,
				options: options.contacts.map(c => ({value: c.sender_id, label: `${c.sender_id}${c.display_name ? " · " + c.display_name : ""}`})), onchange: updateAccounts},
			{fieldtype: "Autocomplete", fieldname: "account", label: __("Channel account"), reqd: 1, options: [], onchange: updateButton},
			{fieldtype: "HTML", fieldname: "availability"}
		], primary_action_label: __("Add"), primary_action: async () => {
			if (!valid()) return;
			const contact = options.contacts.find(c => c.sender_id === dialog.get_value("contact")), account = dialog.get_value("account");
			if (!contact) return;
			dialog.get_primary_btn().prop("disabled", true);
			try {
				// A concurrent first receipt may have consumed the combination since opening.
				const fresh = await call("available_pairs");
				if (!fresh.contacts.some(c => c.sender_id === contact.sender_id) || !fresh.accounts.some(a => a.name === account)) { dialog.hide(); return; }
				if (fresh.used.some(pair => pair.sender_id === contact.sender_id && pair.account === account)) frappe.show_alert(__("This conversation already exists. Opening its history."));
				accountRows = fresh.accounts; providerEnabled = fresh.enabled;
				const existingRow = conversationRows.find(r => r.sender_id === contact.sender_id && r.account === account);
				if (existingRow && Number(existingRow.is_archived)) activeFolder = "archived";
				else if (!existingRow) activeFolder = "chats";
				dialog.hide(); selectContact(existingRow || {...contact, account});
			} finally { if (dialog) updateButton(); }
		}, secondary_action_label: __("Cancel"), secondary_action: () => dialog.hide(), onhide: () => { dialog = null; }});
		// Autocomplete labels are user data. Render their suggestions as text.
		for (const field of [dialog.fields_dict.contact, dialog.fields_dict.account]) {
			field.awesomplete.item = function (item) {
				const entry = this.get_item(item.value) || item;
				return $("<li>").data("item.autocomplete", entry).attr("aria-selected", "false").append($("<a>").append($("<p>").text(entry.label || entry.value)))[0];
			};
		}
		const status = !options.accounts.length ? __("No EMULR accounts are configured. Configure an emulator channel account first.") : !options.contacts.length ? __("No fictitious contacts are available. Create an Emulator Contact first.") : "";
		dialog.fields_dict.availability.$wrapper.text(status);
		dialog.show(); updateAccounts(); updateButton();
	};
	const showError = () => { banner.removeClass("hidden").text(__("Unable to load the emulator. Use Refresh to retry.")); };
	const operate = async (method, args) => {
		if (busy || !enabled) return;
		const revision = generation; setBusy(true);
		try { await call(method, args); if (revision === generation) await refresh(); }
		catch { if (revision === generation) showError(); }
		finally { setBusy(false); }
	};
	const enqueueSend = (id, identity, item, revision) => {
		const removePending = () => {
			const listForId = pendingMessages.get(id);
			if (!listForId) return;
			const nextList = listForId.filter(p => p.client_id !== item.client_id);
			if (nextList.length) pendingMessages.set(id, nextList);
			else pendingMessages.delete(id);
		};
		const prev = sendQueues.get(id);
		const run = async () => {
			item.send_state = "sending";
			if (key() === id && prev) { render(false); setBusy(busy); }
			try {
				const res = await call("send_message", {
					...identity,
					text: item.content,
					client_id: item.client_id,
					dispatch: item.dispatch,
					selection_id: item.selection_id,
				});
				retries.delete(id);
				if (res?.message) {
					removePending();
					if (key() === id) {
						data = data || {sessions: [], session: res.session || null, preference: {}, more_messages: false, more_notifications: false, messages: [], notifications: []};
						const merged = new Map((data.messages || []).map(row => [row.name, row]));
						merged.set(res.message.name, res.message);
						data.messages = [...merged.values()].sort((a, b) => a.timeline_at.localeCompare(b.timeline_at) || a.name.localeCompare(b.name));
						if (!data.session && res.session) data.session = res.session;
						if (typeof historyCache !== "undefined") storeHistoryCache(id, data);
						applyLocalActivity(identity, res.message.content, res.message.timeline_at, res.message.kind || "Incoming");
						render(true);
						renderContacts();
					} else if (typeof historyCache !== "undefined") {
						historyCache.delete(id);
					}
					if (typeof banner?.addClass === "function") banner.addClass("hidden").text("");
					refresh(false);
				} else {
					await refresh(revision === generation);
					removePending();
				}
			} catch {
				item.send_state = "failed";
				if (key() === id) {
					banner.removeClass("hidden").text(__("The send result is uncertain. Retry the same message to keep its event ID."));
					render(false);
				}
			} finally {
				sending.delete(id);
				setBusy(busy);
			}
		};
		const task = (prev ? prev.catch(() => {}).then(run) : run()).finally(() => {
			if (sendQueues.get(id) === task) sendQueues.delete(id);
		});
		sendQueues.set(id, task);
		return task;
	};
	const retrySend = item => {
		if (!item || item.send_state === "sending" || !enabled) return Promise.resolve();
		const id = key(), identity = selection();
		if (item.identityKey && item.identityKey !== id) return Promise.resolve();
		return enqueueSend(id, identity, item, generation);
	};
	const submit = async (dispatch, option) => {
		const identity = selection(), id = key(), text = option?.label || compose.val();
		if (!enabled || !identity.account || !identity.sender || !text.trim()) return;
		if (option && (pendingMessages.get(id) || []).some(p => p.send_state === "sending" && p.dispatch === dispatch && p.selection_id === option?.id)) return;
		const revision = generation;
		const item = {
			client_id: crypto.randomUUID(),
			name: "pending:" + (++localSerial),
			identityKey: id,
			kind: "Incoming",
			content: text,
			timeline_at: new Date().toISOString(),
			send_state: "sending",
			dispatch: dispatch || null,
			selection_id: option?.id || null,
		};
		const bucket = pendingMessages.get(id) || [];
		bucket.push(item);
		pendingMessages.set(id, bucket);
		if (drafts.get(id) === text) drafts.delete(id);
		if (!option && compose.val() === text) { compose.val(""); resizeCompose(); }
		compose.trigger("focus");
		applyLocalActivity(identity, text, item.timeline_at, "Incoming");
		render(true);
		renderContacts();
		setBusy(busy);
		return enqueueSend(id, identity, item, revision);
	};
	const diagnosticLabel = code => ({
		configuration_required: __("Not answered: configuration required"),
		automation_not_configured: __("Not answered: automation not configured"),
		execution_reconciliation_required: __("Execution reconciliation required"),
		binding_unavailable: __("Binding unavailable"),
		episode_closed_before_handoff: __("Episode closed before handoff"),
		technical_response_exceeds_limit: __("Technical response exceeds limit"),
		provider_delivery_failed: __("Provider delivery failed"),
		transport_uncertain: __("Transport uncertain"),
	}[code] || String(code || ""));
	const getProcessingSummaryLabel = state => {
		if (!state) return "";
		if (state.diagnostic) return diagnosticLabel(state.diagnostic);
		if (state.status === "Recovery Required" || state.state === "Blocked") return __("Requires diagnosis");
		if (state.status === "Quota Blocked") return __("Quota Blocked");
		if (state.status === "Human Paused") return __("Human Paused");
		if (state.status === "Ignored") return __("Ignored");
		if (state.state === "Pending") return __("Pending processing");
		if (state.state === "Running") return __("Processing");
		if (state.state === "Done" && state.status === "Complete") return __("Processing completed");
		return __(state.status || state.state || "Unknown");
	};
	const getIncomingStatusIcons = m => {
		const sendState = m.send_state || "received";
		if (sendState === "sending") return [{icon: "clock", tone: "muted", label: __("Sending...")}];
		if (sendState === "failed") return [{icon: "circle-alert", tone: "error", label: __("Send error")}];
		const state = m.processing;
		if (!state) return [{icon: "check", tone: "muted", label: __("Received")}];
		if (state.diagnostic) {
			return [
				{icon: "check", tone: "muted", label: __("Received")},
				{icon: "circle-alert", tone: "error", label: diagnosticLabel(state.diagnostic)},
			];
		}
		if (state.state === "Blocked" || state.status === "Recovery Required" || state.status === "Quota Blocked") {
			return [
				{icon: "check", tone: "muted", label: __("Received")},
				{icon: "circle-alert", tone: "error", label: state.status === "Quota Blocked" ? __("Quota Blocked") : __("Requires diagnosis")},
			];
		}
		if (state.status === "Human Paused") {
			return [
				{icon: "check", tone: "muted", label: __("Received")},
				{icon: "clock", tone: "muted", label: __("Human Paused")},
			];
		}
		if (state.status === "Ignored") {
			return [{icon: "check", tone: "muted", label: `${__("Received")} · ${__("Ignored")}`}];
		}
		if (state.state === "Pending") {
			return [
				{icon: "check", tone: "muted", label: __("Received")},
				{icon: "clock", tone: "muted", label: __("Pending processing")},
			];
		}
		if (state.state === "Running") {
			return [
				{icon: "check", tone: "muted", label: __("Received")},
				{icon: "loader-circle", tone: "muted", label: __("Processing"), spinning: true},
			];
		}
		if (state.state === "Done" && state.status === "Complete") {
			return [{icon: "check-check", tone: "muted", label: `${__("Received")} · ${__("Processing completed")}`}];
		}
		return [{icon: "check", tone: "muted", label: `${__("Received")} · ${__(state.status || state.state || "Unknown")}`}];
	};
	const getTransportStatusIcons = t => {
		if (!t) return [{icon: "clock", tone: "muted", label: __("Unknown")}];
		const status = t.status, delivery = t.delivery_status;
		if (status === "Rejected" || delivery === "Failed") {
			const label = status === "Rejected" && delivery === "Failed"
				? `${__("Rejected")} · ${__("Failed")}`
				: status === "Rejected"
					? __("Rejected")
					: `${__(status || "Accepted")} · ${__("Failed")}`;
			return [{icon: "circle-alert", tone: "error", label}];
		}
		if (status === "Sending" || status === "Processing") {
			return [{icon: "loader-circle", tone: "muted", label: `${__(status)}${delivery ? " · " + __(delivery) : ""}`, spinning: true}];
		}
		if (["Reserved", "Prepared", "Held", "Pending"].includes(status)) {
			return [{icon: "clock", tone: "muted", label: __(status)}];
		}
		if (status === "Accepted") {
			if (delivery === "Read") return [{icon: "check-check", tone: "read", label: `${__("Accepted")} · ${__("Read")}`}];
			if (delivery === "Delivered") return [{icon: "check-check", tone: "muted", label: `${__("Accepted")} · ${__("Delivered")}`}];
			return [{icon: "check", tone: "muted", label: `${__("Accepted")} · ${__(delivery || "Pending")}`}];
		}
		return [{icon: "clock", tone: "muted", label: `${__(status || "Unknown")}${delivery ? " · " + __(delivery) : ""}`}];
	};
	const renderStatusIcons = (container, specs) => {
		const target = $(container).empty();
		for (const spec of specs) {
			$("<span class='emulator-status-icon'>")
				.attr({"data-icon": spec.icon, "data-tone": spec.tone || "muted", role: "img", "aria-label": spec.label, title: spec.label})
				.html(frappe.utils.icon(spec.icon, "xs", "", "", spec.spinning ? "emulator-icon-spin" : "", true))
				.appendTo(target);
		}
		return target;
	};
	const buildInfoSections = (source, item) => {
		if (!item) return [];
		const isNotification = source === "notification";
		const sendState = item.send_state || (item.kind === "Incoming" ? "received" : undefined);
		const acct = accountRows.find(a => a.name === accountName);
		const basicRows = [
			{label: __("Date"), value: formatFullDate(item.timeline_at)},
			{label: __("Time"), value: formatLocalTime(item.timeline_at)},
			{label: __("Direction"), value: !isNotification && item.kind === "Incoming" ? __("Incoming") : __("Outgoing")},
			{
				label: __("Origin"),
				value: isNotification
					? __("Simulated notification")
					: item.kind === "Incoming"
						? __("Simulated subscriber")
						: item.kind === "Human"
							? (item.agent ? `${__("Human agent")} (${item.agent})` : __("Human agent"))
							: __("Bot"),
			},
			{label: __("Channel"), value: __("Emulator")},
			{label: __("Account"), value: accountLabel(acct)},
		];
		if (isNotification && item.recipient) basicRows.push({label: __("Original Recipient"), value: item.recipient});
		const sections = [basicRows];

		if (!isNotification && item.kind === "Incoming") {
			const procRows = [];
			if (sendState === "sending" || sendState === "failed") {
				procRows.push({
					label: __("Reception"),
					value: sendState === "sending" ? __("Sending...") : __("Send error"),
					tone: sendState === "failed" ? "error" : undefined,
				});
			} else {
				procRows.push({label: __("Reception"), value: __("Received")});
				if (item.processing) {
					if (item.processing.status) procRows.push({label: __("Turn status"), value: __(item.processing.status)});
					const summary = getProcessingSummaryLabel(item.processing);
					if (summary) {
						const hasError = Boolean(item.processing.diagnostic || item.processing.state === "Blocked"
							|| ["Recovery Required", "Quota Blocked"].includes(item.processing.status));
						procRows.push({label: __("Processing"), value: summary, tone: hasError ? "error" : undefined});
					}
					if (item.processing.diagnostic) {
						procRows.push({label: __("Diagnostic"), value: diagnosticLabel(item.processing.diagnostic), tone: "error"});
					}
				}
			}
			if (procRows.length) sections.push(procRows);
		}

		const transport = isNotification ? item : item.transport;
		if (transport) {
			const tRows = [
				{
					label: isNotification ? __("Status") : __("Dispatch"),
					value: __(transport.status || "Unknown"),
					tone: transport.status === "Rejected" ? "error" : undefined,
				},
				{
					label: __("Acceptance"),
					value: transport.status === "Accepted"
						? __("Accepted")
						: transport.status === "Rejected"
							? __("Rejected")
							: transport.status === "Unknown"
								? __("Unknown")
								: __("Pending"),
					tone: transport.status === "Rejected" ? "error" : undefined,
				},
			];
			const deliveryValue = transport.delivery_status === "Read"
				? __("Delivered")
				: transport.delivery_status
					? __(transport.delivery_status)
					: transport.status === "Accepted"
						? __("Pending")
						: "";
			if (deliveryValue) {
				tRows.push({
					label: __("Delivery Status"),
					value: deliveryValue,
					tone: transport.delivery_status === "Failed" ? "error" : undefined,
				});
			}
			if (transport.delivery_status === "Read") {
				tRows.push({label: __("Read status"), value: __("Read"), tone: "read"});
			}
			if (transport.status === "Rejected" || transport.delivery_status === "Failed") {
				tRows.push({
					label: __("Failure"),
					value: transport.diagnostic_code
						? diagnosticLabel(transport.diagnostic_code)
						: transport.status === "Rejected"
							? __("Rejected")
							: __("Delivery failed"),
					tone: "error",
				});
			}
			sections.push(tRows);
		}

		const techRows = [];
		if (isNotification) {
			if (item.name) techRows.push({label: __("Notification Queue"), value: item.name});
			if (item.transport_reference) techRows.push({label: __("Transport Reference"), value: item.transport_reference});
		} else {
			if (item.name && !String(item.name).startsWith("pending:")) techRows.push({label: __("Chat Message"), value: item.name});
			if (item.turn) techRows.push({label: __("Chat Turn"), value: item.turn});
			if (item.session) techRows.push({label: __("Chat Session"), value: item.session});
			if (item.dispatch) techRows.push({label: __("Bot Dispatch"), value: item.dispatch});
			if (item.transport?.transport_reference) techRows.push({label: __("Transport Reference"), value: item.transport.transport_reference});
		}
		if (techRows.length) sections.push(techRows);
		return sections;
	};
	const closeInfoPanel = (reason = "owner") => {
		if (!activeInfoPopover) return;
		const pop = activeInfoPopover;
		const bubble = activeInfoBubble;
		activeInfoPopover = null;
		activeInfoBubble = null;
		activeInfoTrigger = null;
		bubble?.removeAttribute("data-info-open");
		pop.close(reason);
		pop.destroy();
	};
	const resolveLiveInfoEntry = bubbleEl => {
		const msgName = bubbleEl.getAttribute("data-message");
		if (msgName) {
			const live = (data?.messages || []).find(m => m.name === msgName)
				|| (pendingMessages.get(key()) || []).find(m => m.name === msgName);
			if (live) return {source: "message", item: live};
		}
		const notifName = bubbleEl.getAttribute("data-notification");
		if (notifName) {
			const live = (data?.notifications || []).find(n => n.name === notifName);
			if (live) return {source: "notification", item: live};
		}
		return $(bubbleEl).data("infoEntry") || null;
	};
	const buildMessageInfoContent = entry => {
		const container = document.createElement("div");
		container.className = "emulator-info-body";
		const header = document.createElement("div");
		header.className = "es-popover__header emulator-info-header";
		const title = document.createElement("div");
		title.className = "es-popover__title";
		title.textContent = __("Message info");
		header.appendChild(title);
		const closeBtn = document.createElement("button");
		closeBtn.type = "button";
		closeBtn.className = "emulator-info-close btn btn-default btn-xs";
		closeBtn.setAttribute("aria-label", __("Close"));
		closeBtn.setAttribute("title", __("Close"));
		closeBtn.innerHTML = frappe.utils.icon("x", "xs", "", "", "", true);
		closeBtn.addEventListener("click", () => closeInfoPanel("escape"));
		header.appendChild(closeBtn);
		container.appendChild(header);

		const sectionsWrap = document.createElement("div");
		sectionsWrap.className = "emulator-info-sections";
		const sections = buildInfoSections(entry?.source, entry?.item);
		for (const rows of sections) {
			const sec = document.createElement("div");
			sec.className = "emulator-info-section";
			const dl = document.createElement("dl");
			dl.className = "emulator-info-list";
			for (const row of rows) {
				const dt = document.createElement("dt");
				dt.textContent = row.label;
				const dd = document.createElement("dd");
				dd.textContent = row.value;
				if (row.tone) dd.setAttribute("data-tone", row.tone);
				dl.appendChild(dt);
				dl.appendChild(dd);
			}
			sec.appendChild(dl);
			sectionsWrap.appendChild(sec);
		}
		container.appendChild(sectionsWrap);
		return container;
	};
	const openMessageInfo = (bubbleEl, triggerEl) => {
		if (!bubbleEl || !triggerEl) return;
		if (activeInfoPopover && activeInfoBubble === bubbleEl) {
			closeInfoPanel("owner");
			return;
		}
		closeInfoPanel("owner");
		const entry = resolveLiveInfoEntry(bubbleEl);
		if (!entry) return;
		activeInfoBubble = bubbleEl;
		activeInfoTrigger = triggerEl;
		const triggerRect = triggerEl.getBoundingClientRect();
		const align = triggerRect.top < window.innerHeight * 0.48 ? "start" : "end";
		const pop = new frappe.ui.Popover({
			trigger: triggerEl,
			side: bubbleEl.getAttribute("data-kind") === "Incoming" ? "left" : "right",
			align,
			offset: 6,
			css_class: "emulator-info-popover",
			content: () => buildMessageInfoContent(resolveLiveInfoEntry(bubbleEl) || entry),
			on_open: () => bubbleEl.setAttribute("data-info-open", "true"),
			on_close: () => {
				bubbleEl.removeAttribute("data-info-open");
				if (activeInfoPopover === pop) {
					activeInfoPopover = null;
					activeInfoBubble = null;
					activeInfoTrigger = null;
					pop.destroy();
				}
			},
		});
		if (pop.trigger_el && pop.ontriggerclick) {
			pop.trigger_el.removeEventListener("click", pop.ontriggerclick);
		}
		activeInfoPopover = pop;
		pop.open();
	};
	const applyReadStatusVisual = item => {
		if (!item?.element) return;
		const iconsEl = $(item.element).find(".emulator-status-icons");
		if (iconsEl.length) renderStatusIcons(iconsEl, getTransportStatusIcons(item.transport));
		if (activeInfoBubble === item.element && activeInfoPopover?.panel) {
			const entry = resolveLiveInfoEntry(item.element);
			if (entry) {
				activeInfoPopover.panel.replaceChildren(buildMessageInfoContent(entry));
			}
		}
	};
	const appendBubbleMeta = (row, timestamp, statusSpecs) => {
		const meta = $("<div class='emulator-message-meta'>").appendTo(row);
		$("<span class='emulator-message-time'>")
			.attr("title", formatDate(timestamp))
			.text(formatLocalTime(timestamp))
			.appendTo(meta);
		const iconsWrap = $("<span class='emulator-status-icons'>").appendTo(meta);
		renderStatusIcons(iconsWrap, statusSpecs);
		$("<button type='button' class='emulator-message-info-btn'>")
			.attr({"aria-label": __("Message info"), title: __("Message info"), "aria-haspopup": "dialog", "aria-expanded": "false"})
			.html(frappe.utils.icon("info", "xs", "", "", "", true))
			.appendTo(meta);
		return meta;
	};
	const renderNotification = n => {
		const row = $("<div class='emulator-notification'>").attr({"data-notification": n.name, "data-timeline-at": n.timeline_at});
		row.data("infoEntry", {source: "notification", item: n});
		row.data("cleanup", () => { if (activeInfoBubble === row[0]) closeInfoPanel("owner"); });
		const header = $("<div class='emulator-notification-header'>").appendTo(row);
		$("<span class='emulator-notification-badge'>").text(__("Simulated notification")).appendTo(header);
		$("<span class='emulator-notification-recipient'>").text(__("Original recipient: {0}", [n.recipient])).appendTo(header);
		$("<div class='emulator-message-content'>").text(n.content_type === "Template" ? n.rendered_content || `${__("Template Reference")}: ${n.template_reference}` : n.prepared_text).appendTo(row);
		appendBubbleMeta(row, n.timeline_at, getTransportStatusIcons(n));
		if (diagnostics && ["Processing", "Unknown", "Accepted"].includes(n.status)) {
			const tools = $("<details class='emulator-diagnostics'>").appendTo(row);
			$("<summary>").text(__("EMULR diagnostics")).appendTo(tools);
			$("<div class='small text-muted'>").text(n.transport_reference || n.name).appendTo(tools);
			for (const status of ["Sent", "Delivered", "Failed"]) $("<button class='emulator-operation btn btn-default btn-xs mr-1' type='button'>").text(__(status)).on("click", () => operate("simulate_notification", {...selection(), notification: n.name, status})).appendTo(tools);
			if (n.status !== "Accepted") for (const outcome of ["Accepted", "Rejected"]) $("<button class='emulator-operation btn btn-default btn-xs mr-1' type='button'>").text(__("Reconcile") + ": " + __(outcome)).on("click", () => operate("simulate_notification", {...selection(), notification: n.name, outcome})).appendTo(tools);
		}
		return row;
	};
	const renderMessage = m => {
		const sendState = m.send_state || (m.kind === "Incoming" ? "received" : undefined);
		const row = $("<div class='emulator-message'>").attr({"data-kind": m.kind, "data-message": m.name, "data-timeline-at": m.timeline_at});
		if (sendState) row.attr("data-send-state", sendState);
		row.data("infoEntry", {source: "message", item: m});
		row.data("cleanup", () => { if (activeInfoBubble === row[0]) closeInfoPanel("owner"); });
		$("<div class='emulator-message-content'>").text(m.content || "").appendTo(row);
		for (const option of m.options || []) $("<button class='emulator-operation emulator-option btn btn-default btn-sm mr-2 mt-2' type='button'>").text(option.label).on("click", () => submit(m.dispatch, option)).appendTo(row);
		const t = m.transport;
		const statusSpecs = m.kind === "Incoming" ? getIncomingStatusIcons(m) : getTransportStatusIcons(t);
		appendBubbleMeta(row, m.timeline_at, statusSpecs);
		if (sendState === "failed") {
			const errorBar = $("<div class='emulator-message-error-bar'>").appendTo(row);
			$("<span class='emulator-error-hint'>").text(__("Send error")).appendTo(errorBar);
			$("<button type='button' class='emulator-retry-btn btn btn-default btn-xs'>").text(__("Retry")).on("click", () => retrySend(m)).appendTo(errorBar);
		}
		if (diagnostics && t && ["Accepted", "Unknown", "Sending"].includes(t.status)) {
			const actions = $("<details class='emulator-diagnostics small mt-2'>").appendTo(row);
			$("<summary>").text(__("EMULR diagnostics")).appendTo(actions);
			for (const status of ["Sent", "Delivered", "Failed"]) $("<button class='emulator-operation btn btn-default btn-xs mr-1' type='button'>").text(__(status)).on("click", () => operate("simulate_status", {...selection(), dispatch: m.dispatch, status})).appendTo(actions);
			if (t.status !== "Accepted") for (const outcome of ["Accepted", "Rejected"]) $("<button class='emulator-operation btn btn-default btn-xs mr-1' type='button'>").text(__("Reconcile") + ": " + __(outcome)).on("click", () => operate("simulate_result", {...selection(), dispatch: m.dispatch, outcome})).appendTo(actions);
		}
		return row;
	};
	const render = (scroll = false) => {
		const position = history.scrollTop(), atBottom = history[0].scrollHeight - position - history.innerHeight() < 80;
		const localPending = pendingMessages.get(key()) || [];
		const persistedNames = new Set((data?.messages || []).map(m => m.name));
		const visiblePending = localPending.filter(p => !p.message_name || !persistedNames.has(p.message_name));
		const timeline = [
			...(data?.messages || []).map(m => ({...m, source:"message"})),
			...visiblePending.map(m => ({...m, source:"message"})),
			...(data?.notifications || []).map(n => ({...n, source:"notification"}))
		].sort((a, b) => a.timeline_at.localeCompare(b.timeline_at) || `${a.source}:${a.name}`.localeCompare(`${b.source}:${b.name}`));
		const rows = []; let date = "";
		for (const item of timeline) {
			const day = new Date(item.timeline_at).toLocaleDateString(frappe.boot.lang || undefined);
			if (day !== date) { rows.push({key:`date:${day}`, signature:day, date:day}); date = day; }
			rows.push({key:`${item.source}:${item.name}`, signature:JSON.stringify({...item, client_receipt:undefined, diagnostics}), item});
		}
		reconcile(history, rows, row => row.date ? $("<div class='emulator-date'>").text(row.date) : row.item.source === "notification" ? renderNotification(row.item) : renderMessage(row.item));
		if (!rows.length && data !== null) empty(history, "This conversation has no visible messages yet.");
		root.find(".emulator-earlier").toggleClass("hidden", !data?.more_messages);
		earlierNotifications.toggleClass("hidden", !data?.more_notifications);
		root.find(".emulator-paging").toggleClass("hidden", !data?.more_messages && !data?.more_notifications);
		setBusy(busy); history.scrollTop(scroll || atBottom ? history[0].scrollHeight : position);
		syncEvidence(); scanVisible();
	};
	const cancelObservations = () => {
		evidenceEpoch++;
		for (const observation of observations.values()) clearTimeout(observation.timer);
		observations.clear();
	};
	const evidenceItems = () => [
		...(data?.messages || []).filter(m => m.transport && m.client_receipt).map(m => ({
			row: m, transport: m.transport, source_type: "Bot Dispatch", name: m.dispatch,
			element: history.find(`[data-message="${m.name}"]`)[0]
		})),
		...(data?.notifications || []).filter(n => n.client_receipt).map(n => ({
			row: n, transport: n, source_type: "Notification Queue", name: n.name,
			element: history.find(`[data-notification="${n.name}"]`)[0]
		}))
	];
	const evidenceArgs = item => ({...selection(), source_type: item.source_type, name: item.name, receipt: item.row.client_receipt});
	const canObserve = item => {
		if (!automaticReceipts || !enabled || !active || !sender || document.hidden || !document.hasFocus()
			|| item.transport.status !== "Accepted" || item.transport.delivery_status !== "Delivered"
			|| !item.element?.isConnected || !item.element.getClientRects().length) return false;
		const box = item.element.getBoundingClientRect(), viewport = history[0].getBoundingClientRect();
		const top = Math.max(0, viewport.top), bottom = Math.min(window.innerHeight, viewport.bottom);
		const visible = Math.min(box.bottom, bottom) - Math.max(box.top, top);
		if (!(box.width > 0 && bottom > top && box.left >= Math.max(0, viewport.left)
			&& box.right <= Math.min(window.innerWidth, viewport.right)
			&& visible >= Math.min(box.height, bottom - top) * .75)) return false;
		for (let element = item.element; element; element = element.parentElement) {
			const style = getComputedStyle(element);
			if (style.visibility !== "visible" || Number(style.opacity) === 0) return false;
		}
		const exposed = document.elementFromPoint((box.left + box.right) / 2,
			(Math.max(box.top, top) + Math.min(box.bottom, bottom)) / 2);
		return !!exposed && item.element.contains(exposed);
	};
	const scanVisible = () => {
		const items = evidenceItems(), ids = new Set(items.map(item => `${item.source_type}:${item.name}`));
		for (const [id, observation] of observations) if (!ids.has(id)) { clearTimeout(observation.timer); observations.delete(id); }
		for (const item of items) {
			const id = `${item.source_type}:${item.name}`;
			let existing = observations.get(id);
			if (existing && existing.element !== item.element) { clearTimeout(existing.timer); observations.delete(id); existing = null; }
			if (!canObserve(item)) {
				if (existing) { clearTimeout(existing.timer); observations.delete(id); }
				continue;
			}
			if (existing || confirmedReads.has(id)) continue;
			const observation = {epoch: evidenceEpoch, identity: key(), element: item.element, timer: null};
			observations.set(id, observation);
			const current = () => observations.get(id) === observation && observation.epoch === evidenceEpoch
				&& observation.identity === key() && canObserve(item);
			call("observe_message", evidenceArgs(item)).then(proof => {
				if (!current()) return;
				// Start after the server has signed the observation, preserving at least 700 ms on both sides.
				const started = performance.now();
				const confirmRead = async () => {
					if (!current()) return;
					// Browser timers can fire early. Measure the continuous dwell before posting evidence.
					const remaining = 700 - (performance.now() - started);
					if (remaining > 0) {
						observation.timer = setTimeout(confirmRead, Math.ceil(remaining));
						return;
					}
					try {
						await call("client_status", {...evidenceArgs(item), receipt: proof.receipt, status: "Read"});
						confirmedReads.add(id);
						if (current()) {
							item.transport.delivery_status = "Read";
							if (typeof applyReadStatusVisual === "function") applyReadStatusVisual(item);
							else $(item.element).find(".emulator-delivery-status").text(__("Read"));
						}
					} catch { /* A later refresh revalidates transport evidence. */ }
				};
				observation.timer = setTimeout(confirmRead, 700);
			}).catch(() => { /* Do not turn diagnostic failures into delivery evidence. */ });
		}
	};
	const syncEvidence = async () => {
		if (evidenceSyncing || loadingHistory > 0 || !automaticReceipts || !active || !enabled || document.hidden) return;
		evidenceSyncing = true;
		const revision = generation, identity = key();
		let changed = false;
		try {
			for (const item of evidenceItems().filter(item => item.transport.status === "Accepted"
				&& [null, undefined, "", "Pending", "Sent"].includes(item.transport.delivery_status)).slice(0, 20)) {
				if (!automaticReceipts || !active || document.hidden || revision !== generation || identity !== key()) break;
				try {
					const result = await call("client_status", {...evidenceArgs(item), status: "Delivered"});
					if (revision !== generation || identity !== key()) break;
					if (result.delivery === "Delivered" || result.delivery === "Read") {
						item.transport.delivery_status = result.delivery; changed = true;
					}
				} catch { break; }
			}
		} finally {
			evidenceSyncing = false;
			if (changed && revision === generation && identity === key()) render();
		}
	};

	const merge = (older, newer) => [...new Map([...older, ...newer].map(row => [row.name, row])).values()]
		.sort((a, b) => a.timeline_at.localeCompare(b.timeline_at) || a.name.localeCompare(b.name));
	const needsFastPoll = () => (data?.messages || []).some(m => ["Pending", "Running"].includes(m.processing?.state))
		|| (pendingMessages.get(key()) || []).some(p => p.send_state === "sending");
	const schedule = () => {
		clearTimeout(timer); timer = null;
		if (active && !document.hidden) timer = setTimeout(() => { if (paging || loadingHistory > 0) schedule(); else refresh(); }, needsFastPoll() ? 1000 : 5000);
	};
	const callAbortableHistory = args => {
		const xhr = frappe.call({method: api + "history", args, type: "GET"});
		const promise = new Promise((resolve, reject) => {
			if (xhr && typeof xhr.done === "function" && typeof xhr.fail === "function") {
				xhr.done(r => resolve(r?.message));
				xhr.fail((_jq, textStatus, err) => reject(Object.assign(new Error(err || textStatus || "history_request_failed"), {aborted: textStatus === "abort"})));
			} else {
				Promise.resolve(xhr).then(r => resolve(r?.message), reject);
			}
		});
		return {xhr, promise};
	};
	const applyHistoryResult = (result, scroll = false) => {
		const clearedBefore = result.preference?.cleared_before || null;
		if (data) {
			for (const [field, more] of [["messages", "more_messages"], ["notifications", "more_notifications"]]) {
				const existingRows = clearedBefore
					? (data[field] || []).filter(row => String(row.timeline_at || "") > clearedBefore)
					: (data[field] || []);
				if (!result[more] || result[field].some(row => existingRows.some(old => old.name === row.name))) {
					result[more] = existingRows.length ? data[more] : result[more];
					result[field] = merge(existingRows, result[field]);
				}
			}
		}
		const prevSig = data ? JSON.stringify([data.enabled, data.more_messages, data.more_notifications, data.preference, data.messages, data.notifications]) : null;
		const nextSig = JSON.stringify([result.enabled, result.more_messages, result.more_notifications, result.preference, result.messages, result.notifications]);
		data = result;
		enabled = !!result.enabled && selectedContact?.enabled !== 0;
		storeHistoryCache(key(), result);
		if (prevSig !== nextSig || scroll) {
			renderHeading();
			render(scroll);
		}
	};
	const openHistory = async (scroll = false, {background = false} = {}) => {
		if (!active || document.hidden || !sender) return;
		const revision = generation, identity = key();
		const current = () => active && revision === generation && identity === key();
		if (activeHistoryXhr) { try { activeHistoryXhr.abort(); } catch { /* ignore */ } activeHistoryXhr = null; }
		clearTimeout(timer); timer = null;
		loadingHistory++;
		const {xhr, promise} = callAbortableHistory(selection());
		activeHistoryXhr = xhr;
		try {
			const result = await promise;
			if (activeHistoryXhr === xhr) activeHistoryXhr = null;
			if (!current() || !result) return;
			banner.addClass("hidden").text("");
			applyHistoryResult(result, scroll);
		} catch (err) {
			if (activeHistoryXhr === xhr) activeHistoryXhr = null;
			if (err?.aborted || !current()) return;
			if (!background || !data) {
				enabled = false;
				showError();
				renderHeading();
				if (!data) empty(history, "Unable to load history. Use Refresh to retry.");
			}
		} finally {
			loadingHistory = Math.max(0, loadingHistory - 1);
			setBusy(busy);
			fitHeight();
			schedule();
		}
	};
	const refreshConversations = async () => {
		if (!active || document.hidden || conversationsRefreshing) return;
		conversationsRefreshing = true;
		const epochBefore = mutationEpoch;
		try {
			const config = await call("conversations");
			if (!active) return;
			accountRows = config.accounts; providerEnabled = config.enabled;
			if (epochBefore === mutationEpoch) {
				conversationRows = config.conversations;
				for (const [id, listForId] of pendingMessages.entries()) {
					const sendingItems = listForId.filter(p => p.send_state === "sending");
					if (sendingItems.length) {
						const latest = sendingItems[sendingItems.length - 1];
						const parts = JSON.parse(id);
						const existing = conversationRows.find(c => identityKey(c) === id);
						if (!existing || String(latest.timeline_at || "") > String(existing.last_activity || "")) {
							applyLocalActivity({account: parts[0], sender: parts[1]}, latest.content, latest.timeline_at, "Incoming");
						}
					}
				}
			}
			enabled = !!providerEnabled && !!accountRows.find(a => a.name === accountName)?.enabled && selectedContact?.enabled !== 0;
			banner.addClass("hidden").text(""); renderContacts();
			if (sender && !accountRows.some(a => a.name === accountName)) {
				cancelObservations(); generation++; sender = ""; accountName = ""; selectedContact = null; data = null;
				root.removeClass("emulator-chat-open");
			}
			if (!sender) {
				renderHeading(); empty(history, !accountRows.length ? "No EMULR accounts are configured. Configure an emulator channel account first." : "Select or start a conversation.");
			} else {
				selectedContact = conversationRows.find(c => identityKey(c) === key()) || selectedContact;
			}
		} catch {
			if (active) { showError(); }
		} finally {
			conversationsRefreshing = false;
		}
	};
	const refresh = async (scroll = false, {conversationsOnly = false} = {}) => {
		if (!active || document.hidden) return;
		if (refreshing) { refreshAgain = true; refreshScroll = refreshScroll || scroll; return; }
		clearTimeout(timer); timer = null; refreshing = true;
		const revision = generation, identity = key(), epochBefore = mutationEpoch;
		const current = () => active && revision === generation && identity === key();
		try {
			const historyPromise = !conversationsOnly && sender ? callAbortableHistory(selection()).promise.catch(err => ({__error: err})) : null;
			const config = await call("conversations");
			if (!active) return;
			accountRows = config.accounts; providerEnabled = config.enabled;
			if (epochBefore === mutationEpoch) {
				conversationRows = config.conversations;
				for (const [id, listForId] of pendingMessages.entries()) {
					const sendingItems = listForId.filter(p => p.send_state === "sending");
					if (sendingItems.length) {
						const latest = sendingItems[sendingItems.length - 1];
						const parts = JSON.parse(id);
						const existing = conversationRows.find(c => identityKey(c) === id);
						if (!existing || String(latest.timeline_at || "") > String(existing.last_activity || "")) {
							applyLocalActivity({account: parts[0], sender: parts[1]}, latest.content, latest.timeline_at, "Incoming");
						}
					}
				}
			}
			enabled = !!providerEnabled && !!accountRows.find(a => a.name === accountName)?.enabled && selectedContact?.enabled !== 0;
			banner.addClass("hidden").text(""); renderContacts();
			if (sender && !accountRows.some(a => a.name === accountName)) {
				cancelObservations(); generation++; sender = ""; accountName = ""; selectedContact = null; data = null;
				root.removeClass("emulator-chat-open");
			}
			if (!sender) {
				renderHeading(); empty(history, !accountRows.length ? "No EMULR accounts are configured. Configure an emulator channel account first." : "Select or start a conversation."); return;
			}
			selectedContact = conversationRows.find(c => identityKey(c) === key()) || selectedContact;
			if (conversationsOnly) return;
			const result = historyPromise ? await historyPromise : await call("history", selection());
			if (!current()) return;
			if (result?.__error) throw result.__error;
			applyHistoryResult(result, scroll);
		} catch (err) { if (!err?.aborted && current()) { enabled = false; showError(); renderHeading(); if (!data) empty(history, "Unable to load history. Use Refresh to retry."); } }
		finally {
			refreshing = false; setBusy(busy); fitHeight();
			if (refreshAgain && active && !document.hidden) {
				const pendingScroll = refreshScroll;
				refreshAgain = false; refreshScroll = false;
				refresh(pendingScroll);
			}
			else schedule();
		}
	};
	const onRealtimeUpdate = payload => {
		if (!active || document.hidden) return;
		if (payload?.channel && payload.channel !== "emulator") return;
		if (payload?.account) invalidateHistoryCacheByAccount(payload.account);
		const unrelatedAccount = Boolean(payload?.account && accountName && payload.account !== accountName);
		clearTimeout(realtimeTimer);
		realtimeTimer = setTimeout(() => {
			realtimeTimer = null;
			if (active && !document.hidden && !paging) {
				if (unrelatedAccount) refreshConversations();
				else refresh();
			}
		}, 60);
	};
	const loadEarlier = async source => {
		if (paging || !data) return;
		const revision = generation, identity = key(), field = source === "message" ? "messages" : "notifications";
		const cursor = data[field][0]?.name; if (!cursor) return;
		paging = true; root.find(".emulator-paging button").prop("disabled", true);
		const position = history.scrollTop(), height = history[0].scrollHeight;
		try {
			const result = await call("history", {...selection(), [source === "message" ? "message_before" : "notification_before"]: cursor});
			if (revision !== generation || identity !== key()) return;
			const clearedBefore = result.preference?.cleared_before || null;
			const existingRows = clearedBefore ? (data[field] || []).filter(row => String(row.timeline_at || "") > clearedBefore) : (data[field] || []);
			data[field] = merge(result[field], existingRows);
			data[source === "message" ? "more_messages" : "more_notifications"] = result[source === "message" ? "more_messages" : "more_notifications"];
			storeHistoryCache(identity, data);
			render(); history.scrollTop(position + history[0].scrollHeight - height);
		} catch { if (revision === generation) showError(); }
		finally { paging = false; root.find(".emulator-paging button").prop("disabled", false); }
	};
	root.find(".emulator-add").on("click", addConversation);
	tabChats.on("click", () => { if (activeFolder !== "chats") { activeFolder = "chats"; renderContacts(); } });
	tabArchived.on("click", () => { if (activeFolder !== "archived") { activeFolder = "archived"; renderContacts(); } });
	send.on("click", () => submit());
	search.on("input", renderContacts);
	compose.on("input", () => { drafts.set(key(), compose.val()); resizeCompose(); setBusy(busy); });
	compose.on("keydown", event => {
		if (event.key === "Enter" && !event.shiftKey && !event.originalEvent?.isComposing) { event.preventDefault(); submit(); }
	});
	earlierNotifications.on("click", () => loadEarlier("notification"));
	root.find(".emulator-earlier").text(__("Earlier messages")).on("click", () => loadEarlier("message"));
	root.find(".emulator-back").html(frappe.utils.icon("arrow-left", "sm")).attr({"aria-label": __("Back to conversations"), title: __("Back to conversations")}).on("click", () => { root.removeClass("emulator-chat-open"); cancelObservations(); });
	const diagnosticsButton = frappe.ui.button({label: __("Emulator diagnostics"), icon: "settings", css_class: "emulator-diagnostics-button"}).attr({"aria-label": __("Emulator diagnostics"), title: __("Emulator diagnostics")}).prependTo(page.page_actions);
	const diagnosticsMenu = new frappe.ui.Dropdown({trigger: diagnosticsButton, options: () => [
		{label: automaticReceipts ? __("Suspend automatic receipts") : __("Resume automatic receipts"), onclick: () => {
			automaticReceipts = !automaticReceipts; cancelObservations(); render();
			frappe.show_alert({message: automaticReceipts ? __("Automatic receipts resumed.") : __("Automatic receipts suspended for this console."), indicator: "orange"});
		}},
		{label: diagnostics ? __("Hide transport diagnostics") : __("Show transport diagnostics"), onclick: () => { diagnostics = !diagnostics; render(); }}
	]});
	const cancelLongPress = () => {
		if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
	};
	history.on("click", ".emulator-message-info-btn", event => {
		event.preventDefault();
		event.stopPropagation();
		if (suppressLongPressClick) { suppressLongPressClick = false; return; }
		const bubble = event.currentTarget.closest(".emulator-message, .emulator-notification");
		openMessageInfo(bubble, event.currentTarget);
	});
	history.on("contextmenu", ".emulator-message, .emulator-notification", event => {
		event.preventDefault();
		event.stopPropagation();
		cancelLongPress();
		const bubble = event.currentTarget;
		const trigger = bubble.querySelector(".emulator-message-info-btn");
		openMessageInfo(bubble, trigger);
	});
	let pressStartX = 0, pressStartY = 0;
	history.on("pointerdown", ".emulator-message, .emulator-notification", event => {
		if (event.pointerType !== "touch") return;
		if (event.target.closest("button, a, summary, details")) return;
		cancelLongPress();
		pressStartX = event.clientX;
		pressStartY = event.clientY;
		const bubble = event.currentTarget;
		const trigger = bubble.querySelector(".emulator-message-info-btn");
		longPressTimer = setTimeout(() => {
			longPressTimer = null;
			suppressLongPressClick = true;
			setTimeout(() => { suppressLongPressClick = false; }, 350);
			openMessageInfo(bubble, trigger);
		}, 500);
	});
	history.on("pointermove", event => {
		if (!longPressTimer) return;
		if (Math.hypot(event.clientX - pressStartX, event.clientY - pressStartY) > 8) cancelLongPress();
	});
	history.on("pointerup pointercancel", cancelLongPress);
	frappe.realtime.on("messaging_conversation_update", onRealtimeUpdate);
	history.on("scroll", () => {
		cancelLongPress();
		if (activeInfoTrigger && activeInfoPopover) {
			const box = activeInfoTrigger.getBoundingClientRect();
			const viewport = history[0].getBoundingClientRect();
			if (box.bottom < viewport.top || box.top > viewport.bottom) closeInfoPanel("owner");
		}
		scanVisible();
	});
	const focusChanged = () => { cancelObservations(); if (document.hasFocus()) scanVisible(); };
	window.addEventListener("blur", focusChanged); window.addEventListener("focus", focusChanged);
	window.addEventListener("resize", fitHeight);
	const intersection = new IntersectionObserver(scanVisible, {root: history[0], threshold: [0, .75, 1]});
	const contentObserver = new MutationObserver(() => {
		intersection.disconnect();
		for (const item of evidenceItems()) if (item.element) intersection.observe(item.element);
		scanVisible();
	});
	contentObserver.observe(history[0], {childList: true});
	const scheduleScanVisible = () => {
		if (!active || !sender || !history[0].childElementCount || scanRaf) return;
		scanRaf = requestAnimationFrame(() => { scanRaf = null; scanVisible(); });
	};
	const visibilityObserver = new MutationObserver(scheduleScanVisible);
	visibilityObserver.observe(document.body, {subtree: true, childList: true, attributes: true, attributeFilter: ["class", "style", "hidden"]});
	const resizeObserver = new ResizeObserver(fitHeight);
	resizeObserver.observe(page.body[0]);
	page.set_primary_action(__("Refresh"), () => refresh());
	page.add_menu_item(__("Communication Channel Account"), () => frappe.set_route("List", "Communication Channel Account", {channel: "emulator"}));
	page.add_menu_item(__("Add emulator contact"), () => frappe.new_doc("Emulator Contact"));
	wrapper.emulatorShow = () => {
		active = true; fitHeight();
		if (initialLoadStarted) { initialLoadStarted = false; return; }
		refresh();
	};
	wrapper.emulatorHide = () => {
		initialLoadStarted = false; active = false; cancelObservations(); generation++; cancelLongPress(); closeInfoPanel("owner");
		if (activeHistoryXhr) { try { activeHistoryXhr.abort(); } catch { /* ignore */ } activeHistoryXhr = null; }
		if (scanRaf) { cancelAnimationFrame(scanRaf); scanRaf = null; }
		clearTimeout(timer); timer = null; clearTimeout(realtimeTimer); realtimeTimer = null; diagnosticsMenu.close(); dialog?.hide(); confirmDialog?.hide();
	};
	const visibility = () => { cancelObservations(); if (document.hidden) { clearTimeout(timer); timer = null; clearTimeout(realtimeTimer); realtimeTimer = null; } else if (active) refresh(); };
	document.addEventListener("visibilitychange", visibility);
	$(wrapper).on("hide", wrapper.emulatorHide);
	$(wrapper).on("remove", () => {
		wrapper.emulatorHide(); document.removeEventListener("visibilitychange", visibility);
		frappe.realtime.off("messaging_conversation_update", onRealtimeUpdate);
		historyCache.clear();
		list.children().each((_, el) => destroyNode(el));
		diagnosticsMenu.destroy(); contentObserver.disconnect(); intersection.disconnect(); visibilityObserver.disconnect(); resizeObserver.disconnect();
		window.removeEventListener("blur", focusChanged); window.removeEventListener("focus", focusChanged); window.removeEventListener("resize", fitHeight);
	});
	initialLoadStarted = true;
	renderHeading(); fitHeight(); refresh();
};
frappe.pages["emulator-console"].on_page_show = wrapper => wrapper.emulatorShow?.();
