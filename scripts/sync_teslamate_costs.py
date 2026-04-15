#!/usr/bin/env python3
"""
TeslaMate ↔ Tesla Billing API — Cost Sync
==========================================
Matches Tesla's Supercharger billing data against TeslaMate's charging_processes
table and fills in the missing `cost` column.

TeslaMate tracks WHEN and HOW MUCH you charged (kWh) but never calls the
billing API, so charging_processes.cost is almost always NULL for Supercharger
sessions. This script fixes that.

Matching logic — two passes:
  Pass 1 (strict):  VIN must match AND start timestamp within ±tolerance.
                    Covers normal Supercharger sessions for your tracked cars.
  Pass 2 (lenient): Timestamp only, VIN ignored. Catches sessions charged to
                    your Tesla account but for a VIN not tracked in TeslaMate
                    (e.g. charged on other infrastructure, a previous car,
                    a loaner, or a second car added after TeslaMate started).
                    These are flagged with ⚠ in the output — verify before applying.

  Steps:
  1. Pull TeslaMate sessions where cost IS NULL (or --overwrite for all)
  2. Pull billing sessions from Tesla API (all VINs on the account)
  3. Run Pass 1, then Pass 2 on the leftovers
  4. Preview in a table — safe dry-run by default
  5. Run with --apply to write cost values to the DB

Cost used: totalDue (gross) by default. Use --net for netDue (after credits).

Usage:
  # Preview what would be updated (safe, no DB writes)
  python scripts/sync_teslamate_costs.py

  # Actually write costs to TeslaMate DB
  python scripts/sync_teslamate_costs.py --apply

  # Also update sessions that already have a cost (e.g. to fix manual entries)
  python scripts/sync_teslamate_costs.py --apply --overwrite

  # Use net cost (after credits/discounts) instead of gross
  python scripts/sync_teslamate_costs.py --apply --net

  # Widen matching window if sessions aren't matching (default: 5 min)
  python scripts/sync_teslamate_costs.py --tolerance 10

  # Run for one vehicle only
  python scripts/sync_teslamate_costs.py --vin LRW3E7EK2NC519765

Environment variables (or set in .env):
  TESLAMATE_DATABASE_URL  postgresql://teslamate:teslamate@localhost:5432/teslamate
  TESLA_COUNTRY           BE   (for API locale)
  TESLA_LOCALE            fr_BE

Cron (daily at 6am):
  0 6 * * * cd /path/to/ev-charge-cost-teslamate && .venv/bin/python scripts/sync_teslamate_costs.py --apply >> logs/sync.log 2>&1

Requires: pip install psycopg2-binary python-dotenv
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))
import auth
import get_charging_history as charging

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("[error] psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_DB_URL   = "postgresql://teslamate:teslamate@localhost:5432/teslamate"
DEFAULT_TOLERANCE_MIN = 5   # session start must match within this many minutes

# ---------------------------------------------------------------------------
# TeslaMate DB queries
# ---------------------------------------------------------------------------

QUERY_SESSIONS = """
SELECT
    cp.id,
    cp.start_date,
    cp.end_date,
    cp.charge_energy_added,
    cp.charge_energy_used,
    cp.cost,
    cp.duration_min,
    cp.start_battery_level,
    cp.end_battery_level,
    c.vin,
    c.name  AS car_name
FROM charging_processes cp
JOIN cars c ON c.id = cp.car_id
{where_clause}
ORDER BY cp.start_date DESC;
"""

UPDATE_COST = """
UPDATE charging_processes
SET cost = %s
WHERE id = %s;
"""

def connect(db_url: str):
    """Open a psycopg2 connection."""
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        return conn
    except psycopg2.OperationalError as exc:
        print(f"\n[db] Connection failed: {exc}")
        print(f"[db] URL used: {db_url}")
        print("[db] Tip: check TESLAMATE_DATABASE_URL in your .env file")
        print("[db] Tip: if TeslaMate runs in Docker, ensure the DB port is mapped")
        print("[db]       (add '5432:5432' under db: ports: in docker-compose.yml)")
        sys.exit(1)

def fetch_teslamate_sessions(conn, overwrite: bool, vin_filter: str | None) -> list[dict]:
    """Pull charging sessions from TeslaMate DB."""
    conditions = []
    if not overwrite:
        conditions.append("cp.cost IS NULL")
    if vin_filter:
        conditions.append(f"c.vin = '{vin_filter}'")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = QUERY_SESSIONS.format(where_clause=where)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

def to_utc(dt) -> datetime:
    """Normalise any datetime (aware or naive) to UTC."""
    if dt is None:
        return None
    if isinstance(dt, str):
        # Parse ISO8601 strings like "2025-10-19T12:44:55+02:00"
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        # Assume UTC if naive (TeslaMate stores in UTC)
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def find_match(tm_session: dict, billing_sessions: list[dict],
               tolerance_min: int, require_vin: bool = True) -> tuple[dict | None, str]:
    """
    Find the best billing session matching a TeslaMate session.

    Pass 1 (require_vin=True):  VIN must match AND start time within tolerance.
    Pass 2 (require_vin=False): VIN is ignored — timestamp only. Used for sessions
        where the billing account VIN differs from the TeslaMate-tracked VIN
        (e.g. charged on different infrastructure, previous car, loaner, etc.)

    Returns (best_match_or_None, match_type_label).
    """
    vin      = tm_session["vin"]
    tm_start = to_utc(tm_session["start_date"])
    if not tm_start:
        return None, ""

    tolerance  = timedelta(minutes=tolerance_min)
    best       = None
    best_delta = timedelta.max

    for s in billing_sessions:
        if require_vin and s.get("vin") != vin:
            continue
        api_start = to_utc(s.get("chargeStartDateTime"))
        if not api_start:
            continue
        delta = abs(tm_start - api_start)
        if delta <= tolerance and delta < best_delta:
            best       = s
            best_delta = delta

    if best is None:
        return None, ""

    label = "VIN+time" if require_vin else "time-only ⚠"
    return best, label

# ---------------------------------------------------------------------------
# Cost extraction
# ---------------------------------------------------------------------------

def get_cost(billing_session: dict, use_net: bool) -> float | None:
    """
    Extract the cost from a billing session.
    use_net=True  → netDue  (after credits/discounts — what you actually paid)
    use_net=False → totalDue (gross — before credits)
    """
    fees = billing_session.get("fees") or []
    if not fees:
        return None

    # Sum all fee types (CHARGING + CONGESTION if any)
    key = "netDue" if use_net else "totalDue"
    total = sum(f.get(key) or 0 for f in fees)

    return round(total, 4) if total > 0 else None


def get_currency(billing_session: dict) -> str:
    fees = billing_session.get("fees") or []
    return fees[0].get("currencyCode", "?") if fees else "?"

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_match_table(matches: list[dict], use_net: bool, title: str = "Matched sessions"):
    cost_label = "Net cost" if use_net else "Gross cost"
    print(f"\n── {title} {'─'*(97 - len(title))}")
    print(f"  {'TM ID':>10}  {'Date':<12}  {'TM car':<12}  {'API VIN':<20}  {'Location':<24}  "
          f"{'kWh':>5}  {'Δmin':>5}  {cost_label:>10}  {'Was':<8}  Match")
    print(f"  {'─'*10}  {'─'*12}  {'─'*12}  {'─'*20}  {'─'*24}  "
          f"{'─'*5}  {'─'*5}  {'─'*10}  {'─'*8}  {'─'*12}")
    for m in matches:
        tm       = m["tm"]
        api      = m["api"]
        cost_new = m["cost_new"]
        cost_old = tm.get("cost")
        currency = m["currency"]
        # energy from CHARGING fee
        kwh = "---"
        for f in (api.get("fees") or []):
            if f.get("feeType") == "CHARGING" and f.get("uom","").lower() == "kwh":
                kwh = f"{f.get('usageBase', 0):.1f}"
                break
        delta_s  = abs((to_utc(tm["start_date"]) - to_utc(api["chargeStartDateTime"])).total_seconds())
        delta_m  = f"{delta_s/60:.1f}"
        cost_str = f"{currency} {cost_new:.2f}" if cost_new is not None else "---"
        was_str  = f"{cost_old:.2f}" if cost_old is not None else "null"
        site     = (api.get("siteLocationName") or "?")[:22]
        car      = (tm.get("car_name") or tm.get("vin","")[-8:])[:10]
        api_vin  = (api.get("vin") or "?")
        mtype    = m.get("match_type", "VIN+time")
        print(f"  {tm['id']:>10}  {str(tm['start_date'])[:10]:<12}  {car:<12}  "
              f"{api_vin:<20}  {site:<24}  {kwh:>5}  {delta_m:>5}  "
              f"{cost_str:>10}  {was_str:<8}  {mtype}")
    print()


def print_unmatched(unmatched_tm: list[dict], unmatched_api: list[dict]):
    if unmatched_tm:
        print(f"\n[unmatched TeslaMate] {len(unmatched_tm)} session(s) — no billing match found:")
        for s in unmatched_tm[:15]:
            print(f"  {str(s['start_date'])[:16]}  VIN:{s['vin']:<22}  "
                  f"kWh:{str(s.get('charge_energy_added') or '?'):<6}  {s.get('car_name','')}")
        if len(unmatched_tm) > 15:
            print(f"  ... and {len(unmatched_tm)-15} more")
        print("  → Likely: home charging, destination chargers, or outside the API's history window")

    if unmatched_api:
        print(f"\n[unmatched billing API] {len(unmatched_api)} session(s) — not in TeslaMate:")
        for s in unmatched_api[:15]:
            fees = s.get("fees") or []
            cost = sum(f.get("totalDue") or 0 for f in fees)
            cur  = fees[0].get("currencyCode","?") if fees else "?"
            print(f"  {(s.get('chargeStartDateTime') or '')[:16]}  VIN:{s.get('vin','?'):<22}  "
                  f"{cur} {cost:.2f}  {s.get('siteLocationName','?')}")
        if len(unmatched_api) > 15:
            print(f"  ... and {len(unmatched_api)-15} more")
        print("  → Could be: sessions before TeslaMate started, a VIN not in TeslaMate, or a previous car")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync Tesla Supercharger billing costs into TeslaMate's DB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--apply",     action="store_true",
                        help="Write costs to DB (default: dry-run preview only)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Also update sessions that already have a cost")
    parser.add_argument("--net",       action="store_true",
                        help="Use netDue (after credits) instead of totalDue (gross)")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE_MIN,
                        help=f"Time match tolerance in minutes (default: {DEFAULT_TOLERANCE_MIN})")
    parser.add_argument("--vin",       default=None,
                        help="Filter to one VIN (both cars synced by default)")
    parser.add_argument("--db",        default=None,
                        help="PostgreSQL URL (overrides TESLAMATE_DATABASE_URL env var)")
    args = parser.parse_args()

    db_url = args.db or os.getenv("TESLAMATE_DATABASE_URL", DEFAULT_DB_URL)
    cost_label = "net (after credits)" if args.net else "gross"

    print(f"""
╔══════════════════════════════════════════════════════╗
║     TeslaMate ↔ Tesla Billing Cost Sync              ║
╚══════════════════════════════════════════════════════╝
  Mode       : {"⚡ APPLY — writing to DB" if args.apply else "🔍 DRY RUN — preview only (add --apply to write)"}
  Cost type  : {cost_label}
  Overwrite  : {args.overwrite}
  Tolerance  : ±{args.tolerance} min
  VIN filter : {args.vin or "all vehicles"}
  DB         : {db_url[:60]}
""")

    # Step 1: Tesla billing data
    print("[tesla] Loading token...")
    try:
        token = auth.get_access_token()
    except RuntimeError as exc:
        print(f"[tesla] {exc}")
        sys.exit(1)

    print("[tesla] Fetching billing sessions from Tesla API...")
    billing_sessions = charging.fetch_all_sessions(token, vin=args.vin)
    print(f"[tesla] Got {len(billing_sessions)} billing session(s)")

    if not billing_sessions:
        print("[tesla] No billing sessions found. Exiting.")
        sys.exit(0)

    # Step 2: TeslaMate sessions
    print(f"\n[teslamate] Connecting to database...")
    conn = connect(db_url)
    tm_sessions = fetch_teslamate_sessions(conn, overwrite=args.overwrite, vin_filter=args.vin)
    cost_status = "all" if args.overwrite else "cost IS NULL"
    print(f"[teslamate] Found {len(tm_sessions)} session(s) ({cost_status})")

    if not tm_sessions:
        print("[teslamate] Nothing to update.")
        conn.close()
        sys.exit(0)

    # ── Pass 1: Match by VIN + timestamp ──────────────────────────────────────
    print(f"\n[match] Pass 1 — VIN + start time (±{args.tolerance} min)...")
    matches_p1: list[dict] = []
    matched_api_ids: set   = set()
    unmatched_tm: list[dict] = []

    for tm in tm_sessions:
        api, label = find_match(tm, billing_sessions, args.tolerance, require_vin=True)
        if api:
            matches_p1.append({
                "tm":         tm,
                "api":        api,
                "cost_new":   get_cost(api, use_net=args.net),
                "currency":   get_currency(api),
                "match_type": label,
            })
            matched_api_ids.add(api.get("chargeSessionId"))
        else:
            unmatched_tm.append(tm)

    # Billing sessions still unmatched after pass 1
    unmatched_api = [
        s for s in billing_sessions
        if s.get("chargeSessionId") not in matched_api_ids
    ]

    print(f"  Pass 1 result → matched: {len(matches_p1)}  |  "
          f"TeslaMate unmatched: {len(unmatched_tm)}  |  "
          f"API unmatched: {len(unmatched_api)}")

    # ── Pass 2: Cross-VIN match by timestamp only ──────────────────────────────
    # For TeslaMate sessions that had no VIN match, search the remaining billing
    # sessions purely by timestamp. Catches sessions charged to this account
    # under a different VIN (other infrastructure, previous car, loaner, etc.)
    matches_p2: list[dict] = []
    still_unmatched_tm: list[dict] = []

    if unmatched_tm and unmatched_api:
        print(f"\n[match] Pass 2 — timestamp only, ignoring VIN (±{args.tolerance} min)...")
        print(f"  Searching {len(unmatched_tm)} TeslaMate session(s) against "
              f"{len(unmatched_api)} unmatched billing session(s)...")

        for tm in unmatched_tm:
            api, label = find_match(tm, unmatched_api, args.tolerance, require_vin=False)
            if api:
                tm_vin  = tm.get("vin", "?")
                api_vin = api.get("vin", "?")
                matches_p2.append({
                    "tm":         tm,
                    "api":        api,
                    "cost_new":   get_cost(api, use_net=args.net),
                    "currency":   get_currency(api),
                    "match_type": label,
                    "vin_note":   f"TM:{tm_vin[-8:]} ≠ API:{api_vin[-8:]}",
                })
                matched_api_ids.add(api.get("chargeSessionId"))
                # Remove from unmatched_api so it can't match twice
                unmatched_api = [
                    s for s in unmatched_api
                    if s.get("chargeSessionId") != api.get("chargeSessionId")
                ]
            else:
                still_unmatched_tm.append(tm)

        print(f"  Pass 2 result → cross-VIN matched: {len(matches_p2)}  |  "
              f"still unmatched: {len(still_unmatched_tm)}")
    else:
        still_unmatched_tm = unmatched_tm

    all_matches = matches_p1 + matches_p2

    # ── Step 4: Display ────────────────────────────────────────────────────────
    if matches_p1:
        print_match_table(matches_p1, use_net=args.net,
                          title=f"Pass 1 — VIN + timestamp ({len(matches_p1)} sessions)")
    else:
        print("\n  Pass 1: No VIN+time matches found.")

    if matches_p2:
        print(f"  ⚠  Cross-VIN matches below: TeslaMate VIN ≠ billing API VIN.")
        print(f"     These sessions were on your account but for a different tracked VIN.")
        print(f"     Verify the timestamps manually before applying.\n")
        print_match_table(matches_p2, use_net=args.net,
                          title=f"Pass 2 — timestamp only, cross-VIN ({len(matches_p2)} sessions)")

    print_unmatched(still_unmatched_tm, unmatched_api)

    # ── Step 5: Apply or dry-run report ───────────────────────────────────────
    if not args.apply:
        p1 = len(matches_p1)
        p2 = len(matches_p2)
        print(f"\n{'─'*65}")
        print(f"DRY RUN complete.")
        print(f"  {p1} session(s) would be updated  (VIN+time match)")
        if p2:
            print(f"  {p2} session(s) would be updated  (cross-VIN / timestamp only) ⚠")
        print(f"\nRe-run with --apply to write to the database.")
        conn.close()
        return

    # Actually write both pass 1 and pass 2 matches
    print(f"\n[db] Writing {len(all_matches)} cost update(s)...")
    updated = skipped = 0
    with conn.cursor() as cur:
        for m in all_matches:
            if m["cost_new"] is None:
                print(f"  — ID {m['tm']['id']} skipped (no cost / free charging)")
                skipped += 1
                continue
            cur.execute(UPDATE_COST, (m["cost_new"], m["tm"]["id"]))
            cur_cost = m["tm"].get("cost")
            tag = "  ⚠ cross-VIN" if m.get("match_type","").startswith("time") else ""
            print(f"  ✓ ID {m['tm']['id']:>10}  "
                  f"{str(m['tm']['start_date'])[:10]}  "
                  f"{m['api'].get('siteLocationName','?')[:28]:<28}  "
                  f"{m['currency']} {m['cost_new']:.2f}"
                  + (f"  (was {cur_cost:.2f})" if cur_cost is not None else "  (was null)")
                  + tag)
            updated += 1
    conn.commit()
    conn.close()

    print(f"\n[done] Updated: {updated}  |  Skipped (no cost data): {skipped}")
    print(f"[done] Refresh TeslaMate's Grafana dashboard to see the updated charging costs.")


if __name__ == "__main__":
    main()
