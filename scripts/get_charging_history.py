#!/usr/bin/env python3
"""
Tesla Private API - Supercharger Charging History with Costs
=============================================================
Working endpoint (confirmed): GET ownership.tesla.com/mobile-app/charging/history

Returns all sessions for ALL vehicles on the account in one call.
Full billing data included: fees, rates, energy (kWh), invoices.

Real response field names (differ from GraphQL docs):
  siteAddress       → address (street, city, postalCode, country, countryCode)
  siteEntryLocation → {latitude, longitude}
  fees[].rateBase   → rate per unit (kWh or min)
  fees[].usageBase  → quantity used (kWh for CHARGING, minutes for CONGESTION)
  fees[].totalDue   → gross cost
  fees[].netDue     → net cost after credits/discounts
  fees[].uom        → "kwh" or "min"
  fees[].status     → "PAID" / "PENDING"
  invoices[].contentId → use this (NOT chargeSessionId) to download PDF

Fee types observed:
  CHARGING    → electricity cost (rateBase = EUR/kWh, usageBase = kWh)
  CONGESTION  → idle/overstay fee (rateBase = EUR/min, usageBase = minutes)

Run:
  python scripts/get_charging_history.py              # Table of all sessions
  python scripts/get_charging_history.py --vin LRW... # One vehicle only
  python scripts/get_charging_history.py --csv out.csv
  python scripts/get_charging_history.py --json out.json  # Raw response
  python scripts/get_charging_history.py --pretty         # Pretty raw JSON
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import auth
import requests

# ---------------------------------------------------------------------------
# Working endpoint
# ---------------------------------------------------------------------------
HISTORY_URL  = "https://ownership.tesla.com/mobile-app/charging/history"
INVOICE_URL  = "https://ownership.tesla.com/mobile-app/charging/invoice/{content_id}"

# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
def make_headers(token: str) -> dict:
    return {
        "Authorization":     f"Bearer {token}",
        "User-Agent":         "Tesla/1195 CFNetwork/1388 Darwin/22.0.0",
        "X-Tesla-User-Agent": "TeslaApp/4.30.6/ios/17.0",
        "Accept":             "application/json",
        "Accept-Language":    "en-US,en;q=0.9",
    }

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_all_sessions(token: str, vin: str | None = None) -> list[dict]:
    """
    Fetch all charging sessions. The API returns everything in one response
    (no effective pagination — all 36 sessions regardless of page params).
    Both vehicles on the account are included unless --vin filters to one.
    """
    country = os.getenv("TESLA_COUNTRY", "US")
    locale  = os.getenv("TESLA_LOCALE",  "en_US")

    params = {
        "deviceLanguage": "en",
        "deviceCountry":  country,
        "ttpLocale":      locale,
    }
    if vin:
        params["vin"] = vin

    print(f"[charging] GET {HISTORY_URL}")
    resp = requests.get(HISTORY_URL, params=params, headers=make_headers(token), timeout=30)

    if resp.status_code == 401:
        raise RuntimeError("401 — token expired. Run: python scripts/auth.py --refresh")
    if resp.status_code == 403:
        raise RuntimeError("403 — access denied. Try: python scripts/auth.py (fresh login)")
    resp.raise_for_status()

    body = resp.json()
    sessions = body.get("data") or []
    print(f"[charging] {len(sessions)} sessions returned (both vehicles combined)")
    return sessions

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def charging_fee(s: dict) -> dict | None:
    """Return the CHARGING fee entry (electricity cost)."""
    for f in (s.get("fees") or []):
        if f.get("feeType") == "CHARGING":
            return f
    return None

def congestion_fee(s: dict) -> dict | None:
    """Return the CONGESTION/idle fee entry."""
    for f in (s.get("fees") or []):
        if f.get("feeType") in ("CONGESTION", "PARKING"):
            return f
    return None

def energy_kwh(s: dict) -> float:
    """kWh delivered — from CHARGING fee usageBase (uom=kwh)."""
    cf = charging_fee(s)
    if cf and cf.get("uom", "").lower() == "kwh":
        return cf.get("usageBase") or 0
    # fallback: chargingPackage (often null in this API)
    pkg = s.get("chargingPackage") or {}
    return pkg.get("energyApplied") or 0

def total_cost(s: dict) -> float:
    """Total gross cost across all fee types."""
    return sum(f.get("totalDue") or 0 for f in (s.get("fees") or []))

def net_cost(s: dict) -> float:
    """Net cost after credits/discounts."""
    return sum(f.get("netDue") or 0 for f in (s.get("fees") or []))

def summarize_session(s: dict) -> dict:
    cf   = charging_fee(s)
    cong = congestion_fee(s)
    fees = s.get("fees") or []

    currency = (cf or (fees[0] if fees else {})).get("currencyCode", "?")
    gross    = round(total_cost(s), 4)
    net      = round(net_cost(s),   4)
    kwh      = energy_kwh(s)
    rate     = (cf or {}).get("rateBase")
    status   = (cf or (fees[0] if fees else {})).get("status") or ""
    cong_due = round((cong or {}).get("totalDue") or 0, 4)

    addr  = s.get("siteAddress") or {}
    coord = s.get("siteEntryLocation") or {}

    invoices   = s.get("invoices") or []
    content_id = invoices[0]["contentId"] if invoices else None

    return {
        "session_id":     s.get("chargeSessionId"),
        "vin":            s.get("vin"),
        "site_name":      s.get("siteLocationName"),
        "street":         f"{addr.get('street','')} {addr.get('streetNumber','')}".strip(),
        "city":           addr.get("city"),
        "postal_code":    addr.get("postalCode"),
        "country":        addr.get("country"),
        "country_code":   addr.get("countryCode"),
        "latitude":       coord.get("latitude"),
        "longitude":      coord.get("longitude"),
        "start":          s.get("chargeStartDateTime"),
        "stop":           s.get("chargeStopDateTime"),
        "energy_kwh":     round(kwh, 3),
        "rate_per_kwh":   rate,
        "gross_cost":     gross,
        "net_cost":       net,          # after credits/discounts
        "congestion_fee": cong_due,
        "currency":       currency,
        "payment_status": status,
        "is_paid":        (cf or {}).get("isPaid"),
        "billing_type":   s.get("billingType"),
        "session_source": s.get("sessionSource"),
        "is_msp":         s.get("isMsp"),
        "program_type":   s.get("programType"),
        "invoice_file":   invoices[0]["fileName"] if invoices else None,
        "content_id":     content_id,
        "invoice_url":    INVOICE_URL.format(content_id=content_id) if content_id else None,
    }

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
def print_table(sessions: list[dict]):
    print(f"\n{'='*105}")
    print(f"{'Date':<12} {'VIN':<20} {'Location':<26} {'kWh':>6} {'Rate':>8} {'Gross':>10} {'Net':>10} {'St':<6} {'Inv'}")
    print(f"{'='*105}")

    totals_gross: dict[str, float] = {}
    totals_kwh = 0.0

    for s in sessions:
        sm    = summarize_session(s)
        date  = (sm["start"] or "")[:10]
        vin   = (sm["vin"] or "")[-13:]         # last 13 chars of VIN
        loc   = (sm["site_name"] or "")[:24]
        kwh   = f"{sm['energy_kwh']:.1f}" if sm["energy_kwh"] else "---"
        rate  = f"{sm['rate_per_kwh']:.3f}" if sm["rate_per_kwh"] else "---"
        cur   = sm["currency"]
        gross = f"{cur} {sm['gross_cost']:.2f}"
        net   = f"{cur} {sm['net_cost']:.2f}"
        st    = (sm["payment_status"] or "")[:5]
        inv   = "✓" if sm["content_id"] else "-"
        print(f"{date:<12} {vin:<20} {loc:<26} {kwh:>6} {rate:>8} {gross:>10} {net:>10} {st:<6} {inv}")

        totals_gross[cur] = totals_gross.get(cur, 0) + (sm["gross_cost"] or 0)
        totals_kwh += sm["energy_kwh"] or 0

    print(f"{'='*105}")
    print(f"\nTotal sessions : {len(sessions)}")
    print(f"Total energy   : {totals_kwh:.1f} kWh")
    for cur, amt in totals_gross.items():
        print(f"Total gross    : {cur} {amt:.2f}")

def export_csv(sessions: list[dict], path: str):
    rows = [summarize_session(s) for s in sessions]
    if not rows:
        print("[csv] Nothing to export.")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[csv] {len(rows)} sessions → {path}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch Tesla Supercharger history with billing data"
    )
    parser.add_argument("--vin",    default=None, help="Filter to one VIN (default: all vehicles)")
    parser.add_argument("--csv",    default=None, metavar="FILE", help="Export to CSV")
    parser.add_argument("--json",   default=None, metavar="FILE", help="Save raw JSON")
    parser.add_argument("--pretty", action="store_true", help="Print raw JSON to stdout")
    args = parser.parse_args()

    print("[auth] Loading credentials...")
    try:
        token = auth.get_access_token()
    except RuntimeError as exc:
        print(f"[auth] {exc}")
        sys.exit(1)

    sessions = fetch_all_sessions(token, vin=args.vin)
    if not sessions:
        print("[charging] No sessions found.")
        sys.exit(0)

    if args.pretty:
        print(json.dumps(sessions, indent=2, default=str))
    else:
        print_table(sessions)

    if args.csv:
        export_csv(sessions, args.csv)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(sessions, f, indent=2, default=str)
        print(f"[json] Raw data → {args.json}")

if __name__ == "__main__":
    main()
