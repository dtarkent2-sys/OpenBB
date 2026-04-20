# OpenBB SharkQuant Provider

Serves OpenBB standard models from the SharkQuant Postgres archive
(`fmp_bulk` + `fmp_feeds` tables) instead of calling FMP live. Data
is pre-populated nightly by the `fmp-bulk-snapshot` + `fmp-daily-feeds`
jobs in the SharkQuant dashboard repo.

## Why use this instead of `fmp`

- **Free queries** — no FMP credits consumed; we've already paid for
  the bulk pulls
- **Offline-capable** — works even when FMP is down
- **Fast** — Postgres lookup + JSONB index beats REST round-trips
- **Complete history** — every nightly snapshot is archived with
  `captured_date`, so point-in-time queries are possible

## Setup

```bash
pip install -e openbb_platform/providers/sharkquant
export DATABASE_URL="postgresql://sharkquant:...@localhost:5432/sharkquant"
```

OpenBB auto-discovers via the `openbb_provider_extension` entry point.

## Endpoints (v0.1)

| OpenBB call | Backed by |
|---|---|
| `obb.equity.profile(symbol, provider='sharkquant')` | `fmp_bulk` where `bulk_name='profile-bulk'` |

## Adding more endpoints

See `FMP` provider next door (`openbb_platform/providers/fmp/`) for the
standard-model → fetcher pattern. For each new endpoint, map the FMP
bulk table to the standard model via `__alias_dict__`.
