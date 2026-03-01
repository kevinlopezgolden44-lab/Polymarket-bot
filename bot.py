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

def is_market_active(market):
    """Filter out resolved/closed markets"""
    tokens = market.get("tokens", [])
    if not tokens:
        return False
    
    try:
        yes_price = float(tokens[0].get("price", 0.5))
        no_price = float(tokens[1].get("price", 0.5)) if len(tokens) > 1 else 0.5
    except:
        return False

    # Filter out resolved markets (price at exactly 0% or 100%)
    if yes_price >= 0.99 or yes_price <= 0.01:
        return False
    
    # Filter out markets where prices don't add up to ~100%
    if abs((yes_price + no_price) - 1.0) > 0.05:
        return False

    # Filter out zero volume markets
    volume = float(market.get("volume", 0))
    if volume < 100:
        return False

    return True

def score_opportunity(market):
    question = market.get("question", "").lower()
    tokens = market.get("tokens", [])

    try:
        yes_price = float(tokens[0].get("price", 0.5))
    except:
        return 0, "Could not parse prices"

    score = 0
    reasons = []

    # Price extremes (but not resolved - already filtered above)
    if yes_price < 0.10:
        score += 25
        reasons.append(f"YES cheap at {yes_price:.0%}")
    elif yes_price > 0.90:
        score += 25
        reasons.append(f"YES expensive at {yes_price:.0%}")

    # Tighter crypto keywords
    if any(word in question for word in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol"]):
        score += 20
        reasons.append("Crypto market")

    # Tighter sports keywords
    if any(word in question for word in ["nba", "nfl", "mlb", "nhl", "ufc", "premier league", 
                                          "champions league", "world cup", "superbowl", "super bowl",
                                          "playoffs", "championship game"]):
        score += 15
        reasons.append("Sports market")

    # Tighter politics keywords
    if any(word in question for word in ["president", "election", "senate", "congress", 
                                          "governor", "parliament", "vote", "referendum"]):
        score += 15
        reasons.append("Politics market")

    # Volume scoring
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

async def scan_markets():
    log.info("🤖 Polymarket Bot v2 Starting...")
    log.info(f"⚙️  Alert threshold: score {CONFIG['min_score_for_alert']}+")
    log.info("=" * 50)

    await send_telegram("🤖 <b>Polymarket Bot v2 Started!</b>\nNow filtering closed markets and using smarter scoring...")

    alerted_markets = set()

    while True:
        log.info("🔍 Scanning Polymarket markets...")
        markets = await fetch_polymarket_markets()

        if not markets:
            log.warning("⚠️ No markets returned - will retry")
        else:
            # Filter to only active markets
            active_markets = [m for m in markets if is_market_active(m)]
            log.info(f"📊 Found {len(markets)} markets → {len(active_markets)} active after filtering")

            opportunities = []
            for market in active_markets:
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
                        "volume": float(market.get("volume", 0))
                    })

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
