#!/usr/bin/env python3
"""
Tesla Private API - Charging Invoice Downloader
================================================
Downloads PDF invoices for your Supercharger sessions.

How it works:
  1. Fetch all charging sessions via ownership.tesla.com/mobile-app/charging/history
  2. Each session has an invoices[] array with contentId and beInvoiceId
  3. Download PDF via ownership.tesla.com/mobile-app/charging/invoice/{contentId}

Real invoice object (from API):
  {
    "fileName":       "4090P0001884997_NL-BE.pdf",
    "contentId":      "d05d0698-ddb3-4e16-910a-a1c67ce70dad",  ← use this in URL
    "invoiceType":    "IMMEDIATE",
    "beInvoiceId":    "1d60b4f2-ace0-11f0-8da0-005056928cd5",  ← alternate ID
    "invoiceSubType": "SESSION"
  }

Run:
  python scripts/get_invoices.py                    # Download all invoices
  python scripts/get_invoices.py --vin LRW...       # One vehicle only
  python scripts/get_invoices.py --list             # List without downloading
  python scripts/get_invoices.py --output ./invoices
"""

import argparse
import os
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import auth
import get_charging_history as charging

OWNERSHIP_BASE   = "https://ownership.tesla.com"
INVOICE_ENDPOINT = f"{OWNERSHIP_BASE}/mobile-app/charging/invoice/{{content_id}}"
DEFAULT_OUT      = Path("./invoices")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_headers(token: str) -> dict:
    return {
        "Authorization":     f"Bearer {token}",
        "User-Agent":         "Tesla/1195 CFNetwork/1388 Darwin/22.0.0",
        "X-Tesla-User-Agent": "TeslaApp/4.30.6/ios/17.0",
        "Accept":             "application/pdf, application/json",
        "Accept-Language":    "en-US,en;q=0.9",
    }

def safe(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", str(text))[:35]

# ---------------------------------------------------------------------------
# Download one invoice PDF
# ---------------------------------------------------------------------------
def download_invoice(token: str, content_id: str, vin: str, out_path: Path,
                     country: str = "BE") -> bool:
    """
    Try downloading the invoice PDF using contentId.
    Also tries beInvoiceId as fallback URL param.
    """
    url    = INVOICE_ENDPOINT.format(content_id=content_id)
    params = {
        "deviceCountry":  country,
        "deviceLanguage": "en",
        "vin":            vin,
    }

    try:
        resp = requests.get(url, params=params, headers=make_headers(token), timeout=30)

        if resp.status_code == 404:
            print(f"    404 — invoice not found (may have expired)")
            return False
        if resp.status_code == 502:
            # 502 often means the contentId is right but the back-end is flaky — retry once
            print(f"    502 — backend error, retrying once...")
            resp = requests.get(url, params=params, headers=make_headers(token), timeout=30)

        if not resp.ok:
            print(f"    {resp.status_code} — {resp.text[:120]}")
            return False

        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type:
            # Some invoices return JSON with a redirect URL
            try:
                body = resp.json()
                redirect = body.get("data", {}).get("url") or body.get("url")
                if redirect:
                    print(f"    Redirecting to: {redirect[:80]}")
                    resp = requests.get(redirect, headers=make_headers(token), timeout=30)
                    if "pdf" not in resp.headers.get("Content-Type", ""):
                        print(f"    Still not a PDF after redirect: {resp.headers.get('Content-Type')}")
                        return False
                else:
                    print(f"    Unexpected content-type: {content_type}")
                    print(f"    Body: {resp.text[:200]}")
                    return False
            except Exception:
                print(f"    Not a PDF and not parseable JSON. Content-Type: {content_type}")
                return False

        out_path.write_bytes(resp.content)
        print(f"    ✓ Saved: {out_path.name} ({len(resp.content):,} bytes)")
        return True

    except requests.RequestException as exc:
        print(f"    Request failed: {exc}")
        return False

# ---------------------------------------------------------------------------
# Collect invoice metadata from sessions
# ---------------------------------------------------------------------------
def collect_invoices(sessions: list[dict]) -> list[dict]:
    invoices = []
    seen = set()
    for s in sessions:
        for inv in (s.get("invoices") or []):
            cid = inv.get("contentId")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            invoices.append({
                "content_id":   cid,
                "be_invoice_id":inv.get("beInvoiceId"),
                "file_name":    inv.get("fileName"),
                "invoice_type": inv.get("invoiceType"),
                "sub_type":     inv.get("invoiceSubType"),
                "session_date": (s.get("chargeStartDateTime") or "")[:10],
                "site_name":    s.get("siteLocationName") or "unknown",
                "vin":          s.get("vin"),
                "country_code": (s.get("siteAddress") or {}).get("countryCode")
                                 or s.get("countryCode") or "BE",
            })
    return invoices

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def run(token: str, vin: str | None, output_dir: Path, list_only: bool) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n[invoices] Fetching charging history...")
    sessions = charging.fetch_all_sessions(token, vin=vin)
    if not sessions:
        print("[invoices] No sessions found.")
        return {"found": 0, "ok": 0, "failed": 0, "skipped": 0}

    invoices = collect_invoices(sessions)
    print(f"[invoices] {len(invoices)} unique invoice(s) across {len(sessions)} session(s)\n")

    if list_only:
        print(f"{'Date':<12} {'VIN':<20} {'Site':<28} {'Type':<12} {'File'}")
        print("-" * 90)
        for inv in invoices:
            print(f"{inv['session_date']:<12} {(inv['vin'] or '')[-13:]:<20} "
                  f"{inv['site_name'][:26]:<28} {(inv['invoice_type'] or ''):<12} "
                  f"{inv['file_name']}")
        return {"found": len(invoices), "ok": 0, "failed": 0, "skipped": 0}

    ok = failed = skipped = 0
    for inv in invoices:
        date    = safe(inv["session_date"])
        site    = safe(inv["site_name"])
        cid     = inv["content_id"][:8]
        fname   = f"{date}_{site}_{cid}.pdf"
        fpath   = output_dir / fname
        country = inv["country_code"] or "BE"

        if fpath.exists():
            print(f"  = {fname} (already exists)")
            skipped += 1
            continue

        print(f"  ↓ [{inv['session_date']}] {inv['site_name']} ({inv['invoice_type']})")
        success = download_invoice(token, inv["content_id"], inv["vin"] or "", fpath, country)
        if success:
            ok += 1
        else:
            failed += 1

    return {"found": len(invoices), "ok": ok, "failed": failed, "skipped": skipped}

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Download Tesla Supercharger invoice PDFs")
    parser.add_argument("--vin",    default=None,         help="Filter to one VIN")
    parser.add_argument("--output", default="./invoices", help="Output directory")
    parser.add_argument("--list",   action="store_true",  help="List without downloading")
    args = parser.parse_args()

    print("[auth] Loading credentials...")
    try:
        token = auth.get_access_token()
    except RuntimeError as exc:
        print(f"[auth] {exc}")
        sys.exit(1)

    result = run(token, args.vin, Path(args.output), args.list)
    print(f"\n[done] Found:{result['found']}  Downloaded:{result['ok']}  "
          f"Skipped:{result['skipped']}  Failed:{result['failed']}")

if __name__ == "__main__":
    main()
