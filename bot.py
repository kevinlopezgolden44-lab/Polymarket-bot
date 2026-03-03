import asyncio
import aiohttp
import logging
import os
import json
from datetime import datetime
import asyncpg

CONFIG = {
    "check_interval_seconds": 30,
    "min_score_for_alert": 70,
    "log_opportunity_threshold": 40,
    "markets_per_page": 100,
    "max_pages": 50,
    "summary_hour_utc": 13,
    "heartbeat_hour_utc": 12,
    "resolution_check_hours": [6, 8, 10, 12, 14, 16, 18, 20, 22],
    "weekly_analysis_day": 6,
    "dynamic_risk": {
        "base_daily_loss": 20,
        "base_trade_size": 5,
        "max_open_positions": 10,
        "win_streak_bonus": 1.25,
        "loss_streak_penalty": 0.75,
        "streak_threshold": 3
    }
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
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
            score_components TEXT
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
            market_age_hours FLOAT
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
    await conn.execute("""ALTER TABLE alerts ADD COLUMN IF NOT EXISTS user_rating TEXT""")
    await conn.execute("""ALTER TABLE alerts ADD COLUMN IF NOT EXISTS fear_greed_score INTEGER""")
    await conn.execute("""ALTER TABLE alerts ADD COLUMN IF NOT EXISTS fear_greed_regime TEXT""")
    await conn.execute("""ALTER TABLE alerts ADD COLUMN IF NOT EXISTS market_age_hours FLOAT""")
    await conn.execute("""ALTER TABLE alerts ADD COLUMN IF NOT EXISTS score_components TEXT""")
    await conn.execute("""ALTER TABLE sentiment_history ADD COLUMN IF NOT EXISTS trend TEXT""")
    log.info("Database tables ready")

async def get_risk_state(conn):
    today = now().strftime("%Y-%m-%d")
    row = await conn.fetchrow("SELECT * FROM risk_log WHERE date = $1", today)
    if not row:
        await conn.execute("""
            INSERT INTO risk_log (date, daily_loss, trades_today, win_streak, loss_streak, trade_size_multiplier)
            VALUES ($1, 0, 0, 0, 0, 1.0)
        """, today)
        row = await conn.fetchrow("SELECT * FROM risk_log WHERE date = $1", today)
    return dict(row)

async def update_risk_state(conn, profitable):
    today = now().strftime("%Y-%m-%d")
    state = await get_risk_state(conn)
    cfg = CONFIG["dynamic_risk"]
    if profitable:
        new_win = state["win_streak"] + 1
        new_loss = 0
        multiplier = cfg["win_streak_bonus"] if new_win >= cfg["streak_threshold"] else 1.0
    else:
        new_win = 0
        new_loss = state["loss_streak"] + 1
        multiplier = cfg["loss_streak_penalty"] if new_loss >= cfg["streak_threshold"] else 1.0
    await conn.execute("""
        UPDATE risk_log
        SET win_streak = $1, loss_streak = $2, trade_size_multiplier = $3
        WHERE date = $4
    """, new_win, new_loss, multiplier, today)

def get_dynamic_limits(state):
    cfg = CONFIG["dynamic_risk"]
    multiplier = state.get("trade_size_multiplier", 1.0)
    return {
        "max_trade_size": round(cfg["base_trade_size"] * multiplier, 2),
        "max_daily_loss": cfg["base_daily_loss"],
        "max_open_positions": cfg["max_open_positions"]
    }

async def log_alert(conn, opportunity, fear_greed=None, market_age=None):
    fg_score = fear_greed.get("score") if fear_greed else None
    fg_regime = fear_greed.get("regime") if fear_greed else None
    alert_id = await conn.fetchval("""
        INSERT INTO alerts (market_id, question, yes_price, score, reason, volume, alerted_at,
                           fear_greed_score, fear_greed_regime, market_age_hours, score_components)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id
    """,
        opportunity["id"],
        opportunity["question"],
        opportunity["yes_price"],
        opportunity["score"],
        opportunity["reason"],
        opportunity["volume"],
        now(),
        fg_score,
        fg_regime,
        market_age,
        opportunity["reason"]
    )
    count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    log.info("Alert logged to database (total: %d)", count)
    return alert_id

async def log_opportunity(conn, opportunity, fear_greed=None, market_age=None):
    existing = await conn.fetchrow(
        "SELECT id FROM opportunities_log WHERE market_id = $1", opportunity["id"]
    )
    if existing:
        return
    fg_score = fear_greed.get("score") if fear_greed else None
    fg_regime = fear_greed.get("regime") if fear_greed else None
    await conn.execute("""
        INSERT INTO opportunities_log (market_id, question, yes_price, score, reason, volume,
                                      logged_at, fear_greed_score, fear_greed_regime, market_age_hours)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    """,
        opportunity["id"],
        opportunity["question"],
        opportunity["yes_price"],
        opportunity["score"],
        opportunity["reason"],
        opportunity["volume"],
        now(),
        fg_score,
        fg_regime,
        market_age
    )

async def update_price_history(conn, market_id, yes_price):
    last = await conn.fetchrow("""
        SELECT recorded_at FROM price_history
        WHERE market_id = $1
        ORDER BY recorded_at DESC LIMIT 1
    """, market_id)
    if last:
        hours_since = (now() - last["recorded_at"]).total_seconds() / 3600
        if hours_since < 2:
            return
    await conn.execute("""
        INSERT INTO price_history (market_id, yes_price, recorded_at)
        VALUES ($1, $2, $3)
    """, market_id, yes_price, now())

async def log_sentiment(conn, fear_greed):
    await conn.execute("""
        INSERT INTO sentiment_history (score, classification, regime, trend, recorded_at)
        VALUES ($1, $2, $3, $4, $5)
    """, fear_greed["score"], fear_greed["classification"], fear_greed["regime"],
        fear_greed.get("trend", "UNKNOWN"), now())

async def get_daily_stats(conn):
    rows = await conn.fetch("""
        SELECT * FROM alerts
        WHERE alerted_at > NOW() - INTERVAL '24 hours'
    """)
    return rows

async def get_alerted_markets(conn):
    rows = await conn.fetch("SELECT DISTINCT market_id FROM alerts")
    return set(row["market_id"] for row in rows)

async def get_logged_opportunities(conn):
    rows = await conn.fetch("SELECT DISTINCT market_id FROM opportunities_log")
    return set(row["market_id"] for row in rows)

async def send_telegram(message, reply_markup=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured")
        return None
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    log.info("Telegram sent!")
                    return data.get("result", {}).get("message_id")
                else:
                    log.error("Telegram error: %d", resp.status)
                    return None
    except Exception as e:
        log.error("Telegram failed: %s", e)
        return None

async def answer_callback(callback_query_id):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/answerCallbackQuery"
    async with aiohttp.ClientSession() as session:
        await session.post(url, json={"callback_query_id": callback_query_id})

async def get_updates(offset=None):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/getUpdates"
    params = {"timeout": 1}
    if offset:
        params["offset"] = offset
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result", [])
    except:
        pass
    return []

async def send_status(conn):
    try:
        resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
        total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts")
        total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log")
        profitable_count = sum(1 for a in resolved if a["profitable"])
        win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0

        crypto_resolved = [a for a in resolved if "Crypto" in a["reason"]]
        sports_resolved = [a for a in resolved if "Sports" in a["reason"]]
        politics_resolved = [a for a in resolved if "Politics" in a["reason"]]
        crypto_win = round(sum(1 for a in crypto_resolved if a["profitable"]) / len(crypto_resolved) * 100) if crypto_resolved else 0
        sports_win = round(sum(1 for a in sports_resolved if a["profitable"]) / len(sports_resolved) * 100) if sports_resolved else 0
        politics_win = round(sum(1 for a in politics_resolved if a["profitable"]) / len(politics_resolved) * 100) if politics_resolved else 0

        fear_resolved = [a for a in resolved if a.get("fear_greed_regime") in ["Extreme Fear", "Fear"]]
        greed_resolved = [a for a in resolved if a.get("fear_greed_regime") in ["Greed", "Extreme Greed"]]
        fear_win = round(sum(1 for a in fear_resolved if a["profitable"]) / len(fear_resolved) * 100) if fear_resolved else 0
        greed_win = round(sum(1 for a in greed_resolved if a["profitable"]) / len(greed_resolved) * 100) if greed_resolved else 0

        agreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating = 'agree' AND outcome IS NOT NULL")
        disagreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating = 'disagree' AND outcome IS NOT NULL")
        agreed_win = round(sum(1 for a in agreed if a["profitable"]) / len(agreed) * 100) if agreed else 0
        disagreed_win = round(sum(1 for a in disagreed if a["profitable"]) / len(disagreed) * 100) if disagreed else 0

        state = await get_risk_state(conn)
        limits = get_dynamic_limits(state)

        sentiment = await conn.fetchrow("""
            SELECT score, regime, trend FROM sentiment_history
            ORDER BY recorded_at DESC LIMIT 1
        """)

        msg = (
            "<b>Bot Status Report</b>\n"
            + now().strftime("%B %d, %Y %H:%M UTC") + "\n\n"
            + "<b>Win Rates:</b>\n"
            + "Overall: " + str(win_rate) + "% (" + str(len(resolved)) + " resolved)\n"
            + "Crypto: " + str(crypto_win) + "% (" + str(len(crypto_resolved)) + " resolved)\n"
            + "Sports: " + str(sports_win) + "% (" + str(len(sports_resolved)) + " resolved)\n"
            + "Politics: " + str(politics_win) + "% (" + str(len(politics_resolved)) + " resolved)\n\n"
            + "<b>Sentiment Win Rates:</b>\n"
            + "During Fear: " + str(fear_win) + "% (" + str(len(fear_resolved)) + " resolved)\n"
            + "During Greed: " + str(greed_win) + "% (" + str(len(greed_resolved)) + " resolved)\n\n"
            + "<b>Your Judgment:</b>\n"
            + "When you agreed: " + str(agreed_win) + "% win rate (" + str(len(agreed)) + " rated)\n"
            + "When you disagreed: " + str(disagreed_win) + "% win rate (" + str(len(disagreed)) + " rated)\n\n"
            + "<b>Risk Limits:</b>\n"
            + "Max trade size: $" + str(limits["max_trade_size"]) + "\n"
            + "Max daily loss: $" + str(limits["max_daily_loss"]) + "\n"
            + "Max open positions: " + str(limits["max_open_positions"]) + "\n"
            + "Win streak: " + str(state["win_streak"]) + "\n"
            + "Loss streak: " + str(state["loss_streak"]) + "\n\n"
            + "<b>Database:</b>\n"
            + "Total alerts: " + str(total_alerts) + "\n"
            + "Opportunities logged: " + str(total_opps) + "\n"
            + "Resolved alerts: " + str(len(resolved)) + "\n\n"
        )

        if sentiment:
            msg += (
                "<b>Current Sentiment:</b>\n"
                + "Fear and Greed: " + str(sentiment["score"])
                + " (" + str(sentiment["regime"]) + ")\n"
                + "Trend: " + str(sentiment["trend"]) + "\n"
            )

        await send_telegram(msg)
        log.info("Status report sent")
    except Exception as e:
        log.error("Status error: %s", e)
        await send_telegram("Error generating status report: " + str(e)[:200])

async def process_feedback(conn, updates, last_update_id):
    new_last_id = last_update_id
    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id <= last_update_id:
            continue
        new_last_id = max(new_last_id, update_id)

        message = update.get("message", {})
        text = message.get("text", "")
        if text == "/status":
            log.info("Status command received")
            await send_status(conn)

        callback = update.get("callback_query")
        if callback:
            data = callback.get("data", "")
            callback_id = callback.get("id")
            await answer_callback(callback_id)
            if data.startswith("agree_") or data.startswith("disagree_"):
                parts = data.split("_")
                rating = parts[0]
                alert_id = parts[1]
                await conn.execute("""
                    UPDATE alerts SET user_rating = $1 WHERE id = $2
                """, rating, int(alert_id))
                emoji = "👍" if rating == "agree" else "👎"
                log.info("User rated alert %s as %s", alert_id, rating)
                await send_telegram(
                    emoji + " Feedback recorded for alert #" + alert_id + "\n"
                    "Rating: " + rating.capitalize() + "\n"
                    "This helps improve scoring over time!"
                )
    return new_last_id

async def get_fear_greed():
    try:
        url = "https://api.alternative.me/fng/?limit=7"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    entries = data.get("data", [])
                    if entries:
                        current = entries[0]
                        score = int(current.get("value", 50))
                        classification = current.get("value_classification", "Neutral")
                        scores_7d = [int(e.get("value", 50)) for e in entries]
                        avg_7d = round(sum(scores_7d) / len(scores_7d))
                        trend = "IMPROVING" if scores_7d[0] > scores_7d[-1] else "DECLINING"
                        if score <= 25:
                            regime = "Extreme Fear"
                            sentiment_bonus = 15 if trend == "IMPROVING" else 5
                        elif score <= 49:
                            regime = "Fear"
                            sentiment_bonus = 5
                        elif score <= 74:
                            regime = "Greed"
                            sentiment_bonus = -5
                        else:
                            regime = "Extreme Greed"
                            sentiment_bonus = -15
                        return {
                            "score": score,
                            "classification": classification,
                            "regime": regime,
                            "trend": trend,
                            "avg_7d": avg_7d,
                            "sentiment_bonus": sentiment_bonus,
                            "success": True
                        }
    except Exception as e:
        log.error("Fear and Greed error: %s", e)
    return {"success": False, "score": 50, "regime": "Unknown", "sentiment_bonus": 0, "trend": "UNKNOWN"}

async def get_crypto_research(question):
    try:
        question_lower = question.lower()
        if "ethereum" in question_lower or " eth" in question_lower:
            coin_id = "ethereum"
            coin_symbol = "ETH"
        elif "solana" in question_lower or " sol" in question_lower:
            coin_id = "solana"
            coin_symbol = "SOL"
        else:
            coin_id = "bitcoin"
            coin_symbol = "BTC"
        url = "https://api.coincap.io/v2/assets/" + coin_id
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    asset = data.get("data", {})
                    price = float(asset.get("priceUsd", 0))
                    change_24h = float(asset.get("changePercent24Hr", 0))
                    direction = "UP" if change_24h > 0 else "DOWN"
                    return {
                        "coin": coin_symbol,
                        "price": price,
                        "change_24h": round(change_24h, 2),
                        "direction": direction,
                        "success": True
                    }
    except Exception as e:
        log.error("CoinCap error: %s", e)
    return {"success": False}

def analyze_crypto_market(question, yes_price, crypto_data, fear_greed):
    lines = []
    if crypto_data.get("success"):
        current_price = crypto_data["price"]
        coin = crypto_data["coin"]
        change = crypto_data["change_24h"]
        direction = crypto_data["direction"]
        lines.append("Current " + coin + ": $" + str(round(current_price, 2)))
        lines.append("24h change: " + str(change) + "% " + direction)
        import re
        numbers = re.findall(r"\$[\d,]+", question)
        if numbers:
            try:
                target = float(numbers[0].replace("$", "").replace(",", ""))
                diff_pct = round((target - current_price) / current_price * 100, 1)
                if diff_pct > 0:
                    lines.append("Target needs +" + str(diff_pct) + "% move")
                else:
                    lines.append("Target needs " + str(diff_pct) + "% move")
                if yes_price < 0.15 and abs(diff_pct) > 10:
                    lines.append("Assessment: Target far from current price")
                    lines.append("Recommendation: LIKELY DISAGREE")
                elif yes_price < 0.15 and abs(diff_pct) <= 10:
                    lines.append("Assessment: Target reachable but priced low")
                    lines.append("Recommendation: INVESTIGATE")
                elif yes_price > 0.85 and abs(diff_pct) < 5:
                    lines.append("Assessment: Target close, priced high")
                    lines.append("Recommendation: LIKELY AGREE")
                else:
                    lines.append("Recommendation: INVESTIGATE FURTHER")
            except:
                pass
    if fear_greed and fear_greed.get("success"):
        lines.append("Fear and Greed: " + str(fear_greed["score"]) + " (" + fear_greed["regime"] + ")")
        lines.append("7d trend: " + fear_greed.get("trend", "UNKNOWN") + " (avg " + str(fear_greed.get("avg_7d", 0)) + ")")
        if fear_greed["regime"] == "Extreme Fear":
            lines.append("Sentiment: Historically good contrarian conditions")
        elif fear_greed["regime"] == "Extreme Greed":
            lines.append("Sentiment: Market euphoric - exercise caution")
    return "\n".join(lines)

async def get_sports_research(question):
    if not ODDS_API_KEY:
        return {"success": False}
    sports_map = {
        "nba": "basketball_nba",
        "nfl": "americanfootball_nfl",
        "mlb": "baseball_mlb",
        "nhl": "icehockey_nhl",
        "ufc": "mma_mixed_martial_arts",
        "premier league": "soccer_epl",
        "champions league": "soccer_uefa_champs_league",
        "super bowl": "americanfootball_nfl"
    }
    sport_key = "basketball_nba"
    question_lower = question.lower()
    for keyword, key in sports_map.items():
        if keyword in question_lower:
            sport_key = key
            break
    try:
        url = (
            "https://api.the-odds-api.com/v4/sports/" + sport_key + "/odds"
            "?apiKey=" + ODDS_API_KEY
            + "&regions=us&markets=h2h&oddsFormat=decimal"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    games = await resp.json()
                    if games:
                        words = question_lower.split()
                        best_match = None
                        best_score = 0
                        for game in games:
                            home = game.get("home_team", "").lower()
                            away = game.get("away_team", "").lower()
                            match_score = sum(1 for w in words if w in home or w in away)
                            if match_score > best_score:
                                best_score = match_score
                                best_match = game
                        if best_match and best_score >= 1:
                            home_team = best_match.get("home_team", "")
                            away_team = best_match.get("away_team", "")
                            bookmakers = best_match.get("bookmakers", [])
                            if bookmakers:
                                outcomes = bookmakers[0].get("markets", [{}])[0].get("outcomes", [])
                                odds_data = {}
                                for o in outcomes:
                                    implied_prob = round(1 / float(o.get("price", 2.0)) * 100, 1)
                                    odds_data[o.get("name", "")] = implied_prob
                                return {
                                    "success": True,
                                    "home_team": home_team,
                                    "away_team": away_team,
                                    "odds": odds_data
                                }
    except Exception as e:
        log.error("Odds API error: %s", e)
    return {"success": False}

def analyze_sports_market(question, yes_price, sports_data):
    if not sports_data.get("success"):
        return "Could not fetch live odds"
    home = sports_data["home_team"]
    away = sports_data["away_team"]
    odds = sports_data["odds"]
    lines = []
    lines.append("Matchup: " + away + " vs " + home)
    question_lower = question.lower()
    matched_team = None
    matched_prob = None
    for team, prob in odds.items():
        if any(word in question_lower for word in team.lower().split()):
            matched_team = team
            matched_prob = prob
            break
    for team, prob in odds.items():
        lines.append("Vegas: " + team + " " + str(prob) + "%")
    if matched_team and matched_prob:
        polymarket_pct = round(yes_price * 100, 1)
        gap = round(matched_prob - polymarket_pct, 1)
        lines.append("Polymarket YES: " + str(polymarket_pct) + "%")
        lines.append("Vegas implied: " + str(matched_prob) + "%")
        if gap > 10:
            lines.append("Gap: +" + str(gap) + "% vs Vegas")
            lines.append("Recommendation: AGREE - underpriced vs Vegas")
        elif gap < -10:
            lines.append("Gap: " + str(gap) + "% vs Vegas")
            lines.append("Recommendation: DISAGREE - overpriced vs Vegas")
        else:
            lines.append("Gap: " + str(gap) + "% - fair price")
            lines.append("Recommendation: INVESTIGATE")
    return "\n".join(lines)

async def build_research_summary(question, yes_price, reason, fear_greed):
    summary_lines = []
    is_crypto = "Crypto market" in reason
    is_sports = "Sports market" in reason
    if is_crypto:
        crypto_data = await get_crypto_research(question)
        analysis = analyze_crypto_market(question, yes_price, crypto_data, fear_greed)
        summary_lines.append("Crypto Research:")
        summary_lines.append(analysis)
    if is_sports:
        sports_data = await get_sports_research(question)
        analysis = analyze_sports_market(question, yes_price, sports_data)
        summary_lines.append("Sports Research:")
        summary_lines.append(analysis)
    if not summary_lines:
        return None
    return "\n".join(summary_lines)

async def check_resolutions(conn):
    unresolved = await conn.fetch("""
        SELECT * FROM alerts
        WHERE outcome IS NULL
        AND alerted_at < NOW() - INTERVAL '1 hour'
    """)
    unresolved_opps = await conn.fetch("""
        SELECT * FROM opportunities_log
        WHERE outcome IS NULL
        AND logged_at < NOW() - INTERVAL '1 hour'
    """)
    all_unresolved = list(unresolved) + list(unresolved_opps)
    if not all_unresolved:
        log.info("No unresolved items to check")
        return
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
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
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
                                        profitable = alert["yes_price"] < 0.5
                                    elif final_yes <= 0.01:
                                        outcome = "NO"
                                        profitable = alert["yes_price"] > 0.5
                                    else:
                                        continue
                                    table = "alerts" if "alerted_at" in alert.keys() else "opportunities_log"
                                    await conn.execute(
                                        "UPDATE " + table + " SET outcome = $1, profitable = $2 WHERE id = $3",
                                        outcome, profitable, alert["id"]
                                    )
                                    if table == "alerts":
                                        await update_risk_state(conn, profitable)
                                    resolved_count += 1
                                    log.info("Resolved: %s -> %s profitable=%s",
                                             alert["question"][:40], outcome, profitable)
                await asyncio.sleep(0.3)
            except Exception as e:
                log.error("Resolution error: %s", e)
    if resolved_count > 0:
        await send_telegram(
            "<b>Resolution Check Complete</b>\n\n"
            "Resolved " + str(resolved_count) + " markets\n"
            "Type /status to see updated win rate!"
        )

async def send_weekly_analysis(conn):
    total_alerts = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log")
    resolved_alerts = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
    profitable = sum(1 for a in resolved_alerts if a["profitable"])
    win_rate = round(profitable / len(resolved_alerts) * 100) if resolved_alerts else 0
    crypto_resolved = [a for a in resolved_alerts if "Crypto" in a["reason"]]
    sports_resolved = [a for a in resolved_alerts if "Sports" in a["reason"]]
    politics_resolved = [a for a in resolved_alerts if "Politics" in a["reason"]]
    crypto_win = round(sum(1 for a in crypto_resolved if a["profitable"]) / len(crypto_resolved) * 100) if crypto_resolved else 0
    sports_win = round(sum(1 for a in sports_resolved if a["profitable"]) / len(sports_resolved) * 100) if sports_resolved else 0
    politics_win = round(sum(1 for a in politics_resolved if a["profitable"]) / len(politics_resolved) * 100) if politics_resolved else 0
    fear_resolved = [a for a in resolved_alerts if a.get("fear_greed_regime") in ["Extreme Fear", "Fear"]]
    greed_resolved = [a for a in resolved_alerts if a.get("fear_greed_regime") in ["Greed", "Extreme Greed"]]
    fear_win = round(sum(1 for a in fear_resolved if a["profitable"]) / len(fear_resolved) * 100) if fear_resolved else 0
    greed_win = round(sum(1 for a in greed_resolved if a["profitable"]) / len(greed_resolved) * 100) if greed_resolved else 0
    agreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating = 'agree' AND outcome IS NOT NULL")
    agreed_win = round(sum(1 for a in agreed if a["profitable"]) / len(agreed) * 100) if agreed else 0
    disagreed = await conn.fetch("SELECT * FROM alerts WHERE user_rating = 'disagree' AND outcome IS NOT NULL")
    disagreed_win = round(sum(1 for a in disagreed if a["profitable"]) / len(disagreed) * 100) if disagreed else 0
    msg = (
        "<b>Weekly Self-Analysis Report</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "Overall Performance:\n"
        + "Total alerts: " + str(total_alerts) + "\n"
        + "Opportunities logged: " + str(total_opps) + "\n"
        + "Resolved: " + str(len(resolved_alerts)) + "\n"
        + "Win rate: " + str(win_rate) + "%\n\n"
        + "By Category:\n"
        + "Crypto: " + str(crypto_win) + "% (" + str(len(crypto_resolved)) + " resolved)\n"
        + "Sports: " + str(sports_win) + "% (" + str(len(sports_resolved)) + " resolved)\n"
        + "Politics: " + str(politics_win) + "% (" + str(len(politics_resolved)) + " resolved)\n\n"
        + "By Sentiment:\n"
        + "During Fear: " + str(fear_win) + "% (" + str(len(fear_resolved)) + " resolved)\n"
        + "During Greed: " + str(greed_win) + "% (" + str(len(greed_resolved)) + " resolved)\n\n"
        + "Your Judgment:\n"
        + "When agreed: " + str(agreed_win) + "% win rate\n"
        + "When disagreed: " + str(disagreed_win) + "% win rate\n\n"
        + "Type /status anytime for live stats!"
    )
    await send_telegram(msg)
    log.info("Weekly analysis sent")

async def send_daily_summary(conn):
    alerts_today = await get_daily_stats(conn)
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    total_opps = await conn.fetchval("SELECT COUNT(*) FROM opportunities_log")
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
    profitable_count = sum(1 for a in resolved if a["profitable"])
    crypto_count = sum(1 for a in alerts_today if "Crypto" in a["reason"])
    sports_count = sum(1 for a in alerts_today if "Sports" in a["reason"])
    politics_count = sum(1 for a in alerts_today if "Politics" in a["reason"])
    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    agreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating = 'agree'") or 0
    disagreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating = 'disagree'") or 0
    win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0
    state = await get_risk_state(conn)
    limits = get_dynamic_limits(state)
    sentiment_today = await conn.fetchrow("""
        SELECT score, regime, trend FROM sentiment_history
        ORDER BY recorded_at DESC LIMIT 1
    """)
    sentiment_line = ""
    if sentiment_today:
        sentiment_line = (
            "Market Sentiment:\n"
            + "Fear and Greed: " + str(sentiment_today["score"])
            + " (" + str(sentiment_today["regime"]) + ")\n\n"
        )
    msg = (
        "<b>Daily Summary Report</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Avg score: " + str(round(avg_score)) + "/100\n\n"
        + "By Category:\n"
        + "Crypto: " + str(crypto_count) + "\n"
        + "Sports: " + str(sports_count) + "\n"
        + "Politics: " + str(politics_count) + "\n\n"
        + sentiment_line
        + "Your Ratings:\n"
        + "Agreed: " + str(agreed) + "\n"
        + "Disagreed: " + str(disagreed) + "\n\n"
        + "Resolved Markets:\n"
        + "Total resolved: " + str(len(resolved)) + "\n"
        + "Profitable: " + str(profitable_count) + "\n"
        + "Win rate: " + str(win_rate) + "%\n\n"
        + "Dynamic Risk:\n"
        + "Max trade size: $" + str(limits["max_trade_size"]) + "\n"
        + "Win streak: " + str(state["win_streak"]) + "\n"
        + "Loss streak: " + str(state["loss_streak"]) + "\n\n"
        + "Database:\n"
        + "Total alerts: " + str(total_count) + "\n"
        + "Opportunities logged: " + str(total_opps) + "\n\n"
        + "Type /status anytime for live stats!\n"
        + "Keep observing before trading!"
    )
    await send_telegram(msg)
    log.info("Daily summary sent")

async def send_heartbeat(conn):
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    alerts_today = await get_daily_stats(conn)
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
    profitable_count = sum(1 for a in resolved if a["profitable"])
    win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0
    state = await get_risk_state(conn)
    limits = get_dynamic_limits(state)
    msg = (
        "<b>Bot Heartbeat</b>\n"
        + now().strftime("%B %d, %Y %H:%M UTC") + "\n\n"
        + "Status: Running normally\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Total in database: " + str(total_count) + "\n"
        + "Win rate: " + str(win_rate) + "%\n"
        + "Max trade size: $" + str(limits["max_trade_size"]) + "\n\n"
        + "Type /status anytime for full stats!\n"
        + "All systems operational!"
    )
    await send_telegram(msg)
    log.info("Heartbeat sent")

async def fetch_all_markets():
    all_markets = []
    offset = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        while offset < CONFIG["max_pages"] * CONFIG["markets_per_page"]:
            url = (
                "https://gamma-api.polymarket.com/markets"
                "?active=true&closed=false"
                "&limit=" + str(CONFIG["markets_per_page"])
                + "&offset=" + str(offset)
                + "&order=volume24hr&ascending=false"
            )
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        markets = await resp.json()
                        if not markets:
                            break
                        all_markets.extend(markets)
                        page = offset // CONFIG["markets_per_page"] + 1
                        log.info("Page %d: %d markets (total: %d)", page, len(markets), len(all_markets))
                        if len(markets) < CONFIG["markets_per_page"]:
                            break
                        offset += CONFIG["markets_per_page"]
                        await asyncio.sleep(0.5)
                    else:
                        log.error("API error: %d", resp.status)
                        break
            except Exception as e:
                log.error("Error fetching markets: %s", e)
                break
    return all_markets

def is_market_active(market):
    try:
        if market.get("closed", True):
            return False
        if not market.get("active", False):
            return False
        outcomes = market.get("outcomePrices", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if not outcomes:
            return False
        yes_price = float(outcomes[0])
        if yes_price >= 0.99 or yes_price <= 0.01:
            return False
        volume = float(market.get("volumeNum", 0) or 0)
        if volume < 100:
            return False
        return True
    except:
        return False

def get_market_age_hours(market):
    try:
        created = market.get("createdAt", "")
        if created:
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00")).replace(tzinfo=None)
            return round((now() - created_dt).total_seconds() / 3600, 1)
    except:
        pass
    return None

def score_opportunity(market, fear_greed=None):
    try:
        question = market.get("question", "").lower()
        outcomes = market.get("outcomePrices", "[]")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        yes_price = float(outcomes[0])
        volume = float(market.get("volumeNum", 0) or 0)
        volume_24h = float(market.get("volume24hr", 0) or 0)
        end_date = market.get("endDateIso", "")
    except:
        return 0, "Could not parse"

    score = 0
    reasons = []

    if yes_price < 0.10:
        score += 25
        reasons.append("YES cheap at " + str(round(yes_price * 100)) + "%")
    elif yes_price > 0.90:
        score += 25
        reasons.append("YES expensive at " + str(round(yes_price * 100)) + "%")

    is_crypto = any(word in question for word in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"])
    is_sports = any(word in question for word in ["nba", "nfl", "mlb", "nhl", "ufc", "premier league",
                                                   "champions league", "world cup", "super bowl",
                                                   "playoffs", "championship"])
    is_politics = any(word in question for word in ["president", "election", "senate", "congress",
                                                     "governor", "parliament", "vote", "referendum"])

    if is_crypto:
        score += 20
        reasons.append("Crypto market")
        if fear_greed and fear_greed.get("success"):
            bonus = fear_greed.get("sentiment_bonus", 0)
            if bonus != 0:
                score += bonus
                reasons.append("Sentiment bonus: " + str(bonus))

    if is_sports:
        score += 15
        reasons.append("Sports market")

    if is_politics:
        score += 15
        reasons.append("Politics market")

    if volume_24h > 50000:
        score += 25
        reasons.append("Very high 24h volume $" + str(round(volume_24h)))
    elif volume_24h > 10000:
        score += 15
        reasons.append("High 24h volume $" + str(round(volume_24h)))
    elif volume_24h > 1000:
        score += 5
        reasons.append("Medium 24h volume $" + str(round(volume_24h)))

    market_age = get_market_age_hours(market)
    if market_age and market_age < 24:
        score += 10
        reasons.append("New market (" + str(round(market_age)) + "h old)")

    if end_date:
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00")).replace(tzinfo=None)
            hours_left = (end - now()).total_seconds() / 3600
            if 0 < hours_left < 24:
                score += 20
                reasons.append("Closing in " + str(round(hours_left)) + "h")
            elif 0 < hours_left < 72:
                score += 10
                reasons.append("Closing in " + str(round(hours_left)) + "h")
        except:
            pass

    return score, " | ".join(reasons) if reasons else "General market"

async def scan_markets():
    log.info("Polymarket Bot v14 Starting...")
    log.info("Status command /status ON")
    log.info("All v13 features included")
    log.info("=" * 50)

    conn = await asyncpg.connect(DATABASE_URL)
    await init_db(conn)

    await send_telegram(
        "<b>Polymarket Bot v14 Started!</b>\n\n"
        "NEW: Type /status anytime for live stats\n"
        "Fear and Greed Index tracking\n"
        "Sentiment-adjusted scoring\n"
        "All 40+ opportunities logged\n"
        "Resolution check every 2 hours\n"
        "Weekly self-analysis reports\n"
        "Market age tracking\n\n"
        "Type /status now to test it!"
    )

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

            updates = await get_updates(last_update_id + 1)
            if updates:
                last_update_id = await process_feedback(conn, updates, last_update_id)

            if fear_greed_cache["cached_at"] is None or (now() - fear_greed_cache["cached_at"]).total_seconds() > 3600:
                fear_greed = await get_fear_greed()
                fear_greed_cache["data"] = fear_greed
                fear_greed_cache["cached_at"] = now()
                if fear_greed.get("success"):
                    await log_sentiment(conn, fear_greed)
                    log.info("Fear and Greed: %d (%s) %s", fear_greed["score"], fear_greed["regime"], fear_greed.get("trend", ""))
            else:
                fear_greed = fear_greed_cache["data"]

            if current_time.hour == CONFIG["heartbeat_hour_utc"] and current_time.date() != last_heartbeat_date:
                await send_heartbeat(conn)
                last_heartbeat_date = current_time.date()

            if current_time.hour == CONFIG["summary_hour_utc"] and current_time.date() != last_summary_date:
                await send_daily_summary(conn)
                last_summary_date = current_time.date()

            if current_time.weekday() == CONFIG["weekly_analysis_day"] and current_time.date() != last_weekly_date:
                await send_weekly_analysis(conn)
                last_weekly_date = current_time.date()

            resolution_key = str(current_time.date()) + "_" + str(current_time.hour)
            if current_time.hour in CONFIG["resolution_check_hours"] and resolution_key not in last_resolution_hours:
                log.info("Running resolution check...")
                await check_resolutions(conn)
                last_resolution_hours.add(resolution_key)
                if len(last_resolution_hours) > 100:
                    last_resolution_hours = set(list(last_resolution_hours)[-50:])

            log.info("Scanning Polymarket markets...")
            markets = await fetch_all_markets()

            if not markets:
                consecutive_errors += 1
                log.warning("No markets returned (error streak: %d)", consecutive_errors)
                if consecutive_errors >= 10:
                    await send_telegram(
                        "<b>Bot Error Alert</b>\n\n"
                        "Could not reach Polymarket API for 5+ minutes\n"
                        "Bot is still running and retrying"
                    )
                    consecutive_errors = 0
            else:
                consecutive_errors = 0
                active_markets = [m for m in markets if is_market_active(m)]
                log.info("%d total -> %d active after filtering", len(markets), len(active_markets))

                state = await get_risk_state(conn)
                limits = get_dynamic_limits(state)

                opportunities = []
                for market in active_markets:
                    score, reason = score_opportunity(market, fear_greed)
                    market_age = get_market_age_hours(market)
                    if score >= CONFIG["log_opportunity_threshold"]:
                        question = market.get("question", "Unknown")
                        outcomes = market.get("outcomePrices", "[0.5]")
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        yes_price = float(outcomes[0]) if outcomes else 0.5
                        market_id = str(market.get("id", question[:50]))
                        volume = float(market.get("volumeNum", 0) or 0)

                        opp = {
                            "id": market_id,
                            "question": question,
                            "score": score,
                            "reason": reason,
                            "yes_price": yes_price,
                            "volume": volume,
                            "age": market_age
                        }

                        if market_id not in logged_opportunities:
                            await log_opportunity(conn, opp, fear_greed, market_age)
                            logged_opportunities.add(market_id)

                        if score >= CONFIG["min_score_for_alert"]:
                            opportunities.append(opp)

                        if market_id in alerted_markets:
                            await update_price_history(conn, market_id, yes_price)

                opportunities.sort(key=lambda x: x["score"], reverse=True)

                if opportunities:
                    log.info("Found %d alertable opportunities", len(opportunities))
                    for opp in opportunities[:5]:
                        log.info("Score:%d | %s", opp["score"], opp["question"][:60])
                        if opp["id"] not in alerted_markets:
                            alert_id = await log_alert(conn, opp, fear_greed, opp.get("age"))
                            alerted_markets.add(opp["id"])

                            research = await build_research_summary(
                                opp["question"], opp["yes_price"], opp["reason"], fear_greed
                            )

                            msg = (
                                "<b>Opportunity Found!</b>\n\n"
                                "<b>Market:</b> " + opp["question"][:100] + "\n"
                                "<b>YES Price:</b> " + str(round(opp["yes_price"] * 100)) + "%\n"
                                "<b>Score:</b> " + str(opp["score"]) + "/100\n"
                                "<b>Why:</b> " + opp["reason"] + "\n"
                                "<b>Volume:</b> $" + str(round(opp["volume"])) + "\n"
                                "<b>Max Trade Size:</b> $" + str(limits["max_trade_size"]) + "\n"
                            )

                            if opp.get("age") and opp["age"] < 24:
                                msg += "<b>New Market:</b> Only " + str(round(opp["age"])) + "h old\n"

                            if research:
                                msg += "\n" + research + "\n"

                            msg += "\nDo you agree this looks interesting?"

                            reply_markup = {
                                "inline_keyboard": [[
                                    {"text": "👍 Agree", "callback_data": "agree_" + str(alert_id)},
                                    {"text": "👎 Disagree", "callback_data": "disagree_" + str(alert_id)}
                                ]]
                            }
                            await send_telegram(msg, reply_markup=reply_markup)
                else:
                    log.info("No new alertable opportunities this scan")

        except Exception as e:
            log.error("Unexpected error in main loop: %s", e)
            await send_telegram(
                "<b>Bot Error Alert</b>\n\n"
                "Unexpected error: " + str(e)[:200] + "\n"
                "Bot is attempting to recover automatically"
            )

        log.info("Next scan in %d seconds", CONFIG["check_interval_seconds"])
        log.info("-" * 50)
        await asyncio.sleep(CONFIG["check_interval_seconds"])

if __name__ == "__main__":
    asyncio.run(scan_markets())