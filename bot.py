import asyncio
import aiohttp
import logging
import os
import json
from datetime import datetime, timezone

CONFIG = {
    "check_interval_seconds": 30,
    "min_score_for_alert": 70,
    "max_daily_loss": 20,
    "max_trade_size": 5,
    "max_open_positions": 10,
    "markets_per_page": 100,
    "max_pages": 20,
    "summary_hour_utc": 13,
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)

DB_FILE = "alerts_database.json"

def load_database():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"alerts": [], "price_history": {}, "daily_stats": []}

def save_database(db):
    try:
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.error("Failed to save database: %s", e)

def log_alert(db, opportunity):
    alert = {
        "id": opportunity["id"],
        "question": opportunity["question"],
        "yes_price_at_alert": opportunity["yes_price"],
        "score": opportunity["score"],
        "reason": opportunity["reason"],
        "volume_at_alert": opportunity["volume"],
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "outcome": None,
        "profitable": None,
        "price_history": []
    }
    db["alerts"].append(alert)
    save_database(db)
    log.info("Alert logged to database (total: %d)", len(db["alerts"]))

def update_price_history(db, market_id, yes_price):
    if market_id not in db["price_history"]:
        db["price_history"][market_id] = []
    db["price_history"][market_id].append({
        "price": yes_price,
        "time": datetime.now(timezone.utc).isoformat()
    })
    if len(db["price_history"][market_id]) > 100:
        db["price_history"][market_id] = db["price_history"][market_id][-100:]

def get_daily_stats(db):
    now = datetime.now(timezone.utc)
    alerts_today = []
    for alert in db["alerts"]:
        try:
            alerted_at = datetime.fromisoformat(alert["alerted_at"])
            hours_ago = (now - alerted_at).total_seconds() / 3600
            if hours_ago <= 24:
                alerts_today.append(alert)
        except:
            pass
    return alerts_today

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
                    log.info("Telegram alert sent!")
                else:
                    log.error("Telegram error: %d", resp.status)
    except Exception as e:
        log.error("Telegram failed: %s", e)

async def send_daily_summary(db):
    alerts_today = get_daily_stats(db)
    total_alerts = len(db["alerts"])
    crypto_count = sum(1 for a in alerts_today if "Crypto" in a.get("reason", ""))
    sports_count = sum(1 for a in alerts_today if "Sports" in a.get("reason", ""))
    politics_count = sum(1 for a in alerts_today if "Politics" in a.get("reason", ""))
    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    msg = (
        "<b>Daily Summary Report</b>\n"
        + datetime.now(timezone.utc).strftime("%B %d, %Y") + "\n\n"
        + "Last 24 Hours:\n"
        + "Alerts fired: " + str(len(alerts_today)) + "\n"
        + "Avg score: " + str(round(avg_score)) + "/100\n\n"
        + "By Category:\n"
        + "Crypto: " + str(crypto_count) + "\n"
        + "Sports: " + str(sports_count) + "\n"
        + "Politics: " + str(politics_count) + "\n\n"
        + "All Time:\n"
        + "Total alerts logged: " + str(total_alerts) + "\n\n"
        + "Keep observing before trading!"
    )
    await send_telegram(msg)
    log.info("Daily summary sent")

async def fetch_all_markets():
    all_markets = []
    next_cursor = None
    page = 0
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Origin": "https://polymarket.com",
        "Referer": "https://polymarket.com/"
    }
    async with aiohttp.ClientSession() as session:
        while page < CONFIG["max_pages"]:
            url = "https://clob.polymarket.com/markets?active=true&closed=false&limit=" + str(CONFIG["markets_per_page"])
            if next_cursor:
                url = url + "&next_cursor=" + next_cursor
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        markets = data.get("data", [])
                        all_markets.extend(markets)
                        next_cursor = data.get("next_cursor")
                        page += 1
                        log.info("Page %d: fetched %d markets (total: %d)", page, len(markets), len(all_markets))
                        if not next_cursor or not markets:
                            break
                        await asyncio.sleep(0.5)
                    else:
                        log.error("API error: %d", resp.status)
                        break
            except Exception as e:
                log.error("Error fetching page %d: %s", page, e)
                break
    return all_markets

def is_market_active(market):
    try:
        tokens = market.get("tokens", [])
        if not tokens:
            return False
        yes_price = float(tokens[0].get("price", 0.5))
        volume = float(market.get("volume", 0))
        if yes_price >= 0.99 or yes_price <= 0.01:
            return False
        if volume < 100:
            return False
        return True
    except:
        return False

def score_opportunity(market):
    try:
        question = market.get("question", "").lower()
        tokens = market.get("tokens", [])
        yes_price = float(tokens[0].get("price", 0.5))
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
                                         "playoffs", "championship game"]):
        score += 15
        reasons.append("Sports market")

    if any(word in question for word in ["president", "election", "senate", "congress",
                                         "governor", "parliament", "vote", "referendum"]):
        score += 15
        reasons.append("Politics market")

    volume = float(market.get("volume", 0))
    if volume > 100000:
        score += 25
        reasons.append("Very high volume $" + str(round(volume)))
    elif volume > 10000:
        score += 15
        reasons.append("High volume $" + str(round(volume)))
    elif volume > 1000:
        score += 5
        reasons.append("Medium volume $" + str(round(volume)))

    return score, " | ".join(reasons) if reasons else "General market"

async def scan_markets():
    log.info("Polymarket Bot v5 Starting...")
    log.info("=" * 50)

    await send_telegram(
        "<b>Polymarket Bot v5 Started!</b>\n\n"
        "Scanning ALL markets\n"
        "Building performance database\n"
        "Daily summary reports at 9am EST\n"
        "Price history tracking\n\n"
        "Data collection has begun!"
    )

    db = load_database()
    alerted_markets = set(alert["id"] for alert in db["alerts"])
    last_summary_date = None

    while True:
        now = datetime.now(timezone.utc)

        if now.hour == CONFIG["summary_hour_utc"] and now.date() != last_summary_date:
            await send_daily_summary(db)
            last_summary_date = now.date()

        log.info("Scanning ALL Polymarket markets...")
        markets = await fetch_all_markets()

        if not markets:
            log.warning("No markets returned - will retry")
        else:
            active_markets = [m for m in markets if is_market_active(m)]
            log.info("%d total -> %d active after filtering", len(markets), len(active_markets))

            opportunities = []
            for market in active_markets:
                score, reason = score_opportunity(market)
                if score >= 40:
                    question = market.get("question", "Unknown")
                    tokens = market.get("tokens", [])
                    yes_price = float(tokens[0].get("price", 0)) if tokens else 0
                    market_id = market.get("condition_id", question[:50])
                    volume = float(market.get("volume", 0))
                    opportunities.append({
                        "id": market_id,
                        "question": question,
                        "score": score,
                        "reason": reason,
                        "yes_price": yes_price,
                        "volume": volume
                    })
                    if market_id in alerted_markets:
                        update_price_history(db, market_id, yes_price)

            opportunities.sort(key=lambda x: x["score"], reverse=True)

            if opportunities:
                log.info("Found %d opportunities", len(opportunities))
                for opp in opportunities[:5]:
                    log.info("Score:%d | %s", opp["score"], opp["question"][:60])
                    log.info("YES: %d%% | %s", round(opp["yes_price"] * 100), opp["reason"])
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
                        log_alert(db, opp)
                        alerted_markets.add(opp["id"])
            else:
                log.info("No strong opportunities this scan")

        save_database(db)
        log.info("Next scan in %d seconds", CONFIG["check_interval_seconds"])
        log.info("-" * 50)
        await asyncio.sleep(CONFIG["check_interval_seconds"])

if __name__ == "__main__":
    asyncio.run(scan_markets())