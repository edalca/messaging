"""System Manager supervision read-only projections and authorized reassignment."""
import json

import frappe
from frappe import _
from frappe.utils import cint, get_datetime

from messaging.common.primitives import utcnow
from messaging.conversation.doctype.chat_session import human, inbox
from messaging.security.permissions import require_system_manager

SUPERVISION_TABS = ("all", "active", "queued")
SUPERVISION_STATUSES = ("Human Active", "Human Queued")
SUPERVISION_ORDERS = {
	"last_activity": "last_activity desc, name desc",
	"longest_wait": "human_requested_at asc, name desc",
	"oldest_attention": "assigned_at asc, name desc",
}
AVAILABILITY_FILTERS = ("available", "unavailable", "disabled", "full")


def _validate_choice(value, allowed, *, allow_empty=True):
	if value in (None, "", "all", "All") and allow_empty:
		return ""
	if not isinstance(value, str) or value not in allowed:
		frappe.throw(_("Invalid conversation filter."), frappe.ValidationError)
	return value


def _validate_optional_str(value):
	if value in (None, ""):
		return ""
	if not isinstance(value, str):
		frappe.throw(_("Invalid conversation filter."), frappe.ValidationError)
	return value.strip()


def _seconds_between(later_dt, earlier_dt):
	if not later_dt or not earlier_dt:
		return None
	later = inbox._to_utc_naive(later_dt)
	earlier = inbox._to_utc_naive(earlier_dt)
	if not later or not earlier:
		return None
	return max(0, int((later - earlier).total_seconds()))


def _public_id_map(accounts_map):
	account_names = list(accounts_map.keys())
	vals_by_acc = {}
	if account_names:
		for val in frappe.get_all(
			"Communication Provider Value",
			filters={
				"parent": ["in", account_names],
				"parenttype": "Communication Channel Account",
				"parameter_key": ["in", list(inbox.PUBLIC_ACCOUNT_KEYS)],
			},
			fields=["parent", "parameter_key", "value"],
		):
			vals_by_acc.setdefault(val["parent"], []).append(val)
	return {
		acc_name: inbox._extract_public_id_from_values(
			vals_by_acc.get(acc_name),
			(accounts_map.get(acc_name) or {}).get("account_id") or acc_name,
		)
		for acc_name in account_names
	}


def _load_channels_and_accounts(channel=None):
	channels = frappe.get_all(
		"Communication Channel",
		filters={"status": "Enabled"},
		fields=["name", "channel_name", "provider"],
		order_by="channel_name asc",
	)
	acc_filters = {"enabled": 1}
	if channel:
		acc_filters["channel"] = channel
	accounts = frappe.get_all(
		"Communication Channel Account",
		filters=acc_filters,
		fields=["name", "channel", "account_id", "account_name"],
		order_by="account_name asc",
	)
	public_ids = _public_id_map({acc["name"]: acc for acc in accounts})
	for acc in accounts:
		acc["public_id"] = public_ids.get(acc["name"], acc.get("account_id") or acc["name"])
	return channels, accounts


def _occupied_sessions_map(channel=None, account=None, agent=None):
	conditions = ["status IN ('Human Active', 'Human Paused')", "IFNULL(assigned_agent, '') != ''"]
	params = []
	if channel:
		conditions.append("channel = %s")
		params.append(channel)
	if account:
		conditions.append("account = %s")
		params.append(account)
	if agent:
		conditions.append("assigned_agent = %s")
		params.append(agent)
	where_sql = " AND ".join(conditions)
	rows = frappe.db.sql(
		f"""
		SELECT name, channel, account, assigned_agent, status
		FROM `tabChat Session`
		WHERE {where_sql}
		""",
		tuple(params),
		as_dict=True,
	)
	sessions_map = {}
	for r in rows:
		key = (r["channel"], r["account"], r["assigned_agent"])
		bucket = sessions_map.setdefault(
			key, {"active_ids": set(), "paused_ids": set(), "occupied_ids": set()}
		)
		bucket["occupied_ids"].add(r["name"])
		if r["status"] == "Human Active":
			bucket["active_ids"].add(r["name"])
		elif r["status"] == "Human Paused":
			bucket["paused_ids"].add(r["name"])
	return sessions_map


def _occupied_counts_map(channel=None, account=None, agent=None):
	sessions_map = _occupied_sessions_map(channel=channel, account=account, agent=agent)
	return {
		key: {
			"active_count": len(bucket["active_ids"]),
			"paused_count": len(bucket["paused_ids"]),
			"occupied": len(bucket["occupied_ids"]),
		}
		for key, bucket in sessions_map.items()
	}


def evaluate_grant_status(
	*,
	user_exists,
	user_enabled,
	has_role,
	channel_exists,
	channel_enabled,
	account_exists,
	account_enabled,
	grant_enabled,
	available,
	capacity,
	occupied,
):
	"""Shared single source of truth for per-grant eligibility, status_code, and reason_code."""
	user_eligible = bool(user_exists and user_enabled and has_role)
	scope_operational = bool(channel_exists and channel_enabled and account_exists and account_enabled)
	effective_enabled = bool(user_eligible and scope_operational and grant_enabled)
	cap = max(1, cint(capacity or 1))
	occ = max(0, cint(occupied or 0))
	remaining = max(0, cap - occ) if effective_enabled else 0
	can_receive = bool(effective_enabled and available and occ < cap)

	if not channel_exists:
		reason_code = "channel_missing"
	elif not account_exists:
		reason_code = "account_missing"
	elif not user_exists or not user_enabled:
		reason_code = "user_disabled"
	elif not has_role:
		reason_code = "missing_role"
	elif not channel_enabled:
		reason_code = "channel_disabled"
	elif not account_enabled:
		reason_code = "account_disabled"
	elif not grant_enabled:
		reason_code = "grant_disabled"
	elif not available:
		reason_code = "unavailable"
	elif occ >= cap:
		reason_code = "full"
	else:
		reason_code = "available"

	if not effective_enabled:
		status_code = "disabled"
	elif not available:
		status_code = "unavailable"
	elif occ >= cap:
		status_code = "full"
	else:
		status_code = "available"

	return {
		"user_eligible": user_eligible,
		"scope_operational": scope_operational,
		"effective_enabled": effective_enabled,
		"capacity": cap,
		"occupied": occ,
		"remaining": remaining,
		"can_receive": can_receive,
		"status_code": status_code,
		"reason_code": reason_code,
	}


def _build_workload_bundle(*, channel="", account="", agent="", availability=""):
	"""Build per-(user, channel, account) grant rows plus unique-session tracking."""
	grant_filters = {}
	if channel:
		grant_filters["channel"] = channel
	if account:
		grant_filters["account"] = account
	if agent:
		grant_filters["user"] = agent

	grants = frappe.get_all(
		"Chat Agent",
		filters=grant_filters,
		fields=["name", "user", "channel", "account", "enabled", "available", "capacity"],
		order_by="name asc",
		limit_page_length=500,
	)
	if not grants:
		return [], {}

	user_ids = sorted({g["user"] for g in grants if g.get("user")})
	channel_ids = sorted({g["channel"] for g in grants if g.get("channel")})
	account_ids = sorted({g["account"] for g in grants if g.get("account")})

	users_map = {
		u["name"]: u
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", user_ids]},
			fields=["name", "full_name", "enabled"],
		)
	} if user_ids else {}

	roles_by_user = {
		u: set(frappe.get_roles(u))
		for u in user_ids
		if u in users_map
	}

	channels_map = {
		c["name"]: c
		for c in frappe.get_all(
			"Communication Channel",
			filters={"name": ["in", channel_ids]},
			fields=["name", "channel_name", "provider", "status"],
		)
	} if channel_ids else {}

	accounts_map = {
		a["name"]: a
		for a in frappe.get_all(
			"Communication Channel Account",
			filters={"name": ["in", account_ids]},
			fields=["name", "channel", "account_id", "account_name", "enabled"],
		)
	} if account_ids else {}

	public_id_map = _public_id_map(accounts_map)
	sessions_map = _occupied_sessions_map(
		channel=channel or None, account=account or None, agent=agent or None
	)

	rows = []
	for grant in grants:
		user = grant.get("user") or ""
		ch = grant.get("channel") or ""
		acc = grant.get("account") or ""
		user_doc = users_map.get(user)
		user_exists = bool(user_doc)
		user_enabled = bool(user_doc and user_doc.get("enabled"))
		has_role = bool(user_exists and "Messaging Agent" in roles_by_user.get(user, ()))

		ch_doc = channels_map.get(ch)
		channel_exists = bool(ch_doc)
		channel_enabled = bool(ch_doc and ch_doc.get("status") == "Enabled")

		acc_doc = accounts_map.get(acc)
		account_exists = bool(acc_doc and acc_doc.get("channel") == ch)
		account_enabled = bool(account_exists and acc_doc.get("enabled"))

		grant_enabled = bool(grant.get("enabled"))
		available_flag = bool(grant.get("available"))
		session_bucket = sessions_map.get((ch, acc, user), {})
		occupied = len(session_bucket.get("occupied_ids") or ())
		active_count = len(session_bucket.get("active_ids") or ())
		paused_count = len(session_bucket.get("paused_ids") or ())

		eval_info = evaluate_grant_status(
			user_exists=user_exists,
			user_enabled=user_enabled,
			has_role=has_role,
			channel_exists=channel_exists,
			channel_enabled=channel_enabled,
			account_exists=account_exists,
			account_enabled=account_enabled,
			grant_enabled=grant_enabled,
			available=available_flag,
			capacity=grant.get("capacity"),
			occupied=occupied,
		)

		if availability and eval_info["status_code"] != availability:
			continue

		public_id = public_id_map.get(acc, (acc_doc or {}).get("account_id") or acc) if account_exists else acc
		account_name = (
			(acc_doc.get("account_name") or public_id or acc_doc.get("account_id") or acc)
			if account_exists
			else acc
		)
		rows.append({
			"grant_name": grant["name"],
			"has_grant": True,
			"user": user,
			"full_name": (user_doc or {}).get("full_name") or user,
			"channel": ch,
			"channel_name": (ch_doc or {}).get("channel_name") or ch,
			"account": acc,
			"account_name": account_name,
			"account_public_id": public_id,
			"channel_exists": channel_exists,
			"channel_enabled": channel_enabled,
			"account_exists": account_exists,
			"account_enabled": account_enabled,
			"scope_operational": eval_info["scope_operational"],
			"grant_enabled": grant_enabled,
			"user_enabled": user_enabled,
			"has_role": has_role,
			"effective_enabled": eval_info["effective_enabled"],
			"available": available_flag,
			"capacity": eval_info["capacity"],
			"occupied": eval_info["occupied"],
			"active_count": active_count,
			"paused_count": paused_count,
			"remaining": eval_info["remaining"],
			"can_receive": eval_info["can_receive"],
			"status_code": eval_info["status_code"],
			"reason_code": eval_info["reason_code"],
		})

	status_rank = {"available": 0, "full": 1, "unavailable": 2, "disabled": 3}
	rows.sort(
		key=lambda r: (
			(r["full_name"] or r["user"]).lower(),
			r["user"].lower(),
			status_rank.get(r["status_code"], 9),
			(r["channel_name"] or r["channel"]).lower(),
			(r["account_name"] or r["account"]).lower(),
			r["account"].lower(),
		)
	)
	return rows, sessions_map


def consolidate_agent_workload(grants, *, sessions_map=None):
	"""Group per-grant rows by persistent Frappe User ID without summing incompatible capacities."""
	if not grants:
		return []

	grouped = {}
	for grant in grants:
		user = grant.get("user") or ""
		if not user:
			continue
		grouped.setdefault(user, []).append(grant)

	status_rank = {"available": 0, "full": 1, "unavailable": 2, "disabled": 3}
	reason_rank = {"user_disabled": 0, "missing_role": 1, "grant_disabled": 2}
	non_op_reasons = {"channel_missing", "account_missing", "channel_disabled", "account_disabled"}

	users_out = []
	for user, user_grants in grouped.items():
		ordered_grants = sorted(
			user_grants,
			key=lambda g: (
				status_rank.get(g.get("status_code"), 9),
				(g.get("channel_name") or g.get("channel") or "").lower(),
				(g.get("account_name") or g.get("account") or "").lower(),
				(g.get("account") or "").lower(),
			),
		)
		first = ordered_grants[0]
		account_count = len(ordered_grants)
		operational_account_count = sum(1 for g in ordered_grants if g.get("effective_enabled"))
		available_account_count = sum(1 for g in ordered_grants if g.get("can_receive"))
		full_account_count = sum(1 for g in ordered_grants if g.get("status_code") == "full")
		unavailable_account_count = sum(1 for g in ordered_grants if g.get("status_code") == "unavailable")
		non_operational_account_count = account_count - operational_account_count

		if sessions_map is not None:
			active_ids = set()
			occupied_ids = set()
			for g in ordered_grants:
				bucket = sessions_map.get((g.get("channel"), g.get("account"), user)) or {}
				active_ids.update(bucket.get("active_ids") or ())
				occupied_ids.update(bucket.get("occupied_ids") or ())
			active_conversations = len(active_ids)
			occupied_conversations = len(occupied_ids)
		else:
			seen_scopes = {}
			for g in ordered_grants:
				seen_scopes[(g.get("channel"), g.get("account"))] = g
			active_conversations = sum(cint(g.get("active_count", 0)) for g in seen_scopes.values())
			occupied_conversations = sum(cint(g.get("occupied", 0)) for g in seen_scopes.values())

		can_receive = available_account_count > 0
		if can_receive:
			user_status_code = "available"
			user_reason_code = "available"
		elif full_account_count > 0:
			user_status_code = "full"
			user_reason_code = "full"
		elif unavailable_account_count > 0:
			user_status_code = "unavailable"
			user_reason_code = "unavailable"
		else:
			enabling_reasons = [
				g.get("reason_code")
				for g in ordered_grants
				if g.get("reason_code") in reason_rank
			]
			if enabling_reasons:
				user_status_code = "not_enabled"
				user_reason_code = min(enabling_reasons, key=lambda code: reason_rank.get(code, 9))
			elif all(g.get("reason_code") in non_op_reasons for g in ordered_grants):
				user_status_code = "no_operational_accounts"
				user_reason_code = "no_operational_accounts"
			else:
				user_status_code = "not_enabled"
				user_reason_code = ordered_grants[0].get("reason_code") or "grant_disabled"

		users_out.append({
			"user": user,
			"full_name": first.get("full_name") or user,
			"user_enabled": bool(first.get("user_enabled")),
			"has_role": bool(first.get("has_role")),
			"account_count": account_count,
			"operational_account_count": operational_account_count,
			"available_account_count": available_account_count,
			"full_account_count": full_account_count,
			"unavailable_account_count": unavailable_account_count,
			"non_operational_account_count": non_operational_account_count,
			"active_conversations": active_conversations,
			"occupied_conversations": occupied_conversations,
			"can_receive": can_receive,
			"status_code": user_status_code,
			"reason_code": user_reason_code,
			"grants": ordered_grants,
		})

	users_out.sort(key=lambda u: ((u["full_name"] or u["user"]).lower(), u["user"].lower()))
	return users_out


def get_agent_workload(*, channel="", account="", agent="", availability=""):
	"""Build per-(user, channel, account) agent workload rows without summing across scopes."""
	rows, _sessions_map = _build_workload_bundle(
		channel=channel, account=account, agent=agent, availability=availability
	)
	return rows


def get_consolidated_agent_workload(*, channel="", account="", agent="", availability=""):
	"""Build consolidated per-user agent workload cards with nested per-account grants."""
	rows, sessions_map = _build_workload_bundle(
		channel=channel, account=account, agent=agent, availability=availability
	)
	return consolidate_agent_workload(rows, sessions_map=sessions_map)


def _matching_scope_pairs_for_availability(workload, availability):
	if not availability:
		return None
	return [(r["channel"], r["account"], r["user"]) for r in workload]


def _build_session_where_sql(
	*,
	tab="all",
	status="",
	channel="",
	account="",
	agent="",
	availability="",
	workload=None,
	search="",
	search_condition="like",
	can_view_sender=True,
	can_view_profile=True,
):
	clauses = ["s.human_requested_at IS NOT NULL"]
	params = []

	if tab == "active":
		if status == "Human Queued":
			return None, []
		clauses.append("s.status = 'Human Active' AND IFNULL(s.assigned_agent, '') != ''")
	elif tab == "queued":
		if status == "Human Active":
			return None, []
		clauses.append("s.status = 'Human Queued' AND IFNULL(s.assigned_agent, '') = ''")
	else:
		if status == "Human Active":
			clauses.append("s.status = 'Human Active' AND IFNULL(s.assigned_agent, '') != ''")
		elif status == "Human Queued":
			clauses.append("s.status = 'Human Queued' AND IFNULL(s.assigned_agent, '') = ''")
		else:
			clauses.append(
				"((s.status = 'Human Active' AND IFNULL(s.assigned_agent, '') != '') "
				"OR (s.status = 'Human Queued' AND IFNULL(s.assigned_agent, '') = ''))"
			)

	if channel:
		clauses.append("s.channel = %s")
		params.append(channel)
	if account:
		clauses.append("s.account = %s")
		params.append(account)
	if agent:
		clauses.append("s.assigned_agent = %s")
		params.append(agent)

	if availability:
		if workload is None:
			workload = get_agent_workload(
				channel=channel, account=account, agent=agent, availability=availability
			)
		pairs = _matching_scope_pairs_for_availability(workload, availability)
		if not pairs:
			return None, []
		pair_sql = " OR ".join(["(s.channel = %s AND s.account = %s AND s.assigned_agent = %s)"] * len(pairs))
		clauses.append(f"({pair_sql})")
		for ch_val, acc_val, user_val in pairs:
			params.extend([ch_val, acc_val, user_val])

	if search:
		exact = search_condition == "="
		search_clauses = []
		if exact:
			search_clauses.append("s.name = %s")
			params.append(search)
			search_clauses.append("s.assigned_agent = %s")
			params.append(search)
			matching_users = frappe.get_all(
				"User",
				or_filters=[["name", "=", search], ["full_name", "=", search]],
				pluck="name",
				limit_page_length=200,
			)
			if matching_users:
				placeholders = ", ".join(["%s"] * len(matching_users))
				search_clauses.append(f"s.assigned_agent IN ({placeholders})")
				params.extend(matching_users)

			contact_or = [["name", "=", search]]
			if can_view_sender:
				contact_or.append(["external_sender", "=", search])
			if can_view_profile and not any(ch in search for ch in "%_\\\"\''"):
				contact_or.append(["parameter_values", "like", f'%"{search}"%'])
			matching_contacts = frappe.get_all(
				"Chat Contact",
				or_filters=contact_or,
				pluck="name",
				limit_page_length=500,
			)
			if matching_contacts:
				placeholders = ", ".join(["%s"] * len(matching_contacts))
				search_clauses.append(f"s.contact IN ({placeholders})")
				params.extend(matching_contacts)
		else:
			escaped = inbox._escape_like(search)
			like_term = f"%{escaped}%"
			search_clauses.append("s.name LIKE %s")
			params.append(like_term)
			search_clauses.append("s.assigned_agent LIKE %s")
			params.append(like_term)
			matching_users = frappe.get_all(
				"User",
				or_filters=[["name", "like", like_term], ["full_name", "like", like_term]],
				pluck="name",
				limit_page_length=200,
			)
			if matching_users:
				placeholders = ", ".join(["%s"] * len(matching_users))
				search_clauses.append(f"s.assigned_agent IN ({placeholders})")
				params.extend(matching_users)

			contact_or = [["name", "like", like_term]]
			if can_view_sender:
				contact_or.append(["external_sender", "like", like_term])
			if can_view_profile:
				contact_or.append(["parameter_values", "like", like_term])
			matching_contacts = frappe.get_all(
				"Chat Contact",
				or_filters=contact_or,
				pluck="name",
				limit_page_length=500,
			)
			if matching_contacts:
				placeholders = ", ".join(["%s"] * len(matching_contacts))
				search_clauses.append(f"s.contact IN ({placeholders})")
				params.extend(matching_contacts)

		clauses.append(f"({' OR '.join(search_clauses)})")

	return " AND ".join(clauses), params


def _enrich_supervision_sessions(rows):
	"""Batch-load related metadata, unread counts, and message previews with zero N+1 queries."""
	if not rows:
		return []

	session_names = [r["name"] for r in rows]
	contact_names = list({r["contact"] for r in rows if r.get("contact")})
	channel_names = list({r["channel"] for r in rows if r.get("channel")})
	account_names = list({r["account"] for r in rows if r.get("account")})
	user_ids = list({u for r in rows for u in (r.get("assigned_agent"), r.get("closed_by")) if u})

	contacts_map = {
		c["name"]: c
		for c in frappe.get_all(
			"Chat Contact",
			filters={"name": ["in", contact_names]},
			fields=["name", "external_sender", "parameter_values"],
		)
	} if contact_names else {}

	channels_map = {
		ch["name"]: ch
		for ch in frappe.get_all(
			"Communication Channel",
			filters={"name": ["in", channel_names]},
			fields=["name", "channel_name", "provider"],
		)
	} if channel_names else {}

	accounts_map = {
		acc["name"]: acc
		for acc in frappe.get_all(
			"Communication Channel Account",
			filters={"name": ["in", account_names]},
			fields=["name", "account_id", "account_name"],
		)
	} if account_names else {}

	public_id_map = _public_id_map(accounts_map)

	user_names = {
		u["name"]: u["full_name"] or u["name"]
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", user_ids]},
			fields=["name", "full_name"],
		)
	} if user_ids else {}

	first_msg_map = {}
	last_msg_map = {}
	edge_rows = frappe.db.sql(
		"""
		SELECT m.session, m.sequence, m.kind, m.content, m.sent_at,
		       b.min_seq, b.max_seq
		FROM (
			SELECT session, MIN(sequence) AS min_seq, MAX(sequence) AS max_seq
			FROM `tabChat Message`
			WHERE session IN %s
			GROUP BY session
		) b
		INNER JOIN `tabChat Message` m
			ON m.session = b.session AND m.sequence IN (b.min_seq, b.max_seq)
		""",
		(tuple(session_names),),
		as_dict=True,
	)
	for msg in edge_rows:
		if msg["sequence"] == msg["min_seq"]:
			first_msg_map[msg["session"]] = msg["sent_at"]
		if msg["sequence"] == msg["max_seq"]:
			last_msg_map[msg["session"]] = msg

	unread_rows = frappe.db.sql(
		"""
		SELECT m.session, COUNT(*) AS unread
		FROM `tabChat Message` m
		INNER JOIN `tabChat Session` s ON s.name = m.session
		WHERE m.session IN %s
		  AND m.kind = 'Incoming'
		  AND m.sequence > IFNULL(s.read_sequence, 0)
		GROUP BY m.session
		""",
		(tuple(session_names),),
		as_dict=True,
	)
	unread_map = {r["session"]: cint(r["unread"]) for r in unread_rows}

	now_utc = utcnow()
	enriched = []
	for row in rows:
		item = inbox._build_public_session(
			row,
			contact_data=contacts_map.get(row.get("contact")),
			channel_info=channels_map.get(row.get("channel")),
			account_info=accounts_map.get(row.get("account")),
			account_public_id=public_id_map.get(row.get("account"), ""),
			last_msg=last_msg_map.get(row["name"]),
			first_msg_sent_at=first_msg_map.get(row["name"]),
			user_names=user_names,
			unread=unread_map.get(row["name"], 0),
			historical=False,
		)
		item.update(_supervision_timings(row, now_utc=now_utc))
		item["can_reassign"] = bool(row.get("status") == "Human Active" and row.get("assigned_agent"))
		enriched.append(item)
	return enriched


def _supervision_timings(row, *, now_utc=None):
	now_dt = now_utc or utcnow()
	status = row.get("status") or ""
	req_at = row.get("human_requested_at")
	assigned_at = row.get("assigned_at")
	last_activity = row.get("last_activity")

	if status == "Human Queued":
		wait_secs = _seconds_between(now_dt, req_at)
	elif assigned_at and req_at:
		wait_secs = _seconds_between(assigned_at, req_at)
	else:
		wait_secs = None

	if status == "Human Active" and assigned_at:
		attention_secs = _seconds_between(now_dt, assigned_at)
	elif status in inbox.TERMINAL_STATES and assigned_at and row.get("closed_at"):
		attention_secs = _seconds_between(row.get("closed_at"), assigned_at)
	else:
		attention_secs = None

	inactivity_secs = _seconds_between(now_dt, last_activity)
	return {
		"wait_time_seconds": wait_secs if wait_secs is not None else 0,
		"supervision_wait_seconds": wait_secs,
		"attention_elapsed_seconds": attention_secs,
		"inactivity_seconds": inactivity_secs,
	}


def get_summary(*, channel="", account="", agent="", availability="", workload=None, agent_users=None):
	if agent_users is None:
		if workload is None:
			workload, sessions_map = _build_workload_bundle(
				channel=channel, account=account, agent=agent, availability=availability
			)
			agent_users = consolidate_agent_workload(workload, sessions_map=sessions_map)
		else:
			agent_users = consolidate_agent_workload(workload)
	elif workload is None:
		workload = [grant for u in agent_users for grant in (u.get("grants") or ())]

	active_where, active_params = _build_session_where_sql(
		tab="active",
		channel=channel,
		account=account,
		agent=agent,
		availability=availability,
		workload=workload,
	)
	if active_where:
		in_attention = cint(
			frappe.db.sql(
				f"SELECT COUNT(*) FROM `tabChat Session` s WHERE {active_where}",
				tuple(active_params),
			)[0][0]
		)
	else:
		in_attention = 0

	if agent or availability:
		waiting = 0
		avg_wait_seconds = None
	else:
		q_where, q_params = _build_session_where_sql(
			tab="queued",
			channel=channel,
			account=account,
		)
		queued_rows = frappe.db.sql(
			f"SELECT s.human_requested_at FROM `tabChat Session` s WHERE {q_where}",
			tuple(q_params),
			as_dict=True,
		)
		waiting = len(queued_rows)
		now_utc = utcnow()
		waits = [
			secs
			for r in queued_rows
			if (secs := _seconds_between(now_utc, r.get("human_requested_at"))) is not None
		]
		avg_wait_seconds = round(sum(waits) / len(waits)) if waits else None

	available_agents = sum(1 for u in agent_users if u.get("can_receive"))

	return {
		"in_attention": in_attention,
		"waiting": waiting,
		"available_agents": available_agents,
		"avg_wait_seconds": avg_wait_seconds,
	}


@frappe.whitelist()
def summary(channel=None, account=None, agent=None, availability=None):
	"""Return System Manager supervision KPIs computed directly from backend records."""
	require_system_manager()
	ch = _validate_optional_str(channel)
	acc = _validate_optional_str(account)
	ag = _validate_optional_str(agent)
	avail = _validate_choice(availability, AVAILABILITY_FILTERS)
	return get_summary(channel=ch, account=acc, agent=ag, availability=avail)


@frappe.whitelist()
def agent_workload(channel=None, account=None, agent=None, availability=None):
	"""Return per-scope (user, channel, account) agent occupancy and availability."""
	require_system_manager()
	ch = _validate_optional_str(channel)
	acc = _validate_optional_str(account)
	ag = _validate_optional_str(agent)
	avail = _validate_choice(availability, AVAILABILITY_FILTERS)
	return get_agent_workload(channel=ch, account=acc, agent=ag, availability=avail)


@frappe.whitelist()
def overview(channel=None, account=None, agent=None, availability=None):
	"""Combined System Manager snapshot of KPIs, consolidated agent workload, and filter metadata."""
	require_system_manager()
	ch = _validate_optional_str(channel)
	acc = _validate_optional_str(account)
	ag = _validate_optional_str(agent)
	avail = _validate_choice(availability, AVAILABILITY_FILTERS)

	workload, sessions_map = _build_workload_bundle(
		channel=ch, account=acc, agent=ag, availability=avail
	)
	agent_users_workload = consolidate_agent_workload(workload, sessions_map=sessions_map)
	summary_data = get_summary(
		channel=ch,
		account=acc,
		agent=ag,
		availability=avail,
		workload=workload,
		agent_users=agent_users_workload,
	)
	channels, accounts = _load_channels_and_accounts(channel=ch or None)

	all_workload = (
		get_agent_workload(channel=ch, account=acc)
		if (ag or avail)
		else workload
	)
	agent_users = {r["user"]: r["full_name"] for r in all_workload}
	active_assigned = frappe.get_all(
		"Chat Session",
		filters={"status": "Human Active", "assigned_agent": ["is", "set"]},
		pluck="assigned_agent",
		limit_page_length=200,
	)
	missing_users = sorted({u for u in active_assigned if u and u not in agent_users})
	if missing_users:
		for u in frappe.get_all(
			"User",
			filters={"name": ["in", missing_users]},
			fields=["name", "full_name"],
		):
			agent_users[u["name"]] = u["full_name"] or u["name"]
	filter_agents = sorted(
		[{"user": u, "full_name": name} for u, name in agent_users.items()],
		key=lambda item: ((item["full_name"] or item["user"]).lower(), item["user"].lower()),
	)

	return {
		"summary": summary_data,
		"workload": workload,
		"agent_users": agent_users_workload,
		"channels": channels,
		"accounts": accounts,
		"agents": filter_agents,
	}


@frappe.whitelist()
def conversations(
	tab="all",
	status=None,
	channel=None,
	account=None,
	agent=None,
	availability=None,
	search=None,
	search_condition="like",
	order_by="last_activity",
	start=0,
	limit=30,
):
	"""Server-paginated, read-only supervision list for Human Active and Human Queued sessions."""
	require_system_manager()

	tab_val = _validate_choice(tab or "all", SUPERVISION_TABS, allow_empty=False)
	status_val = _validate_choice(status, SUPERVISION_STATUSES)
	ch = _validate_optional_str(channel)
	acc = _validate_optional_str(account)
	ag = _validate_optional_str(agent)
	avail = _validate_choice(availability, AVAILABILITY_FILTERS)
	search_val = _validate_optional_str(search)
	cond_val = _validate_choice(search_condition or "like", ("like", "="), allow_empty=False)
	order_key = _validate_choice(order_by or "last_activity", tuple(SUPERVISION_ORDERS.keys()), allow_empty=False)

	effective_active = tab_val == "active" or (tab_val == "all" and status_val == "Human Active")
	effective_queued = tab_val == "queued" or (tab_val == "all" and status_val == "Human Queued")

	if order_key == "longest_wait" and (not effective_queued or effective_active):
		frappe.throw(
			_("Longest wait ordering is only available for queued conversations."),
			frappe.ValidationError,
		)
	if order_key == "oldest_attention" and (not effective_active or effective_queued):
		frappe.throw(
			_("Oldest attention ordering is only available for active conversations."),
			frappe.ValidationError,
		)

	contact_probe = frappe.new_doc("Chat Contact")
	can_view_sender = frappe.session.user == "Administrator" or contact_probe.has_permlevel_access_to("external_sender")
	can_view_profile = frappe.session.user == "Administrator" or contact_probe.has_permlevel_access_to("parameter_values")

	where_sql, params = _build_session_where_sql(
		tab=tab_val,
		status=status_val,
		channel=ch,
		account=acc,
		agent=ag,
		availability=avail,
		search=search_val,
		search_condition=cond_val,
		can_view_sender=can_view_sender,
		can_view_profile=can_view_profile,
	)

	start_idx = max(0, cint(start))
	limit_n = inbox.page_size(limit)

	if not where_sql:
		return {
			"items": [],
			"total": 0,
			"start": start_idx,
			"limit": limit_n,
			"has_more": False,
		}

	total = cint(
		frappe.db.sql(
			f"SELECT COUNT(*) FROM `tabChat Session` s WHERE {where_sql}",
			tuple(params),
		)[0][0]
	)

	order_sql = SUPERVISION_ORDERS[order_key]
	select_cols = ", ".join(f"s.{col}" for col in inbox.SESSION_FIELDS)
	rows = frappe.db.sql(
		f"""
		SELECT {select_cols}
		FROM `tabChat Session` s
		WHERE {where_sql}
		ORDER BY {order_sql}
		LIMIT %s OFFSET %s
		""",
		tuple([*params, limit_n, start_idx]),
		as_dict=True,
	)

	items = _enrich_supervision_sessions(rows)
	return {
		"items": items,
		"total": total,
		"start": start_idx,
		"limit": limit_n,
		"has_more": (start_idx + len(items)) < total,
	}


@frappe.whitelist()
def detail(session_name, before=None, limit=60):
	"""Strict read-only supervision detail for an open or recently closed human session."""
	require_system_manager()
	name = _validate_optional_str(session_name)
	if not name or not frappe.db.exists("Chat Session", name):
		frappe.throw(_("This conversation is not available for supervision."), frappe.PermissionError)

	session_doc = frappe.get_doc("Chat Session", name)
	if not session_doc.human_requested_at:
		frappe.throw(_("This conversation is not available for supervision."), frappe.PermissionError)

	data = inbox.history(session_name=name, before=before, limit=limit, mine_only=0, historical=0)
	data["can_read"] = False

	contact_lang = frappe.db.get_value("Chat Contact", session_doc.contact, "preferred_language") or ""
	if data.get("contact"):
		data["contact"]["preferred_language"] = contact_lang

	session_info = data.get("session") or {}
	session_info.update(
		_supervision_timings(
			{
				"status": session_doc.status,
				"human_requested_at": session_doc.human_requested_at,
				"assigned_at": session_doc.assigned_at,
				"last_activity": session_doc.last_activity,
				"closed_at": session_doc.closed_at,
			}
		)
	)
	session_info["can_reassign"] = bool(
		session_doc.status == "Human Active" and session_doc.assigned_agent
	)
	session_info["is_terminal"] = bool(session_doc.status in inbox.TERMINAL_STATES)
	data["session"] = session_info

	assigned_agent_info = None
	if session_doc.assigned_agent:
		matching = get_agent_workload(
			channel=session_doc.channel,
			account=session_doc.account,
			agent=session_doc.assigned_agent,
		)
		if matching:
			assigned_agent_info = matching[0]
		else:
			user_doc = frappe.db.get_value(
				"User", session_doc.assigned_agent, ["full_name", "enabled"], as_dict=True
			)
			ch_doc = frappe.db.get_value(
				"Communication Channel", session_doc.channel, ["name", "channel_name", "status"], as_dict=True
			)
			acc_doc = frappe.db.get_value(
				"Communication Channel Account",
				session_doc.account,
				["name", "channel", "account_name", "enabled"],
				as_dict=True,
			)
			occ_map = _occupied_counts_map(
				channel=session_doc.channel, account=session_doc.account, agent=session_doc.assigned_agent
			)
			occ_info = occ_map.get((session_doc.channel, session_doc.account, session_doc.assigned_agent), {})
			has_role = bool(user_doc and "Messaging Agent" in frappe.get_roles(session_doc.assigned_agent))
			eval_info = evaluate_grant_status(
				user_exists=bool(user_doc),
				user_enabled=bool(user_doc and user_doc.get("enabled")),
				has_role=has_role,
				channel_exists=bool(ch_doc),
				channel_enabled=bool(ch_doc and ch_doc.get("status") == "Enabled"),
				account_exists=bool(acc_doc and acc_doc.get("channel") == session_doc.channel),
				account_enabled=bool(acc_doc and acc_doc.get("channel") == session_doc.channel and acc_doc.get("enabled")),
				grant_enabled=False,
				available=False,
				capacity=0,
				occupied=cint(occ_info.get("occupied", 0)),
			)
			assigned_agent_info = {
				"user": session_doc.assigned_agent,
				"full_name": (user_doc or {}).get("full_name") or session_doc.assigned_agent,
				"channel": session_doc.channel,
				"channel_name": (data.get("provenance", {}).get("channel") or {}).get("channel_name") or session_doc.channel,
				"account": session_doc.account,
				"account_name": (data.get("provenance", {}).get("account") or {}).get("account_name") or session_doc.account,
				"has_grant": False,
				"grant_enabled": False,
				"user_enabled": bool(user_doc and user_doc.get("enabled")),
				"has_role": has_role,
				"effective_enabled": False,
				"available": False,
				"capacity": 0,
				"occupied": cint(occ_info.get("occupied", 0)),
				"remaining": 0,
				"can_receive": False,
				"status_code": eval_info["status_code"],
				"reason_code": eval_info["reason_code"],
			}
	data["assigned_agent_info"] = assigned_agent_info
	return data


@frappe.whitelist()
def reassignment_candidates(session_name):
	"""Return authorized Chat Agent candidates for this session's exact channel/account scope."""
	require_system_manager()
	name = _validate_optional_str(session_name)
	if not name or not frappe.db.exists("Chat Session", name):
		frappe.throw(_("This conversation is not available for supervision."), frappe.PermissionError)

	session_doc = frappe.get_doc("Chat Session", name)
	if not session_doc.human_requested_at:
		frappe.throw(_("This conversation is not available for supervision."), frappe.PermissionError)
	if session_doc.status != "Human Active" or not session_doc.assigned_agent:
		frappe.throw(
			_("Only active assigned conversations can be reassigned."),
			frappe.ValidationError,
		)

	scope_workload = get_agent_workload(
		channel=session_doc.channel,
		account=session_doc.account,
	)
	candidates = [
		{
			"user": row["user"],
			"full_name": row["full_name"],
			"channel": row["channel"],
			"channel_name": row["channel_name"],
			"account": row["account"],
			"account_name": row["account_name"],
			"available": row["available"],
			"occupied": row["occupied"],
			"capacity": row["capacity"],
			"remaining": row["remaining"],
			"eligible": row["can_receive"],
			"status_code": row["status_code"],
		}
		for row in scope_workload
		if row["effective_enabled"] and row["user"] != session_doc.assigned_agent
	]

	assigned_name = frappe.db.get_value("User", session_doc.assigned_agent, "full_name") or session_doc.assigned_agent
	contact_data = frappe.db.get_value(
		"Chat Contact", session_doc.contact, ["external_sender", "parameter_values"], as_dict=True
	) or {}
	contact_probe = frappe.new_doc("Chat Contact")
	can_view_sender = frappe.session.user == "Administrator" or contact_probe.has_permlevel_access_to("external_sender")
	can_view_profile = frappe.session.user == "Administrator" or contact_probe.has_permlevel_access_to("parameter_values")
	params = frappe.parse_json(contact_data.get("parameter_values") or "{}") if can_view_profile else {}
	contact_name = (
		params.get("name")
		or params.get("nombre")
		or (contact_data.get("external_sender") if can_view_sender else "")
		or session_doc.contact
	)
	ch_name = frappe.db.get_value("Communication Channel", session_doc.channel, "channel_name") or session_doc.channel
	acc_name = frappe.db.get_value("Communication Channel Account", session_doc.account, "account_name") or session_doc.account

	return {
		"session": {
			"name": session_doc.name,
			"status": session_doc.status,
			"modified": str(session_doc.modified),
			"contact_name": contact_name,
			"channel": session_doc.channel,
			"channel_name": ch_name,
			"account": session_doc.account,
			"account_name": acc_name,
			"assigned_agent": session_doc.assigned_agent,
			"assigned_agent_name": assigned_name,
		},
		"candidates": candidates,
	}


@frappe.whitelist(methods=["POST"])
def reassign(session_name, new_agent, expected_modified):
	"""Reassign an active human conversation through the official Chat Session human.act contract."""
	require_system_manager()
	name = _validate_optional_str(session_name)
	target_agent = _validate_optional_str(new_agent)
	revision = _validate_optional_str(expected_modified)

	if not name or not frappe.db.exists("Chat Session", name):
		frappe.throw(_("This conversation is not available for supervision."), frappe.PermissionError)
	if not target_agent:
		frappe.throw(_("Select a new agent for reassignment."), frappe.ValidationError)
	if not revision:
		frappe.throw(_("The conversation changed. Refresh before assigning."), frappe.ValidationError)

	session_doc = frappe.get_doc("Chat Session", name)
	if not session_doc.human_requested_at:
		frappe.throw(_("This conversation is not available for supervision."), frappe.PermissionError)
	try:
		revision_dt = get_datetime(revision)
	except (TypeError, ValueError):
		revision_dt = None
	if not revision_dt or revision_dt != get_datetime(session_doc.modified):
		frappe.throw(_("The conversation changed. Refresh before assigning."), frappe.ValidationError)
	if session_doc.status != "Human Active" or not session_doc.assigned_agent:
		frappe.throw(
			_("Only active assigned conversations can be reassigned."),
			frappe.ValidationError,
		)
	if target_agent == session_doc.assigned_agent:
		frappe.throw(
			_("Select a different agent to reassign this conversation."),
			frappe.ValidationError,
		)

	return human.act(
		session_name=name,
		action="Assign",
		agent=target_agent,
		expected_modified=revision,
	)
