#!/usr/bin/env python3
"""
Agrisnellaad / EVC-Net ↔ TeslaMate — Charging Cost Sync
=========================================================
Fetches your non-Tesla public charging session data from agrisnellaad.evc-net.com
and fills in the cost column in TeslaMate's charging_processes table.

EVC-Net platform details (reverse-engineered from the platform + HA integration):
  Login   : POST https://agrisnellaad.evc-net.com/Login/Login
            fields: emailField, passwordField
            response: 302 → captures PHPSESSID + SERVERID cookies
  API     : POST https://agrisnellaad.evc-net.com/api/ajax
            payload: requests=<JSON with handler/method/params>
  Export  : GET  https://agrisnellaad.evc-net.com/Transactions/Export
            (or discovered automatically by scanning the Transactions page)

Matching to TeslaMate:
  EVC-Net sessions have NO VIN — matching is purely by start timestamp ±tolerance.
  Energy (kWh) is used as a secondary confidence check.
  Sessions are flagged ⚠ if kWh differs by more than 10% from TeslaMate.

Usage:
  # Auto-login + download + dry-run
  python scripts/sync_evc_costs.py

  # Use a manually downloaded Excel file (fallback)
  python scripts/sync_evc_costs.py --file ~/Downloads/transactions.xlsx

  # Write to TeslaMate DB
  python scripts/sync_evc_costs.py --apply

  # Widen time match window (default ±5 min)
  python scripts/sync_evc_costs.py --tolerance 10

  # Export all transactions since a date
  python scripts/sync_evc_costs.py --since 2024-01-01

Environment variables (or .env):
  EVC_EMAIL               your@email.com
  EVC_PASSWORD            yourpassword
  EVC_BASE_URL            https://agrisnellaad.evc-net.com  (default)
  TESLAMATE_DATABASE_URL  postgresql://teslamate:teslamate@localhost:5432/teslamate

Cron (daily at 6:30am, after Tesla sync):
  30 6 * * * cd ~/Desktop/Coding/ev-charge-cost-teslamate && .venv/bin/python scripts/sync_evc_costs.py --apply >> logs/sync_evc.log 2>&1

Requires: pip install requests openpyxl psycopg2-binary python-dotenv
"""

import argparse
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path

# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

import requests

try:
    import openpyxl
except ImportError:
    print("[error] openpyxl not installed. Run: pip install openpyxl")
    sys.exit(1)

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("[error] psycopg2 not installed. Run: pip install psycopg2-binary")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL  = "https://agrisnellaad.evc-net.com"
DEFAULT_DB_URL    = "postgresql://teslamate:teslamate@localhost:5432/teslamate"
DEFAULT_TOLERANCE = 5   # minutes
KWH_TOLERANCE_PCT = 0.15  # 15% — flag if kWh differs by more than this

LOGIN_PATH       = "/Login/Login"
AJAX_PATH        = "/api/ajax"
TRANSACTIONS_PATH= "/Transactions"

# Candidate export URL patterns to try (in order)
EXPORT_URL_CANDIDATES = [
    "/Transactions/Export",
    "/Transactions/ExportExcel",
    "/Transactions/ExcelExport",
    "/Transactions/Download",
    "/Report/Transactions/Export",
]

# Expected Excel columns (flexible — matched case-insensitively, partial match ok)
COL_MAP = {
    "start":    ["start date", "startdate", "start tijd", "start"],
    "stop":     ["end date",   "end date",  "stop",  "eind"],
    "kwh":      ["energy (kwh)", "energy",  "kwh",   "energie"],
    "cost":     ["costs",      "cost",      "prijs", "bedrag", "price excl", "price"],
    "currency": ["currency",   "valuta"],
    "location": ["location",   "locatie",   "charge point", "laadpunt"],
    "card":     ["card",       "kaart",     "badge"],
    "session":  ["id",         "session",   "transaction"],
}

# ---------------------------------------------------------------------------
# EVC-Net login
# ---------------------------------------------------------------------------

class EVCSession:
    """Authenticated session against an EVC-Net platform instance."""

    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.email    = email
        self.password = password
        self.session  = requests.Session()
        self.session.headers.update({
            "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })

    def login(self) -> bool:
        """POST credentials; capture PHPSESSID + SERVERID cookies. Returns True on success."""
        login_url = self.base_url + LOGIN_PATH
        print(f"[evc] Logging in to {login_url} ...")

        # First GET to pick up any initial cookies / CSRF tokens
        try:
            get_resp = self.session.get(login_url, timeout=20, allow_redirects=True)
        except requests.RequestException as exc:
            print(f"[evc] Could not reach login page: {exc}")
            return False

        # Look for hidden input tokens (CSRF)
        hidden = {}
        for m in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']', get_resp.text, re.I):
            hidden[m.group(1)] = m.group(2)
        for m in re.finditer(r'<input[^>]+name=["\']([^"\']+)["\'][^>]*type=["\']hidden["\'][^>]*value=["\']([^"\']*)["\']', get_resp.text, re.I):
            hidden[m.group(1)] = m.group(2)

        payload = {
            "emailField":    self.email,
            "passwordField": self.password,
            **hidden,
        }

        post_resp = self.session.post(
            login_url,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      login_url,
                "Origin":       self.base_url,
            },
            allow_redirects=False,  # capture cookies before redirect
            timeout=20,
        )

        # Follow redirect manually if needed
        if post_resp.status_code in (301, 302, 303):
            redirect = post_resp.headers.get("Location", "")
            if redirect and not redirect.startswith("http"):
                redirect = self.base_url + redirect
            if redirect:
                self.session.get(redirect, timeout=20)

        cookies = self.session.cookies.get_dict()
        has_session = "PHPSESSID" in cookies or "SERVERID" in cookies or len(cookies) > 0

        if not has_session:
            print(f"[evc] Login may have failed — no session cookies captured.")
            print(f"      Status: {post_resp.status_code}  Cookies: {cookies}")
            return False

        print(f"[evc] Logged in ✓  (cookies: {list(cookies.keys())})")
        return True

    def ajax(self, handler: str, method: str, params: dict = None) -> dict:
        """Call the EVC-Net AJAX API."""
        import json
        payload = {
            "requests": json.dumps({
                "handler": handler,
                "method":  method,
                "params":  params or {},
            })
        }
        resp = self.session.post(
            self.base_url + AJAX_PATH,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_export(self, since: str | None = None, until: str | None = None) -> bytes | None:
        """
        Try known export URL patterns and return raw bytes of the Excel file,
        or None if none worked.
        """
        params = {}
        if since:
            params["startDate"] = since
            params["StartDate"] = since
            params["start"]     = since
        if until:
            params["endDate"]   = until
            params["EndDate"]   = until
            params["end"]       = until

        for path in EXPORT_URL_CANDIDATES:
            url = self.base_url + path
            try:
                resp = self.session.get(url, params=params, timeout=30, allow_redirects=True)
                ct   = resp.headers.get("Content-Type", "")
                if resp.status_code == 200 and (
                    "excel" in ct or "spreadsheet" in ct
                    or "octet-stream" in ct or "zip" in ct
                    or resp.content[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")  # xlsx / xls magic
                ):
                    print(f"[evc] Export found at {url} ({len(resp.content):,} bytes)")
                    return resp.content
                else:
                    print(f"[evc] {path} → {resp.status_code} ({ct[:40]})")
            except requests.RequestException as exc:
                print(f"[evc] {path} → error: {exc}")

        return None

    def get_transactions_via_ajax(self, since: str | None = None) -> list[dict] | None:
        """
        Fallback: try to fetch transaction data via the AJAX API directly.
        Returns list of transaction dicts or None if not supported.
        """
        for handler in ["TransactionsAsyncService", "ChargingHistoryAsyncService",
                        "SessionAsyncService", "ReportAsyncService"]:
            for method in ["overview", "list", "getTransactions", "history"]:
                try:
                    params = {}
                    if since:
                        params["startDate"] = since
                    result = self.ajax(handler, method, params)
                    if isinstance(result, list) and result:
                        print(f"[evc] AJAX transactions via {handler}.{method}: {len(result)} records")
                        return result
                    if isinstance(result, dict) and result.get("data"):
                        data = result["data"]
                        if isinstance(data, list):
                            print(f"[evc] AJAX transactions via {handler}.{method}: {len(data)} records")
                            return data
                except Exception:
                    continue
        return None

# ---------------------------------------------------------------------------
# Excel parsing
# ---------------------------------------------------------------------------

def _find_col(headers: list[str], aliases: list[str]) -> int | None:
    """Find column index by matching header against a list of aliases (case-insensitive)."""
    for i, h in enumerate(headers):
        h_lower = str(h).lower().strip()
        for alias in aliases:
            if alias in h_lower:
                return i
    return None


def parse_excel(data: bytes) -> list[dict]:
    """
    Parse an EVC-Net Excel export into a list of session dicts.
    Handles both .xlsx and legacy .xls (via openpyxl read_only mode).
    """
    try:
        wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        print(f"[excel] Failed to open workbook: {exc}")
        return []

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        print("[excel] Workbook is empty.")
        return []

    # Find header row (first row with more than 3 non-empty cells)
    header_row_idx = 0
    for i, row in enumerate(rows):
        non_empty = sum(1 for c in row if c is not None)
        if non_empty >= 3:
            header_row_idx = i
            break

    headers = [str(c).strip() if c is not None else "" for c in rows[header_row_idx]]
    print(f"[excel] Headers found: {[h for h in headers if h]}")

    # Map columns
    col = {key: _find_col(headers, aliases) for key, aliases in COL_MAP.items()}
    missing = [k for k, v in col.items() if v is None and k in ("start", "cost")]
    if missing:
        print(f"[excel] Warning: could not locate columns: {missing}")
        print(f"        Available headers: {headers}")

    sessions = []
    for row in rows[header_row_idx + 1:]:
        if all(c is None for c in row):
            continue  # skip empty rows

        def cell(key):
            idx = col.get(key)
            return row[idx] if idx is not None and idx < len(row) else None

        start_raw = cell("start")
        cost_raw  = cell("cost")

        if start_raw is None:
            continue

        # Parse start datetime
        if isinstance(start_raw, datetime):
            start_dt = start_raw
        else:
            try:
                start_dt = datetime.fromisoformat(str(start_raw).strip())
            except (ValueError, TypeError):
                for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                            "%d-%m-%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
                    try:
                        start_dt = datetime.strptime(str(start_raw).strip(), fmt)
                        break
                    except ValueError:
                        pass
                else:
                    continue  # unparseable row

        # Parse cost
        cost = None
        if cost_raw is not None:
            try:
                cost = float(str(cost_raw).replace(",", ".").replace("€", "").strip())
            except (ValueError, TypeError):
                pass

        # Parse kWh
        kwh = None
        kwh_raw = cell("kwh")
        if kwh_raw is not None:
            try:
                kwh = float(str(kwh_raw).replace(",", ".").strip())
            except (ValueError, TypeError):
                pass

        stop_raw = cell("stop")
        stop_dt  = None
        if isinstance(stop_raw, datetime):
            stop_dt = stop_raw
        elif stop_raw:
            try:
                stop_dt = datetime.fromisoformat(str(stop_raw).strip())
            except (ValueError, TypeError):
                pass

        sessions.append({
            "start_dt":  start_dt,
            "stop_dt":   stop_dt,
            "kwh":       kwh,
            "cost":      cost,
            "currency":  cell("currency") or "EUR",
            "location":  cell("location"),
            "card":      cell("card"),
            "session_id":cell("session"),
        })

    print(f"[excel] Parsed {len(sessions)} session row(s)")
    return sessions

# ---------------------------------------------------------------------------
# TeslaMate DB
# ---------------------------------------------------------------------------

QUERY_TM = """
SELECT
    cp.id,
    cp.start_date,
    cp.end_date,
    cp.charge_energy_added,
    cp.cost,
    c.vin,
    c.name AS car_name
FROM charging_processes cp
JOIN cars c ON c.id = cp.car_id
{where}
ORDER BY cp.start_date DESC;
"""

UPDATE_COST = "UPDATE charging_processes SET cost = %s WHERE id = %s;"

def db_connect(url: str):
    try:
        conn = psycopg2.connect(url)
        conn.autocommit = False
        return conn
    except psycopg2.OperationalError as exc:
        print(f"[db] Connection failed: {exc}")
        print(f"[db] URL: {url}")
        print("[db] Tip: add '5432:5432' under 'database: ports:' in TeslaMate docker-compose.yml")
        sys.exit(1)

def fetch_tm_sessions(conn, overwrite: bool) -> list[dict]:
    where = "" if overwrite else "WHERE cp.cost IS NULL"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(QUERY_TM.format(where=where))
        return [dict(r) for r in cur.fetchall()]

# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def to_utc(dt) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def match_sessions(tm_sessions: list[dict], evc_sessions: list[dict],
                   tolerance_min: int) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Match TeslaMate sessions to EVC sessions by start timestamp (no VIN available).
    Also checks kWh similarity as a confidence indicator.

    Returns: (matches, unmatched_tm, unmatched_evc)
    """
    matched_evc = set()
    matches     = []
    unmatched_tm= []

    tolerance = timedelta(minutes=tolerance_min)

    for tm in tm_sessions:
        tm_start = to_utc(tm["start_date"])
        if not tm_start:
            unmatched_tm.append(tm)
            continue

        best      = None
        best_delta= timedelta.max

        for i, evc in enumerate(evc_sessions):
            if i in matched_evc:
                continue
            evc_start = to_utc(evc["start_dt"])
            if not evc_start:
                continue
            delta = abs(tm_start - evc_start)
            if delta <= tolerance and delta < best_delta:
                best       = (i, evc)
                best_delta = delta

        if best:
            idx, evc = best
            matched_evc.add(idx)

            # kWh confidence check
            tm_kwh  = tm.get("charge_energy_added") or 0
            evc_kwh = evc.get("kwh") or 0
            kwh_ok  = True
            if tm_kwh and evc_kwh:
                diff_pct = abs(tm_kwh - evc_kwh) / max(tm_kwh, evc_kwh)
                kwh_ok   = diff_pct <= KWH_TOLERANCE_PCT

            delta_s = best_delta.total_seconds()
            matches.append({
                "tm":       tm,
                "evc":      evc,
                "delta_m":  delta_s / 60,
                "kwh_ok":   kwh_ok,
                "tm_kwh":   tm_kwh,
                "evc_kwh":  evc_kwh,
            })
        else:
            unmatched_tm.append(tm)

    unmatched_evc = [evc for i, evc in enumerate(evc_sessions) if i not in matched_evc]
    return matches, unmatched_tm, unmatched_evc

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_match_table(matches: list[dict]):
    print(f"\n{'='*110}")
    print(f"  {'TM ID':>8}  {'Date':<12}  {'Car':<12}  {'Location':<28}  "
          f"{'TM kWh':>7}  {'EVC kWh':>7}  {'Δmin':>5}  {'Cost':>10}  {'Was':<8}  OK?")
    print(f"  {'─'*8}  {'─'*12}  {'─'*12}  {'─'*28}  "
          f"{'─'*7}  {'─'*7}  {'─'*5}  {'─'*10}  {'─'*8}  {'─'*4}")
    for m in matches:
        tm      = m["tm"]
        evc     = m["evc"]
        cost    = evc.get("cost")
        cur     = evc.get("currency") or "EUR"
        cost_s  = f"{cur} {cost:.2f}" if cost is not None else "---"
        was_s   = f"{tm.get('cost'):.2f}" if tm.get("cost") is not None else "null"
        car     = (tm.get("car_name") or tm.get("vin","")[-8:])[:10]
        loc     = str(evc.get("location") or "?")[:26]
        tm_kwh  = f"{m['tm_kwh']:.1f}" if m["tm_kwh"] else "---"
        evc_kwh = f"{m['evc_kwh']:.1f}" if m["evc_kwh"] else "---"
        ok      = "✓" if m["kwh_ok"] else "⚠ kWh"
        print(f"  {tm['id']:>8}  {str(tm['start_date'])[:10]:<12}  {car:<12}  "
              f"{loc:<28}  {tm_kwh:>7}  {evc_kwh:>7}  {m['delta_m']:>5.1f}  "
              f"{cost_s:>10}  {was_s:<8}  {ok}")
    print()

def print_unmatched(unmatched_tm, unmatched_evc):
    if unmatched_tm:
        print(f"\n[unmatched TeslaMate] {len(unmatched_tm)} session(s) — no EVC match:")
        for s in unmatched_tm[:10]:
            print(f"  {str(s['start_date'])[:16]}  {s.get('car_name','?'):<12}  "
                  f"kWh:{s.get('charge_energy_added') or '?'}")
        if len(unmatched_tm) > 10:
            print(f"  ... and {len(unmatched_tm)-10} more")
        print("  → Likely Supercharger sessions (handled by sync_teslamate_costs.py)")

    if unmatched_evc:
        print(f"\n[unmatched EVC] {len(unmatched_evc)} session(s) — not in TeslaMate:")
        for s in unmatched_evc[:10]:
            start = s["start_dt"].strftime("%Y-%m-%d %H:%M") if s.get("start_dt") else "?"
            print(f"  {start}  {str(s.get('location') or '?'):<30}  "
                  f"kWh:{s.get('kwh') or '?'}  {s.get('currency','EUR')} {s.get('cost') or '?'}")
        if len(unmatched_evc) > 10:
            print(f"  ... and {len(unmatched_evc)-10} more")
        print("  → Sessions before TeslaMate started, or TeslaMate wasn't running")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync EVC-Net (agrisnellaad) charging costs into TeslaMate"
    )
    parser.add_argument("--apply",     action="store_true",
                        help="Write costs to DB (default: dry-run)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Also update sessions that already have a cost")
    parser.add_argument("--file",      default=None, metavar="FILE",
                        help="Use a manually downloaded Excel file instead of auto-fetch")
    parser.add_argument("--since",     default=None, metavar="YYYY-MM-DD",
                        help="Fetch transactions from this date onwards (default: all)")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE,
                        help=f"Timestamp match tolerance in minutes (default: {DEFAULT_TOLERANCE})")
    parser.add_argument("--db",        default=None,
                        help="PostgreSQL URL (overrides TESLAMATE_DATABASE_URL)")
    args = parser.parse_args()

    db_url = args.db or os.getenv("TESLAMATE_DATABASE_URL", DEFAULT_DB_URL)
    base_url = os.getenv("EVC_BASE_URL", DEFAULT_BASE_URL)

    print(f"""
╔══════════════════════════════════════════════════════╗
║   EVC-Net (Agrisnellaad) ↔ TeslaMate Cost Sync      ║
╚══════════════════════════════════════════════════════╝
  Mode      : {"⚡ APPLY" if args.apply else "🔍 DRY RUN (add --apply to write)"}
  Source    : {"File: " + args.file if args.file else "Auto-fetch from " + base_url}
  Since     : {args.since or "all time"}
  Tolerance : ±{args.tolerance} min
  DB        : {db_url[:60]}
""")

    # ── Step 1: Get EVC session data ──────────────────────────────────────────
    evc_sessions: list[dict] = []

    if args.file:
        # Manual file path
        fpath = Path(args.file)
        if not fpath.exists():
            print(f"[error] File not found: {fpath}")
            sys.exit(1)
        print(f"[evc] Reading {fpath} ...")
        evc_sessions = parse_excel(fpath.read_bytes())

    else:
        # Auto-login + export
        email    = os.getenv("EVC_EMAIL")
        password = os.getenv("EVC_PASSWORD")
        if not email or not password:
            print("[error] EVC_EMAIL and EVC_PASSWORD must be set in .env")
            print("        Or use --file path/to/export.xlsx for a manual download")
            sys.exit(1)

        evc = EVCSession(base_url, email, password)
        if not evc.login():
            print("\n[evc] Login failed. Please check EVC_EMAIL / EVC_PASSWORD in .env")
            print("      You can also manually export from the website and use:")
            print("        python scripts/sync_evc_costs.py --file ~/Downloads/transactions.xlsx")
            sys.exit(1)

        # Try Excel export
        print(f"\n[evc] Attempting to download transaction export...")
        raw = evc.get_export(since=args.since)

        if raw:
            evc_sessions = parse_excel(raw)
        else:
            # Fallback: try AJAX
            print("[evc] Direct export not found — trying AJAX API...")
            ajax_data = evc.get_transactions_via_ajax(since=args.since)
            if ajax_data:
                # Convert AJAX response to our session format
                for item in ajax_data:
                    start_raw = item.get("startDate") or item.get("start") or item.get("StartDate")
                    cost_raw  = item.get("costs")    or item.get("cost")  or item.get("totalCosts")
                    kwh_raw   = item.get("energy")   or item.get("kwh")   or item.get("energyKwh")
                    if not start_raw:
                        continue
                    try:
                        start_dt = datetime.fromisoformat(str(start_raw))
                    except Exception:
                        continue
                    evc_sessions.append({
                        "start_dt":  start_dt,
                        "stop_dt":   None,
                        "kwh":       float(kwh_raw)  if kwh_raw  else None,
                        "cost":      float(cost_raw) if cost_raw else None,
                        "currency":  item.get("currency", "EUR"),
                        "location":  item.get("location") or item.get("chargePointId"),
                        "card":      item.get("card"),
                        "session_id":item.get("id"),
                    })
                print(f"[evc] {len(evc_sessions)} session(s) from AJAX")

        if not evc_sessions:
            print("\n[evc] ❌ Could not retrieve transaction data automatically.")
            print("\n  Manual steps:")
            print(f"  1. Go to {base_url}/Transactions")
            print(f"  2. Set the date range filter and click 'Export'")
            print(f"  3. Save the Excel file")
            print(f"  4. Re-run: python scripts/sync_evc_costs.py --file ~/Downloads/export.xlsx")
            sys.exit(1)

    if not evc_sessions:
        print("[evc] No sessions to process.")
        sys.exit(0)

    print(f"\n[evc] {len(evc_sessions)} EVC session(s) loaded")

    # ── Step 2: TeslaMate sessions ────────────────────────────────────────────
    print(f"\n[teslamate] Connecting...")
    conn = db_connect(db_url)
    tm_sessions = fetch_tm_sessions(conn, overwrite=args.overwrite)
    cost_status = "all" if args.overwrite else "cost IS NULL"
    print(f"[teslamate] {len(tm_sessions)} session(s) ({cost_status})")

    if not tm_sessions:
        print("[teslamate] Nothing to update.")
        conn.close()
        sys.exit(0)

    # ── Step 3: Match ─────────────────────────────────────────────────────────
    print(f"\n[match] Matching by timestamp (±{args.tolerance} min) + kWh check...")
    matches, unmatched_tm, unmatched_evc = match_sessions(
        tm_sessions, evc_sessions, args.tolerance
    )

    kwh_warnings = [m for m in matches if not m["kwh_ok"]]
    print(f"  Matched: {len(matches)}  |  TM unmatched: {len(unmatched_tm)}  |  "
          f"EVC unmatched: {len(unmatched_evc)}  |  kWh warnings: {len(kwh_warnings)}")

    # ── Step 4: Display ───────────────────────────────────────────────────────
    if matches:
        print_match_table(matches)
    else:
        print("\n  No matches found. Try --tolerance 10 or check that date ranges overlap.")

    if kwh_warnings:
        print(f"  ⚠  {len(kwh_warnings)} match(es) have >15% kWh difference between "
              f"TeslaMate and EVC — verify these manually.")

    print_unmatched(unmatched_tm, unmatched_evc)

    # ── Step 5: Apply / dry-run ───────────────────────────────────────────────
    if not args.apply:
        print(f"\n{'─'*60}")
        print(f"DRY RUN — {len(matches)} session(s) would be updated.")
        if kwh_warnings:
            print(f"          {len(kwh_warnings)} have kWh mismatches ⚠ — review before applying.")
        print(f"Add --apply to write to the database.")
        conn.close()
        return

    print(f"\n[db] Writing {len(matches)} cost update(s)...")
    updated = skipped = 0
    with conn.cursor() as cur:
        for m in matches:
            cost = m["evc"].get("cost")
            if cost is None:
                print(f"  — TM ID {m['tm']['id']} skipped (no cost in EVC data)")
                skipped += 1
                continue
            cur.execute(UPDATE_COST, (cost, m["tm"]["id"]))
            flag = "  ⚠ kWh mismatch" if not m["kwh_ok"] else ""
            was  = m["tm"].get("cost")
            print(f"  ✓ ID {m['tm']['id']:>8}  {str(m['tm']['start_date'])[:10]}  "
                  f"{str(m['evc'].get('location') or '?')[:28]:<28}  "
                  f"{m['evc'].get('currency','EUR')} {cost:.2f}"
                  + (f"  (was {was:.2f})" if was is not None else "  (was null)")
                  + flag)
            updated += 1

    conn.commit()
    conn.close()
    print(f"\n[done] Updated: {updated}  |  Skipped: {skipped}")
    print("[done] Refresh TeslaMate Grafana to see updated costs.")


if __name__ == "__main__":
    main()
