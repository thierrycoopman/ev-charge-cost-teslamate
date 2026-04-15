# tesla-sync

[![CI](https://github.com/thierrycoopman/ev-charge-cost-teslamate/actions/workflows/ci.yml/badge.svg)](https://github.com/thierrycoopman/ev-charge-cost-teslamate/actions/workflows/ci.yml)
[![Docker](https://github.com/thierrycoopman/ev-charge-cost-teslamate/actions/workflows/docker.yml/badge.svg)](https://github.com/thierrycoopman/ev-charge-cost-teslamate/actions/workflows/docker.yml)
[![Image](https://ghcr.io/thierrycoopman/tesla-sync)](https://github.com/thierrycoopman/ev-charge-cost-teslamate/pkgs/container/tesla-sync)

---

> 🤖 **Vibe-coded with AI** — This project was written entirely through a conversation with [Claude](https://claude.ai) (Anthropic). The code, scripts, Dockerfile, CI pipelines, and this README were all generated and iterated on via AI pair programming, with a human steering the direction and testing the results. Use it, fork it, break it — just know what you're working with.

---

A self-hosted Docker container that runs alongside [TeslaMate](https://github.com/teslamate-org/teslamate) and automatically fills in the **charging cost** column that TeslaMate intentionally leaves blank.

Supports two cost sources:
- 🔴 **Tesla Superchargers** — fetched directly from the Tesla private API (the same one the Tesla app uses)
- 🟢 **Third-party EV chargers** — imported from an EVC-Net platform (e.g. agrisnellaad.evc-net.com)

The container runs two sync jobs every morning via cron and writes the costs into TeslaMate's PostgreSQL database. Grafana dashboards update automatically.

---

## Table of Contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Quick Start — Docker (recommended)](#quick-start--docker-recommended)
- [Configuration reference](#configuration-reference)
- [First-time Tesla authentication](#first-time-tesla-authentication)
- [Testing & manual runs](#testing--manual-runs)
- [Managing the container](#managing-the-container)
- [GitHub Actions & releases](#github-actions--releases)
- [Building from source](#building-from-source)
- [Local development (no Docker)](#local-development-no-docker)
- [Script reference](#script-reference)
- [Troubleshooting](#troubleshooting)
- [Legal & ToS](#legal--tos-considerations)

---

## How it works

### Tesla Supercharger sync (`sync_teslamate_costs.py`)

1. Calls `GET ownership.tesla.com/mobile-app/charging/history` — the same endpoint the Tesla app uses to display your billing history.
2. Fetches every session for every vehicle on your account (one call, no pagination needed).
3. Matches each billing session to a TeslaMate `charging_process` row by **VIN + start timestamp** (±5 min).
4. A second pass matches any remaining sessions by **timestamp only** — this catches sessions at third-party chargers that were billed through your Tesla account but tracked under a different VIN in TeslaMate.
5. Writes the gross cost (`totalDue`) to `charging_processes.cost`. Pass `--net` to use `netDue` (after credits) instead.

Sessions that already have a cost are skipped unless you pass `--overwrite`.

### EVC-Net sync (`sync_evc_costs.py`)

1. Logs in to your EVC-Net portal (e.g. `agrisnellaad.evc-net.com`) with your email and password.
2. Downloads the transaction export as an Excel file.
3. Parses the Excel with flexible column detection (handles Dutch/English headers, various date formats).
4. Matches each EVC session to a TeslaMate row by **start timestamp** (±5 min) — no VIN available from EVC-Net.
5. Uses kWh as a secondary confidence check (flags matches where kWh differs by >15%).
6. Writes the cost to `charging_processes.cost`.

If the auto-download fails (the export URL varies by EVC-Net installation), you can pass `--file ~/Downloads/export.xlsx` with a manually downloaded file.

### Token management

Tesla uses OAuth2 + PKCE. After the one-time browser login, an **access token** (8-hour lifetime) and **refresh token** (~45 day lifetime) are saved to a Docker volume. Every time a sync runs, the script silently refreshes the access token if needed. As long as the container runs at least once every 45 days, you never need to log in again.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Docker host (your Mac / server)             │
│                                                                 │
│  ┌─────────────────────┐        ┌──────────────────────────┐   │
│  │  TeslaMate stack    │        │  tesla-sync container    │   │
│  │                     │        │                          │   │
│  │  teslamate ─────────┼──────▶ │  reads: car data         │   │
│  │  grafana            │        │                          │   │
│  │  database ──────────┼──────▶ │  writes: cost column     │   │
│  │  mqtt               │        │                          │   │
│  └──────────┬──────────┘        │  cron 06:00 Tesla sync   │   │
│             │  same Docker       │  cron 06:30 EVC sync     │   │
│             └──────network──────▶│                          │   │
│                                  └──────────┬───────────────┘   │
│                                             │                   │
└─────────────────────────────────────────────┼───────────────────┘
                                              │ HTTPS
                                  ┌───────────▼─────────────┐
                                  │  ownership.tesla.com     │
                                  │  agrisnellaad.evc-net.com│
                                  └──────────────────────────┘
```

Key points:
- The `tesla-sync` container **joins TeslaMate's Docker network** — no ports need to be exposed on the database.
- The Tesla token is stored in a **named Docker volume** — it survives container rebuilds and restarts.
- Logs are bind-mounted to `./logs/` so you can read them on the host without exec-ing into the container.

---

## Quick Start — Docker (recommended)

### Prerequisites

- Docker + Docker Compose installed
- TeslaMate running (with its default Docker Compose setup)
- A Tesla account with Supercharger history
- (Optional) An EVC-Net account for third-party charger costs

### Step 1 — Get the code

```bash
git clone https://github.com/thierrycoopman/ev-charge-cost-teslamate.git
cd ev-charge-cost-teslamate
```

### Step 2 — Configure `.env`

```bash
cp .env.example .env
```

Open `.env` and fill in the values marked as required:

```ini
# ── Required ──────────────────────────────────────────────────────────────
# Your Tesla account region
TESLA_COUNTRY=BE          # ISO 3166-1 alpha-2: BE, FR, DE, CH, GB, US …
TESLA_LOCALE=fr_BE        # Affects currency/language in responses

# TeslaMate's PostgreSQL — uses internal Docker hostname "database"
TESLAMATE_DATABASE_URL=postgresql://teslamate:teslamate@database:5432/teslamate

# The name of TeslaMate's Docker network (find it in step 3)
TESLAMATE_NETWORK=teslamate_default

# ── Optional — EVC-Net / Agrisnellaad ─────────────────────────────────────
EVC_EMAIL=your@email.com
EVC_PASSWORD=yourpassword
```

### Step 3 — Find your TeslaMate network name

```bash
docker network ls | grep tesla
# Example output:
#   3f8a1c2b4d5e   teslamate_default   bridge   local
#                  ↑ copy this name → TESLAMATE_NETWORK in .env
```

### Step 4 — Pull the image and start

**Using the pre-built image from GitHub Container Registry (fastest):**

```bash
docker pull ghcr.io/thierrycoopman/tesla-sync:latest

# Start (reads image name from docker-compose.yml)
docker compose up -d
```

**Or build locally** (see [Building from source](#building-from-source)):

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Step 5 — Authenticate with Tesla (one-time only)

```bash
docker compose exec tesla-sync python scripts/auth.py
```

You will see:

```
[auth] Tesla login URL:

  https://auth.tesla.com/oauth2/v3/authorize?client_id=ownerapi&...

[auth] Running headless — copy the URL above into your browser.

Log in to your Tesla account. You will be redirected to a page that
says 'Page Not Found' — that is expected. Copy the full URL from your
browser's address bar and paste it below.

Callback URL:
```

1. Copy the printed URL → paste it into your browser
2. Log in to your Tesla account (MFA works fine)
3. The page will show "Page Not Found" — this is correct
4. Copy the **full URL** from your browser's address bar (it starts with `https://auth.tesla.com/void/callback?code=...`)
5. Paste it into the terminal → press Enter

The token is saved to the `tesla-sync-data` Docker volume. **You won't need to do this again** unless you explicitly revoke access in the Tesla app.

### Step 6 — Verify everything is working

```bash
# Check container status
docker compose ps

# See startup logs and cron schedule
docker logs tesla-sync

# Run a dry-run Tesla sync right now (no DB writes)
docker compose exec tesla-sync sh -c '. /app/.env-cron && python scripts/sync_teslamate_costs.py'

# Run a dry-run EVC sync right now (no DB writes)
docker compose exec tesla-sync sh -c '. /app/.env-cron && python scripts/sync_evc_costs.py'
```

---

## Configuration reference

All configuration is done via environment variables in your `.env` file.

| Variable | Required | Default | Description |
|---|---|---|---|
| `TESLA_COUNTRY` | ✅ | `BE` | ISO 3166-1 alpha-2 country code. Affects currency in API responses. |
| `TESLA_LOCALE` | ✅ | `fr_BE` | BCP 47 locale. Affects language in API responses. |
| `TESLA_EMAIL` | — | — | Tesla account email (informational only — not used for auth). |
| `TESLA_TOKEN_FILE` | — | `/app/data/tesla_tokens.json` | Path to token cache inside the container. Do not change unless you have a reason. |
| `TESLAMATE_DATABASE_URL` | ✅ | — | PostgreSQL connection URL. Use `database` as the hostname when running inside Docker alongside TeslaMate. |
| `TESLAMATE_NETWORK` | ✅ | `teslamate_default` | Name of TeslaMate's Docker network. Find with `docker network ls \| grep tesla`. |
| `EVC_EMAIL` | — | — | EVC-Net login email. If not set, the EVC sync is skipped. |
| `EVC_PASSWORD` | — | — | EVC-Net login password. |
| `EVC_BASE_URL` | — | `https://agrisnellaad.evc-net.com` | Base URL of your EVC-Net portal. Change if you use a different EVC-Net instance. |

### Sync script flags

Both sync scripts support these flags when run manually:

| Flag | Description |
|---|---|
| *(no flags)* | Dry-run: shows what would be written, makes no DB changes |
| `--apply` | Write costs to the database |
| `--overwrite` | Also update sessions that already have a cost |
| `--tolerance N` | Timestamp match window in minutes (default: 5) |
| `--net` | *(Tesla only)* Use net cost after credits instead of gross |
| `--vin VIN` | *(Tesla only)* Filter to a single vehicle |
| `--file path` | *(EVC only)* Use a manually downloaded Excel file instead of auto-fetching |
| `--since YYYY-MM-DD` | *(EVC only)* Only process transactions from this date |
| `--db URL` | Override `TESLAMATE_DATABASE_URL` for this run |

---

## First-time Tesla authentication

The Tesla authentication uses **OAuth2 Authorization Code + PKCE** — the same flow as the Tesla mobile app. It requires a one-time browser interaction.

```
You                    Tesla Auth Server              Container
 │                            │                           │
 │ ──── open URL in browser ─▶│                           │
 │ ──── log in + MFA ────────▶│                           │
 │ ◀─── redirect to callback ─│                           │
 │                            │                           │
 │ ──── paste callback URL ──────────────────────────────▶│
 │                            │ ◀─ exchange code ─────────│
 │                            │ ── tokens ───────────────▶│
 │                            │                           │ saves to /app/data/
 │                            │                           │ tesla_tokens.json
```

**Token refresh**: The access token expires every 8 hours. The refresh token lasts ~45 days. Every time a sync script runs, it automatically refreshes the access token using the cached refresh token. The refreshed refresh token (Tesla issues a new one each time) is written back to the volume. You never need to re-authenticate as long as the sync runs at least once every 45 days.

**Token security**: The token file lives in a named Docker volume (`tesla-sync-data`) with `chmod 600`. It is never written into the image itself — `.dockerignore` ensures no tokens are baked in at build time.

**Revoking access**: Tesla app → Account → Security → Manage Devices → remove the entry.

---

## Testing & manual runs

### Dry-run (recommended first step)

```bash
# Tesla Supercharger sync — preview without writing
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/sync_teslamate_costs.py'

# EVC-Net sync — preview without writing
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/sync_evc_costs.py'
```

The dry-run shows a table of matched sessions and what cost would be written:

```
══════════════════════════════════════════════════════════════════════
  TM ID     Date         Car           Location             Cost     Was      OK?
  ────────  ───────────  ────────────  ───────────────────  ───────  ───────  ────
    12345   2024-11-15   Bunny         Bern Bethlehem       EUR 8.42  null    ✓
    12301   2024-11-08   New Bunny     Zurich Altstetten    EUR 12.10 null    ✓
    12289   2024-10-31   Bunny         Brussels North       EUR 6.87  null    ✓

DRY RUN — 3 session(s) would be updated.
Add --apply to write to the database.
```

### Apply to database

```bash
# Tesla sync — write costs
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/sync_teslamate_costs.py --apply'

# EVC sync — write costs
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/sync_evc_costs.py --apply'
```

### EVC with a manual Excel export

If the auto-fetch fails (export URL discovery is best-effort), download the Excel manually:

1. Go to your EVC portal → Transactions → Export
2. Save the `.xlsx` file
3. Copy it into the container (or use a bind mount):

```bash
docker cp ~/Downloads/transactions.xlsx tesla-sync:/tmp/transactions.xlsx
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/sync_evc_costs.py --file /tmp/transactions.xlsx'
```

---

## Managing the container

### View logs

```bash
# Live tail (both sync outputs streamed to docker logs)
docker logs -f tesla-sync

# Host-side log files (bind-mounted from ./logs/)
tail -f logs/sync_tesla.log
tail -f logs/sync_evc.log
```

### Cron schedule

| Time (UTC) | Job |
|---|---|
| 06:00 daily | `sync_teslamate_costs.py --apply` (Tesla Supercharger costs) |
| 06:30 daily | `sync_evc_costs.py --apply` (EVC-Net costs) |
| 04:00 Sunday | Log rotation (keeps last 5 000 lines per file) |

Adjust the times by editing `crontab` and rebuilding.

### Restart / update

```bash
# Restart the container (keeps token volume intact)
docker compose restart tesla-sync

# Update to latest image
docker compose pull
docker compose up -d

# Force rebuild from source
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Remove (keeping token)

```bash
# Stop and remove container — token volume is preserved
docker compose down

# Start again later — no need to re-authenticate
docker compose up -d
```

### Remove everything (including token)

```bash
# This deletes the token — you will need to authenticate again
docker compose down -v
```

---

## GitHub Actions & releases

This repo uses GitHub Actions to automatically build and publish a multi-platform Docker image to the **GitHub Container Registry (ghcr.io)** whenever a new release tag is pushed.

### How the CI pipeline works

```
Pull Request          Push to main           Tag v1.2.3
     │                     │                     │
     ▼                     ▼                     ▼
 [ci.yml]            [ci.yml]             [ci.yml]
  ● Python syntax     ● Python syntax      ● Python syntax
  ● Import tests      ● Import tests       ● Import tests
  ● CLI smoke test    ● CLI smoke test     ● CLI smoke test
                            +                    +
                     [docker.yml]         [docker.yml]
                      ● Build image        ● Build image
                      ● Push :latest       ● Push :1.2.3
                                           ● Push :1.2
                                           ● Push :1
                                           ● Push :latest
```

**Platforms built**: `linux/amd64` (x86-64 servers) and `linux/arm64` (Raspberry Pi, Apple Silicon via Docker Desktop).

**Registry**: `ghcr.io/thierrycoopman/tesla-sync`  
**Visibility**: Linked to the GitHub repo — public if the repo is public.

### Creating a release

```bash
# Tag a new version
git tag v1.0.0
git push origin v1.0.0

# GitHub Actions automatically:
# 1. Runs all CI checks
# 2. Builds the Docker image for amd64 + arm64
# 3. Pushes to ghcr.io with tags: 1.0.0, 1.0, 1, latest
# 4. Creates a GitHub Release (draft — you fill in release notes)
```

Go to **GitHub → Releases** to publish the draft release with notes.

### Using a specific version

In your `docker-compose.yml`, pin to a specific version instead of `latest`:

```yaml
image: ghcr.io/thierrycoopman/tesla-sync:1.0.0   # pinned — never auto-updates
image: ghcr.io/thierrycoopman/tesla-sync:1        # major — auto-gets 1.x.y patches
image: ghcr.io/thierrycoopman/tesla-sync:latest   # always latest main
```

Pinning to a specific version (`1.0.0`) is recommended for production — you control exactly when you update. Run `docker compose pull && docker compose up -d` to upgrade.

### Enable the workflows in your repo

1. Push this code to your GitHub repository
2. Go to **Settings → Actions → General → Allow all actions**
3. Go to **Settings → Packages** and ensure the Container Registry is enabled
4. The `GITHUB_TOKEN` secret is available automatically — no setup needed

The first push to `main` will trigger a build and publish `:latest`.

---

## Building from source

Use this when you want to test local changes before publishing.

```bash
# Clone
git clone https://github.com/thierrycoopman/ev-charge-cost-teslamate.git
cd ev-charge-cost-teslamate

# Configure
cp .env.example .env
# edit .env ...

# Build image locally and start
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build

# Authenticate Tesla (same as pre-built setup)
docker compose exec tesla-sync python scripts/auth.py
```

The `docker-compose.build.yml` override replaces the pre-built image reference with `build: .` so Docker builds from the local `Dockerfile`.

---

## Local development (no Docker)

If you want to run the scripts directly on your Mac (e.g. for debugging):

```bash
# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# edit .env — set TESLAMATE_DATABASE_URL to localhost:5432
# (you'll need to expose TeslaMate's DB port in its docker-compose.yml)

# 4. Authenticate
python scripts/auth.py

# 5. Run sync scripts
python scripts/sync_teslamate_costs.py
python scripts/sync_evc_costs.py

# 6. Explore the API
python scripts/get_charging_history.py
python scripts/get_invoices.py
```

**TeslaMate DB port exposure** (needed for local dev only — NOT needed for Docker):

In TeslaMate's `docker-compose.yml`, add under `database:`:
```yaml
database:
  ports:
    - "5432:5432"
```

Then use `localhost:5432` in your `.env`.

---

## Script reference

Beyond the sync container, this repo contains standalone scripts for exploring the Tesla API.

### `scripts/auth.py`

Handles Tesla OAuth2 + PKCE authentication.

```bash
python scripts/auth.py                   # Interactive browser login
python scripts/auth.py --refresh         # Force-refresh the cached token
python scripts/auth.py --show            # Print current token info (masked)
python scripts/auth.py --reuse-teslamate # Import token from TeslaMate's DB
```

### `scripts/get_charging_history.py`

Fetches all Supercharger sessions with full billing data.

```bash
python scripts/get_charging_history.py              # Table of all sessions
python scripts/get_charging_history.py --csv out.csv
python scripts/get_charging_history.py --vin LRW... # Filter to one vehicle
python scripts/get_charging_history.py --raw         # Raw JSON dump
```

**What you get per session**: site name, address, GPS, start/stop time, energy (kWh), tier pricing, total cost, parking fee, currency, payment status, invoice reference.

### `scripts/get_invoices.py`

Downloads PDF invoices for all Supercharger sessions.

```bash
python scripts/get_invoices.py               # Download all to ./invoices/
python scripts/get_invoices.py --list        # List without downloading
python scripts/get_invoices.py --output ~/Documents/Tesla/
python scripts/get_invoices.py --vin LRW...  # Filter to one vehicle
```

### `scripts/get_vehicle_data.py`

Fetches vehicle state, config, and nearby charging sites.

```bash
python scripts/get_vehicle_data.py              # List all vehicles
python scripts/get_vehicle_data.py --id 1234567890
python scripts/get_vehicle_data.py --nearby --wake
```

### `scripts/explore_endpoints.py`

General-purpose Tesla API prober for discovery and debugging.

```bash
python scripts/explore_endpoints.py --list
python scripts/explore_endpoints.py --get /api/1/users/me
python scripts/explore_endpoints.py --ownership /mobile-app/charging/history
python scripts/explore_endpoints.py --raw GET https://owner-api.teslamotors.com/api/1/products
```

---

## Troubleshooting

### `Error: network teslamate_default not found`

The `TESLAMATE_NETWORK` value in `.env` doesn't match the actual network name.

```bash
docker network ls | grep tesla
# Copy the exact name from the output → update TESLAMATE_NETWORK in .env
docker compose up -d
```

### `DB connection failed: could not connect to server`

The container can't reach TeslaMate's PostgreSQL. Check:

1. Is TeslaMate running? `docker compose -f /path/to/teslamate/docker-compose.yml ps`
2. Is the network name correct? (see above)
3. Are you using `database` as the hostname (not `localhost`)? Check `TESLAMATE_DATABASE_URL` in `.env`.

```bash
# Test connectivity from inside the container
docker compose exec tesla-sync python3 -c "
import psycopg2, os
conn = psycopg2.connect(os.environ['TESLAMATE_DATABASE_URL'])
print('Connected:', conn.get_dsn_parameters())
"
```

### `[auth] Token refresh failed`

The refresh token has expired (>45 days without a sync). Re-authenticate:

```bash
docker compose exec tesla-sync python scripts/auth.py
```

### `EVC login failed — no session cookies`

- Check `EVC_EMAIL` and `EVC_PASSWORD` in `.env` — log in manually at your EVC portal to verify they work.
- If the portal uses a captcha or 2FA, automated login may not be possible. Use `--file` with a manual export instead.

### `No matches found` in EVC sync

The timestamps between EVC export and TeslaMate differ by more than 5 minutes. Try widening the window:

```bash
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/sync_evc_costs.py --tolerance 15'
```

### Tesla sessions showing in the wrong currency

Set `TESLA_COUNTRY` to match your Tesla account's billing country (e.g. `BE` for Belgium, `FR` for France). Restart the container after changing `.env`.

### `403 Forbidden` from Tesla API

Usually a stale token. Force a refresh:

```bash
docker compose exec tesla-sync \
  sh -c '. /app/.env-cron && python scripts/auth.py --refresh'
```

---

## Legal & ToS considerations

> **TL;DR**: For personal use on your own account. Be responsible.

1. **Tesla Terms of Service** prohibit unauthorized automation. However, these scripts access only your own billing data using the same endpoints the Tesla mobile app uses. Accessing your own data is generally accepted by the community and Tesla has tolerated such tools for years.

2. **No commercial use**: Don't build a product on these private endpoints. Tesla provides the official [Fleet API](https://developer.tesla.com/docs/fleet-api) for that.

3. **Rate limiting**: The sync runs once per day and fetches data in a single API call. This is far below any reasonable rate limit.

4. **Community precedent**: [TeslaMate](https://github.com/teslamate-org/teslamate), [TeslaFi](https://www.teslafi.com/), [Tessie](https://tessie.com/), and hundreds of GitHub projects have used these same private endpoints for years.

5. **No warranty**: These endpoints can change or break at any time without notice. There is no SLA.

---

## Contributing

Found a new endpoint? Got a schema update? PRs welcome.

Of particular interest:
- 🌍 Regional billing differences (non-EU, non-US)
- 📱 New Tesla app versions that change endpoints
- 🔌 Other EV charging network integrations (OCPI, OCPP platforms)
- 🧪 Additional test coverage

---

## Community resources

| Resource | URL |
|---|---|
| TeslaMate | https://github.com/teslamate-org/teslamate |
| Tesla community API docs | https://tesla-api.timdorr.com |
| Tesla Fleet API (official) | https://developer.tesla.com/docs/fleet-api |
| timdorr/tesla-api issues | https://github.com/timdorr/tesla-api/issues |
