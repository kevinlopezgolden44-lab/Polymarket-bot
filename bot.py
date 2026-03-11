import asyncio
import aiohttp
import logging
import os
import re
import json
from datetime import datetime, timedelta
import asyncpg

from database import (
    init_db, get_risk_state, get_dynamic_limits, reset_loss_streak,
    log_alert, log_opportunity, update_price_history, get_price_history,
    log_sentiment, get_daily_stats, get_alerted_markets,
    get_logged_opportunities, check_resolutions,
    update_open_positions, run_weekly_backtest,
    record_alert_snapshot, cleanup_old_snapshots,
    update_risk_state,
)
from scoring import score_opportunity, is_market_active, detect_category, detect_market_type
from research import (
    get_fear_greed, build_research_summary, get_crypto_data,
    prefetch_all_crypto, get_sports_odds,
    prefetch_sports_odds, detect_sport_key, FUTURES_ONLY_SPORTS,
)
from analysis import (
    analyze_price_momentum, analyze_price_velocity,
    analyze_liquidity, check_cross_market_consistency,
    detect_polymarket_lag, analyze_event_timing,
    check_resolution_ambiguity, calculate_confidence_tier
)
from telegram import (
    send_message, send_alert, get_updates, answer_callback,
    send_status, send_daily_summary, send_heartbeat, send_weekly_analysis
)

CONFIG = {
    "check_interval_seconds": 30,
    "min_score_for_alert": 85,
    "log_opportunity_threshold": 40,
    "markets_per_page": 100,
    "max_pages": 50,
    "summary_hour_utc": 13,
    "heartbeat_hour_utc": 12,
    "resolution_check_hours": [6, 8, 10, 12, 14, 16, 18, 20, 22],
    "weekly_analysis_day": 6,
    # Connection pool settings
    "pool_min_size": 2,
    "pool_max_size": 10,
    # Market filters
    "min_volume_24h": 1000,         # Skip markets with thin liquidity
    "min_yes_price": 0.02,          # Skip near-zero YES price (market decided)
    "max_yes_price": 0.98,          # Skip near-certain YES price (market decided)
    "coin_flip_low": 0.44,          # Coin-flip filter: YES price range
    "coin_flip_high": 0.56,
    "coin_flip_max_hours": 4,       # Coin-flip filter: resolves within N hours
    # Position management
    "take_profit_pct": 40.0,        # Exit at +40%
    "stop_loss_pct": -25.0,         # Exit at -25%
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
THERUNDOWN_API_KEY = os.environ.get("THERUNDOWN_API_KEY") or os.environ.get("ODDS_API_KEY")  # ODDS_API_KEY fallback for existing deployments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

# Filters defined once at module level — not inside the hot loop
ESPORTS_KEYWORDS = [
    "counter-strike", "cs2", "valorant", "dota", "league of legends",
    "lol:", "bo3", "bo5", "esport", "gaming league", "blast",
    "esl", "faceit", "dreamhack", "iem ", "majors:"
]
WEATHER_KEYWORDS = [
    "temperature", "rainfall", "precipitation", "hurricane",
    "tornado", "snowfall", "weather", "degrees", "fahrenheit",
    "celsius", "highest temp", "lowest temp", "wind speed"
]
TENNIS_KEYWORDS = [
    "atp ", "wta ", "open tennis", "wimbledon",
    "french open", "us open tennis", "roland garros"
]
GOLF_KEYWORDS = [
    "pga tour", "pga championship", "masters golf", "the masters",
    "us open golf", "the open championship", "ryder cup",
    "shot", "birdie", "bogey", "fairway", "leaderboard",
    "round 1", "round 2", "round 3", "round 4",
    "stroke play", "match play", "hole in one",
    # Player names most likely to appear in golf markets
    "scottie scheffler", "rory mcilroy", "viktor hovland",
    "jon rahm", "xander schauffele", "collin morikawa",
    "tiger woods", "phil mickelson", "dustin johnson",
]
SOCIAL_COUNT_PATTERN = re.compile(
    r'(post|tweet|share) \d+[\-+]\d* (tweet|post|time|x post|message)',
    re.IGNORECASE
)
SHORT_WINDOW_KEYWORDS = ["up or down", "pump or dump", "higher or lower", "above or below"]
OVER_UNDER_KEYWORDS   = ["o/u ", ": o/u", "over/under", "total points", "total runs", "total goals"]
FUTURES_KEYWORDS = [
    "win the nba finals", "win the nba championship",
    "win the super bowl", "win the world series",
    "win the stanley cup", "win the champions league",
    "win the world cup", "nba champion", "nfl champion",
    "2025 champion", "2026 champion", "2027 champion",
    "win it all", "go all the way",
]
FUTURES_PATTERN = re.compile(
    r'win the \d{4} (nba|nfl|mlb|nhl|champions league|world cup|super bowl)'
)

def now():
    return datetime.utcnow()


async def fetch_all_markets():
    all_markets = []
    offset = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 3

    async with aiohttp.ClientSession() as session:
        while offset < CONFIG["max_pages"] * CONFIG["markets_per_page"]:
            url = (
                "https://gamma-api.polymarket.com/markets"
                "?active=true&closed=false"
                "&limit=" + str(CONFIG["markets_per_page"])
                + "&offset=" + str(offset)
                + "&order=volume24hr&ascending=false"
            )
            page = offset // CONFIG["markets_per_page"] + 1
            page_ok = False

            for attempt in range(3):
                try:
                    async with session.get(url, headers=headers,
                                           timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        if resp.status == 200:
                            markets = await resp.json()
                            if not markets:
                                log.info("Page %d: empty — end of markets", page)
                                return all_markets
                            all_markets.extend(markets)
                            log.info("Page %d: %d markets (total: %d)",
                                     page, len(markets), len(all_markets))
                            if len(markets) < CONFIG["markets_per_page"]:
                                return all_markets
                            page_ok = True
                            break
                        elif resp.status == 429:
                            wait = 2 ** attempt
                            log.warning("Page %d: rate limited, retrying in %ds", page, wait)
                            await asyncio.sleep(wait)
                        else:
                            log.warning("Page %d: API error %d (attempt %d/3)",
                                        page, resp.status, attempt + 1)
                            await asyncio.sleep(1)
                except asyncio.TimeoutError:
                    log.warning("Page %d: timeout (attempt %d/3)", page, attempt + 1)
                    await asyncio.sleep(1)
                except Exception as e:
                    log.warning("Page %d: fetch error (attempt %d/3): %s", page, attempt + 1, e)
                    await asyncio.sleep(1)

            if page_ok:
                consecutive_failures = 0
                offset += CONFIG["markets_per_page"]
                await asyncio.sleep(0.3)
            else:
                consecutive_failures += 1
                log.error("Page %d: failed after 3 attempts (consecutive failures: %d)",
                          page, consecutive_failures)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log.error("Stopping fetch after %d consecutive page failures", consecutive_failures)
                    break
                # Skip this page and continue to the next
                offset += CONFIG["markets_per_page"]

    return all_markets


async def load_upcoming_events(conn):
    try:
        rows = await conn.fetch("""
            SELECT event_name, event_date, category, relevance_keywords
            FROM event_calendar
            WHERE event_date > NOW()
            ORDER BY event_date ASC
        """)
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("load_upcoming_events error: %s", e)
        return []


async def seed_event_calendar(conn):
    events = [
        ("FOMC Meeting", "2026-03-18 18:00:00", "Economics", "fed,federal reserve,interest rate,fomc,rate decision"),
        ("NBA Playoffs Start", "2026-04-15 00:00:00", "Sports", "nba,playoffs,basketball"),
        ("MLB Season Start", "2026-03-26 00:00:00", "Sports", "mlb,baseball,world series"),
        ("US CPI Report", "2026-03-12 12:30:00", "Economics", "cpi,inflation,consumer price"),
        ("US Jobs Report", "2026-04-03 12:30:00", "Economics", "jobs,unemployment,nonfarm payroll"),
        ("FIFA World Cup 2026", "2026-06-11 00:00:00", "Sports", "world cup,fifa,soccer,football"),
        ("US Midterms", "2026-11-03 00:00:00", "Politics", "election,senate,congress,midterm,vote"),
    ]
    for name, date_str, category, keywords in events:
        existing = await conn.fetchrow(
            "SELECT id FROM event_calendar WHERE event_name=$1", name
        )
        if not existing:
            await conn.execute("""
                INSERT INTO event_calendar (event_name, event_date, category, relevance_keywords, created_at)
                VALUES ($1, $2, $3, $4, $5)
            """, name, datetime.fromisoformat(date_str), category, keywords, now())
    log.info("Event calendar seeded")


async def process_updates(pool, updates, last_update_id):
    new_last_id = last_update_id
    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id <= last_update_id:
            continue
        new_last_id = max(new_last_id, update_id)

        message = update.get("message", {})
        if message:
            text = message.get("text", "")
            if text and "/status" in text.lower():
                log.info("Status command received")
                async with pool.acquire() as conn:
                    await send_status(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, conn)

        callback = update.get("callback_query")
        if callback:
            data = callback.get("data", "")
            callback_id = callback.get("id")
            await answer_callback(TELEGRAM_TOKEN, callback_id)
            if data.startswith("agree_") or data.startswith("disagree_"):
                parts = data.split("_")
                rating = parts[0]
                alert_id = parts[1]
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE alerts SET user_rating=$1 WHERE id=$2",
                        rating, int(alert_id)
                    )
                emoji = "👍" if rating == "agree" else "👎"
                await send_message(
                    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                    emoji + " Feedback recorded for alert #" + alert_id + "\n"
                    "Rating: " + rating.capitalize()
                )
    return new_last_id


def _should_skip_market(question_lower):
    """Returns True if this market should be filtered out before scoring."""
    if any(kw in question_lower for kw in ESPORTS_KEYWORDS):
        return True
    if any(kw in question_lower for kw in WEATHER_KEYWORDS):
        return True
    if any(kw in question_lower for kw in TENNIS_KEYWORDS):
        return True
    if any(kw in question_lower for kw in GOLF_KEYWORDS):
        return True
    if any(kw in question_lower for kw in SHORT_WINDOW_KEYWORDS):
        return True
    if any(kw in question_lower for kw in OVER_UNDER_KEYWORDS):
        return True
    if any(kw in question_lower for kw in FUTURES_KEYWORDS):
        return True
    if FUTURES_PATTERN.search(question_lower):
        return True
    # Tweet/post count bucket markets e.g. "post 360-379 tweets"
    # Only one bucket can resolve YES — guaranteed losses on all others
    if SOCIAL_COUNT_PATTERN.search(question_lower):
        return True
    return False


async def main():
    log.info("=" * 50)
    log.info("Polymarket Bot v16 Starting...")
    log.info("Change: Connection pooling (asyncpg.create_pool)")
    log.info("Change: Vegas gap now wired into score_opportunity")
    log.info("=" * 50)

    # ── CONNECTION POOL ────────────────────────────────────────────────────────
    # Creates a pool of 2–10 persistent connections.
    # If a connection drops (Railway timeout etc), the pool auto-reconnects.
    # All DB calls acquire a connection, use it, then return it to the pool.
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=CONFIG["pool_min_size"],
        max_size=CONFIG["pool_max_size"],
        command_timeout=30,         # per-query timeout
        max_inactive_connection_lifetime=300,  # recycle idle connections every 5 min
    )
    log.info("Connection pool ready (min=%d max=%d)",
             CONFIG["pool_min_size"], CONFIG["pool_max_size"])

    async with pool.acquire() as conn:
        await init_db(conn)
        await seed_event_calendar(conn)
        await reset_loss_streak(conn)

    await send_message(
        TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
        "<b>Polymarket Bot v16 Started!</b>\n\n"
        "Changes in this version:\n"
        "Connection pooling — bot survives DB reconnects\n"
        "Vegas gap now scores markets (not just shown in alerts)\n\n"
        "Type /status to check stats!"
    )

    alerted_markets = set()
    logged_opportunities = set()
    async with pool.acquire() as conn:
        alerted_markets = await get_alerted_markets(conn)
        logged_opportunities = await get_logged_opportunities(conn)

    last_summary_date = None
    last_heartbeat_date = None
    last_weekly_date = None
    last_resolution_hours = set()
    last_update_id = 0
    consecutive_errors = 0
    fear_greed_cache = {"data": None, "cached_at": None}

    while True:
        try:
            current_time = now()

            # ── Telegram updates ───────────────────────────────────────────────
            updates = await get_updates(TELEGRAM_TOKEN, last_update_id + 1)
            if updates:
                last_update_id = await process_updates(pool, updates, last_update_id)

            # ── Fear & Greed (refresh hourly) ──────────────────────────────────
            if (fear_greed_cache["cached_at"] is None or
                    (now() - fear_greed_cache["cached_at"]).total_seconds() > 3600):
                fear_greed = await get_fear_greed()
                fear_greed_cache["data"] = fear_greed
                fear_greed_cache["cached_at"] = now()
                if fear_greed.get("success"):
                    async with pool.acquire() as conn:
                        await log_sentiment(conn, fear_greed)
                    log.info("Fear and Greed: %d (%s) %s",
                             fear_greed["score"], fear_greed["regime"],
                             fear_greed.get("trend", ""))
            else:
                fear_greed = fear_greed_cache["data"]

            # ── Scheduled tasks ────────────────────────────────────────────────
            if (current_time.hour == CONFIG["heartbeat_hour_utc"] and
                    current_time.date() != last_heartbeat_date):
                async with pool.acquire() as conn:
                    await send_heartbeat(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, conn)
                last_heartbeat_date = current_time.date()

            if (current_time.hour == CONFIG["summary_hour_utc"] and
                    current_time.date() != last_summary_date):
                async with pool.acquire() as conn:
                    await send_daily_summary(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, conn)
                last_summary_date = current_time.date()

            if (current_time.weekday() == CONFIG["weekly_analysis_day"] and
                    current_time.date() != last_weekly_date):
                async with pool.acquire() as conn:
                    await send_weekly_analysis(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, conn)
                    backtest_report = await run_weekly_backtest(conn)
                    await send_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, backtest_report)
                    await cleanup_old_snapshots(conn)
                last_weekly_date = current_time.date()

            resolution_key = str(current_time.date()) + "_" + str(current_time.hour)
            if (current_time.hour in CONFIG["resolution_check_hours"] and
                    resolution_key not in last_resolution_hours):
                log.info("Running resolution check...")
                async with pool.acquire() as conn:
                    resolved_count = await check_resolutions(conn)
                    # Update win/loss streaks for each newly resolved market
                    if resolved_count > 0:
                        recently_resolved = await conn.fetch("""
                            SELECT profitable FROM alerts
                            WHERE outcome IS NOT NULL
                            ORDER BY alerted_at DESC
                            LIMIT $1
                        """, resolved_count)
                        for row in recently_resolved:
                            if row["profitable"] is not None:
                                await update_risk_state(conn, row["profitable"])
                last_resolution_hours.add(resolution_key)
                if len(last_resolution_hours) > 100:
                    last_resolution_hours = set(list(last_resolution_hours)[-50:])
                if resolved_count > 0:
                    await send_message(
                        TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                        "<b>Resolution Check Complete</b>\n\n"
                        "Resolved " + str(resolved_count) + " markets\n"
                        "Type /status to see updated win rate!"
                    )

            # ── Monitor open positions (pool passed — each position gets own conn)
            closed_positions = await update_open_positions(pool)
            for pos in closed_positions:
                emoji = "✅" if pos["profitable"] else "❌"
                direction = "+" if pos["return_pct"] >= 0 else ""
                msg = (
                    f"{emoji} <b>Position Closed [{pos['outcome_type']}]</b>\n\n"
                    f"{pos['question'][:80]}\n\n"
                    f"Entry: {round(pos['entry_price']*100)}¢  →  "
                    f"Exit: {round(pos['exit_price']*100)}¢\n"
                    f"Return: {direction}{pos['return_pct']}%  "
                    f"(Peak was +{pos['peak_return_pct']}%)\n"
                    f"Reason: {pos['exit_reason']}"
                )
                await send_message(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)

            # ── Pre-fetch data before scan ─────────────────────────────────────
            await prefetch_all_crypto()

            async with pool.acquire() as conn:
                upcoming_events = await load_upcoming_events(conn)

            log.info("Scanning Polymarket markets...")
            markets = await fetch_all_markets()

            # Build scan-level odds cache — fetch each sport ONCE, reuse per market.
            # Without this, 30+ sports markets = 30+ API calls per scan loop,
            # burning through the free tier (500 requests/month) in hours.
            _scan_odds_cache: dict = {}  # sport_key -> [games]
            if THERUNDOWN_API_KEY and markets:
                seen_sport_keys: set = set()
                for _m in markets:
                    if detect_category(_m.get("question", "")) == "Sports":
                        _sk = detect_sport_key(_m.get("question", ""))
                        if _sk and _sk not in FUTURES_ONLY_SPORTS and _sk not in seen_sport_keys:
                            seen_sport_keys.add(_sk)
                            _games = await prefetch_sports_odds(_sk, THERUNDOWN_API_KEY)
                            if _games:
                                _scan_odds_cache[_sk] = _games
                if seen_sport_keys:
                    log.info("Sports odds cached for: %s", ", ".join(seen_sport_keys))

            if not markets:
                consecutive_errors += 1
                log.warning("No markets returned (error streak: %d)", consecutive_errors)
                if consecutive_errors >= 10:
                    await send_message(
                        TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                        "<b>Bot Error Alert</b>\n\n"
                        "Could not reach Polymarket API for 5+ minutes\n"
                        "Bot is still running and retrying"
                    )
                    consecutive_errors = 0
            else:
                consecutive_errors = 0
                active_markets = [m for m in markets if is_market_active(m)]
                log.info("%d total -> %d active after filtering",
                         len(markets), len(active_markets))

                async with pool.acquire() as conn:
                    state = await get_risk_state(conn)
                limits = get_dynamic_limits(state)

                opportunities = []

                for market in active_markets:
                    question_lower = market.get("question", "").lower()

                    # ── Pre-score filters ──────────────────────────────────────
                    if _should_skip_market(question_lower):
                        continue

                    # Coin-flip filter: near-50 price AND resolves in under 4 hours
                    try:
                        _outcomes_chk = market.get("outcomePrices", "[0.5]")
                        if isinstance(_outcomes_chk, str):
                            _outcomes_chk = json.loads(_outcomes_chk)
                        _yes_chk = float(_outcomes_chk[0]) if _outcomes_chk else 0.5
                        _end_chk = market.get("endDate") or market.get("end_date")
                        if _end_chk:
                            _end_dt_chk = datetime.fromisoformat(
                                str(_end_chk).replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                            _hrs_to_res = (_end_dt_chk - now()).total_seconds() / 3600
                            if _hrs_to_res < CONFIG["coin_flip_max_hours"] and CONFIG["coin_flip_low"] <= _yes_chk <= CONFIG["coin_flip_high"]:
                                continue
                    except Exception:
                        pass

                    # Minimum volume filter
                    if float(market.get("volume24hr", 0) or 0) < CONFIG["min_volume_24h"]:
                        continue

                    # YES price bounds filter
                    try:
                        _outcomes_price = market.get("outcomePrices", "[0.5]")
                        if isinstance(_outcomes_price, str):
                            _outcomes_price = json.loads(_outcomes_price)
                        _yes_price_chk = float(_outcomes_price[0]) if _outcomes_price else 0.5
                        if _yes_price_chk < CONFIG["min_yes_price"] or _yes_price_chk > CONFIG["max_yes_price"]:
                            continue
                    except Exception:
                        pass

                    # ── Pre-fetch price history ────────────────────────────────
                    _market_id_pre = str(market.get("id", ""))
                    _history_pre = []
                    if _market_id_pre:
                        try:
                            async with pool.acquire() as conn:
                                _history_pre = await get_price_history(conn, _market_id_pre)
                        except Exception:
                            pass

                    # Detect category early so we can look up the scan-level cache
                    category = detect_category(market.get("question", ""))
                    sports_odds = None
                    if category == "Sports" and THERUNDOWN_API_KEY:
                        try:
                            _sk = detect_sport_key(market.get("question", ""))
                            if _sk and _sk not in FUTURES_ONLY_SPORTS:
                                # Use already-fetched game list for this sport —
                                # avoids a live API call per market (free tier: 500/month)
                                _cached_games = _scan_odds_cache.get(_sk)
                                sports_odds = await get_sports_odds(
                                    market.get("question", ""), THERUNDOWN_API_KEY,
                                    prefetched_games=_cached_games,
                                )
                        except Exception as e:
                            log.warning("Sports odds lookup error: %s", e)

                    result = score_opportunity(
                        market,
                        price_history_rows=_history_pre,
                        all_markets=active_markets,
                        upcoming_events=upcoming_events,
                        fear_greed=fear_greed,
                        sports_odds=sports_odds,   # ← NOW PASSED IN AT SCORE TIME
                    )
                    score = result["score"]
                    reason = result["reason"]
                    category = result["category"]
                    market_age = result["signals"]["age_hours"]

                    if score < CONFIG["log_opportunity_threshold"]:
                        continue

                    question = market.get("question", "Unknown")
                    outcomes = market.get("outcomePrices", "[0.5]")
                    if isinstance(outcomes, str):
                        outcomes = json.loads(outcomes)
                    yes_price = float(outcomes[0]) if outcomes else 0.5
                    market_id = str(market.get("id", question[:50]))
                    volume = float(market.get("volumeNum", 0) or 0)

                    # ── Days to resolution ─────────────────────────────────────
                    days_to_resolution = None
                    end_date_raw = market.get("endDate") or market.get("end_date")
                    if end_date_raw:
                        try:
                            end_dt = datetime.fromisoformat(
                                str(end_date_raw).replace("Z", "+00:00")
                            ).replace(tzinfo=None)
                            days_to_resolution = round(
                                (end_dt - now()).total_seconds() / 86400, 1
                            )
                        except Exception:
                            pass

                    bid_price = float(market.get("bestBid", 0) or 0) or None
                    ask_price = float(market.get("bestAsk", 0) or 0) or None

                    score_breakdown = {
                        "base": 50,
                        "liquidity": 5 if result["signals"].get("liquidity", {}).get("liquid", True) else -20,
                        "momentum": result["signals"].get("momentum", {}).get("signal", "STABLE"),
                        "velocity": result["signals"].get("velocity", {}).get("fast_move", False),
                        "ambiguity": bool(result["signals"].get("ambiguity")),
                        "lag": bool(result["signals"].get("lag")),
                        "age_hours": market_age,
                        "vegas_gap": result["signals"].get("vegas_gap"),
                        "final_score": score,
                    }

                    market_type = result.get("market_type", "GENERAL")

                    # ── 30-day price range ────────────────────────────────────
                    price_pct_of_range = None
                    async with pool.acquire() as conn:
                        history_30d = await conn.fetch("""
                            SELECT yes_price FROM price_history
                            WHERE market_id=$1
                            AND recorded_at > NOW() - INTERVAL '30 days'
                        """, market_id)
                    if history_30d and len(history_30d) >= 3:
                        prices_30d = [float(r["yes_price"]) for r in history_30d]
                        lo, hi = min(prices_30d), max(prices_30d)
                        if hi > lo:
                            price_pct_of_range = round((yes_price - lo) / (hi - lo) * 100, 1)

                    async with pool.acquire() as conn:
                        revisit_count = await conn.fetchval(
                            "SELECT COUNT(*) FROM alerts WHERE market_id=$1", market_id
                        )

                    opp = {
                        "id": market_id,
                        "question": question,
                        "score": score,
                        "reason": reason,
                        "yes_price": yes_price,
                        "volume": volume,
                        "age": market_age,
                        "category": category,
                        "confidence_tier": "LOW",
                        "days_to_resolution": days_to_resolution,
                        "bid_price": bid_price,
                        "ask_price": ask_price,
                        "score_breakdown": score_breakdown,
                        "market_type": market_type,
                        "price_pct_of_range": price_pct_of_range,
                        # Expose Vegas gap in opp for alert display
                        "vegas_gap": result["signals"].get("vegas_gap"),
                        "vegas_implied": result["signals"].get("vegas_implied"),
                        "direction": result.get("direction", "NO_EDGE"),
                        "edge_pct": result.get("edge_pct"),
                    }

                    # ── Analysis modules (using scoring results — no re-computation)
                    liquidity = result["signals"]["liquidity"]
                    if not liquidity["liquid"]:
                        opp["liquidity_warning"] = liquidity["warning"]

                    ambiguity = result["signals"]["ambiguity"]
                    if ambiguity:
                        opp["ambiguity_warning"] = ambiguity

                    event_matches = result["signals"]["matched_events"]
                    if event_matches:
                        opp["upcoming_events"] = event_matches

                    inconsistencies = result["signals"]["inconsistencies"]
                    if inconsistencies:
                        opp["inconsistencies"] = inconsistencies

                    # ── Price history analysis ─────────────────────────────────
                    # Seed confirming/contradicting from scoring.py's counts so that
                    # liquidity, spread, ambiguity, days-to-resolution etc. are included.
                    confirming = result.get("confirming", 0)
                    contradicting = result.get("contradicting", 0)

                    if market_id in alerted_markets:
                        async with pool.acquire() as conn:
                            await update_price_history(conn, market_id, yes_price)
                            history = await get_price_history(conn, market_id)

                        if history:
                            momentum = analyze_price_momentum(history)
                            opp["momentum"] = momentum
                            if momentum["signal"] in ["RISING", "STRONG_RISING"]:
                                confirming += 1
                            elif momentum["signal"] in ["FALLING", "STRONG_FALLING"]:
                                contradicting += 1

                            velocity = analyze_price_velocity(history)
                            if velocity["fast_move"]:
                                opp["velocity_alert"] = velocity["alert"]
                                confirming += 1

                            try:
                                now_ts = now()
                                def price_n_days_ago(h, days):
                                    cutoff = now_ts - timedelta(hours=days*24)
                                    candidates = [r for r in h if r["recorded_at"] <= cutoff]
                                    return float(candidates[0]["yes_price"]) if candidates else None
                                opp["price_1d_ago"] = price_n_days_ago(history, 1)
                                opp["price_3d_ago"] = price_n_days_ago(history, 3)
                                opp["price_7d_ago"] = price_n_days_ago(history, 7)
                            except Exception:
                                pass

                        async with pool.acquire() as conn:
                            alert_row = await conn.fetchrow(
                                "SELECT id, alerted_at FROM alerts WHERE market_id=$1 ORDER BY alerted_at DESC LIMIT 1",
                                market_id
                            )
                            if alert_row:
                                await record_alert_snapshot(
                                    conn, alert_row["id"], market_id, yes_price,
                                    alert_row["alerted_at"],
                                    bid_price=opp.get("bid_price"),
                                    ask_price=opp.get("ask_price")
                                )

                    # ── Polymarket lag detection (Crypto) ──────────────────────
                    if category == "Crypto":
                        crypto_data = await get_crypto_data(question)
                        lag = detect_polymarket_lag(question, yes_price, crypto_data)
                        if lag:
                            opp["lag_detected"] = lag
                            confirming += 2

                    # Vegas gap confirming signals (already scored — just count for tier)
                    vegas_gap = result["signals"].get("vegas_gap")
                    if vegas_gap is not None and abs(vegas_gap) > 10:
                        confirming += 2 if abs(vegas_gap) > 15 else 1

                    opp["confidence_tier"] = calculate_confidence_tier(
                        score, confirming, contradicting
                    )

                    # ── Signals fired string ───────────────────────────────────
                    fired = []
                    if opp.get("momentum", {}).get("signal") in ["RISING", "STRONG_RISING"]:
                        fired.append("momentum_up")
                    if opp.get("momentum", {}).get("signal") in ["FALLING", "STRONG_FALLING"]:
                        fired.append("momentum_down")
                    if opp.get("velocity_alert"):
                        fired.append("velocity")
                    if opp.get("lag_detected"):
                        fired.append("lag_detected")
                    if opp.get("inconsistencies"):
                        fired.append("cross_market")
                    if opp.get("upcoming_events"):
                        fired.append("event_timing")
                    if opp.get("liquidity_warning"):
                        fired.append("low_liquidity")
                    if opp.get("ambiguity_warning"):
                        fired.append("ambiguous")
                    if vegas_gap is not None and abs(vegas_gap) > 10:
                        fired.append("vegas_gap")
                    opp["signals_fired"] = ",".join(fired)

                    # ── Log opportunity (silent, 40+) ──────────────────────────
                    if market_id not in logged_opportunities:
                        async with pool.acquire() as conn:
                            await log_opportunity(conn, opp, fear_greed, market_age)
                        logged_opportunities.add(market_id)

                    if score >= CONFIG["min_score_for_alert"]:
                        opportunities.append(opp)

                opportunities.sort(key=lambda x: x["score"], reverse=True)

                if opportunities:
                    log.info("Found %d alertable opportunities", len(opportunities))
                    for opp in opportunities[:5]:
                        log.info("Score:%d [%s] %s",
                                 opp["score"], opp["category"], opp["question"][:60])
                        if opp["id"] not in alerted_markets:
                            async with pool.acquire() as conn:
                                alert_id = await log_alert(
                                    conn, opp, fear_greed, opp.get("age")
                                )
                            alerted_markets.add(opp["id"])

                            # Research summary for alert message
                            # Sports odds already fetched for scoring — reuse via
                            # build_research_summary which fetches internally.
                            # For crypto the cache is already warm from prefetch.
                            research = await build_research_summary(
                                opp["question"], opp["yes_price"],
                                opp["category"], fear_greed, THERUNDOWN_API_KEY
                            )

                            await send_alert(
                                TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                                opp, alert_id, research, limits
                            )
                        else:
                            async with pool.acquire() as conn:
                                await conn.execute("""
                                    UPDATE alerts SET revisit_count = COALESCE(revisit_count, 0) + 1
                                    WHERE market_id=$1 AND id=(
                                        SELECT id FROM alerts WHERE market_id=$1
                                        ORDER BY alerted_at DESC LIMIT 1
                                    )
                                """, opp["id"])
                else:
                    log.info("No new alertable opportunities this scan")

        except Exception as e:
            log.error("Unexpected error: %s", e)
            await send_message(
                TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
                "<b>Bot Error Alert</b>\n\n"
                "Unexpected error: " + str(e)[:200] + "\n"
                "Bot is attempting to recover automatically"
            )

        log.info("Next scan in %d seconds", CONFIG["check_interval_seconds"])
        log.info("-" * 50)
        await asyncio.sleep(CONFIG["check_interval_seconds"])


if __name__ == "__main__":
    asyncio.run(main())