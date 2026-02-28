​import asyncio
import aiohttp
import requests
import json
import logging
from datetime import datetime

# ============================================================
# CONFIGURATION - Edit these values to customize your bot
# ============================================================
CONFIG = {
    "check_interval_seconds": 30,      # How often to scan markets
    "min_edge_threshold": 0.15,        # Only flag if signal differs by 15%+
    "max_daily_loss": 20,              # Stop trading if down $20 in a day
    "max_trade_size": 5,               # Maximum $ per trade
    "max_open_positions": 10,          # Maximum simultaneous trades
    "capital_at_risk_pct": 0.30,       # Max 30% of bankroll in trades
}

# ============================================================
# LOGGING SETUP - Readable plain English logs
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ============================================================
# MARKET MONITOR
# ============================================================
async def fetch_polymarket_markets():
    """Fetch active markets from Polymarket"""
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
    """
    Score a market opportunity based on available signals.
    Returns a score between 0-100 and a reason.
    """
    question = market.get("question", "").lower()
    tokens = market.get("tokens", [])
    
    if len(tokens) < 2:
        return 0, "Not enough token data"

    try:
        yes_price = float(tokens[0].get("price", 0.5))
        no_price = float(tokens[1].get("price", 0.5))
    except:
        return 0, "Could not parse prices"

    score = 0
    reasons = []

    # Check for extreme prices (potential mispricings)
    if yes_price < 0.05:
        score += 30
        reasons.append(f"YES very cheap at {yes_price:.0%}")
    elif yes_price > 0.95:
        score += 30
        reasons.append(f"YES very expensive at {yes_price:.0%}")

    # Check for crypto markets
    if any(word in question for word in ["btc", "bitcoin", "eth", "crypto"]):
        score += 20
        reasons.append("Crypto market")

    # Check for sports markets
    if any(word in question for word in ["win", "score", "game", "match", "championship"]):
        score += 15
        reasons.append("Sports market")

    # Check for news/politics
    if any(word in question for word in ["president", "election", "will", "policy"]):
        score += 15
        reasons.append("News/Politics market")

    # Volume check
    volume = float(market.get("volume", 0))
    if volume > 10000:
        score += 20
        reasons.append(f"High volume ${volume:,.0f}")
    elif volume > 1000:
        score += 10
        reasons.append(f"Medium volume ${volume:,.0f}")

    return score, " | ".join(reasons) if reasons else "General market"

async def scan_markets():
    """Main scanning loop"""
    log.info("🤖 Polymarket Bot Starting...")
    log.info(f"⚙️  Scanning every {CONFIG['check_interval_seconds']} seconds")
    log.info(f"⚙️  Min edge threshold: {CONFIG['min_edge_threshold']:.0%}")
    log.info(f"⚙️  Max trade size: ${CONFIG['max_trade_size']}")
    log.info("=" * 50)

    daily_opportunities = []

    while True:
        log.info("🔍 Scanning Polymarket markets...")
        markets = await fetch_polymarket_markets()

        if not markets:
            log.warning("⚠️  No markets returned - will retry")
        else:
            log.info(f"📊 Found {len(markets)} active markets")
            opportunities = []

            for market in markets:
                score, reason = score_opportunity(market)
                if score >= 40:  # Only show meaningful opportunities
                    question = market.get("question", "Unknown")
                    tokens = market.get("tokens", [])
                    yes_price = float(tokens[0].get("price", 0)) if tokens else 0
                    
                    opportunities.append({
                        "question": question,
                        "score": score,
                        "reason": reason,
                        "yes_price": yes_price,
                        "volume": market.get("volume", 0)
                    })

            # Sort by score
            opportunities.sort(key=lambda x: x["score"], reverse=True)

            if opportunities:
                log.info(f"✅ Found {len(opportunities)} opportunities:")
                for i, opp in enumerate(opportunities[:5], 1):  # Show top 5
                    log.info(f"  #{i} Score:{opp['score']} | {opp['question'][:60]}...")
                    log.info(f"      YES Price: {opp['yes_price']:.0%} | {opp['reason']}")
                daily_opportunities.extend(opportunities)
            else:
                log.info("  No strong opportunities found this scan")

        log.info(f"⏰ Next scan in {CONFIG['check_interval_seconds']} seconds")
        log.info("-" * 50)
        await asyncio.sleep(CONFIG["check_interval_seconds"])

# ============================================================
# MAIN ENTRY POINT
# ============================================================
if __name__ == "__main__":
    asyncio.run(scan_markets())
