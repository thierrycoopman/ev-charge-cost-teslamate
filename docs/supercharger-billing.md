# Supercharger Billing & Charging Cost Data

> The single most useful thing this private API gives you that the official Fleet API does **not**: detailed per-session cost breakdowns with tier pricing, parking fees, and downloadable invoices.

---

## The Core Problem

The **official Tesla Fleet API** (`fleet-api.prd.*.cloud.tesla.com`) has a `charging_history` endpoint, but:
- It returns session data **without itemized costs**
- It does **not** provide invoice PDFs
- It requires a registered partner application (OAuth app registration)
- Tier-based pricing breakdown is absent

The **private GraphQL API** the Tesla app uses (`akamai-apigateway-charging-ownership.tesla.com`) gives you:
- ✅ Full cost breakdown per fee type (CHARGING, PARKING)
- ✅ Per-tier pricing (Tier 1/2/3 in kWh or per-minute)
- ✅ Currency and exchange rate info
- ✅ Invoice PDF references
- ✅ Payment status (PAID, PENDING, etc.)
- ✅ MSP (Supercharger membership) credit tracking
- ✅ Site-level coordinates and address

---

## Primary Endpoint: GraphQL Charging History

### Request

```
POST https://akamai-apigateway-charging-ownership.tesla.com/graphql
     ?operationName=getChargingHistoryV2
     &deviceLanguage=en
     &deviceCountry=FR
     &ttpLocale=fr_FR
     &vin=<VIN>                  ← optional filter
```

**Headers:**
```http
Authorization: Bearer <access_token>
Content-Type: application/json
User-Agent: Tesla/1195 CFNetwork/1388 Darwin/22.0.0
x-tesla-user-agent: TeslaApp/4.30.6/ios/17.0
```

**Body:**
```json
{
  "operationName": "getChargingHistoryV2",
  "variables": {
    "pageNumber": 1,
    "sortBy": "start_datetime",
    "sortOrder": "DESC"
  },
  "query": "query getChargingHistoryV2(...) { ... }"
}
```

See `scripts/get_charging_history.py` for the complete GraphQL query.

### Response Structure

```json
{
  "data": {
    "me": {
      "chargingHistoryV2": {
        "totalResults": 47,
        "data": [
          {
            "chargeSessionId": "CS-abc123",
            "sessionId": "S-xyz789",
            "vin": "LRW...",
            "vehicleMakeType": "MODEL_3",
            "siteLocationName": "Bern Bethlehem",
            "chargeStartDateTime": "2024-11-15T09:23:41+01:00",
            "chargeStopDateTime":  "2024-11-15T10:05:17+01:00",
            "unlatchDateTime":     "2024-11-15T10:07:03+01:00",
            "countryCode": "CH",
            "programType": "SUPERCHARGER",
            "billingType": "PAYMENT_CARD",
            "isMsp": false,

            "chargingPackage": {
              "energyApplied": 38.7,
              "distance": null,
              "distanceUnit": null
            },

            "fees": [
              {
                "sessionFeeId": "SFI-001",
                "feeType": "CHARGING",
                "payorUid": "uid-user-123",
                "amountDue": 12.34,
                "currencyCode": "CHF",
                "pricingType": "PER_KWH",
                "usageBase": 0,
                "usageTier1": 38.7,
                "usageTier2": 0,
                "usageTier3": 0,
                "totalDue": 12.34,
                "paymentStatus": "PAID",
                "paymentStatusDisplayText": "Paid",
                "rate": {
                  "unitRate": 0.319,
                  "unitRateBase": 0,
                  "unitRateTier1": 0.319,
                  "unitRateTier2": 0,
                  "unitRateTier3": 0,
                  "programType": "SUPERCHARGER",
                  "rateDisplayText": "CHF 0.319/kWh"
                }
              },
              {
                "sessionFeeId": "SFI-002",
                "feeType": "PARKING",
                "amountDue": 0,
                "currencyCode": "CHF",
                "pricingType": "PER_MINUTE",
                "totalDue": 0,
                "paymentStatus": "PAID",
                "rate": {
                  "unitRate": 0.50,
                  "rateDisplayText": "CHF 0.50/min after grace period"
                }
              }
            ],

            "invoices": [
              {
                "fileName": "Tesla_Supercharger_20241115.pdf",
                "contentId": "a1b2c3d4-e5f6-...",
                "invoiceType": "CHARGING_RECEIPT"
              }
            ],

            "address": {
              "streetAddress": "Murtenstrasse 123",
              "city": "Bern",
              "stateProvinceCode": "BE",
              "postalCode": "3018",
              "country": "Switzerland"
            },

            "coordinate": {
              "latitude": 46.9480,
              "longitude": 7.4474
            },

            "credit": null
          }
        ]
      }
    }
  }
}
```

---

## Fee Types

| `feeType` | Description |
|---|---|
| `CHARGING` | Electricity cost (per kWh or per minute) |
| `PARKING` | Idle/overstay fee after charging completes |
| `MEMBERSHIP` | MSP (Supercharger membership) fee |
| `CREDIT` | Applied credit/discount (negative amount) |

---

## Pricing Types

| `pricingType` | Description |
|---|---|
| `PER_KWH` | Charged per kilowatt-hour delivered |
| `PER_MINUTE` | Charged per minute (less common in EU, common in some US states) |
| `FREE` | Free Supercharging (promotional/included in vehicle purchase) |
| `MSP` | Member Supercharger Program (subscription) |

---

## Tier Pricing Explained

Tesla Superchargers sometimes use tiered pricing based on power level:

```
Tier 1: Low power (≤ X kW) — charged at unitRateTier1
Tier 2: Medium power        — charged at unitRateTier2
Tier 3: High power          — charged at unitRateTier3

usageTier1/2/3 = kWh consumed at that tier
unitRateTier1/2/3 = price per kWh at that tier
```

In many European countries, all charging is at a single rate (only Tier 1 is non-zero).

---

## Downloading Invoice PDFs

Once you have a `contentId` from the history response:

```
GET https://ownership.tesla.com/mobile-app/charging/invoice/<contentId>
    ?deviceCountry=FR
    &deviceLanguage=fr
    &vin=<VIN>
```

**Headers:** Same as above (Bearer + User-Agent)

**Response:** `application/pdf` — the official Tesla charging receipt

```bash
# Using curl (after obtaining a token)
TOKEN=$(cat ~/.tesla_tokens.json | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -H "Authorization: Bearer $TOKEN" \
     -H "User-Agent: Tesla/1195 CFNetwork/1388 Darwin/22.0.0" \
     -H "x-tesla-user-agent: TeslaApp/4.30.6/ios/17.0" \
     "https://ownership.tesla.com/mobile-app/charging/invoice/a1b2c3d4-e5f6-...?deviceCountry=FR&deviceLanguage=fr&vin=LRW..." \
     -o charging_receipt.pdf
```

Or use the automated script:
```bash
python scripts/get_invoices.py --output ./my_invoices
```

---

## Pending Balances

Check for unpaid Supercharger charges:

```json
POST https://akamai-apigateway-charging-ownership.tesla.com/graphql
     ?operationName=getPendingBalances

{
  "query": "query getPendingBalances { me { pendingBalances { amount countryCode currencyCode sessionFeeIds } } }",
  "variables": {}
}
```

---

## Alternative REST Endpoint (Older, Less Detail)

```
GET https://www.tesla.com/teslaaccount/charging/api/history?vin=<VIN>
```

This older endpoint may return charging history in a simpler format, but:
- Does **not** include per-tier pricing breakdown
- May redirect to the Tesla login page if the token isn't recognized as a "web" session token
- Less reliable than the GraphQL endpoint

---

## Comparing APIs: What You Get

| Data Point | Owner API / GraphQL | Official Fleet API |
|---|---|---|
| Session list (dates, location) | ✅ | ✅ |
| Energy delivered (kWh) | ✅ | ✅ |
| **Total cost** | ✅ | ❌ |
| **Cost breakdown by tier** | ✅ | ❌ |
| **Per-kWh rate** | ✅ | ❌ |
| **Parking fee** | ✅ | ❌ |
| **Invoice PDF download** | ✅ | ✅ (requires partner app) |
| **Pending unpaid balance** | ✅ | ❌ |
| **Payment status** | ✅ | ❌ |
| **MSP credit tracking** | ✅ | ❌ |
| GPS coordinates of site | ✅ | ✅ |
| Supercharger stall availability | ✅ (nearby sites) | ✅ (nearby sites) |
| Requires app registration | ❌ | ✅ |

---

## Known Limitations

1. **Token type matters**: The `akamai-apigateway` endpoint expects a token obtained via the mobile app OAuth flow (not a Fleet API partner token). If you get 403, re-authenticate with `auth.py`.

2. **Historical limit**: Tesla may only return sessions from the last 2 years via this API.

3. **Region mismatch**: Pass the correct `deviceCountry`/`ttpLocale` matching your Tesla account's country to get correct currency and formatting.

4. **Free Supercharging**: Sessions with `programType: FREE` will show `totalDue: 0` and `pricingType: FREE`. No invoice is generated.

5. **API changes**: Tesla has changed this endpoint multiple times. If it stops working, check community resources:
   - [timdorr/tesla-api GitHub discussions](https://github.com/timdorr/tesla-api/discussions)
   - [TeslaMate GitHub discussions](https://github.com/teslamate-org/teslamate/discussions)
   - [Tesla Motors Club API forum](https://teslamotorsclub.com/tmc/tags/api/)
