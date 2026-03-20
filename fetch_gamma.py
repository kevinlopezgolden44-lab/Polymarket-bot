"""
Polymarket Historical Data Fetcher — Fixed Version
Pulls resolved market data from Polymarket's Gamma API
and stores it in your PostgreSQL database.

Start command: python fetch_gamma.py

Changes from v1:
- Added debug output for first page to diagnose field issues
- Removed date params (Gamma API ignores them) — filters client-side instead
- Fixed outcomePrices parsing to correctly detect YES/NO resolution
- Loosened filters to accept more markets
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
MAX_PAGES  = 300
MIN_VOLUME = 500   # lowered from 1000 to capture more markets
RATE_DELAY = 0.4

DATASETS = [
    {
        "label":   "Primary (Apr 2025 - Mar 2026)",
        "dataset": "primary",
        "start":   "2025-04-01",
        "end":     "2026-03-20",
    },
    {
        "label":   "Validation (Jan 2024 - Mar 2025)",
        "dataset": "validation",
        "start":   "2024-01-01",
        "end":     "2025-04-01",
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
    log.info("historical_markets table ready")


def parse_price(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_outcome_prices(val):
    """Parse outcomePrices — returns (yes_price, no_price) tuple."""
    try:
        prices = json.loads(val) if isinstance(val, str) else val
        if not prices or not isinstance(prices, list):
            return None, None
        yes_p = float(prices[0]) if len(prices) > 0 else None
        no_p  = float(prices[1]) if len(prices) > 1 else None
        return yes_p, no_p
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


def debug_market(m):
    """Print key fields from a market for debugging."""
    log.info("  DEBUG market fields:")
    for key in ["id", "question", "resolved", "closed", "outcomePrices",
                "bestBid", "bestAsk", "volumeNum", "volume", "volume24hr",
                "startDate", "createdAt", "endDate", "category"]:
        val = m.get(key)
        if isinstance(val, str) and len(val) > 80:
            val = val[:80] + "..."
        log.info("    %s: %s", key, repr(val))


def parse_market(m, dataset):
    """
    Parse a market from the Gamma API response.

    Key insight: outcomePrices at resolution time:
    - If resolved YES: prices = ["1", "0"] or ["0.99", "0.01"]
    - If resolved NO:  prices = ["0", "1"] or ["0.01", "0.99"]
    - If still open:   prices = mid-market prices like ["0.45", "0.55"]
    """
    yes_p, no_p = parse_outcome_prices(m.get("outcomePrices"))

    bid = parse_price(m.get("bestBid"))
    ask = parse_price(m.get("bestAsk"))

    # For resolved markets, outcomePrices shows final resolution
    # For the initial price we want the mid at time of creation
    # Since we don't have historical snapshots, use current bid/ask mid
    # or fall back to yes_p if bid/ask not available
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        initial_price = (bid + ask) / 2
    elif yes_p is not None:
        initial_price = yes_p
    else:
        initial_price = None

    # Determine resolution outcome
    resolved_yes = None
    is_resolved = m.get("resolved", False)
    is_closed   = m.get("closed", False)

    if (is_resolved or is_closed) and yes_p is not None:
        if yes_p >= 0.95:
            resolved_yes = 1   # resolved YES
        elif yes_p <= 0.05:
            resolved_yes = 0   # resolved NO
        # else: ambiguous resolution, skip

    created_at = parse_dt(m.get("startDate") or m.get("createdAt"))
    end_date   = parse_dt(m.get("endDate"))

    duration = None
    if created_at and end_date:
        duration = round((end_date - created_at).total_seconds() / 3600, 1)

    return {
        "market_id":               str(m.get("id", "")),
        "question":                m.get("question", ""),
        "raw_category":            m.get("category", ""),
        "dataset":                 dataset,
        "created_at":              created_at,
        "end_date":                end_date,
        "initial_bid":             bid,
        "initial_ask":             ask,
        "initial_price":           initial_price,
        "resolution_price":        yes_p,
        "resolved_yes":            resolved_yes,
        "total_volume_usd":        parse_price(m.get("volumeNum") or m.get("volume")),
        "volume_24h_usd":          parse_price(m.get("volume24hr")),
        "time_to_resolution_hours": duration,
        "fetched_at":              datetime.utcnow(),
        "_created_str":            created_at.strftime("%Y-%m-%d") if created_at else None,
    }


def is_valid(p, start, end):
    """Quality and date filters."""
    if not p["market_id"] or not p["question"]:
        return False, "no_id_or_question"
    if p["resolved_yes"] is None:
        return False, "no_outcome"
    if p["initial_price"] is None:
        return False, "no_price"
    if not p["total_volume_usd"] or p["total_volume_usd"] < MIN_VOLUME:
        return False, f"low_volume_{p['total_volume_usd']}"
    # Date filter on creation date
    d = p["_created_str"]
    if d and not (start <= d <= end):
        return False, f"wrong_date_{d}"
    return True, None


async def fetch_and_store(conn, cfg):
    label, dataset, start, end = cfg["label"], cfg["dataset"], cfg["start"], cfg["end"]
    log.info("=" * 55)
    log.info("Fetching: %s", label)
    log.info("Date range: %s → %s", start, end)
    log.info("=" * 55)

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    log.info("Already in DB: %d", existing)

    total_stored  = 0
    total_skipped = 0
    skip_reasons  = {}
    page = 0
    debug_done = False

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }

    # NOTE: Gamma API date params are unreliable — we filter client-side instead
    base_params = {
        "closed":    "true",
        "resolved":  "true",
        "order":     "endDate",
        "ascending": "false",
    }

    async with aiohttp.ClientSession() as session:
        while page < MAX_PAGES:
            offset = page * PAGE_SIZE
            params = {**base_params, "limit": PAGE_SIZE, "offset": offset}

            try:
                async with session.get(
                    GAMMA_BASE, params=params, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 429:
                        log.warning("Rate limited — waiting 10s")
                        await asyncio.sleep(10)
                        continue
                    if resp.status != 200:
                        log.error("API error %d", resp.status)
                        break
                    markets = await resp.json()
            except Exception as e:
                log.error("Fetch error: %s", e)
                break

            if not markets:
                log.info("Empty page — done")
                break

            # Debug first page to show actual field values
            if not debug_done and markets:
                log.info("--- DEBUG: First market in response ---")
                debug_market(markets[0])
                log.info("--- Sample parsed ---")
                sample = parse_market(markets[0], dataset)
                for k, v in sample.items():
                    log.info("  %s: %s", k, repr(v))
                log.info("--- End debug ---")
                debug_done = True

            page_stored = 0
            for m in markets:
                p = parse_market(m, dataset)
                valid, reason = is_valid(p, start, end)
                if not valid:
                    total_skipped += 1
                    # Track top skip reasons
                    r_key = reason.split("_")[0] if "_" in reason else reason
                    skip_reasons[r_key] = skip_reasons.get(r_key, 0) + 1
                    continue

                try:
                    await conn.execute("""
                        INSERT INTO historical_markets (
                            market_id, question, raw_category, dataset,
                            created_at, end_date, initial_bid, initial_ask,
                            initial_price, resolution_price, resolved_yes,
                            total_volume_usd, volume_24h_usd,
                            time_to_resolution_hours, fetched_at
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                        ON CONFLICT (market_id) DO NOTHING
                    """,
                        p["market_id"], p["question"], p["raw_category"], p["dataset"],
                        p["created_at"], p["end_date"], p["initial_bid"], p["initial_ask"],
                        p["initial_price"], p["resolution_price"], p["resolved_yes"],
                        p["total_volume_usd"], p["volume_24h_usd"],
                        p["time_to_resolution_hours"], p["fetched_at"]
                    )
                    page_stored += 1
                    total_stored += 1
                except Exception as e:
                    log.warning("Insert error: %s", e)

            log.info(
                "Page %d | fetched=%d stored=%d skipped=%d | total_stored=%d",
                page + 1, len(markets), page_stored,
                total_skipped - (total_skipped - (len(markets) - page_stored)),
                total_stored
            )

            # Log skip reasons every 5 pages
            if page > 0 and page % 5 == 0:
                log.info("Skip reasons so far: %s", skip_reasons)

            if len(markets) < PAGE_SIZE:
                log.info("Last page")
                break

            page += 1
            await asyncio.sleep(RATE_DELAY)

    log.info("Skip reasons: %s", skip_reasons)

    final = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    log.info("Total stored for %s: %d", dataset, final)


async def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        return
    conn = await asyncpg.connect(DATABASE_URL)
    await init_table(conn)
    for cfg in DATASETS:
        await fetch_and_store(conn, cfg)
    await conn.close()
    log.info("All done — run backtest.py next")


if __name__ == "__main__":
    asyncio.run(main())