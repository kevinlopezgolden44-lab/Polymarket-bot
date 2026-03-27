"""
Polymarket Historical Data Fetcher — v9

Key changes from v8:
- NO category filter — collects ALL categories (Crypto, Sports, Politics, Economics, Science)
- Filters multi-outcome markets (len(outcomePrices) > 2) — these corrupt binary analysis
- Stores additional fields: num_outcomes, raw_category_gamma, market_age_days,
  question_word_count, initial_spread
- 6 month windows: Primary Oct 2025-Mar 2026, Validation Apr-Sep 2025
  (back-to-back periods, same regime, clean comparison)
- Resume logic: picks up from oldest stored date on restart
- DB keepalive every 200 pages
- No MAX_PAGES — stops only when date window exhausted
- Summary uses Python math (no SQL ROUND type errors)
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

GAMMA_BASE  = "https://gamma-api.polymarket.com/markets"
PAGE_SIZE   = 100
RATE_DELAY  = 0.1

DATASETS = [
    {
        "label":        "Primary (Oct 2025 - Mar 2026)",
        "dataset":      "primary",
        "end_date_min": "2025-10-01",
        "end_date_max": "2026-03-20",
    },
    {
        "label":        "Validation (Apr 2025 - Sep 2025)",
        "dataset":      "validation",
        "end_date_min": "2025-04-01",
        "end_date_max": "2025-09-30",
    },
]


# ─────────────────────────────────────────────
# DB setup
# ─────────────────────────────────────────────

async def init_table(conn):
    """Create table with all fields. Safe to run repeatedly."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_markets (
            id                       SERIAL PRIMARY KEY,
            market_id                TEXT NOT NULL UNIQUE,
            question                 TEXT NOT NULL,
            raw_category             TEXT,       -- our detect_category result
            raw_category_gamma       TEXT,       -- Polymarket's own category label
            dataset                  TEXT NOT NULL,
            created_at               TIMESTAMP,
            end_date                 TIMESTAMP,
            end_date_str             TEXT,
            initial_bid              FLOAT,
            initial_ask              FLOAT,
            initial_price            FLOAT,
            initial_spread           FLOAT,      -- ask - bid at fetch time
            resolution_price         FLOAT,
            resolved_yes             INTEGER,    -- 1=YES, 0=NO
            num_outcomes             INTEGER,    -- 2=binary, >2=multi-outcome (filtered)
            total_volume_usd         FLOAT,
            volume_24h_usd           FLOAT,
            time_to_resolution_hours FLOAT,
            market_age_days          FLOAT,      -- endDate - startDate in days
            question_word_count      INTEGER,    -- proxy for question complexity
            fetched_at               TIMESTAMP NOT NULL
        )
    """)
    # Safe migrations for tables created by earlier versions
    migrations = [
        ("raw_category_gamma",   "TEXT"),
        ("initial_spread",       "FLOAT"),
        ("num_outcomes",         "INTEGER"),
        ("market_age_days",      "FLOAT"),
        ("question_word_count",  "INTEGER"),
        ("end_date_str",         "TEXT"),
        ("volume_24h_usd",       "FLOAT"),
        ("time_to_resolution_hours", "FLOAT"),
    ]
    for col, typedef in migrations:
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
    """Returns (yes_price, num_outcomes)."""
    try:
        prices = json.loads(val) if isinstance(val, str) else val
        if not prices or not isinstance(prices, list):
            return None, 0
        return float(prices[0]), len(prices)
    except Exception:
        return None, 0


def parse_dt(val):
    if not val:
        return None
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except Exception:
        return None


def parse_market(m, dataset):
    yes_p, num_outcomes = parse_outcome_prices(m.get("outcomePrices"))
    bid = parse_price(m.get("bestBid"))
    ask = parse_price(m.get("bestAsk"))

    # Initial price: bid/ask mid preferred, fallback to yes_p, then 0.5
    if bid is not None and ask is not None and 0 < bid < ask:
        initial_price = round((bid + ask) / 2, 4)
        initial_spread = round(ask - bid, 4)
    elif yes_p is not None:
        initial_price = yes_p
        initial_spread = None
    else:
        initial_price = 0.5
        initial_spread = None

    # Resolution outcome — only valid for binary markets
    resolved_yes = None
    if (m.get("resolved") or m.get("closed")) and yes_p is not None:
        if yes_p >= 0.90:
            resolved_yes = 1
        elif yes_p <= 0.10:
            resolved_yes = 0

    created_at   = parse_dt(m.get("startDate") or m.get("createdAt"))
    end_date     = parse_dt(m.get("endDate"))
    end_date_str = end_date.strftime("%Y-%m-%d") if end_date else None

    # Market duration
    time_to_res = None
    market_age_days = None
    if created_at and end_date and end_date > created_at:
        delta_hours = (end_date - created_at).total_seconds() / 3600
        time_to_res = round(delta_hours, 1)
        market_age_days = round(delta_hours / 24, 1)

    question = m.get("question", "")
    word_count = len(question.split()) if question else 0

    return {
        "market_id":               str(m.get("id", "")),
        "question":                question,
        "raw_category":            None,          # filled by detect_category below
        "raw_category_gamma":      m.get("category", ""),  # Polymarket's own label
        "dataset":                 dataset,
        "created_at":              created_at,
        "end_date":                end_date,
        "end_date_str":            end_date_str,
        "initial_bid":             bid,
        "initial_ask":             ask,
        "initial_price":           initial_price,
        "initial_spread":          initial_spread,
        "resolution_price":        yes_p,
        "resolved_yes":            resolved_yes,
        "num_outcomes":            num_outcomes,
        "total_volume_usd":        parse_price(m.get("volumeNum") or m.get("volume")),
        "volume_24h_usd":          parse_price(m.get("volume24hr")),
        "time_to_resolution_hours": time_to_res,
        "market_age_days":         market_age_days,
        "question_word_count":     word_count,
        "fetched_at":              datetime.utcnow(),
    }


def is_valid(p, end_min, end_max):
    """Quality filters."""
    if not p["market_id"] or not p["question"]:
        return False, "no_id"
    if p["resolved_yes"] is None:
        return False, "no_outcome"
    if p["initial_price"] is None:
        return False, "no_price"

    # Filter multi-outcome markets — binary only
    # Multi-outcome: "Will Powell say inflation 0-10, 11-20, 21-30 times?"
    # These corrupt binary win rate analysis
    if p["num_outcomes"] != 2:
        return False, "multi_outcome"

    # Date filter on end date
    d = p["end_date_str"]
    if d:
        if d > end_max:
            return False, "future"
        if d < end_min:
            return False, "too_old"

    return True, None


# ─────────────────────────────────────────────
# Category detection (imported from scoring.py)
# ─────────────────────────────────────────────

try:
    from scoring import detect_category
    log.info("Loaded detect_category from scoring.py")
except ImportError:
    log.warning("Could not import scoring.py — using fallback category detection")
    import re
    def detect_category(question):
        q = question.lower()
        if any(w in q for w in ["bitcoin","btc","ethereum","crypto","solana","xrp","doge"]):
            return "Crypto"
        if re.search(r'\b(eth|sol|bnb)\b', q):
            return "Crypto"
        if " vs " in q or " vs. " in q:
            return "Sports"
        if re.search(r'\b(nba|nfl|mlb|nhl|ufc)\b', q):
            return "Sports"
        if any(w in q for w in ["election","president","trump","biden","senate","congress",
                                  "tariff","ceasefire","nato","vote","democrat","republican"]):
            return "Politics"
        if any(w in q for w in ["fed","inflation","cpi","gdp","fomc","interest rate",
                                  "recession","unemployment","s&p","nasdaq","powell"]):
            return "Economics"
        if any(w in q for w in ["fda","vaccine","spacex","nasa","cancer","ai model",
                                  "openai","gpt","climate","drug trial","approval"]):
            return "Science"
        return "General"


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
            return []
    except Exception as e:
        log.warning("Fetch error at offset %d: %s", offset, e)
        return []


async def binary_search_start(session, headers, target_date):
    """Find the page where end dates first go <= target_date."""
    log.info("Binary search for %s...", target_date)
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
            for m in markets if parse_dt(m.get("endDate"))
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

    # Resume from oldest stored date
    resume_target = end_max
    if existing > 0:
        oldest_stored = await conn.fetchval(
            "SELECT MIN(end_date_str) FROM historical_markets WHERE dataset=$1", dataset
        )
        if oldest_stored and oldest_stored > end_min:
            resume_target = oldest_stored
            log.info("Resuming from: %s", resume_target)
        else:
            log.info("Dataset already complete")
            return

    start_page       = await binary_search_start(session, headers, resume_target)
    page             = start_page
    pages_fetched    = 0
    total_stored     = 0
    skip_reasons     = {}
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

            # Apply detect_category
            p["raw_category"] = detect_category(p["question"])

            valid, reason = is_valid(p, end_min, end_max)
            if not valid:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                continue

            try:
                await conn.execute("""
                    INSERT INTO historical_markets (
                        market_id, question, raw_category, raw_category_gamma,
                        dataset, created_at, end_date, end_date_str,
                        initial_bid, initial_ask, initial_price, initial_spread,
                        resolution_price, resolved_yes, num_outcomes,
                        total_volume_usd, volume_24h_usd,
                        time_to_resolution_hours, market_age_days,
                        question_word_count, fetched_at
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                        $13,$14,$15,$16,$17,$18,$19,$20,$21
                    )
                    ON CONFLICT (market_id) DO NOTHING
                """,
                    p["market_id"], p["question"],
                    p["raw_category"], p["raw_category_gamma"],
                    p["dataset"], p["created_at"], p["end_date"], p["end_date_str"],
                    p["initial_bid"], p["initial_ask"],
                    p["initial_price"], p["initial_spread"],
                    p["resolution_price"], p["resolved_yes"], p["num_outcomes"],
                    p["total_volume_usd"], p["volume_24h_usd"],
                    p["time_to_resolution_hours"], p["market_age_days"],
                    p["question_word_count"], p["fetched_at"]
                )
                page_stored += 1
                total_stored += 1
            except Exception as e:
                log.warning("Insert error: %s", e)

        min_d = min(page_dates) if page_dates else None
        max_d = max(page_dates) if page_dates else None
        log.info("Page %d | stored=%d | total=%d | dates=%s→%s | skips=%s",
                 page, page_stored, total_stored, min_d, max_d, skip_reasons)

        # Stop when past the window
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

    # Category breakdown
    cat_rows = await conn.fetch("""
        SELECT raw_category, COUNT(*) as n,
               SUM(resolved_yes) as wins
        FROM historical_markets
        WHERE dataset=$1
        GROUP BY raw_category
        ORDER BY n DESC
    """, dataset)

    yes_rate = round((yes_count or 0) / final * 100, 1) if final else 0
    log.info("─" * 55)
    log.info("Done: %s", label)
    log.info("Total: %d | YES rate: %.1f%%", final, yes_rate)
    log.info("Date range: %s → %s", oldest, newest)
    log.info("Skip reasons: %s", skip_reasons)
    log.info("Category breakdown:")
    for r in cat_rows:
        cat_wr = round((r["wins"] or 0) / r["n"] * 100, 1) if r["n"] else 0
        log.info("  %-12s n=%6d  WR=%.1f%%", r["raw_category"], r["n"], cat_wr)
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

    total_existing = await conn.fetchval("SELECT COUNT(*) FROM historical_markets")
    if total_existing == 0:
        log.info("Fresh start")
    else:
        log.info("Resuming — %d records in DB", total_existing)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        for cfg in DATASETS:
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