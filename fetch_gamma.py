"""
Polymarket Historical Data Fetcher — v5
Changes from v4:
- No volume filter at all during fetch — collect everything
- Smart stop: skips pages where ALL markets are outside date range
  (handles future-dated markets that appear first in API results)
- Continues scrolling until it finds markets in the target window
- Volume filtering happens entirely in backtest.py
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
MAX_PAGES  = 600
RATE_DELAY = 0.3

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


def parse_market(m, dataset):
    yes_p, no_p = parse_outcome_prices(m.get("outcomePrices"))

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


async def fetch_and_store(conn, cfg):
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

    total_stored       = 0
    total_skipped      = 0
    skip_reasons       = {}
    page               = 0
    consecutive_future = 0   # pages where all markets are in the future
    consecutive_past   = 0   # pages where all markets are before our window

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
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

            page_stored  = 0
            page_dates   = []

            for m in markets:
                p = parse_market(m, dataset)

                if p["end_date_str"]:
                    page_dates.append(p["end_date_str"])

                # Minimal validity checks — no volume filter
                if not p["market_id"] or not p["question"]:
                    skip_reasons["no_id"] = skip_reasons.get("no_id", 0) + 1
                    total_skipped += 1
                    continue
                if p["resolved_yes"] is None:
                    skip_reasons["no_outcome"] = skip_reasons.get("no_outcome", 0) + 1
                    total_skipped += 1
                    continue
                if p["initial_price"] is None:
                    skip_reasons["no_price"] = skip_reasons.get("no_price", 0) + 1
                    total_skipped += 1
                    continue

                # Date filter
                d = p["end_date_str"]
                if d:
                    if d > end_max:
                        skip_reasons["future"] = skip_reasons.get("future", 0) + 1
                        total_skipped += 1
                        continue
                    if d < end_min:
                        skip_reasons["too_old"] = skip_reasons.get("too_old", 0) + 1
                        total_skipped += 1
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

            # Determine page date range
            min_date = min(page_dates) if page_dates else None
            max_date = max(page_dates) if page_dates else None

            log.info(
                "Page %d | stored=%d | total=%d | dates=%s→%s | skips=%s",
                page + 1, page_stored, total_stored,
                min_date, max_date, skip_reasons
            )

            # Smart stopping logic
            if page_dates:
                all_future = all(d > end_max for d in page_dates)
                all_past   = all(d < end_min for d in page_dates)

                if all_future:
                    consecutive_future += 1
                    if consecutive_future >= 3:
                        log.info("3 consecutive future pages — skipping ahead faster")
                        # Don't stop — keep scrolling, just log
                else:
                    consecutive_future = 0

                if all_past:
                    consecutive_past += 1
                    if consecutive_past >= 3:
                        log.info("3 consecutive past pages — passed our date window, stopping")
                        break
                else:
                    consecutive_past = 0

            if len(markets) < PAGE_SIZE:
                log.info("Last page")
                break

            page += 1
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

    # Clear and re-fetch
    existing = await conn.fetchval("SELECT COUNT(*) FROM historical_markets")
    if existing > 0:
        log.info("Clearing %d existing records...", existing)
        await conn.execute("TRUNCATE TABLE historical_markets RESTART IDENTITY")

    for cfg in DATASETS:
        await fetch_and_store(conn, cfg)

    await conn.close()
    log.info("All done — run backtest.py next")


if __name__ == "__main__":
    asyncio.run(main())