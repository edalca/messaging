frappe.pages["conversation-inbox"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Conversation Inbox"),
		single_column: true
	});

	$(wrapper).addClass("inbox-page");
	page.set_title_sub(__("Human attention requests pending and waiting for an agent"));

	const api = "messaging.conversation.doctype.chat_session.inbox.";
	const humanApi = "messaging.conversation.doctype.chat_session.human.";

	const state = {
		selectedSession: null,
		status: "",
		channel: "",
		account: "",
		search: "",
		searchCondition: "like",
		start: 0,
		limit: 30,
		hasMore: true,
		hasMoreMessages: false,
		channels: [],
		accounts: [],
		messages: [],
		sessionData: null,
		busy: false
	};

	const translate = window.__ || ((text) => text);
	const root = $(frappe.render_template("conversation_inbox", { _: translate, __: translate })).appendTo(page.body);

	let active = true;
	// Dynamically compute and fit container height to fill viewport without window scrolling
	const fitHeight = () => {
		if (active && root[0] && root[0].getClientRects().length) {
			root.height(Math.max(0, window.innerHeight - root[0].getBoundingClientRect().top - 16));
		}
	};

	const call = async (method, args = {}, isHuman = false) => {
		const base = isHuman ? humanApi : api;
		const res = await frappe.call({ method: base + method, args });
		return res.message;
	};

	const formatWaitTime = (seconds) => {
		if (!seconds || seconds <= 0) return __("Just now");
		const mins = Math.floor(seconds / 60);
		const hours = Math.floor(mins / 60);
		const days = Math.floor(hours / 24);
		if (days > 0) return `${days}d ${hours % 24}h`;
		if (hours > 0) return `${hours}h ${mins % 60}m`;
		if (mins > 0) return `${mins}m`;
		return `${seconds}s`;
	};

	const getInitials = (name) => {
		if (!name) return "?";
		const clean = name.trim();
		if (!clean) return "?";
		if (/^\+?\d+$/.test(clean)) return "#";
		const parts = clean.split(/[\s-]+/);
		if (parts.length >= 2) {
			return (parts[0][0] + parts[1][0]).toUpperCase();
		}
		return clean.substring(0, 2).toUpperCase();
	};

	const escapeHtml = (str) => frappe.utils.escape_html(str || "");
	const iconHtml = (name, size = "xs", extraClass = "") => (
		frappe.utils?.icon ? frappe.utils.icon(name, size, "", "", extraClass, true) : ""
	);

	// Initialize Channel filter dropdown and data
	const loadChannels = async () => {
		try {
			const data = await call("channels_and_accounts", {});
			state.channels = data.channels || [];
			state.accounts = data.accounts || [];
		} catch (err) {
			console.error("Error loading channels", err);
		}
	};

	// Same Desk menu as the list/report view switcher. Options are read on
	// each open so the check mark follows the channel selected right now.
	const channelFilterOptions = () =>
		state.channels.map((channel) => ({
			label: channel.channel_name || channel.name,
			selected: state.channel === channel.name,
			onclick: () => setChannelFilter(channel.name)
		}));

	const channelDropdown = frappe.ui?.Dropdown
		? new frappe.ui.Dropdown({
			trigger: root.find(".btn-channel-filter"),
			empty_text: __("No channels available"),
			options: channelFilterOptions
		})
		: null;

	// Deterministic theme color for tag badges matching Frappe Form's exact get_tag_theme
	const getTagTheme = (label) => {
		const themes = ["blue", "green", "amber", "red", "violet"];
		let hash = 0;
		const str = String(label || "");
		for (let i = 0; i < str.length; i++) {
			hash = (hash * 31 + str.charCodeAt(i)) % 997;
		}
		return themes[hash % themes.length];
	};

	// Render channel tag badge matching Frappe Form's exact Tag component
	const renderActiveTag = (label) => {
		const bar = root.find(".queue-active-filter-bar").empty();
		if (!label) {
			bar.addClass("hidden");
			return;
		}

		let $tag;
		const theme = getTagTheme(label);
		if (typeof frappe.ui?.badge === "function") {
			$tag = frappe.ui.badge({
				label: label,
				theme: theme,
				size: "md",
				icon_right: "x",
				css_class: "form-tag active-channel-tag",
				title: label
			});
			$tag.find(".es-badge__label").addClass("pill-label ellipsis");
		} else {
			const xIcon = frappe.utils?.icon ? frappe.utils.icon("x", "sm", "", "", "", true) : "&times;";
			$tag = $(`
				<span class="es-badge form-tag active-channel-tag" data-theme="${theme}" data-size="md" title="${escapeHtml(label)}">
					<span class="es-badge__label pill-label ellipsis">${escapeHtml(label)}</span>
					<span class="es-badge__affix" role="button" tabindex="0" aria-label="${__("Remove filter")}">${xIcon}</span>
				</span>
			`);
		}

		const $remove = $tag.find(".es-badge__affix");
		$remove
			.attr({ role: "button", tabindex: 0, "aria-label": __("Remove filter") })
			.on("click", (e) => {
				e.preventDefault();
				e.stopPropagation();
				setChannelFilter("");
			})
			.on("keydown", (e) => {
				if (e.key === "Enter" || e.key === " ") {
					e.preventDefault();
					setChannelFilter("");
				}
			});

		bar.append($tag).removeClass("hidden");
	};

	const matchIcon = (condition) => {
		const name = condition === "=" ? "equal" : "equal-approximately";
		if (frappe.utils?.icon) return frappe.utils.icon(name);
		return "";
	};

	const initFilterButton = () => {
		const funnelSvg = frappe.utils?.icon ? frappe.utils.icon("funnel") : "";
		const xSvg = frappe.utils?.icon ? frappe.utils.icon("x") : "";
		root.find(".btn-channel-filter .filter-icon").html(funnelSvg);
		root.find(".filter-x-button .filter-icon").html(xSvg);
		root.find(".queue-search-match-icon").html(matchIcon(state.searchCondition));
	};

	const setChannelFilter = async (channelName) => {
		state.channel = channelName || "";
		state.account = "";
		state.start = 0;

		const filterBtn = root.find(".btn-channel-filter");
		const filterXBtn = root.find(".filter-x-button");
		const filterLabel = filterBtn.find(".button-label");

		if (state.channel) {
			const chObj = state.channels.find((c) => c.name === state.channel);
			const label = chObj ? (chObj.channel_name || chObj.name) : state.channel;

			renderActiveTag(label);

			filterBtn.removeClass("btn-default").addClass("btn-primary-light");
			filterLabel.html(__("Filters {0}", [`<span class="filter-label">1</span>`]));
			filterBtn.attr("title", __("1 Filter Applied"));
			filterXBtn.prop("disabled", false);
		} else {
			renderActiveTag(null);

			filterBtn.removeClass("btn-primary-light").addClass("btn-default");
			filterLabel.text(__("Filter"));
			filterBtn.attr("title", __("Filter by Channel"));
			filterXBtn.prop("disabled", false);
		}

		await refreshAll(false);
		fitHeight();
	};

	// Load operational summary counters (pending requests queue only)
	const loadSummary = async () => {
		try {
			const summary = await call("inbox_summary", {
				channel: state.channel,
				account: state.account
			});
			root.find(".metric-total").text(summary.total || 0);
			root.find(".metric-waiting").text(summary.waiting || 0);
			root.find(".metric-pending").text(summary.pending || 0);
		} catch (err) {
			console.error("Error loading inbox summary", err);
		}
	};

	const statusBadgeMeta = (status) => {
		if (status === "Human Queued") {
			return { label: __("Waiting for Agent"), cardClass: "status-badge-queued", pillClass: "badge-info" };
		}
		if (status === "Human Requested") {
			return { label: __("Pending"), cardClass: "status-badge-requested", pillClass: "badge-warning" };
		}
		return { label: __(status || ""), cardClass: "status-badge-requested", pillClass: "badge-secondary" };
	};

	const syncStatusButtons = () => {
		root.find(".filter-status-btn").each(function () {
			const val = $(this).data("status") || "";
			$(this).toggleClass("active", val === state.status);
		});
	};

	// Load unassigned pending requests queue with continuous infinite scroll support
	const loadQueue = async (preserveSelection = true, reset = true, options = {}) => {
		const { preservePagination = false, silent = false } = options;
		const listContainer = root.find(".queue-items-list");
		const existingCount = listContainer.find(".queue-item-card").length;
		const fetchLimit = (reset && preservePagination && existingCount > state.limit)
			? Math.min(100, existingCount)
			: state.limit;

		if (reset) {
			state.start = 0;
			state.hasMore = true;
		}

		const scrollEl = root.find(".queue-items-scroll")[0];
		const prevScrollTop = scrollEl ? scrollEl.scrollTop : 0;

		if (!silent) {
			root.find(".queue-loading-placeholder").removeClass("hidden");
		}
		if (reset) {
			root.find(".queue-empty-message").addClass("hidden");
		}

		try {
			const rows = await call("conversations", {
				status: state.status,
				channel: state.channel,
				account: state.account,
				search: state.search,
				search_condition: state.searchCondition,
				unassigned_only: 1,
				start: state.start,
				limit: fetchLimit
			});

			root.find(".queue-loading-placeholder").addClass("hidden");
			if (reset) {
				listContainer.empty();
			}

			if (!rows || rows.length === 0) {
				state.hasMore = false;
				if (reset) {
					showQueueEmpty();
					if (state.selectedSession) {
						clearDetailView();
					}
				}
				return;
			}

			state.hasMore = rows.length >= fetchLimit;
			if (reset && rows.length > state.limit) {
				state.start = rows.length - state.limit;
			}

			for (const row of rows) {
				// Prevent duplicate cards if already rendered
				if (listContainer.find(`.queue-item-card[data-session="${row.name}"]`).length > 0) {
					continue;
				}

				const isSelected = state.selectedSession === row.name;
				const initials = getInitials(row.contact_name);
				const badgeMeta = statusBadgeMeta(row.status);
				const timeText = formatWaitTime(row.wait_time_seconds);

				const card = $(`
					<div class="queue-item-card ${isSelected ? "active" : ""}" data-session="${row.name}">
						<div class="queue-item-avatar">${escapeHtml(initials)}</div>
						<div class="queue-item-body">
							<div class="queue-item-top">
								<span class="queue-item-name" title="${escapeHtml(row.contact_name)}">${escapeHtml(row.contact_name)}</span>
								<span class="queue-item-time" title="${__("Waiting Time")}">${iconHtml("clock", "xs")}${escapeHtml(timeText)}</span>
							</div>
							<div class="queue-item-provenance" title="${escapeHtml(row.channel_name)} · ${escapeHtml(row.account_name)}">
								<span class="tag-channel">${escapeHtml(row.channel_name || row.channel)}</span>
								<span class="tag-account">${escapeHtml(row.account_name || row.account)}</span>
							</div>
							<div class="queue-item-snippet" title="${escapeHtml(row.last_message)}">
								${row.last_message ? escapeHtml(row.last_message) : `<span class="font-italic text-muted">${__("No messages yet")}</span>`}
							</div>
							<div class="queue-item-footer">
								<span class="${badgeMeta.cardClass}">${escapeHtml(badgeMeta.label)}</span>
								${row.unread > 0 ? `<span class="badge-unread-count" title="${__("Unread")}">${row.unread}</span>` : ""}
							</div>
						</div>
					</div>
				`);

				card.on("click", () => selectSession(row.name));
				listContainer.append(card);
			}

			if (reset && scrollEl) {
				scrollEl.scrollTop = prevScrollTop;
			}

			if (reset && state.selectedSession) {
				const stillPresent = rows.some((r) => r.name === state.selectedSession);
				if (!stillPresent) {
					clearDetailView();
				} else if (preserveSelection) {
					root.find(`.queue-item-card[data-session="${state.selectedSession}"]`).addClass("active");
				}
			}
			fitHeight();
		} catch (err) {
			root.find(".queue-loading-placeholder").addClass("hidden");
			console.error("Error loading queue", err);
			frappe.show_alert({ message: __("Failed to load conversations"), indicator: "red" });
		} finally {
			fitHeight();
		}
	};

	// Select request handler (read-only triage inspection)
	const selectSession = async (sessionName, updateListClass = true, options = {}) => {
		const { preserveScroll = false, fromNotification = false } = options;
		if (updateListClass) {
			root.find(".queue-item-card").removeClass("active");
			root.find(`.queue-item-card[data-session="${CSS.escape(sessionName)}"]`).addClass("active");
		}

		state.selectedSession = sessionName;
		root.addClass("mobile-detail-open inbox-has-selection");

		try {
			const data = await call("history", { session_name: sessionName });
			if (state.selectedSession !== sessionName) return;
			const s = data?.session;
			if (!s || (s.status !== "Human Requested" && s.status !== "Human Queued") || s.assigned_agent) {
				clearDetailView();
				if (fromNotification) {
					frappe.show_alert({
						message: __("This request is no longer pending assignment."),
						indicator: "orange"
					});
				}
				await loadSummary();
				await loadQueue(false, true, { silent: true });
				return;
			}
			state.sessionData = data;
			state.messages = data.messages || [];
			state.hasMoreMessages = Boolean(data.has_more);

			renderDetailView(data, { preserveScroll });
			renderContactView(data);
			fitHeight();
		} catch (err) {
			console.error("Error loading session history", err);
			clearDetailView();
			frappe.show_alert({
				message: fromNotification
					? __("You do not have permission to view this request, or it is no longer available.")
					: __("Failed to load conversation details"),
				indicator: fromNotification ? "orange" : "red"
			});
		} finally {
			fitHeight();
		}
	};

	const showQueueEmpty = () => {
		const filtered = Boolean(state.status || state.channel || state.account || state.search);
		root.find(".queue-empty-title").text(__("No pending requests"));
		root.find(".queue-empty-text").text(__("Requests that require human attention will appear here when they arrive."));
		root.find(".queue-empty-idle").toggleClass("hidden", filtered);
		root.find(".queue-empty-filtered").toggleClass("hidden", !filtered);
		root.find(".queue-empty-message").removeClass("hidden");
	};

	// Clear detail and contact views
	const clearDetailView = () => {
		state.selectedSession = null;
		state.sessionData = null;
		state.messages = [];
		state.hasMoreMessages = false;
		root.removeClass("mobile-detail-open inbox-has-selection");

		root.find(".detail-header-bar").addClass("hidden");
		root.find(".detail-content-area").addClass("hidden");
		root.find(".detail-no-selection").removeClass("hidden");

		root.find(".contact-content-area").addClass("hidden");
		root.find(".contact-no-selection").removeClass("hidden");
	};

	// Render center panel (Pending Request evaluation detail; read-only + Take Attention)
	const renderDetailView = (data, options = {}) => {
		const s = data.session;
		const c = data.contact;
		const p = data.provenance;
		const agentStatus = data.agent_status || {};

		root.addClass("inbox-has-selection");
		root.find(".detail-no-selection").addClass("hidden");
		root.find(".detail-header-bar").removeClass("hidden");
		root.find(".detail-content-area").removeClass("hidden");

		// Request header
		root.find(".detail-contact-heading").text(c.display_name || c.external_sender || c.name);

		const channelLabel = p.channel?.channel_name || s.channel_name || s.channel;
		const accountLabel = p.account?.account_name || s.account_name || s.account;
		root.find(".channel-indicator-tag").text(channelLabel);
		root.find(".account-indicator-tag").text(`· ${accountLabel}`);

		const statusMeta = statusBadgeMeta(s.status);
		const statusPill = root.find(".detail-status-pill").empty();
		statusPill.text(statusMeta.label);
		statusPill.removeClass("badge-primary badge-warning badge-info badge-secondary badge-success");
		statusPill.addClass(statusMeta.pillClass);

		// Take Attention action gated strictly by backend eligibility & state
		const takeBtn = root.find(".btn-take-attention");
		const canTake = Boolean(s.can_take && s.status === "Human Queued" && !s.assigned_agent);
		takeBtn
			.prop("disabled", !canTake || state.busy)
			.html(`<i class="fa fa-user-check mr-1"></i>${__("Take Attention")}`);
		if (!canTake) {
			takeBtn.attr(
				"title",
				s.assigned_agent
					? __("Already assigned")
					: (s.status !== "Human Queued" ? __("Request is not queued yet") : __("No available capacity"))
			);
		} else {
			takeBtn.attr("title", __("Take this request"));
		}

		// Contextual capacity / eligibility banner
		const banner = root.find(".detail-attention-banner").addClass("hidden").removeClass("tone-warning tone-info").empty();
		if (!s.assigned_agent && agentStatus && (!agentStatus.has_grant || !agentStatus.available || agentStatus.remaining <= 0)) {
			const warnText = !agentStatus.has_grant
				? __("You do not have an enabled agent grant for this account.")
				: (!agentStatus.available
					? __("Your agent status on this account is currently unavailable.")
					: __("Your capacity on this account is full ({0}/{1}).", [agentStatus.occupied || 0, agentStatus.capacity || 0]));
			banner.addClass("tone-warning").text(warnText).removeClass("hidden");
		}

		// Request summary
		const summaryBody = root.find(".detail-summary-card .summary-card-body").empty();
		const waitSecs = s.wait_time_seconds || 0;
		root.find(".summary-time-waiting").text(`${__("Waiting")}: ${formatWaitTime(waitSecs)}`);

		const humanReqs = data.human_requests || [];
		if (humanReqs.length > 0) {
			const latestReq = humanReqs[humanReqs.length - 1];
			if (latestReq.fields && latestReq.fields.length > 0) {
				for (const f of latestReq.fields) {
					summaryBody.append(`
						<div class="summary-field-row">
							<span class="summary-field-label">${escapeHtml(f.label || f.key)}:</span>
							<span class="summary-field-val">${escapeHtml(f.value != null ? String(f.value) : "")}</span>
						</div>
					`);
				}
			} else {
				summaryBody.append(`<div class="text-muted small">${__("Transfer request submitted from flow.")}</div>`);
			}
		} else {
			const incomingMsgs = state.messages.filter((m) => m.kind === "Incoming");
			const lastIncoming = incomingMsgs.length > 0 ? incomingMsgs[incomingMsgs.length - 1] : null;
			if (lastIncoming && lastIncoming.content) {
				summaryBody.append(`
					<div class="summary-field-row">
						<span class="summary-field-label">${__("Initial Request")}:</span>
						<span class="summary-field-val">${escapeHtml(lastIncoming.content)}</span>
					</div>
				`);
			} else {
				summaryBody.append(`<div class="text-muted small">${__("No structured request parameters recorded.")}</div>`);
			}
		}

		// Messages and events stream
		renderMessagesTimeline(data.messages || [], options);
	};

	const getTransportStatusIcons = (transport) => {
		if (!transport) return [{ icon: "clock", tone: "muted", label: __("Unknown") }];
		const status = transport.status || "";
		const delivery = transport.delivery_status || "";
		const diag = transport.diagnostic_code ? ` (${transport.diagnostic_code})` : "";
		if (status === "Rejected" || delivery === "Failed") {
			const label = status === "Rejected" && delivery === "Failed"
				? `${__("Rejected")} · ${__("Failed")}`
				: status === "Rejected"
					? __("Rejected")
					: `${__(status || "Accepted")} · ${__("Failed")}`;
			return [{ icon: "circle-alert", tone: "error", label: label + diag }];
		}
		if (status === "Sending" || status === "Processing") {
			return [{
				icon: "loader-circle",
				tone: "muted",
				label: `${__(status)}${delivery ? " · " + __(delivery) : ""}`,
				spinning: true,
			}];
		}
		if (["Reserved", "Prepared", "Held", "Pending"].includes(status)) {
			return [{ icon: "clock", tone: "muted", label: __(status) + diag }];
		}
		if (status === "Accepted") {
			if (delivery === "Read") {
				return [{ icon: "check-check", tone: "read", label: `${__("Accepted")} · ${__("Read")}` }];
			}
			if (delivery === "Delivered") {
				return [{ icon: "check-check", tone: "muted", label: `${__("Accepted")} · ${__("Delivered")}` }];
			}
			return [{ icon: "check", tone: "muted", label: `${__("Accepted")} · ${__(delivery || "Pending")}` }];
		}
		return [{
			icon: "clock",
			tone: "muted",
			label: `${__(status || "Unknown")}${delivery ? " · " + __(delivery) : ""}${diag}`,
		}];
	};

	const renderStatusIconsHtml = (transport) => {
		const specs = getTransportStatusIcons(transport);
		if (!specs.length) return "";
		return `<span class="msg-status-icons">${specs.map((spec) => (
			`<span class="msg-status-icon" data-icon="${escapeHtml(spec.icon)}" data-tone="${escapeHtml(spec.tone || "muted")}" ` +
			`role="img" aria-label="${escapeHtml(spec.label)}" title="${escapeHtml(spec.label)}">` +
			`${iconHtml(spec.icon, "xs", spec.spinning ? "inbox-icon-spin" : "")}` +
			`</span>`
		)).join("")}</span>`;
	};

	// Render message timeline
	const renderMessagesTimeline = (messages, options = {}) => {
		const container = root.find(".detail-content-area")[0];
		const wasNearBottom = container
			? (container.scrollHeight - container.scrollTop - container.clientHeight < 90)
			: true;
		const prevScrollTop = container ? container.scrollTop : 0;

		const stream = root.find(".detail-messages-stream").empty();

		for (const m of messages) {
			if (m.kind === "Event") {
				const eventText = __(m.content || "System Event");
				stream.append(`
					<div class="system-event-bubble">
						<span>${escapeHtml(eventText)}</span>
					</div>
				`);
				continue;
			}

			const isIncoming = m.kind === "Incoming";
			const isAgent = m.kind === "Human";

			let bubbleClass = "incoming";
			let senderLabel = "";

			if (isIncoming) {
				bubbleClass = "incoming";
			} else if (isAgent) {
				bubbleClass = "outgoing-agent";
				senderLabel = m.origin_label || "";
			} else {
				bubbleClass = "outgoing-bot";
				senderLabel = m.origin_label || "";
			}

			let timeStr = "";
			if (m.sent_at) {
				try {
					timeStr = frappe.datetime.str_to_user(m.sent_at);
				} catch (e) {
					timeStr = m.sent_at;
				}
			}

			const deliveryHtml = !isIncoming ? renderStatusIconsHtml(m.transport) : "";

			stream.append(`
				<div class="msg-row">
					<div class="msg-bubble ${bubbleClass}">
						${senderLabel ? `<div class="msg-sender-tag">${escapeHtml(senderLabel)}</div>` : ""}
						<div class="msg-text">${escapeHtml(m.content || "")}</div>
						<div class="msg-meta-stamp">
							<span>${escapeHtml(timeStr)}</span>
							${deliveryHtml}
						</div>
					</div>
				</div>
			`);
		}

		if (!messages || messages.length === 0) {
			stream.append(`<div class="text-center p-4 text-muted small">${__("No retained messages are available.")}</div>`);
		}

		// Earlier messages trigger button
		const earliestSeq = messages.length > 0 ? messages[0].sequence : 0;
		const showEarlier = Boolean(state.hasMoreMessages && messages.length && earliestSeq > 1);
		root.find(".btn-load-earlier").toggleClass("hidden", !showEarlier);

		if (container) {
			if (!options.preserveScroll || wasNearBottom) {
				container.scrollTop = container.scrollHeight;
			} else {
				container.scrollTop = prevScrollTop;
			}
		}
	};

	// Render right panel (Contact and origin information)
	const renderContactView = (data) => {
		const c = data.contact;
		const s = data.session;
		const p = data.provenance;

		root.find(".contact-no-selection").addClass("hidden");
		root.find(".contact-content-area").removeClass("hidden");

		// Avatar & display name
		const initials = getInitials(c.display_name || c.external_sender);
		root.find(".contact-initials-badge").text(initials);
		root.find(".contact-name-heading").text(c.display_name || c.name);
		root.find(".contact-sender-id").text(c.external_sender ? `${__("Sender ID")}: ${c.external_sender}` : "");

		// Provenance section
		const channelName = p.channel?.channel_name || s.channel_name || s.channel;
		const accountName = p.account?.account_name || s.account_name || s.account;
		const accountId = p.account?.account_id || s.account_id || s.account;
		const publicId = p.account?.public_id || s.account_public_id;
		const providerName = p.channel?.provider || s.provider || "";

		root.find(".provenance-val-channel").text(channelName);
		root.find(".provenance-val-account").text(accountName);
		root.find(".provenance-val-account-id").text(accountId);
		root.find(".provenance-val-provider").text(providerName);

		if (publicId && publicId !== accountId) {
			root.find(".row-public-id").removeClass("hidden");
			root.find(".provenance-val-public-id").text(publicId);
		} else {
			root.find(".row-public-id").addClass("hidden");
		}

		// Channel account document link when authorized
		if (p.can_view_account && accountId) {
			root.find(".provenance-link-wrap").removeClass("hidden");
			root.find(".btn-open-account-document").off("click").on("click", () => {
				frappe.set_route("Form", "Communication Channel Account", accountId);
			});
		} else {
			root.find(".provenance-link-wrap").addClass("hidden");
		}

		// Operational timings
		const waitSecs = s.wait_time_seconds || 0;
		root.find(".timing-val-waiting").text(formatWaitTime(waitSecs));

		const formatStamp = (value) => {
			if (!value) return "-";
			try {
				return frappe.datetime.str_to_user(value);
			} catch (e) {
				return value;
			}
		};

		root.find(".timing-val-activity").text(formatStamp(s.last_activity));

		const agentStatus = data.agent_status;
		if (agentStatus && agentStatus.has_grant) {
			const availLabel = agentStatus.available ? __("Available") : __("Unavailable");
			root.find(".row-agent-capacity").removeClass("hidden");
			root.find(".timing-val-agent-capacity").text(`${agentStatus.occupied || 0} / ${agentStatus.capacity || 0} (${availLabel})`);
		} else {
			root.find(".row-agent-capacity").addClass("hidden");
		}

		const regStatus = c.registration_status || "Pending";
		root.find(".timing-val-registration").text(__(regStatus)).removeClass("badge-success badge-light");
		if (regStatus === "Complete") {
			root.find(".timing-val-registration").addClass("badge-success");
		} else {
			root.find(".timing-val-registration").addClass("badge-light");
		}

		// Registration data fields
		const customFields = c.profile_fields || [];
		const customSection = root.find(".contact-custom-data-section");
		const customList = root.find(".contact-custom-data-list").empty();

		if (customFields.length > 0) {
			customSection.removeClass("hidden");
			for (const f of customFields) {
				customList.append(`
					<div class="provenance-row mb-1">
						<span class="text-muted">${escapeHtml(f.label || f.key)}:</span>
						<span class="font-weight-medium">${escapeHtml(f.value != null ? String(f.value) : "")}</span>
					</div>
				`);
			}
		} else {
			customSection.addClass("hidden");
		}

		// View contact history button
		root.find(".btn-open-contact-history").off("click").on("click", () => showContactHistoryDialog(c.name, c.display_name));
	};

	// Read-only modal for contact's previous sessions
	const showContactHistoryDialog = async (contactName, contactDisplayName) => {
		const dialog = new frappe.ui.Dialog({
			title: `${__("Contact History")}: ${contactDisplayName || contactName}`,
			size: "large",
			fields: [{ fieldname: "history_html", fieldtype: "HTML" }]
		});

		const wrapper = dialog.fields_dict.history_html.$wrapper;
		wrapper.html(`
			<div class="text-center p-3 text-muted">
				<span class="spinner-border spinner-border-sm mr-2" role="status"></span>
				${__("Loading previous conversations...")}
			</div>
		`);
		dialog.show();

		try {
			const profile = await call("contact_profile", { contact_name: contactName });
			const convs = profile.conversations || [];
			wrapper.empty();

			if (convs.length === 0) {
				wrapper.html(`<div class="text-muted p-4 text-center">${__("No previous conversations found for this contact.")}</div>`);
				return;
			}

			const table = $(`
				<table class="table table-bordered table-sm small m-0">
					<thead>
						<tr>
							<th>${__("Session")}</th>
							<th>${__("Status")}</th>
							<th>${__("Channel")}</th>
							<th>${__("Account")}</th>
							<th>${__("Assigned Agent")}</th>
							<th>${__("Last Activity")}</th>
						</tr>
					</thead>
					<tbody></tbody>
				</table>
			`);

			const tbody = table.find("tbody");
			for (const conv of convs) {
				let actTime = conv.last_activity || "";
				try {
					actTime = frappe.datetime.str_to_user(conv.last_activity);
				} catch (e) {}

				tbody.append(`
					<tr>
						<td class="font-weight-bold text-monospace">${escapeHtml(conv.name)}</td>
						<td><span class="badge badge-light">${escapeHtml(__(conv.status))}</span></td>
						<td>${escapeHtml(conv.channel_name || conv.channel)}</td>
						<td>${escapeHtml(conv.account_name || conv.account)}</td>
						<td>${escapeHtml(conv.assigned_agent || __("Unassigned"))}</td>
						<td>${escapeHtml(actTime)}</td>
					</tr>
				`);
			}

			wrapper.append(table);
		} catch (err) {
			console.error("Error loading contact history", err);
			wrapper.html(`<div class="text-danger p-3 text-center">${__("Failed to load contact history.")}</div>`);
		}
	};

	// Take Attention execution -> assigns via official human.act and removes from pending queue
	const takeAttention = async () => {
		if (!state.selectedSession || state.busy) return;
		const sessionName = state.selectedSession;
		const takeBtn = root.find(".btn-take-attention");

		state.busy = true;
		takeBtn.prop("disabled", true).html(`<span class="spinner-border spinner-border-sm mr-1"></span>${__("Taking...")}`);

		try {
			await call("act", { session_name: sessionName, action: "Take" }, true);
			frappe.show_alert({
				message: __("Conversation taken successfully. Assigned to you."),
				indicator: "green"
			});

			clearDetailView();
			await loadSummary();
			await loadQueue(false, true);
		} catch (err) {
			console.error("Error taking attention", err);
			frappe.msgprint({
				title: __("Cannot Take Conversation"),
				message: __("This conversation was already taken or changed status. Refreshing inbox..."),
				indicator: "orange"
			});
			clearDetailView();
			await refreshAll(false);
		} finally {
			state.busy = false;
		}
	};

	// Refresh summary, queue, and optionally currently selected pending session
	const refreshAll = async (preserveSelection = true, options = {}) => {
		const { preservePagination = false, silent = false, reloadHistory = true } = options;
		await loadSummary();
		await loadQueue(preserveSelection, true, { preservePagination, silent });
		if (reloadHistory && preserveSelection && state.selectedSession) {
			await selectSession(state.selectedSession, false, { preserveScroll: true });
		}
	};

	// Event handlers
	root.find(".btn-take-attention").on("click", takeAttention);

	// Summary metric card shortcuts filter the pending queue
	root.on("click", ".summary-metric-card", function () {
		const filterStatus = $(this).data("filter") || "";
		state.status = filterStatus;
		state.start = 0;
		syncStatusButtons();
		loadQueue(false, true);
	});

	// Status filter pills (All / Waiting / Pending)
	root.on("click", ".filter-status-btn", function () {
		state.status = $(this).data("status") || "";
		state.start = 0;
		syncStatusButtons();
		loadQueue(false, true);
	});

	// Clear filter x-button beside filter button
	root.on("click", ".filter-x-button", function (e) {
		e.preventDefault();
		e.stopPropagation();
		channelDropdown?.close("owner");
		setChannelFilter("");
	});

	root.on("click", ".match-type-dropdown-menu .dropdown-item", function (e) {
		e.preventDefault();
		const condition = $(this).data("match-type") === "=" ? "=" : "like";
		if (condition === state.searchCondition) return;
		state.searchCondition = condition;
		const button = root.find(".match-type-dropdown-btn");
		button.attr("title", condition === "=" ? __("Equals") : __("Like"));
		root.find(".queue-search-match-icon").html(matchIcon(condition));
		if (state.search) {
			state.start = 0;
			loadQueue(false, true);
		}
	});

	// Search input with debounce
	let searchTimeout = null;
	root.find(".queue-search-input").on("input", function () {
		clearTimeout(searchTimeout);
		searchTimeout = setTimeout(() => {
			state.search = $(this).val().trim();
			state.start = 0;
			loadQueue(false, true);
		}, 300);
	});

	// Infinite scroll on requests queue
	root.find(".queue-items-scroll").on("scroll", function () {
		const el = this;
		if (el.scrollHeight - el.scrollTop - el.clientHeight < 60 && !state.busy && state.hasMore) {
			state.start += state.limit;
			loadQueue(true, false);
		}
	});

	// Mobile back navigation button
	root.find(".btn-nav-back-mobile").on("click", () => {
		root.removeClass("mobile-detail-open");
	});

	// Load earlier messages action
	root.find(".btn-load-earlier").on("click", async () => {
		if (!state.selectedSession || !state.messages.length) return;
		const earliest = state.messages[0].sequence;
		try {
			const data = await call("history", { session_name: state.selectedSession, before: earliest });
			if (data && data.messages && data.messages.length > 0) {
				state.hasMoreMessages = Boolean(data.has_more);
				state.messages = [...data.messages, ...state.messages];
				renderMessagesTimeline(state.messages, { preserveScroll: true });
			} else {
				state.hasMoreMessages = false;
				root.find(".btn-load-earlier").addClass("hidden");
			}
		} catch (err) {
			console.error("Error loading earlier messages", err);
		}
	});

	// Page primary action: Refresh
	page.set_primary_action(__("Refresh"), () => refreshAll(true), "refresh");

	// Realtime synchronization without excessive polling or unnecessary history calls
	let realtimeTimer = null;
	const onRealtimeUpdate = (payload) => {
		if (!active || frappe.get_route()[0] !== "conversation-inbox" || document.hidden) return;
		if (state.channel && payload?.channel && payload.channel !== state.channel) return;
		clearTimeout(realtimeTimer);
		realtimeTimer = setTimeout(async () => {
			if (!active || frappe.get_route()[0] !== "conversation-inbox" || state.busy) return;
			await loadSummary();
			await loadQueue(true, true, { preservePagination: true, silent: true });
			if (
				state.selectedSession
				&& (!payload?.session || payload.session === state.selectedSession)
			) {
				await selectSession(state.selectedSession, false, { preserveScroll: true });
			}
		}, 250);
	};
	frappe.realtime.on("messaging_conversation_update", onRealtimeUpdate);

	const consumeRequestedSession = () => {
		let requested = null;
		if (frappe.route_options && frappe.route_options.session) {
			requested = String(frappe.route_options.session || "").trim() || null;
			delete frappe.route_options.session;
		}
		if (!requested) {
			const params = new URLSearchParams(window.location.search || "");
			const paramVal = params.get("session");
			if (paramVal) {
				requested = paramVal.trim() || null;
			}
		}
		if (requested) {
			const url = new URL(window.location.href);
			if (url.searchParams.has("session")) {
				url.searchParams.delete("session");
				window.history.replaceState(null, "", url.pathname + url.search + url.hash);
			}
		}
		return requested;
	};

	const openRequestedSession = async (sessionName) => {
		if (!sessionName) return;
		await selectSession(sessionName, true, { fromNotification: true });
	};

	// Initial data load and height adjustment
	initFilterButton();
	let pendingTargetSession = consumeRequestedSession();
	let initialReady = loadChannels()
		.then(() => refreshAll(false))
		.then(async () => {
			fitHeight();
			const target = pendingTargetSession || consumeRequestedSession();
			pendingTargetSession = null;
			if (target) {
				await openRequestedSession(target);
			}
		});

	// Dynamic height synchronization with window and page body resize
	window.addEventListener("resize", fitHeight);
	const resizeObserver = new ResizeObserver(fitHeight);
	resizeObserver.observe(page.body[0]);

	// Page show and hide lifecycle handlers
	wrapper.inboxShow = async () => {
		active = true;
		fitHeight();
		if (initialReady) {
			await initialReady;
			initialReady = null;
			const laterTarget = consumeRequestedSession();
			if (laterTarget) {
				await openRequestedSession(laterTarget);
			}
			return;
		}
		await refreshAll(true);
		const target = consumeRequestedSession();
		if (target) {
			await openRequestedSession(target);
		}
	};
	wrapper.inboxHide = () => {
		active = false;
		clearTimeout(realtimeTimer);
	};

	const onRouteChange = () => {
		if (active && frappe.get_route()[0] === "conversation-inbox") {
			const target = consumeRequestedSession();
			if (target) {
				Promise.resolve(initialReady).then(() => openRequestedSession(target));
			}
		}
	};
	frappe.router?.on?.("change", onRouteChange);

	$(wrapper).on("hide", wrapper.inboxHide);

	// Low-frequency fallback sync for counters/queue when websocket is idle (no history polling)
	const fallbackSyncTimer = setInterval(() => {
		if (active && frappe.get_route()[0] === "conversation-inbox" && !document.hidden && !state.busy) {
			loadSummary();
			loadQueue(true, true, { preservePagination: true, silent: true });
		}
	}, 60000);

	$(wrapper).on("remove", () => {
		channelDropdown?.destroy();
		wrapper.inboxHide();
		clearInterval(fallbackSyncTimer);
		clearTimeout(realtimeTimer);
		frappe.router?.off?.("change", onRouteChange);
		frappe.realtime.off("messaging_conversation_update", onRealtimeUpdate);
		resizeObserver.disconnect();
		window.removeEventListener("resize", fitHeight);
	});

	// Initial layout calculation
	fitHeight();
};

frappe.pages["conversation-inbox"].on_page_show = (wrapper) => wrapper.inboxShow?.();
