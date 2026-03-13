import asyncio
import asyncpg
import logging
from datetime import datetime

log = logging.getLogger(__name__)

def now():
    return datetime.utcnow()


# ── CONNECTION POOL HELPER ─────────────────────────────────────────────────────
# All functions accept either a pool or a direct connection.
# If passed a pool, they acquire a connection for the duration of the call.
# This makes every function safe to call from both contexts.

async def _acquire(conn_or_pool):
    """Returns (conn, should_release) tuple."""
    if isinstance(conn_or_pool, asyncpg.pool.Pool):
        conn = await conn_or_pool.acquire()
        return conn, True
    return conn_or_pool, False

async def _release(pool, conn, should_release):
    if should_release:
        await pool.release(conn)


async def backfill_outcome_types(conn):
    """
    One-time migration for resolved alerts that predate the outcome_type
    and return tracking columns. Safe to run on every startup — it only
    touches rows where outcome_type IS NULL and outcome IS NOT NULL.
    """
    rows = await conn.fetch("""
        SELECT id, yes_price, outcome, profitable
        FROM alerts
        WHERE outcome IS NOT NULL
        AND outcome_type IS NULL
    """)

    if rows:
        log.info("Backfilling %d resolved alerts with outcome_type + return data...", len(rows))
        updated = 0

        for row in rows:
            try:
                entry = float(row["yes_price"])
                outcome = row["outcome"]
                profitable = row["profitable"]

                if outcome == "YES":
                    exit_price = 0.99
                elif outcome == "NO":
                    exit_price = 0.01
                else:
                    exit_price = entry

                if outcome == "YES":
                    outcome_type = "FULL_WIN" if entry < 0.5 else "LOSS"
                elif outcome == "NO":
                    outcome_type = "FULL_WIN" if entry > 0.5 else "LOSS"
                else:
                    outcome_type = "PARTIAL_WIN" if profitable else "LOSS"

                if entry and entry > 0:
                    exit_return_pct = round((exit_price - entry) / entry * 100, 2)
                else:
                    exit_return_pct = 0.0

                peak_price = exit_price if outcome_type in ("FULL_WIN", "PARTIAL_WIN") else entry
                peak_return_pct = round((peak_price - entry) / entry * 100, 2) if entry else 0.0

                await conn.execute("""
                    UPDATE alerts
                    SET outcome_type    = $1,
                        entry_price     = $2,
                        exit_price      = $3,
                        exit_return_pct = $4,
                        peak_price      = $5,
                        peak_return_pct = $6,
                        exit_reason     = 'RESOLVED',
                        is_backfilled   = TRUE
                    WHERE id = $7
                """, outcome_type, entry, exit_price,
                    exit_return_pct, peak_price, peak_return_pct,
                    row["id"])
                updated += 1

            except Exception as e:
                log.warning("Backfill error on alert id=%s: %s", row["id"], e)

        log.info("Backfill complete: %d alerts updated", updated)
    else:
        log.info("Backfill: nothing to migrate")

    UPGRADE_DATE = "2026-03-06"
    await conn.execute(f"""
        UPDATE alerts
        SET is_backfilled = TRUE
        WHERE (is_backfilled IS NULL OR is_backfilled = FALSE)
        AND alerted_at < '{UPGRADE_DATE}'
    """)
    log.info("Backfill flag pass complete")


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
            category TEXT,
            outcome_type TEXT,
            entry_price FLOAT,
            peak_price FLOAT,
            exit_price FLOAT,
            peak_return_pct FLOAT,
            exit_return_pct FLOAT,
            exit_reason TEXT,
            signals_fired TEXT,
            days_to_resolution FLOAT,
            price_7d_ago FLOAT,
            price_3d_ago FLOAT,
            price_1d_ago FLOAT,
            score_breakdown TEXT,
            bid_price FLOAT,
            ask_price FLOAT,
            alerts_in_last_24h INTEGER,
            active_open_positions_count INTEGER,
            peak_reached_at TIMESTAMP,
            first_profitable_at TIMESTAMP,
            last_profitable_at TIMESTAMP,
            market_type TEXT,
            price_pct_of_range FLOAT,
            hold_duration_hours FLOAT,
            hour_of_day_utc INTEGER,
            loss_pattern TEXT,
            revisit_count INTEGER DEFAULT 0,
            resolution_price FLOAT,
            loss_reason TEXT,
            volume_at_resolution FLOAT,
            actual_hold_days FLOAT,
            direction TEXT,
            edge_pct FLOAT,
            vegas_gap FLOAT,
            vegas_implied FLOAT,
            spread FLOAT
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_positions (
            id SERIAL PRIMARY KEY,
            alert_id INTEGER NOT NULL REFERENCES alerts(id),
            market_id TEXT NOT NULL,
            entry_price FLOAT NOT NULL,
            peak_price FLOAT NOT NULL,
            current_price FLOAT NOT NULL,
            opened_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP,
            exit_reason TEXT,
            exit_price FLOAT,
            return_pct FLOAT,
            outcome_type TEXT,
            is_open BOOLEAN DEFAULT TRUE
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_trades (
            id SERIAL PRIMARY KEY,
            sim_date TEXT NOT NULL,
            alert_id INTEGER REFERENCES alerts(id),
            market_id TEXT NOT NULL,
            question TEXT NOT NULL,
            category TEXT,
            confidence_tier TEXT,
            entry_price FLOAT NOT NULL,
            stake FLOAT NOT NULL,
            exit_price FLOAT,
            return_pct FLOAT,
            pnl FLOAT,
            outcome_type TEXT,
            exit_reason TEXT,
            opened_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP,
            is_open BOOLEAN DEFAULT TRUE
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS sim_daily_log (
            id SERIAL PRIMARY KEY,
            sim_date TEXT NOT NULL UNIQUE,
            starting_bankroll FLOAT NOT NULL DEFAULT 200.0,
            ending_bankroll FLOAT,
            total_staked FLOAT DEFAULT 0,
            total_pnl FLOAT DEFAULT 0,
            trades_placed INTEGER DEFAULT 0,
            trades_won INTEGER DEFAULT 0,
            trades_lost INTEGER DEFAULT 0,
            busted BOOLEAN DEFAULT FALSE,
            busted_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_price_snapshots (
            id SERIAL PRIMARY KEY,
            alert_id INTEGER REFERENCES alerts(id),
            market_id TEXT NOT NULL,
            yes_price FLOAT NOT NULL,
            bid_price FLOAT,
            ask_price FLOAT,
            recorded_at TIMESTAMP NOT NULL,
            minutes_since_alert INTEGER
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
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
    """)

    # Safe column additions
    for col, typedef in [
        ("user_rating", "TEXT"),
        ("fear_greed_score", "INTEGER"),
        ("fear_greed_regime", "TEXT"),
        ("market_age_hours", "FLOAT"),
        ("score_components", "TEXT"),
        ("confidence_tier", "TEXT"),
        ("category", "TEXT"),
        ("outcome_type", "TEXT"),
        ("entry_price", "FLOAT"),
        ("peak_price", "FLOAT"),
        ("exit_price", "FLOAT"),
        ("peak_return_pct", "FLOAT"),
        ("exit_return_pct", "FLOAT"),
        ("exit_reason", "TEXT"),
        ("signals_fired", "TEXT"),
        ("is_backfilled", "BOOLEAN"),
        ("days_to_resolution", "FLOAT"),
        ("price_7d_ago", "FLOAT"),
        ("price_3d_ago", "FLOAT"),
        ("price_1d_ago", "FLOAT"),
        ("score_breakdown", "TEXT"),
        ("bid_price", "FLOAT"),
        ("ask_price", "FLOAT"),
        ("alerts_in_last_24h", "INTEGER"),
        ("active_open_positions_count", "INTEGER"),
        ("peak_reached_at", "TIMESTAMP"),
        ("first_profitable_at", "TIMESTAMP"),
        ("last_profitable_at", "TIMESTAMP"),
        ("market_type", "TEXT"),
        ("price_pct_of_range", "FLOAT"),
        ("hold_duration_hours", "FLOAT"),
        ("hour_of_day_utc", "INTEGER"),
        ("loss_pattern", "TEXT"),
        ("revisit_count", "INTEGER"),
        ("resolution_price", "FLOAT"),
        ("loss_reason", "TEXT"),
        ("volume_at_resolution", "FLOAT"),
        ("actual_hold_days", "FLOAT"),
        ("direction", "TEXT"),
        ("edge_pct", "FLOAT"),
        ("vegas_gap", "FLOAT"),
        ("vegas_implied", "FLOAT"),
        ("spread", "FLOAT"),
        ("suppressed", "BOOLEAN"),
        ("suppression_reason", "TEXT"),
        ("ob_imbalance", "FLOAT"),
        ("ob_signal", "TEXT"),
        ("volume_delta", "FLOAT"),
        ("volume_delta_signal", "TEXT"),
        ("market_direction", "TEXT"),
    ]:
        await conn.execute(f"ALTER TABLE alerts ADD COLUMN IF NOT EXISTS {col} {typedef}")

    for col, typedef in [("category", "TEXT")]:
        await conn.execute(f"ALTER TABLE opportunities_log ADD COLUMN IF NOT EXISTS {col} {typedef}")

    await conn.execute("ALTER TABLE sentiment_history ADD COLUMN IF NOT EXISTS trend TEXT")

    log.info("Database tables ready")
    await backfill_outcome_types(conn)


async def get_risk_state(conn):
    today = now().strftime("%Y-%m-%d")
    row = await conn.fetchrow("SELECT * FROM risk_log WHERE date = $1", today)
    if not row:
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
    entry_price = opportunity["yes_price"]
    signals_fired = opportunity.get("signals_fired", "")

    alerts_24h = await conn.fetchval("""
        SELECT COUNT(*) FROM alerts
        WHERE alerted_at > NOW() - INTERVAL '24 hours'
    """)
    open_positions = await conn.fetchval("""
        SELECT COUNT(*) FROM trade_positions WHERE is_open = TRUE
    """)

    import json as _json
    score_breakdown_str = None
    if opportunity.get("score_breakdown"):
        try:
            score_breakdown_str = _json.dumps(opportunity["score_breakdown"])
        except Exception:
            pass

    alert_id = await conn.fetchval("""
        INSERT INTO alerts (market_id, question, yes_price, score, reason, volume, alerted_at,
                           fear_greed_score, fear_greed_regime, market_age_hours,
                           score_components, confidence_tier, category,
                           entry_price, peak_price, signals_fired,
                           days_to_resolution, price_7d_ago, price_3d_ago, price_1d_ago,
                           score_breakdown, bid_price, ask_price,
                           alerts_in_last_24h, active_open_positions_count,
                           market_type, price_pct_of_range, hour_of_day_utc,
                           direction, edge_pct, vegas_gap, vegas_implied, spread,
                           suppressed, suppression_reason,
                           ob_imbalance, ob_signal, volume_delta, volume_delta_signal,
                           market_direction)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                $17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,
                $29,$30,$31,$32,$33,$34,$35,$36,$37,$38,$39,$40)
        RETURNING id
    """,
        opportunity["id"], opportunity["question"], opportunity["yes_price"],
        opportunity["score"], opportunity["reason"], opportunity["volume"], now(),
        fg_score, fg_regime, market_age,
        opportunity["reason"], opportunity.get("confidence_tier", "Medium"),
        opportunity.get("category", "General"),
        entry_price, entry_price,
        signals_fired,
        opportunity.get("days_to_resolution"),
        opportunity.get("price_7d_ago"),
        opportunity.get("price_3d_ago"),
        opportunity.get("price_1d_ago"),
        score_breakdown_str,
        opportunity.get("bid_price"),
        opportunity.get("ask_price"),
        alerts_24h,
        open_positions,
        opportunity.get("market_type"),
        opportunity.get("price_pct_of_range"),
        now().hour,
        opportunity.get("direction"),
        opportunity.get("edge_pct"),
        opportunity.get("vegas_gap"),
        opportunity.get("vegas_implied"),
        opportunity.get("spread"),
        opportunity.get("suppressed", False),
        opportunity.get("suppression_reason"),
        opportunity.get("ob_imbalance"),
        opportunity.get("ob_signal"),
        opportunity.get("volume_delta"),
        opportunity.get("volume_delta_signal"),
        opportunity.get("signals", {}).get("market_direction"),
    )

    await conn.execute("""
        INSERT INTO trade_positions (alert_id, market_id, entry_price, peak_price,
                                     current_price, opened_at)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, alert_id, opportunity["id"], entry_price, entry_price, entry_price, now())

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


async def update_open_positions(pool):
    """
    Called every scan. Uses pool so each market fetch gets its own
    connection — no blocking while waiting on HTTP responses.
    """
    import aiohttp
    import json as _json

    async with pool.acquire() as conn:
        open_positions = await conn.fetch("""
            SELECT tp.*, a.question, a.entry_price as alert_entry,
                   a.direction as alert_direction
            FROM trade_positions tp
            JOIN alerts a ON tp.alert_id = a.id
            WHERE tp.is_open = TRUE
            AND tp.opened_at < NOW() - INTERVAL '10 minutes'
        """)

    if not open_positions:
        return []

    TAKE_PROFIT_PCT    = 40.0
    STOP_LOSS_PCT      = -20.0   # Tightened from -25% — data shows avg exit was -33.8%
                                  # so -25% wasn't executing. -20% target = ~-25% in practice.
    TRAILING_STOP_ACTIVATE = 10.0  # Start trailing once trade is up +10%
    TRAILING_STOP_PCT      = 15.0  # Trail at -15% from peak
                                    # e.g. peak=+20% → stop at +5%, not -20%

    closed = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        for pos in open_positions:
            try:
                url = "https://gamma-api.polymarket.com/markets?id=" + str(pos["market_id"])
                async with session.get(url, headers=headers,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        continue
                    markets = await resp.json()
                    if not markets:
                        continue
                    market = markets[0]

                    outcomes = market.get("outcomePrices", "[]")
                    if isinstance(outcomes, str):
                        outcomes = _json.loads(outcomes)
                    if not outcomes:
                        continue
                    current_price = float(outcomes[0])

                    entry = pos["entry_price"]
                    if entry == 0:
                        continue

                    return_pct = round((current_price - entry) / entry * 100, 2)
                    new_peak = max(pos["peak_price"], current_price)

                    # Each DB operation gets its own connection from the pool
                    async with pool.acquire() as conn:
                        await conn.execute("""
                            UPDATE trade_positions
                            SET current_price=$1, peak_price=$2
                            WHERE id=$3
                        """, current_price, new_peak, pos["id"])

                        peak_return = round((new_peak - entry) / entry * 100, 2)
                        await conn.execute("""
                            UPDATE alerts SET peak_price=$1, peak_return_pct=$2
                            WHERE id=$3
                        """, new_peak, peak_return, pos["alert_id"])

                        await update_position_timing(conn, pos["alert_id"], current_price, entry)

                    market_closed = market.get("closed", False)
                    exit_reason = None
                    outcome_type = None

                    if market_closed:
                        exit_reason = "RESOLVED"
                        # Use stored direction (BUY_YES/BUY_NO) — fallback to
                        # entry price heuristic for alerts logged before direction
                        # was added (entry < 0.5 implies a YES purchase).
                        direction = pos.get("alert_direction") or (
                            "BUY_YES" if entry < 0.5 else "BUY_NO"
                        )
                        if current_price >= 0.99:
                            outcome_type = "FULL_WIN" if direction == "BUY_YES" else "LOSS"
                        elif current_price <= 0.01:
                            outcome_type = "FULL_WIN" if direction == "BUY_NO" else "LOSS"
                        else:
                            outcome_type = "PARTIAL_WIN" if return_pct > 5 else (
                                "BREAKEVEN" if return_pct > -5 else "LOSS"
                            )
                    elif return_pct >= TAKE_PROFIT_PCT:
                        exit_reason = "TAKE_PROFIT"
                        outcome_type = "PARTIAL_WIN"
                    elif return_pct <= STOP_LOSS_PCT:
                        exit_reason = "STOP_LOSS"
                        outcome_type = "LOSS"

                    # ── Trailing stop check ───────────────────────────────
                    # Once a trade reaches +10%, trail at -15% from peak.
                    # e.g. peak=+20% means stop is at +5%. Protects gains on
                    # the 14 reversal losses that peaked at avg +14.75%.
                    if exit_reason is None:
                        peak_return = round((new_peak - entry) / entry * 100, 2)
                        if peak_return >= TRAILING_STOP_ACTIVATE:
                            trailing_stop_level = peak_return - TRAILING_STOP_PCT
                            if return_pct <= trailing_stop_level:
                                exit_reason = "TRAILING_STOP"
                                outcome_type = "PARTIAL_WIN" if return_pct > 0 else "LOSS"
                                log.info(
                                    "Trailing stop triggered: peak=%.1f%% current=%.1f%% stop_level=%.1f%%",
                                    peak_return, return_pct, trailing_stop_level
                                )

                    if exit_reason:
                        profitable = outcome_type in ("FULL_WIN", "PARTIAL_WIN")

                        hold_secs = (now() - pos["opened_at"]).total_seconds()
                        hold_hours = round(hold_secs / 3600, 1)
                        actual_hold_days = round(hold_secs / 86400, 2)

                        loss_pattern = None
                        if not profitable:
                            if exit_reason in ("STOP_LOSS", "TRAILING_STOP") and pos["peak_price"] > entry:
                                loss_pattern = "REVERSAL"
                            elif exit_reason == "STOP_LOSS":
                                loss_pattern = "SUDDEN_DROP"
                            elif exit_reason == "TRAILING_STOP":
                                loss_pattern = "REVERSAL"
                            elif exit_reason == "RESOLVED":
                                loss_pattern = "NEVER_MOVED"

                        loss_reason = None
                        if not profitable:
                            loss_reason = derive_loss_reason(
                                entry, current_price,
                                float(pos.get("alert_entry") or entry),
                                peak_price=pos["peak_price"]
                            )

                        async with pool.acquire() as conn:
                            await conn.execute("""
                                UPDATE trade_positions
                                SET is_open=FALSE, closed_at=$1, exit_reason=$2,
                                    exit_price=$3, return_pct=$4, outcome_type=$5
                                WHERE id=$6
                            """, now(), exit_reason, current_price, return_pct, outcome_type, pos["id"])

                            await conn.execute("""
                                UPDATE alerts
                                SET outcome=$1, profitable=$2, outcome_type=$3,
                                    exit_price=$4, exit_return_pct=$5, exit_reason=$6,
                                    hold_duration_hours=$7, loss_pattern=$8,
                                    resolution_price=$9, loss_reason=$10,
                                    actual_hold_days=$11
                                WHERE id=$12
                            """,
                                exit_reason, profitable, outcome_type,
                                current_price, return_pct, exit_reason,
                                hold_hours, loss_pattern,
                                current_price, loss_reason,
                                actual_hold_days, pos["alert_id"]
                            )

                            await update_risk_state(conn, profitable)

                        closed.append({
                            "question": pos["question"],
                            "entry_price": entry,
                            "exit_price": current_price,
                            "return_pct": return_pct,
                            "peak_return_pct": peak_return,
                            "exit_reason": exit_reason,
                            "outcome_type": outcome_type,
                            "loss_reason": loss_reason,
                            "profitable": profitable,
                        })

                        log.info(
                            "Position closed [%s] %s | entry=%.2f exit=%.2f return=%.1f%%",
                            outcome_type, pos["question"][:40],
                            entry, current_price, return_pct
                        )

                await asyncio.sleep(0.3)
            except Exception as e:
                log.error("update_open_positions error: %s", e)

    return closed


def derive_loss_reason(entry, resolution_price, alert_yes_price, peak_price=None):
    if resolution_price is None or entry is None or entry == 0:
        return None

    price_change_pct = abs(resolution_price - entry) / entry * 100
    bet_yes = entry < 0.5
    resolved_yes = resolution_price >= 0.99
    resolved_no = resolution_price <= 0.01

    if bet_yes and resolved_no:
        return "WRONG_DIRECTION"
    if not bet_yes and resolved_yes:
        return "WRONG_DIRECTION"

    if price_change_pct < 10:
        return "NO_MOVEMENT"

    if peak_price and peak_price > entry:
        return "REVERSAL"

    return "WRONG_DIRECTION"


async def check_resolutions(conn):
    """
    Legacy full-resolution check (runs on schedule).
    Catches markets that closed without being caught by update_open_positions.
    """
    import aiohttp
    import json as _json

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
                                    outcomes = _json.loads(outcomes)
                                if outcomes:
                                    final_yes = float(outcomes[0])
                                    entry = float(alert.get("entry_price") or alert["yes_price"])
                                    return_pct = round((final_yes - entry) / entry * 100, 2) if entry else 0

                                    direction = alert.get("direction") or (
                                        "BUY_YES" if entry < 0.5 else "BUY_NO"
                                    )
                                    if final_yes >= 0.99:
                                        outcome = "YES"
                                        outcome_type = "FULL_WIN" if direction == "BUY_YES" else "LOSS"
                                    elif final_yes <= 0.01:
                                        outcome = "NO"
                                        outcome_type = "FULL_WIN" if direction == "BUY_NO" else "LOSS"
                                    else:
                                        outcome = "PARTIAL"
                                        outcome_type = (
                                            "PARTIAL_WIN" if return_pct > 5
                                            else "BREAKEVEN" if return_pct > -5
                                            else "LOSS"
                                        )

                                    profitable = outcome_type in ("FULL_WIN", "PARTIAL_WIN")
                                    table = "alerts" if "alerted_at" in alert.keys() else "opportunities_log"

                                    if table == "alerts":
                                        loss_pattern = None
                                        if not profitable:
                                            history = await conn.fetch("""
                                                SELECT yes_price FROM price_history
                                                WHERE market_id=$1
                                                ORDER BY recorded_at ASC
                                            """, alert["market_id"])
                                            if history and len(history) >= 2:
                                                prices = [float(r["yes_price"]) for r in history]
                                                entry_p = float(alert.get("entry_price") or alert["yes_price"])
                                                peak_p = max(prices)
                                                ever_profitable = peak_p > entry_p
                                                price_range = max(prices) - min(prices)
                                                if ever_profitable and return_pct < 0:
                                                    loss_pattern = "REVERSAL"
                                                elif price_range < entry_p * 0.05:
                                                    loss_pattern = "NEVER_MOVED"
                                                elif prices[-1] < prices[0] and all(
                                                    prices[i] >= prices[i+1] for i in range(len(prices)-1)
                                                ):
                                                    loss_pattern = "SLOW_BLEED"
                                                else:
                                                    loss_pattern = "SUDDEN_DROP"

                                        hold_hours = None
                                        actual_hold_days = None
                                        alerted_at = alert.get("alerted_at")
                                        if alerted_at:
                                            hold_secs = (now() - alerted_at).total_seconds()
                                            hold_hours = round(hold_secs / 3600, 1)
                                            actual_hold_days = round(hold_secs / 86400, 2)

                                        peak_p = float(alert.get("peak_price") or 0) or None
                                        loss_reason = None
                                        if not profitable:
                                            loss_reason = derive_loss_reason(
                                                entry, final_yes,
                                                float(alert["yes_price"]),
                                                peak_price=peak_p
                                            )

                                        vol_at_res = market.get("volume")
                                        try:
                                            vol_at_res = float(vol_at_res) if vol_at_res else None
                                        except (TypeError, ValueError):
                                            vol_at_res = None

                                        await conn.execute("""
                                            UPDATE alerts
                                            SET outcome=$1, profitable=$2, outcome_type=$3,
                                                exit_price=$4, exit_return_pct=$5, exit_reason='RESOLVED',
                                                loss_pattern=$6, hold_duration_hours=$7,
                                                resolution_price=$8, loss_reason=$9,
                                                volume_at_resolution=$10, actual_hold_days=$11
                                            WHERE id=$12
                                        """, outcome, profitable, outcome_type,
                                            final_yes, return_pct, loss_pattern, hold_hours,
                                            final_yes, loss_reason, vol_at_res, actual_hold_days,
                                            alert["id"])
                                        await update_risk_state(conn, profitable)
                                    else:
                                        await conn.execute(
                                            "UPDATE opportunities_log SET outcome=$1, profitable=$2 WHERE id=$3",
                                            outcome, profitable, alert["id"]
                                        )

                                    resolved_count += 1
                                    log.info("Resolved: %s -> %s [%s] return=%.1f%%",
                                             alert["question"][:40], outcome, outcome_type, return_pct)
                await asyncio.sleep(0.3)
            except Exception as e:
                log.error("Resolution error: %s", e)
    return resolved_count


async def run_weekly_backtest(conn):
    rows = await conn.fetch("""
        SELECT category, confidence_tier, fear_greed_regime, signals_fired,
               outcome_type, exit_return_pct, peak_return_pct, profitable,
               is_backfilled, market_type, loss_pattern, hold_duration_hours,
               hour_of_day_utc, loss_reason, actual_hold_days
        FROM alerts
        WHERE outcome IS NOT NULL
        AND alerted_at > NOW() - INTERVAL '90 days'
    """)

    if not rows:
        return "Weekly Backtest: Not enough data yet. Keep building history!"

    total = len(rows)
    wins = sum(1 for r in rows if r["profitable"])
    win_rate = round(wins / total * 100, 1) if total else 0

    live_rows = [r for r in rows if not r.get("is_backfilled")]
    returns = [r["exit_return_pct"] for r in live_rows if r["exit_return_pct"] is not None]
    avg_return = round(sum(returns) / len(returns), 1) if returns else None
    peak_returns = [r["peak_return_pct"] for r in live_rows if r["peak_return_pct"] is not None]
    avg_peak = round(sum(peak_returns) / len(peak_returns), 1) if peak_returns else None

    avg_return_str = (("+" if avg_return >= 0 else "") + str(avg_return) + "%") if avg_return is not None else "tracking soon"
    avg_peak_str = ("+" + str(avg_peak) + "%") if avg_peak is not None else "tracking soon"

    by_category = {}
    for r in rows:
        cat = r["category"] or "Unknown"
        by_category.setdefault(cat, {"wins": 0, "total": 0})
        by_category[cat]["total"] += 1
        if r["profitable"]:
            by_category[cat]["wins"] += 1

    cat_lines = []
    for cat, d in sorted(by_category.items(), key=lambda x: -x[1]["total"]):
        wr = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0
        cat_lines.append(f"  {cat}: {wr}% ({d['total']} trades)")

    by_tier = {}
    for r in rows:
        tier = r["confidence_tier"] or "Unknown"
        by_tier.setdefault(tier, {"wins": 0, "total": 0})
        by_tier[tier]["total"] += 1
        if r["profitable"]:
            by_tier[tier]["wins"] += 1

    tier_lines = []
    for tier in ["HIGH", "MEDIUM", "LOW"]:
        if tier in by_tier:
            d = by_tier[tier]
            wr = round(d["wins"] / d["total"] * 100, 1)
            tier_lines.append(f"  {tier}: {wr}% ({d['total']} trades)")

    signal_stats = {}
    for r in rows:
        sigs = r["signals_fired"] or ""
        for sig in sigs.split(","):
            sig = sig.strip()
            if not sig:
                continue
            signal_stats.setdefault(sig, {"wins": 0, "total": 0})
            signal_stats[sig]["total"] += 1
            if r["profitable"]:
                signal_stats[sig]["wins"] += 1

    sig_lines = []
    for sig, d in sorted(signal_stats.items(), key=lambda x: -x[1]["total"])[:6]:
        wr = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0
        sig_lines.append(f"  {sig}: {wr}% ({d['total']} trades)")

    outcome_counts = {}
    for r in rows:
        ot = r["outcome_type"] or "UNKNOWN"
        outcome_counts[ot] = outcome_counts.get(ot, 0) + 1

    outcome_lines = [f"  {k}: {v}" for k, v in sorted(outcome_counts.items(), key=lambda x: -x[1])]

    by_market_type = {}
    for r in rows:
        mt = r.get("market_type") or "Unknown"
        by_market_type.setdefault(mt, {"wins": 0, "total": 0})
        by_market_type[mt]["total"] += 1
        if r["profitable"]:
            by_market_type[mt]["wins"] += 1

    mt_lines = []
    for mt, d in sorted(by_market_type.items(), key=lambda x: -x[1]["total"]):
        wr = round(d["wins"] / d["total"] * 100, 1) if d["total"] else 0
        mt_lines.append(f"  {mt}: {wr}% ({d['total']} trades)")

    loss_patterns = {}
    for r in rows:
        if not r["profitable"] and r.get("loss_pattern"):
            lp = r["loss_pattern"]
            loss_patterns[lp] = loss_patterns.get(lp, 0) + 1
    lp_lines = [f"  {k}: {v}" for k, v in sorted(loss_patterns.items(), key=lambda x: -x[1])]
    lp_section = lp_lines if lp_lines else ["  No loss pattern data yet"]

    loss_reasons = {}
    for r in rows:
        if not r["profitable"] and r.get("loss_reason"):
            lr = r["loss_reason"]
            loss_reasons[lr] = loss_reasons.get(lr, 0) + 1
    total_losses = sum(loss_reasons.values())
    lr_lines = []
    for k, v in sorted(loss_reasons.items(), key=lambda x: -x[1]):
        pct = round(v / total_losses * 100) if total_losses else 0
        lr_lines.append(f"  {k}: {v} ({pct}%)")
    lr_section = lr_lines if lr_lines else ["  No loss reason data yet"]

    hold_times = [r.get("hold_duration_hours") for r in rows if r.get("hold_duration_hours")]
    avg_hold = round(sum(hold_times) / len(hold_times), 1) if hold_times else None
    hold_str = f"{avg_hold}h" if avg_hold else "tracking soon"

    by_hour = {}
    for r in rows:
        h = r.get("hour_of_day_utc")
        if h is not None:
            bucket = f"{h:02d}:00"
            by_hour.setdefault(bucket, {"wins": 0, "total": 0})
            by_hour[bucket]["total"] += 1
            if r["profitable"]:
                by_hour[bucket]["wins"] += 1
    hour_lines = []
    for h, d in sorted(by_hour.items()):
        if d["total"] >= 3:
            wr = round(d["wins"] / d["total"] * 100, 1)
            hour_lines.append(f"  {h} UTC: {wr}% ({d['total']} trades)")
    hour_section = hour_lines if hour_lines else ["  Need more data (min 3 trades/hour)"]

    sig_section = sig_lines if sig_lines else ["  No signal data yet"]

    lines = [
        "📊 <b>Weekly Backtest Report</b>",
        "",
        f"<b>Overall ({total} closed trades)</b>",
        f"Win Rate: {win_rate}%",
        f"Avg Exit Return: {avg_return_str}",
        f"Avg Peak Return: {avg_peak_str}",
        f"Avg Hold Duration: {hold_str}",
        "",
        "<b>By Category:</b>",
    ] + cat_lines + [
        "",
        "<b>By Market Type:</b>",
    ] + mt_lines + [
        "",
        "<b>By Confidence Tier:</b>",
    ] + tier_lines + [
        "",
        "<b>By Signal:</b>",
    ] + sig_section + [
        "",
        "<b>Loss Reasons (Why We Lost):</b>",
    ] + lr_section + [
        "",
        "<b>Loss Patterns (How Price Moved):</b>",
    ] + lp_section + [
        "",
        "<b>By Hour (UTC):</b>",
    ] + hour_section + [
        "",
        "<b>Outcome Breakdown:</b>",
    ] + outcome_lines

    return "\n".join(lines)


async def record_alert_snapshot(conn, alert_id, market_id, yes_price,
                                  alerted_at, bid_price=None, ask_price=None):
    last = await conn.fetchrow("""
        SELECT recorded_at FROM alert_price_snapshots
        WHERE alert_id = $1 ORDER BY recorded_at DESC LIMIT 1
    """, alert_id)

    if last:
        mins_since = (now() - last["recorded_at"]).total_seconds() / 60
        if mins_since < 14:
            return

    hours_since_alert = (now() - alerted_at).total_seconds() / 3600
    if hours_since_alert > 48:
        return

    minutes_since = int((now() - alerted_at).total_seconds() / 60)

    await conn.execute("""
        INSERT INTO alert_price_snapshots
            (alert_id, market_id, yes_price, bid_price, ask_price,
             recorded_at, minutes_since_alert)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
    """, alert_id, market_id, yes_price, bid_price, ask_price, now(), minutes_since)


async def update_position_timing(conn, alert_id, current_price, entry_price):
    alert = await conn.fetchrow("""
        SELECT peak_price, peak_reached_at, first_profitable_at, entry_price
        FROM alerts WHERE id = $1
    """, alert_id)

    if not alert:
        return

    updates = {}
    current_peak = alert["peak_price"] or entry_price

    if current_price > current_peak:
        updates["peak_reached_at"] = now()

    if current_price > entry_price:
        if not alert["first_profitable_at"]:
            updates["first_profitable_at"] = now()
        updates["last_profitable_at"] = now()

    if updates:
        set_clauses = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
        values = list(updates.values())
        await conn.execute(
            f"UPDATE alerts SET {set_clauses} WHERE id = $1",
            alert_id, *values
        )


async def get_alert_price_curve(conn, alert_id):
    rows = await conn.fetch("""
        SELECT yes_price, bid_price, ask_price, recorded_at, minutes_since_alert
        FROM alert_price_snapshots
        WHERE alert_id = $1
        ORDER BY recorded_at ASC
    """, alert_id)
    return [dict(r) for r in rows]


async def cleanup_old_snapshots(conn):
    await conn.execute("""
        DELETE FROM alert_price_snapshots
        WHERE recorded_at < NOW() - INTERVAL '30 days'
    """)
    log.info("Cleaned up old price snapshots")

# ── BOT STATE (persistent key-value, survives redeploys) ───────────────────────
async def get_state(conn, key: str) -> str | None:
    """Read a value from bot_state. Returns None if key doesn't exist."""
    row = await conn.fetchrow("SELECT value FROM bot_state WHERE key = $1", key)
    return row["value"] if row else None


async def set_state(conn, key: str, value: str) -> None:
    """Upsert a value into bot_state."""
    await conn.execute("""
        INSERT INTO bot_state (key, value, updated_at)
        VALUES ($1, $2, $3)
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = $3
    """, key, value, __import__('datetime').datetime.utcnow())