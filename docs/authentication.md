# Tesla API Authentication

> **Status as of 2025:** Tesla's private Owner API uses OAuth 2.0 with PKCE via `auth.tesla.com`. The authentication flow below is what the Tesla mobile app itself uses, reverse-engineered by the community.

---

## Overview

Tesla uses **OAuth 2.0 Authorization Code flow with PKCE** (RFC 7636). There is no fixed `client_secret` — security is provided by the PKCE `code_verifier`/`code_challenge` pair.

```
Your App ──► auth.tesla.com ──► User logs in ──► Callback URL with ?code=...
    │                                                         │
    └─────────── POST /oauth2/v3/token ◄──────────────────────┘
                 (exchange code for tokens)
                         │
                    access_token  (short-lived, ~8h)
                    refresh_token (long-lived, ~90 days)
```

---

## Key Endpoints

| Endpoint | URL |
|---|---|
| Authorization | `https://auth.tesla.com/oauth2/v3/authorize` |
| Token exchange | `https://auth.tesla.com/oauth2/v3/token` |
| Callback URI | `https://auth.tesla.com/void/callback` |

> **China accounts:** Use `auth.tesla.cn` and `owner-api.vn.cloud.tesla.cn` instead.

---

## Step-by-Step Flow

### 1. Generate PKCE Pair

```python
import secrets, hashlib, base64

code_verifier  = secrets.token_urlsafe(86)           # Random 86-char string
digest         = hashlib.sha256(code_verifier.encode()).digest()
code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
state          = secrets.token_urlsafe(16)            # CSRF protection
```

### 2. Build Authorization URL

```
GET https://auth.tesla.com/oauth2/v3/authorize
  ?client_id=ownerapi
  &redirect_uri=https://auth.tesla.com/void/callback
  &response_type=code
  &scope=openid email offline_access
  &state=<random_state>
  &code_challenge=<challenge>
  &code_challenge_method=S256
```

Open this URL in a browser. The user logs in with their Tesla credentials (including MFA if enabled).

### 3. Capture Callback

After login, Tesla redirects to:
```
https://auth.tesla.com/void/callback?code=<auth_code>&state=<state>&issuer=...
```

This page shows "Not Found" in the browser — that's expected. Copy the full URL.

Verify `state` matches what you sent (CSRF check). Extract `code`.

### 4. Exchange Code for Tokens

```
POST https://auth.tesla.com/oauth2/v3/token
Content-Type: application/json

{
  "grant_type": "authorization_code",
  "client_id": "ownerapi",
  "code": "<auth_code>",
  "code_verifier": "<code_verifier>",
  "redirect_uri": "https://auth.tesla.com/void/callback"
}
```

**Response:**
```json
{
  "access_token":  "eyJhbGci...",
  "refresh_token": "eyJhbGci...",
  "id_token":      "eyJhbGci...",
  "expires_in":    28800,
  "token_type":    "Bearer"
}
```

- `access_token` — ~8 hour lifetime. Use as `Authorization: Bearer <token>`
- `refresh_token` — ~90 day lifetime. Use to get new access tokens
- `id_token` — JWT with user claims (email, sub, etc.)

### 5. Refresh Tokens

```
POST https://auth.tesla.com/oauth2/v3/token
Content-Type: application/json

{
  "grant_type":    "refresh_token",
  "client_id":     "ownerapi",
  "refresh_token": "<refresh_token>"
}
```

---

## Required Headers

All API requests must include:

```http
Authorization: Bearer <access_token>
User-Agent: Tesla/1195 CFNetwork/1388 Darwin/22.0.0
x-tesla-user-agent: TeslaApp/4.30.6/ios/17.0
Content-Type: application/json
Accept: application/json
```

The `User-Agent` and `x-tesla-user-agent` headers mimic the iOS app. Some endpoints (especially `akamai-apigateway-charging-ownership.tesla.com`) may return 403 if these are missing or wrong.

---

## Quick Start with `auth.py`

```bash
# Initial login (opens browser)
python scripts/auth.py

# Check token status
python scripts/auth.py --show

# Force refresh
python scripts/auth.py --refresh

# Import tokens from TeslaMate's PostgreSQL DB
TESLAMATE_DATABASE_URL=postgresql://teslamate:teslamate@localhost:5432/teslamate \
  python scripts/auth.py --reuse-teslamate
```

Tokens are cached in `~/.tesla_tokens.json` (chmod 600).

---

## Reusing TeslaMate Tokens

If you already run TeslaMate, it has valid tokens in its PostgreSQL database:

```sql
-- Connect to TeslaMate DB
SELECT access_token, refresh_token FROM settings LIMIT 1;
```

Copy these into your `.env`:
```env
TESLA_ACCESS_TOKEN=eyJhbGci...
TESLA_REFRESH_TOKEN=eyJhbGci...
```

Or use the `--reuse-teslamate` flag which connects to the DB automatically.

---

## Token Types

| Token | Used For |
|---|---|
| **Owner API token** (from `auth.tesla.com`) | Private/unofficial endpoints: `owner-api.teslamotors.com`, `ownership.tesla.com`, `akamai-apigateway-charging-ownership.tesla.com` |
| **Fleet API partner token** | Official Fleet API (`fleet-api.prd.*.cloud.tesla.com`) — requires separate app registration |

Most scripts in this project use the **Owner API token** (personal use, no app registration needed).

---

## MFA (Multi-Factor Authentication)

The Tesla app supports TOTP (authenticator app) and SMS codes. During the OAuth flow:

1. After entering username/password, Tesla may show a challenge page
2. The user enters their 6-digit code
3. The authorization continues normally

`auth.py` uses a real browser (via `webbrowser.open`) so MFA works interactively. If automating with Selenium, note that Tesla's SSO returns **403 if `navigator.webdriver` is detected**.

---

## Security Notes

- **Never commit tokens** — they're in `~/.tesla_tokens.json`, not in the project
- **`.env` contains credentials** — it's in `.gitignore`
- Tesla tokens give **full account access** including vehicle commands, payment info, and account data
- Treat `refresh_token` like a password — it lasts ~90 days and can generate new access tokens
- If compromised, revoke at: `Tesla app → Account → Security → Third-party app access`

---

## Fleet API vs Owner API Authentication

As of 2024-2025, Tesla is migrating the token exchange domain:

| Flow | Domain | Notes |
|---|---|---|
| User login | `auth.tesla.com` | Still works for personal use |
| Partner token exchange (Fleet API) | `fleet-auth.prd.vn.cloud.tesla.com` | Required for Fleet API partners as of Aug 2025 |
| China users | `auth.tesla.cn` | Separate domain |

This project uses **personal tokens** via `auth.tesla.com` — no Fleet API registration needed.
