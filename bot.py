import asyncio
import aiohttp
import logging
import os

# ============================================================
# CONFIGURATION
# ============================================================
CONFIG = {
    "check_interval_seconds": 30,
    "min_score_for_alert": 70,
    "max_daily_loss": 20,
    "max_trade_size": 5,
    "max_open_positions": 10,
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
# TELEGRAM
# ============================================================
async def send_telegram(message):
    """Send alert to Telegram"""
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

# ============================================================
# MARKET MONITOR
# ============================================================
async def fetch_polymarket_markets():
    url = "https://clob.polymarket.com/markets?active=true&closed=false"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
    except Exception as e:
        log.error(f"❌ Error fetching markets: {e}")
    return []

def score_opportunity(market):
    question = market.get("question", "").lower()
    tokens = market.get("tokens", [])
    
    if len(tokens) < 2:
        return 0, "Not enough token data"

    try:
        yes_price = float(tokens[0].get("price", 0.5))
    except:
        return 0, "Could not parse prices"

    score = 0
    reasons = []

    if yes_price < 0.05:
        score += 30
        reasons.append(f"YES very cheap at {yes_price:.0%}")
    elif yes_price > 0.95:
        score += 30
        reasons.append(f"YES very expensive at {yes_price:.0%}")

    if any(word in question for word in ["btc", "bitcoin", "eth", "crypto"]):
        score += 20
        reasons.append("Crypto market")

    if any(word in question for word in ["win", "score", "game", "match", "championship"]):
        score += 15
        reasons.append("Sports market")

    if any(word in question for word in ["president", "election", "will", "policy"]):
        score += 15
        reasons.append("News/Politics market")

    volume = float(market.get("volume", 0))
    if volume > 10000:
        score += 20
        reasons.append(f"High volume ${volume:,.0f}")
    elif volume > 1000:
        score += 10
        reasons.append(f"Medium volume ${volume:,.0f}")

    return score, " | ".join(reasons) if reasons else "General market"

async def scan_markets():
    log.info("🤖 Polymarket Bot Starting...")
    log.info(f"⚙️  Alert threshold: score {CONFIG['min_score_for_alert']}+")
    log.info("=" * 50)

    await send_telegram("🤖 <b>Polymarket Bot Started!</b>\nScanning markets every 30 seconds...")

    alerted_markets = set()

    while True:
        log.info("🔍 Scanning Polymarket markets...")
        markets = await fetch_polymarket_markets()

        if not markets:
            log.warning("⚠️ No markets returned - will retry")
        else:
            log.info(f"📊 Found {len(markets)} active markets")
            opportunities = []

            for market in markets:
                score, reason = score_opportunity(market)
                if score >= 40:
                    question = market.get("question", "Unknown")
                    tokens = market.get("tokens", [])
                    yes_price = float(tokens[0].get("price", 0)) if tokens else 0
                    market_id = market.get("condition_id", question[:50])

                    opportunities.append({
                        "id": market_id,
                        "question": question,
                        "score": score,
                        "reason": reason,
                        "yes_price": yes_price,
                        "volume": market.get("volume", 0)
                    })

            opportunities.sort(key=lambda x: x["score"], reverse=True)

            if opportunities:
                log.info(f"✅ Found {len(opportunities)} opportunities")
                for opp in opportunities[:5]:
                    log.info(f"  Score:{opp['score']} | {opp['question'][:60]}...")
                    log.info(f"  YES: {opp['yes_price']:.0%} | {opp['reason']}")

                    # Send Telegram alert for high score NEW opportunities
                    if opp["score"] >= CONFIG["min_score_for_alert"] and opp["id"] not in alerted_markets:
                        msg = (
                            f"🚨 <b>Opportunity Found!</b>\n\n"
                            f"📋 <b>Market:</b> {opp['question'][:100]}\n"
                            f"💰 <b>YES Price:</b> {opp['yes_price']:.0%}\n"
                            f"📊 <b>Score:</b> {opp['score']}/100\n"
                            f"🔍 <b>Why:</b> {opp['reason']}\n\n"
                            f"⚠️ <i>Research before trading!</i>"
                        )
                        await send_telegram(msg)
                        alerted_markets.add(opp["id"])
            else:
                log.info("No strong opportunities this scan")

        log.info(f"⏰ Next scan in {CONFIG['check_interval_seconds']} seconds")
        log.info("-" * 50)
        await asyncio.sleep(CONFIG["check_interval_seconds"])

# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    asyncio.run(scan_markets())
