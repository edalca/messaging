/* global frappe, __ */
"use strict";

function createConversationSupervisionHelpers() {
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
		if (typeof __ === "function") return __(key, args);
		if (!Array.isArray(args)) return key;
		return String(key).replace(/\{(\d+)\}/g, (match, idx) =>
			args[Number(idx)] !== undefined ? String(args[Number(idx)]) : match
		);
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

	function formatDuration(seconds, fallback) {
		if (seconds == null || seconds === "") return fallback !== undefined ? fallback : "—";
		const total = Number(seconds);
		if (!Number.isFinite(total) || total < 0) return fallback !== undefined ? fallback : "—";
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

	function statusLabel(status) {
		if (status === "Human Active") return t("In Attention");
		if (status === "Human Queued") return t("Waiting");
		return t(status || "");
	}

	function agentStatusMeta(statusCode) {
		if (statusCode === "available") return {label: t("Available"), pillClass: "is-available", barClass: ""};
		if (statusCode === "full") return {label: t("Full"), pillClass: "is-full", barClass: "is-full"};
		if (statusCode === "disabled") return {label: t("Disabled"), pillClass: "is-disabled", barClass: "is-inactive"};
		return {label: t("Unavailable"), pillClass: "is-unavailable", barClass: "is-inactive"};
	}

	function agentUserStatusMeta(userGroup) {
		const code = (userGroup && userGroup.status_code) || "not_enabled";
		if (code === "available") return {label: t("Available"), pillClass: "is-available"};
		if (code === "full") return {label: t("No capacity"), pillClass: "is-full"};
		if (code === "unavailable") return {label: t("Unavailable"), pillClass: "is-unavailable"};
		if (code === "no_operational_accounts") return {label: t("No operational accounts"), pillClass: "is-retired"};
		return {label: t("Not enabled"), pillClass: "is-disabled"};
	}

	function grantStatusMeta(grant) {
		const reason = (grant && (grant.reason_code || grant.status_code)) || "disabled";
		if (reason === "available") {
			return {label: t("Available"), pillClass: "is-available", barClass: ""};
		}
		if (reason === "full") {
			return {label: t("Full"), pillClass: "is-full", barClass: "is-full"};
		}
		if (reason === "unavailable") {
			return {label: t("Unavailable"), pillClass: "is-unavailable", barClass: "is-inactive"};
		}
		if (reason === "grant_disabled") {
			return {label: t("Agent grant disabled"), pillClass: "is-disabled", barClass: "is-inactive"};
		}
		if (reason === "user_disabled") {
			return {label: t("User disabled"), pillClass: "is-disabled", barClass: "is-inactive"};
		}
		if (reason === "missing_role") {
			return {label: t("Missing Messaging Agent role"), pillClass: "is-disabled", barClass: "is-inactive"};
		}
		if (reason === "channel_disabled") {
			return {label: t("Channel disabled"), pillClass: "is-disabled", barClass: "is-inactive"};
		}
		if (reason === "account_disabled") {
			return {label: t("Account disabled"), pillClass: "is-disabled", barClass: "is-inactive"};
		}
		if (reason === "account_missing") {
			return {label: t("Retired or missing account"), pillClass: "is-retired", barClass: "is-inactive"};
		}
		if (reason === "channel_missing") {
			return {label: t("Retired or missing channel"), pillClass: "is-retired", barClass: "is-inactive"};
		}
		return {label: t("Disabled"), pillClass: "is-disabled", barClass: "is-inactive"};
	}

	function formatCountLabel(count, singularKey, pluralKey) {
		const n = Number(count || 0);
		return n === 1 ? t(singularKey, [n]) : t(pluralKey, [n]);
	}

	function consolidateAgentWorkload(grants) {
		const byUser = new Map();
		for (const grant of grants || []) {
			if (!grant || !grant.user) continue;
			if (!byUser.has(grant.user)) byUser.set(grant.user, []);
			byUser.get(grant.user).push(grant);
		}
		const nonOpReasons = new Set(["channel_missing", "account_missing", "channel_disabled", "account_disabled"]);
		const reasonRank = {user_disabled: 0, missing_role: 1, grant_disabled: 2};
		const out = [];
		for (const [user, userGrants] of byUser.entries()) {
			const first = userGrants[0];
			const opCount = userGrants.filter(g => g.effective_enabled).length;
			const availCount = userGrants.filter(g => g.can_receive).length;
			const fullCount = userGrants.filter(g => g.status_code === "full").length;
			const unavailCount = userGrants.filter(g => g.status_code === "unavailable").length;
			const seenScopes = new Map();
			for (const g of userGrants) {
				seenScopes.set(`${g.channel}:${g.account}`, g);
			}
			let activeConvs = 0;
			for (const g of seenScopes.values()) {
				activeConvs += Number(g.active_count != null ? g.active_count : (g.occupied || 0));
			}
			let statusCode = "not_enabled";
			let reasonCode = first.reason_code || "grant_disabled";
			if (availCount > 0) {
				statusCode = "available";
				reasonCode = "available";
			} else if (fullCount > 0) {
				statusCode = "full";
				reasonCode = "full";
			} else if (unavailCount > 0) {
				statusCode = "unavailable";
				reasonCode = "unavailable";
			} else {
				const enablingReasons = userGrants
					.map(g => g.reason_code)
					.filter(r => Object.prototype.hasOwnProperty.call(reasonRank, r));
				if (enablingReasons.length) {
					enablingReasons.sort((a, b) => reasonRank[a] - reasonRank[b]);
					statusCode = "not_enabled";
					reasonCode = enablingReasons[0];
				} else if (userGrants.every(g => nonOpReasons.has(g.reason_code))) {
					statusCode = "no_operational_accounts";
					reasonCode = "no_operational_accounts";
				}
			}
			out.push({
				user,
				full_name: first.full_name || user,
				user_enabled: Boolean(first.user_enabled),
				has_role: Boolean(first.has_role),
				account_count: userGrants.length,
				operational_account_count: opCount,
				available_account_count: availCount,
				full_account_count: fullCount,
				unavailable_account_count: unavailCount,
				non_operational_account_count: userGrants.length - opCount,
				active_conversations: activeConvs,
				can_receive: availCount > 0,
				status_code: statusCode,
				reason_code: reasonCode,
				grants: userGrants,
			});
		}
		return out;
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

	function allowedOrderKeysForState(tab, status) {
		const effectiveActive = tab === "active" || (tab === "all" && status === "Human Active");
		const effectiveQueued = tab === "queued" || (tab === "all" && status === "Human Queued");
		const keys = ["last_activity"];
		if (effectiveQueued && !effectiveActive) keys.push("longest_wait");
		if (effectiveActive && !effectiveQueued) keys.push("oldest_attention");
		return keys;
	}

	function renderAgentUserCardHtml(userGroup, selectedAgent) {
		const displayName = userGroup.full_name || userGroup.user;
		const meta = agentUserStatusMeta(userGroup);
		const grants = userGroup.grants || [];
		const accountCount = Number(userGroup.account_count != null ? userGroup.account_count : grants.length);
		const activeConvs = Number(userGroup.active_conversations || 0);
		const isSelected = Boolean(selectedAgent && selectedAgent === userGroup.user);
		const isInactive = userGroup.status_code === "not_enabled" || userGroup.status_code === "no_operational_accounts";

		const linkedLabel = formatCountLabel(accountCount, "{0} linked account", "{0} linked accounts");
		const activeConvLabel = formatCountLabel(activeConvs, "{0} active conversation", "{0} active conversations");

		return [
			`<button type="button" class="cs-agent-card ${isSelected ? "is-selected" : ""} ${isInactive ? "is-disabled" : ""}" `,
			`data-agent="${escapeHtml(userGroup.user)}" data-status="${escapeHtml(userGroup.status_code)}" `,
			`aria-pressed="${isSelected ? "true" : "false"}" data-testid="cs-agent-card">`,
			`<div class="cs-agent-card-top">`,
			`<div class="cs-agent-avatar">${escapeHtml(initialsOf(displayName))}</div>`,
			`<div class="cs-agent-meta">`,
			`<div class="cs-agent-name" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</div>`,
			`<div class="cs-agent-sub" title="${escapeHtml(linkedLabel)}">${escapeHtml(linkedLabel)}</div>`,
			`</div>`,
			`<span class="cs-agent-status-pill ${meta.pillClass}">${escapeHtml(meta.label)}</span>`,
			`</div>`,
			`<div class="cs-agent-metrics">`,
			`<div class="cs-agent-count">${escapeHtml(activeConvLabel)}</div>`,
			`</div>`,
			`</button>`,
		].join("");
	}

	function renderSessionCardHtml(row, selectedName) {
		const isSelected = row.name === selectedName;
		const displayName = row.contact_name || row.external_sender || row.contact || row.name;
		const channelLabel = row.channel_name || row.channel || "";
		const accountLabel = row.account_name || row.account_public_id || row.account || "";
		const isQueued = row.status === "Human Queued";
		const timeText = isQueued
			? formatDuration(row.supervision_wait_seconds != null ? row.supervision_wait_seconds : row.wait_time_seconds, "0s")
			: formatRelative(row.last_message_time || row.last_activity);
		const timeTitle = isQueued ? t("Waiting Time") : t("Last Activity");
		const statusBadgeClass = isQueued ? "cs-badge-waiting" : "cs-badge-in-attention";
		const agentBadgeHtml = isQueued || !row.assigned_agent
			? `<span class="cs-badge cs-badge-unassigned">${escapeHtml(t("Unassigned"))}</span>`
			: `<span class="cs-badge cs-badge-agent">${escapeHtml(row.assigned_agent_name || row.assigned_agent)}</span>`;
		const unread = Number(row.unread || 0);
		const previewHtml = row.last_message
			? escapeHtml(row.last_message)
			: `<span class="font-italic text-muted">${escapeHtml(t("No messages yet"))}</span>`;

		return [
			`<button type="button" class="cs-session-item ${isSelected ? "is-selected" : ""}" data-session="${escapeHtml(row.name)}" data-testid="cs-session-item">`,
			`<div class="cs-item-avatar">${escapeHtml(initialsOf(displayName))}</div>`,
			`<div class="cs-item-body">`,
			`<div class="cs-item-top">`,
			`<span class="cs-item-name" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</span>`,
			`<span class="cs-item-time" title="${escapeHtml(timeTitle)}">${isQueued ? iconHtml("clock", "xs") : ""}${escapeHtml(timeText)}</span>`,
			`</div>`,
			`<div class="cs-item-provenance">`,
			channelLabel ? `<span class="tag-channel">${escapeHtml(channelLabel)}</span>` : "",
			accountLabel ? `<span class="tag-account" title="${escapeHtml(accountLabel)}">${escapeHtml(accountLabel)}</span>` : "",
			`<span class="cs-item-session-id">${escapeHtml(row.name)}</span>`,
			`</div>`,
			`<div class="cs-item-preview" title="${escapeHtml(row.last_message || "")}">${previewHtml}</div>`,
			`<div class="cs-item-footer">`,
			`<div class="cs-item-badges">`,
			`<span class="cs-badge ${statusBadgeClass}">${escapeHtml(statusLabel(row.status))}</span>`,
			agentBadgeHtml,
			`</div>`,
			unread > 0 ? `<span class="cs-unread-badge" title="${escapeHtml(t("Unread"))}">${escapeHtml(unread)}</span>` : "",
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
		formatRelative,
		statusLabel,
		agentStatusMeta,
		agentUserStatusMeta,
		grantStatusMeta,
		consolidateAgentWorkload,
		getTransportStatusSpecs,
		collectDeduplicatedRegistration,
		buildMessageAriaLabel,
		allowedOrderKeysForState,
		renderAgentUserCardHtml,
		renderSessionCardHtml,
	};
}

if (typeof module !== "undefined" && module.exports) {
	module.exports = createConversationSupervisionHelpers();
}

if (typeof frappe !== "undefined" && frappe.pages) {
	const csHelpers = createConversationSupervisionHelpers();
	const API_BASE = "messaging.conversation.doctype.chat_session.supervision";
	const REALTIME_EVENT = "messaging_conversation_update";
	const PAGE_LIMIT = 30;
	const MESSAGE_LIMIT = 60;

	frappe.pages["conversation-supervision"].on_page_load = function (wrapper) {
		const page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Conversation Supervision"),
			single_column: true,
		});
		$(wrapper).addClass("cs-page");
		page.set_title_sub(__("Monitor ongoing human attentions and agent workload."));

		const root = $(frappe.render_template("conversation_supervision")).appendTo(page.body);
		root.find(".cs-empty-icon").html(csHelpers.iconHtml("users", "md"));
		root.find(".cs-back-icon").html(csHelpers.iconHtml("arrow-left", "xs"));

		const els = {
			root,
			kpiActive: root.find('[data-kpi-val="in_attention"]'),
			kpiWaiting: root.find('[data-kpi-val="waiting"]'),
			kpiAgents: root.find('[data-kpi-val="available_agents"]'),
			kpiAvgWait: root.find('[data-kpi-val="avg_wait"]'),
			agentScopeLabel: root.find(".cs-agent-scope-label"),
			clearAgentBtn: root.find(".cs-clear-agent-filter"),
			agentCards: root.find(".cs-agent-cards"),
			search: root.find(".cs-search-input"),
			filterToggleBtn: root.find(".btn-cs-filter"),
			filterClearBtn: root.find(".filter-x-button"),
			filterPanel: root.find(".cs-filter-panel"),
			filterBar: root.find(".cs-active-filter-bar"),
			channel: root.find('[data-filter="channel"]'),
			account: root.find('[data-filter="account"]'),
			status: root.find('[data-filter="status"]'),
			agent: root.find('[data-filter="agent"]'),
			availability: root.find('[data-filter="availability"]'),
			orderBy: root.find('[data-filter="order_by"]'),
			list: root.find(".cs-list-body"),
			pagination: root.find(".cs-pagination"),
			paginationInfo: root.find(".cs-pagination-info"),
			pagePrev: root.find(".cs-page-prev"),
			pageNext: root.find(".cs-page-next"),
			chatEmpty: root.find(".cs-chat-empty"),
			chatEmptyTitle: root.find('[data-testid="cs-chat-empty-title"]'),
			chatEmptyDesc: root.find('[data-testid="cs-chat-empty-desc"]'),
			chatActive: root.find(".cs-chat-active"),
			chatHead: root.find(".cs-chat-head"),
			readonlyBanner: root.find(".cs-readonly-banner"),
			timeline: root.find(".cs-timeline"),
			infoEmpty: root.find(".cs-info-empty"),
			infoBody: root.find(".cs-info-body"),
		};

		const state = {
			tab: "all",
			detailTab: "session",
			sessions: [],
			total: 0,
			start: 0,
			limit: PAGE_LIMIT,
			hasMore: false,
			selectedName: null,
			activeData: null,
			messages: [],
			hasMoreMessages: false,
			workload: [],
			agentUsers: null,
			channels: [],
			accounts: [],
			agents: [],
			filtersOpen: false,
			filters: {
				search: "",
				searchCondition: "like",
				channel: "",
				account: "",
				status: "",
				agent: "",
				availability: "",
				order_by: "last_activity",
			},
			loadingMoreMessages: false,
			busyAction: false,
			overviewRequestSeq: 0,
			listRequestSeq: 0,
			detailRequestSeq: 0,
			realtimeBound: false,
			realtimeHandler: null,
			refreshTimer: null,
			pollTimer: null,
			searchTimer: null,
			pageActive: false,
		};

		function setMobileView(view) {
			els.root.attr("data-mobile-view", view || "summary");
		}

		function fitHeight() {
			if (!state.pageActive || !root[0]) return;
			if (window.innerWidth <= 767) {
				root.css("height", "");
				return;
			}
			if (root[0].getClientRects().length) {
				root.height(Math.max(520, window.innerHeight - root[0].getBoundingClientRect().top - 16));
			}
		}

		async function callGet(method, args) {
			const res = await frappe.call({
				method: `${API_BASE}.${method}`,
				args: args || {},
				type: "GET",
				silent: true,
			});
			return res && res.message;
		}

		async function callPost(method, args) {
			const res = await frappe.call({
				method: `${API_BASE}.${method}`,
				args: args || {},
				type: "POST",
			});
			return res && res.message;
		}

		function matchIcon(condition) {
			return csHelpers.iconHtml(condition === "=" ? "equal" : "equal-approximately");
		}

		function initFilterControls() {
			root.find(".btn-cs-filter .filter-icon").html(csHelpers.iconHtml("funnel"));
			root.find(".filter-x-button .filter-icon").html(csHelpers.iconHtml("x"));
			root.find(".cs-search-match-icon").html(matchIcon(state.filters.searchCondition));
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
					`<span class="es-badge form-tag active-filter-tag" data-theme="${theme}" data-size="md" title="${csHelpers.escapeHtml(label)}">` +
					`<span class="es-badge__label pill-label ellipsis">${csHelpers.escapeHtml(label)}</span>` +
					`<span class="es-badge__affix" role="button" tabindex="0" aria-label="${csHelpers.escapeHtml(__("Remove filter"))}">${csHelpers.iconHtml("x", "sm")}</span>` +
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

		function renderSummary(summary) {
			const s = summary || {};
			els.kpiActive.text(s.in_attention != null ? s.in_attention : 0);
			els.kpiWaiting.text(s.waiting != null ? s.waiting : 0);
			els.kpiAgents.text(s.available_agents != null ? s.available_agents : 0);
			els.kpiAvgWait.text(
				s.avg_wait_seconds == null ? "—" : csHelpers.formatDuration(s.avg_wait_seconds, "0s")
			);
		}

		function renderAgentWorkload() {
			const users = Array.isArray(state.agentUsers)
				? state.agentUsers
				: csHelpers.consolidateAgentWorkload(state.workload || []);
			const scopeParts = [];
			if (state.filters.channel) {
				const ch = (state.channels || []).find(c => c.name === state.filters.channel);
				scopeParts.push((ch && (ch.channel_name || ch.name)) || state.filters.channel);
			}
			if (state.filters.account) {
				const acc = (state.accounts || []).find(a => a.name === state.filters.account);
				scopeParts.push((acc && (acc.account_name || acc.account_id || acc.name)) || state.filters.account);
			}
			els.agentScopeLabel.text(scopeParts.length ? scopeParts.join(" · ") : __("All accounts"));
			els.clearAgentBtn.toggleClass("hidden", !state.filters.agent);

			if (!users.length) {
				els.agentCards.html(`<div class="cs-agent-empty text-muted small">${csHelpers.escapeHtml(__("No configured agents in this scope."))}</div>`);
				return;
			}
			els.agentCards.html(
				users.map(u => csHelpers.renderAgentUserCardHtml(u, state.filters.agent)).join("")
			);
		}

		function syncOrderOptions() {
			const allowed = csHelpers.allowedOrderKeysForState(state.tab, state.filters.status);
			if (!allowed.includes(state.filters.order_by)) {
				state.filters.order_by = "last_activity";
			}
			els.orderBy.find("option").each(function () {
				const val = $(this).attr("value");
				$(this).prop("disabled", !allowed.includes(val));
			});
			els.orderBy.val(state.filters.order_by);
		}

		function populateFilterSelects() {
			const chVal = state.filters.channel;
			els.channel.empty().append(`<option value="">${csHelpers.escapeHtml(__("All Channels"))}</option>`);
			for (const ch of state.channels || []) {
				els.channel.append(`<option value="${csHelpers.escapeHtml(ch.name)}">${csHelpers.escapeHtml(ch.channel_name || ch.name)}</option>`);
			}
			els.channel.val(chVal);

			const accVal = state.filters.account;
			els.account.empty().append(`<option value="">${csHelpers.escapeHtml(__("All Accounts"))}</option>`);
			for (const acc of state.accounts || []) {
				if (chVal && acc.channel !== chVal) continue;
				const accLabel = acc.account_name || acc.account_id || acc.name;
				els.account.append(`<option value="${csHelpers.escapeHtml(acc.name)}">${csHelpers.escapeHtml(accLabel)}</option>`);
			}
			if (accVal && els.account.find(`option[value="${CSS.escape(accVal)}"]`).length === 0) {
				state.filters.account = "";
			} else {
				els.account.val(accVal);
			}

			const agVal = state.filters.agent;
			els.agent.empty().append(`<option value="">${csHelpers.escapeHtml(__("All Agents"))}</option>`);
			for (const ag of state.agents || []) {
				els.agent.append(`<option value="${csHelpers.escapeHtml(ag.user)}">${csHelpers.escapeHtml(ag.full_name || ag.user)}</option>`);
			}
			els.agent.val(agVal);
		}

		function syncFilterInputsFromState() {
			root.find(".cs-tab-btn").each(function () {
				const tVal = $(this).attr("data-tab") || "all";
				$(this).toggleClass("active", tVal === state.tab);
			});
			els.channel.val(state.filters.channel);
			els.account.val(state.filters.account);
			els.status.val(state.filters.status);
			els.agent.val(state.filters.agent);
			els.availability.val(state.filters.availability);
			syncOrderOptions();
			renderActiveFilterTags();
		}

		function renderActiveFilterTags() {
			const f = state.filters;
			const tags = [];
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
			if (f.status) {
				tags.push({
					label: `${__("Status")}: ${csHelpers.statusLabel(f.status)}`,
					clear: () => { state.filters.status = ""; },
				});
			}
			if (f.agent) {
				const ag = (state.agents || []).find(a => a.user === f.agent);
				tags.push({
					label: `${__("Agent")}: ${(ag && ag.full_name) || f.agent}`,
					clear: () => { state.filters.agent = ""; },
				});
			}
			if (f.availability) {
				tags.push({
					label: `${__("Agent Availability")}: ${csHelpers.agentStatusMeta(f.availability).label}`,
					clear: () => { state.filters.availability = ""; },
				});
			}

			els.filterBar.empty();
			for (const item of tags) {
				els.filterBar.append(buildBadgeTag(item.label, () => {
					item.clear();
					state.start = 0;
					populateFilterSelects();
					syncFilterInputsFromState();
					renderAgentWorkload();
					refreshAll();
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

		function hasActiveFilters() {
			const f = state.filters;
			return Boolean(f.search || f.channel || f.account || f.status || f.agent || f.availability);
		}

		function renderConversationList() {
			const rows = state.sessions || [];
			if (!rows.length) {
				let msg = __("No active conversations");
				if (hasActiveFilters()) {
					msg = __("No conversations match your filters.");
				} else if (state.tab === "queued") {
					msg = __("No pending requests");
				}
				els.list.html(`<div class="cs-empty-list" data-testid="cs-empty-list">${csHelpers.escapeHtml(msg)}</div>`);
				els.pagination.addClass("hidden");
				return;
			}
			els.list.html(rows.map(r => csHelpers.renderSessionCardHtml(r, state.selectedName)).join(""));
			const fromIdx = state.start + 1;
			const toIdx = state.start + rows.length;
			els.paginationInfo.text(`${fromIdx}–${toIdx} / ${state.total}`);
			els.pagePrev.prop("disabled", state.start <= 0);
			els.pageNext.prop("disabled", !state.hasMore);
			els.pagination.toggleClass("hidden", state.total <= state.limit && state.start === 0);
		}

		function updateSelectedCardClass() {
			els.list.find(".cs-session-item").each(function () {
				const name = $(this).attr("data-session");
				$(this).toggleClass("is-selected", name === state.selectedName);
			});
		}

		function renderStatusIconsHtml(transport) {
			const specs = csHelpers.getTransportStatusSpecs(transport);
			if (!specs.length) return "";
			return `<span class="cs-status-icons">${specs.map(s =>
				`<span class="cs-status-icon" data-icon="${csHelpers.escapeHtml(s.icon)}" data-tone="${csHelpers.escapeHtml(s.tone || "muted")}" role="img" aria-label="${csHelpers.escapeHtml(s.label)}" title="${csHelpers.escapeHtml(s.label)}">${csHelpers.iconHtml(s.icon, "xs", s.spinning ? "cs-icon-spin" : "")}</span>`
			).join("")}</span>`;
		}

		function renderTimeline(scrollMode) {
			const data = state.activeData;
			if (!data || !data.session) return;
			const el = els.timeline[0];
			const prevScrollHeight = el ? el.scrollHeight : 0;
			const prevScrollTop = el ? el.scrollTop : 0;
			const nearBottom = el ? (el.scrollHeight - el.scrollTop - el.clientHeight < 90) : true;
			const tz = csHelpers.getFrappeTimeZone();
			const messages = state.messages || [];
			const parts = [];

			if (state.hasMoreMessages) {
				parts.push(
					`<button type="button" class="btn btn-default btn-xs cs-load-more" data-testid="cs-load-more">${csHelpers.escapeHtml(__("Load earlier messages"))}</button>`
				);
			}

			if (!messages.length) {
				parts.push(`<div class="cs-empty-list">${csHelpers.escapeHtml(__("No retained messages are available."))}</div>`);
			} else {
				for (const m of messages) {
					const msgStamp = m.sent_at || m.event_at;
					if (m.kind === "Event") {
						parts.push(
							`<div class="cs-event-row" data-kind="Event" data-message="${csHelpers.escapeHtml(m.name || "")}">` +
							`<span>${csHelpers.escapeHtml(m.content || "")}</span>` +
							(msgStamp ? `<span class="cs-event-time" title="${csHelpers.escapeHtml(csHelpers.formatDateTime(msgStamp, tz))}">${csHelpers.escapeHtml(csHelpers.formatTime(msgStamp, tz))}</span>` : "") +
							`</div>`
						);
						continue;
					}
					const isIncoming = m.kind === "Incoming";
					const originLabel = isIncoming ? "" : (m.origin_label || "");
					const iconsHtml = isIncoming ? "" : renderStatusIconsHtml(m.transport);
					parts.push(
						`<div class="cs-msg ${isIncoming ? "is-incoming" : "is-outgoing"}" ` +
						`data-kind="${csHelpers.escapeHtml(m.kind)}" ` +
						`data-message="${csHelpers.escapeHtml(m.name || "")}" ` +
						`aria-label="${csHelpers.escapeHtml(csHelpers.buildMessageAriaLabel(m))}">` +
						(originLabel ? `<div class="cs-msg-origin">${csHelpers.escapeHtml(originLabel)}</div>` : "") +
						`<div class="cs-msg-content">${csHelpers.escapeHtml(m.content || "")}</div>` +
						`<div class="cs-msg-meta">` +
						`<span class="cs-msg-time" title="${csHelpers.escapeHtml(csHelpers.formatDateTime(msgStamp, tz))}">${csHelpers.escapeHtml(csHelpers.formatDateTime(msgStamp, tz))}</span>` +
						iconsHtml +
						`</div>` +
						`</div>`
					);
				}
			}

			els.timeline.html(parts.join(""));
			if (el) {
				if (scrollMode === "bottom" || (scrollMode === "keep" && nearBottom)) {
					el.scrollTop = el.scrollHeight;
				} else if (scrollMode === "prepend") {
					const addedHeight = el.scrollHeight - prevScrollHeight;
					el.scrollTop = Math.max(0, prevScrollTop + addedHeight);
				}
			}
		}

		function openInInbox(sessionName) {
			frappe.route_options = {session: sessionName};
			frappe.set_route("conversation-inbox");
		}

		function openInHistory(sessionName) {
			frappe.route_options = {session: sessionName};
			frappe.set_route("conversation-history");
		}

		function renderChatHeader() {
			const data = state.activeData;
			const session = data && data.session;
			if (!session) return;
			const contact = data.contact || {};
			const displayName = contact.display_name || session.contact_name || contact.external_sender || session.contact || session.name;
			const channelLabel = session.channel_name || session.channel || "";
			const accountLabel = session.account_name || session.account_public_id || session.account || "";
			const subParts = [];
			if (channelLabel) {
				subParts.push(`<span class="tag-channel">${csHelpers.escapeHtml(channelLabel)}</span>`);
			}
			const metaText = [accountLabel, session.name].filter(Boolean).join(" · ");
			if (metaText) {
				subParts.push(`<span>${csHelpers.escapeHtml(metaText)}</span>`);
			}

			const isQueued = session.status === "Human Queued";
			const isActive = session.status === "Human Active";
			const isTerminal = Boolean(session.is_terminal);
			const pillClass = isActive ? "is-active" : (isQueued ? "is-queued" : "");

			const actionLinkHtml = isQueued
				? `<button type="button" class="btn btn-default btn-xs cs-open-inbox-btn" data-testid="cs-open-inbox-btn">${csHelpers.escapeHtml(__("Open in Inbox"))}</button>`
				: (isTerminal
					? `<button type="button" class="btn btn-default btn-xs cs-open-history-btn" data-testid="cs-open-history-btn">${csHelpers.escapeHtml(__("Open in History"))}</button>`
					: "");

			els.chatHead.html([
				`<div class="cs-head-left">`,
				`<button type="button" class="btn btn-default btn-xs cs-back-btn" data-testid="cs-back-btn" aria-label="${csHelpers.escapeHtml(__("Back to list"))}">${csHelpers.iconHtml("arrow-left", "xs")}</button>`,
				`<div class="cs-head-identity">`,
				`<div class="cs-head-name">${csHelpers.escapeHtml(displayName)}</div>`,
				`<div class="cs-head-sub">${subParts.join(" · ")}</div>`,
				`</div>`,
				`</div>`,
				`<div class="cs-head-actions">`,
				`<span class="cs-status-pill ${pillClass}" data-testid="cs-session-status">${csHelpers.escapeHtml(csHelpers.statusLabel(session.status))}</span>`,
				actionLinkHtml,
				`<button type="button" class="btn btn-default btn-xs cs-mobile-details-btn" data-testid="cs-mobile-details-btn">${csHelpers.escapeHtml(__("Details"))}</button>`,
				`</div>`,
			].join(""));

			if (isTerminal) {
				els.readonlyBanner
					.addClass("is-terminal")
					.html(
						`<span>${csHelpers.escapeHtml(__("This conversation has ended ({0}). Operational actions are disabled.", [csHelpers.statusLabel(session.status)]))}</span>` +
						`<button type="button" class="btn btn-default btn-xs cs-open-history-btn">${csHelpers.escapeHtml(__("Open in History"))}</button>`
					);
			} else {
				els.readonlyBanner
					.removeClass("is-terminal")
					.text(__("Supervision view (read-only). Messages cannot be sent or closed from here."));
			}
		}

		function renderInfoPanel() {
			root.find(".cs-detail-tab-btn").each(function () {
				const tVal = $(this).attr("data-detail-tab") || "session";
				$(this).toggleClass("active", tVal === state.detailTab);
			});

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
			const agentInfo = data.assigned_agent_info;
			const tz = csHelpers.getFrappeTimeZone();

			const channelLabel = (prov.channel && prov.channel.channel_name) || session.channel_name || session.channel || "—";
			const accountLabel = (prov.account && prov.account.account_name) || session.account_name || session.account || "—";

			if (state.detailTab === "session") {
				const reqCards = [];
				for (const req of data.human_requests || []) {
					const fieldsHtml = (req.fields || []).map(f =>
						`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(f.label || f.key)}</span><span class="cs-info-val">${csHelpers.escapeHtml(f.value != null && f.value !== "" ? f.value : "—")}</span></div>`
					).join("");
					if (fieldsHtml) {
						reqCards.push(`<div class="cs-request-card">${fieldsHtml}</div>`);
					}
				}

				const isActive = session.status === "Human Active" && Boolean(session.assigned_agent) && Boolean(session.can_reassign);
				const isQueued = session.status === "Human Queued";
				const isTerminal = Boolean(session.is_terminal);

				const actionsParts = [];
				if (isActive) {
					actionsParts.push(
						`<button type="button" class="btn btn-primary btn-sm btn-block cs-reassign-btn" data-testid="cs-btn-reassign">${csHelpers.escapeHtml(__("Reassign Conversation"))}</button>`
					);
				} else if (isQueued) {
					actionsParts.push(
						`<button type="button" class="btn btn-primary btn-sm btn-block cs-open-inbox-btn" data-testid="cs-btn-open-inbox">${csHelpers.escapeHtml(__("Open in Inbox"))}</button>`,
						`<button type="button" class="btn btn-default btn-sm btn-block cs-reassign-btn" disabled title="${csHelpers.escapeHtml(__("Queued requests must be taken from Conversation Inbox."))}" data-testid="cs-btn-reassign">${csHelpers.escapeHtml(__("Reassign Conversation"))}</button>`
					);
				} else if (isTerminal) {
					actionsParts.push(
						`<button type="button" class="btn btn-default btn-sm btn-block cs-open-history-btn" data-testid="cs-btn-open-history">${csHelpers.escapeHtml(__("Open in History"))}</button>`,
						`<button type="button" class="btn btn-default btn-sm btn-block cs-reassign-btn" disabled data-testid="cs-btn-reassign">${csHelpers.escapeHtml(__("Reassign Conversation"))}</button>`
					);
				}

				const waitText = session.supervision_wait_seconds != null
					? csHelpers.formatDuration(session.supervision_wait_seconds, "0s")
					: "—";
				const attentionText = session.attention_elapsed_seconds != null
					? csHelpers.formatDuration(session.attention_elapsed_seconds, "0s")
					: "—";

				els.infoBody.html([
					`<div class="cs-info-section">`,
					`<div class="cs-info-heading">${csHelpers.escapeHtml(__("Session"))}</div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Session"))}</span><span class="cs-info-val cs-info-mono">${csHelpers.escapeHtml(session.name)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Status"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(csHelpers.statusLabel(session.status))}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Attention type"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(session.attention_type_label || __("Human attention"))}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Channel"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(channelLabel)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Account"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(accountLabel)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Assigned Agent"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(session.assigned_agent_name || __("Unassigned"))}</span></div>`,
					`</div>`,
					`<div class="cs-info-section">`,
					`<div class="cs-info-heading">${csHelpers.escapeHtml(__("Operational Timings"))}</div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Started At"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(session.started_at ? csHelpers.formatDateTime(session.started_at, tz) : "—")}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Last Activity"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(session.last_activity ? csHelpers.formatDateTime(session.last_activity, tz) : "—")}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Time in Attention"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(attentionText)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Waiting Time"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(waitText)}</span></div>`,
					`</div>`,
					`<div class="cs-info-section">`,
					`<div class="cs-info-heading">${csHelpers.escapeHtml(__("Human request details"))}</div>`,
					reqCards.length ? reqCards.join("") : `<div class="text-muted small">—</div>`,
					`</div>`,
					actionsParts.length ? `<div class="cs-info-section"><div class="cs-info-heading">${csHelpers.escapeHtml(__("Actions"))}</div><div class="cs-actions-stack">${actionsParts.join("")}</div></div>` : "",
				].filter(Boolean).join("")).removeClass("hidden");
				return;
			}

			if (state.detailTab === "agent") {
				if (!session.assigned_agent || !agentInfo) {
					els.infoBody.html([
						`<div class="cs-info-section">`,
						`<div class="cs-info-heading">${csHelpers.escapeHtml(__("Agent"))}</div>`,
						`<div class="text-muted small">${csHelpers.escapeHtml(__("This conversation has not been assigned to an agent yet."))}</div>`,
						`</div>`,
					].join("")).removeClass("hidden");
					return;
				}
				const statusMeta = csHelpers.grantStatusMeta(agentInfo);
				const enabledText = agentInfo.effective_enabled ? __("Enabled") : __("Disabled");
				const availText = agentInfo.available ? __("Available") : __("Unavailable");
				els.infoBody.html([
					`<div class="cs-contact-hero">`,
					`<div class="cs-avatar">${csHelpers.escapeHtml(csHelpers.initialsOf(agentInfo.full_name || agentInfo.user))}</div>`,
					`<div class="cs-contact-name">${csHelpers.escapeHtml(agentInfo.full_name || agentInfo.user)}</div>`,
					`<div class="cs-contact-handle">${csHelpers.escapeHtml(agentInfo.user)}</div>`,
					`</div>`,
					`<div class="cs-info-section">`,
					`<div class="cs-info-heading">${csHelpers.escapeHtml(__("Availability & Capacity"))}</div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Status"))}</span><span class="cs-info-val"><span class="cs-agent-status-pill ${statusMeta.pillClass}">${csHelpers.escapeHtml(statusMeta.label)}</span></span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Enabled"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(enabledText)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Agent Availability"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(availText)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Channel"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(agentInfo.channel_name || channelLabel)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Account"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(agentInfo.account_name || accountLabel)}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Configured Capacity"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(agentInfo.has_grant ? String(agentInfo.capacity) : "—")}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Active Conversations"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(String(agentInfo.occupied || 0))}</span></div>`,
					`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Available Capacity"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(agentInfo.has_grant ? String(agentInfo.remaining || 0) : "—")}</span></div>`,
					`</div>`,
				].join("")).removeClass("hidden");
				return;
			}

			// Contact tab (strictly read-only & permission-filtered)
			const displayName = contact.display_name || session.contact_name || contact.external_sender || session.contact || session.name;
			const handle = contact.external_sender || "—";
			const profileRows = (contact.profile_fields || []).map(pf =>
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(pf.label || pf.key)}</span><span class="cs-info-val">${csHelpers.escapeHtml(pf.value != null && pf.value !== "" ? pf.value : "—")}</span></div>`
			).join("");
			const regRows = csHelpers.collectDeduplicatedRegistration(contact.profile_fields || [], contact.registration).map(r =>
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(r.label)}</span><span class="cs-info-val">${csHelpers.escapeHtml(r.value)}</span></div>`
			).join("");

			els.infoBody.html([
				`<div class="cs-contact-hero">`,
				`<div class="cs-avatar">${csHelpers.escapeHtml(csHelpers.initialsOf(displayName))}</div>`,
				`<div class="cs-contact-name">${csHelpers.escapeHtml(displayName)}</div>`,
				`<div class="cs-contact-handle">${csHelpers.escapeHtml(handle)}</div>`,
				`</div>`,
				`<div class="cs-info-section">`,
				`<div class="cs-info-heading">${csHelpers.escapeHtml(__("Contact"))}</div>`,
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Public Identifier"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(handle)}</span></div>`,
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Channel"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(channelLabel)}</span></div>`,
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Account"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(accountLabel)}</span></div>`,
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Language"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(contact.preferred_language || "—")}</span></div>`,
				`<div class="cs-info-row"><span class="cs-info-key">${csHelpers.escapeHtml(__("Registration Status"))}</span><span class="cs-info-val">${csHelpers.escapeHtml(__(contact.registration_status || "Pending"))}</span></div>`,
				`</div>`,
				(profileRows || regRows)
					? `<div class="cs-info-section"><div class="cs-info-heading">${csHelpers.escapeHtml(__("Registration Data"))}</div>${profileRows}${regRows}</div>`
					: "",
			].filter(Boolean).join("")).removeClass("hidden");
		}

		function renderWorkspace(scrollMode) {
			const hasSession = Boolean(state.activeData && state.activeData.session);
			els.chatEmpty.toggleClass("hidden", hasSession);
			els.chatActive.toggleClass("hidden", !hasSession);
			if (hasSession) {
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
			if (opts.emptyTitle || opts.emptyDesc) {
				els.chatEmptyTitle.text(opts.emptyTitle);
				els.chatEmptyDesc.text(opts.emptyDesc);
			} else {
				els.chatEmptyTitle.text(__("Select a conversation to supervise"));
				els.chatEmptyDesc.text(__("Choose an active conversation or queued request to review messages, events, and agent load."));
			}
			updateSelectedCardClass();
			renderWorkspace(null);
		}

		async function loadOverview() {
			const reqId = ++state.overviewRequestSeq;
			try {
				const data = await callGet("overview", {
					channel: state.filters.channel || undefined,
					account: state.filters.account || undefined,
				});
				if (reqId !== state.overviewRequestSeq || !data) return;
				state.workload = data.workload || [];
				state.agentUsers = data.agent_users || [];
				state.channels = data.channels || [];
				state.accounts = data.accounts || [];
				state.agents = data.agents || [];
				renderSummary(data.summary);
				renderAgentWorkload();
				populateFilterSelects();
				syncFilterInputsFromState();
			} catch (err) {
				if (reqId === state.overviewRequestSeq) {
					frappe.show_alert({message: __("Unable to load supervision summary."), indicator: "red"});
				}
			}
		}

		async function loadConversations(options) {
			const opts = options || {};
			if (opts.reset !== false) {
				state.start = 0;
			}
			const reqId = ++state.listRequestSeq;
			try {
				const res = await callGet("conversations", {
					tab: state.tab,
					status: state.filters.status || undefined,
					channel: state.filters.channel || undefined,
					account: state.filters.account || undefined,
					agent: state.filters.agent || undefined,
					availability: state.filters.availability || undefined,
					search: state.filters.search || undefined,
					search_condition: state.filters.searchCondition || "like",
					order_by: state.filters.order_by || "last_activity",
					start: state.start,
					limit: state.limit,
				});
				if (reqId !== state.listRequestSeq) return;
				const items = (res && res.items) || [];
				const total = Number((res && res.total) || 0);
				if (items.length === 0 && total > 0 && state.start > 0) {
					state.start = 0;
					return loadConversations({reset: true});
				}
				state.sessions = items;
				state.total = total;
				state.hasMore = Boolean(res && res.has_more);
				renderConversationList();
			} catch (err) {
				if (reqId !== state.listRequestSeq) return;
				els.list.html(`<div class="cs-empty-list">${csHelpers.escapeHtml(__("Unable to load conversations."))}</div>`);
			} finally {
				if (reqId === state.listRequestSeq) fitHeight();
			}
		}

		async function openSession(sessionName, options) {
			if (!sessionName) return;
			const opts = options || {};
			const reqId = ++state.detailRequestSeq;
			state.selectedName = sessionName;
			if (!opts.keepMobileView) {
				setMobileView("chat");
			}
			updateSelectedCardClass();

			try {
				const res = await callGet("detail", {
					session_name: sessionName,
					limit: MESSAGE_LIMIT,
				});
				if (reqId !== state.detailRequestSeq || state.selectedName !== sessionName) return;
				state.activeData = res;
				state.messages = (res && res.messages) || [];
				state.hasMoreMessages = Boolean(res && res.has_more);
				renderWorkspace(opts.scrollMode || "bottom");
			} catch (err) {
				if (reqId !== state.detailRequestSeq) return;
				clearActiveSession({
					emptyTitle: __("Conversation no longer available"),
					emptyDesc: __("This conversation is no longer available or you do not have permission to view it."),
				});
				frappe.show_alert({message: __("Conversation no longer available"), indicator: "orange"});
			}
		}

		async function loadEarlierMessages() {
			if (!state.selectedName || !state.messages.length || state.loadingMoreMessages) return;
			const sessionName = state.selectedName;
			const earliestSeq = state.messages[0].sequence;
			if (!earliestSeq) return;
			const reqId = state.detailRequestSeq;
			state.loadingMoreMessages = true;
			try {
				const res = await callGet("detail", {
					session_name: sessionName,
					before: earliestSeq,
					limit: MESSAGE_LIMIT,
				});
				if (reqId !== state.detailRequestSeq || state.selectedName !== sessionName) return;
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

		async function openReassignDialog() {
			const activeSession = state.activeData && state.activeData.session;
			if (!activeSession || activeSession.status !== "Human Active" || !activeSession.assigned_agent || state.busyAction) {
				return;
			}
			let payload;
			try {
				payload = await callGet("reassignment_candidates", {session_name: activeSession.name});
			} catch (err) {
				frappe.show_alert({message: __("This conversation can no longer be reassigned."), indicator: "orange"});
				await refreshAll();
				return;
			}
			if (!payload || !payload.session) return;
			const s = payload.session;
			const candidates = payload.candidates || [];
			const eligibleCandidates = candidates.filter(c => c.eligible);

			const dialog = new frappe.ui.Dialog({
				title: __("Reassign Conversation"),
				fields: [{fieldname: "body_html", fieldtype: "HTML"}],
				primary_action_label: __("Confirm Reassignment"),
				secondary_action_label: __("Cancel"),
				primary_action: async () => {
					if (state.busyAction) return;
					const newAgent = dialog.$wrapper.find(".cs-reassign-select").val();
					if (!newAgent) {
						frappe.show_alert({message: __("Select a new agent for reassignment."), indicator: "orange"});
						return;
					}
					state.busyAction = true;
					dialog.get_primary_btn().prop("disabled", true);
					try {
						await callPost("reassign", {
							session_name: s.name,
							new_agent: newAgent,
							expected_modified: s.modified,
						});
						dialog.hide();
						frappe.show_alert({message: __("Conversation updated"), indicator: "green"});
						await refreshAll();
					} catch (err) {
						dialog.get_primary_btn().prop("disabled", false);
						frappe.show_alert({
							message: __("The conversation changed or the agent is unavailable. Refreshing..."),
							indicator: "orange",
						});
						await refreshAll();
					} finally {
						state.busyAction = false;
					}
				},
			});

			const optionsHtml = [
				`<option value="">${csHelpers.escapeHtml(__("Select an agent..."))}</option>`,
				...candidates.map(c => {
					const statusTxt = csHelpers.agentStatusMeta(c.status_code).label;
					const scopeTxt = `${c.channel_name || c.channel} · ${c.account_name || c.account}`;
					const label = `${c.full_name} — ${statusTxt} (${c.occupied}/${c.capacity}) · ${scopeTxt}`;
					return `<option value="${csHelpers.escapeHtml(c.user)}" ${c.eligible ? "" : "disabled"}>${csHelpers.escapeHtml(label)}</option>`;
				}),
			].join("");

			const noEligibleHint = eligibleCandidates.length === 0
				? `<div class="text-muted small mt-2">${csHelpers.escapeHtml(__("No authorized agents with available capacity were found for this account."))}</div>`
				: "";

			dialog.fields_dict.body_html.$wrapper.html([
				`<div class="cs-reassign-modal-body">`,
				`<div class="form-group">`,
				`<label class="control-label">${csHelpers.escapeHtml(__("Conversation"))}</label>`,
				`<div class="form-control-static small"><strong>${csHelpers.escapeHtml(s.name)}</strong> · ${csHelpers.escapeHtml(s.contact_name)}</div>`,
				`</div>`,
				`<div class="form-group">`,
				`<label class="control-label">${csHelpers.escapeHtml(__("Current Agent"))}</label>`,
				`<div class="form-control-static small">${csHelpers.escapeHtml(s.assigned_agent_name || s.assigned_agent)}</div>`,
				`</div>`,
				`<div class="form-group">`,
				`<label class="control-label">${csHelpers.escapeHtml(__("Channel"))} / ${csHelpers.escapeHtml(__("Account"))}</label>`,
				`<div class="form-control-static small">${csHelpers.escapeHtml(`${s.channel_name} · ${s.account_name}`)}</div>`,
				`</div>`,
				`<div class="form-group mb-0">`,
				`<label class="control-label" for="cs-reassign-select">${csHelpers.escapeHtml(__("New Agent"))}</label>`,
				`<select id="cs-reassign-select" class="form-control cs-reassign-select" data-testid="cs-reassign-select">${optionsHtml}</select>`,
				noEligibleHint,
				`</div>`,
				`</div>`,
			].join(""));

			dialog.get_primary_btn().prop("disabled", true).addClass("disabled");
			dialog.$wrapper.find(".cs-reassign-select").on("change", function () {
				const chosen = Boolean($(this).val());
				dialog.get_primary_btn().prop("disabled", !chosen).toggleClass("disabled", !chosen);
			});
			dialog.show();
		}

		async function refreshAll(options) {
			const opts = options || {};
			await Promise.all([
				loadOverview(),
				loadConversations({reset: opts.resetList === true}),
			]);
			if (state.selectedName && !opts.skipDetail) {
				await openSession(state.selectedName, {scrollMode: "keep", keepMobileView: true});
			}
		}

		function scheduleRefresh() {
			if (state.refreshTimer) clearTimeout(state.refreshTimer);
			state.refreshTimer = setTimeout(() => {
				if (state.pageActive && frappe.get_route()[0] === "conversation-supervision" && !document.hidden && !state.busyAction) {
					refreshAll();
				}
			}, 250);
		}

		function bindRealtime() {
			if (state.realtimeBound || !frappe.realtime || typeof frappe.realtime.on !== "function") return;
			state.realtimeHandler = payload => {
				if (!state.pageActive || frappe.get_route()[0] !== "conversation-supervision" || document.hidden) return;
				if (state.filters.channel && payload && payload.channel && payload.channel !== state.filters.channel) return;
				if (state.filters.account && payload && payload.account && payload.account !== state.filters.account) return;
				scheduleRefresh();
			};
			frappe.realtime.on(REALTIME_EVENT, state.realtimeHandler);
			state.realtimeBound = true;
		}

		function unbindRealtime() {
			if (state.realtimeBound && state.realtimeHandler && frappe.realtime && typeof frappe.realtime.off === "function") {
				frappe.realtime.off(REALTIME_EVENT, state.realtimeHandler);
			}
			state.realtimeBound = false;
			state.realtimeHandler = null;
		}

		function startPolling() {
			if (state.pollTimer) clearInterval(state.pollTimer);
			state.pollTimer = setInterval(() => {
				if (!state.pageActive || document.hidden || frappe.get_route()[0] !== "conversation-supervision" || state.busyAction) return;
				const rtConnected = Boolean(frappe.realtime && frappe.realtime.socket && frappe.realtime.socket.connected);
				if (!rtConnected) {
					refreshAll();
				}
			}, 60000);
		}

		function clearAllFilters() {
			state.filters.search = "";
			state.filters.searchCondition = "like";
			state.filters.channel = "";
			state.filters.account = "";
			state.filters.status = "";
			state.filters.agent = "";
			state.filters.availability = "";
			state.filters.order_by = "last_activity";
			state.start = 0;
			els.search.val("");
			root.find(".cs-search-match-icon").html(matchIcon("like"));
			root.find(".match-type-dropdown-btn").attr("title", __("Like"));
			populateFilterSelects();
			syncFilterInputsFromState();
			refreshAll({resetList: true});
		}

		const refreshBtn = page.set_primary_action(__("Refresh"), () => refreshAll(), "refresh");
		if (refreshBtn && refreshBtn.attr) {
			refreshBtn.attr("data-testid", "cs-refresh-btn");
		}

		initFilterControls();
		syncFilterInputsFromState();
		$(window).on("resize.conversation_supervision", fitHeight);

		// KPI shortcut cards
		root.on("click", "button.cs-metric-card[data-tab-jump]", function () {
			const nextTab = $(this).attr("data-tab-jump");
			if (!nextTab) return;
			state.tab = nextTab;
			state.filters.status = "";
			state.start = 0;
			syncFilterInputsFromState();
			setMobileView("list");
			loadConversations({reset: true});
		});

		// Agent strip card click toggles agent filter without altering other filters
		els.agentCards.on("click", ".cs-agent-card", function () {
			const wasFocused = document.activeElement === this;
			const agentUser = $(this).attr("data-agent") || "";
			state.filters.agent = state.filters.agent === agentUser ? "" : agentUser;
			state.start = 0;
			syncFilterInputsFromState();
			renderAgentWorkload();
			if (wasFocused && agentUser) {
				const btn = els.agentCards.find(".cs-agent-card").filter(function () {
					return $(this).attr("data-agent") === agentUser;
				});
				if (btn.length) btn[0].focus();
			}
			setMobileView("list");
			loadConversations({reset: true});
		});

		els.clearAgentBtn.on("click", () => {
			state.filters.agent = "";
			state.start = 0;
			syncFilterInputsFromState();
			renderAgentWorkload();
			loadConversations({reset: true});
		});

		// Left panel tabs (In Attention / Waiting / All)
		root.on("click", ".cs-tab-btn", function () {
			const nextTab = $(this).attr("data-tab") || "all";
			if (nextTab === state.tab) return;
			state.tab = nextTab;
			state.filters.status = "";
			state.start = 0;
			syncFilterInputsFromState();
			loadConversations({reset: true});
		});

		// Search debounce & match type
		els.search.on("input", () => {
			state.filters.search = els.search.val().trim();
			if (state.searchTimer) clearTimeout(state.searchTimer);
			state.searchTimer = setTimeout(() => loadConversations({reset: true}), 250);
		});

		root.find(".match-type-dropdown-menu .dropdown-item").on("click", e => {
			const cond = $(e.currentTarget).attr("data-match-type") === "=" ? "=" : "like";
			if (cond === state.filters.searchCondition) return;
			state.filters.searchCondition = cond;
			root.find(".cs-search-match-icon").html(matchIcon(cond));
			root.find(".match-type-dropdown-btn").attr("title", cond === "=" ? __("Equals") : __("Like"));
			if (state.filters.search) loadConversations({reset: true});
		});

		// Filter drawer & clear
		els.filterToggleBtn.on("click", () => {
			state.filtersOpen = !state.filtersOpen;
			els.filterPanel.toggleClass("hidden", !state.filtersOpen);
			els.filterToggleBtn.attr("aria-expanded", state.filtersOpen ? "true" : "false");
			fitHeight();
		});
		els.filterClearBtn.on("click", clearAllFilters);

		// Structured filters
		els.filterPanel.on("change", ".cs-filter-control", e => {
			const field = $(e.currentTarget).attr("data-filter");
			const val = $(e.currentTarget).val() || "";
			if (!field) return;
			state.filters[field] = val;
			if (field === "channel") {
				state.filters.account = "";
				populateFilterSelects();
			}
			state.start = 0;
			syncFilterInputsFromState();
			if (field === "channel" || field === "account") {
				refreshAll({resetList: true});
			} else {
				if (field === "agent") renderAgentWorkload();
				loadConversations({reset: true});
			}
		});

		// Pagination
		els.pagePrev.on("click", () => {
			if (state.start <= 0) return;
			state.start = Math.max(0, state.start - state.limit);
			loadConversations({reset: false});
		});
		els.pageNext.on("click", () => {
			if (!state.hasMore) return;
			state.start += state.limit;
			loadConversations({reset: false});
		});

		// Session selection
		els.list.on("click", ".cs-session-item", e => {
			const name = $(e.currentTarget).attr("data-session");
			if (name) openSession(name, {scrollMode: "bottom"});
		});

		// Timeline pagination & contextual page navigation
		els.timeline.on("click", ".cs-load-more", loadEarlierMessages);
		root.on("click", ".cs-open-inbox-btn", () => {
			if (state.selectedName) openInInbox(state.selectedName);
		});
		root.on("click", ".cs-open-history-btn", () => {
			if (state.selectedName) openInHistory(state.selectedName);
		});

		// Right panel tabs & Reassign trigger
		root.on("click", ".cs-detail-tab-btn", function () {
			const nextTab = $(this).attr("data-detail-tab") || "session";
			if (nextTab === state.detailTab) return;
			state.detailTab = nextTab;
			renderInfoPanel();
		});
		root.on("click", ".cs-reassign-btn", openReassignDialog);

		// Mobile 4-step progressive navigation
		root.on("click", ".cs-mobile-to-list", () => setMobileView("list"));
		root.on("click", ".cs-mobile-back-summary", () => setMobileView("summary"));
		root.on("click", ".cs-back-btn", () => setMobileView("list"));
		root.on("click", ".cs-mobile-details-btn", () => setMobileView("details"));
		root.on("click", ".cs-info-back-btn", () => setMobileView("chat"));

		$(wrapper).on("remove", () => {
			state.pageActive = false;
			unbindRealtime();
			if (state.pollTimer) {
				clearInterval(state.pollTimer);
				state.pollTimer = null;
			}
			if (state.refreshTimer) {
				clearTimeout(state.refreshTimer);
				state.refreshTimer = null;
			}
			if (state.searchTimer) {
				clearTimeout(state.searchTimer);
				state.searchTimer = null;
			}
			$(window).off("resize.conversation_supervision", fitHeight);
		});

		wrapper._csState = state;
		wrapper._csBindLifecycle = async (active) => {
			state.pageActive = active;
			if (active) {
				fitHeight();
				bindRealtime();
				startPolling();
				await refreshAll({resetList: true});
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

	frappe.pages["conversation-supervision"].on_page_show = function (wrapper) {
		if (typeof wrapper._csBindLifecycle === "function") {
			wrapper._csBindLifecycle(true);
		}
	};

	frappe.pages["conversation-supervision"].on_page_hide = function (wrapper) {
		if (typeof wrapper._csBindLifecycle === "function") {
			wrapper._csBindLifecycle(false);
		}
	};
}
