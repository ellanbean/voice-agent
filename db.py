"""
Supabase access shared by the agent, the dialer and the ops scripts.

Uses the SERVICE ROLE key (server side only — never ship it to a browser).
Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

All functions are small and synchronous; the agent calls them from async code
via asyncio.to_thread so they never block the audio loop.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


@lru_cache(maxsize=1)
def client() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return create_client(url, key)


def enabled() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ leads
def get_lead(lead_id: str) -> dict | None:
    r = client().table("leads").select("*").eq("id", lead_id).limit(1).execute()
    return r.data[0] if r.data else None


def upsert_lead(row: dict) -> dict:
    """Insert or update by (phone, campaign)."""
    r = client().table("leads").upsert(row, on_conflict="phone,campaign").execute()
    return r.data[0]


def call_now(lead_id: str) -> None:
    """Team lead override: put the lead at the front of the dialer queue regardless of back-off."""
    client().table("leads").update({"status": "new", "next_attempt": _now()}).eq("id", lead_id).execute()


def is_dnc(phone: str) -> bool:
    r = client().table("do_not_call").select("phone").eq("phone", phone).limit(1).execute()
    return bool(r.data)


def add_dnc(phone: str, reason: str, source: str = "agent") -> None:
    client().table("do_not_call").upsert({"phone": phone, "reason": reason, "source": source}).execute()


def due_leads(limit: int, countries: list[str] | None = None) -> list[dict]:
    """Leads the dialer may call now: status new/retry, next_attempt passed, consent recorded."""
    q = (
        client().table("leads").select("*")
        .in_("status", ["new", "retry"])
        .neq("campaign", "sim")                   # simulator leads are never dialed
        .lte("next_attempt", _now())
        .not_.is_("consent_at", "null")
        .order("next_attempt")
        .limit(limit)
    )
    if countries:
        q = q.in_("country", countries)
    return q.execute().data


def mark_calling(lead_id: str) -> None:
    client().table("leads").update({"status": "calling"}).eq("id", lead_id).execute()


def record_attempt(lead_id: str, outcome: dict, retry_hours: int = 24, max_attempts: int = 3) -> None:
    """Translate an agent outcome into the lead's next state."""
    lead = get_lead(lead_id) or {}
    attempts = int(lead.get("attempts") or 0) + 1
    status = outcome.get("status", "unknown")
    update: dict[str, Any] = {"attempts": attempts, "last_outcome": status}

    if status == "callback_booked":
        update["status"] = "callback"
    elif status == "not_interested":
        update["status"] = "not_interested"
    elif status == "converted":
        update["status"] = "converted"
    elif status == "invalid_number":
        update["status"] = "invalid"
    elif status in {"not_connected", "callee_hung_up", "no_answer"}:
        if attempts >= max_attempts:
            update["status"] = "exhausted"
        else:
            update["status"] = "retry"
            update["next_attempt"] = (datetime.now(timezone.utc) + timedelta(hours=retry_hours)).isoformat()
    else:
        # answered but no explicit outcome: treat as done, no more automatic calls
        update["status"] = "contacted"
    client().table("leads").update(update).eq("id", lead_id).execute()


# ------------------------------------------------------------------ calls
def open_call(room: str, lead_id: str | None, phone: str | None) -> None:
    client().table("calls").upsert(
        {"room": room, "lead_id": lead_id, "phone": phone, "started_at": _now()}, on_conflict="room"
    ).execute()


def close_call(room: str, outcome: dict, transcript: dict, duration_s: int | None) -> None:
    client().table("calls").update(
        {
            "status": outcome.get("status"),
            "trunk_id": outcome.get("trunk"),
            "voice": outcome.get("voice"),
            "outcome": outcome,
            "transcript": transcript,
            "duration_s": duration_s,
            "ended_at": _now(),
        }
    ).eq("room", room).execute()


def add_callback(lead_id: str | None, room: str, requested_for: str, note: str = "") -> None:
    call = client().table("calls").select("id").eq("room", room).limit(1).execute().data
    client().table("callbacks").insert(
        {"lead_id": lead_id, "call_id": call[0]["id"] if call else None, "requested_for": requested_for, "note": note}
    ).execute()


def active_call_count(max_age_minutes: int = 30) -> int:
    """Calls with no ended_at, ignoring stale rows from a crashed worker."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    r = (
        client().table("calls").select("id", count="exact")
        .is_("ended_at", "null").gte("started_at", since).execute()
    )
    return r.count or 0


def close_stale_calls(max_age_minutes: int = 120) -> int:
    """Calls still open long after they started (worker crashed, webhook missing) get closed as 'abandoned'."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    r = (
        client().table("calls").update({"ended_at": _now(), "stage": "ended", "status": "abandoned"})
        .is_("ended_at", "null").lt("started_at", since).execute()
    )
    rows = r.data or []
    for c in rows:
        client().table("call_segments").update({"ended_at": _now()}).eq("call_id", c["id"]).is_("ended_at", "null").execute()
    return len(rows)


def reset_stale_calling(max_age_minutes: int = 30) -> int:
    """Leads stuck in 'calling' (worker died mid-call) go back to 'retry'."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    r = (
        client().table("leads").update({"status": "retry"})
        .eq("status", "calling").lt("updated_at", since).execute()
    )
    return len(r.data or [])


# ------------------------------------------------------------------ campaigns / settings
def campaigns() -> list[dict]:
    return client().table("campaigns").select("*").order("name").execute().data


def ensure_campaign(name: str) -> None:
    client().table("campaigns").upsert({"name": name}, on_conflict="name", ignore_duplicates=True).execute()


def set_campaign_paused(name: str, paused: bool) -> None:
    client().table("campaigns").upsert({"name": name, "paused": paused}, on_conflict="name").execute()


def paused_campaigns() -> set[str]:
    r = client().table("campaigns").select("name").eq("paused", True).execute()
    return {row["name"] for row in r.data}


# ------------------------------------------------------------------ read models for the web app
def lead_board(limit: int = 500, campaign: str | None = None, status: str | None = None) -> list[dict]:
    q = client().table("lead_board").select("*").limit(limit)
    if campaign:
        q = q.eq("campaign", campaign)
    if status:
        q = q.eq("status", status)
    return q.execute().data


def recent_calls(limit: int = 200) -> list[dict]:
    return (
        client().table("calls")
        .select("id,room,phone,status,voice,trunk_id,duration_s,started_at,ended_at,lead_id,outcome")
        .order("started_at", desc=True).limit(limit).execute().data
    )


def get_call(call_id: str) -> dict | None:
    r = client().table("calls").select("*").eq("id", call_id).limit(1).execute()
    return r.data[0] if r.data else None


def open_callbacks() -> list[dict]:
    return (
        client().table("callbacks").select("*, leads(name,phone,country), assigned:profiles(email,full_name)")
        .eq("done", False).order("created_at").execute().data
    )


def set_callback_done(callback_id: str, done: bool = True) -> None:
    client().table("callbacks").update({"done": done}).eq("id", callback_id).execute()


def dnc_list(limit: int = 500) -> list[dict]:
    return client().table("do_not_call").select("*").order("created_at", desc=True).limit(limit).execute().data


def remove_dnc(phone: str) -> None:
    client().table("do_not_call").delete().eq("phone", phone).execute()


def status_counts(campaign: str | None = None) -> dict[str, int]:
    q = client().table("leads").select("status")
    if campaign:
        q = q.eq("campaign", campaign)
    counts: dict[str, int] = {}
    for row in q.execute().data:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


# ------------------------------------------------------------------ users & roles (Supabase Auth + profiles)
ROLES = ["admin", "team_lead", "agent_sales", "agent_payments", "pending"]
LANGS = ["en", "hi"]


def sign_up(email: str, password: str, full_name: str, phone: str = "", employee_code: str = "") -> dict:
    """Create the auth user (email confirmation off → usable immediately).
    The DB trigger creates the profile row (first user = admin)."""
    r = client().auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True,
         "user_metadata": {"full_name": full_name, "phone": phone, "employee_code": employee_code}}
    )
    return {"id": r.user.id, "email": r.user.email}


def sign_in(email: str, password: str) -> dict | None:
    """Verify credentials via Supabase Auth; returns profile (id, email, role) or None."""
    from supabase import create_client as _cc
    # Use the anon-safe flow on a throwaway client so the service client keeps its own session untouched.
    anon = _cc(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    try:
        res = anon.auth.sign_in_with_password({"email": email, "password": password})
    except Exception:  # noqa: BLE001 — wrong password / unknown user
        return None
    if not res.user:
        return None
    return get_profile(res.user.id)


def get_profile(user_id: str) -> dict | None:
    r = client().table("profiles").select("*").eq("id", user_id).limit(1).execute()
    return r.data[0] if r.data else None


def profiles() -> list[dict]:
    return client().table("profiles").select("*").order("created_at").execute().data


def set_role(user_id: str, role: str) -> None:
    if role not in ROLES:
        raise ValueError(role)
    client().table("profiles").update({"role": role}).eq("id", user_id).execute()


def delete_user(user_id: str) -> None:
    client().auth.admin.delete_user(user_id)   # cascades to profiles


def assign_callback(callback_id: str, user_id: str | None) -> None:
    client().table("callbacks").update({"assigned_to_id": user_id}).eq("id", callback_id).execute()


def my_callbacks(user_id: str) -> list[dict]:
    return (
        client().table("callbacks").select("*, leads(name,phone,country), assigned:profiles(email,full_name)")
        .eq("done", False).eq("assigned_to_id", user_id).order("created_at").execute().data
    )


def set_language(user_id: str, language: str) -> None:
    if language not in LANGS:
        raise ValueError(language)
    client().table("profiles").update({"language": language}).eq("id", user_id).execute()


# ------------------------------------------------------------------ presence
def set_status(user_id: str, status: str, room: str | None = None) -> None:
    client().table("profiles").update(
        {"status": status, "status_since": _now(), "current_room": room}
    ).eq("id", user_id).execute()


def pick_available_agent(role: str, exclude: list[str] | None = None) -> dict | None:
    """Best available agent of the role: lowest priority number first (team lead
    sets it; 1 = top), then whoever has been waiting longest. No language matching —
    the interpreter bridges customer and agent languages."""
    # team leads and admins who go available take handovers of any kind
    q = client().table("profiles").select("*").in_("role", [role, "team_lead", "admin"]).eq("status", "available")
    if exclude:
        q = q.not_.in_("id", exclude)
    r = q.order("priority").order("status_since").limit(1).execute().data
    return r[0] if r else None


def set_priority(user_id: str, priority: int) -> None:
    client().table("profiles").update({"priority": max(1, min(999, int(priority)))}).eq("id", user_id).execute()


def update_profile_details(user_id: str, full_name: str, phone: str, employee_code: str) -> None:
    client().table("profiles").update({"full_name": full_name, "phone": phone, "employee_code": employee_code}).eq("id", user_id).execute()


def floor() -> list[dict]:
    return client().table("floor").select("*").order("status").execute().data


# ------------------------------------------------------------------ call stages / segments
def set_call_stage(room: str, stage: str, **fields) -> None:
    client().table("calls").update({"stage": stage, **fields}).eq("room", room).execute()


def get_call_by_room(room: str) -> dict | None:
    r = client().table("calls").select("*").eq("room", room).limit(1).execute()
    return r.data[0] if r.data else None


def open_segment(room: str, stage: str, agent_id: str | None) -> str | None:
    call = get_call_by_room(room)
    if not call:
        return None
    r = client().table("call_segments").insert(
        {"call_id": call["id"], "stage": stage, "agent_id": agent_id}
    ).execute()
    return r.data[0]["id"] if r.data else None


def close_segment(room: str, stage: str, agent_id: str | None = None, **wrap) -> None:
    call = get_call_by_room(room)
    if not call:
        return
    q = client().table("call_segments").update({"ended_at": _now(), **wrap}).eq("call_id", call["id"]).eq("stage", stage).is_("ended_at", "null")
    if agent_id:
        q = q.eq("agent_id", agent_id)
    q.execute()


def end_call(room: str, final_status: str | None = None) -> None:
    """Call is over (webhook or agent hang-up): close open segments, stamp ended_at."""
    call = get_call_by_room(room)
    if not call:
        return
    client().table("call_segments").update({"ended_at": _now()}).eq("call_id", call["id"]).is_("ended_at", "null").execute()
    upd = {"ended_at": _now(), "stage": "ended"}
    if final_status:
        upd["status"] = final_status
    if call.get("ended_at") is None:
        client().table("calls").update(upd).eq("room", room).execute()
    # free any agent still attached to this room
    client().table("profiles").update({"status": "wrap_up", "status_since": _now()}).eq("current_room", room).in_("status", ["on_call", "ringing"]).execute()


# ------------------------------------------------------------------ transfers
def create_transfer(room: str, from_stage: str, to_stage: str, to_agent_id: str, summary: str) -> dict:
    call = get_call_by_room(room)
    r = client().table("transfers").insert(
        {"call_id": call["id"] if call else None, "room": room, "from_stage": from_stage,
         "to_stage": to_stage, "to_agent_id": to_agent_id, "summary": summary}
    ).execute()
    return r.data[0]


def get_transfer(transfer_id: str) -> dict | None:
    r = client().table("transfers").select("*").eq("id", transfer_id).limit(1).execute()
    return r.data[0] if r.data else None


def set_transfer_status(transfer_id: str, status: str) -> None:
    client().table("transfers").update({"status": status, "answered_at": _now()}).eq("id", transfer_id).execute()


def ringing_transfer_for(agent_id: str) -> dict | None:
    r = (
        client().table("transfers").select("*, calls(phone,lead_name,language,summary,lead_id)")
        .eq("to_agent_id", agent_id).eq("status", "ringing").order("requested_at", desc=True).limit(1).execute()
    )
    return r.data[0] if r.data else None


def call_context(room: str) -> dict:
    """Everything the human console shows: call, lead, AI transcript so far, segments."""
    call = get_call_by_room(room) or {}
    lead = get_lead(call["lead_id"]) if call.get("lead_id") else None
    segs = client().table("call_segments").select("*, profiles(full_name,email)").eq("call_id", call.get("id", "")).order("started_at").execute().data if call else []
    return {"call": call, "lead": lead, "segments": segs}


def agent_report(days: int = 7) -> list[dict]:
    """Per-agent: segments handled, talk seconds, sales, revenue — last N days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    segs = (
        client().table("call_segments").select("agent_id, stage, started_at, ended_at, disposition, sale_amount, profiles(full_name,email,role)")
        .gte("started_at", since).not_.is_("agent_id", "null").execute().data
    )
    out: dict[str, dict] = {}
    for s in segs:
        a = out.setdefault(s["agent_id"], {"name": (s.get("profiles") or {}).get("full_name") or (s.get("profiles") or {}).get("email"),
                                           "role": (s.get("profiles") or {}).get("role"), "calls": 0, "talk_s": 0, "sales": 0, "revenue": 0.0, "callbacks": 0})
        a["calls"] += 1
        if s.get("started_at") and s.get("ended_at"):
            t0 = datetime.fromisoformat(s["started_at"].replace("Z", "+00:00")); t1 = datetime.fromisoformat(s["ended_at"].replace("Z", "+00:00"))
            a["talk_s"] += max(0, int((t1 - t0).total_seconds()))
        if s.get("disposition") == "sale":
            a["sales"] += 1; a["revenue"] += float(s.get("sale_amount") or 0)
        if s.get("disposition") == "callback":
            a["callbacks"] += 1
    for a in out.values():
        a["avg_handle_s"] = int(a["talk_s"] / a["calls"]) if a["calls"] else 0
    return sorted(out.values(), key=lambda x: -x["calls"])


# ---------------------------------------------------------------- interpreter telemetry
def add_interp_segment(row: dict) -> None:
    """One translated segment with its latency breakdown (written by interpreter.py)."""
    call = get_call_by_room(row["room"])
    data = {k: row.get(k) for k in ("room", "lane", "seg", "src_lang", "dst_lang", "src_text", "dst_text", "mt_backend",
                                      "merged", "mt_ms", "tts_ms", "queued_ms", "total_ms")}
    data["call_id"] = call["id"] if call else None
    client().table("interp_segments").insert(data).execute()


def set_call_language(room: str, lang: str) -> None:
    client().table("calls").update({"language": lang}).eq("room", room).execute()


def set_interp_stats(room: str, stats: dict) -> None:
    client().table("calls").update({"interp_stats": stats}).eq("room", room).execute()


def interp_segments(call_id: str) -> list[dict]:
    return client().table("interp_segments").select("*").eq("call_id", call_id).order("created_at").execute().data


def interp_report(days: int = 7) -> dict:
    """Latency percentiles across every interpreted segment in the window (for the Reports page)."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = client().table("interp_segments").select("lane,total_ms,mt_ms,tts_ms,queued_ms,merged").gte("created_at", since).limit(20000).execute().data
    def pct(xs, p):
        if not xs:
            return None
        s = sorted(xs)
        return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]
    out = {"segments": len(rows), "merged": sum(1 for r in rows if (r.get("merged") or 1) > 1)}
    for key in ("total_ms", "mt_ms", "tts_ms", "queued_ms"):
        xs = [r[key] for r in rows if r.get(key) is not None]
        out[key] = {"p50": pct(xs, 0.5), "p95": pct(xs, 0.95), "max": max(xs) if xs else None}
    for lane in ("c2a", "a2c"):
        xs = [r["total_ms"] for r in rows if r.get("lane") == lane and r.get("total_ms") is not None]
        out[lane] = {"n": len(xs), "p50": pct(xs, 0.5), "p95": pct(xs, 0.95)}
    return out
