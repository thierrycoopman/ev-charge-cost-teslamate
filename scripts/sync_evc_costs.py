#!/usr/bin/env python3
"""
EVC-Net (Last Mile Solutions) ↔ TeslaMate — Charging Cost Sync
===============================================================
Fetches your non-Tesla public charging session data from any EVC-Net
white-label portal and fills in the cost column in TeslaMate's DB.

About EVC-Net:
  Last Mile Solutions operates a white-label charging management platform
  used by many European charge point operators. Every operator gets their
  own subdomain (e.g. agrisnellaad.evc-net.com, orangecharging.evc-net.com).
  The platform software is identical across all of them — same login path,
  same AJAX API, same Excel export format.

  There is NO central API. Credentials are per-portal (your agrisnellaad
  login does not work on 50five's portal, and vice versa).

Multi-network support:
  If you have charging cards on multiple EVC-Net networks, configure them
  all in evc_accounts.json and point EVC_ACCOUNTS_FILE at it. The script
  fetches from every network and merges the results before matching.

Single-network (simple / backward-compatible):
  Set EVC_EMAIL, EVC_PASSWORD and optionally EVC_BASE_URL in .env.

Usage:
  # Dry-run (no DB writes) — reads accounts from .env or EVC_ACCOUNTS_FILE
  python scripts/sync_evc_costs.py

  # Use a manually downloaded Excel file (bypasses auto-fetch)
  python scripts/sync_evc_costs.py --file ~/Downloads/transactions.xlsx

  # Write to TeslaMate DB
  python scripts/sync_evc_costs.py --apply

  # Run for one specific network only (when using accounts file)
  python scripts/sync_evc_costs.py --network agrisnellaad

  # Widen time match window (default ±5 min)
  python scripts/sync_evc_costs.py --tolerance 10

  # Only process transactions since a date
  python scripts/sync_evc_costs.py --since 2024-01-01

Environment variables (.env):
  # ── Single network (simple) ──────────────────────────────────────────────
  EVC_EMAIL               your@email.com
  EVC_PASSWORD            yourpassword
  EVC_BASE_URL            https://agrisnellaad.evc-net.com   (default)

  # ── Multiple networks (accounts file) ───────────────────────────────────
  EVC_ACCOUNTS_FILE       /app/evc_accounts.json
  # (see evc_accounts.example.json for the format)

  # ── TeslaMate ────────────────────────────────────────────────────────────
  TESLAMATE_DATABASE_URL  postgresql://teslamate:teslamate@localhost:5432/teslamate

Cron (daily at 6:30am, after Tesla sync):
  30 6 * * * cd ~/Desktop/Coding/ev-charge-cost-teslamate && .venv/bin/python scripts/sync_evc_costs.py --apply >> logs/sync_evc.log 2>&1

Requires: pip install requests openpyxl psycopg2-binary python-dotenv
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

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
DEFAULT_TOLERANCE = 5     # minutes
DEFAULT_CURRENCY  = "EUR"
KWH_TOLERANCE_PCT = 0.15  # flag if kWh differs by more than this

LOGIN_PATH        = "/Login/Login"
AJAX_PATH         = "/api/ajax"

# Pages to scrape for the export form / CSRF tokens (tried in order)
TRANSACTIONS_PAGES = [
    "/Transactions",
    "/Transactions/Index",
    "/MyTransactions",
    "/History",
]

# Candidate export URL patterns tried in order (same across all EVC-Net portals)
EXPORT_URL_CANDIDATES = [
    "/Transactions/Export",
    "/Transactions/ExportExcel",
    "/Transactions/ExcelExport",
    "/Transactions/Download",
    "/Report/Transactions/Export",
    "/Transactions/ExportCsv",
    "/Transactions/Excel",
    "/Export/Transactions",
]

# Known EVC-Net (Last Mile Solutions) white-label portals.
# All share the same platform — only the base URL differs.
# Full list in evc_accounts.example.json.
KNOWN_NETWORKS: dict[str, str] = {
    "agrisnellaad":            "https://agrisnellaad.evc-net.com",
    "50five-belux":            "https://50five-sbelux.evc-net.com",
    "50five-nl":               "https://50five-snl.evc-net.com",
    "50five-de":               "https://50five-sde.evc-net.com",
    "50five-uk":               "https://50five-suk.evc-net.com",
    "50five":                  "https://50five.evc-net.com",
    "orange-charging":         "https://orangecharging.evc-net.com",
    "chargewell":              "https://chargewell.evc-net.com",
    "chargekom":               "https://chargekom.evc-net.com",
    "oplaadpunten":            "https://oplaadpunten.evc-net.com",
    "electromobility-sol":     "https://electromobilitysolutions.evc-net.com",
    "indelec":                 "https://indelec.evc-net.com",
}

# Expected Excel columns — matched case-insensitively, partial match ok.
# Add aliases here if your portal uses different column names.
COL_MAP = {
    "start":    ["start date", "startdate", "start tijd", "start"],
    "stop":     ["end date",   "stop date", "stop",       "eind"],
    "kwh":      ["energy (kwh)", "energy",  "kwh",        "energie"],
    "cost":     ["costs",      "cost",      "prijs",      "bedrag", "price excl", "price"],
    "currency": ["currency",   "valuta"],
    "location": ["location",   "locatie",   "charge point", "laadpunt"],
    "card":     ["card",       "kaart",     "badge"],
    "session":  ["id",         "session",   "transaction"],
}

# ---------------------------------------------------------------------------
# Account loading
# ---------------------------------------------------------------------------

def load_accounts(accounts_file: str | None = None) -> list[dict]:
    """
    Load EVC-Net account configurations. Two modes:

    1. JSON accounts file (multi-network):
         EVC_ACCOUNTS_FILE=/app/evc_accounts.json
         Format: [{"name": "...", "base_url": "...", "email": "...", "password": "..."}]
         See evc_accounts.example.json for a full template.

    2. Environment variables (single network, backward-compatible):
         EVC_EMAIL, EVC_PASSWORD, EVC_BASE_URL

    Returns a list of account dicts, always with keys: name, base_url, email, password.
    """
    # ── Mode 1: accounts file ────────────────────────────────────────────────
    file_path = accounts_file or os.getenv("EVC_ACCOUNTS_FILE")
    if file_path:
        fp = Path(file_path)
        if not fp.exists():
            print(f"[evc] Accounts file not found: {fp}")
            sys.exit(1)
        with fp.open() as f:
            accounts = json.load(f)
        if not isinstance(accounts, list):
            print(f"[evc] Accounts file must contain a JSON array. Got: {type(accounts)}")
            sys.exit(1)
        for i, a in enumerate(accounts):
            for key in ("base_url", "email", "password"):
                if not a.get(key):
                    print(f"[evc] Account #{i+1} in {fp} is missing required field: '{key}'")
                    sys.exit(1)
            if not a.get("name"):
                a["name"] = urlparse(a["base_url"]).hostname.split(".")[0]
        print(f"[evc] Loaded {len(accounts)} account(s) from {fp}")
        return accounts

    # ── Mode 2: environment variables ────────────────────────────────────────
    email    = os.getenv("EVC_EMAIL")
    password = os.getenv("EVC_PASSWORD")
    base_url = os.getenv("EVC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")

    if email and password:
        name = urlparse(base_url).hostname.split(".")[0]
        return [{"name": name, "base_url": base_url, "email": email, "password": password}]

    return []

# ---------------------------------------------------------------------------
# EVC-Net session (login + export)
# ---------------------------------------------------------------------------

class EVCSession:
    """Authenticated session against one EVC-Net portal instance."""

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
        """
        Log in to the EVC-Net portal. Returns True on success.

        POST credentials to /Login/Login together with any hidden CSRF fields
        AND the submit button's name/value (required by the PHP backend).
        Verifies success by checking that the post-login page does NOT show
        the 'signed-out' body class.
        """
        login_url = self.base_url + LOGIN_PATH

        # GET the login page first — picks up PHPSESSID and hidden CSRF tokens
        try:
            get_resp = self.session.get(login_url, timeout=20, allow_redirects=True)
        except requests.RequestException as exc:
            print(f"  Could not reach login page: {exc}")
            return False

        # Extract all hidden <input> fields (CSRF tokens, etc.)
        hidden: dict[str, str] = {}
        for tag in re.findall(r"<input[^>]+>", get_resp.text, re.I):
            type_m  = re.search(r'type=["\']([^"\']+)["\']',  tag, re.I)
            name_m  = re.search(r'name=["\']([^"\']+)["\']',  tag, re.I)
            value_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
            if name_m and type_m and type_m.group(1).lower() == "hidden":
                hidden[name_m.group(1)] = value_m.group(1) if value_m else ""

        # Also collect submit buttons — PHP checks which button triggered the POST
        for tag in re.findall(r"<(?:input|button)[^>]+>", get_resp.text, re.I):
            type_m  = re.search(r'type=["\']([^"\']+)["\']',  tag, re.I)
            name_m  = re.search(r'name=["\']([^"\']+)["\']',  tag, re.I)
            value_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
            if name_m and type_m and type_m.group(1).lower() == "submit":
                hidden[name_m.group(1)] = value_m.group(1) if value_m else ""

        payload = {"emailField": self.email, "passwordField": self.password, **hidden}

        post_resp = self.session.post(
            login_url, data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer":      login_url,
                "Origin":       self.base_url,
            },
            allow_redirects=False, timeout=20,
        )

        # Follow redirect (302 on success, or back to login on failure).
        # The portal sometimes issues an HTTP redirect for the post-login page
        # even though the site runs on HTTPS.  Python's http.cookiejar refuses
        # to send `secure` cookies over plain HTTP, which breaks the session.
        # Force every redirect URL to HTTPS to avoid this.
        landed_html = ""
        if post_resp.status_code in (301, 302, 303):
            redirect = post_resp.headers.get("Location", "")
            if redirect and not redirect.startswith("http"):
                redirect = self.base_url + redirect
            # Upgrade http → https so the secure session cookie is sent
            if redirect.startswith("http://"):
                redirect = "https://" + redirect[7:]
            if redirect:
                landed = self.session.get(redirect, timeout=20, allow_redirects=True)
                landed_html = landed.text
                # If we landed back on the login page, credentials were wrong
                if "/Login/Login" in redirect or "login" in redirect.lower():
                    print("  Login failed — redirected back to login page "
                          "(check EVC_EMAIL / EVC_PASSWORD)")
                    return False
        else:
            landed_html = post_resp.text

        cookies = self.session.cookies.get_dict()
        if not cookies:
            print(f"  Login failed — no session cookies (HTTP {post_resp.status_code})")
            return False

        # Verify we're actually authenticated (portal puts 'signed-out' on body when not)
        if "signed-out" in landed_html and "signed-in" not in landed_html:
            print("  Login failed — session not authenticated "
                  "(portal shows signed-out page; check credentials)")
            return False

        print(f"  Logged in ✓  (cookies: {list(cookies.keys())})")
        return True

    @staticmethod
    def _is_xlsx(resp: "requests.Response") -> bool:
        """Return True if the response looks like an Excel/ZIP binary file."""
        ct = resp.headers.get("Content-Type", "").lower()
        return (
            "excel" in ct or "spreadsheet" in ct or
            "octet-stream" in ct or "zip" in ct or
            resp.content[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0")
        )

    @staticmethod
    def _parse_form(html: str) -> dict[str, str]:
        """
        Extract all form field values from an HTML fragment:
        - hidden inputs (CSRF tokens, etc.)
        - text/date inputs (their current value= attribute)
        - select elements (their selected option's value, or first option)
        Returns a flat name→value dict.
        """
        fields: dict[str, str] = {}

        # <input> fields (hidden, text, date, etc. — not submit/button/file)
        for tag in re.findall(r"<input[^>]+>", html, re.I):
            type_m  = re.search(r'type=["\']([^"\']+)["\']',  tag, re.I)
            name_m  = re.search(r'name=["\']([^"\']+)["\']',  tag, re.I)
            value_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
            if not name_m:
                continue
            t = (type_m.group(1).lower() if type_m else "text")
            if t not in ("submit", "button", "reset", "file", "image", "checkbox", "radio"):
                fields[name_m.group(1)] = value_m.group(1) if value_m else ""

        # <select> elements — use selected option or first option
        for sel_m in re.finditer(r"<select([^>]*)>(.*?)</select>", html,
                                  re.DOTALL | re.I):
            name_m = re.search(r'name=["\']([^"\']+)["\']', sel_m.group(1), re.I)
            if not name_m:
                continue
            name    = name_m.group(1)
            options = re.findall(r"<option([^>]*)>", sel_m.group(2), re.I)
            value   = ""
            for opt in options:
                val_m = re.search(r'value=["\']([^"\']*)["\']', opt, re.I)
                if "selected" in opt.lower() and val_m:
                    value = val_m.group(1)
                    break
            if not value and options:
                val_m = re.search(r'value=["\']([^"\']*)["\']', options[0], re.I)
                value = val_m.group(1) if val_m else ""
            fields[name] = value

        return fields

    @staticmethod
    def _detect_date_format(html: str) -> str:
        """
        Detect the date format used by the portal's date pickers.
        Looks for data-date-format attributes (e.g. 'DD-MM-YYYY', 'YYYY-MM-DD').
        Returns the detected format or 'YYYY-MM-DD' as default.
        """
        m = re.search(r'data-date-format=["\']([^"\']+)["\']', html, re.I)
        return m.group(1) if m else "YYYY-MM-DD"

    @staticmethod
    def _format_date(iso_date: str, fmt: str) -> str:
        """
        Convert a YYYY-MM-DD date string to the portal's date format.
        Supports: DD-MM-YYYY, MM-DD-YYYY, DD/MM/YYYY, MM/DD/YYYY, YYYY-MM-DD.
        """
        try:
            dt = datetime.strptime(iso_date, "%Y-%m-%d")
        except ValueError:
            return iso_date  # pass through if already formatted
        sep   = "-" if "-" in fmt else "/"
        upper = fmt.upper()
        if upper.startswith("DD"):
            return dt.strftime(f"%d{sep}%m{sep}%Y")
        if upper.startswith("MM"):
            return dt.strftime(f"%m{sep}%d{sep}%Y")
        return iso_date  # YYYY-MM-DD fallback

    def _load_transactions_page(self) -> tuple[str, str]:
        """
        GET the Transactions listing page (trying several candidate paths).
        Returns (page_html, final_url) or ("", "") on failure.
        """
        for path in TRANSACTIONS_PAGES:
            url = self.base_url + path
            try:
                resp = self.session.get(url, timeout=20, allow_redirects=True)
                # A real Transactions page is substantially larger than a login redirect
                if resp.status_code == 200 and len(resp.text) > 5_000:
                    print(f"  Transactions page: {path} → {resp.status_code} "
                          f"({len(resp.text):,} chars)")
                    return resp.text, resp.url
            except requests.RequestException:
                continue
        return "", ""

    def get_export(self, since: str | None = None, until: str | None = None,
                   debug_dir: "Path | None" = None) -> bytes | None:
        """
        Download the transaction export.

        Strategy:
        1. Load the Transactions page and detect the date format.
        2. Find every <form> on the page; for each form that has a submit button
           whose name or value contains 'export', build the full form payload and
           submit with that button active.  This covers portals where the export
           is a plain form field (e.g. exportButton=Export on /Transactions/List).
        3. Fall back to known GET/POST URL candidates for portals with a dedicated
           export endpoint.
        """
        # ── Load Transactions page ────────────────────────────────────────────
        page_html, page_url = self._load_transactions_page()

        if debug_dir and page_html:
            debug_path = Path(debug_dir) / "transactions_page.html"
            debug_path.write_text(page_html, encoding="utf-8")
            print(f"  [debug] Transactions page saved to {debug_path}")

        referer    = page_url or (self.base_url + "/Transactions")
        date_fmt   = self._detect_date_format(page_html) if page_html else "YYYY-MM-DD"
        today      = datetime.now().strftime("%Y-%m-%d")
        since_fmt  = self._format_date(since, date_fmt) if since else \
                     self._format_date("2000-01-01", date_fmt)
        until_fmt  = self._format_date(until or today, date_fmt)

        # ── Stage 1: scrape forms from the Transactions page ─────────────────
        if page_html:
            for form_m in re.finditer(r"<form([^>]*)>(.*?)</form>",
                                      page_html, re.DOTALL | re.I):
                form_attrs, form_body = form_m.group(1), form_m.group(2)

                # Collect all submit buttons in this form
                submit_buttons: list[tuple[str, str]] = []
                for tag in re.findall(r"<(?:input|button)[^>]+>", form_body, re.I):
                    type_m  = re.search(r'type=["\']([^"\']+)["\']',  tag, re.I)
                    name_m  = re.search(r'name=["\']([^"\']+)["\']',  tag, re.I)
                    value_m = re.search(r'value=["\']([^"\']*)["\']', tag, re.I)
                    if name_m and type_m and type_m.group(1).lower() == "submit":
                        submit_buttons.append((name_m.group(1),
                                               value_m.group(1) if value_m else ""))

                # Only care about forms that have an "export" submit button
                export_btn = next(
                    ((n, v) for n, v in submit_buttons
                     if "export" in n.lower() or "export" in v.lower()),
                    None,
                )
                if not export_btn:
                    continue

                # Build the full form payload
                form_data = self._parse_form(form_body)

                # Override date fields with the correct format for this portal
                for key in list(form_data.keys()):
                    key_lower = key.lower()
                    if "start" in key_lower or "from" in key_lower:
                        form_data[key] = since_fmt
                    elif "end" in key_lower or "to" in key_lower:
                        form_data[key] = until_fmt

                # Activate the export button (don't include other submit buttons)
                for n, v in submit_buttons:
                    form_data.pop(n, None)
                form_data[export_btn[0]] = export_btn[1]

                action_m = re.search(r'action=["\']([^"\']+)["\']', form_attrs, re.I)
                action   = action_m.group(1) if action_m else "/Transactions"
                action_url = action if action.startswith("http") \
                             else self.base_url + action

                method_m = re.search(r'method=["\'](\w+)["\']', form_attrs, re.I)
                use_post = method_m and method_m.group(1).upper() == "POST"

                print(f"  Submitting export form → {action} "
                      f"({'POST' if use_post else 'GET'})")
                try:
                    fn = self.session.post if use_post else self.session.get
                    resp = fn(
                        action_url,
                        data=form_data if use_post else None,
                        params=form_data if not use_post else None,
                        headers={"Referer": referer},
                        timeout=60, allow_redirects=True,
                    )
                    if resp.status_code == 200 and self._is_xlsx(resp):
                        print(f"  ✓ Export downloaded ({len(resp.content):,} bytes)")
                        return resp.content
                    ct = resp.headers.get("Content-Type", "")
                    print(f"  Form → {action} : {resp.status_code} ({ct[:80]})")
                except requests.RequestException as exc:
                    print(f"  Form → {action} : error: {exc}")

        # ── Stage 2: known export URL candidates (GET + POST) ─────────────────
        page_tokens = self._parse_form(page_html) if page_html else {}
        extra = {
            "startDateField": since_fmt, "endDateField": until_fmt,
            "startDate":      since_fmt, "endDate":      until_fmt,
            "exportButton":   "Export",
        }
        for path in EXPORT_URL_CANDIDATES:
            url = self.base_url + path
            for use_post in (False, True):
                payload = {**page_tokens, **extra}
                try:
                    fn   = self.session.post if use_post else self.session.get
                    resp = fn(url,
                              data=payload if use_post else None,
                              params=payload if not use_post else None,
                              headers={"Referer": referer},
                              timeout=60, allow_redirects=True)
                    if resp.status_code == 200 and self._is_xlsx(resp):
                        verb = "POST" if use_post else "GET"
                        print(f"  ✓ Export via {verb} → {path} "
                              f"({len(resp.content):,} bytes)")
                        return resp.content
                    if not use_post:
                        ct = resp.headers.get("Content-Type", "")
                        print(f"  GET {path} → {resp.status_code} ({ct[:60]})")
                except requests.RequestException as exc:
                    if not use_post:
                        print(f"  GET {path} → error: {exc}")

        return None

    def get_transactions_via_ajax(self, since: str | None = None) -> list[dict] | None:
        """Fallback: attempt to retrieve transactions via the AJAX API."""
        handlers = [
            "TransactionsAsyncService", "ChargingHistoryAsyncService",
            "SessionAsyncService",      "ReportAsyncService",
        ]
        methods = ["overview", "list", "getTransactions", "history", "export"]
        for handler in handlers:
            for method in methods:
                try:
                    params  = {"startDate": since} if since else {}
                    payload = {
                        "requests": json.dumps(
                            {"handler": handler, "method": method, "params": params}
                        )
                    }
                    resp = self.session.post(
                        self.base_url + AJAX_PATH,
                        data=payload,
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "X-Requested-With": "XMLHttpRequest"},
                        timeout=30,
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    data   = result if isinstance(result, list) else result.get("data", [])
                    if isinstance(data, list) and data:
                        print(f"  AJAX: {handler}.{method} → {len(data)} record(s)")
                        return data
                except Exception:
                    continue
        return None

# ---------------------------------------------------------------------------
# Session fetching (per account)
# ---------------------------------------------------------------------------

def _parse_ajax_data(items: list[dict]) -> list[dict]:
    """Convert raw AJAX response items to our normalised session dict format."""
    sessions = []
    for item in items:
        start_raw = item.get("startDate") or item.get("start") or item.get("StartDate")
        cost_raw  = item.get("costs")     or item.get("cost")  or item.get("totalCosts")
        kwh_raw   = item.get("energy")    or item.get("kwh")   or item.get("energyKwh")
        if not start_raw:
            continue
        try:
            start_dt = datetime.fromisoformat(str(start_raw))
        except Exception:
            continue
        sessions.append({
            "start_dt":  start_dt,
            "stop_dt":   None,
            "kwh":       parse_float(kwh_raw),
            "cost":      parse_float(cost_raw),
            "currency":  item.get("currency", DEFAULT_CURRENCY),
            "location":  item.get("location") or item.get("chargePointId"),
            "card":      item.get("card"),
            "session_id":item.get("id"),
        })
    return sessions


def fetch_sessions_for_account(account: dict, since: str | None,
                               debug_dir: "Path | None" = None) -> list[dict]:
    """
    Log in to one EVC-Net portal and fetch all transaction sessions.
    Returns a list of session dicts, each tagged with 'network' = account name.
    On failure, logs a warning and returns [].

    Pass debug_dir to save the Transactions page HTML for manual inspection
    when the automatic export cannot be found.
    """
    name     = account["name"]
    base_url = account["base_url"]

    print(f"\n  ┌── {name}  ({base_url})")

    evc = EVCSession(base_url, account["email"], account["password"])
    if not evc.login():
        print(f"  └── ⚠  Login failed — skipping. Check credentials for '{name}'.")
        return []

    raw = evc.get_export(since=since, debug_dir=debug_dir)
    if raw:
        sessions = parse_excel(raw, label=name)
    else:
        print(f"  No Excel export found — trying AJAX API...")
        ajax_raw = evc.get_transactions_via_ajax(since=since)
        if ajax_raw:
            sessions = _parse_ajax_data(ajax_raw)
            print(f"  {len(sessions)} session(s) from AJAX")
        else:
            print(f"  └── ❌ Could not retrieve data automatically.")
            if debug_dir:
                print(f"      [debug] Transactions page HTML saved to {debug_dir}/")
                print(f"              Inspect it to find the export form/button.")
            print(f"      Manual fallback:")
            print(f"        1. Go to {base_url}/Transactions → set date range → Export")
            print(f"        2. Save the .xlsx file")
            print(f"        3. Re-run: python scripts/sync_evc_costs.py --file ~/Downloads/export.xlsx")
            return []

    # Tag every session with the network name so the output table is clear
    for s in sessions:
        s["network"] = name

    print(f"  └── ✓  {len(sessions)} session(s) retrieved from {name}")
    return sessions

# ---------------------------------------------------------------------------
# Excel parsing
# ---------------------------------------------------------------------------

def parse_float(raw) -> float | None:
    """Parse a numeric cell value that may use commas, currency symbols, or spaces."""
    if raw is None:
        return None
    try:
        return float(str(raw).replace(",", ".").replace("€", "").strip())
    except (ValueError, TypeError):
        return None


def _find_col(headers: list[str], aliases: list[str]) -> int | None:
    for i, h in enumerate(headers):
        h_lower = str(h).lower().strip()
        for alias in aliases:
            if alias in h_lower:
                return i
    return None


def parse_excel(data: bytes, label: str = "") -> list[dict]:
    """
    Parse an EVC-Net Excel export into a list of session dicts.
    Handles .xlsx (and attempts .xls via openpyxl compatibility).
    Column detection is flexible: case-insensitive, partial match, Dutch/English.
    """
    tag = f"[excel:{label}]" if label else "[excel]"
    try:
        wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        print(f"{tag} Failed to open workbook: {exc}")
        return []

    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        print(f"{tag} Workbook is empty.")
        return []

    # First row with ≥3 non-empty cells is the header
    header_row_idx = next(
        (i for i, row in enumerate(rows) if sum(c is not None for c in row) >= 3), 0
    )
    headers = [str(c).strip() if c is not None else "" for c in rows[header_row_idx]]
    print(f"{tag} Headers: {[h for h in headers if h]}")

    col     = {key: _find_col(headers, aliases) for key, aliases in COL_MAP.items()}
    missing = [k for k, v in col.items() if v is None and k in ("start", "cost")]
    if missing:
        print(f"{tag} ⚠ Could not find columns: {missing}  (available: {headers})")

    def cell(row, key):
        idx = col.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    sessions = []
    for row in rows[header_row_idx + 1:]:
        if all(c is None for c in row):
            continue

        start_raw = cell(row, "start")
        if start_raw is None:
            continue

        # Parse start datetime — try several formats
        if isinstance(start_raw, datetime):
            start_dt = start_raw
        else:
            start_dt = None
            try:
                start_dt = datetime.fromisoformat(str(start_raw).strip())
            except (ValueError, TypeError):
                for fmt in ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S",
                            "%d-%m-%Y %H:%M:%S", "%m/%d/%Y %H:%M"):
                    try:
                        start_dt = datetime.strptime(str(start_raw).strip(), fmt)
                        break
                    except ValueError:
                        continue
            if start_dt is None:
                continue

        stop_raw = cell(row, "stop")
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
            "kwh":       parse_float(cell(row, "kwh")),
            "cost":      parse_float(cell(row, "cost")),
            "currency":  cell(row, "currency") or DEFAULT_CURRENCY,
            "location":  cell(row, "location"),
            "card":      cell(row, "card"),
            "session_id":cell(row, "session"),
        })

    print(f"{tag} Parsed {len(sessions)} session(s)")
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
        print(f"[db] URL tried: {url}")
        print("[db] If running locally: expose port 5432 in TeslaMate's docker-compose.yml")
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


def match_sessions(
    tm_sessions: list[dict], evc_sessions: list[dict], tolerance_min: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Match TeslaMate rows to EVC sessions by start timestamp (no VIN in EVC data).
    kWh is used as a secondary confidence indicator (flagged if >15% difference).
    Returns: (matches, unmatched_tm, unmatched_evc)
    """
    # Pre-compute EVC UTC timestamps once — avoids recomputing for every TM session
    evc_starts  = [(i, to_utc(e["start_dt"]), e) for i, e in enumerate(evc_sessions)]
    matched_evc = set()
    matches      = []
    unmatched_tm = []
    tolerance    = timedelta(minutes=tolerance_min)

    for tm in tm_sessions:
        tm_start  = to_utc(tm["start_date"])
        if not tm_start:
            unmatched_tm.append(tm)
            continue

        best       = None
        best_delta = timedelta.max

        for i, evc_start, evc in evc_starts:
            if i in matched_evc or evc_start is None:
                continue
            delta = abs(tm_start - evc_start)
            if delta <= tolerance and delta < best_delta:
                best       = (i, evc)
                best_delta = delta

        if best:
            idx, evc = best
            matched_evc.add(idx)
            tm_kwh   = tm.get("charge_energy_added") or 0
            evc_kwh  = evc.get("kwh") or 0
            kwh_ok   = True
            if tm_kwh and evc_kwh:
                kwh_ok = abs(tm_kwh - evc_kwh) / max(tm_kwh, evc_kwh) <= KWH_TOLERANCE_PCT
            matches.append({
                "tm": tm, "evc": evc,
                "delta_m": best_delta.total_seconds() / 60,
                "kwh_ok": kwh_ok, "tm_kwh": tm_kwh, "evc_kwh": evc_kwh,
            })
        else:
            unmatched_tm.append(tm)

    unmatched_evc = [evc for i, evc in enumerate(evc_sessions) if i not in matched_evc]
    return matches, unmatched_tm, unmatched_evc

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_match_table(matches: list[dict]):
    print(f"\n{'='*120}")
    print(f"  {'TM ID':>8}  {'Date':<12}  {'Car':<10}  {'Network':<16}  {'Location':<24}  "
          f"{'TM kWh':>7}  {'EVC kWh':>7}  {'Δmin':>5}  {'Cost':>10}  {'Was':<8}  OK?")
    print(f"  {'─'*8}  {'─'*12}  {'─'*10}  {'─'*16}  {'─'*24}  "
          f"{'─'*7}  {'─'*7}  {'─'*5}  {'─'*10}  {'─'*8}  {'─'*4}")
    for m in matches:
        tm      = m["tm"]
        evc     = m["evc"]
        cost    = evc.get("cost")
        cur     = evc.get("currency") or DEFAULT_CURRENCY
        cost_s  = f"{cur} {cost:.2f}" if cost is not None else "---"
        was_s   = f"{tm.get('cost'):.2f}" if tm.get("cost") is not None else "null"
        car     = (tm.get("car_name") or tm.get("vin", "")[-8:])[:8]
        net     = str(evc.get("network") or "")[:14]
        loc     = str(evc.get("location") or "?")[:22]
        tm_kwh  = f"{m['tm_kwh']:.1f}" if m["tm_kwh"] else "---"
        evc_kwh = f"{m['evc_kwh']:.1f}" if m["evc_kwh"] else "---"
        ok      = "✓" if m["kwh_ok"] else "⚠ kWh"
        print(f"  {tm['id']:>8}  {str(tm['start_date'])[:10]:<12}  {car:<10}  {net:<16}  "
              f"{loc:<24}  {tm_kwh:>7}  {evc_kwh:>7}  {m['delta_m']:>5.1f}  "
              f"{cost_s:>10}  {was_s:<8}  {ok}")
    print()


def print_unmatched(unmatched_tm: list, unmatched_evc: list):
    if unmatched_tm:
        print(f"\n[unmatched TeslaMate] {len(unmatched_tm)} session(s) — no EVC match found:")
        for s in unmatched_tm[:10]:
            print(f"  {str(s['start_date'])[:16]}  {s.get('car_name','?'):<12}  "
                  f"kWh:{s.get('charge_energy_added') or '?'}")
        if len(unmatched_tm) > 10:
            print(f"  … and {len(unmatched_tm)-10} more")
        print("  → Likely Supercharger sessions (handled by sync_teslamate_costs.py)")

    if unmatched_evc:
        print(f"\n[unmatched EVC] {len(unmatched_evc)} session(s) — not found in TeslaMate:")
        for s in unmatched_evc[:10]:
            start = s["start_dt"].strftime("%Y-%m-%d %H:%M") if s.get("start_dt") else "?"
            net   = s.get("network", "")
            print(f"  {start}  [{net:<14}]  {str(s.get('location') or '?'):<28}  "
                  f"kWh:{s.get('kwh') or '?':>6}  {s.get('currency','EUR')} {s.get('cost') or '?'}")
        if len(unmatched_evc) > 10:
            print(f"  … and {len(unmatched_evc)-10} more")
        print("  → Sessions before TeslaMate started, or TeslaMate wasn't tracking at the time")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Sync EVC-Net charging costs into TeslaMate's DB"
    )
    parser.add_argument("--apply",     action="store_true",
                        help="Write costs to DB (default: dry-run)")
    parser.add_argument("--overwrite", action="store_true",
                        help="Also update sessions that already have a cost")
    parser.add_argument("--file",      default=None, metavar="FILE",
                        help="Use a manually downloaded Excel file (skips auto-fetch)")
    parser.add_argument("--accounts",  default=None, metavar="FILE",
                        help="JSON accounts file (overrides EVC_ACCOUNTS_FILE env var)")
    parser.add_argument("--network",   default=None, metavar="NAME",
                        help="Run for one named network only (when using accounts file)")
    parser.add_argument("--since",     default=None, metavar="YYYY-MM-DD",
                        help="Only process transactions from this date onwards")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE,
                        help=f"Timestamp match window in minutes (default: {DEFAULT_TOLERANCE})")
    parser.add_argument("--db",        default=None,
                        help="PostgreSQL URL (overrides TESLAMATE_DATABASE_URL)")
    parser.add_argument("--list-networks", action="store_true",
                        help="Print all known EVC-Net network URLs and exit")
    parser.add_argument("--debug", action="store_true",
                        help="Save Transactions page HTML to ./logs/ to help diagnose export failures")
    args = parser.parse_args()

    # ── --list-networks ───────────────────────────────────────────────────────
    if args.list_networks:
        print("\nKnown EVC-Net (Last Mile Solutions) networks:\n")
        for name, url in KNOWN_NETWORKS.items():
            print(f"  {name:<28}  {url}")
        print("\nAdd any of these to your evc_accounts.json with your credentials.")
        print("Note: credentials are per-portal — each network has its own user database.\n")
        return

    db_url = args.db or os.getenv("TESLAMATE_DATABASE_URL", DEFAULT_DB_URL)

    print(f"""
╔══════════════════════════════════════════════════════╗
║      EVC-Net ↔ TeslaMate Cost Sync                  ║
╚══════════════════════════════════════════════════════╝
  Mode      : {"⚡ APPLY" if args.apply else "🔍 DRY RUN (add --apply to write)"}
  Since     : {args.since or "all time"}
  Tolerance : ±{args.tolerance} min
  DB        : {db_url[:60]}
""")

    # ── Step 1: Collect EVC sessions ──────────────────────────────────────────
    evc_sessions: list[dict] = []

    if args.file:
        fpath = Path(args.file)
        if not fpath.exists():
            print(f"[error] File not found: {fpath}")
            sys.exit(1)
        print(f"[evc] Reading {fpath} ...")
        evc_sessions = parse_excel(fpath.read_bytes())

    else:
        accounts = load_accounts(args.accounts)

        if not accounts:
            print("[error] No EVC-Net accounts configured.\n")
            print("  Option 1 — single network via .env:")
            print("    EVC_EMAIL=your@email.com")
            print("    EVC_PASSWORD=yourpassword")
            print("    EVC_BASE_URL=https://agrisnellaad.evc-net.com  (or other network)")
            print("")
            print("  Option 2 — multiple networks via accounts file:")
            print("    cp evc_accounts.example.json evc_accounts.json")
            print("    # fill in your credentials")
            print("    EVC_ACCOUNTS_FILE=/path/to/evc_accounts.json")
            print("")
            print("  Option 3 — manual Excel export:")
            print("    python scripts/sync_evc_costs.py --file ~/Downloads/export.xlsx")
            print("")
            print("  List all known EVC-Net networks:")
            print("    python scripts/sync_evc_costs.py --list-networks")
            sys.exit(1)

        if args.network:
            accounts = [a for a in accounts if a["name"] == args.network]
            if not accounts:
                print(f"[error] No account named '{args.network}' in the accounts file.")
                sys.exit(1)

        debug_dir = None
        if args.debug:
            debug_dir = Path(__file__).parent.parent / "logs"
            debug_dir.mkdir(exist_ok=True)
            print(f"[debug] Transactions page HTML will be saved to {debug_dir}/")

        print(f"[evc] Fetching from {len(accounts)} network(s) in parallel...")
        with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
            futures = {
                pool.submit(fetch_sessions_for_account, acct, args.since, debug_dir): acct["name"]
                for acct in accounts
            }
            for future in as_completed(futures):
                evc_sessions.extend(future.result())

        if not evc_sessions:
            print("\n[evc] No sessions retrieved from any network.")
            sys.exit(1)

    print(f"\n[evc] {len(evc_sessions)} total EVC session(s) loaded")

    # ── Step 2: TeslaMate sessions ────────────────────────────────────────────
    print(f"\n[teslamate] Connecting...")
    conn = db_connect(db_url)
    tm_sessions = fetch_tm_sessions(conn, overwrite=args.overwrite)
    label = "all (--overwrite)" if args.overwrite else "cost IS NULL"
    print(f"[teslamate] {len(tm_sessions)} session(s) fetched ({label})")

    if not tm_sessions:
        print("[teslamate] Nothing to update.")
        conn.close()
        sys.exit(0)

    # ── Step 3: Match ─────────────────────────────────────────────────────────
    print(f"\n[match] Matching by timestamp (±{args.tolerance} min) + kWh confidence...")
    matches, unmatched_tm, unmatched_evc = match_sessions(
        tm_sessions, evc_sessions, args.tolerance
    )

    kwh_warnings = [m for m in matches if not m["kwh_ok"]]
    print(f"  Matched: {len(matches)}  |  "
          f"TM unmatched: {len(unmatched_tm)}  |  "
          f"EVC unmatched: {len(unmatched_evc)}  |  "
          f"kWh warnings: {len(kwh_warnings)}")

    # ── Step 4: Display ───────────────────────────────────────────────────────
    if matches:
        print_match_table(matches)
    else:
        print("\n  No matches found.")
        print("  Try: --tolerance 10  or check that date ranges overlap between EVC export and TeslaMate.")

    if kwh_warnings:
        print(f"  ⚠  {len(kwh_warnings)} match(es) have >15% kWh difference — verify manually before applying.")

    print_unmatched(unmatched_tm, unmatched_evc)

    # ── Step 5: Apply or dry-run summary ─────────────────────────────────────
    if not args.apply:
        print(f"\n{'─'*60}")
        print(f"DRY RUN — {len(matches)} session(s) would be updated.")
        if kwh_warnings:
            print(f"          {len(kwh_warnings)} have kWh mismatch ⚠ — review before applying.")
        print("Add --apply to write to the database.")
        conn.close()
        return

    print(f"\n[db] Writing {len(matches)} cost update(s)...")
    updated = skipped = 0
    with conn.cursor() as cur:
        for m in matches:
            cost = m["evc"].get("cost")
            if cost is None:
                print(f"  — TM ID {m['tm']['id']} skipped (no cost value in EVC data)")
                skipped += 1
                continue
            cur.execute(UPDATE_COST, (cost, m["tm"]["id"]))
            was  = m["tm"].get("cost")
            flag = "  ⚠ kWh mismatch" if not m["kwh_ok"] else ""
            net  = m["evc"].get("network", "")
            print(f"  ✓ ID {m['tm']['id']:>8}  {str(m['tm']['start_date'])[:10]}  "
                  f"[{net:<14}]  {str(m['evc'].get('location') or '?')[:24]:<24}  "
                  f"{m['evc'].get('currency','EUR')} {cost:.2f}"
                  + (f"  (was {was:.2f})" if was is not None else "  (was null)")
                  + flag)
            updated += 1

    conn.commit()
    conn.close()
    print(f"\n[done] Updated: {updated}  |  Skipped (no cost): {skipped}")
    print("[done] Refresh TeslaMate → Grafana to see the updated costs.")


if __name__ == "__main__":
    main()
