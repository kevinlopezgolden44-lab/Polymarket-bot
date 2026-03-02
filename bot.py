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
    "markets_per_page": 100,
    "max_pages": 50,
    "summary_hour_utc": 13,
    "heartbeat_hour_utc": 12,
    "resolution_check_hour_utc": 6,
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

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
            user_rating TEXT
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
        ALTER TABLE alerts ADD COLUMN IF NOT EXISTS user_rating TEXT
    """)
    log.info("Database tables ready")

async def log_alert(conn, opportunity, message_id=None):
    alert_id = await conn.fetchval("""
        INSERT INTO alerts (market_id, question, yes_price, score, reason, volume, alerted_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
    """,
        opportunity["id"],
        opportunity["question"],
        opportunity["yes_price"],
        opportunity["score"],
        opportunity["reason"],
        opportunity["volume"],
        now()
    )
    count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    log.info("Alert logged to database (total: %d)", count)
    return alert_id

async def update_price_history(conn, market_id, yes_price):
    await conn.execute("""
        INSERT INTO price_history (market_id, yes_price, recorded_at)
        VALUES ($1, $2, $3)
    """, market_id, yes_price, now())

async def get_daily_stats(conn):
    rows = await conn.fetch("""
        SELECT * FROM alerts
        WHERE alerted_at > NOW() - INTERVAL '24 hours'
    """)
    return rows

async def get_alerted_markets(conn):
    rows = await conn.fetch("SELECT DISTINCT market_id FROM alerts")
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

async def process_feedback(conn, updates, last_update_id):
    new_last_id = last_update_id
    for update in updates:
        update_id = update.get("update_id", 0)
        if update_id <= last_update_id:
            continue
        new_last_id = max(new_last_id, update_id)
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
                log.info("User rated alert %s as %s", alert_id, rating)
    return new_last_id

async def check_resolutions(conn):
    unresolved = await conn.fetch("""
        SELECT * FROM alerts
        WHERE outcome IS NULL
        AND alerted_at < NOW() - INTERVAL '1 hour'
    """)
    if not unresolved:
        log.info("No unresolved alerts to check")
        return

    log.info("Checking resolution for %d alerts", len(unresolved))
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json"
    }

    resolved_count = 0
    async with aiohttp.ClientSession() as session:
        for alert in unresolved:
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
                                    await conn.execute("""
                                        UPDATE alerts
                                        SET outcome = $1, profitable = $2
                                        WHERE id = $3
                                    """, outcome, profitable, alert["id"])
                                    resolved_count += 1
                                    log.info("Resolved alert %d: %s -> %s (profitable: %s)",
                                             alert["id"], alert["question"][:40], outcome, profitable)
                await asyncio.sleep(0.3)
            except Exception as e:
                log.error("Error checking resolution for alert %d: %s", alert["id"], e)

    log.info("Resolved %d alerts", resolved_count)

async def send_daily_summary(conn):
    alerts_today = await get_daily_stats(conn)
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    resolved = await conn.fetch("SELECT * FROM alerts WHERE outcome IS NOT NULL")
    profitable_count = sum(1 for a in resolved if a["profitable"])
    crypto_count = sum(1 for a in alerts_today if "Crypto" in a["reason"])
    sports_count = sum(1 for a in alerts_today if "Sports" in a["reason"])
    politics_count = sum(1 for a in alerts_today if "Politics" in a["reason"])
    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    agreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating = 'agree'")
    disagreed = await conn.fetchval("SELECT COUNT(*) FROM alerts WHERE user_rating = 'disagree'")
    win_rate = round(profitable_count / len(resolved) * 100) if resolved else 0
    msg = (
        "<b>Daily Summary Report</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Avg score: " + str(round(avg_score)) + "/100\n\n"
        + "By Category:\n"
        + "Crypto: " + str(crypto_count) + "\n"
        + "Sports: " + str(sports_count) + "\n"
        + "Politics: " + str(politics_count) + "\n\n"
        + "Your Ratings:\n"
        + "Agreed: " + str(agreed) + "\n"
        + "Disagreed: " + str(disagreed) + "\n\n"
        + "Resolved Markets:\n"
        + "Total resolved: " + str(len(resolved)) + "\n"
        + "Would have been profitable: " + str(profitable_count) + "\n"
        + "Win rate: " + str(win_rate) + "%\n\n"
        + "Total all time: " + str(total_count) + "\n\n"
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
    msg = (
        "<b>Bot Heartbeat</b>\n"
        + now().strftime("%B %d, %Y %H:%M UTC") + "\n\n"
        + "Status: Running normally\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Total in database: " + str(total_count) + "\n"
        + "Win rate so far: " + str(win_rate) + "%\n\n"
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

def score_opportunity(market):
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

    if any(word in question for word in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"]):
        score += 20
        reasons.append("Crypto market")

    if any(word in question for word in ["nba", "nfl", "mlb", "nhl", "ufc", "premier league",
                                         "champions league", "world cup", "super bowl",
                                         "playoffs", "championship"]):
        score += 15
        reasons.append("Sports market")

    if any(word in question for word in ["president", "election", "senate", "congress",
                                         "governor", "parliament", "vote", "referendum"]):
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
    log.info("Polymarket Bot v10 Starting...")
    log.info("PostgreSQL persistent database ON")
    log.info("Error alerting ON")
    log.info("Daily heartbeat ON")
    log.info("Resolution checker ON")
    log.info("Agree/Disagree feedback ON")
    log.info("=" * 50)

    conn = await asyncpg.connect(DATABASE_URL)
    await init_db(conn)

    await send_telegram(
        "<b>Polymarket Bot v10 Started!</b>\n\n"
        "Persistent PostgreSQL database\n"
        "Auto resolution checker\n"
        "Agree/Disagree feedback buttons\n"
        "Daily heartbeat at 8am EST\n"
        "Daily summary at 9am EST\n"
        "Resolution check at 6am EST\n\n"
        "All systems go!"
    )

    alerted_markets = await get_alerted_markets(conn)
    last_summary_date = None
    last_heartbeat_date = None
    last_resolution_date = None
    last_update_id = 0
    consecutive_errors = 0

    while True:
        try:
            current_time = now()

            updates = await get_updates(last_update_id + 1)
            if updates:
                last_update_id = await process_feedback(conn, updates, last_update_id)

            if current_time.hour == CONFIG["heartbeat_hour_utc"] and current_time.date() != last_heartbeat_date:
                await send_heartbeat(conn)
                last_heartbeat_date = current_time.date()

            if current_time.hour == CONFIG["summary_hour_utc"] and current_time.date() != last_summary_date:
                await send_daily_summary(conn)
                last_summary_date = current_time.date()

            if current_time.hour == CONFIG["resolution_check_hour_utc"] and current_time.date() != last_resolution_date:
                log.info("Running daily resolution check...")
                await check_resolutions(conn)
                last_resolution_date = current_time.date()

            log.info("Scanning Polymarket markets...")
            markets = await fetch_all_markets()

            if not markets:
                consecutive_errors += 1
                log.warning("No markets returned (error streak: %d)", consecutive_errors)
                if consecutive_errors >= 10:
                    await send_telegram(
                        "<b>Bot Error Alert</b>\n\n"
                        "Could not reach Polymarket API for 5+ minutes\n"
                        "Bot is still running and retrying\n"
                        "Please check Railway logs if this persists"
                    )
                    consecutive_errors = 0
            else:
                consecutive_errors = 0
                active_markets = [m for m in markets if is_market_active(m)]
                log.info("%d total -> %d active after filtering", len(markets), len(active_markets))

                opportunities = []
                for market in active_markets:
                    score, reason = score_opportunity(market)
                    if score >= 40:
                        question = market.get("question", "Unknown")
                        outcomes = market.get("outcomePrices", "[0.5]")
                        if isinstance(outcomes, str):
                            outcomes = json.loads(outcomes)
                        yes_price = float(outcomes[0]) if outcomes else 0.5
                        market_id = str(market.get("id", question[:50]))
                        volume = float(market.get("volumeNum", 0) or 0)

                        opportunities.append({
                            "id": market_id,
                            "question": question,
                            "score": score,
                            "reason": reason,
                            "yes_price": yes_price,
                            "volume": volume
                        })

                        if market_id in alerted_markets:
                            await update_price_history(conn, market_id, yes_price)

                opportunities.sort(key=lambda x: x["score"], reverse=True)

                if opportunities:
                    log.info("Found %d opportunities", len(opportunities))
                    for opp in opportunities[:5]:
                        log.info("Score:%d | %s", opp["score"], opp["question"][:60])
                        if opp["score"] >= CONFIG["min_score_for_alert"] and opp["id"] not in alerted_markets:
                            alert_id = await log_alert(conn, opp)
                            alerted_markets.add(opp["id"])
                            msg = (
                                "<b>Opportunity Found!</b>\n\n"
                                "<b>Market:</b> " + opp["question"][:100] + "\n"
                                "<b>YES Price:</b> " + str(round(opp["yes_price"] * 100)) + "%\n"
                                "<b>Score:</b> " + str(opp["score"]) + "/100\n"
                                "<b>Why:</b> " + opp["reason"] + "\n"
                                "<b>Volume:</b> $" + str(round(opp["volume"])) + "\n\n"
                                "Do you agree this looks interesting?"
                            )
                            reply_markup = {
                                "inline_keyboard": [[
                                    {"text": "👍 Agree", "callback_data": "agree_" + str(alert_id)},
                                    {"text": "👎 Disagree", "callback_data": "disagree_" + str(alert_id)}
                                ]]
                            }
                            await send_telegram(msg, reply_markup=reply_markup)
                else:
                    log.info("No strong opportunities this scan")

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