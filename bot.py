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
            profitable BOOLEAN
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
        CREATE TABLE IF NOT EXISTS bot_stats (
            id SERIAL PRIMARY KEY,
            stat_key TEXT UNIQUE NOT NULL,
            stat_value TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
    """)
    log.info("Database tables ready")

async def log_alert(conn, opportunity):
    await conn.execute("""
        INSERT INTO alerts (market_id, question, yes_price, score, reason, volume, alerted_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
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

async def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured")
        return
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    log.info("Telegram sent!")
                else:
                    log.error("Telegram error: %d", resp.status)
    except Exception as e:
        log.error("Telegram failed: %s", e)

async def send_daily_summary(conn):
    alerts_today = await get_daily_stats(conn)
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    crypto_count = sum(1 for a in alerts_today if "Crypto" in a["reason"])
    sports_count = sum(1 for a in alerts_today if "Sports" in a["reason"])
    politics_count = sum(1 for a in alerts_today if "Politics" in a["reason"])
    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    msg = (
        "<b>Daily Summary Report</b>\n"
        + now().strftime("%B %d, %Y") + "\n\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Avg score: " + str(round(avg_score)) + "/100\n\n"
        + "Crypto: " + str(crypto_count) + "\n"
        + "Sports: " + str(sports_count) + "\n"
        + "Politics: " + str(politics_count) + "\n\n"
        + "Total all time: " + str(total_count) + "\n\n"
        + "Keep observing before trading!"
    )
    await send_telegram(msg)
    log.info("Daily summary sent")

async def send_heartbeat(conn):
    total_count = await conn.fetchval("SELECT COUNT(*) FROM alerts")
    alerts_today = await get_daily_stats(conn)
    msg = (
        "<b>Bot Heartbeat</b>\n"
        + now().strftime("%B %d, %Y %H:%M UTC") + "\n\n"
        + "Status: Running normally\n"
        + "Alerts today: " + str(len(alerts_today)) + "\n"
        + "Total alerts in database: " + str(total_count) + "\n\n"
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
    log.info("Polymarket Bot v9 Starting...")
    log.info("PostgreSQL persistent database ON")
    log.info("Error alerting ON")
    log.info("Daily heartbeat ON")
    log.info("=" * 50)

    conn = await asyncpg.connect(DATABASE_URL)
    await init_db(conn)

    await send_telegram(
        "<b>Polymarket Bot v9 Started!</b>\n\n"
        "Persistent PostgreSQL database\n"
        "Error alerting enabled\n"
        "Daily heartbeat at 8am EST\n"
        "Daily summary at 9am EST\n\n"
        "Timezone bug fixed!"
    )

    alerted_markets = await get_alerted_markets(conn)
    last_summary_date = None
    last_heartbeat_date = None
    consecutive_errors = 0

    while True:
        try:
            current_time = now()

            if current_time.hour == CONFIG["heartbeat_hour_utc"] and current_time.date() != last_heartbeat_date:
                await send_heartbeat(conn)
                last_heartbeat_date = current_time.date()

            if current_time.hour == CONFIG["summary_hour_utc"] and current_time.date() != last_summary_date:
                await send_daily_summary(conn)
                last_summary_date = current_time.date()

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
                            msg = (
                                "<b>Opportunity Found!</b>\n\n"
                                "<b>Market:</b> " + opp["question"][:100] + "\n"
                                "<b>YES Price:</b> " + str(round(opp["yes_price"] * 100)) + "%\n"
                                "<b>Score:</b> " + str(opp["score"]) + "/100\n"
                                "<b>Why:</b> " + opp["reason"] + "\n"
                                "<b>Volume:</b> $" + str(round(opp["volume"])) + "\n\n"
                                "Research before trading!"
                            )
                            await send_telegram(msg)
                            await log_alert(conn, opp)
                            alerted_markets.add(opp["id"])
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