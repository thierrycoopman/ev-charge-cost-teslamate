#!/usr/bin/env python3
"""
EVC-Net connection test — no TeslaMate DB required
===================================================
Tests login, export download, and Excel parsing for every configured
EVC-Net account. Use this before running sync_evc_costs.py to confirm
your credentials work and the export format is recognised.

Usage:
  python scripts/test_evc.py                         # tests accounts from .env
  python scripts/test_evc.py --file export.xlsx      # tests Excel parsing only
  python scripts/test_evc.py --save                  # saves downloaded xlsx to ./logs/
  python scripts/test_evc.py --debug                 # saves xlsx + Transactions page HTML for inspection
"""

import argparse
import os
import sys
from pathlib import Path

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

# Reuse everything from the sync script — no duplication
sys.path.insert(0, str(Path(__file__).parent))
from sync_evc_costs import (
    EVCSession, load_accounts, parse_excel, DEFAULT_CURRENCY
)


def test_account(account: dict, since: str | None, save_dir: Path | None,
                 debug: bool = False) -> bool:
    name     = account["name"]
    base_url = account["base_url"]

    print(f"\n{'─'*60}")
    print(f"  Network  : {name}")
    print(f"  Portal   : {base_url}")
    print(f"  Email    : {account['email']}")
    print()

    evc = EVCSession(base_url, account["email"], account["password"])

    # ── 1. Login ──────────────────────────────────────────────────────────────
    print("  [1/3] Login...")
    if not evc.login():
        print("  ✗  Login failed — check EVC_EMAIL / EVC_PASSWORD")
        return False
    print("  ✓  Login OK")

    # ── 2. Download export ────────────────────────────────────────────────────
    print("\n  [2/3] Downloading export...")
    debug_dir = save_dir if debug else None
    raw = evc.get_export(since=since, debug_dir=debug_dir)

    if raw is None:
        print("  ✗  Export download failed")
        print("     The export URL could not be found automatically.")
        if debug and save_dir:
            print(f"     [debug] Transactions page HTML saved to {save_dir}/transactions_page.html")
            print(f"             Open it to find the export form or button.")
        print(f"     Try manually: go to {base_url}/Transactions → Export → save xlsx")
        print("     Then test parsing: python scripts/test_evc.py --file ~/Downloads/export.xlsx")
        return False

    print(f"  ✓  Downloaded {len(raw):,} bytes")

    if save_dir:
        save_path = save_dir / f"evc_test_{name}.xlsx"
        save_path.write_bytes(raw)
        print(f"  ✓  Saved to {save_path}")

    # ── 3. Parse ──────────────────────────────────────────────────────────────
    print("\n  [3/3] Parsing Excel...")
    sessions = parse_excel(raw, label=name)

    if not sessions:
        print("  ✗  No sessions parsed — column names may not be recognised")
        print("     Use --save to keep the xlsx and inspect it manually")
        return False

    print(f"  ✓  {len(sessions)} session(s) parsed\n")

    # Print a summary table of the first 10 sessions
    print(f"  {'Date':<12}  {'Location':<30}  {'kWh':>6}  {'Cost':>10}  {'Currency'}")
    print(f"  {'─'*12}  {'─'*30}  {'─'*6}  {'─'*10}  {'─'*8}")
    for s in sessions[:10]:
        date  = s["start_dt"].strftime("%Y-%m-%d") if s.get("start_dt") else "?"
        loc   = str(s.get("location") or "?")[:28]
        kwh   = f"{s['kwh']:.2f}"   if s.get("kwh")  is not None else "---"
        cost  = f"{s['cost']:.2f}"  if s.get("cost") is not None else "---"
        cur   = s.get("currency") or DEFAULT_CURRENCY
        print(f"  {date:<12}  {loc:<30}  {kwh:>6}  {cost:>10}  {cur}")

    if len(sessions) > 10:
        print(f"  … and {len(sessions) - 10} more session(s)")

    total_cost = sum(s["cost"] for s in sessions if s.get("cost") is not None)
    total_kwh  = sum(s["kwh"]  for s in sessions if s.get("kwh")  is not None)
    currencies = {s.get("currency") or DEFAULT_CURRENCY for s in sessions}
    print(f"\n  Total: {total_kwh:.1f} kWh  |  {'/'.join(currencies)} {total_cost:.2f}  ({len(sessions)} sessions)")

    return True


def main():
    parser = argparse.ArgumentParser(description="Test EVC-Net connection and Excel parsing")
    parser.add_argument("--file",    default=None, metavar="FILE",
                        help="Test Excel parsing only (skip login and download)")
    parser.add_argument("--save",    action="store_true",
                        help="Save downloaded xlsx files to ./logs/ for inspection")
    parser.add_argument("--debug",   action="store_true",
                        help="Save Transactions page HTML to ./logs/ to diagnose export failures "
                             "(implies --save)")
    parser.add_argument("--since",   default=None, metavar="YYYY-MM-DD",
                        help="Limit export to transactions since this date")
    parser.add_argument("--network", default=None, metavar="NAME",
                        help="Test one named account only")
    parser.add_argument("--accounts", default=None, metavar="FILE",
                        help="JSON accounts file (overrides EVC_ACCOUNTS_FILE)")
    args = parser.parse_args()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║          EVC-Net Connection Test                     ║")
    print("╚══════════════════════════════════════════════════════╝")

    # ── File-only mode (no login needed) ─────────────────────────────────────
    if args.file:
        fpath = Path(args.file)
        if not fpath.exists():
            print(f"[error] File not found: {fpath}")
            sys.exit(1)
        print(f"\n  Testing Excel parsing only: {fpath}\n")
        sessions = parse_excel(fpath.read_bytes())
        if sessions:
            print(f"\n  ✓  Parsed {len(sessions)} session(s). Format recognised.")
            print("  Ready to run: python scripts/sync_evc_costs.py --file " + str(fpath))
        else:
            print("\n  ✗  No sessions parsed. Check column names in the file.")
        return

    # ── Live login + download mode ────────────────────────────────────────────
    accounts = load_accounts(args.accounts)

    if not accounts:
        print("\n  [error] No EVC-Net accounts configured.\n")
        print("  Set in .env:")
        print("    EVC_EMAIL=your@email.com")
        print("    EVC_PASSWORD=yourpassword")
        print("    EVC_BASE_URL=https://agrisnellaad.evc-net.com")
        print("\n  Or use --file to test parsing a manually downloaded xlsx:")
        print("    python scripts/test_evc.py --file ~/Downloads/export.xlsx")
        sys.exit(1)

    if args.network:
        accounts = [a for a in accounts if a["name"] == args.network]
        if not accounts:
            print(f"  [error] No account named '{args.network}'")
            sys.exit(1)

    save_dir = None
    if args.save or args.debug:
        save_dir = Path(__file__).parent.parent / "logs"
        save_dir.mkdir(exist_ok=True)

    print(f"\n  Testing {len(accounts)} account(s)...")

    results = {}
    for account in accounts:
        results[account["name"]] = test_account(
            account, args.since, save_dir, debug=args.debug
        )

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    print("  Results:")
    for name, ok in results.items():
        print(f"    {'✓' if ok else '✗'}  {name}")

    all_ok = all(results.values())
    if all_ok:
        print(f"\n  All accounts OK. Next step:")
        print(f"    python scripts/sync_evc_costs.py          # dry-run (needs TeslaMate DB)")
        print(f"    python scripts/sync_evc_costs.py --apply  # write costs to DB")
    else:
        print(f"\n  Some accounts failed. Fix the issues above before running sync.")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
