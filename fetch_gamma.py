"""
Polymarket Historical Data Fetcher — v6
Changes from v5:
- Binary search approach: probes ahead to find the right offset
  before starting the main fetch loop
- Skips thousands of future-dated pages in seconds instead of minutes
- Still no volume filter — collect everything, filter in backtest
"""

import asyncio
import aiohttp
import asyncpg
import os
import json
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

GAMMA_BASE = "https://gamma-api.polymarket.com/markets"
PAGE_SIZE  = 100
MAX_PAGES  = 20000  # 40 pages/day × 365 days = ~14,600 pages for primary alone
RATE_DELAY = 0.1  # reduced from 0.3 — speeds up from 73min to 25min per dataset

DATASETS = [
    {
        "label":        "Primary (ended Apr 2025 - Mar 2026)",
        "dataset":      "primary",
        "end_date_min": "2025-04-01",
        "end_date_max": "2026-03-20",
    },
    {
        "label":        "Validation (ended Jan 2024 - Mar 2025)",
        "dataset":      "validation",
        "end_date_min": "2024-01-01",
        "end_date_max": "2025-04-01",
    },
]


async def init_table(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_markets (
            id                       SERIAL PRIMARY KEY,
            market_id                TEXT NOT NULL UNIQUE,
            question                 TEXT NOT NULL,
            raw_category             TEXT,
            dataset                  TEXT NOT NULL,
            created_at               TIMESTAMP,
            end_date                 TIMESTAMP,
            end_date_str             TEXT,
            initial_bid              FLOAT,
            initial_ask              FLOAT,
            initial_price            FLOAT,
            resolution_price         FLOAT,
            resolved_yes             INTEGER,
            total_volume_usd         FLOAT,
            volume_24h_usd           FLOAT,
            time_to_resolution_hours FLOAT,
            fetched_at               TIMESTAMP NOT NULL
        )
    """)
    # Safe column additions for tables created by earlier versions
    for col, typedef in [
        ("end_date_str", "TEXT"),
        ("volume_24h_usd", "FLOAT"),
        ("time_to_resolution_hours", "FLOAT"),
    ]:
        await conn.execute(
            f"ALTER TABLE historical_markets ADD COLUMN IF NOT EXISTS {col} {typedef}"
        )
    log.info("historical_markets table ready")


def parse_price(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_outcome_prices(val):
    try:
        prices = json.loads(val) if isinstance(val, str) else val
        if not prices or not isinstance(prices, list):
            return None, None
        yes_p = float(prices[0]) if len(prices) > 0 else None
        return yes_p, None
    except Exception:
        return None, None


def parse_dt(val):
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def get_page_end_dates(markets):
    """Extract end date strings from a list of raw market dicts."""
    dates = []
    for m in markets:
        end_date = parse_dt(m.get("endDate"))
        if end_date:
            dates.append(end_date.strftime("%Y-%m-%d"))
    return dates


async def fetch_page_raw(session, offset, headers):
    """Fetch a single page, return raw market list."""
    params = {
        "closed": "true", "resolved": "true",
        "order": "endDate", "ascending": "false",
        "limit": PAGE_SIZE, "offset": offset
    }
    try:
        async with session.get(
            GAMMA_BASE, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 429:
                await asyncio.sleep(10)
                return []
            return []
    except Exception as e:
        log.warning("Fetch error at offset %d: %s", offset, e)
        return []


async def find_start_offset(session, headers, target_end_max):
    """
    Binary search to find the approximate offset where markets
    start having end dates <= target_end_max.
    Returns the starting page offset.
    """
    log.info("Binary searching for start of date window (end_max=%s)...", target_end_max)

    lo, hi = 0, 500  # search between page 0 and page 500
    best_offset = 0

    for _ in range(10):  # max 10 binary search steps
        mid = (lo + hi) // 2
        offset = mid * PAGE_SIZE
        markets = await fetch_page_raw(session, offset, headers)
        if not markets:
            hi = mid
            continue

        dates = get_page_end_dates(markets)
        if not dates:
            hi = mid
            continue

        oldest = min(dates)
        newest = max(dates)
        log.info("  Probe page %d: dates %s → %s", mid, oldest, newest)

        if newest > target_end_max:
            # Still in the future — go further right (higher offset)
            lo = mid + 1
            best_offset = mid + 1
        else:
            # We've gone past the window or are in it — go left
            hi = mid

        await asyncio.sleep(0.3)

    start_page = max(0, best_offset - 2)  # back up 2 pages to be safe
    log.info("Starting fetch at page %d (offset %d)", start_page, start_page * PAGE_SIZE)
    return start_page


def parse_market(m, dataset):
    yes_p, _ = parse_outcome_prices(m.get("outcomePrices"))
    bid = parse_price(m.get("bestBid"))
    ask = parse_price(m.get("bestAsk"))

    if bid is not None and ask is not None and 0 < bid < ask:
        initial_price = round((bid + ask) / 2, 4)
    elif yes_p is not None:
        initial_price = yes_p
    else:
        initial_price = None

    resolved_yes = None
    if (m.get("resolved") or m.get("closed")) and yes_p is not None:
        if yes_p >= 0.90:
            resolved_yes = 1
        elif yes_p <= 0.10:
            resolved_yes = 0

    created_at   = parse_dt(m.get("startDate") or m.get("createdAt"))
    end_date     = parse_dt(m.get("endDate"))
    end_date_str = end_date.strftime("%Y-%m-%d") if end_date else None

    duration = None
    if created_at and end_date and end_date > created_at:
        duration = round((end_date - created_at).total_seconds() / 3600, 1)

    return {
        "market_id":               str(m.get("id", "")),
        "question":                m.get("question", ""),
        "raw_category":            m.get("category", ""),
        "dataset":                 dataset,
        "created_at":              created_at,
        "end_date":                end_date,
        "end_date_str":            end_date_str,
        "initial_bid":             bid,
        "initial_ask":             ask,
        "initial_price":           initial_price,
        "resolution_price":        yes_p,
        "resolved_yes":            resolved_yes,
        "total_volume_usd":        parse_price(m.get("volumeNum") or m.get("volume")),
        "volume_24h_usd":          parse_price(m.get("volume24hr")),
        "time_to_resolution_hours": duration,
        "fetched_at":              datetime.utcnow(),
    }


async def fetch_and_store(conn, cfg, session, headers):
    label   = cfg["label"]
    dataset = cfg["dataset"]
    end_min = cfg["end_date_min"]
    end_max = cfg["end_date_max"]

    log.info("=" * 55)
    log.info("Fetching: %s", label)
    log.info("End date range: %s → %s", end_min, end_max)
    log.info("=" * 55)

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    log.info("Already in DB: %d", existing)

    # Binary search for starting offset
    start_page = await find_start_offset(session, headers, end_max)

    total_stored  = 0
    skip_reasons  = {}
    page          = start_page
    consecutive_past = 0

    pages_fetched = 0
    while pages_fetched < MAX_PAGES:
        offset  = page * PAGE_SIZE
        markets = await fetch_page_raw(session, offset, headers)

        if not markets:
            log.info("Empty page — done")
            break

        page_stored = 0
        page_dates  = []

        for m in markets:
            p = parse_market(m, dataset)
            if p["end_date_str"]:
                page_dates.append(p["end_date_str"])

            # Minimal validity
            if not p["market_id"] or not p["question"]:
                skip_reasons["no_id"] = skip_reasons.get("no_id", 0) + 1
                continue
            if p["resolved_yes"] is None:
                skip_reasons["no_outcome"] = skip_reasons.get("no_outcome", 0) + 1
                continue
            if p["initial_price"] is None:
                skip_reasons["no_price"] = skip_reasons.get("no_price", 0) + 1
                continue

            # Date filter
            d = p["end_date_str"]
            if d:
                if d > end_max:
                    skip_reasons["future"] = skip_reasons.get("future", 0) + 1
                    continue
                if d < end_min:
                    skip_reasons["too_old"] = skip_reasons.get("too_old", 0) + 1
                    continue

            try:
                await conn.execute("""
                    INSERT INTO historical_markets (
                        market_id, question, raw_category, dataset,
                        created_at, end_date, end_date_str,
                        initial_bid, initial_ask, initial_price,
                        resolution_price, resolved_yes,
                        total_volume_usd, volume_24h_usd,
                        time_to_resolution_hours, fetched_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                    ON CONFLICT (market_id) DO NOTHING
                """,
                    p["market_id"], p["question"], p["raw_category"], p["dataset"],
                    p["created_at"], p["end_date"], p["end_date_str"],
                    p["initial_bid"], p["initial_ask"], p["initial_price"],
                    p["resolution_price"], p["resolved_yes"],
                    p["total_volume_usd"], p["volume_24h_usd"],
                    p["time_to_resolution_hours"], p["fetched_at"]
                )
                page_stored += 1
                total_stored += 1
            except Exception as e:
                log.warning("Insert error: %s", e)

        min_date = min(page_dates) if page_dates else None
        max_date = max(page_dates) if page_dates else None

        log.info(
            "Page %d | stored=%d | total=%d | dates=%s→%s",
            page, page_stored, total_stored, min_date, max_date
        )

        # Stop when we've gone past the window
        if page_dates and all(d < end_min for d in page_dates):
            consecutive_past += 1
            if consecutive_past >= 3:
                log.info("Passed date window — stopping")
                break
        else:
            consecutive_past = 0

        if len(markets) < PAGE_SIZE:
            log.info("Last page")
            break

        page += 1
        pages_fetched += 1
        await asyncio.sleep(RATE_DELAY)

    final = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    yes_rate = await conn.fetchval(
        "SELECT ROUND(AVG(resolved_yes::float) * 100, 1) "
        "FROM historical_markets WHERE dataset=$1", dataset
    )
    log.info("─" * 55)
    log.info("Done: %s", label)
    log.info("Total stored: %d", final)
    log.info("YES rate:     %.1f%%", yes_rate or 0)
    log.info("Skip reasons: %s", skip_reasons)
    log.info("─" * 55)


async def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        return

    conn = await asyncpg.connect(DATABASE_URL)
    await init_table(conn)

    existing = await conn.fetchval("SELECT COUNT(*) FROM historical_markets")
    if existing > 0:
        log.info("Clearing %d existing records...", existing)
        await conn.execute("TRUNCATE TABLE historical_markets RESTART IDENTITY")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        for cfg in DATASETS:
            await fetch_and_store(conn, cfg, session, headers)

    await conn.close()
    log.info("All done — run backtest.py next")


if __name__ == "__main__":
    asyncio.run(main())