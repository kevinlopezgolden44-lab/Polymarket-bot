import aiohttp
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

def now():
    return datetime.utcnow()

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

async def get_crypto_data(question):
    question_lower = question.lower()
    if "ethereum" in question_lower or " eth" in question_lower:
        coincap_id = "ethereum"
        coingecko_id = "ethereum"
        coin_symbol = "ETH"
    elif "solana" in question_lower or " sol" in question_lower:
        coincap_id = "solana"
        coingecko_id = "solana"
        coin_symbol = "SOL"
    elif "xrp" in question_lower or "ripple" in question_lower:
        coincap_id = "xrp"
        coingecko_id = "ripple"
        coin_symbol = "XRP"
    else:
        coincap_id = "bitcoin"
        coingecko_id = "bitcoin"
        coin_symbol = "BTC"

    async with aiohttp.ClientSession() as session:
        # Primary: CoinCap
        try:
            url = "https://api.coincap.io/v2/assets/" + coincap_id
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    asset = data.get("data", {})
                    price = float(asset.get("priceUsd", 0))
                    change_24h = float(asset.get("changePercent24Hr", 0))
                    return {
                        "coin": coin_symbol,
                        "price": price,
                        "change_24h": round(change_24h, 2),
                        "direction": "UP" if change_24h > 0 else "DOWN",
                        "source": "coincap",
                        "success": True
                    }
        except Exception as e:
            log.warning("CoinCap error: %s - trying CoinGecko fallback", e)

        # Fallback: CoinGecko
        try:
            url = (
                "https://api.coingecko.com/api/v3/simple/price"
                "?ids=" + coingecko_id +
                "&vs_currencies=usd&include_24hr_change=true"
            )
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
                        "source": "coingecko",
                        "success": True
                    }
        except Exception as e:
            log.error("CoinGecko fallback error: %s", e)

    return {"success": False}

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