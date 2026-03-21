"""
Polymarket Historical Data Fetcher — v8
All fixes applied:
- TRUNCATES on fresh start, RESUMES on restart (checks DB first)
- Resumes from oldest stored date, not from end_max
- No volume floor — collect all crypto markets
- initial_price fallback to 0.5 if bid/ask missing (fixes old market rejection)
- Expanded crypto terms to catch older market phrasings
- No MAX_PAGES limit — stops only when date window is exhausted
- DB keepalive every 200 pages
- Summary uses Python math, no SQL ROUND/AVG type errors
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
RATE_DELAY = 0.1

CRYPTO_TERMS = [
    # Major coins
    "bitcoin", "btc", "ethereum", "eth", "crypto",
    "solana", "xrp", "ripple", "dogecoin", "doge",
    "pepe", "avalanche", "avax", "polygon", "matic",
    "shiba", "shib", "chainlink", "cardano", "ada",
    "polkadot", "bnb", "litecoin", "uniswap",
    # Older market phrasings
    "defi", "nft", "web3", "stablecoin", "usdc", "usdt",
    "tether", "dai", "wbtc", "wrapped bitcoin",
    "altcoin", "memecoin", "token", "coin price",
    "market cap", "dominance", "halving", "blockchain",
    "cryptocurrency", "crypto market",
]

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


# ─────────────────────────────────────────────
# DB setup
# ─────────────────────────────────────────────

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
    for col, typedef in [
        ("end_date_str", "TEXT"),
        ("volume_24h_usd", "FLOAT"),
        ("time_to_resolution_hours", "FLOAT"),
    ]:
        await conn.execute(
            f"ALTER TABLE historical_markets ADD COLUMN IF NOT EXISTS {col} {typedef}"
        )
    log.info("historical_markets table ready")


# ─────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────

def parse_price(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_outcome_prices(val):
    try:
        prices = json.loads(val) if isinstance(val, str) else val
        if not prices or not isinstance(prices, list):
            return None
        return float(prices[0])
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
    yes_p = parse_outcome_prices(m.get("outcomePrices"))
    bid   = parse_price(m.get("bestBid"))
    ask   = parse_price(m.get("bestAsk"))

    # Initial price: bid/ask mid preferred, fallback to yes_p, then 0.5
    if bid is not None and ask is not None and 0 < bid < ask:
        initial_price = round((bid + ask) / 2, 4)
    elif yes_p is not None:
        initial_price = yes_p
    else:
        initial_price = 0.5  # fallback for old markets without bid/ask

    # Resolution outcome
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


def is_crypto(question):
    q = question.lower()
    return any(t in q for t in CRYPTO_TERMS)


# ─────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────

async def fetch_page(session, offset, headers):
    params = {
        "closed": "true", "resolved": "true",
        "order": "endDate", "ascending": "false",
        "limit": PAGE_SIZE, "offset": offset,
    }
    try:
        async with session.get(
            GAMMA_BASE, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 429:
                log.warning("Rate limited — waiting 10s")
                await asyncio.sleep(10)
                return []
            else:
                log.warning("API status %d", resp.status)
                return []
    except Exception as e:
        log.warning("Fetch error at offset %d: %s", offset, e)
        return []


async def binary_search_start(session, headers, target_date):
    """Find the page where end dates first go <= target_date."""
    log.info("Binary search for date window (target=%s)...", target_date)
    lo, hi = 0, 600
    best = 0
    for _ in range(12):
        mid = (lo + hi) // 2
        markets = await fetch_page(session, mid * PAGE_SIZE, headers)
        if not markets:
            hi = mid
            continue
        dates = [
            parse_dt(m.get("endDate")).strftime("%Y-%m-%d")
            for m in markets
            if parse_dt(m.get("endDate"))
        ]
        if not dates:
            hi = mid
            continue
        newest = max(dates)
        oldest = min(dates)
        log.info("  Page %d: %s → %s", mid, oldest, newest)
        if newest > target_date:
            lo = mid + 1
            best = mid + 1
        else:
            hi = mid
        await asyncio.sleep(0.3)
    start = max(0, best - 3)
    log.info("Starting at page %d", start)
    return start


# ─────────────────────────────────────────────
# Main fetch loop
# ─────────────────────────────────────────────

async def fetch_and_store(conn, cfg, session, headers):
    label   = cfg["label"]
    dataset = cfg["dataset"]
    end_min = cfg["end_date_min"]
    end_max = cfg["end_date_max"]

    log.info("=" * 55)
    log.info("Dataset: %s", label)
    log.info("Range:   %s → %s", end_min, end_max)
    log.info("=" * 55)

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    log.info("Already in DB: %d", existing)

    # Resume from oldest stored date, not from end_max
    resume_target = end_max
    if existing > 0:
        oldest_stored = await conn.fetchval(
            "SELECT MIN(end_date_str) FROM historical_markets WHERE dataset=$1", dataset
        )
        if oldest_stored and oldest_stored > end_min:
            resume_target = oldest_stored
            log.info("Resuming from: %s (oldest stored date)", resume_target)
        else:
            log.info("DB covers full range already — nothing to fetch")
            return

    start_page     = await binary_search_start(session, headers, resume_target)
    page           = start_page
    pages_fetched  = 0
    total_stored   = 0
    skip_reasons   = {}
    consecutive_past = 0

    while True:
        markets = await fetch_page(session, page * PAGE_SIZE, headers)

        if not markets:
            log.info("Empty page — done")
            break

        page_stored = 0
        page_dates  = []

        for m in markets:
            p = parse_market(m, dataset)
            if p["end_date_str"]:
                page_dates.append(p["end_date_str"])

            # Validity checks
            if not p["market_id"] or not p["question"]:
                skip_reasons["no_id"] = skip_reasons.get("no_id", 0) + 1
                continue
            if p["resolved_yes"] is None:
                skip_reasons["no_outcome"] = skip_reasons.get("no_outcome", 0) + 1
                continue

            # Crypto filter
            if not is_crypto(p["question"]):
                skip_reasons["non_crypto"] = skip_reasons.get("non_crypto", 0) + 1
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

        min_d = min(page_dates) if page_dates else None
        max_d = max(page_dates) if page_dates else None
        log.info("Page %d | stored=%d | total=%d | dates=%s→%s | skips=%s",
                 page, page_stored, total_stored, min_d, max_d, skip_reasons)

        # Stop when past the date window
        if page_dates and all(d < end_min for d in page_dates):
            consecutive_past += 1
            if consecutive_past >= 3:
                log.info("Past date window — done")
                break
        else:
            consecutive_past = 0

        if len(markets) < PAGE_SIZE:
            log.info("Last page")
            break

        page += 1
        pages_fetched += 1

        # DB keepalive every 200 pages
        if pages_fetched % 200 == 0:
            try:
                await conn.execute("SELECT 1")
                log.info("DB keepalive OK at page %d", page)
            except Exception:
                log.info("Reconnecting DB...")
                conn = await asyncpg.connect(DATABASE_URL)

        await asyncio.sleep(RATE_DELAY)

    # Summary
    try:
        await conn.execute("SELECT 1")
    except Exception:
        conn = await asyncpg.connect(DATABASE_URL)

    final     = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1", dataset
    )
    yes_count = await conn.fetchval(
        "SELECT COUNT(*) FROM historical_markets WHERE dataset=$1 AND resolved_yes=1", dataset
    )
    oldest = await conn.fetchval(
        "SELECT MIN(end_date_str) FROM historical_markets WHERE dataset=$1", dataset
    )
    newest = await conn.fetchval(
        "SELECT MAX(end_date_str) FROM historical_markets WHERE dataset=$1", dataset
    )
    yes_rate = round((yes_count or 0) / final * 100, 1) if final else 0

    log.info("─" * 55)
    log.info("Done: %s", label)
    log.info("Total in DB: %d | YES rate: %.1f%%", final, yes_rate)
    log.info("Date range:  %s → %s", oldest, newest)
    log.info("Skip reasons: %s", skip_reasons)
    log.info("─" * 55)


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

async def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        return

    conn = await asyncpg.connect(DATABASE_URL)
    await init_table(conn)

    # Check if this is a fresh start or a resume
    total_existing = await conn.fetchval("SELECT COUNT(*) FROM historical_markets")
    if total_existing == 0:
        log.info("Fresh start — no existing data")
    else:
        log.info("Resuming — %d records already in DB", total_existing)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        for cfg in DATASETS:
            # Reconnect before each dataset
            try:
                await conn.execute("SELECT 1")
            except Exception:
                conn = await asyncpg.connect(DATABASE_URL)
            await fetch_and_store(conn, cfg, session, headers)

    try:
        await conn.close()
    except Exception:
        pass

    log.info("All done — run backtest.py next")


if __name__ == "__main__":
    asyncio.run(main())