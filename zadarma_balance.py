"""
Zadarma prepaid balance check. Run daily before calling hours (Heroku Scheduler / cron).

    python zadarma_balance.py            # prints balance; exits 1 if below ZADARMA_MIN_BALANCE
    python zadarma_balance.py --stats    # also prints yesterday's call minutes and cost

Env: ZADARMA_API_KEY, ZADARMA_API_SECRET (generate at https://my.zadarma.com/api/),
     ZADARMA_MIN_BALANCE (default 20), ALERT_WEBHOOK_URL (optional: n8n/Slack/etc. gets a JSON POST)

Auth per Zadarma docs: Authorization: "<key>:<signature>", signature =
base64( hmac_sha1( secret, method + sorted_query + md5(sorted_query) ) ).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
import sys
from datetime import date, timedelta
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

load_dotenv()
BASE = "https://api.zadarma.com"


def _call(method: str, params: dict | None = None) -> dict:
    key, secret = os.environ["ZADARMA_API_KEY"], os.environ["ZADARMA_API_SECRET"]
    params = dict(sorted((params or {}).items()))
    query = urlencode(params)
    md5 = hashlib.md5(query.encode()).hexdigest()
    digest = hmac.new(secret.encode(), (method + query + md5).encode(), hashlib.sha1).hexdigest()
    sig = base64.b64encode(digest.encode()).decode()
    r = requests.get(BASE + method, params=params, headers={"Authorization": f"{key}:{sig}"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(f"Zadarma API error: {data}")
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    bal = _call("/v1/info/balance/")
    balance, currency = float(bal["balance"]), bal["currency"]
    minimum = float(os.getenv("ZADARMA_MIN_BALANCE", "20"))
    print(f"Zadarma balance: {balance:.2f} {currency} (alert below {minimum:.0f})")

    if args.stats:
        y = date.today() - timedelta(days=1)
        st = _call("/v1/statistics/", {"start": f"{y} 00:00:00", "end": f"{y} 23:59:59"})
        calls = st.get("stats", [])
        secs = sum(int(c.get("seconds", 0)) for c in calls)
        cost = sum(float(c.get("cost", 0)) for c in calls)
        print(f"Yesterday: {len(calls)} calls, {secs // 60} min, {cost:.2f} {currency}")

    if balance < minimum:
        msg = {"alert": "zadarma_low_balance", "balance": balance, "currency": currency, "minimum": minimum}
        print("LOW BALANCE — top up before calling hours", file=sys.stderr)
        hook = os.getenv("ALERT_WEBHOOK_URL")
        if hook:
            try:
                requests.post(hook, json=msg, timeout=10)
            except requests.RequestException as e:
                print(f"alert webhook failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
