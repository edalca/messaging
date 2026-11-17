/* global frappe, __ */
"use strict";

function createConversationHistoryHelpers() {
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

	function t(key, args) {
		return typeof __ === "function" ? __(key, args) : key;
	}

	function getFrappeTimeZone() {
		if (typeof frappe === "undefined") return "";
		return (
			(frappe.boot && frappe.boot.time_zone && (frappe.boot.time_zone.user || frappe.boot.time_zone.system))
			|| (frappe.sys_defaults && frappe.sys_defaults.time_zone)
			|| ""
		);
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

	function formatTime(iso, tz) {
		const dt = parseUtcDate(iso);
		if (!dt) return "";
		const targetTz = tz || getFrappeTimeZone();
		try {
			const opts = {hour: "2-digit", minute: "2-digit"};
			if (targetTz) opts.timeZone = targetTz;
			return dt.toLocaleTimeString([], opts);
		} catch (e) {
			try {
				return dt.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
			} catch (err) {
				return safeText(iso).slice(11, 16);
			}
		}
	}

	function formatDateTime(iso, tz) {
		const dt = parseUtcDate(iso);
		if (!dt) return safeText(iso);
		const targetTz = tz || getFrappeTimeZone();
		const baseOpts = {
			year: "numeric",
			month: "short",
			day: "2-digit",
			hour: "2-digit",
			minute: "2-digit",
		};
		try {
			const opts = targetTz ? Object.assign({}, baseOpts, {timeZone: targetTz}) : baseOpts;
			return dt.toLocaleString([], opts);
		} catch (e) {
			try {
				return dt.toLocaleString([], baseOpts);
			} catch (err) {
				return safeText(iso);
			}
		}
	}

	function formatDuration(seconds) {
		if (seconds == null || seconds === "") return "";
		const total = Number(seconds);
		if (!Number.isFinite(total) || total < 0) return "";
		const whole = Math.floor(total);
		if (whole < 60) return `${whole}s`;
		const mins = Math.floor(whole / 60);
		const remSecs = whole % 60;
		if (mins < 60) {
			return remSecs ? `${mins}m ${remSecs}s` : `${mins}m`;
		}
		const hours = Math.floor(mins / 60);
		const remMins = mins % 60;
		if (hours < 24) {
			return remMins ? `${hours}h ${remMins}m` : `${hours}h`;
		}
		const days = Math.floor(hours / 24);
		const remHours = hours % 24;
		return remHours ? `${days}d ${remHours}h` : `${days}d`;
	}

	function statusLabel(status) {
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

	function attentionTypeLabel(session) {
		if (!session) return "";
		if (session.attention_type_label) return session.attention_type_label;
		if (session.attention_type === "Human") return t("Human attention");
		if (session.attention_type === "Automatic") return t("Automatic attention");
		return (session.human_requested_at || session.assigned_agent)
			? t("Human attention")
			: t("Automatic attention");
	}

	function getTransportStatusSpecs(transport) {
		if (!transport || !transport.status) {
			return [];
		}
		const status = transport.status;
		const delivery = transport.delivery_status || "";
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
			return [{
				icon: "loader-circle",
				tone: "muted",
				label: `${t(status)}${delivery ? " · " + t(delivery) : ""}`,
				spinning: true,
			}];
		}
		if (["Reserved", "Prepared", "Held", "Pending"].includes(status)) {
			return [{icon: "clock", tone: "muted", label: `${t(status)}${diagSuffix}`}];
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
		return [{icon: "clock", tone: "muted", label: `${t(status)}${delivery ? " · " + t(delivery) : ""}${diagSuffix}`}];
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

	function renderSessionCardHtml(row, selectedName, tz) {
		const isSelected = row.name === selectedName;
		const displayName = row.contact_name || row.external_sender || row.contact || row.name;
		const channelLabel = row.channel_name || row.channel || "";
		const accountLabel = row.account_name || row.account_public_id || row.account || "";
		const stamp = row.activity_at || row.closed_at || row.last_activity || row.expires_at;
		const timeText = stamp ? formatDateTime(stamp, tz) : "";
		const statusClass = row.status === "Expired" ? "ch-badge-expired" : "ch-badge-closed";
		const reasonText = row.close_reason ? closeReasonLabel(row) : "";
		const isHuman = (row.attention_type === "Human") || Boolean(row.human_requested_at || row.assigned_agent);
		const attentionClass = isHuman ? "ch-badge-human" : "ch-badge-auto";
		const attentionText = attentionTypeLabel(row);
		const previewHtml = row.last_message
			? escapeHtml(row.last_message)
			: `<span class="font-italic text-muted">${escapeHtml(t("No messages yet"))}</span>`;

		return [
			`<button type="button" class="ch-session-item ${isSelected ? "is-selected" : ""}" data-session="${escapeHtml(row.name)}" data-testid="ch-session-item">`,
			`<div class="ch-item-avatar">${escapeHtml(initialsOf(displayName))}</div>`,
			`<div class="ch-item-body">`,
			`<div class="ch-item-top">`,
			`<span class="ch-item-name" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</span>`,
			`<span class="ch-item-time">${escapeHtml(timeText)}</span>`,
			`</div>`,
			`<div class="ch-item-provenance">`,
			channelLabel ? `<span class="tag-channel">${escapeHtml(channelLabel)}</span>` : "",
			accountLabel ? `<span class="tag-account" title="${escapeHtml(accountLabel)}">${escapeHtml(accountLabel)}</span>` : "",
			`<span class="ch-item-session-id">${escapeHtml(row.name)}</span>`,
			`</div>`,
			`<div class="ch-item-preview" title="${escapeHtml(row.last_message || "")}">${previewHtml}</div>`,
			`<div class="ch-item-footer">`,
			`<span class="ch-badge ${statusClass}">${escapeHtml(statusLabel(row.status))}</span>`,
			reasonText ? `<span class="ch-badge ch-badge-reason">${escapeHtml(reasonText)}</span>` : "",
			`<span class="ch-badge ${attentionClass}">${escapeHtml(attentionText)}</span>`,
			`</div>`,
			`</div>`,
			`</button>`,
		].join("");
	}

	return {
		safeText,
		escapeHtml,
		iconHtml,
		initialsOf,
		getFrappeTimeZone,
		parseUtcDate,
		formatTime,
		formatDateTime,
		formatDuration,
		statusLabel,
		closeReasonLabel,
		attentionTypeLabel,
		getTransportStatusSpecs,
		collectDeduplicatedRegistration,
		buildMessageAriaLabel,
		renderSessionCardHtml,
	};
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = createConversationHistoryHelpers();
}

if (typeof frappe !== "undefined" && frappe.pages) {
	const chHelpers = createConversationHistoryHelpers();
	const API_BASE = "messaging.conversation.doctype.chat_session.inbox";
	const PAGE_LIMIT = 30;
	const MESSAGE_LIMIT = 60;

	frappe.pages["conversation-history"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Conversation History"),
			single_column: true,
		});
		$(wrapper).addClass("ch-page");
		page.set_title_sub(__("Read-only history of completed and expired conversations"));

		const root = $(frappe.render_template("conversation_history")).appendTo(page.body);
		root.find(".ch-empty-icon").html(chHelpers.iconHtml("history", "md"));

		const els = {
			root,
			search: root.find(".ch-search-input"),
			filterToggleBtn: root.find(".btn-ch-filter"),
			filterClearBtn: root.find(".filter-x-button"),
			filterPanel: root.find(".ch-filter-panel"),
			filterBar: root.find(".ch-active-filter-bar"),
			mineBtn: root.find(".ch-mine-btn"),
			dateFrom: root.find("[data-filter='date_from']"),
			dateTo: root.find("[data-filter='date_to']"),
			channel: root.find("[data-filter='channel']"),
			account: root.find("[data-filter='account']"),
			attentionType: root.find("[data-filter='attention_type']"),
			status: root.find("[data-filter='status']"),
			closeReason: root.find("[data-filter='close_reason']"),
			agentField: root.find(".ch-agent-filter-field"),
			agent: root.find("[data-filter='agent']"),
			list: root.find(".ch-list-body"),
			chatEmpty: root.find(".ch-chat-empty"),
			chatEmptyTitle: root.find("[data-testid='ch-chat-empty-title']"),
			chatEmptyDesc: root.find("[data-testid='ch-chat-empty-desc']"),
			chatActive: root.find(".ch-chat-active"),
			chatHead: root.find(".ch-chat-head"),
			readonlyBanner: root.find(".ch-readonly-banner"),
			timeline: root.find(".ch-timeline"),
			infoEmpty: root.find(".ch-info-empty"),
			infoBody: root.find(".ch-info-body"),
		};

		const state = {
			sessions: [],
			loadedNames: new Set(),
			selectedName: null,
			activeData: null,
			messages: [],
			hasMoreMessages: false,
			channels: [],
			accounts: [],
			agents: [],
			isManager: false,
			filtersOpen: false,
			filters: {
				search: "",
				searchCondition: "like",
				date_from: "",
				date_to: "",
				channel: "",
				account: "",
				attention_type: "",
				status: "",
				close_reason: "",
				agent: "",
				mine: 0,
			},
			start: 0,
			limit: PAGE_LIMIT,
			hasMore: false,
			loadingList: false,
			loadingMoreMessages: false,
			listRequestSeq: 0,
			detailRequestSeq: 0,
			searchTimer: null,
			pageActive: false,
			pendingTargetSession: null,
		};

		function fitHeight() {
			if (state.pageActive && root[0] && root[0].getClientRects().length) {
				root.height(Math.max(460, window.innerHeight - root[0].getBoundingClientRect().top - 16));
			}
		}

		async function callApi(method, args) {
			const res = await frappe.call({
				method,
				args: args || {},
				type: "GET",
				silent: true,
			});
			return res && res.message;
		}

		function matchIcon(condition) {
			return chHelpers.iconHtml(condition === "=" ? "equal" : "equal-approximately");
		}

		function initFilterControls() {
			root.find(".btn-ch-filter .filter-icon").html(chHelpers.iconHtml("funnel"));
			root.find(".filter-x-button .filter-icon").html(chHelpers.iconHtml("x"));
			root.find(".ch-search-match-icon").html(matchIcon(state.filters.searchCondition));
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

		function buildBadgeTag(label, onRemove) {
			const theme = getTagTheme(label);
			let $tag;
			if (frappe.ui && typeof frappe.ui.badge === "function") {
				$tag = frappe.ui.badge({
					label,
					theme,
					size: "md",
					icon_right: "x",
					css_class: "form-tag active-filter-tag",
					title: label,
				});
				$tag.find(".es-badge__label").addClass("pill-label ellipsis");
			} else {
				$tag = $(
					`<span class="es-badge form-tag active-filter-tag" data-theme="${theme}" data-size="md" title="${chHelpers.escapeHtml(label)}">` +
					`<span class="es-badge__label pill-label ellipsis">${chHelpers.escapeHtml(label)}</span>` +
					`<span class="es-badge__affix" role="button" tabindex="0" aria-label="${chHelpers.escapeHtml(__("Remove filter"))}">${chHelpers.iconHtml("x", "sm")}</span>` +
					`</span>`
				);
			}
			$tag.find(".es-badge__affix")
				.attr({role: "button", tabindex: 0, "aria-label": __("Remove filter")})
				.on("click", e => {
					e.preventDefault();
					e.stopPropagation();
					onRemove();
				})
				.on("keydown", e => {
					if (e.key === "Enter" || e.key === " ") {
						e.preventDefault();
						onRemove();
					}
				});
			return $tag;
		}

		function populateFilterSelects() {
			const chVal = state.filters.channel;
			els.channel.empty().append(`<option value="">${chHelpers.escapeHtml(__("All Channels"))}</option>`);
			for (const ch of state.channels || []) {
				const optLabel = ch.channel_name || ch.name;
				els.channel.append(`<option value="${chHelpers.escapeHtml(ch.name)}">${chHelpers.escapeHtml(optLabel)}</option>`);
			}
			els.channel.val(chVal);

			const accVal = state.filters.account;
			els.account.empty().append(`<option value="">${chHelpers.escapeHtml(__("All Accounts"))}</option>`);
			for (const acc of state.accounts || []) {
				if (chVal && acc.channel !== chVal) continue;
				const accLabel = acc.account_name || acc.account_id || acc.name;
				els.account.append(`<option value="${chHelpers.escapeHtml(acc.name)}">${chHelpers.escapeHtml(accLabel)}</option>`);
			}
			if (accVal && els.account.find(`option[value="${CSS.escape(accVal)}"]`).length === 0) {
				state.filters.account = "";
			} else {
				els.account.val(accVal);
			}

			els.agentField.toggleClass("hidden", !state.isManager);
			if (state.isManager) {
				const agentVal = state.filters.agent;
				els.agent.empty().append(`<option value="">${chHelpers.escapeHtml(__("All Agents"))}</option>`);
				for (const ag of state.agents || []) {
					els.agent.append(`<option value="${chHelpers.escapeHtml(ag.user)}">${chHelpers.escapeHtml(ag.full_name || ag.user)}</option>`);
				}
				els.agent.val(agentVal);
				els.agent.prop("disabled", Boolean(state.filters.mine));
			}
		}

		function syncFilterInputsFromState() {
			els.dateFrom.val(state.filters.date_from);
			els.dateTo.val(state.filters.date_to);
			els.channel.val(state.filters.channel);
			els.account.val(state.filters.account);
			els.attentionType.val(state.filters.attention_type);
			els.status.val(state.filters.status);
			els.closeReason.val(state.filters.close_reason);
			els.agent.val(state.filters.agent);

			root.find(".ch-quick-status-btn").each(function () {
				const btnStatus = $(this).attr("data-status") || "";
				$(this).toggleClass("active", btnStatus === state.filters.status);
			});
			els.mineBtn.toggleClass("active", Boolean(state.filters.mine))
				.attr("aria-pressed", state.filters.mine ? "true" : "false");

			renderActiveFilterTags();
		}

		function renderActiveFilterTags() {
			const f = state.filters;
			const tags = [];
			if (f.date_from) {
				tags.push({
					label: `${__("Date from")}: ${f.date_from}`,
					clear: () => { state.filters.date_from = ""; },
				});
			}
			if (f.date_to) {
				tags.push({
					label: `${__("Date to")}: ${f.date_to}`,
					clear: () => { state.filters.date_to = ""; },
				});
			}
			if (f.channel) {
				const ch = (state.channels || []).find(c => c.name === f.channel);
				tags.push({
					label: `${__("Channel")}: ${(ch && (ch.channel_name || ch.name)) || f.channel}`,
					clear: () => { state.filters.channel = ""; state.filters.account = ""; },
				});
			}
			if (f.account) {
				const acc = (state.accounts || []).find(a => a.name === f.account);
				tags.push({
					label: `${__("Account")}: ${(acc && (acc.account_name || acc.account_id || acc.name)) || f.account}`,
					clear: () => { state.filters.account = ""; },
				});
			}
			if (f.attention_type) {
				tags.push({
					label: `${__("Attention type")}: ${chHelpers.attentionTypeLabel({attention_type: f.attention_type})}`,
					clear: () => { state.filters.attention_type = ""; },
				});
			}
			if (f.status) {
				tags.push({
					label: `${__("Status")}: ${__(f.status)}`,
					clear: () => { state.filters.status = ""; },
				});
			}
			if (f.close_reason) {
				tags.push({
					label: `${__("Close reason")}: ${__(f.close_reason)}`,
					clear: () => { state.filters.close_reason = ""; },
				});
			}
			if (f.mine) {
				tags.push({
					label: __("My previous attentions"),
					clear: () => { state.filters.mine = 0; },
				});
			} else if (f.agent) {
				const ag = (state.agents || []).find(a => a.user === f.agent);
				tags.push({
					label: `${__("Agent")}: ${(ag && ag.full_name) || f.agent}`,
					clear: () => { state.filters.agent = ""; },
				});
			}

			els.filterBar.empty();
			for (const item of tags) {
				els.filterBar.append(buildBadgeTag(item.label, () => {
					item.clear();
					populateFilterSelects();
					syncFilterInputsFromState();
					loadConversations({reset: true});
				}));
			}
			els.filterBar.toggleClass("hidden", tags.length === 0);

			const labelEl = els.filterToggleBtn.find(".button-label");
			if (tags.length > 0) {
				els.filterToggleBtn.removeClass("btn-default").addClass("btn-primary-light");
				labelEl.html(__("Filters {0}", [`<span class="filter-label">${tags.length}</span>`]));
				els.filterToggleBtn.attr("title", `${tags.length} ${__("Filters")}`);
			} else {
				els.filterToggleBtn.removeClass("btn-primary-light").addClass("btn-default");
				labelEl.text(__("Filters"));
				els.filterToggleBtn.attr("title", __("Filters"));
			}
			fitHeight();
		}

		function hasActiveFiltersOrSearch() {
			const f = state.filters;
			return Boolean(
				f.search || f.date_from || f.date_to || f.channel || f.account
				|| f.attention_type || f.status || f.close_reason || f.agent || f.mine
			);
		}

		function renderConversationList() {
			const rows = state.sessions || [];
			if (!rows.length) {
				const msg = hasActiveFiltersOrSearch()
					? __("No historical conversations match your filters.")
					: __("No historical conversations found.");
				els.list.html(`<div class="ch-empty-list" data-testid="ch-empty-list">${chHelpers.escapeHtml(msg)}</div>`);
				return;
			}
			const tz = chHelpers.getFrappeTimeZone();
			const htmlParts = rows.map(r => chHelpers.renderSessionCardHtml(r, state.selectedName, tz));
			if (state.hasMore) {
				htmlParts.push(
					`<div class="ch-list-more-wrap">` +
					`<button type="button" class="btn btn-default btn-xs ch-load-more-sessions" data-testid="ch-load-more-sessions">${chHelpers.escapeHtml(__("Load more conversations"))}</button>` +
					`</div>`
				);
			}
			els.list.html(htmlParts.join(""));
		}

		function updateSelectedCardClass() {
			els.list.find(".ch-session-item").each(function () {
				const name = $(this).attr("data-session");
				$(this).toggleClass("is-selected", name === state.selectedName);
			});
		}

		function renderStatusIconsHtml(transport) {
			const specs = chHelpers.getTransportStatusSpecs(transport);
			if (!specs.length) return "";
			return `<span class="ch-status-icons">${specs.map(s =>
				`<span class="ch-status-icon" data-icon="${chHelpers.escapeHtml(s.icon)}" data-tone="${chHelpers.escapeHtml(s.tone || "muted")}" role="img" aria-label="${chHelpers.escapeHtml(s.label)}" title="${chHelpers.escapeHtml(s.label)}">${chHelpers.iconHtml(s.icon, "xs", s.spinning ? "ch-icon-spin" : "")}</span>`
			).join("")}</span>`;
		}

		function renderTimeline(scrollMode) {
			const data = state.activeData;
			if (!data || !data.session) return;
			const el = els.timeline[0];
			const prevScrollHeight = el ? el.scrollHeight : 0;
			const prevScrollTop = el ? el.scrollTop : 0;
			const tz = chHelpers.getFrappeTimeZone();
			const messages = state.messages || [];
			const parts = [];

			if (state.hasMoreMessages) {
				parts.push(
					`<button type="button" class="btn btn-default btn-xs ch-load-more" data-testid="ch-load-more">${chHelpers.escapeHtml(__("Load earlier messages"))}</button>`
				);
			}

			if (!messages.length) {
				parts.push(`<div class="ch-empty-list">${chHelpers.escapeHtml(__("No retained messages are available."))}</div>`);
			} else {
				for (const m of messages) {
					const msgStamp = m.sent_at || m.event_at;
					if (m.kind === "Event") {
						parts.push(
							`<div class="ch-event-row" data-kind="Event" data-message="${chHelpers.escapeHtml(m.name || "")}">` +
							`<span>${chHelpers.escapeHtml(m.content || "")}</span>` +
							(msgStamp ? `<span class="ch-event-time" title="${chHelpers.escapeHtml(chHelpers.formatDateTime(msgStamp, tz))}">${chHelpers.escapeHtml(chHelpers.formatTime(msgStamp, tz))}</span>` : "") +
							`</div>`
						);
						continue;
					}
					const isIncoming = m.kind === "Incoming";
					const originLabel = isIncoming ? "" : (m.origin_label || "");
					const iconsHtml = isIncoming ? "" : renderStatusIconsHtml(m.transport);
					parts.push(
						`<div class="ch-msg ${isIncoming ? "is-incoming" : "is-outgoing"}" ` +
						`data-kind="${chHelpers.escapeHtml(m.kind)}" ` +
						`data-message="${chHelpers.escapeHtml(m.name || "")}" ` +
						`aria-label="${chHelpers.escapeHtml(chHelpers.buildMessageAriaLabel(m))}">` +
						(originLabel ? `<div class="ch-msg-origin">${chHelpers.escapeHtml(originLabel)}</div>` : "") +
						`<div class="ch-msg-content">${chHelpers.escapeHtml(m.content || "")}</div>` +
						`<div class="ch-msg-meta">` +
						`<span class="ch-msg-time" title="${chHelpers.escapeHtml(chHelpers.formatDateTime(msgStamp, tz))}">${chHelpers.escapeHtml(chHelpers.formatDateTime(msgStamp, tz))}</span>` +
						iconsHtml +
						`</div>` +
						`</div>`
					);
				}
			}

			els.timeline.html(parts.join(""));
			if (el) {
				if (scrollMode === "bottom") {
					el.scrollTop = el.scrollHeight;
				} else if (scrollMode === "prepend") {
					const addedHeight = el.scrollHeight - prevScrollHeight;
					el.scrollTop = Math.max(0, prevScrollTop + addedHeight);
				}
			}
		}

		function renderChatHeader() {
			const data = state.activeData;
			const session = data && data.session;
			if (!session) return;
			const contact = data.contact || {};
			const displayName = contact.display_name || session.contact_name || contact.external_sender || session.contact || session.name;
			const channelLabel = session.channel_name || session.channel || "";
			const accountLabel = session.account_name || session.account_public_id || session.account || "";
			const subHtmlParts = [];
			if (channelLabel) {
				subHtmlParts.push(`<span class="tag-channel">${chHelpers.escapeHtml(channelLabel)}</span>`);
			}
			const textMeta = [accountLabel, session.name].filter(Boolean).join(" · ");
			if (textMeta) {
				subHtmlParts.push(`<span>${chHelpers.escapeHtml(textMeta)}</span>`);
			}
			const isExpired = session.status === "Expired";
			const attClass = session.attention_type === "Human" ? "ch-badge-human" : "ch-badge-auto";

			els.chatHead.html([
				`<div class="ch-head-left">`,
				`<button type="button" class="btn btn-default btn-sm ch-back-btn" data-testid="ch-back-btn" aria-label="${chHelpers.escapeHtml(__("Back to list"))}">${chHelpers.iconHtml("arrow-left", "xs")}</button>`,
				`<div class="ch-head-identity">`,
				`<div class="ch-head-name">${chHelpers.escapeHtml(displayName)}</div>`,
				`<div class="ch-head-sub">${subHtmlParts.join(" · ")}</div>`,
				`</div>`,
				`</div>`,
				`<div class="ch-head-badges">`,
				`<span class="ch-badge ${attClass}" data-testid="ch-session-attention">${chHelpers.escapeHtml(chHelpers.attentionTypeLabel(session))}</span>`,
				`<span class="ch-status-pill ${isExpired ? "is-expired" : ""}" data-testid="ch-session-status">${chHelpers.escapeHtml(chHelpers.statusLabel(session.status))}</span>`,
				`</div>`,
			].join(""));

			const reason = chHelpers.closeReasonLabel(session);
			const bannerText = session.status === "Expired"
				? __("Expired session. Read-only archive.")
				: (reason ? `${reason} · ${__("Read-only archive")}` : __("Read-only historical conversation."));
			els.readonlyBanner.text(bannerText).removeClass("hidden");
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
			const prov = data.provenance || {};
			const tz = chHelpers.getFrappeTimeZone();

			const displayName = contact.display_name || session.contact_name || contact.external_sender || session.contact || session.name;
			const handle = contact.external_sender || session.external_sender || "";
			const channelLabel = (prov.channel && prov.channel.channel_name) || session.channel_name || session.channel || "";
			const providerLabel = (prov.channel && prov.channel.provider) || session.provider || "";
			const accountLabel = (prov.account && prov.account.account_name) || session.account_name || session.account || "";
			const accountId = (prov.account && prov.account.account_id) || session.account_id || session.account || "";
			const publicId = (prov.account && prov.account.public_id) || session.account_public_id || "";

			const totalDur = chHelpers.formatDuration(session.total_duration_seconds);
			const waitDur = session.attention_type === "Human" ? chHelpers.formatDuration(session.human_wait_seconds) : "";
			const humanDur = (session.attention_type === "Human" && session.assigned_agent)
				? chHelpers.formatDuration(session.human_attention_seconds)
				: "";
			const reasonLabel = chHelpers.closeReasonLabel(session);

			const profileRows = (contact.profile_fields || []).map(pf =>
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(pf.label || pf.key)}</span><span class="ch-info-val">${chHelpers.escapeHtml(pf.value)}</span></div>`
			).join("");
			const regRows = chHelpers.collectDeduplicatedRegistration(contact.profile_fields || [], contact.registration).map(r =>
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(r.label)}</span><span class="ch-info-val">${chHelpers.escapeHtml(r.value)}</span></div>`
			).join("");

			const reqParts = [];
			for (const req of data.human_requests || []) {
				const fieldsHtml = (req.fields || []).map(f =>
					`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(f.label || f.key)}</span><span class="ch-info-val">${chHelpers.escapeHtml(f.value)}</span></div>`
				).join("");
				if (fieldsHtml) {
					reqParts.push(`<div class="ch-request-card">${fieldsHtml}</div>`);
				}
			}

			els.infoBody.html([
				`<div class="ch-contact-hero">`,
				`<div class="ch-avatar">${chHelpers.escapeHtml(chHelpers.initialsOf(displayName))}</div>`,
				`<div class="ch-contact-name">${chHelpers.escapeHtml(displayName)}</div>`,
				handle ? `<div class="ch-contact-handle">${chHelpers.escapeHtml(handle)}</div>` : "",
				`</div>`,
				`<div class="ch-info-section">`,
				`<div class="ch-info-heading">${chHelpers.escapeHtml(__("Origin & Provenance"))}</div>`,
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Channel"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(channelLabel)}</span></div>`,
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Account"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(accountLabel)}</span></div>`,
				accountId ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Account ID"))}</span><span class="ch-info-val ch-info-mono">${chHelpers.escapeHtml(accountId)}</span></div>` : "",
				(publicId && publicId !== accountId) ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Public ID"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(publicId)}</span></div>` : "",
				providerLabel ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Provider"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(providerLabel)}</span></div>` : "",
				`</div>`,
				`<div class="ch-info-section">`,
				`<div class="ch-info-heading">${chHelpers.escapeHtml(__("Session Details"))}</div>`,
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Session"))}</span><span class="ch-info-val ch-info-mono">${chHelpers.escapeHtml(session.name)}</span></div>`,
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Status"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(chHelpers.statusLabel(session.status))}</span></div>`,
				`<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Attention type"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(chHelpers.attentionTypeLabel(session))}</span></div>`,
				session.assigned_agent_name ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Assigned Agent"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(session.assigned_agent_name)}</span></div>` : "",
				(session.status === "Closed" && session.closed_by_name) ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Closed By"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(session.closed_by_name)}</span></div>` : "",
				reasonLabel ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Close Reason"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(reasonLabel)}</span></div>` : "",
				`</div>`,
				`<div class="ch-info-section">`,
				`<div class="ch-info-heading">${chHelpers.escapeHtml(__("Finalization & Timings"))}</div>`,
				session.started_at ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Started At"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(chHelpers.formatDateTime(session.started_at, tz))}</span></div>` : "",
				session.ended_at ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Ended At"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(chHelpers.formatDateTime(session.ended_at, tz))}</span></div>` : "",
				session.last_activity ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Last Activity"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(chHelpers.formatDateTime(session.last_activity, tz))}</span></div>` : "",
				totalDur ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Total Duration"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(totalDur)}</span></div>` : "",
				waitDur ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Human Wait Time"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(waitDur)}</span></div>` : "",
				humanDur ? `<div class="ch-info-row"><span class="ch-info-key">${chHelpers.escapeHtml(__("Human Attention Duration"))}</span><span class="ch-info-val">${chHelpers.escapeHtml(humanDur)}</span></div>` : "",
				`</div>`,
				profileRows ? `<div class="ch-info-section"><div class="ch-info-heading">${chHelpers.escapeHtml(__("Contact Information"))}</div>${profileRows}</div>` : "",
				regRows ? `<div class="ch-info-section"><div class="ch-info-heading">${chHelpers.escapeHtml(__("Registration Details"))}</div>${regRows}</div>` : "",
				reqParts.length ? `<div class="ch-info-section"><div class="ch-info-heading">${chHelpers.escapeHtml(__("Human request details"))}</div>${reqParts.join("")}</div>` : "",
			].filter(Boolean).join("")).removeClass("hidden");
		}

		function setEmptyCenterMessage(title, desc) {
			els.chatEmptyTitle.text(title || __("Select a historical conversation"));
			els.chatEmptyDesc.text(desc || __("Choose a session from the left list to read its messages and events."));
		}

		function renderWorkspace(scrollMode) {
			const hasSession = Boolean(state.activeData && state.activeData.session);
			els.chatEmpty.toggleClass("hidden", hasSession);
			els.chatActive.toggleClass("hidden", !hasSession);
			if (hasSession) {
				setEmptyCenterMessage();
				renderChatHeader();
				renderTimeline(scrollMode);
			}
			renderInfoPanel();
			fitHeight();
		}

		function clearActiveSession(options) {
			const opts = options || {};
			state.selectedName = null;
			state.activeData = null;
			state.messages = [];
			state.hasMoreMessages = false;
			els.root.removeClass("mobile-detail-open");
			if (opts.emptyTitle || opts.emptyDesc) {
				setEmptyCenterMessage(opts.emptyTitle, opts.emptyDesc);
			} else {
				setEmptyCenterMessage();
			}
			updateSelectedCardClass();
			renderWorkspace(null);
		}

		async function loadFilters() {
			try {
				const res = await callApi(`${API_BASE}.channels_and_accounts`, {});
				state.channels = (res && res.channels) || [];
				state.accounts = (res && res.accounts) || [];
				state.agents = (res && res.agents) || [];
				state.isManager = Boolean(res && res.is_manager);
				populateFilterSelects();
				syncFilterInputsFromState();
			} catch (e) {
				// Non-fatal filter metadata load error
			}
		}

		async function loadConversations(options) {
			const opts = options || {};
			const reset = opts.reset !== false;
			if (!reset && (state.loadingList || !state.hasMore)) return;

			if (state.filters.date_from && state.filters.date_to && state.filters.date_from > state.filters.date_to) {
				frappe.show_alert({message: __("Start date cannot be after end date."), indicator: "orange"});
				return;
			}

			const requestId = ++state.listRequestSeq;
			state.loadingList = true;
			if (reset) {
				state.start = 0;
				state.hasMore = false;
			}

			try {
				const rows = await callApi(`${API_BASE}.historical_conversations`, {
					status: state.filters.status || undefined,
					close_reason: state.filters.close_reason || undefined,
					attention_type: state.filters.attention_type || undefined,
					channel: state.filters.channel || undefined,
					account: state.filters.account || undefined,
					agent: (!state.filters.mine && state.filters.agent) ? state.filters.agent : undefined,
					mine: state.filters.mine ? 1 : 0,
					date_from: state.filters.date_from || undefined,
					date_to: state.filters.date_to || undefined,
					search: state.filters.search || undefined,
					search_condition: state.filters.searchCondition || "like",
					start: state.start,
					limit: state.limit,
				});
				if (requestId !== state.listRequestSeq) return;

				const batch = Array.isArray(rows) ? rows : [];
				if (reset) {
					state.sessions = [];
					state.loadedNames = new Set();
				}
				for (const item of batch) {
					if (item && item.name && !state.loadedNames.has(item.name)) {
						state.loadedNames.add(item.name);
						state.sessions.push(item);
					}
				}
				state.hasMore = batch.length >= state.limit;
				state.start = state.sessions.length;
				renderConversationList();
			} catch (err) {
				if (requestId !== state.listRequestSeq) return;
				if (reset) {
					state.sessions = [];
					state.loadedNames = new Set();
					state.hasMore = false;
					els.list.html(`<div class="ch-empty-list" data-testid="ch-error-list">${chHelpers.escapeHtml(__("Unable to load historical conversations."))}</div>`);
				}
				frappe.show_alert({message: __("Unable to load historical conversations."), indicator: "red"});
			} finally {
				if (requestId === state.listRequestSeq) {
					state.loadingList = false;
					fitHeight();
				}
			}
		}

		async function openSession(sessionName, options) {
			if (!sessionName) return;
			const opts = options || {};
			const requestId = ++state.detailRequestSeq;
			state.selectedName = sessionName;
			els.root.addClass("mobile-detail-open");
			updateSelectedCardClass();

			try {
				const res = await callApi(`${API_BASE}.history`, {
					session_name: sessionName,
					limit: MESSAGE_LIMIT,
					historical: 1,
				});
				if (requestId !== state.detailRequestSeq || state.selectedName !== sessionName) return;
				state.activeData = res;
				state.messages = (res && res.messages) || [];
				state.hasMoreMessages = Boolean(res && res.has_more);
				renderWorkspace("bottom");
			} catch (err) {
				if (requestId !== state.detailRequestSeq) return;
				const msg = __("This conversation is not available or you do not have permission to view it.");
				clearActiveSession({
					emptyTitle: __("Conversation unavailable"),
					emptyDesc: msg,
				});
				frappe.show_alert({
					message: opts.fromRoute ? msg : __("Unable to load the selected conversation."),
					indicator: "orange",
				});
			}
		}

		async function loadEarlierMessages() {
			if (!state.selectedName || !state.messages.length || state.loadingMoreMessages) return;
			const sessionName = state.selectedName;
			const earliestSeq = state.messages[0].sequence;
			if (!earliestSeq) return;

			const requestId = state.detailRequestSeq;
			state.loadingMoreMessages = true;
			try {
				const res = await callApi(`${API_BASE}.history`, {
					session_name: sessionName,
					before: earliestSeq,
					limit: MESSAGE_LIMIT,
					historical: 1,
				});
				if (requestId !== state.detailRequestSeq || state.selectedName !== sessionName) return;
				const older = (res && res.messages) || [];
				const seen = new Set(state.messages.map(m => m.name || String(m.sequence)));
				const uniqueOlder = older.filter(m => !seen.has(m.name || String(m.sequence)));
				state.messages = [...uniqueOlder, ...state.messages];
				state.hasMoreMessages = Boolean(res && res.has_more);
				renderTimeline("prepend");
			} catch (err) {
				frappe.show_alert({message: __("Unable to load earlier messages."), indicator: "red"});
			} finally {
				state.loadingMoreMessages = false;
			}
		}

		function consumeRequestedSession() {
			let requested = null;
			if (frappe.route_options && frappe.route_options.session) {
				requested = String(frappe.route_options.session || "").trim() || null;
				delete frappe.route_options.session;
			}
			if (!requested) {
				const params = new URLSearchParams(window.location.search || "");
				const val = params.get("session");
				if (val) {
					requested = val.trim() || null;
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
		}

		function clearAllFilters() {
			state.filters.search = "";
			state.filters.searchCondition = "like";
			state.filters.date_from = "";
			state.filters.date_to = "";
			state.filters.channel = "";
			state.filters.account = "";
			state.filters.attention_type = "";
			state.filters.status = "";
			state.filters.close_reason = "";
			state.filters.agent = "";
			state.filters.mine = 0;
			els.search.val("");
			root.find(".ch-search-match-icon").html(matchIcon("like"));
			root.find(".match-type-dropdown-btn").attr("title", __("Like"));
			populateFilterSelects();
			syncFilterInputsFromState();
			loadConversations({reset: true});
		}

		// Secondary refresh action (no write actions)
		const refreshBtn = page.set_secondary_action(__("Refresh"), async () => {
			await loadConversations({reset: true});
			if (state.selectedName) {
				await openSession(state.selectedName);
			}
		}, "refresh");
		if (refreshBtn && refreshBtn.attr) {
			refreshBtn.attr("data-testid", "ch-refresh-btn");
		}

		initFilterControls();
		syncFilterInputsFromState();
		$(window).on("resize.conversation_history", fitHeight);

		// Search debounce
		els.search.on("input", () => {
			state.filters.search = els.search.val().trim();
			if (state.searchTimer) clearTimeout(state.searchTimer);
			state.searchTimer = setTimeout(() => loadConversations({reset: true}), 250);
		});

		root.find(".match-type-dropdown-menu .dropdown-item").on("click", e => {
			const cond = $(e.currentTarget).attr("data-match-type") === "=" ? "=" : "like";
			if (cond === state.filters.searchCondition) return;
			state.filters.searchCondition = cond;
			root.find(".ch-search-match-icon").html(matchIcon(cond));
			root.find(".match-type-dropdown-btn").attr("title", cond === "=" ? __("Equals") : __("Like"));
			if (state.filters.search) loadConversations({reset: true});
		});

		// Toggle collapsible filter drawer
		els.filterToggleBtn.on("click", () => {
			state.filtersOpen = !state.filtersOpen;
			els.filterPanel.toggleClass("hidden", !state.filtersOpen);
			els.filterToggleBtn.attr("aria-expanded", state.filtersOpen ? "true" : "false");
			fitHeight();
		});

		els.filterClearBtn.on("click", clearAllFilters);

		// Quick status pills
		root.on("click", ".ch-quick-status-btn", e => {
			const nextStatus = $(e.currentTarget).attr("data-status") || "";
			state.filters.status = nextStatus;
			syncFilterInputsFromState();
			loadConversations({reset: true});
		});

		// My previous attentions toggle
		els.mineBtn.on("click", () => {
			state.filters.mine = state.filters.mine ? 0 : 1;
			if (state.filters.mine) {
				state.filters.agent = "";
			}
			populateFilterSelects();
			syncFilterInputsFromState();
			loadConversations({reset: true});
		});

		// Structured filter inputs
		els.filterPanel.on("change", ".ch-filter-control", e => {
			const field = $(e.currentTarget).attr("data-filter");
			const val = $(e.currentTarget).val() || "";
			if (!field) return;
			state.filters[field] = val;
			if (field === "channel") {
				state.filters.account = "";
				populateFilterSelects();
			}
			if (field === "agent" && val) {
				state.filters.mine = 0;
			}
			syncFilterInputsFromState();
			loadConversations({reset: true});
		});

		// Session selection & list pagination
		els.list.on("click", ".ch-session-item", e => {
			const name = $(e.currentTarget).attr("data-session");
			if (name) openSession(name);
		});

		els.list.on("click", ".ch-load-more-sessions", () => {
			loadConversations({reset: false});
		});

		els.list.on("scroll", function () {
			if (this.scrollHeight - this.scrollTop - this.clientHeight < 80 && !state.loadingList && state.hasMore) {
				loadConversations({reset: false});
			}
		});

		// Mobile back button & timeline pagination
		els.chatHead.on("click", ".ch-back-btn", () => {
			els.root.removeClass("mobile-detail-open");
		});

		els.timeline.on("click", ".ch-load-more", loadEarlierMessages);

		const onRouteChange = () => {
			if (state.pageActive && frappe.get_route()[0] === "conversation-history") {
				const target = consumeRequestedSession();
				if (target) {
					openSession(target, {fromRoute: true});
				}
			}
		};
		if (frappe.router && typeof frappe.router.on === "function") {
			frappe.router.on("change", onRouteChange);
		}

		$(wrapper).on("remove", () => {
			state.pageActive = false;
			if (state.searchTimer) clearTimeout(state.searchTimer);
			$(window).off("resize.conversation_history", fitHeight);
			if (frappe.router && typeof frappe.router.off === "function") {
				frappe.router.off("change", onRouteChange);
			}
		});

		state.pendingTargetSession = consumeRequestedSession();
		wrapper._chState = state;
		wrapper._chBindLifecycle = async (active) => {
			state.pageActive = active;
			if (active) {
				fitHeight();
				const routeTarget = state.pendingTargetSession || consumeRequestedSession();
				state.pendingTargetSession = null;
				await loadFilters();
				await loadConversations({reset: true});
				if (routeTarget) {
					await openSession(routeTarget, {fromRoute: true});
				}
			}
		};
	};

	frappe.pages["conversation-history"].on_page_show = function (wrapper) {
		if (typeof wrapper._chBindLifecycle === "function") {
			wrapper._chBindLifecycle(true);
		}
	};

	frappe.pages["conversation-history"].on_page_hide = function (wrapper) {
		if (typeof wrapper._chBindLifecycle === "function") {
			wrapper._chBindLifecycle(false);
		}
	};
}
