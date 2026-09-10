# Flight data providers

This system never invents flight data. Every number on the dashboard traces back to a
real provider response stored in `flight_observations`.

Providers sit behind one interface (`app/providers/base.py::FlightDataProvider`), so the
data source can be changed with a configuration edit and no code change:

```
FlightDataProvider (ABC)
    ├── AeroDataBoxProvider     app/providers/aerodatabox.py
    ├── AviationStackProvider   app/providers/aviationstack.py
    ├── FlightAwareProvider     app/providers/flightaware.py
    └── MockProvider            app/providers/mock.py   (development only)
```

`PROVIDER_CHAIN` is an ordered failover list. The first provider that is configured,
healthy, and returns data wins. If every provider in the chain fails, the cycle is
recorded as a **failed run** — the system never writes an empty board, because
"no flights returned" and "the provider is down" must not look the same in history.

---

## Comparison

| | AeroDataBox | AviationStack | FlightAware AeroAPI |
|---|---|---|---|
| **Role** | default | secondary | authoritative |
| **Discovers flights automatically** | yes (whole departure board) | yes (route-filtered) | yes (whole departure board) |
| **Cancellations** | yes (`withCancelled=true`) | yes (`flight_status=cancelled`) | yes (explicit `cancelled` boolean) |
| **Terminal / gate** | yes | yes | yes |
| **Aircraft** | model + registration | IATA type + registration | type + registration |
| **Cost model** | per API unit | per request | per returned result |
| **Free tier** | 600 units/month (RapidAPI Basic) | ~100 requests/month, **HTTP only** | trial credit, then billed |
| **Calls per cycle (2 routes)** | 3–4 (12h window chunks) | 2 (one per route) | 2 (scheduled + departed) |
| **TLS on free tier** | yes | **no** | yes |

### Recommended setup

- **Getting started / lowest cost:** AeroDataBox on the RapidAPI free tier. 600 units/month
  is enough for hourly collection over a 36-hour window if you keep
  `COLLECTION_INTERVAL_MINUTES=60`.
- **Production:** AeroDataBox Starter (direct, $19/month, 40k units) with FlightAware as a
  fallback: `PROVIDER_CHAIN=aerodatabox,flightaware`.
- **Highest accuracy regardless of cost:** `PROVIDER_CHAIN=flightaware,aerodatabox`.

---

## AeroDataBox (default)

**Endpoint used**

```
GET /flights/airports/iata/{code}/{fromLocal}/{toLocal}
    ?withLeg=true
    &direction=Departure
    &withCancelled=true
    &withCodeshared={INCLUDE_CODESHARE}
    &withCargo=false
    &withPrivate=false
    &withLocation=false
```

**Constraints handled in code**

- One request covers **at most 12 hours**; `AeroDataBoxProvider` chunks wider windows
  (`AERODATABOX_WINDOW_HOURS`).
- `{fromLocal}`/`{toLocal}` are **local airport time without an offset**
  (`2026-09-06T08:15`), not UTC.
- Times arrive as `{"utc": "2026-09-06 05:15Z", "local": "2026-09-06 08:15+03:00"}`. The
  UTC field is preferred; the local one is a fallback.
- A single call returns the board for *all* destinations, so both monitored routes are
  satisfied by one request. The response cache
  (`PROVIDER_CACHE_TTL_SECONDS`) keeps it that way.

**Getting a key**

1. Sign up at <https://rapidapi.com/aedbx-aedbx/api/aerodatabox> and subscribe to the
   Basic (free) plan.
2. Copy the `X-RapidAPI-Key` value into `.env`:

   ```dotenv
   AERODATABOX_API_KEY=your_rapidapi_key
   AERODATABOX_BASE_URL=https://aerodatabox.p.rapidapi.com
   AERODATABOX_AUTH_HEADER=X-RapidAPI-Key
   AERODATABOX_HOST_HEADER=aerodatabox.p.rapidapi.com
   ```

For a **direct** subscription (<https://aerodatabox.com/pricing>), use the base URL and
header name shown in your dashboard and clear the RapidAPI host header:

```dotenv
AERODATABOX_BASE_URL=https://api.aerodatabox.com
AERODATABOX_AUTH_HEADER=x-api-key
AERODATABOX_HOST_HEADER=
```

**Limitation:** AeroDataBox does not publish a cancellation *reason*. The
`cancellation_reason` column stays `NULL` rather than being filled with a guess.

---

## AviationStack (secondary)

**Endpoint used**

```
GET /v1/flights?access_key=...&dep_iata=IST&arr_iata=IKA&limit=100&offset=0
```

Statuses: `scheduled`, `active`, `landed`, `cancelled`, `incident`, `diverted`.

**Why it is cheap:** the API filters by route server-side, so cost is two requests per
cycle no matter how busy the airport is.

**Getting a key:** sign up at <https://aviationstack.com/product>, then:

```dotenv
AVIATIONSTACK_API_KEY=your_key
AVIATIONSTACK_BASE_URL=https://api.aviationstack.com/v1
PROVIDER_CHAIN=aviationstack
```

> **Security warning.** The free tier does **not** support TLS. To use it you must set
> `AVIATIONSTACK_BASE_URL=http://api.aviationstack.com/v1`, which sends your API key in
> cleartext. The provider logs a `provider.insecure_transport` warning when it detects
> this. Treat a free-tier key as disposable and never reuse that password anywhere else.
> A paid plan supports HTTPS.

**Limitation:** the free tier's ~100 requests/month is not enough for hourly collection
(a month needs ~1,440). Use it as a fallback, not a primary.

---

## FlightAware AeroAPI v4 (authoritative)

**Endpoints used** — both are queried and merged:

```
GET /airports/{id}/flights/scheduled_departures?start=...&end=...
GET /airports/{id}/flights/departures?start=...&end=...
```

`scheduled_departures` carries the upcoming board (where cancellations appear);
`departures` carries the settled outcome. When both describe the same leg, the more
settled record wins.

**Authentication:** `x-apikey: <your key>` header.

**Units gotcha (handled):** AeroAPI reports `departure_delay` and `arrival_delay` in
**seconds**, not minutes. `_seconds_to_minutes()` converts them; getting this wrong would
inflate every delay 60×.

**Getting a key:** register at <https://www.flightaware.com/aeroapi/portal/>, then:

```dotenv
FLIGHTAWARE_API_KEY=your_key
FLIGHTAWARE_BASE_URL=https://aeroapi.flightaware.com/aeroapi
PROVIDER_CHAIN=flightaware
```

**Limitation:** billed per returned result, so a busy airport board is not free. Keep it
late in the chain unless accuracy matters more than cost.

---

## MockProvider (development only)

Generates **synthetic** flights so the dashboard, scheduler and report pipeline can be
exercised without spending API quota.

Three independent guards keep it out of production analytics:

1. It refuses to run unless `ALLOW_MOCK_PROVIDER=true`.
2. It refuses to run when `ENVIRONMENT=production`, whatever the flag says.
3. Everything it emits is tagged `ProviderKind.MOCK`, persisted as `flights.is_mock =
   true`. Every analytics query filters those rows out (`app/analytics/queries.py::
   _base_select`), and `GET /flights` hides them unless you pass `include_mock=true`.

```dotenv
ENVIRONMENT=development
ALLOW_MOCK_PROVIDER=true
PROVIDER_CHAIN=mock
```

With only mock data present, the dashboard and reports correctly say **"No real flight
data collected yet"** — that is the quarantine working, not a bug.

---

## Evaluated and rejected

| Source | Why not |
|---|---|
| **OpenSky Network** | Free, but serves ADS-B state vectors only — no schedules and no cancellations. It cannot answer any question this system asks. |
| **Amadeus Flight Status** | Requires a known carrier + flight number + date per request, so it cannot *discover* flights. The spec requires automatic discovery. |
| **Flightradar24 / Cirium / OAG** | Excellent data, enterprise contracts and pricing. |
| **Scraping istairport.com** | Probed during design: the departures endpoint 301-redirects and the published terms discourage automated collection. A brittle scraper is a poor foundation for multi-month statistics, so it was not shipped. The provider interface makes it easy to add later if a licensed feed becomes available. |

---

## Adding a new provider

1. Implement `FlightDataProvider` in `app/providers/your_provider.py`:

   ```python
   class YourProvider(FlightDataProvider):
       name = "yours"
       kind = ProviderKind.REAL

       @property
       def is_configured(self) -> bool:
           return bool(settings.your_api_key)

       async def fetch_departures(self, origin, destinations, window_start, window_end):
           ...  # return ProviderResult(provider=self.name, flights=[...])
   ```

2. Return `NormalizedFlight` objects. Use `app/providers/normalization.py` for status
   mapping and delay derivation so the two safety rules hold automatically:
   **missing data is never a cancellation**, and **an unknown delay is never zero**.
3. Register it in `PROVIDER_FACTORIES` (`app/providers/registry.py`).
4. Add its settings to `app/core/config.py` and `.env.example`.
5. Add a parsing test in `backend/tests/test_providers.py` using a recorded payload.

Nothing else changes — the collector, analytics, reports and dashboard are provider-agnostic.
