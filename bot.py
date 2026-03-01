import asyncio
import aiohttp
import logging
import os
import json
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION
# ============================================================
CONFIG = {
    "check_interval_seconds": 30,
    "min_score_for_alert": 70,
    "max_daily_loss": 20,
    "max_trade_size": 5,
    "max_open_positions": 10,
    "markets_per_page": 100,
    "max_pages": 20,
    "summary_hour_utc": 13,  # 9am EST = 1pm UTC
}

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ============================================================
# DATABASE (JSON file stored on Railway)
# ============================================================
DB_FILE = "alerts_database.json"

def load_database():
    """Load the alerts database from file"""
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"alerts": [], "price_history": {}, "daily_stats": []}

def save_database(db):
    """Save the alerts database to file"""
    try:
        with open(DB_FILE, "w") as f:
            json.dump(db, f, indent=2)
    except Exception as e:
        log.error(f"❌ Failed to save database: {e}")

def log_alert(db, opportunity):
    """Log a new alert to the database"""
    alert = {
        "id": opportunity["id"],
        "question": opportunity["question"],
        "yes_price_at_alert": opportunity["yes_price"],
        "score": opportunity["score"],
        "reason": opportunity["reason"],
        "volume_at_alert": opportunity["volume"],
        "alerted_at": datetime.now(timezone.utc).isoformat(),
        "outcome": None,  # To be filled when market resolves
        "profitable": None,  # To be filled when market resolves
        "price_history": []
    }
    db["alerts"].append(alert)
    save_database(db)
    log.info(f"💾 Alert logged to database (total: {len(db['alerts'])})")

def update_price_history(db, market_id, yes_price):
    """Track price movement for alerted markets"""
    if market_id not in db["price_history"]:
        db["price_history"][market_id] = []
    
    db["price_history"][market_id].append({
        "price": yes_price,
        "time": datetime.now(timezone.utc).isoformat()
    })
    
    # Keep only last 100 price points per market
    if len(db["price_history"][market_id]) > 100:
        db["price_history"][market_id] = db["price_history"][market_id][-100:]

def get_daily_stats(db):
    """Get stats for the last 24 hours"""
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

# ============================================================
# TELEGRAM
# ============================================================
async def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("⚠️ Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    log.info("📲 Telegram alert sent!")
                else:
                    log.error(f"❌ Telegram error: {resp.status}")
    except Exception as e:
        log.error(f"❌ Telegram failed: {e}")

async def send_daily_summary(db):
    """Send daily summary report to Telegram"""
    alerts_today = get_daily_stats(db)
    total_alerts = len(db["alerts"])
    
    # Count by category
    crypto_count = sum(1 for a in alerts_today if "Crypto" in a.get("reason", ""))
    sports_count = sum(1 for a in alerts_today if "Sports" in a.get("reason", ""))
    politics_count = sum(1 for a in alerts_today if "Politics" in a.get("reason", ""))
    
    # Average score today
    avg_score = sum(a["score"] for a in alerts_today) / len(alerts_today) if alerts_today else 0
    
    msg = (
        f"📊 <b>Daily Summary Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%B %d, %Y')}\n\n"
        f"<b>Last 24 Hours:</b>\n"
        f"🚨 Alerts fired: {len(alerts_today)}\n"
        f"📈 Avg score: {avg_score:.0f}/100\n\n"
        f"<b>By Category:</b>\n"
        f"₿ Crypto: {crypto_count}\n"
        f"🏆 Sports: {sports_count}\n"
        f"🏛 Politics: {politics_count}\n\n"
        f"<b>All Time:</b>\n"
        f"💾 Total alerts logged: {total_alerts}\n\n"
        f"<i>Keep observing before trading!</i>"
    )
    await send_telegram(msg)
    log.info("📊 Daily summary sent")

# ============================================================
# MARKET FETCHING WITH PAGINATION
# ============================================================
async def fetch_all_markets():
    """Fetch ALL markets using pagination"""
    all_markets = []
    next_cursor = None
    page = 0
    
    async with aiohttp.ClientSession() as session:
        while page < CONFIG["max_pages"]:
            url = f"https://clob.polymarket.com/markets?active=true&closed=false&limit={CONFIG['markets_per_page']}"
            if next_cursor:
                url += f"&next_cursor={next_cursor}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
                "Origin": "https://polymarket.com",
                "Referer": "https://polymarket.com/"
            }
            
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        markets = data.get("data", [])
                        all_markets.extend(markets)
                        
                        next_cursor = data.get("next_cursor")
                        page += 1
                        
                        log.info(f"📄 Page {page}: fetched {len(markets)} markets (total: {len(all_markets)})")
                        
                        if not next_cursor or not markets:
                            break
                        
                        await asyncio.sleep(0.5)  # Be polite to the API
                    else:
                        log.error(f"❌ API error: {resp.status}")
                        break
            except Exception as e:
                log.error(f"❌ Error fetching page {page}: {e}")
                break
    
    return all_markets

# ============================================================
# MARKET FILTERING
# ============================================================
def is_market_active(market):
    """Filter out resolved/closed/low quality markets"""
    tokens = market.get("tokens", [])
    if not tokens:
        return False
    
    try:
        yes_price = float(tokens[0].get("price", 0.5))
        no_price = float(tokens[1].get("price", 0.5)) if len(tokens) > 1 else 0.5
    except:
        return False

    if yes_price >= 0.99 or yes_price <= 0.01:
        return False
    
    if abs((yes_price + no_price) - 1.0) > 0.05:
        return False

    volume = float(market.get("volume", 0))
    if volume < 100:
        return False

    return True

# ============================================================
# SCORING
# ============================================================
def score_opportunity(market):
    question = market.get("question", "").lower()
    tokens = market.get("tokens", [])

    try:
        yes_price = float(tokens[0].get("price", 0.5))
    except:
        return 0, "Could not parse prices"

    score = 0
    reasons = []

    if yes_price < 0.10:
        score += 25
        reasons.append(f"YES cheap at {yes_price:.0%}")
    elif yes_price > 0.90:
        score += 25
        reasons.append(f"YES expensive at {yes_price:.0%}")

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
        reasons.append(f"Very high volume ${volume:,.0f}")
    elif volume > 10000:
        score += 15
        reasons.append(f"High volume ${volume:,.0f}")
    elif volume > 1000:
        score += 5
        reasons.append(f"Medium volume ${volume:,.0f}")

    return score, " | ".join(reasons) if reasons else "General market"

# ============================================================
# MAIN LOOP
# ============================================================
async def scan_markets():
    log.info("🤖 Polymarket Bot v3 Starting...")
    log.info("📄 Pagination: ON")
    log.info("💾 Database tracking: ON")
    log.info("📊 Daily summaries: ON")
    log.info("=" * 50)

    await send_telegram(
        "🤖 <b>Polymarket Bot v3 Started!</b>\n\n"
        "✅ Scanning ALL markets (pagination)\n"
        "✅ Building performance database\n"
        "✅ Daily summary reports at 9am EST\n"
        "✅ Price history tracking\n\n"
        "<i>Data collection has begun!</i>"
    )

    db = load_database()
    alerted_markets = set(alert["id"] for alert in db["alerts"])
    last_summary_date = None

    while True:
        now = datetime.now(timezone.utc)

        # Send daily summary at 9am EST
        if now.hour == CONFIG["summary_hour_utc"] and now.date() != last_summary_date:
            await send_daily_summary(db)
            last_summary_date = now.date()

        log.info("🔍 Scanning ALL Polymarket markets...")
        markets = await fetch_all_markets()

        if not markets:
            log.warning("⚠️ No markets returned - will retry")
        else:
            active_markets = [m for m in markets if is_market_active(m)]
            log.info(f"📊 {len(markets)} total → {len(active_markets)} active after filtering")

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

                    # Track price history for already-alerted markets
                    if market_id in alerted_markets:
                        update_price_history(db, market_id, yes_price)

            opportunities.sort(key=lambda x: x["score"], reverse=True)

            if opportunities:
                log.info(f"✅ Found {len(opportunities)} opportunities")
                for opp in opportunities[:5]:
                    log.info(f"  Score:{opp['score']} | {opp['question'][:60]}...")
                    log.info(f"  YES: {opp['yes_price']:.0%} | {opp['reason']}")

                    if opp["score"] >= CONFIG["min_score_for_alert"] and opp["id"] not in alerted_markets:
                        msg = (
                            f"🚨 <b>Opportunity Found!</b>\n\n"
                            f"📋 <b>Market:</b> {opp['question'][:100]}\n"
                            f"💰 <b>YES Price:</b> {opp['yes_price']:.0%}\n"
                            f"📊 <b>Score:</b> {opp['score']}/100\n"
                            f"🔍 <b>Why:</b> {opp['reason']}\n"
                            f"💵 <b>Volume:</b> ${opp['volume']:,.0f}\n\n"
                            f"⚠️ <i>Research before trading!</i>"
                        )
                        await send_telegram(msg)
                        log_alert(db, opp)
                        alerted_markets.add(opp["id"])
            else:
                log.info("No strong opportunities this scan")

        save_database(db)
        log.info(f"⏰ Next scan in {CONFIG['check_interval_seconds']} seconds")
        log.info("-" * 50)
        await asyncio.sleep(CONFIG["check_interval_seconds"])

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    asyncio.run(scan_markets())
