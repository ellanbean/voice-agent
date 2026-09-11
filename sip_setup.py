"""
Register a SIP outbound trunk with LiveKit from one of the sip/*.json files (no `lk` CLI needed).

  python sip_setup.py sip/plivo-outbound-trunk.json      # → prints ST_xxxx
  python sip_setup.py --list                              # show trunks already registered

Then: SIP_OUTBOUND_TRUNK_IDS=ST_xxxx in .env and `heroku config:set SIP_OUTBOUND_TRUNK_IDS=ST_xxxx -a into3-voice`.
"""
import asyncio
import json
import sys

from dotenv import load_dotenv
from livekit import api

load_dotenv()

TRANSPORTS = {"SIP_TRANSPORT_AUTO": 0, "SIP_TRANSPORT_UDP": 1, "SIP_TRANSPORT_TCP": 2, "SIP_TRANSPORT_TLS": 3}


async def main():
    lk = api.LiveKitAPI()
    try:
        if "--list" in sys.argv:
            res = await lk.sip.list_sip_outbound_trunk(api.ListSIPOutboundTrunkRequest())
            for t in res.items:
                print(f"{t.sip_trunk_id}  {t.name:<20} {t.address:<35} numbers={list(t.numbers)}")
            return
        path = sys.argv[1]
        cfg = json.load(open(path, encoding="utf-8"))["trunk"]
        if "REPLACE" in cfg["address"] or "XXXX" in "".join(cfg["numbers"]):
            raise SystemExit(f"fill in the real values in {path} first")
        trunk = api.SIPOutboundTrunkInfo(
            name=cfg["name"], address=cfg["address"], numbers=cfg["numbers"],
            auth_username=cfg.get("auth_username", ""), auth_password=cfg.get("auth_password", ""),
            transport=TRANSPORTS.get(cfg.get("transport", "SIP_TRANSPORT_AUTO"), 0),
        )
        created = await lk.sip.create_sip_outbound_trunk(api.CreateSIPOutboundTrunkRequest(trunk=trunk))
        print("created", created.sip_trunk_id)
        print(f"next:  heroku config:set SIP_OUTBOUND_TRUNK_IDS={created.sip_trunk_id} -a into3-voice   (and the same line in .env)")
    finally:
        await lk.aclose()


asyncio.run(main())
