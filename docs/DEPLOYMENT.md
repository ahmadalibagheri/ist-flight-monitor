# Production deployment

## 1. Prepare the host

Requirements: Docker Engine 24+ with the Compose plugin, 2 GB RAM, and disk for the
database. Storage grows with observation count:

```
rows/day ≈ flights_per_day × (60 / COLLECTION_INTERVAL_MINUTES) × 24
```

At ~25 flights/day and hourly collection that is ~600 observation rows/day — roughly
100–150 MB/year including indexes. Small.

## 2. Configure

```bash
git clone <repo> && cd airport
cp .env.example .env
```

Set, at minimum:

```dotenv
ENVIRONMENT=production
LOG_FORMAT=json

POSTGRES_PASSWORD=<long random string>
DATABASE_URL=postgresql+psycopg://flightmon:<same password>@db:5432/flightmon

PROVIDER_CHAIN=aerodatabox
AERODATABOX_API_KEY=<your key>

TELEGRAM_BOT_TOKEN=<token>
TELEGRAM_CHAT_ID=<chat id>

CORS_ORIGINS=https://flights.example.com
NEXT_PUBLIC_API_URL=https://flights.example.com/api
```

Generate the password with `openssl rand -base64 36`.

`ENVIRONMENT=production` hard-disables the mock provider regardless of
`ALLOW_MOCK_PROVIDER`.

> `NEXT_PUBLIC_API_URL` is read at **request time** by the frontend container, so changing
> it needs only `docker compose up -d frontend` — no image rebuild. (The value is injected
> into the page as `window.__API_BASE__`; see `frontend/lib/api.ts::apiBase()`.)

## 3. Launch

```bash
docker compose up -d
docker compose ps
curl -s localhost:8000/ready | python3 -m json.tool
```

`ready: true` requires a reachable database and at least one configured REAL provider in
the chain.

## 4. Put a TLS reverse proxy in front

The stack serves plain HTTP and ships no authentication. Terminate TLS and restrict the
admin surface upstream. Example nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name flights.example.com;

    ssl_certificate     /etc/letsencrypt/live/flights.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/flights.example.com/privkey.pem;

    # Dashboard
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Public API
    location /api/ {
        proxy_pass http://127.0.0.1:8000/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # Health probes
    location ~ ^/(health|ready|providers)$ {
        proxy_pass http://127.0.0.1:8000;
    }

    # Admin endpoints mutate state and are unauthenticated - lock them down.
    location /api/v1/admin/ {
        allow 10.0.0.0/8;
        deny all;
        proxy_pass http://127.0.0.1:8000/api/v1/admin/;
    }
}
```

Then stop exposing the container ports publicly — bind them to localhost in an override
file:

```yaml
# docker-compose.override.yml
services:
  api:
    ports: ["127.0.0.1:8000:8000"]
  frontend:
    ports: ["127.0.0.1:3000:3000"]
  db:
    ports: []          # database reachable only on the compose network
```

## 5. Scaling

- **The scheduler must run exactly once.** The `worker` service owns it; the `api` service
  sets `SCHEDULER_ENABLED=false`. If you scale the API (`docker compose up -d --scale
  api=3`), keep the worker at one replica or you will collect and alert multiple times per
  cycle.
- Duplicate alerts are still prevented by the `UNIQUE(dedup_key)` constraint on
  `sent_alerts`, but duplicate collection wastes API quota.
- The API is stateless and safe to scale horizontally behind the proxy.

## 6. Backups

The observation history is the irreplaceable asset — it cannot be re-fetched from any
provider after the fact.

```bash
# Nightly dump
docker compose exec -T db pg_dump -U flightmon -Fc flightmon > flightmon-$(date +%F).dump

# Restore
docker compose exec -T db pg_restore -U flightmon -d flightmon --clean --if-exists \
  < flightmon-2026-09-06.dump
```

Statistics tables need no backup — rebuild them after a restore:

```bash
curl -X POST "localhost:8000/api/v1/admin/aggregate?start_date=2026-01-01&end_date=$(date +%F)"
```

## 7. Upgrades

```bash
git pull
docker compose build
docker compose up -d          # the api container runs `alembic upgrade head` on start
docker compose logs api | head -40
```

Migrations run inside the API container before uvicorn binds, so a failed migration means
the API never starts serving rather than serving against a wrong schema.

To migrate manually:

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
```

## 8. Monitoring

| Probe | Meaning |
|---|---|
| `GET /health` | Process is alive. Use for liveness. |
| `GET /ready` | Database reachable **and** a real provider configured. Use for readiness. |
| `GET /providers` | Per-provider health, failure counts, cooldowns. |
| `GET /api/v1/reports/collection-runs` | Did the last cycles succeed, and what did they write? |

Alert your own monitoring on:

- `/ready` returning `ready: false`
- the newest `collection_runs` row being older than 2 × `COLLECTION_INTERVAL_MINUTES`
- `provider_health.consecutive_failures` at or above `PROVIDER_FAILURE_THRESHOLD`

Structured JSON logs are emitted to stdout; ship them with your existing Docker log driver.

## 9. Secrets

`.env` holds live credentials. Keep it `chmod 600`, owned by the deploy user, and out of
version control (it is git-ignored). For managed platforms, prefer injecting the same
variables through the platform's secret store rather than mounting a file; the application
reads only environment variables, so no code change is needed.

Rotating a provider key is a `.env` edit plus `docker compose up -d`.
