"""Postgres access for the SharkQuant provider.

Each fetcher acquires a short-lived connection, runs its query, closes.
No pooling here — OpenBB fetchers are called infrequently enough that
psycopg2's per-request overhead is negligible.
"""
from __future__ import annotations

import os
from typing import Any


def _dsn(credentials: dict[str, str] | None) -> str:
    """Resolve the Postgres DSN from credentials or environment.

    OpenBB passes `credentials` into `aextract_data` — we pull
    DATABASE_URL from there if set, else fall back to os.environ.
    """
    if credentials:
        url = credentials.get("sharkquant_database_url") or credentials.get("DATABASE_URL")
        if url:
            return url
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "SharkQuant provider requires DATABASE_URL (either in OpenBB credentials "
            "as 'sharkquant_database_url' or the DATABASE_URL env var)"
        )
    return url


def fetch_bulk_rows(
    credentials: dict[str, str] | None,
    bulk_name: str,
    symbols: list[str],
    *,
    bulk_key: str | None = None,
    latest_only: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return a list of JSONB `data` payloads from `fmp_bulk`.

    Args:
        bulk_name: e.g. 'profile-bulk', 'ratios-ttm-bulk'
        symbols: filter by `primary_key` (FMP stores symbol there)
        bulk_key: narrow to a specific capture (e.g. '2025-Q4'); None = any
        latest_only: pick the most recent captured_date per primary_key
        limit: cap result count

    Returns:
        list of dicts (the raw JSON FMP returned for that row).
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor

    dsn = _dsn(credentials)
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            params: list[Any] = [bulk_name]
            sql_parts = ["bulk_name = %s"]
            if symbols:
                sql_parts.append("primary_key = ANY(%s)")
                params.append([s.upper() for s in symbols])
            if bulk_key:
                sql_parts.append("bulk_key = %s")
                params.append(bulk_key)
            where = " AND ".join(sql_parts)

            if latest_only:
                query = f"""
                    SELECT DISTINCT ON (primary_key) primary_key, captured_date, data
                    FROM fmp_bulk
                    WHERE {where}
                    ORDER BY primary_key, captured_date DESC
                """
            else:
                query = f"""
                    SELECT primary_key, captured_date, data
                    FROM fmp_bulk
                    WHERE {where}
                    ORDER BY captured_date DESC, primary_key
                """
            if limit:
                query += f" LIMIT {int(limit)}"

            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
