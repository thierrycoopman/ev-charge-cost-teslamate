#!/usr/bin/env python3
"""
Tesla Private API - Authentication Helper
=========================================
Handles OAuth2 + PKCE authentication against Tesla's SSO service (auth.tesla.com).
Stores tokens in a local cache file and auto-refreshes when expired.

Tesla uses the Authorization Code flow with PKCE:
  1. Generate code_verifier + code_challenge
  2. Open browser to auth.tesla.com/oauth2/v3/authorize
  3. User logs in → redirected to https://auth.tesla.com/void/callback?code=...
  4. Exchange code for access_token + refresh_token
  5. Cache tokens; auto-refresh via refresh_token when expired

Usage:
  python auth.py                  # Interactive login
  python auth.py --refresh        # Force-refresh using cached refresh_token
  python auth.py --show           # Print current cached token (masked)
  python auth.py --reuse-teslamate # Extract tokens from a running TeslaMate instance

Requires: pip install requests requests-oauthlib cryptography
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Token cache location — override with TESLA_TOKEN_FILE env var.
# The Docker container sets this to /app/data/tesla_tokens.json (mounted volume).
CACHE_FILE = Path(os.getenv("TESLA_TOKEN_FILE", str(Path.home() / ".tesla_tokens.json")))

AUTH_BASE    = "https://auth.tesla.com"
TOKEN_URL    = f"{AUTH_BASE}/oauth2/v3/token"
AUTH_URL     = f"{AUTH_BASE}/oauth2/v3/authorize"
CALLBACK_URL = f"{AUTH_BASE}/void/callback"

# Client credentials used by the Tesla mobile app (reverse-engineered, public knowledge)
CLIENT_ID     = "ownerapi"
# The Fleet API client ID (for partner apps — leave blank unless you have one)
FLEET_CLIENT_ID = os.getenv("TESLA_CLIENT_ID", "")

# Owner API base URL
OWNER_API_BASE = "https://owner-api.teslamotors.com"

# Scopes requested (same as the mobile app)
SCOPES = "openid email offline_access"

# User-Agent strings that mimic the Tesla mobile app
HEADERS = {
    "User-Agent": "Tesla/1195 CFNetwork/1388 Darwin/22.0.0",
    "x-tesla-user-agent": "TeslaApp/4.30.6/ios/17.0",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair():
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(86)  # ~128 bytes of entropy, URL-safe
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _random_state():
    return secrets.token_urlsafe(16)

# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(data: dict):
    CACHE_FILE.write_text(json.dumps(data, indent=2))
    CACHE_FILE.chmod(0o600)  # Only owner can read/write
    print(f"[auth] Tokens saved to {CACHE_FILE}")


def _is_expired(cache: dict, margin_seconds: int = 300) -> bool:
    """Return True if the access token will expire within `margin_seconds`."""
    expires_at = cache.get("expires_at", 0)
    return time.time() + margin_seconds >= expires_at

# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------

def build_auth_url(verifier: str, challenge: str, state: str) -> str:
    """Build the Tesla SSO authorization URL."""
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_tokens(code: str, verifier: str) -> dict:
    """Exchange an authorization code for access + refresh tokens."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": CALLBACK_URL,
    }
    resp = requests.post(TOKEN_URL, json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data


def refresh_tokens(refresh_token: str) -> dict:
    """Use a refresh token to get new access + refresh tokens."""
    payload = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": refresh_token,
    }
    resp = requests.post(TOKEN_URL, json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    data["expires_at"] = time.time() + data.get("expires_in", 3600)
    return data

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def interactive_login() -> dict:
    """
    Full interactive OAuth2 + PKCE login flow.
    Opens a browser, prompts for the callback URL, and returns tokens.
    """
    verifier, challenge = _pkce_pair()
    state = _random_state()
    auth_url = build_auth_url(verifier, challenge, state)

    print("\n[auth] Tesla login URL:")
    print(f"\n  {auth_url}\n")

    # Try to open the browser — silently skip if running headless (e.g. inside Docker)
    try:
        opened = webbrowser.open(auth_url)
        if opened:
            print("[auth] Browser opened automatically.")
        else:
            print("[auth] Could not open browser — copy the URL above into your browser manually.")
    except Exception:
        print("[auth] Running headless — copy the URL above into your browser.")

    print("")
    print("Log in to your Tesla account. You will be redirected to a page that")
    print("says 'Page Not Found' — that is expected. Copy the full URL from your")
    print("browser's address bar (it starts with https://auth.tesla.com/void/callback)")
    print("and paste it below.\n")
    callback_raw = input("Callback URL: ").strip()

    parsed = urllib.parse.urlparse(callback_raw)
    params = urllib.parse.parse_qs(parsed.query)

    if "error" in params:
        raise RuntimeError(f"Auth error: {params['error'][0]} — {params.get('error_description', [''])[0]}")

    if "state" not in params or params["state"][0] != state:
        raise RuntimeError("State mismatch — possible CSRF attack. Aborting.")

    code = params["code"][0]
    print(f"\n[auth] Got authorization code. Exchanging for tokens...")

    tokens = exchange_code_for_tokens(code, verifier)
    _save_cache(tokens)
    print("[auth] Login successful!\n")
    return tokens


def get_valid_tokens() -> dict:
    """
    Return valid tokens, refreshing if necessary.
    Raises RuntimeError if no cached tokens exist.
    """
    cache = _load_cache()
    if not cache:
        raise RuntimeError(
            "No cached tokens found. Run `python auth.py` to log in first."
        )

    if _is_expired(cache):
        print("[auth] Access token expired, refreshing...")
        try:
            new_tokens = refresh_tokens(cache["refresh_token"])
            _save_cache(new_tokens)
            return new_tokens
        except requests.HTTPError as exc:
            raise RuntimeError(
                f"Token refresh failed: {exc}. Re-run `python auth.py` to log in again."
            ) from exc

    return cache


def get_access_token() -> str:
    """Convenience: return just the access token string."""
    return get_valid_tokens()["access_token"]


def authed_session() -> requests.Session:
    """Return a requests.Session pre-configured with the Bearer token + Tesla headers."""
    token = get_access_token()
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers["Authorization"] = f"Bearer {token}"
    return session

# ---------------------------------------------------------------------------
# TeslaMate token reuse
# ---------------------------------------------------------------------------

def import_from_teslamate():
    """
    Try to extract tokens from a running TeslaMate PostgreSQL database.
    Requires psycopg2: pip install psycopg2-binary

    TeslaMate stores tokens in the `settings` table:
      SELECT access_token, refresh_token FROM settings LIMIT 1;
    """
    try:
        import psycopg2
    except ImportError:
        print("[teslamate] psycopg2 not installed. Run: pip install psycopg2-binary")
        return None

    db_url = os.getenv(
        "TESLAMATE_DATABASE_URL",
        "postgresql://teslamate:teslamate@localhost:5432/teslamate"
    )
    print(f"[teslamate] Connecting to: {db_url}")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT access_token, refresh_token FROM settings LIMIT 1;")
        row = cur.fetchone()
        conn.close()

        if not row:
            print("[teslamate] No tokens found in settings table.")
            return None

        access_token, refresh_token = row
        tokens = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            # TeslaMate doesn't store expiry, so set a short window to force refresh
            "expires_at": time.time() + 60,
            "expires_in": 60,
        }
        _save_cache(tokens)
        print("[teslamate] Tokens imported from TeslaMate. Will refresh on next use.")
        return tokens

    except Exception as exc:
        print(f"[teslamate] Failed to import tokens: {exc}")
        return None

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Tesla OAuth2 authentication helper"
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Force-refresh the cached access token using the refresh token"
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print the current cached token info (access token is masked)"
    )
    parser.add_argument(
        "--reuse-teslamate", action="store_true",
        help="Import tokens from a running TeslaMate PostgreSQL database"
    )
    args = parser.parse_args()

    if args.show:
        cache = _load_cache()
        if not cache:
            print("No cached tokens found.")
            sys.exit(1)
        expires_at = cache.get("expires_at", 0)
        expires_in = max(0, int(expires_at - time.time()))
        tok = cache.get("access_token", "")
        masked = tok[:8] + "..." + tok[-4:] if len(tok) > 12 else "***"
        print(f"Access token : {masked}")
        print(f"Expires in   : {expires_in}s ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at))})")
        print(f"Has refresh  : {'yes' if cache.get('refresh_token') else 'no'}")
        return

    if args.reuse_teslamate:
        import_from_teslamate()
        return

    if args.refresh:
        cache = _load_cache()
        if not cache or not cache.get("refresh_token"):
            print("[auth] No cached refresh token. Run without --refresh to log in.")
            sys.exit(1)
        print("[auth] Forcing token refresh...")
        new_tokens = refresh_tokens(cache["refresh_token"])
        _save_cache(new_tokens)
        print("[auth] Tokens refreshed successfully.")
        return

    # Default: interactive login
    interactive_login()


if __name__ == "__main__":
    main()
