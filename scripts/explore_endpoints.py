#!/usr/bin/env python3
"""
Tesla Private API - General Endpoint Explorer
=============================================
A flexible tool to probe any Tesla API endpoint (Owner API, ownership.tesla.com,
or the GraphQL charging gateway). Useful for discovering new endpoints, testing
responses, and experimenting without writing a dedicated script.

Includes a built-in catalog of known endpoints with notes.

Usage:
  python explore_endpoints.py --list                            # List known endpoints
  python explore_endpoints.py --get /api/1/vehicles            # GET owner API endpoint
  python explore_endpoints.py --get /api/1/vehicles/{id}/nearby_charging_sites --id 12345
  python explore_endpoints.py --ownership /mobile-app/charging/history  # ownership.tesla.com
  python explore_endpoints.py --graphql getChargingHistoryV2   # Named GraphQL query
  python explore_endpoints.py --graphql getPendingBalances
  python explore_endpoints.py --graphql GetInstrumentBySource --gql-vars '{"paymentSource":"SUPERCHARGE","countryCode":"FR","currencyCode":"EUR"}'
  python explore_endpoints.py --raw GET https://owner-api.teslamotors.com/api/1/users/me
"""

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import auth

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------

OWNER_API_BASE      = "https://owner-api.teslamotors.com"
OWNERSHIP_BASE      = "https://ownership.tesla.com"
GRAPHQL_BASE        = "https://akamai-apigateway-charging-ownership.tesla.com"
FLEET_API_BASE      = "https://fleet-api.prd.na.vn.cloud.tesla.com"  # North America
FLEET_API_EU        = "https://fleet-api.prd.eu.vn.cloud.tesla.com"  # Europe
ACCOUNT_BASE        = "https://account.tesla.com"

# ---------------------------------------------------------------------------
# Endpoint catalog
# ---------------------------------------------------------------------------

KNOWN_ENDPOINTS = {
    # ── Owner API ─────────────────────────────────────────────────────────
    "account_info": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/users/me",
        "desc": "Current user account information",
        "auth": "Bearer",
        "notes": "Returns email, full_name, profile_image_url, vault_uuid",
    },
    "vehicles": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles",
        "desc": "List all vehicles on account",
        "auth": "Bearer",
        "notes": "Removed Jan 2024 for some accounts; use /products instead",
    },
    "products": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/products",
        "desc": "List all products (vehicles + Powerwalls)",
        "auth": "Bearer",
        "notes": "Replacement for /vehicles",
    },
    "vehicle_data": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/vehicle_data",
        "desc": "Full vehicle state snapshot",
        "auth": "Bearer",
        "notes": "Requires vehicle awake. Add ?endpoints=charge_state;drive_state etc.",
    },
    "charge_state": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/data_request/charge_state",
        "desc": "Battery & charging state",
        "auth": "Bearer",
        "notes": "Subset of vehicle_data",
    },
    "drive_state": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/data_request/drive_state",
        "desc": "GPS location, heading, speed, power",
        "auth": "Bearer",
    },
    "climate_state": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/data_request/climate_state",
        "desc": "Interior/exterior temperatures, HVAC state",
        "auth": "Bearer",
    },
    "vehicle_config": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/data_request/vehicle_config",
        "desc": "Static vehicle configuration (trim, options, etc.)",
        "auth": "Bearer",
    },
    "nearby_charging_sites": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/nearby_charging_sites",
        "desc": "Superchargers and destination chargers near vehicle",
        "auth": "Bearer",
        "notes": "Requires vehicle awake and GPS. Returns billing_info per site.",
    },
    "service_data": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/vehicles/{vehicle_id}/service_data",
        "desc": "Service center history",
        "auth": "Bearer",
        "notes": "Not always available",
    },
    "users_keys": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/users/keys",
        "desc": "Paired phone/key-card keys for vehicle access",
        "auth": "Bearer",
    },
    "referral_data": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/users/referral_data",
        "desc": "Referral program info and credits",
        "auth": "Bearer",
    },
    "feature_config": {
        "method": "GET",
        "base": OWNER_API_BASE,
        "path": "/api/1/users/feature_config",
        "desc": "Feature flags enabled for this account",
        "auth": "Bearer",
    },

    # ── Ownership API (Tesla App BFF) ─────────────────────────────────────
    "charging_history_rest": {
        "method": "GET",
        "base": "https://www.tesla.com",
        "path": "/teslaaccount/charging/api/history",
        "desc": "Supercharger session history (REST, older endpoint)",
        "auth": "Bearer",
        "notes": "Add ?vin=VIN to filter. May redirect to login page if token is wrong type.",
        "params": {"vin": "{vin}"},
    },
    "subscription_invoices": {
        "method": "GET",
        "base": OWNERSHIP_BASE,
        "path": "/mobile-app/subscriptions/invoices",
        "desc": "List of subscription invoices (FSD, Premium Connectivity, etc.)",
        "auth": "Bearer",
        "notes": "400 error in some regions; endpoint may need additional params",
    },
    "charging_invoice_pdf": {
        "method": "GET",
        "base": OWNERSHIP_BASE,
        "path": "/mobile-app/charging/invoice/{content_id}",
        "desc": "Download a Supercharger session invoice as PDF",
        "auth": "Bearer",
        "notes": "content_id comes from getChargingHistoryV2 response",
        "params": {"deviceCountry": "US", "deviceLanguage": "en", "vin": "{vin}"},
    },
    "sub_invoice_pdf": {
        "method": "GET",
        "base": OWNERSHIP_BASE,
        "path": "/mobile-app/charging/subscription/invoice/{invoice_id}",
        "desc": "Download a subscription invoice PDF",
        "auth": "Bearer",
    },

    # ── GraphQL (charging ownership) ──────────────────────────────────────
    "graphql_charging_history": {
        "method": "POST",
        "base": GRAPHQL_BASE,
        "path": "/graphql",
        "desc": "Charging session history with full billing breakdown (GraphQL)",
        "auth": "Bearer",
        "operation": "getChargingHistoryV2",
        "notes": "The primary endpoint for billing/cost data",
    },
    "graphql_pending_balances": {
        "method": "POST",
        "base": GRAPHQL_BASE,
        "path": "/graphql",
        "desc": "Pending unpaid balances across charging services (GraphQL)",
        "auth": "Bearer",
        "operation": "getPendingBalances",
    },
    "graphql_charging_vehicles": {
        "method": "POST",
        "base": GRAPHQL_BASE,
        "path": "/graphql",
        "desc": "Vehicles linked to charging account (GraphQL)",
        "auth": "Bearer",
        "operation": "getChargingVehicles",
    },
    "graphql_payment_instrument": {
        "method": "POST",
        "base": GRAPHQL_BASE,
        "path": "/graphql",
        "desc": "Payment method details for a payment source (GraphQL)",
        "auth": "Bearer",
        "operation": "GetInstrumentBySource",
        "notes": "Variables: paymentSource (SUPERCHARGE), countryCode, currencyCode",
    },

    # ── Fleet API (official, for reference) ───────────────────────────────
    "fleet_vehicles": {
        "method": "GET",
        "base": FLEET_API_BASE,
        "path": "/api/1/vehicles",
        "desc": "[Fleet API] Vehicle list — requires partner app credentials",
        "auth": "Bearer (partner token)",
        "notes": "Official API. Requires separate Fleet API registration.",
    },
    "fleet_charging_history": {
        "method": "GET",
        "base": FLEET_API_BASE,
        "path": "/api/1/vehicles/{vin}/charging_history",
        "desc": "[Fleet API] Official charging history — less billing detail than GraphQL",
        "auth": "Bearer (partner token)",
        "notes": "Returns sessions but NOT detailed per-session cost breakdown",
    },
}

# ---------------------------------------------------------------------------
# GraphQL queries
# ---------------------------------------------------------------------------

GRAPHQL_QUERIES = {
    "getChargingHistoryV2": {
        "variables": {
            "pageNumber": 1,
            "sortBy": "start_datetime",
            "sortOrder": "DESC",
        },
        "query": """
query getChargingHistoryV2($pageNumber: Int!, $sortBy: String, $sortOrder: SortByEnum) {
  me {
    chargingHistoryV2(pageNumber: $pageNumber, sortBy: $sortBy, sortOrder: $sortOrder) {
      totalResults
      data {
        chargeSessionId
        vin
        siteLocationName
        chargeStartDateTime
        chargeStopDateTime
        fees { feeType amountDue totalDue currencyCode pricingType paymentStatus }
        invoices { fileName contentId invoiceType }
        address { city stateProvinceCode country }
      }
    }
  }
}""",
    },
    "getPendingBalances": {
        "variables": {},
        "query": """
query getPendingBalances {
  me {
    pendingBalances {
      amount
      countryCode
      currencyCode
      sessionFeeIds
    }
  }
}""",
    },
    "getChargingVehicles": {
        "variables": {},
        "query": """
query getChargingVehicles {
  me {
    chargingVehicles {
      vin
      carType
      deliveryDate
      imageUrl
    }
  }
}""",
    },
    "GetInstrumentBySource": {
        "variables": {
            "paymentSource": "SUPERCHARGE",
            "countryCode": "US",
            "currencyCode": "USD",
        },
        "query": """
query GetInstrumentBySource($paymentSource: PaymentSource!, $countryCode: String!, $currencyCode: String!) {
  me {
    paymentInstrument(paymentSource: $paymentSource, countryCode: $countryCode, currencyCode: $currencyCode) {
      expiryMonth
      expiryYear
      lastFourDigits
      paymentChannels
      paymentInstrumentType
      accountType
    }
  }
}""",
    },
}

# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------

def do_get(session: requests.Session, url: str, params: dict = None) -> requests.Response:
    return session.get(url, params=params, timeout=30)


def do_post(session: requests.Session, url: str, body: dict,
            params: dict = None) -> requests.Response:
    return session.post(url, json=body, params=params, timeout=30)


def call_graphql(session: requests.Session, operation: str,
                 extra_vars: dict = None, vin: str = None) -> dict:
    """Call a named GraphQL operation."""
    if operation not in GRAPHQL_QUERIES:
        raise ValueError(f"Unknown GraphQL operation: {operation}. "
                         f"Known: {list(GRAPHQL_QUERIES.keys())}")

    q = GRAPHQL_QUERIES[operation]
    variables = dict(q["variables"])
    if extra_vars:
        variables.update(extra_vars)

    params = {
        "operationName": operation,
        "deviceLanguage": "en",
        "deviceCountry": "US",
        "ttpLocale": "en_US",
    }
    if vin:
        params["vin"] = vin

    payload = {
        "operationName": operation,
        "variables": variables,
        "query": q["query"],
    }

    resp = session.post(f"{GRAPHQL_BASE}/graphql", json=payload, params=params, timeout=30)
    return resp


def print_response(resp: requests.Response, pretty: bool = True):
    """Print an HTTP response with status, headers summary, and body."""
    print(f"\n{'='*60}")
    print(f"Status: {resp.status_code} {resp.reason}")
    print(f"Content-Type: {resp.headers.get('Content-Type', '?')}")
    print(f"Content-Length: {len(resp.content):,} bytes")
    print(f"{'='*60}")

    content_type = resp.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            data = resp.json()
            if pretty:
                print(json.dumps(data, indent=2, default=str))
            else:
                print(data)
        except Exception:
            print(resp.text[:2000])
    elif "pdf" in content_type:
        print(f"[PDF response — {len(resp.content):,} bytes. Use --save to download.]")
    else:
        print(resp.text[:2000])

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Tesla API endpoint explorer — probe any endpoint interactively",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python explore_endpoints.py --list
  python explore_endpoints.py --get /api/1/users/me
  python explore_endpoints.py --get /api/1/vehicles/1234567890/nearby_charging_sites
  python explore_endpoints.py --ownership /mobile-app/subscriptions/invoices
  python explore_endpoints.py --graphql getChargingHistoryV2
  python explore_endpoints.py --graphql GetInstrumentBySource --gql-vars '{"paymentSource":"SUPERCHARGE","countryCode":"FR","currencyCode":"EUR"}'
  python explore_endpoints.py --raw GET https://owner-api.teslamotors.com/api/1/products
        """,
    )
    parser.add_argument("--list", action="store_true",
                        help="List all known endpoints with descriptions")
    parser.add_argument("--get", type=str, metavar="PATH",
                        help="GET an Owner API path (e.g. /api/1/vehicles)")
    parser.add_argument("--ownership", type=str, metavar="PATH",
                        help="GET an ownership.tesla.com path")
    parser.add_argument("--graphql", type=str, metavar="OPERATION",
                        help=f"Named GraphQL operation. One of: {list(GRAPHQL_QUERIES.keys())}")
    parser.add_argument("--gql-vars", type=str, default=None,
                        help="JSON string of extra GraphQL variables (e.g. '{\"pageNumber\":2}')")
    parser.add_argument("--vin", type=str, default=None,
                        help="VIN to pass as query param (for endpoints that need it)")
    parser.add_argument("--raw", nargs="+", metavar=("METHOD", "URL"),
                        help="Make a raw request: --raw GET https://...")
    parser.add_argument("--params", type=str, default=None,
                        help="JSON string of query parameters")
    parser.add_argument("--save", type=str, default=None, metavar="FILE",
                        help="Save response body to file (useful for PDFs)")
    parser.add_argument("--no-pretty", action="store_true",
                        help="Disable JSON pretty-printing")
    args = parser.parse_args()

    if args.list:
        print(f"\n{'Name':<35} {'Method':<6} {'Description'}")
        print("-" * 90)
        for name, ep in KNOWN_ENDPOINTS.items():
            print(f"{name:<35} {ep['method']:<6} {ep['desc']}")
        print()
        print("GraphQL operations:", ", ".join(GRAPHQL_QUERIES.keys()))
        return

    # Authenticate
    print("[auth] Loading credentials...")
    try:
        session = auth.authed_session()
    except RuntimeError as exc:
        print(f"[auth] {exc}")
        sys.exit(1)

    resp = None
    extra_params = json.loads(args.params) if args.params else {}

    if args.get:
        url = OWNER_API_BASE + args.get
        print(f"[explore] GET {url}")
        resp = do_get(session, url, params=extra_params or None)

    elif args.ownership:
        url = OWNERSHIP_BASE + args.ownership
        params = {"deviceCountry": "US", "deviceLanguage": "en"}
        if args.vin:
            params["vin"] = args.vin
        params.update(extra_params)
        print(f"[explore] GET {url}")
        resp = do_get(session, url, params=params)

    elif args.graphql:
        extra_vars = json.loads(args.gql_vars) if args.gql_vars else None
        print(f"[explore] GraphQL POST → {args.graphql}")
        try:
            resp = call_graphql(session, args.graphql, extra_vars=extra_vars, vin=args.vin)
        except ValueError as exc:
            print(f"[error] {exc}")
            sys.exit(1)

    elif args.raw:
        if len(args.raw) < 2:
            print("[error] --raw requires METHOD and URL")
            sys.exit(1)
        method, url = args.raw[0].upper(), args.raw[1]
        print(f"[explore] {method} {url}")
        if method == "GET":
            resp = do_get(session, url, params=extra_params or None)
        elif method == "POST":
            body = extra_params or {}
            resp = do_post(session, url, body=body)
        else:
            print(f"[error] Unsupported method: {method}")
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(0)

    if resp is not None:
        print_response(resp, pretty=not args.no_pretty)

        if args.save:
            with open(args.save, "wb") as f:
                f.write(resp.content)
            print(f"\n[saved] Response written to {args.save}")


if __name__ == "__main__":
    main()
