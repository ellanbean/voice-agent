"""
Auto-dialer: works through the Supabase `leads` queue during calling hours.

    python dialer.py            # run forever (Heroku `dialer` process)
    python dialer.py --once     # one pass, then exit (cron / manual)
    python dialer.py --dry-run  # show who would be called, call nobody

Rules, in order, for every candidate lead:
  1. status is new/retry, next_attempt has passed, consent_at is set   (db.due_leads)
  2. phone is not on do_not_call
  3. it is inside calling hours in the lead's own timezone (per-country defaults below)
  4. we are under MAX_CONCURRENT_CALLS
  5. we haven't exceeded CALLS_PER_MINUTE (keeps traffic human-shaped for the carrier)
Then: mark the lead 'calling' and create a LiveKit agent dispatch. The agent
reports the outcome; db.record_attempt() decides retry / done / exhausted.

Env: LIVEKIT_URL/KEY/SECRET, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, AGENT_NAME,
     MAX_CONCURRENT_CALLS (default 5), CALLS_PER_MINUTE (default 6),
     CALLING_HOURS (default "09:00-20:00"), CALLING_DAYS (default "1-6" = Mon–Sat),
     DIALER_COUNTRIES (optional, e.g. "GB,DE" to restrict a run)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from livekit import api

import db

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dialer")

AGENT_NAME = os.getenv("AGENT_NAME", "sales-caller")
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_CALLS", "5"))
CALLS_PER_MINUTE = int(os.getenv("CALLS_PER_MINUTE", "6"))
POLL_SECONDS = int(os.getenv("DIALER_POLL_SECONDS", "20"))

# Sensible national defaults for B2C outbound. Tighten per campaign if needed.
COUNTRY_TZ = {
    "GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin", "AT": "Europe/Vienna",
    "CH": "Europe/Zurich", "FR": "Europe/Paris", "BE": "Europe/Brussels", "NL": "Europe/Amsterdam",
    "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "IT": "Europe/Rome", "PL": "Europe/Warsaw",
    "SE": "Europe/Stockholm", "DK": "Europe/Copenhagen", "NO": "Europe/Oslo", "FI": "Europe/Helsinki",
    "IN": "Asia/Kolkata",
}
# Legal/decent windows (local time). UK guidance: 08–21 weekdays, 09–17 Sat; many callers stop at 20:00.
COUNTRY_HOURS = {
    "GB": ("09:00", "20:00"), "DE": ("09:00", "19:00"), "FR": ("10:00", "20:00"), "ES": ("10:00", "20:30"),
    "IT": ("09:00", "20:00"), "NL": ("09:00", "20:00"), "IN": ("10:00", "20:00"),
}
DEFAULT_HOURS = tuple(os.getenv("CALLING_HOURS", "09:00-20:00").split("-"))
DEFAULT_DAYS = os.getenv("CALLING_DAYS", "1-6")  # ISO weekday range, 1=Mon


def _parse_days(spec: str) -> set[int]:
    a, _, b = spec.partition("-")
    return set(range(int(a), int(b or a) + 1))


CALLING_DAYS = _parse_days(DEFAULT_DAYS)


def in_calling_window(lead: dict, now_utc: datetime | None = None) -> tuple[bool, str]:
    country = (lead.get("country") or "").upper()
    tz = lead.get("timezone") or COUNTRY_TZ.get(country, "UTC")
    now = (now_utc or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(tz))
    if now.isoweekday() not in CALLING_DAYS:
        return False, f"{tz}: not a calling day"
    start, end = COUNTRY_HOURS.get(country, DEFAULT_HOURS)
    hhmm = now.strftime("%H:%M")
    ok = start <= hhmm < end
    return ok, f"{tz} {hhmm} window {start}-{end}"


async def dispatch_call(lk: api.LiveKitAPI, lead: dict) -> str:
    room = f"call-{lead['id'][:8]}-{int(time.time())}"
    metadata = {
        "phone": lead["phone"],
        "lead_id": lead["id"],
        "name": lead.get("name") or "",
        "company": lead.get("company") or "",
        "country": lead.get("country") or "",
        "language": lead.get("language") or "",
        "source": lead.get("source") or "a website enquiry",
        "campaign": lead.get("campaign") or "general",
    }
    await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room, metadata=json.dumps(metadata))
    )
    return room


async def one_pass(lk: api.LiveKitAPI, dry_run: bool, minute_budget: list[float]) -> int:
    countries = [c.strip().upper() for c in os.getenv("DIALER_COUNTRIES", "").split(",") if c.strip()] or None
    stale = db.reset_stale_calling()
    if stale:
        log.warning("reset %d lead(s) stuck in 'calling'", stale)
    active = db.active_call_count()
    slots = MAX_CONCURRENT - active
    if slots <= 0:
        log.info("at capacity: %d active calls", active)
        return 0

    paused = db.paused_campaigns()
    candidates = db.due_leads(limit=slots * 4, countries=countries)
    placed = 0
    for lead in candidates:
        if placed >= slots:
            break
        if (lead.get("campaign") or "general") in paused:
            continue  # campaign paused from the web app
        # rate limit: drop timestamps older than 60s, refuse if budget spent
        now = time.time()
        minute_budget[:] = [t for t in minute_budget if now - t < 60]
        if len(minute_budget) >= CALLS_PER_MINUTE:
            log.info("per-minute limit reached (%d/min); waiting", CALLS_PER_MINUTE)
            break

        ok, why = in_calling_window(lead)
        if not ok:
            log.debug("skip %s: %s", lead["phone"], why)
            continue
        if db.is_dnc(lead["phone"]):
            db.client().table("leads").update({"status": "dnc"}).eq("id", lead["id"]).execute()
            log.info("skip %s: on do-not-call list", lead["phone"])
            continue

        if dry_run:
            log.info("[dry-run] would call %s (%s, %s) — %s", lead["phone"], lead.get("name"), lead.get("country"), why)
            placed += 1
            continue

        db.mark_calling(lead["id"])
        room = await dispatch_call(lk, lead)
        minute_budget.append(time.time())
        placed += 1
        log.info("dispatched %s → %s (%s)", lead["phone"], room, why)
    return placed


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not db.enabled():
        raise SystemExit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")

    lk = api.LiveKitAPI()
    minute_budget: list[float] = []
    try:
        while True:
            try:
                n = await one_pass(lk, args.dry_run, minute_budget)
                if n:
                    log.info("pass complete: %d call(s) placed", n)
            except Exception as e:  # noqa: BLE001 — keep the loop alive
                log.exception("dialer pass failed: %s", e)
            if args.once:
                break
            await asyncio.sleep(POLL_SECONDS)
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
