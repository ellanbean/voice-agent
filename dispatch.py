"""
Place an outbound call by dispatching a job to the worker.

    python dispatch.py +4479xxxxxxx --name "Sam" --country GB --language en
    python dispatch.py +4915xxxxxxxx --name "Lena" --country DE --language de --lead-id L-1042

Anything that can hit the LiveKit API can do the same thing (n8n, your CRM):
it is one CreateAgentDispatch call with the lead in `metadata`.

Env: LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, AGENT_NAME (default sales-caller)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time

from dotenv import load_dotenv
from livekit import api

load_dotenv()


def check_e164(number: str) -> str:
    if not re.fullmatch(r"\+[1-9]\d{6,14}", number):
        raise SystemExit(f"{number!r} is not E.164 (e.g. +447911123456)")
    return number


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phone", type=check_e164)
    ap.add_argument("--name", default="")
    ap.add_argument("--company", default="")
    ap.add_argument("--country", default="", help="ISO code, e.g. GB, DE, FR")
    ap.add_argument("--language", default="", help="e.g. en, de, fr, es — leave blank to auto-detect")
    ap.add_argument("--source", default="a website enquiry")
    ap.add_argument("--campaign", default="general")
    ap.add_argument("--lead-id", default=None)
    args = ap.parse_args()

    lead_id = args.lead_id or f"L-{int(time.time())}"
    room = f"call-{lead_id}-{int(time.time())}"
    metadata = {
        "phone": args.phone,
        "lead_id": lead_id,
        "name": args.name,
        "company": args.company,
        "country": args.country,
        "language": args.language,
        "source": args.source,
        "campaign": args.campaign,
    }

    lk = api.LiveKitAPI()  # reads LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET
    try:
        dispatch = await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=os.getenv("AGENT_NAME", "sales-caller"),
                room=room,
                metadata=json.dumps(metadata),
            )
        )
        print(f"dispatched {dispatch.id} → room {room} → {args.phone}")
    finally:
        await lk.aclose()


if __name__ == "__main__":
    asyncio.run(main())
