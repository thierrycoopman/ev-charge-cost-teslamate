#!/usr/bin/env python3
"""
Tesla Private API - Extended Vehicle Data Explorer
===================================================
Fetches vehicle data from Tesla's Owner API — the same data the Tesla mobile
app uses, but not all of it is available via the official Fleet API.

Endpoints covered:
  - GET /api/1/vehicles                       List all vehicles on account
  - GET /api/1/vehicles/{id}/vehicle_data     Full state snapshot (charges, climate, drive, etc.)
  - GET /api/1/vehicles/{id}/charge_state     Battery & charging state
  - GET /api/1/vehicles/{id}/climate_state    Climate/HVAC state
  - GET /api/1/vehicles/{id}/drive_state      GPS location, speed, power
  - GET /api/1/vehicles/{id}/gui_settings     Unit preferences (miles/km, etc.)
  - GET /api/1/vehicles/{id}/nearby_charging_sites  Superchargers + destination chargers near vehicle
  - GET /api/1/vehicles/{id}/service_data     Service center records

Run:
  python get_vehicle_data.py                        # List vehicles
  python get_vehicle_data.py --id <vehicle_id>      # Full data for specific vehicle
  python get_vehicle_data.py --all                  # All endpoints for all vehicles
  python get_vehicle_data.py --nearby               # Show nearby Superchargers
  python get_vehicle_data.py --json output.json     # Save to JSON

Note: Vehicle must be "awake" (online) for most endpoints to return data.
      Use --wake to wake the vehicle first (sends a wake command).
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import auth

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OWNER_API = "https://owner-api.teslamotors.com/api/1"

# Endpoints that work on a sleeping vehicle (cached data)
ENDPOINTS_NO_WAKE = [
    "vehicles",
    "gui_settings",
    "vehicle_config",
]

# Endpoints that require the vehicle to be awake
ENDPOINTS_REQUIRE_WAKE = [
    "vehicle_data",
    "charge_state",
    "climate_state",
    "drive_state",
    "nearby_charging_sites",
    "service_data",
    "mobile_enabled",
    "media_state",
    "software_update",
]

# Composite endpoint (fetches multiple state objects in one call)
VEHICLE_DATA_ENDPOINTS = [
    "charge_state",
    "climate_state",
    "drive_state",
    "gui_settings",
    "vehicle_config",
    "vehicle_state",
]

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_vehicles(session: requests.Session) -> list[dict]:
    """Fetch all vehicles associated with the account."""
    resp = session.get(f"{OWNER_API}/vehicles", timeout=30)
    if resp.status_code == 404:
        print("[vehicles] 404 — the /vehicles endpoint may be deprecated for your account type.")
        print("           Trying products endpoint instead...")
        return get_products(session)
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", [])


def get_products(session: requests.Session) -> list[dict]:
    """Fetch all Tesla products (vehicles + energy devices) — newer endpoint."""
    resp = session.get(f"{OWNER_API}/products", timeout=30)
    if resp.status_code == 404:
        print("[vehicles] /products also 404. Owner API may be fully deprecated for your account.")
        return []
    resp.raise_for_status()
    data = resp.json()
    return data.get("response", [])


def wake_vehicle(session: requests.Session, vehicle_id: str | int, timeout: int = 60) -> bool:
    """
    Send a wake command and poll until the vehicle is online or timeout.

    Returns True if vehicle came online, False if timed out.
    """
    print(f"[wake] Sending wake command to vehicle {vehicle_id}...")
    resp = session.post(f"{OWNER_API}/vehicles/{vehicle_id}/wake_up", timeout=30)
    if not resp.ok:
        print(f"[wake] Wake command failed: {resp.status_code}")
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = session.get(f"{OWNER_API}/vehicles/{vehicle_id}", timeout=15)
        if resp.ok:
            state = resp.json().get("response", {}).get("state")
            if state == "online":
                print(f"[wake] Vehicle is online!")
                return True
            print(f"[wake] State: {state} — waiting...", end="\r")
        time.sleep(3)

    print(f"\n[wake] Timed out after {timeout}s. Vehicle may be in poor connectivity area.")
    return False


def fetch_endpoint(session: requests.Session, vehicle_id: str | int, endpoint: str) -> dict | None:
    """
    Fetch a specific vehicle endpoint.

    Returns the parsed response dict, or None on error.
    """
    url = f"{OWNER_API}/vehicles/{vehicle_id}/{endpoint}"
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code == 408:
            print(f"  [!] {endpoint}: 408 Request Timeout — vehicle is asleep. Use --wake.")
            return None
        if resp.status_code == 404:
            print(f"  [!] {endpoint}: 404 Not Found — endpoint may not apply to this vehicle.")
            return None
        if resp.status_code == 401:
            print(f"  [!] {endpoint}: 401 Unauthorized — token expired, re-run auth.py")
            return None
        resp.raise_for_status()
        return resp.json().get("response", {})
    except requests.RequestException as exc:
        print(f"  [!] {endpoint}: request failed — {exc}")
        return None


def fetch_vehicle_data(session: requests.Session, vehicle_id: str | int) -> dict | None:
    """
    Fetch the composite vehicle_data endpoint — multiple state objects in one call.
    More efficient than fetching each sub-endpoint separately.
    """
    url = f"{OWNER_API}/vehicles/{vehicle_id}/vehicle_data"
    params = {"endpoints": ";".join(VEHICLE_DATA_ENDPOINTS)}
    try:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 408:
            print("  [!] vehicle_data: 408 — vehicle is asleep. Use --wake to wake it.")
            return None
        resp.raise_for_status()
        return resp.json().get("response", {})
    except requests.RequestException as exc:
        print(f"  [!] vehicle_data: request failed — {exc}")
        return None


def fetch_nearby_chargers(session: requests.Session, vehicle_id: str | int) -> dict | None:
    """
    Fetch nearby charging sites (Superchargers + destination chargers).
    Requires vehicle to be awake and have GPS.
    """
    data = fetch_endpoint(session, vehicle_id, "nearby_charging_sites")
    return data

# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def print_vehicle_summary(v: dict):
    """Print a one-line summary of a vehicle."""
    vid      = v.get("id") or v.get("vehicle_id")
    vin      = v.get("vin")
    name     = v.get("display_name") or v.get("name")
    state    = v.get("state")
    model    = v.get("model") or v.get("car_type")
    print(f"  ID: {vid:>15}  VIN: {vin}  Name: {name:<20}  State: {state:<10}  Model: {model}")


def print_charge_state(cs: dict):
    """Pretty-print charge state."""
    if not cs:
        return
    print(f"\n{'--- Charge State ':=<50}")
    print(f"  Battery level      : {cs.get('battery_level')}%")
    print(f"  Usable battery     : {cs.get('usable_battery_level')}%")
    print(f"  Est. range (mi)    : {cs.get('battery_range')}")
    print(f"  Ideal range (mi)   : {cs.get('ideal_battery_range')}")
    print(f"  Charging state     : {cs.get('charging_state')}")
    print(f"  Charger type       : {cs.get('fast_charger_type')} (DC fast: {cs.get('fast_charger_present')})")
    print(f"  Charge rate (mi/h) : {cs.get('charge_rate')}")
    print(f"  Charge power (kW)  : {cs.get('charger_power')}")
    print(f"  Energy added (kWh) : {cs.get('charge_energy_added')}")
    print(f"  Miles added        : {cs.get('charge_miles_added_ideal')}")
    print(f"  Charge limit       : {cs.get('charge_limit_soc')}%")
    print(f"  Time to full (min) : {cs.get('minutes_to_full_charge')}")
    print(f"  Supercharger session: {cs.get('charge_session_id')}")


def print_drive_state(ds: dict):
    """Pretty-print drive state."""
    if not ds:
        return
    print(f"\n{'--- Drive State ':=<50}")
    print(f"  Latitude      : {ds.get('latitude')}")
    print(f"  Longitude     : {ds.get('longitude')}")
    print(f"  Speed (mph)   : {ds.get('speed')}")
    print(f"  Power (kW)    : {ds.get('power')}")
    print(f"  Odometer      : {ds.get('odometer')}")
    print(f"  Shift state   : {ds.get('shift_state')}")
    print(f"  Heading       : {ds.get('heading')}°")
    print(f"  GPS quality   : {ds.get('gps_as_of')} ({ds.get('native_location_supported')})")


def print_nearby_chargers(data: dict):
    """Pretty-print nearby charging sites."""
    if not data:
        return

    superchargers = data.get("superchargers", [])
    destination   = data.get("destination_charging", [])

    print(f"\n{'--- Nearby Superchargers ':=<50}")
    for sc in superchargers:
        name       = sc.get("name")
        avail      = sc.get("available_stalls")
        total      = sc.get("total_stalls")
        dist_miles = sc.get("distance_miles")
        site_closed = sc.get("site_closed")
        billing    = sc.get("billing_info", {})
        print(f"  {name}")
        print(f"    Stalls: {avail}/{total} available  |  {dist_miles:.1f} mi away  |  Closed: {site_closed}")
        if billing:
            print(f"    Billing: {billing}")

    print(f"\n{'--- Destination Chargers ':=<50}")
    for dc in destination:
        print(f"  {dc.get('name')} — {dc.get('distance_miles'):.1f} mi")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch extended vehicle data from Tesla Owner API"
    )
    parser.add_argument("--id", type=str, default=None, metavar="VEHICLE_ID",
                        help="Vehicle ID (integer) to query")
    parser.add_argument("--all", action="store_true",
                        help="Fetch all available endpoints for each vehicle")
    parser.add_argument("--wake", action="store_true",
                        help="Wake vehicle before fetching data")
    parser.add_argument("--nearby", action="store_true",
                        help="Show nearby Superchargers")
    parser.add_argument("--json", type=str, default=None, metavar="FILE",
                        help="Save results to JSON file")
    parser.add_argument("--pretty", action="store_true",
                        help="Pretty-print raw JSON to stdout")
    args = parser.parse_args()

    print("[auth] Loading credentials...")
    try:
        session = auth.authed_session()
    except RuntimeError as exc:
        print(f"[auth] {exc}")
        sys.exit(1)

    # Fetch vehicle list
    print("\n[vehicles] Fetching vehicle list...")
    vehicles = get_vehicles(session)

    if not vehicles:
        print("[vehicles] No vehicles found on this account.")
        sys.exit(0)

    print(f"\n[vehicles] Found {len(vehicles)} vehicle(s):\n")
    for v in vehicles:
        print_vehicle_summary(v)

    # Determine which vehicle(s) to query
    if args.id:
        targets = [v for v in vehicles if str(v.get("id")) == args.id
                   or str(v.get("vehicle_id")) == args.id]
        if not targets:
            print(f"\n[error] No vehicle found with ID {args.id}")
            sys.exit(1)
    else:
        targets = vehicles

    results = {}

    for vehicle in targets:
        vid = vehicle.get("id") or vehicle.get("vehicle_id")
        vin = vehicle.get("vin")
        name = vehicle.get("display_name") or vin

        print(f"\n{'='*60}")
        print(f"Vehicle: {name} (ID: {vid}, VIN: {vin})")
        print(f"{'='*60}")

        if args.wake:
            wake_vehicle(session, vid)

        vehicle_data = {}

        # Always fetch full vehicle_data if possible
        print("\n[data] Fetching vehicle_data (composite)...")
        vdata = fetch_vehicle_data(session, vid)
        if vdata:
            vehicle_data["vehicle_data"] = vdata
            if not args.pretty:
                print_charge_state(vdata.get("charge_state"))
                print_drive_state(vdata.get("drive_state"))

        # Nearby chargers
        if args.nearby or args.all:
            print("\n[data] Fetching nearby charging sites...")
            nearby = fetch_nearby_chargers(session, vid)
            if nearby:
                vehicle_data["nearby_charging_sites"] = nearby
                if not args.pretty:
                    print_nearby_chargers(nearby)

        # Additional endpoints if --all
        if args.all:
            for ep in ["service_data", "mobile_enabled", "vehicle_config"]:
                print(f"\n[data] Fetching {ep}...")
                data = fetch_endpoint(session, vid, ep)
                if data:
                    vehicle_data[ep] = data

        results[str(vid)] = vehicle_data

        if args.pretty:
            print(json.dumps(vehicle_data, indent=2, default=str))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\n[json] Saved to {args.json}")


if __name__ == "__main__":
    main()
