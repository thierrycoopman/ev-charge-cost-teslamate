#!/usr/bin/env python3
"""
Tesla API — Quick probe of the working ownership.tesla.com endpoint.
Prints the FULL response of the first session so we can see all available fields.

Usage:
  python scripts/debug_charging.py
  python scripts/debug_charging.py --vin LRW...
  python scripts/debug_charging.py --pages 2
"""

import json, os, sys, argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import auth
import requests

parser = argparse.ArgumentParser()
parser.add_argument("--vin",   default=None)
parser.add_argument("--pages", type=int, default=1)
args = parser.parse_args()

token   = auth.get_access_token()
COUNTRY = os.getenv("TESLA_COUNTRY", "US")
LOCALE  = os.getenv("TESLA_LOCALE",  "en_US")

HEADERS = {
    "Authorization": f"Bearer {token}",
    "User-Agent":        "Tesla/1195 CFNetwork/1388 Darwin/22.0.0",
    "X-Tesla-User-Agent":"TeslaApp/4.30.6/ios/17.0",
    "Accept":            "application/json",
    "Accept-Language":   "en-US,en;q=0.9",
}

BASE_URL = "https://ownership.tesla.com/mobile-app/charging/history"

# ── 1. Check all vehicles on account ─────────────────────────────────────────
print("=" * 65)
print("Vehicles on account:")
print("=" * 65)
r = requests.get("https://owner-api.teslamotors.com/api/1/products",
                 headers=HEADERS, timeout=15)
products = r.json().get("response", [])
for p in products:
    print(f"  VIN: {p.get('vin')}  Name: {p.get('display_name')}  ID: {p.get('id')}")

# ── 2. Probe pagination params ────────────────────────────────────────────────
print("\n" + "=" * 65)
print("Probing pagination params:")
print("=" * 65)
for param_set in [
    {},
    {"pageNo": 1, "pageSize": 5},
    {"page": 1,   "size": 5},
    {"offset": 0, "limit": 5},
]:
    params = {"deviceLanguage": "en", "deviceCountry": COUNTRY, "ttpLocale": LOCALE}
    if args.vin:
        params["vin"] = args.vin
    params.update(param_set)
    r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
    data = r.json()
    items = data.get("data") or []
    count = len(items) if isinstance(items, list) else "?"
    print(f"  params={param_set}  →  status={r.status_code}  items={count}")

# ── 3. Full dump of first session ─────────────────────────────────────────────
print("\n" + "=" * 65)
print("Full first session (ALL fields):")
print("=" * 65)
params = {"deviceLanguage": "en", "deviceCountry": COUNTRY, "ttpLocale": LOCALE}
if args.vin:
    params["vin"] = args.vin
r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=20)
data  = r.json()
items = data.get("data") or []
if items:
    print(json.dumps(items[0], indent=2, default=str))
    print(f"\n--- Keys in session object: {list(items[0].keys())}")
    print(f"--- Total sessions in this response: {len(items)}")
else:
    print("No sessions returned.")
    print(json.dumps(data, indent=2))

# ── 4. Try invoice download from first session ────────────────────────────────
print("\n" + "=" * 65)
print("Invoice probe from first session:")
print("=" * 65)
if items:
    s = items[0]
    print(f"Session: {s.get('chargeSessionId')}")
    invoices = s.get("invoices") or []
    print(f"invoices field: {invoices}")
    # Try fetching with the chargeSessionId directly
    session_id = s.get("chargeSessionId") or s.get("sessionId")
    if session_id:
        for inv_url in [
            f"https://ownership.tesla.com/mobile-app/charging/invoice/{session_id}",
        ]:
            inv_params = {"deviceCountry": COUNTRY, "deviceLanguage": "en",
                          "vin": s.get("vin")}
            ri = requests.get(inv_url, params=inv_params, headers=HEADERS, timeout=15)
            print(f"  Invoice URL: {inv_url}")
            print(f"  Status: {ri.status_code}  Content-Type: {ri.headers.get('Content-Type')}")
            if "pdf" in ri.headers.get("Content-Type",""):
                with open("test_invoice.pdf","wb") as f: f.write(ri.content)
                print(f"  → PDF saved as test_invoice.pdf ({len(ri.content):,} bytes)")
            else:
                print(f"  → Body: {ri.text[:300]}")
