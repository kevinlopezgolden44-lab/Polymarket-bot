"""
CLOB Price History Fetcher
Enriches historical_markets table with TRUE opening prices
by fetching price history from the Polymarket CLOB API.

This solves the survivorship bias problem:
- Gamma API gives us FINAL prices (near 0 or 1 after resolution)
- CLOB prices-history gives us the FULL price series including opening price
- We extract the price at market creation time as the true entry price

Only processes non-Crypto markets (Sports, Politics, Economics, Science, General)
since Crypto opening prices from Gamma are reliable enough.

Start command: python fetch_clob_prices.py

Runtime estimate: ~2-3 hours for 5,000 non-Crypto markets
(1 CLOB call per market + 1 Gamma call to get clobTokenIds)
"""

import asyncio
import aiohttp
import asyncpg
import os
import json
import logging
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DATABASE_URL  = os.environ.get("DATABASE_URL")
GAMMA_BASE    = "https://gamma-api.polymarket.com/markets"
CLOB_BASE     = "https://clob.polymarket.com"
RATE_DELAY    = 0.15   # seconds between requests
BATCH_SIZE    = 50     # markets per batch


async def init_columns(conn):
    """Add opening_price column to historical_markets if not present."""
    await conn.execute("""
        ALTER TABLE historical_markets
        ADD COLUMN IF NOT EXISTS opening_price FLOAT
    """)
    await conn.execute("""
        ALTER TABLE historical_markets
        ADD COLUMN IF NOT EXISTS clob_token_id TEXT
    """)
    await conn.execute("""
        ALTER TABLE historical_markets
        ADD COLUMN IF NOT EXISTS price_fetched_at TIMESTAMP
    """)
    log.info("Columns ready")


async def get_clob_token_id(session, market_id, headers):
    """Fetch clobTokenIds for a market from Gamma API."""
    url = f"{GAMMA_BASE}/{market_id}"
    try:
        async with session.get(
            url, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                tokens = data.get("clobTokenIds") or data.get("tokens", [])
                if isinstance(tokens, str):
                    tokens = json.loads(tokens)
                if tokens and len(tokens) > 0:
                    return str(tokens[0])  # YES token ID
            return None
    except Exception as e:
        log.warning("Gamma fetch error for %s: %s", market_id, e)
        return None


async def get_opening_price(session, token_id, created_at, headers):
    """
    Fetch full price history for a token and extract the opening price.
    Opening price = first price recorded after market creation.
    """
    url = f"{CLOB_BASE}/prices-history"
    params = {"market": token_id, "interval": "max", "fidelity": 60}

    try:
        async with session.get(
            url, params=params, headers=headers,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            history = data.get("history", [])
            if not history:
                return None

            # Sort by timestamp ascending
            history.sort(key=lambda x: x.get("t", 0))

            # Find the first price at or after market creation
            if created_at:
                created_ts = created_at.replace(tzinfo=timezone.utc).timestamp()
                for point in history:
                    if point.get("t", 0) >= created_ts:
                        price = float(point.get("p", 0))
                        if 0.01 <= price <= 0.99:  # valid market price
                            return round(price, 4)

            # Fallback: use earliest price in history
            for point in history:
                price = float(point.get("p", 0))
                if 0.01 <= price <= 0.99:
                    return round(price, 4)

            return None

    except Exception as e:
        log.warning("CLOB price history error for %s: %s", token_id[:12], e)
        return None


async def process_batch(conn, session, markets, headers):
    """Process a batch of markets: get token ID then opening price."""
    processed = 0
    for market in markets:
        market_id  = market["market_id"]
        created_at = market["created_at"]

        # Step 1: Get clobTokenId from Gamma API
        token_id = await get_clob_token_id(session, market_id, headers)
        await asyncio.sleep(RATE_DELAY)

        if not token_id:
            await conn.execute(
                "UPDATE historical_markets SET price_fetched_at=$1 WHERE market_id=$2",
                datetime.utcnow(), market_id
            )
            continue

        # Step 2: Get opening price from CLOB
        opening_price = await get_opening_price(session, token_id, created_at, headers)
        await asyncio.sleep(RATE_DELAY)

        # Step 3: Update DB
        await conn.execute("""
            UPDATE historical_markets
            SET opening_price = $1,
                clob_token_id = $2,
                price_fetched_at = $3
            WHERE market_id = $4
        """, opening_price, token_id, datetime.utcnow(), market_id)

        if opening_price:
            processed += 1
            log.debug("  %s: opening=%.2f (token=%s)",
                      market_id, opening_price, token_id[:12])

    return processed


async def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set")
        return

    conn = await asyncpg.connect(DATABASE_URL)
    await init_columns(conn)

    # Count non-Crypto markets that need opening prices
    total = await conn.fetchval("""
        SELECT COUNT(*) FROM historical_markets
        WHERE raw_category != 'Crypto'
          AND resolved_yes IS NOT NULL
          AND price_fetched_at IS NULL
    """)
    log.info("Non-Crypto markets needing opening prices: %d", total)

    if total == 0:
        log.info("All non-Crypto markets already processed")
        # Show summary of what we have
        rows = await conn.fetch("""
            SELECT raw_category,
                   COUNT(*) as total,
                   SUM(CASE WHEN opening_price IS NOT NULL THEN 1 ELSE 0 END) as with_price,
                   SUM(resolved_yes) as wins
            FROM historical_markets
            WHERE raw_category != 'Crypto'
              AND resolved_yes IS NOT NULL
            GROUP BY raw_category
            ORDER BY total DESC
        """)
        log.info("Category summary:")
        for r in rows:
            with_p = r["with_price"] or 0
            total_r = r["total"]
            # Win rate using opening_price as entry filter
            if with_p > 0:
                wr = await conn.fetchval("""
                    SELECT COUNT(*) FROM historical_markets
                    WHERE raw_category=$1
                      AND opening_price IS NOT NULL
                      AND opening_price BETWEEN 0.05 AND 0.95
                      AND resolved_yes=1
                """, r["raw_category"])
                eligible = await conn.fetchval("""
                    SELECT COUNT(*) FROM historical_markets
                    WHERE raw_category=$1
                      AND opening_price IS NOT NULL
                      AND opening_price BETWEEN 0.05 AND 0.95
                """, r["raw_category"])
                real_wr = round(wr/eligible*100, 1) if eligible else 0
                log.info("  %-12s total=%d  with_price=%d  real_WR=%.1f%%",
                         r["raw_category"], total_r, with_p, real_wr)
        await conn.close()
        return

    # Process in batches
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json"
    }

    offset = 0
    total_processed = 0
    total_with_price = 0

    async with aiohttp.ClientSession() as session:
        while True:
            markets = await conn.fetch("""
                SELECT market_id, created_at, raw_category
                FROM historical_markets
                WHERE raw_category != 'Crypto'
                  AND resolved_yes IS NOT NULL
                  AND price_fetched_at IS NULL
                ORDER BY raw_category, market_id
                LIMIT $1 OFFSET $2
            """, BATCH_SIZE, offset)

            if not markets:
                break

            log.info("Batch %d-%d / %d | categories: %s",
                     offset+1, offset+len(markets), total,
                     ", ".join(set(m["raw_category"] for m in markets)))

            processed = await process_batch(conn, session, markets, headers)
            total_processed += len(markets)
            total_with_price += processed

            log.info("  Batch done: %d/%d got opening prices | total: %d/%d",
                     processed, len(markets), total_with_price, total_processed)

            # Reconnect check every 500 markets
            if total_processed % 500 == 0:
                try:
                    await conn.execute("SELECT 1")
                except Exception:
                    conn = await asyncpg.connect(DATABASE_URL)

            offset += BATCH_SIZE

            if total_processed >= total:
                break

    # Final summary with REAL win rates using opening prices
    log.info("=" * 55)
    log.info("FINAL RESULTS — True opening price win rates")
    log.info("=" * 55)

    for category in ["Sports", "Politics", "Economics", "Science", "General"]:
        rows = await conn.fetch("""
            SELECT
                opening_price,
                resolved_yes
            FROM historical_markets
            WHERE raw_category = $1
              AND opening_price IS NOT NULL
              AND opening_price BETWEEN 0.05 AND 0.95
              AND resolved_yes IS NOT NULL
            ORDER BY opening_price
        """, category)

        if not rows:
            log.info("%-12s no data yet", category)
            continue

        total_cat = len(rows)
        wins      = sum(r["resolved_yes"] for r in rows)
        wr        = round(wins/total_cat*100, 1) if total_cat else 0

        # Price buckets
        buckets = [
            (0.05, 0.20, "5-20c"),
            (0.20, 0.40, "20-40c"),
            (0.40, 0.60, "40-60c"),
            (0.60, 0.80, "60-80c"),
            (0.80, 0.96, "80c+"),
        ]
        log.info("%-12s n=%d  WR=%.1f%%", category, total_cat, wr)
        for lo, hi, lbl in buckets:
            sub = [r for r in rows if lo <= r["opening_price"] < hi]
            if not sub: continue
            sub_wr = sum(r["resolved_yes"] for r in sub) / len(sub) * 100
            flag = "✅" if sub_wr > 33.3 else "❌"
            log.info("  %s %-8s n=%5d  WR=%.1f%%", flag, lbl, len(sub), sub_wr)

    await conn.close()
    log.info("Done")


if __name__ == "__main__":
    asyncio.run(main())