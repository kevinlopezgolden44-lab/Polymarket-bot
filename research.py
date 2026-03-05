import aiohttp
import asyncio
import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

def now():
    return datetime.utcnow()

# ── MODULE-LEVEL CACHE ─────────────────────────────────────────────────────────
_crypto_cache: dict = {}          # coingecko_id -> {"data": ..., "fetched_at": datetime}
_crypto_lock = asyncio.Lock()     # prevents simultaneous duplicate requests
CRYPTO_CACHE_TTL_SECONDS = 120   # reuse data for 2 minutes before re-fetching

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
    return {"success": False, "score": 50, "regime": "Unknown",
            "sentiment_bonus": 0, "trend": "UNKNOWN", "classification": "Unknown"}

def _parse_coin(question: str) -> tuple[str, str]:
    """Return (coingecko_id, symbol) based on question text."""
    q = question.lower()
    if "ethereum" in q or " eth" in q:
        return "ethereum", "ETH"
    if "solana" in q or " sol" in q:
        return "solana", "SOL"
    if "xrp" in q or "ripple" in q:
        return "ripple", "XRP"
    return "bitcoin", "BTC"

async def _fetch_coingecko(coingecko_id: str, coin_symbol: str) -> dict:
    """Raw fetch from CoinGecko with retry-after support. Returns success dict or {"success": False}."""
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=" + coingecko_id +
        "&vs_currencies=usd&include_24hr_change=true"
    )
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        asset = data.get(coingecko_id, {})
                        price = float(asset.get("usd", 0))
                        change_24h = float(asset.get("usd_24h_change", 0))
                        return {
                            "coin": coin_symbol,
                            "price": price,
                            "change_24h": round(change_24h, 2),
                            "direction": "UP" if change_24h > 0 else "DOWN",
                            "success": True,
                        }
                    elif resp.status == 429:
                        retry_after = int(resp.headers.get("Retry-After", 60))
                        log.warning(
                            "CoinGecko rate limit hit (attempt %d/3) - waiting %ds",
                            attempt + 1, retry_after,
                        )
                        if attempt < 2:
                            await asyncio.sleep(retry_after)
                        else:
                            log.warning("CoinGecko rate limit: all retries exhausted, skipping scan")
                    else:
                        log.warning("CoinGecko unexpected status %d", resp.status)
                        break
        except asyncio.TimeoutError:
            log.warning("CoinGecko timeout (attempt %d/3)", attempt + 1)
        except Exception as e:
            log.error("CoinGecko error: %s", e)
            break
    return {"success": False}

async def get_crypto_data(question: str) -> dict:
    """
    Returns crypto price data for the coin mentioned in `question`.
    Results are cached per-coin for CRYPTO_CACHE_TTL_SECONDS to avoid
    hammering the CoinGecko free-tier rate limit across many markets.
    """
    coingecko_id, coin_symbol = _parse_coin(question)

    async with _crypto_lock:
        cached = _crypto_cache.get(coingecko_id)
        if cached:
            age = (now() - cached["fetched_at"]).total_seconds()
            if age < CRYPTO_CACHE_TTL_SECONDS:
                log.debug("CoinGecko cache hit for %s (age %.0fs)", coingecko_id, age)
                return cached["data"]

        # Cache miss or stale — fetch fresh data
        result = await _fetch_coingecko(coingecko_id, coin_symbol)
        if result["success"]:
            _crypto_cache[coingecko_id] = {"data": result, "fetched_at": now()}
        return result

async def get_sports_odds(question, odds_api_key):
    if not odds_api_key:
        return {"success": False}
    sports_map = {
        "nba": "basketball_nba",
        "nfl": "americanfootball_nfl",
        "mlb": "baseball_mlb",
        "nhl": "icehockey_nhl",
        "ufc": "mma_mixed_martial_arts",
        "premier league": "soccer_epl",
        "champions league": "soccer_uefa_champs_league",
        "world cup": "soccer_fifa_world_cup_winner",
        "super bowl": "americanfootball_nfl",
        "march madness": "basketball_ncaab",
        "ncaa": "basketball_ncaab"
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
            "?apiKey=" + odds_api_key
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
                            bookmakers = best_match.get("bookmakers", [])
                            if bookmakers:
                                outcomes = bookmakers[0].get("markets", [{}])[0].get("outcomes", [])
                                odds_data = {}
                                for o in outcomes:
                                    implied_prob = round(1 / float(o.get("price", 2.0)) * 100, 1)
                                    odds_data[o.get("name", "")] = implied_prob
                                return {
                                    "success": True,
                                    "home_team": best_match.get("home_team", ""),
                                    "away_team": best_match.get("away_team", ""),
                                    "odds": odds_data
                                }
    except Exception as e:
        log.error("Odds API error: %s", e)
    return {"success": False}

def build_crypto_summary(question, yes_price, crypto_data, fear_greed):
    lines = ["Crypto Research:"]
    if crypto_data.get("success"):
        price = crypto_data["price"]
        coin = crypto_data["coin"]
        change = crypto_data["change_24h"]
        direction = crypto_data["direction"]
        lines.append("Current " + coin + ": $" + str(round(price, 2)))
        lines.append("24h change: " + str(change) + "% " + direction)

        numbers = re.findall(r"\$[\d,]+", question)
        if numbers:
            try:
                target = float(numbers[0].replace("$", "").replace(",", ""))
                diff_pct = round((target - price) / price * 100, 1)
                prefix = "+" if diff_pct > 0 else ""
                lines.append("Target needs " + prefix + str(diff_pct) + "% move")
                if yes_price < 0.15 and abs(diff_pct) > 10:
                    lines.append("Recommendation: LIKELY DISAGREE - target far")
                elif yes_price < 0.15 and abs(diff_pct) <= 10:
                    lines.append("Recommendation: INVESTIGATE - target reachable")
                elif yes_price > 0.85 and abs(diff_pct) < 5:
                    lines.append("Recommendation: LIKELY AGREE - target close")
                else:
                    lines.append("Recommendation: INVESTIGATE FURTHER")
            except Exception as e:
                log.warning("build_crypto_summary target calc error: %s", e)

    if fear_greed and fear_greed.get("success"):
        lines.append("Fear and Greed: " + str(fear_greed["score"]) +
                     " (" + fear_greed["regime"] + ") " + fear_greed.get("trend", ""))
        if fear_greed["regime"] == "Extreme Fear":
            lines.append("Sentiment: Contrarian conditions - historically favorable")
        elif fear_greed["regime"] == "Extreme Greed":
            lines.append("Sentiment: Market euphoric - exercise caution")

    return "\n".join(lines)

def build_sports_summary(question, yes_price, sports_data):
    if not sports_data.get("success"):
        return "Sports Research: Could not fetch live odds"
    lines = ["Sports Research:"]
    home = sports_data["home_team"]
    away = sports_data["away_team"]
    odds = sports_data["odds"]
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
            lines.append("Gap: +" + str(gap) + "% - AGREE underpriced vs Vegas")
        elif gap < -10:
            lines.append("Gap: " + str(gap) + "% - DISAGREE overpriced vs Vegas")
        else:
            lines.append("Gap: " + str(gap) + "% - fair price, INVESTIGATE")
    return "\n".join(lines)

async def build_research_summary(question, yes_price, category, fear_greed, odds_api_key):
    parts = []
    if category == "Crypto":
        crypto_data = await get_crypto_data(question)
        parts.append(build_crypto_summary(question, yes_price, crypto_data, fear_greed))
    if category == "Sports":
        sports_data = await get_sports_odds(question, odds_api_key)
        parts.append(build_sports_summary(question, yes_price, sports_data))
    return "\n\n".join(parts) if parts else None