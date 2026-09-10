"""
Import leads from a CSV into Supabase.

    python import_leads.py leads.csv --campaign uk-sept
    python import_leads.py leads.csv --campaign uk-sept --consent-now   # stamp consent_at=now for all rows

CSV columns (header row required; extra columns are ignored):
    phone, name, company, country, language, source, consent_at
- phone must be E.164 (+447911123456). Rows that aren't are reported and skipped.
- country is ISO-2 (GB, DE...). Required.
- consent_at: ISO timestamp of the opt-in. If blank and --consent-now is not given,
  the lead is imported but the dialer will NOT call it (consent_at null).

Existing (phone, campaign) rows are updated, not duplicated.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone

import db

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--campaign", default="general")
    ap.add_argument("--consent-now", action="store_true", help="set consent_at=now where blank")
    args = ap.parse_args()

    if not db.enabled():
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")

    ok = skipped = 0
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            phone = (row.get("phone") or "").strip().replace(" ", "")
            country = (row.get("country") or "").strip().upper()
            if not E164.match(phone) or len(country) != 2:
                print(f"line {i}: skipped (phone={phone!r}, country={country!r})")
                skipped += 1
                continue
            consent = (row.get("consent_at") or "").strip() or (
                datetime.now(timezone.utc).isoformat() if args.consent_now else None
            )
            db.upsert_lead(
                {
                    "phone": phone,
                    "name": (row.get("name") or "").strip() or None,
                    "company": (row.get("company") or "").strip() or None,
                    "country": country,
                    "language": (row.get("language") or "").strip() or None,
                    "source": (row.get("source") or "").strip() or None,
                    "consent_at": consent,
                    "campaign": args.campaign,
                }
            )
            ok += 1
    print(f"imported/updated {ok}, skipped {skipped}")


if __name__ == "__main__":
    main()
