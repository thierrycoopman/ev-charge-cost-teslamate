# Tesla Private API — Complete Endpoint Reference

> **Disclaimer:** All endpoints in this document are unofficial/reverse-engineered from the Tesla mobile app. They are not covered by any public SLA, can change without notice, and are not endorsed by Tesla. See the main README for ToS considerations.

---

## Base URLs

| Service | Base URL | Region |
|---|---|---|
| Owner API | `https://owner-api.teslamotors.com` | Global |
| Ownership BFF | `https://ownership.tesla.com` | Global |
| Charging GraphQL | `https://akamai-apigateway-charging-ownership.tesla.com` | Global |
| Tesla Account | `https://www.tesla.com` | Global |
| Auth | `https://auth.tesla.com` | Global (non-CN) |
| Fleet API (official) | `https://fleet-api.prd.na.vn.cloud.tesla.com` | North America |
| Fleet API (official) | `https://fleet-api.prd.eu.vn.cloud.tesla.com` | Europe |
| Owner API (China) | `https://owner-api.vn.cloud.tesla.cn` | China |
| Auth (China) | `https://auth.tesla.cn` | China |

---

## Authentication Headers (Required for all endpoints)

```http
Authorization: Bearer <access_token>
User-Agent: Tesla/1195 CFNetwork/1388 Darwin/22.0.0
x-tesla-user-agent: TeslaApp/4.30.6/ios/17.0
Content-Type: application/json
Accept: application/json
```

---

## 1. Account & User Endpoints

### GET `/api/1/users/me`
**Base:** `owner-api.teslamotors.com`
**Description:** Current authenticated user's account information.

**Response:**
```json
{
  "response": {
    "email": "user@example.com",
    "full_name": "Thierry Dupont",
    "profile_image_url": "https://...",
    "vault_uuid": "uuid-...",
    "referral_code": "THIERRY12345"
  }
}
```

---

### GET `/api/1/users/keys`
**Base:** `owner-api.teslamotors.com`
**Description:** Paired phone keys and key cards associated with vehicles.

---

### GET `/api/1/users/referral_data`
**Base:** `owner-api.teslamotors.com`
**Description:** Referral program status, referral code, and accrued credits.

---

### GET `/api/1/users/feature_config`
**Base:** `owner-api.teslamotors.com`
**Description:** Feature flags enabled for this account (beta features, regional features).

---

## 2. Vehicle List Endpoints

### GET `/api/1/products`
**Base:** `owner-api.teslamotors.com`
**Description:** List all products on the account (vehicles + Powerwall/Solar).
**Notes:** Preferred over `/vehicles` as of 2024.

**Response (vehicle):**
```json
{
  "id": 1234567890,
  "vehicle_id": 987654321,
  "vin": "LRW...",
  "display_name": "Mon Tesla",
  "state": "online",
  "color": null,
  "access_type": "OWNER",
  "tokens": ["abc123", "def456"],
  "option_codes": "AD15,MDL3,...",
  "in_service": false,
  "id_s": "1234567890",
  "calendar_enabled": true,
  "api_version": 67,
  "backseat_token": null,
  "backseat_token_updated_at": null
}
```

**State values:** `online`, `asleep`, `offline`, `waking`, `updating`

---

### GET `/api/1/vehicles`
**Base:** `owner-api.teslamotors.com`
**Description:** Vehicle list (older endpoint). Removed for some accounts in Jan 2024.

---

## 3. Vehicle Data Endpoints

> These endpoints require the vehicle to be **online/awake** (state = "online").
> A sleeping vehicle returns `408 Request Timeout`. Wake it first via POST `.../wake_up`.

### POST `/api/1/vehicles/{id}/wake_up`
**Description:** Send a wake signal to the vehicle. Poll GET `/api/1/vehicles/{id}` until `state == "online"`.

---

### GET `/api/1/vehicles/{id}/vehicle_data`
**Description:** Composite data endpoint — fetches multiple state objects in a single call.

**Query params:**
```
?endpoints=charge_state;climate_state;drive_state;gui_settings;vehicle_config;vehicle_state
```

**Response sections:**
| Key | Description |
|---|---|
| `charge_state` | Battery level, charging status, rate, session info |
| `climate_state` | Temperatures, HVAC, seat heaters |
| `drive_state` | GPS, speed, power, heading, shift state |
| `gui_settings` | Unit preferences (miles/km, 12/24h, etc.) |
| `vehicle_config` | Static config: trim, options, color, efficiency |
| `vehicle_state` | Locks, sentry, software version, odometer |

---

### GET `/api/1/vehicles/{id}/data_request/charge_state`
**Description:** Current charging & battery state.

**Key fields:**
```json
{
  "battery_level": 72,
  "usable_battery_level": 71,
  "battery_range": 204.5,
  "ideal_battery_range": 221.3,
  "charging_state": "Disconnected",
  "fast_charger_present": false,
  "fast_charger_type": "<invalid>",
  "fast_charger_brand": null,
  "charge_rate": 0,
  "charger_power": 0,
  "charge_energy_added": 15.2,
  "charge_miles_added_ideal": 48.3,
  "charge_miles_added_rated": 44.8,
  "charge_limit_soc": 80,
  "minutes_to_full_charge": 0,
  "charge_session_id": "CS-...",
  "conn_charge_cable": "<invalid>",
  "charger_voltage": 0,
  "charger_actual_current": 0,
  "charger_phases": null,
  "charger_pilot_current": 32,
  "scheduled_charging_mode": "Off",
  "scheduled_charging_start_time": null,
  "time_to_full_charge": 0
}
```

**Charging state values:** `Charging`, `Complete`, `Disconnected`, `NoPower`, `Starting`, `Stopped`

---

### GET `/api/1/vehicles/{id}/data_request/drive_state`
**Description:** Real-time vehicle position and motion.

**Key fields:**
```json
{
  "latitude": 46.9480,
  "longitude": 7.4474,
  "heading": 247,
  "speed": null,
  "power": -5,
  "shift_state": "P",
  "odometer": 42381.7,
  "gps_as_of": 1700000000,
  "native_location_supported": 1,
  "native_latitude": 46.9480,
  "native_longitude": 7.4474,
  "native_type": "wgs"
}
```

---

### GET `/api/1/vehicles/{id}/data_request/climate_state`
**Key fields:** `inside_temp`, `outside_temp`, `driver_temp_setting`, `is_climate_on`, `fan_status`, `seat_heater_left/right`, `is_preconditioning`, `battery_heater`

---

### GET `/api/1/vehicles/{id}/data_request/vehicle_config`
**Description:** Static vehicle configuration (does not require awake vehicle).

**Key fields:** `car_type`, `trim_badging`, `exterior_color`, `wheel_type`, `roof_color`, `efficiency_package`, `has_air_suspension`, `has_ludicrous_mode`, `motorized_charge_port`, `plg`, `rear_seat_heaters`, `rhd`, `sun_roof_installed`

---

### GET `/api/1/vehicles/{id}/nearby_charging_sites`
**Description:** Superchargers and destination chargers near vehicle's current GPS location.

**Response:**
```json
{
  "superchargers": [
    {
      "type": "supercharger",
      "name": "Bern Bethlehem",
      "location": { "lat": 46.948, "long": 7.447 },
      "distance_miles": 2.3,
      "available_stalls": 4,
      "total_stalls": 12,
      "site_closed": false,
      "billing_info": "CHF 0.319/kWh",
      "billing_time_zone_id": "Europe/Zurich"
    }
  ],
  "destination_charging": [
    {
      "type": "destination",
      "name": "Hotel Bellevue",
      "location": { "lat": 46.951, "long": 7.449 },
      "distance_miles": 3.1
    }
  ]
}
```

---

### GET `/api/1/vehicles/{id}/service_data`
**Description:** Service center appointment and history data. Not always populated.

---

## 4. Supercharger Billing — GraphQL Endpoint

**Base URL:** `https://akamai-apigateway-charging-ownership.tesla.com/graphql`

All operations use `POST` with `Content-Type: application/json`.

Common query parameters for all operations:
```
?operationName=<operation>
&deviceLanguage=fr
&deviceCountry=FR
&ttpLocale=fr_FR
&vin=<VIN>          (optional, filters to one vehicle)
```

---

### Operation: `getChargingHistoryV2`

Paginated Supercharger session history with full billing breakdown.

**Variables:**
```json
{
  "pageNumber": 1,
  "sortBy": "start_datetime",
  "sortOrder": "DESC"
}
```

**Key response fields per session:** See `docs/supercharger-billing.md` for the full schema.

**Notes:** This is the primary endpoint for cost data. Returns ~25 sessions per page.

---

### Operation: `getPendingBalances`

Returns any unpaid Supercharger balances on the account.

**Variables:** `{}` (none)

**Response:**
```json
{
  "data": {
    "me": {
      "pendingBalances": [
        {
          "amount": 4.50,
          "countryCode": "CH",
          "currencyCode": "CHF",
          "sessionFeeIds": ["SFI-001", "SFI-002"]
        }
      ]
    }
  }
}
```

---

### Operation: `getChargingVehicles`

Lists vehicles registered to your charging account.

**Variables:** `{}` (none)

**Response:**
```json
{
  "data": {
    "me": {
      "chargingVehicles": [
        {
          "vin": "LRW...",
          "carType": "MODEL_3",
          "deliveryDate": "2022-06-15",
          "imageUrl": "https://..."
        }
      ]
    }
  }
}
```

---

### Operation: `GetInstrumentBySource`

Returns payment method details for a given payment source.

**Variables:**
```json
{
  "paymentSource": "SUPERCHARGE",
  "countryCode": "CH",
  "currencyCode": "CHF"
}
```

**Response:**
```json
{
  "data": {
    "me": {
      "paymentInstrument": {
        "expiryMonth": 12,
        "expiryYear": 2026,
        "lastFourDigits": "4242",
        "paymentChannels": ["SUPERCHARGE"],
        "paymentInstrumentType": "CREDIT_CARD",
        "accountType": "INDIVIDUAL"
      }
    }
  }
}
```

---

## 5. Invoice & Receipt Endpoints

### GET `/mobile-app/charging/invoice/{content_id}`
**Base:** `https://ownership.tesla.com`
**Description:** Download a Supercharger session receipt as PDF.

**Query params:**
```
?deviceCountry=CH
&deviceLanguage=fr
&vin=<VIN>
```

**Response:** `application/pdf` — the official Tesla charging receipt

**Get `content_id`** from `getChargingHistoryV2` response → `invoices[].contentId`

---

### GET `/mobile-app/subscriptions/invoices`
**Base:** `https://ownership.tesla.com`
**Description:** List subscription invoices (FSD subscription, Premium Connectivity, etc.)
**Notes:** Returns 400 or 404 in some regions. May require additional parameters.

---

### GET `/mobile-app/charging/subscription/invoice/{invoice_id}`
**Base:** `https://ownership.tesla.com`
**Description:** Download a subscription invoice PDF.

---

## 6. Charging History via Tesla.com (REST, older)

### GET `/teslaaccount/charging/api/history`
**Base:** `https://www.tesla.com`

**Query params:**
```
?vin=<VIN>
```

**Notes:** This is an older REST endpoint. It may:
- Require a session cookie in addition to (or instead of) the Bearer token
- Return less detail than the GraphQL endpoint
- Redirect to the login page if the token type doesn't match

---

## 7. Official Fleet API (Reference)

These are **official** endpoints requiring Fleet API partner registration:

| Endpoint | Description |
|---|---|
| `GET /api/1/vehicles` | Vehicle list |
| `GET /api/1/vehicles/{vin}/charging_history` | Charging history (no cost data) |
| `GET /api/1/vehicles/{vin}/charge_state` | Battery & charging state |
| `GET /api/1/vehicles/{vin}/nearby_charging_sites` | Nearby Superchargers |
| `GET /api/1/vehicles/{vin}/location_data` | GPS location |
| `POST /api/1/vehicles/{vin}/command/charge_start` | Start charging |
| `POST /api/1/vehicles/{vin}/command/charge_stop` | Stop charging |

Documentation: [developer.tesla.com/docs/fleet-api](https://developer.tesla.com/docs/fleet-api)

---

## HTTP Status Codes

| Code | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request (missing/invalid parameters) |
| `401` | Unauthorized — expired or wrong token |
| `403` | Forbidden — wrong User-Agent, IP block, or wrong token type |
| `404` | Endpoint not found or deprecated |
| `408` | Request timeout — vehicle is asleep |
| `429` | Rate limited — slow down requests |
| `503` | Tesla server overloaded — retry with backoff |

---

## Rate Limits

Tesla does not publish rate limits. Based on community observation:
- Polling vehicle data more often than every **30 seconds** risks rate limiting
- History/invoice endpoints: conservative is 1 request/second
- The mobile app polls every 3-5 seconds when actively monitoring a charging session

---

## Community Resources

- **timdorr/tesla-api** — Original community documentation: https://tesla-api.timdorr.com/
- **TeslaPy** — Python library: https://github.com/tdorssers/TeslaPy
- **TeslaMate** — Self-hosted logger: https://github.com/teslamate-org/teslamate
- **Tesla Motors Club API threads** — https://teslamotorsclub.com/tmc/tags/api/
- **Tesla Fleet API (official)** — https://developer.tesla.com/docs/fleet-api
