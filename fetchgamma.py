"""
Polymarket Historical Data Fetcher
Pulls resolved market data from Polymarket's Gamma API
and stores it in your PostgreSQL database.

Start command: python fetch_gamma.py

Fetches two datasets:
  - Primary:    Apr 2025 - Mar 2026 (12 months)
  - Validation: Jan 2024 - Mar 2025 (15 months)

Safe to re-run — skips markets already in the DB via ON CONFLICT DO NOTHING.
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
MIN_VOLUME = 1000
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
    try:
        prices = json.loads(val) if isinstance(val, str) else val
        return float(prices[0]) if prices else None
    except Exception:
        return None


def parse_dt(val):
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def parse_market(m, dataset):
    bid = parse_price(m.get("bestBid"))
    ask = parse_price(m.get("bestAsk"))
    yes_price = parse_outcome_prices(m.get("outcomePrices"))
    mid = (bid + ask) / 2 if bid and ask else yes_price
    created_at = parse_dt(m.get("startDate") or m.get("createdAt"))
    end_date   = parse_dt(m.get("endDate"))
    duration   = None
    if created_at and end_date:
        duration = round((end_date - created_at).total_seconds() / 3600, 1)
    final = parse_outcome_prices(m.get("outcomePrices"))
    resolved_yes = None
    if m.get("resolved") and final is not None:
        resolved_yes = 1 if final >= 0.99 else 0
    return {
        "market_id":               str(m.get("id", "")),
        "question":                m.get("question", ""),
        "raw_category":            m.get("category", ""),
        "dataset":                 dataset,
        "created_at":              created_at,
        "end_date":                end_date,
        "initial_bid":             bid,
        "initial_ask":             ask,
        "initial_price":           mid,
        "resolution_price":        final,
        "resolved_yes":            resolved_yes,
        "total_volume_usd":        parse_price(m.get("volumeNum") or m.get("volume")),
        "volume_24h_usd":          parse_price(m.get("volume24hr")),
        "time_to_resolution_hours": duration,
        "fetched_at":              datetime.utcnow(),
    }


def is_valid(p, start, end):
    if not p["market_id"] or not p["question"]:
        return False
    if p["resolved_yes"] is None:
        return False
    if p["initial_price"] is None:
        return False
    if not (0.05 <= p["initial_price"] <= 0.95):
        return False
    if not p["total_volume_usd"] or p["total_volume_usd"] < MIN_VOLUME:
        return False
    if p["created_at"]:
        d = p["created_at"].strftime("%Y-%m-%d")
        if not (start <= d <= end):
            return False
    return True


async def fetch_and_store(conn, cfg):
    label, dataset, start, end = cfg["label"], cfg["dataset"], cfg["start"], cfg["end"]
    log.info("=" * 55)
    log.info("Fetching: %s", label)
    log.info("=" * 55)

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    log.info("Already in DB: %d", existing)

    total_stored = 0
    page = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }
    base_params = {
        "closed": "true", "resolved": "true",
        "order": "startDate", "ascending": "false",
        "start_date_min": start, "start_date_max": end,
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

            page_stored = 0
            for m in markets:
                p = parse_market(m, dataset)
                if not is_valid(p, start, end):
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

            log.info("Page %d: %d markets, %d stored (total: %d)",
                     page + 1, len(markets), page_stored, total_stored)

            if len(markets) < PAGE_SIZE:
                log.info("Last page")
                break

            page += 1
            await asyncio.sleep(RATE_DELAY)

    final = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    yes_rate = await conn.fetchval(
        "SELECT ROUND(AVG(resolved_yes::float) * 100, 1) FROM historical_markets WHERE dataset=$1",
        dataset
    )
    log.info("Done: %d total in DB, YES rate=%.1f%%", final, yes_rate or 0)


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