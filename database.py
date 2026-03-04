import asyncpg
import logging
from datetime import datetime

log = logging.getLogger(__name__)

def now():
    return datetime.utcnow()

async def init_db(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            yes_price FLOAT NOT NULL,
            score INTEGER NOT NULL,
            reason TEXT NOT NULL,
            volume FLOAT NOT NULL,
            alerted_at TIMESTAMP NOT NULL,
            outcome TEXT,
            profitable BOOLEAN,
            user_rating TEXT,
            fear_greed_score INTEGER,
            fear_greed_regime TEXT,
            market_age_hours FLOAT,
            score_components TEXT,
            confidence_tier TEXT,
            category TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities_log (
            id SERIAL PRIMARY KEY,
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            yes_price FLOAT NOT NULL,
            score INTEGER NOT NULL,
            reason TEXT NOT NULL,
            volume FLOAT NOT NULL,
            logged_at TIMESTAMP NOT NULL,
            outcome TEXT,
            profitable BOOLEAN,
            fear_greed_score INTEGER,
            fear_greed_regime TEXT,
            market_age_hours FLOAT,
            category TEXT
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id SERIAL PRIMARY KEY,
            market_id TEXT NOT NULL,
            yes_price FLOAT NOT NULL,
            recorded_at TIMESTAMP NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_history (
            id SERIAL PRIMARY KEY,
            score INTEGER NOT NULL,
            classification TEXT NOT NULL,
            regime TEXT NOT NULL,
            trend TEXT,
            recorded_at TIMESTAMP NOT NULL
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS risk_log (
            id SERIAL PRIMARY KEY,
            date TEXT NOT NULL,
            daily_loss FLOAT DEFAULT 0,
            trades_today INTEGER DEFAULT 0,
            win_streak INTEGER DEFAULT 0,
            loss_streak INTEGER DEFAULT 0,
            trade_size_multiplier FLOAT DEFAULT 1.0
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS event_calendar (
            id SERIAL PRIMARY KEY,
            event_name TEXT NOT NULL,
            event_date TIMESTAMP NOT NULL,
            category TEXT NOT NULL,
            relevance_keywords TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)

    # Safe column additions for existing tables
    for col, typedef in [
        ("user_rating", "TEXT"),
        ("fear_greed_score", "INTEGER"),
        ("fear_greed_regime", "TEXT"),
        ("market_age_hours", "FLOAT"),
        ("score_components", "TEXT"),
        ("confidence_tier", "TEXT"),
        ("category", "TEXT"),
    ]:
        await conn.execute(f"ALTER TABLE alerts ADD COLUMN IF NOT EXISTS {col} {typedef}")

    for col, typedef in [("category", "TEXT")]:
        await conn.execute(f"ALTER TABLE opportunities_log ADD COLUMN IF NOT EXISTS {col} {typedef}")

    await conn.execute("ALTER TABLE sentiment_history ADD COLUMN IF NOT EXISTS trend TEXT")

    log.info("Database tables ready")

async def get_risk_state(conn):
    today = now().strftime("%Y-%m-%d")
    row = await conn.fetchrow("SELECT * FROM risk_log WHERE date = $1", today)
    if not row:
        # Carry over streaks from yesterday
        yesterday = await conn.fetchrow("""
            SELECT win_streak, loss_streak, trade_size_multiplier
            FROM risk_log ORDER BY date DESC LIMIT 1
        """)
        win_streak = yesterday["win_streak"] if yesterday else 0
        loss_streak = yesterday["loss_streak"] if yesterday else 0
        multiplier = yesterday["trade_size_multiplier"] if yesterday else 1.0
        await conn.execute("""
            INSERT INTO risk_log (date, daily_loss, trades_today, win_streak, loss_streak, trade_size_multiplier)
            VALUES ($1, 0, 0, $2, $3, $4)
        """, today, win_streak, loss_streak, multiplier)
        row = await conn.fetchrow("SELECT * FROM risk_log WHERE date = $1", today)
    return dict(row)

async def reset_loss_streak(conn):
    today = now().strftime("%Y-%m-%d")
    await conn.execute("""
        UPDATE risk_log SET loss_streak = 0, win_streak = 0, trade_size_multiplier = 1.0
        WHERE date = $1
    """, today)
    log.info("Loss streak reset to 0")

async def update_risk_state(conn, profitable):
    today = now().strftime("%Y-%m-%d")
    state = await get_risk_state(conn)
    if profitable:
        new_win = state["win_streak"] + 1
        new_loss = 0
        multiplier = 1.25 if new_win >= 3 else 1.0
    else:
        new_win = 0
        new_loss = state["loss_streak"] + 1
        multiplier = 0.75 if new_loss >= 3 else 1.0
    await conn.execute("""
        UPDATE risk_log SET win_streak=$1, loss_streak=$2, trade_size_multiplier=$3
        WHERE date=$4
    """, new_win, new_loss, multiplier, today)

def get_dynamic_limits(state):
    multiplier = state.get("trade_size_multiplier", 1.0)
    return {
        "max_trade_size": round(5 * multiplier, 2),
        "max_daily_loss": 20,
        "max_open_positions": 10
    }

async def log_alert(conn, opportunity, fear_greed=None, market_age=None):
    fg_score = fear_greed.get("score") if fear_greed else None
    fg_regime = fear_greed.get("regime") if fear_greed else None
    alert_id = await conn.fetchval("""
        INSERT INTO alerts (market_id, question, yes_price, score, reason, volume, alerted_at,
                           fear_greed_score, fear_greed_regime, market_age_hours,
                           score_components, confidence_tier, category)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
        RETURNING id
    """,
        opportunity["id"], opportunity["question"], opportunity["yes_price"],
        opportunity["score"], opportunity["reason"], opportunity["volume"], now(),
        fg_score, fg_regime, market_age,
        opportunity["reason"], opportunity.get("confidence_tier", "Medium"),
        opportunity.get("category", "General")
    )
    count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    log.info("Alert logged (total: %d)", count)
    return alert_id

async def log_opportunity(conn, opportunity, fear_greed=None, market_age=None):
    existing = await conn.fetchrow(
        "SELECT id FROM opportunities_log WHERE market_id=$1", opportunity["id"]
    )
    if existing:
        return
    fg_score = fear_greed.get("score") if fear_greed else None
    fg_regime = fear_greed.get("regime") if fear_greed else None
    await conn.execute("""
        INSERT INTO opportunities_log (market_id, question, yes_price, score, reason,
                                      volume, logged_at, fear_greed_score, fear_greed_regime,
                                      market_age_hours, category)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
    """,
        opportunity["id"], opportunity["question"], opportunity["yes_price"],
        opportunity["score"], opportunity["reason"], opportunity["volume"], now(),
        fg_score, fg_regime, market_age, opportunity.get("category", "General")
    )

async def update_price_history(conn, market_id, yes_price):
    last = await conn.fetchrow("""
        SELECT recorded_at FROM price_history
        WHERE market_id=$1 ORDER BY recorded_at DESC LIMIT 1
    """, market_id)
    if last:
        hours_since = (now() - last["recorded_at"]).total_seconds() / 3600
        if hours_since < 2:
            return
    await conn.execute("""
        INSERT INTO price_history (market_id, yes_price, recorded_at) VALUES ($1,$2,$3)
    """, market_id, yes_price, now())

async def get_price_history(conn, market_id, limit=20):
    rows = await conn.fetch("""
        SELECT yes_price, recorded_at FROM price_history
        WHERE market_id=$1 ORDER BY recorded_at DESC LIMIT $2
    """, market_id, limit)
    return rows

async def log_sentiment(conn, fear_greed):
    await conn.execute("""
        INSERT INTO sentiment_history (score, classification, regime, trend, recorded_at)
        VALUES ($1,$2,$3,$4,$5)
    """, fear_greed["score"], fear_greed["classification"],
        fear_greed["regime"], fear_greed.get("trend", "UNKNOWN"), now())

async def get_daily_stats(conn):
    return await conn.fetch("""
        SELECT * FROM alerts WHERE alerted_at > NOW() - INTERVAL '24 hours'
    """)

async def get_alerted_markets(conn):
    rows = await conn.fetch("SELECT DISTINCT market_id FROM alerts")
    return set(r["market_id"] for r in rows)

async def get_logged_opportunities(conn):
    rows = await conn.fetch("SELECT DISTINCT market_id FROM opportunities_log")
    return set(r["market_id"] for r in rows)

async def check_resolutions(conn):
    import aiohttp, json
    unresolved_alerts = await conn.fetch("""
        SELECT * FROM alerts WHERE outcome IS NULL
        AND alerted_at < NOW() - INTERVAL '1 hour'
    """)
    unresolved_opps = await conn.fetch("""
        SELECT * FROM opportunities_log WHERE outcome IS NULL
        AND logged_at < NOW() - INTERVAL '1 hour'
    """)
    all_unresolved = list(unresolved_alerts) + list(unresolved_opps)
    if not all_unresolved:
        log.info("No unresolved items to check")
        return 0

    log.info("Checking resolution for %d items", len(all_unresolved))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    resolved_count = 0
    async with aiohttp.ClientSession() as session:
        for alert in all_unresolved:
            try:
                url = "https://gamma-api.polymarket.com/markets?id=" + str(alert["market_id"])
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        markets = await resp.json()
                        if markets:
                            market = markets[0]
                            if market.get("closed", False):
                                outcomes = market.get("outcomePrices", "[]")
                                if isinstance(outcomes, str):
                                    outcomes = json.loads(outcomes)
                                if outcomes:
                                    final_yes = float(outcomes[0])
                                    if final_yes >= 0.99:
                                        outcome = "YES"
                                        # FIXED: YES resolution profitable if bought YES cheap
                                        profitable = alert["yes_price"] < 0.5
                                    elif final_yes <= 0.01:
                                        outcome = "NO"
                                        # FIXED: NO resolution profitable if bought NO cheap (YES expensive)
                                        profitable = alert["yes_price"] > 0.5
                                    else:
                                        continue
                                    table = "alerts" if "alerted_at" in alert.keys() else "opportunities_log"
                                    await conn.execute(
                                        f"UPDATE {table} SET outcome=$1, profitable=$2 WHERE id=$3",
                                        outcome, profitable, alert["id"]
                                    )
                                    if table == "alerts":
                                        await update_risk_state(conn, profitable)
                                    resolved_count += 1
                                    log.info("Resolved: %s -> %s profitable=%s",
                                             alert["question"][:40], outcome, profitable)
                import asyncio
                await asyncio.sleep(0.3)
            except Exception as e:
                log.error("Resolution error: %s", e)
    return resolved_count