/* global frappe, __ */
"use strict";

function createMyConversationsHelpers() {
	function safeText(value) {
		return value == null ? "" : String(value);
	}

	function escapeHtml(value) {
		return safeText(value)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;")
			.replace(/'/g, "&#39;");
	}

	function iconHtml(name, size, svgClass) {
		if (typeof frappe !== "undefined" && frappe.utils && typeof frappe.utils.icon === "function") {
			return frappe.utils.icon(name, size || "sm", "", "", svgClass || "", true);
		}
		return "";
	}

	function initialsOf(name) {
		const raw = safeText(name).trim();
		if (!raw) return "?";
		if (/^\+?\d+$/.test(raw)) return "#";
		const parts = raw.split(/\s+/).filter(Boolean);
		if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
		return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
	}

	function parseUtcDate(iso) {
		if (!iso) return null;
		const str = safeText(iso).trim();
		if (!str) return null;
		const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(str);
		const normalized = hasTz ? str : `${str.replace(" ", "T")}Z`;
		const dt = new Date(normalized);
		return Number.isNaN(dt.getTime()) ? null : dt;
	}

	function formatTime(iso) {
		const dt = parseUtcDate(iso);
		if (!dt) return "";
		try {
			return dt.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
		} catch (e) {
			return safeText(iso).slice(11, 16);
		}
	}

	function formatDateTime(iso) {
		const dt = parseUtcDate(iso);
		if (!dt) return safeText(iso);
		try {
			return dt.toLocaleString([], {
				year: "numeric",
				month: "short",
				day: "2-digit",
				hour: "2-digit",
				minute: "2-digit",
			});
		} catch (e) {
			return safeText(iso);
		}
	}

	function formatRelative(iso) {
		const dt = parseUtcDate(iso);
		if (!dt) return "";
		const diffSec = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
		if (diffSec < 60) return "0m";
		const mins = Math.floor(diffSec / 60);
		if (mins < 60) return `${mins}m`;
		const hrs = Math.floor(mins / 60);
		if (hrs < 24) return `${hrs}h`;
		const days = Math.floor(hrs / 24);
		return `${days}d`;
	}

	function t(key) {
		return typeof __ === "function" ? __(key) : key;
	}

	function statusLabel(status) {
		if (status === "Human Active") return t("Active human attention");
		return t(status || "");
	}

	function closeReasonLabel(session) {
		if (!session) return "";
		if (session.close_reason_label) return session.close_reason_label;
		if (session.close_reason === "Manual") return t("Closed manually");
		if (session.close_reason === "Inactivity") return t("Closed due to inactivity");
		if (session.close_reason === "Technical Error") return t("Technical error");
		return session.close_reason ? t(session.close_reason) : "";
	}

	function getTransportStatusSpecs(transport, sendState) {
		if (sendState === "sending") {
			return [{icon: "clock", tone: "muted", label: t("Sending...")}];
		}
		if (sendState === "failed") {
			return [{icon: "circle-alert", tone: "error", label: t("Send error")}];
		}
		if (!transport || !transport.status) {
			return [{icon: "clock", tone: "muted", label: t("Unknown")}];
		}
		const status = transport.status;
		const delivery = transport.delivery_status;
		const diagSuffix = transport.diagnostic_code ? ` (${transport.diagnostic_code})` : "";
		if (status === "Rejected" || status === "Failed" || delivery === "Failed") {
			const baseLabel = status === "Rejected" && delivery === "Failed"
				? `${t("Rejected")} · ${t("Failed")}`
				: status === "Rejected" || status === "Failed"
					? t(status)
					: `${t(status || "Accepted")} · ${t("Failed")}`;
			return [{icon: "circle-alert", tone: "error", label: `${baseLabel}${diagSuffix}`}];
		}
		if (status === "Sending" || status === "Processing") {
			return [{icon: "loader-circle", tone: "muted", label: `${t(status)}${delivery ? " · " + t(delivery) : ""}`, spinning: true}];
		}
		if (["Reserved", "Prepared", "Held", "Pending"].includes(status)) {
			return [{icon: "clock", tone: "muted", label: t(status)}];
		}
		if (status === "Read" || (status === "Accepted" && delivery === "Read")) {
			const label = status === "Accepted" ? `${t("Accepted")} · ${t("Read")}` : t("Read");
			return [{icon: "check-check", tone: "read", label}];
		}
		if (status === "Delivered" || (status === "Accepted" && delivery === "Delivered")) {
			const label = status === "Accepted" ? `${t("Accepted")} · ${t("Delivered")}` : t("Delivered");
			return [{icon: "check-check", tone: "muted", label}];
		}
		if (status === "Sent" || status === "Accepted") {
			const label = status === "Accepted"
				? `${t("Accepted")} · ${t(delivery || "Pending")}`
				: t("Sent");
			return [{icon: "check", tone: "muted", label}];
		}
		return [{icon: "clock", tone: "muted", label: `${t(status)}${delivery ? " · " + t(delivery) : ""}`}];
	}

	function mergeTimelineMessages(persisted, pending) {
		const seenClientIds = new Set();
		const seenNames = new Set();
		const merged = [];
		for (const m of persisted || []) {
			if (!m) continue;
			if (m.name) seenNames.add(m.name);
			if (m.client_id) seenClientIds.add(m.client_id);
			merged.push(m);
		}
		for (const p of pending || []) {
			if (!p) continue;
			if (p.name && seenNames.has(p.name)) continue;
			if (p.client_id && seenClientIds.has(p.client_id)) continue;
			merged.push(p);
		}
		return merged;
	}

	function collectDeduplicatedRegistration(profileFields, registration) {
		if (!registration || typeof registration !== "object") return [];
		const shown = new Set();
		for (const pf of profileFields || []) {
			if (!pf) continue;
			if (pf.key) shown.add(safeText(pf.key).trim().toLowerCase());
			if (pf.label) shown.add(safeText(pf.label).trim().toLowerCase());
			if (pf.value != null && pf.key === "name") shown.add("name");
		}
		const rows = [];
		for (const [k, v] of Object.entries(registration)) {
			if (v == null || safeText(v).trim() === "") continue;
			const norm = safeText(k).trim().toLowerCase();
			if (shown.has(norm)) continue;
			rows.push({
				key: k,
				label: k.replace(/_/g, " ").replace(/^\w/, c => c.toUpperCase()),
				value: v,
			});
		}
		return rows;
	}

	function buildMessageAriaLabel(m) {
		return m.origin_label ? `${m.origin_label}: ${safeText(m.content)}` : safeText(m.content);
	}

	function renderSessionCardHtml(row, selectedName) {
		const isSelected = row.name === selectedName;
		const displayName = row.contact_name || row.contact_external_sender || row.contact || row.name;
		const channelLabel = row.channel_name || row.channel || "";
		const accountLabel = row.account_name || row.account_public_id || row.account || "";
		const unread = Number(row.unread || 0);
		return [
			`<button type="button" class="mc-session-item ${isSelected ? "is-selected" : ""}" data-session="${escapeHtml(row.name)}" data-testid="mc-session-item">`,
			`<div class="mc-item-avatar">${escapeHtml(initialsOf(displayName))}</div>`,
			`<div class="mc-item-body">`,
			`<div class="mc-item-top">`,
			`<span class="mc-item-name">${escapeHtml(displayName)}</span>`,
			`<span class="mc-item-time">${escapeHtml(formatRelative(row.last_message_time || row.last_activity))}</span>`,
			`</div>`,
			(channelLabel || accountLabel)
				? `<div class="mc-item-provenance">${channelLabel ? `<span class="tag-channel">${escapeHtml(channelLabel)}</span>` : ""}${accountLabel ? `<span class="tag-account">${escapeHtml(accountLabel)}</span>` : ""}</div>`
				: "",
			`<div class="mc-item-preview">${escapeHtml(row.last_message || "")}</div>`,
			unread > 0 ? `<div class="mc-item-footer"><span class="mc-unread-badge" data-testid="mc-unread-badge">${escapeHtml(unread)}</span></div>` : "",
			`</div>`,
			`</button>`,
		].join("");
	}

	return {
		safeText,
		escapeHtml,
		iconHtml,
		initialsOf,
		formatTime,
		formatDateTime,
		formatRelative,
		statusLabel,
		closeReasonLabel,
		getTransportStatusSpecs,
		mergeTimelineMessages,
		collectDeduplicatedRegistration,
		buildMessageAriaLabel,
		renderSessionCardHtml,
	};
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = createMyConversationsHelpers();
}

if (typeof frappe !== "undefined" && frappe.pages) {
	const mcHelpers = createMyConversationsHelpers();
	const API_BASE = "messaging.conversation.doctype.chat_session.inbox";
	const HUMAN_API = "messaging.conversation.doctype.chat_session.human.act";
	const REALTIME_EVENT = "messaging_conversation_update";

	frappe.pages["my-conversations"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("My Conversations"),
			single_column: true,
		});
		$(wrapper).addClass("mc-page");
		page.set_title_sub(__("Assigned conversations requiring your response"));

		const root = $(frappe.render_template("my_conversations")).appendTo(page.body);
		root.find(".mc-empty-icon").html(mcHelpers.iconHtml("message-circle", "md"));
		root.find(".mc-send-btn").html(mcHelpers.iconHtml("arrow-up", "sm"));

		const els = {
			root,
			search: root.find(".mc-search-input"),
			filterBtn: root.find(".btn-mc-channel-filter"),
			filterClearBtn: root.find(".filter-x-button"),
			filterBar: root.find(".mc-active-filter-bar"),
			list: root.find(".mc-list-body"),
			chatEmpty: root.find(".mc-chat-empty"),
			chatActive: root.find(".mc-chat-active"),
			chatHead: root.find(".mc-chat-head"),
			readonlyBanner: root.find(".mc-readonly-banner"),
			timeline: root.find(".mc-timeline"),
			composerArea: root.find(".mc-composer-area"),
			composeState: root.find(".mc-compose-state"),
			composeInput: root.find(".mc-compose-input"),
			sendBtn: root.find(".mc-send-btn"),
			infoEmpty: root.find(".mc-info-empty"),
			infoBody: root.find(".mc-info-body"),
		};

		const state = {
			sessions: [],
			selectedName: null,
			activeData: null,
			channels: [],
			accounts: [],
			filters: {search: "", searchCondition: "like", channel: "", account: ""},
			drafts: new Map(),
			pendingBySession: new Map(),
			sendingBusy: false,
			actionBusy: false,
			loadedLimit: 60,
			realtimeBound: false,
			refreshTimer: null,
			pollTimer: null,
			searchTimer: null,
			pageActive: false,
		};

		function fitHeight() {
			if (state.pageActive && root[0] && root[0].getClientRects().length) {
				root.height(Math.max(460, window.innerHeight - root[0].getBoundingClientRect().top - 16));
			}
		}

		async function callApi(method, args, opts) {
			const res = await frappe.call({
				method,
				args: args || {},
				type: (opts && opts.type) || "GET",
				freeze: Boolean(opts && opts.freeze),
			});
			return res && res.message;
		}

		function matchIcon(condition) {
			return mcHelpers.iconHtml(condition === "=" ? "equal" : "equal-approximately");
		}

		function initFilterControls() {
			root.find(".btn-mc-channel-filter .filter-icon").html(mcHelpers.iconHtml("funnel"));
			root.find(".filter-x-button .filter-icon").html(mcHelpers.iconHtml("x"));
			root.find(".mc-search-match-icon").html(matchIcon(state.filters.searchCondition));
		}

		function getTagTheme(label) {
			const themes = ["blue", "green", "amber", "red", "violet"];
			let hash = 0;
			const str = String(label || "");
			for (let i = 0; i < str.length; i++) {
				hash = (hash * 31 + str.charCodeAt(i)) % 997;
			}
			return themes[hash % themes.length];
		}

		function renderActiveTag(label) {
			els.filterBar.empty();
			if (!label) {
				els.filterBar.addClass("hidden");
				return;
			}
			let $tag;
			const theme = getTagTheme(label);
			if (frappe.ui && typeof frappe.ui.badge === "function") {
				$tag = frappe.ui.badge({
					label,
					theme,
					size: "md",
					icon_right: "x",
					css_class: "form-tag active-channel-tag",
					title: label,
				});
				$tag.find(".es-badge__label").addClass("pill-label ellipsis");
			} else {
				$tag = $(
					`<span class="es-badge form-tag active-channel-tag" data-theme="${theme}" data-size="md" title="${mcHelpers.escapeHtml(label)}">` +
					`<span class="es-badge__label pill-label ellipsis">${mcHelpers.escapeHtml(label)}</span>` +
					`<span class="es-badge__affix" role="button" tabindex="0" aria-label="${mcHelpers.escapeHtml(__("Remove filter"))}">${mcHelpers.iconHtml("x", "sm")}</span>` +
					`</span>`
				);
			}
			$tag.find(".es-badge__affix")
				.attr({role: "button", tabindex: 0, "aria-label": __("Remove filter")})
				.on("click", e => {
					e.preventDefault();
					e.stopPropagation();
					setChannelFilter("");
				});
			els.filterBar.append($tag).removeClass("hidden");
		}

		function updateFilterButtonUi() {
			const hasFilter = Boolean(state.filters.channel || state.filters.account);
			const labelParts = [];
			if (state.filters.channel) {
				const ch = (state.channels || []).find(c => c.name === state.filters.channel);
				labelParts.push((ch && (ch.channel_name || ch.name)) || state.filters.channel);
			}
			if (state.filters.account) {
				const acc = (state.accounts || []).find(a => a.name === state.filters.account);
				labelParts.push((acc && (acc.account_name || acc.account_id || acc.name)) || state.filters.account);
			}
			const activeLabel = labelParts.join(" · ");
			renderActiveTag(activeLabel);
			const labelEl = els.filterBtn.find(".button-label");
			if (hasFilter) {
				els.filterBtn.removeClass("btn-default").addClass("btn-primary-light");
				labelEl.html(__("Filters {0}", [`<span class="filter-label">1</span>`]));
				els.filterBtn.attr("title", __("1 Filter Applied"));
			} else {
				els.filterBtn.removeClass("btn-primary-light").addClass("btn-default");
				labelEl.text(__("Filter"));
				els.filterBtn.attr("title", __("Filter by Channel"));
			}
			fitHeight();
		}

		function setChannelFilter(channelName, accountName) {
			state.filters.channel = channelName || "";
			state.filters.account = accountName || "";
			updateFilterButtonUi();
			refreshAll();
		}

		function filterDropdownOptions() {
			const opts = [];
			for (const c of state.channels || []) {
				const chLabel = c.channel_name || c.name;
				opts.push({
					label: chLabel,
					selected: state.filters.channel === c.name && !state.filters.account,
					onclick: () => setChannelFilter(c.name, ""),
				});
				for (const a of state.accounts || []) {
					if (a.channel !== c.name) continue;
					const accLabel = a.account_name || a.account_id || a.name;
					opts.push({
						label: `${chLabel} · ${accLabel}`,
						selected: state.filters.account === a.name,
						onclick: () => setChannelFilter(c.name, a.name),
					});
				}
			}
			return opts;
		}

		if (frappe.ui && frappe.ui.Dropdown) {
			new frappe.ui.Dropdown({
				trigger: els.filterBtn,
				empty_text: __("No channels available"),
				options: filterDropdownOptions,
			});
		}

		function resizeComposer() {
			const el = els.composeInput[0];
			if (!el) return;
			el.style.height = "auto";
			el.style.height = `${Math.min(160, Math.max(22, el.scrollHeight))}px`;
		}

		function updateComposerControls() {
			const session = state.activeData && state.activeData.session;
			const canReply = Boolean(session && session.can_reply);
			const text = els.composeInput.val().trim();
			els.composerArea.toggleClass("hidden", !session);
			els.composeInput.prop("disabled", !canReply || state.sendingBusy);
			els.sendBtn.prop("disabled", !canReply || !text || state.sendingBusy);
			if (session && !canReply) {
				const reason = mcHelpers.closeReasonLabel(session);
				const msg = session.status === "Closed"
					? (reason || __("This conversation is closed."))
					: __("This conversation is read-only.");
				els.composeState.text(msg).removeClass("hidden");
			} else {
				els.composeState.text("").addClass("hidden");
			}
		}

		function renderSummary(summary) {
			const activeCount = Number((summary && summary.mine_active) || 0);
			page.set_indicator(
				`${__("Active human attention")}: ${activeCount}`,
				activeCount > 0 ? "green" : "gray"
			);
			if (page.indicator && page.indicator.attr) {
				page.indicator.attr("data-testid", "mc-kpi-active");
			} else {
				$(wrapper).find(".indicator-pill").attr("data-testid", "mc-kpi-active");
			}
		}

		function renderConversationList() {
			const rows = state.sessions || [];
			if (!rows.length) {
				const msg = (state.filters.search || state.filters.channel || state.filters.account)
					? __("No assigned conversations match your filters.")
					: __("You have no active assigned conversations.");
				els.list.html(`<div class="mc-empty-list" data-testid="mc-empty-list">${mcHelpers.escapeHtml(msg)}</div>`);
				return;
			}
			els.list.html(rows.map(r => mcHelpers.renderSessionCardHtml(r, state.selectedName)).join(""));
		}

		function renderStatusIconsHtml(transport, sendState) {
			const specs = mcHelpers.getTransportStatusSpecs(transport, sendState);
			if (!specs.length) return "";
			return `<span class="mc-status-icons">${specs.map(s =>
				`<span class="mc-status-icon" data-icon="${mcHelpers.escapeHtml(s.icon)}" data-tone="${mcHelpers.escapeHtml(s.tone || "muted")}" role="img" aria-label="${mcHelpers.escapeHtml(s.label)}" title="${mcHelpers.escapeHtml(s.label)}">${mcHelpers.iconHtml(s.icon, "xs", s.spinning ? "mc-icon-spin" : "")}</span>`
			).join("")}</span>`;
		}

		function renderTimeline(scrollToBottom) {
			const data = state.activeData;
			if (!data || !data.session) return;
			const sessionName = data.session.name;
			const el = els.timeline[0];
			const nearBottom = el ? (el.scrollHeight - el.scrollTop - el.clientHeight < 80) : true;

			const pending = state.pendingBySession.get(sessionName) || [];
			const messages = mcHelpers.mergeTimelineMessages(data.messages || [], pending);
			const parts = [];

			if (data.has_more) {
				parts.push(
					`<button type="button" class="btn btn-default btn-xs mc-load-more" data-testid="mc-load-more">${mcHelpers.escapeHtml(__("Load earlier messages"))}</button>`
				);
			}

			for (const m of messages) {
				const msgStamp = m.timeline_at || m.sent_at || m.event_at;
				if (m.kind === "Event") {
					parts.push(
						`<div class="mc-event-row" data-kind="Event" data-message="${mcHelpers.escapeHtml(m.name || "")}">` +
						`<span>${mcHelpers.escapeHtml(m.content || "")}</span>` +
						`<span class="mc-event-time" title="${mcHelpers.escapeHtml(mcHelpers.formatDateTime(msgStamp))}">${mcHelpers.escapeHtml(mcHelpers.formatTime(msgStamp))}</span>` +
						`</div>`
					);
					continue;
				}
				const isIncoming = m.kind === "Incoming";
				const originLabel = isIncoming ? "" : m.origin_label || "";
				const iconsHtml = isIncoming ? "" : renderStatusIconsHtml(m.transport, m.send_state);
				const errorHtml = m.send_state === "failed"
					? `<div class="mc-msg-error-bar">` +
						`<span>${mcHelpers.escapeHtml(m.error_message || __("Send error"))}</span>` +
						`<button type="button" class="btn btn-default btn-xs mc-retry-btn" data-client-id="${mcHelpers.escapeHtml(m.client_id || "")}">${mcHelpers.escapeHtml(__("Retry"))}</button>` +
						`</div>`
					: "";
				parts.push(
					`<div class="mc-msg ${isIncoming ? "is-incoming" : "is-outgoing"}" ` +
					`data-kind="${mcHelpers.escapeHtml(m.kind)}" ` +
					`data-message="${mcHelpers.escapeHtml(m.name || "")}" ` +
					`data-send-state="${mcHelpers.escapeHtml(m.send_state || "")}" ` +
					`aria-label="${mcHelpers.escapeHtml(mcHelpers.buildMessageAriaLabel(m))}">` +
					(originLabel ? `<div class="mc-msg-origin">${mcHelpers.escapeHtml(originLabel)}</div>` : "") +
					`<div class="mc-msg-content">${mcHelpers.escapeHtml(m.content || "")}</div>` +
					`<div class="mc-msg-meta">` +
					`<span class="mc-msg-time" title="${mcHelpers.escapeHtml(mcHelpers.formatDateTime(msgStamp))}">${mcHelpers.escapeHtml(mcHelpers.formatTime(msgStamp))}</span>` +
					iconsHtml +
					`</div>` +
					errorHtml +
					`</div>`
				);
			}

			els.timeline.html(parts.join(""));
			if (el && (scrollToBottom || nearBottom)) {
				el.scrollTop = el.scrollHeight;
			}
		}

		function renderChatHeader() {
			const data = state.activeData;
			const session = data && data.session;
			if (!session) return;
			const displayName = session.contact_name || session.contact_external_sender || session.contact || session.name;
			const channelLabel = session.channel_name || session.channel || "";
			const accountLabel = session.account_name || session.account_public_id || session.account || "";
			const subHtmlParts = [];
			if (channelLabel) {
				subHtmlParts.push(`<span class="tag-channel">${mcHelpers.escapeHtml(channelLabel)}</span>`);
			}
			const textMeta = [accountLabel, session.contact_external_sender].filter(Boolean).join(" · ");
			if (textMeta) {
				subHtmlParts.push(`<span>${mcHelpers.escapeHtml(textMeta)}</span>`);
			}
			const isActive = session.status === "Human Active";
			const closeBtn = session.can_close
				? `<button type="button" class="btn btn-danger btn-sm mc-close-btn" data-testid="mc-btn-close">${mcHelpers.escapeHtml(__("Close attention"))}</button>`
				: "";

			els.chatHead.html([
				`<div class="mc-head-left">`,
				`<button type="button" class="btn btn-default btn-sm mc-back-btn" data-testid="mc-back-btn" aria-label="${mcHelpers.escapeHtml(__("Back to list"))}">${mcHelpers.iconHtml("arrow-left", "xs")}</button>`,
				`<div class="mc-head-identity">`,
				`<div class="mc-head-name">${mcHelpers.escapeHtml(displayName)}</div>`,
				`<div class="mc-head-sub">${subHtmlParts.join(" · ")}</div>`,
				`</div>`,
				`</div>`,
				`<div class="mc-head-actions">`,
				`<span class="mc-status-badge ${isActive ? "is-active" : ""}" data-testid="mc-session-status">${mcHelpers.escapeHtml(mcHelpers.statusLabel(session.status))}</span>`,
				closeBtn,
				`</div>`,
			].join(""));

			if (session.status === "Closed") {
				const reason = mcHelpers.closeReasonLabel(session);
				els.readonlyBanner.text(reason || __("This conversation is closed.")).removeClass("hidden");
			} else if (session.status === "Human Paused") {
				els.readonlyBanner.text(__("Historical paused conversation. You may inspect or close it.")).removeClass("hidden");
			} else {
				els.readonlyBanner.text("").addClass("hidden");
			}
		}

		function renderInfoPanel() {
			const data = state.activeData;
			if (!data || !data.session) {
				els.infoEmpty.removeClass("hidden");
				els.infoBody.addClass("hidden").empty();
				return;
			}
			els.infoEmpty.addClass("hidden");
			const session = data.session;
			const contact = data.contact || {};
			const displayName = contact.display_name || session.contact_name || contact.external_sender || session.contact || session.name;
			const handle = contact.external_sender || session.contact_external_sender || "";
			const profileRows = (contact.profile_fields || []).map(pf =>
				`<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(pf.label || pf.key)}</span><span class="mc-info-val">${mcHelpers.escapeHtml(pf.value)}</span></div>`
			).join("");
			const regRows = mcHelpers.collectDeduplicatedRegistration(contact.profile_fields || [], contact.registration).map(r =>
				`<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(r.label)}</span><span class="mc-info-val">${mcHelpers.escapeHtml(r.value)}</span></div>`
			).join("");
			const reasonLabel = mcHelpers.closeReasonLabel(session);

			const reqParts = [];
			for (const req of data.human_requests || []) {
				const fieldsHtml = (req.fields || []).map(f =>
					`<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(f.label || f.key)}</span><span class="mc-info-val">${mcHelpers.escapeHtml(f.value)}</span></div>`
				).join("");
				if (!fieldsHtml) continue;
				reqParts.push(`<div class="mc-request-card">${fieldsHtml}</div>`);
			}

			els.infoBody.html([
				`<div class="mc-contact-hero">`,
				`<div class="mc-avatar">${mcHelpers.escapeHtml(mcHelpers.initialsOf(displayName))}</div>`,
				`<div class="mc-contact-name">${mcHelpers.escapeHtml(displayName)}</div>`,
				handle ? `<div class="mc-contact-handle">${mcHelpers.escapeHtml(handle)}</div>` : "",
				`</div>`,
				profileRows ? `<div class="mc-info-section"><div class="mc-info-heading">${mcHelpers.escapeHtml(__("Contact Information"))}</div>${profileRows}</div>` : "",
				regRows ? `<div class="mc-info-section"><div class="mc-info-heading">${mcHelpers.escapeHtml(__("Registration Details"))}</div>${regRows}</div>` : "",
				`<div class="mc-info-section">`,
				`<div class="mc-info-heading">${mcHelpers.escapeHtml(__("Operational Status"))}</div>`,
				`<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Status"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(mcHelpers.statusLabel(session.status))}</span></div>`,
				`<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Channel"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(session.channel_name || session.channel || "")}</span></div>`,
				`<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Account"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(session.account_name || session.account_public_id || session.account || "")}</span></div>`,
				session.assigned_agent_name ? `<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Assigned Agent"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(session.assigned_agent_name)}</span></div>` : "",
				session.assigned_at ? `<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Assigned At"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(mcHelpers.formatDateTime(session.assigned_at))}</span></div>` : "",
				session.last_activity ? `<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Last Activity"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(mcHelpers.formatDateTime(session.last_activity))}</span></div>` : "",
				reasonLabel ? `<div class="mc-info-row"><span class="mc-info-key">${mcHelpers.escapeHtml(__("Close Reason"))}</span><span class="mc-info-val">${mcHelpers.escapeHtml(reasonLabel)}</span></div>` : "",
				`</div>`,
				reqParts.length ? `<div class="mc-info-section"><div class="mc-info-heading">${mcHelpers.escapeHtml(__("Human request details"))}</div>${reqParts.join("")}</div>` : "",
			].filter(Boolean).join("")).removeClass("hidden");
		}

		function renderWorkspace(scrollToBottom) {
			const hasSession = Boolean(state.activeData && state.activeData.session);
			els.chatEmpty.toggleClass("hidden", hasSession);
			els.chatActive.toggleClass("hidden", !hasSession);
			if (hasSession) {
				renderChatHeader();
				renderTimeline(scrollToBottom);
			}
			renderInfoPanel();
			updateComposerControls();
			fitHeight();
		}

		function clearActiveSession() {
			state.selectedName = null;
			state.activeData = null;
			els.root.removeClass("mobile-detail-open");
			renderConversationList();
			renderWorkspace(false);
		}

		async function markSessionRead(session) {
			if (!session || !session.is_mine || !session.message_sequence) return;
			if ((session.read_sequence || 0) >= session.message_sequence) return;
			try {
				const seq = await callApi(`${API_BASE}.mark_read`, {
					session_name: session.name,
					through: session.message_sequence,
				}, {type: "POST"});
				if (state.activeData && state.activeData.session && state.activeData.session.name === session.name) {
					state.activeData.session.read_sequence = seq;
					state.activeData.session.unread = 0;
				}
				const target = state.sessions.find(r => r.name === session.name);
				if (target) target.unread = 0;
				renderConversationList();
			} catch (e) {
				// Non-fatal read cursor update
			}
		}

		async function loadFilters() {
			try {
				const res = await callApi(`${API_BASE}.channels_and_accounts`, {});
				state.channels = (res && res.channels) || [];
				state.accounts = (res && res.accounts) || [];
			} catch (e) {
				// Ignore transient filter load error
			}
		}

		async function loadSummary() {
			try {
				const res = await callApi(`${API_BASE}.inbox_summary`, {
					channel: state.filters.channel || undefined,
					account: state.filters.account || undefined,
				});
				renderSummary(res);
			} catch (e) {
				// Ignore transient summary error
			}
		}

		async function openSession(sessionName, opts) {
			if (!sessionName) return;
			if (state.selectedName && state.selectedName !== sessionName) {
				state.drafts.set(state.selectedName, els.composeInput.val());
			}
			const switching = state.selectedName !== sessionName;
			if (switching) state.loadedLimit = 60;
			state.selectedName = sessionName;
			els.root.addClass("mobile-detail-open");
			renderConversationList();
			try {
				const res = await callApi(`${API_BASE}.history`, {
					session_name: sessionName,
					limit: state.loadedLimit,
					mine_only: 1,
				});
				if (state.selectedName !== sessionName) return;
				state.activeData = res;
				if (switching) {
					els.composeInput.val(state.drafts.get(sessionName) || "");
					resizeComposer();
				}
				renderWorkspace(Boolean(opts && opts.scrollToBottom) || switching);
				await markSessionRead(res && res.session);
			} catch (err) {
				frappe.show_alert({message: __("Unable to load the selected conversation."), indicator: "red"});
				clearActiveSession();
			}
		}

		async function loadConversations(opts) {
			try {
				const res = await callApi(`${API_BASE}.conversations`, {
					mine: 1,
					channel: state.filters.channel || undefined,
					account: state.filters.account || undefined,
					search: state.filters.search || undefined,
					search_condition: state.filters.searchCondition || "like",
					limit: 50,
				});
				state.sessions = Array.isArray(res) ? res : [];
				renderConversationList();
				if (state.selectedName) {
					const stillPresent = state.sessions.some(r => r.name === state.selectedName);
					if (stillPresent && !(opts && opts.skipReloadActive)) {
						await openSession(state.selectedName, {scrollToBottom: false});
					} else if (!stillPresent && state.activeData && state.activeData.session && state.activeData.session.name === state.selectedName) {
						try {
							const refreshed = await callApi(`${API_BASE}.history`, {
								session_name: state.selectedName,
								limit: state.loadedLimit,
								mine_only: 1,
							});
							state.activeData = refreshed;
							renderWorkspace(false);
						} catch (e) {
							clearActiveSession();
						}
					}
				}
			} catch (err) {
				els.list.html(`<div class="mc-empty-list">${mcHelpers.escapeHtml(__("Unable to load assigned conversations."))}</div>`);
			}
		}

		async function refreshAll(opts) {
			await loadSummary();
			await loadConversations(opts);
		}

		function scheduleRefresh() {
			if (state.refreshTimer) clearTimeout(state.refreshTimer);
			state.refreshTimer = setTimeout(() => {
				if (state.pageActive) refreshAll();
			}, 250);
		}

		async function sendMessage(retryPayload) {
			const session = state.activeData && state.activeData.session;
			if (!session || !session.can_reply || state.sendingBusy) return;
			const content = retryPayload ? retryPayload.content : els.composeInput.val().trim();
			if (!content) return;
			const clientId = retryPayload
				? retryPayload.client_id
				: `cid-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;

			const pending = state.pendingBySession.get(session.name) || [];
			let localItem = pending.find(p => p.client_id === clientId);
			if (!localItem) {
				localItem = {
					client_id: clientId,
					kind: "Human",
					origin_label: __("Agent"),
					content,
					timeline_at: new Date().toISOString(),
					send_state: "sending",
					agent: frappe.session.user,
					agent_name: frappe.session.user_fullname || frappe.session.user,
				};
				pending.push(localItem);
				state.pendingBySession.set(session.name, pending);
			} else {
				localItem.send_state = "sending";
				localItem.error_message = "";
			}

			if (!retryPayload) {
				els.composeInput.val("");
				state.drafts.set(session.name, "");
				resizeComposer();
			}

			state.sendingBusy = true;
			updateComposerControls();
			renderTimeline(true);

			try {
				await callApi(`${API_BASE}.send`, {
					session_name: session.name,
					content,
					client_id: clientId,
				}, {type: "POST"});
				const currentPending = (state.pendingBySession.get(session.name) || []).filter(p => p.client_id !== clientId);
				state.pendingBySession.set(session.name, currentPending);
				await refreshAll();
				await openSession(session.name, {scrollToBottom: true});
			} catch (err) {
				localItem.send_state = "failed";
				localItem.error_message = __("Send error");
				renderTimeline(true);
				frappe.show_alert({message: __("Message could not be sent."), indicator: "red"});
			} finally {
				state.sendingBusy = false;
				updateComposerControls();
				els.composeInput.trigger("focus");
			}
		}

		async function executeCloseAttention() {
			const session = state.activeData && state.activeData.session;
			if (!session || state.actionBusy) return;
			state.actionBusy = true;
			try {
				await callApi(HUMAN_API, {
					session_name: session.name,
					action: "Close",
				}, {type: "POST", freeze: true});
				frappe.show_alert({message: __("Attention closed."), indicator: "green"});
				clearActiveSession();
				await refreshAll({skipReloadActive: true});
			} catch (err) {
				frappe.show_alert({message: __("Unable to close this conversation."), indicator: "red"});
			} finally {
				state.actionBusy = false;
			}
		}

		function bindRealtime() {
			if (state.realtimeBound || !frappe.realtime || typeof frappe.realtime.on !== "function") return;
			state.realtimeHandler = payload => {
				if (!state.pageActive) return;
				if (!payload) {
					scheduleRefresh();
					return;
				}
				if (state.filters.channel && payload.channel && payload.channel !== state.filters.channel) return;
				if (state.filters.account && payload.account && payload.account !== state.filters.account) return;
				scheduleRefresh();
			};
			frappe.realtime.on(REALTIME_EVENT, state.realtimeHandler);
			state.realtimeBound = true;
		}

		function startPolling() {
			if (state.pollTimer) clearInterval(state.pollTimer);
			state.pollTimer = setInterval(() => {
				if (!state.pageActive || document.hidden) return;
				const rtConnected = Boolean(frappe.realtime && frappe.realtime.socket && frappe.realtime.socket.connected);
				if (!rtConnected) {
					refreshAll();
				}
			}, 20000);
		}

		// Native Frappe header actions & controls
		const refreshBtn = page.set_secondary_action(__("Refresh"), () => refreshAll(), "refresh");
		if (refreshBtn && refreshBtn.attr) {
			refreshBtn.attr("data-testid", "mc-refresh-btn");
		}
		initFilterControls();
		renderSummary({mine_active: 0});
		$(window).on("resize.my_conversations", fitHeight);

		els.search.on("input", () => {
			state.filters.search = els.search.val().trim();
			if (state.searchTimer) clearTimeout(state.searchTimer);
			state.searchTimer = setTimeout(() => refreshAll(), 220);
		});
		root.find(".match-type-dropdown-menu .dropdown-item").on("click", e => {
			const cond = $(e.currentTarget).attr("data-match-type") || "like";
			state.filters.searchCondition = cond;
			root.find(".mc-search-match-icon").html(matchIcon(cond));
			root.find(".match-type-dropdown-btn").attr("title", cond === "=" ? __("Equals") : __("Like"));
			if (state.filters.search) refreshAll();
		});
		els.filterClearBtn.on("click", () => {
			state.filters.search = "";
			state.filters.searchCondition = "like";
			els.search.val("");
			root.find(".mc-search-match-icon").html(matchIcon("like"));
			setChannelFilter("", "");
		});
		els.list.on("click", ".mc-session-item", e => {
			const name = $(e.currentTarget).attr("data-session");
			if (name) openSession(name, {scrollToBottom: true});
		});
		els.chatHead.on("click", ".mc-back-btn", () => {
			els.root.removeClass("mobile-detail-open");
		});
		els.chatHead.on("click", ".mc-close-btn", () => {
			const session = state.activeData && state.activeData.session;
			if (!session || state.actionBusy) return;
			frappe.confirm(
				__("Close this human attention? The subscriber will be able to start a new bot flow on their next message."),
				() => executeCloseAttention()
			);
		});
		els.timeline.on("click", ".mc-load-more", () => {
			if (!state.selectedName) return;
			state.loadedLimit += 60;
			openSession(state.selectedName, {scrollToBottom: false});
		});
		els.timeline.on("click", ".mc-retry-btn", e => {
			const clientId = $(e.currentTarget).attr("data-client-id");
			const session = state.activeData && state.activeData.session;
			if (!session || !clientId) return;
			const pending = state.pendingBySession.get(session.name) || [];
			const item = pending.find(p => p.client_id === clientId);
			if (item) sendMessage(item);
		});
		els.composeInput.on("input", () => {
			resizeComposer();
			updateComposerControls();
		});
		els.composeInput.on("keydown", e => {
			if (e.key === "Enter" && !e.shiftKey) {
				e.preventDefault();
				sendMessage();
			} else if (e.key === "Escape") {
				els.composeInput.trigger("blur");
			}
		});
		els.sendBtn.on("click", () => sendMessage());

		wrapper._mcState = state;
		wrapper._mcBindLifecycle = (active) => {
			state.pageActive = active;
			if (active) {
				fitHeight();
				bindRealtime();
				startPolling();
				loadFilters().then(() => refreshAll());
			} else {
				if (state.pollTimer) {
					clearInterval(state.pollTimer);
					state.pollTimer = null;
				}
				if (state.refreshTimer) {
					clearTimeout(state.refreshTimer);
					state.refreshTimer = null;
				}
			}
		};
	};

	frappe.pages["my-conversations"].on_page_show = function (wrapper) {
		if (typeof wrapper._mcBindLifecycle === "function") {
			wrapper._mcBindLifecycle(true);
		}
	};

	frappe.pages["my-conversations"].on_page_hide = function (wrapper) {
		if (typeof wrapper._mcBindLifecycle === "function") {
			wrapper._mcBindLifecycle(false);
		}
	};
}
