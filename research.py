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
CRYPTO_CACHE_TTL_SECONDS = 300   # reuse data for 5 minutes before re-fetching

ALL_COINS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "ripple": "XRP",
}

async def prefetch_all_crypto():
    """
    Fetch all tracked coins in a SINGLE CoinGecko request.
    Call this once at the start of each scan loop so individual
    market lookups never touch the API mid-scan.
    """
    ids = ",".join(ALL_COINS.keys())
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=" + ids +
        "&vs_currencies=usd&include_24hr_change=true"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    fetched_at = now()
                    async with _crypto_lock:
                        for coingecko_id, symbol in ALL_COINS.items():
                            asset = data.get(coingecko_id, {})
                            if not asset:
                                continue
                            price = float(asset.get("usd", 0))
                            change_24h = float(asset.get("usd_24h_change", 0))
                            _crypto_cache[coingecko_id] = {
                                "fetched_at": fetched_at,
                                "data": {
                                    "coin": symbol,
                                    "price": price,
                                    "change_24h": round(change_24h, 2),
                                    "direction": "UP" if change_24h > 0 else "DOWN",
                                    "success": True,
                                }
                            }
                    log.info("CoinGecko prefetch OK: %s", ", ".join(ALL_COINS.keys()))
                    return True
                elif resp.status == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    log.warning("CoinGecko prefetch rate limited - retry after %ds", retry_after)
                else:
                    log.warning("CoinGecko prefetch status %d", resp.status)
    except Exception as e:
        log.error("CoinGecko prefetch error: %s", e)
    return False

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
                        # sentiment_bonus is only applied to Crypto markets in scoring.py.
                        # Extreme Fear = contrarian buy signal (market oversold, mispricings likely)
                        # Extreme Greed = caution, market euphoric and overcorrected
                        if score <= 25:
                            regime = "Extreme Fear"
                            sentiment_bonus = 10   # strong contrarian signal for crypto
                        elif score <= 49:
                            regime = "Fear"
                            sentiment_bonus = 5    # mild contrarian signal
                        elif score <= 74:
                            regime = "Greed"
                            sentiment_bonus = -5   # slight caution
                        else:
                            regime = "Extreme Greed"
                            sentiment_bonus = -8   # market likely overcorrected
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
    """
    Single-coin fallback fetch — only called on a cache miss.
    Under normal operation prefetch_all_crypto() fills the cache
    before any market scan, so this path should rarely be hit.
    """
    url = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=" + coingecko_id +
        "&vs_currencies=usd&include_24hr_change=true"
    )
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
                    log.warning("CoinGecko fallback rate limited - crypto skipped for this market")
                else:
                    log.warning("CoinGecko fallback status %d", resp.status)
    except Exception as e:
        log.error("CoinGecko fallback error: %s", e)
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

    question_lower = question.lower()

    # Draw markets can't be compared to h2h Vegas odds — skip entirely
    draw_phrases = ["end in a draw", "in a draw", "result in a draw", "be a draw",
                    "draw?", "drawn match", "tied game", "end in a tie"]
    if any(p in question_lower for p in draw_phrases):
        log.info("Skipping Vegas odds for draw market: %s", question[:60])
        return {"success": False, "reason": "draw_market"}

    # Ordered list — specific keywords first, soccer clubs before generic terms
    sports_map = [
        ("champions league",    "soccer_uefa_champs_league"),
        ("premier league",      "soccer_epl"),
        ("la liga",             "soccer_spain_la_liga"),
        ("serie a",             "soccer_italy_serie_a"),
        ("bundesliga",          "soccer_germany_bundesliga"),
        ("ligue 1",             "soccer_france_ligue_one"),
        ("mls",                 "soccer_usa_mls"),
        ("world cup",           "soccer_fifa_world_cup_winner"),
        ("real madrid",         "soccer_spain_la_liga"),
        ("barcelona",           "soccer_spain_la_liga"),
        ("manchester city",     "soccer_epl"),
        ("manchester united",   "soccer_epl"),
        ("liverpool",           "soccer_epl"),
        ("arsenal",             "soccer_epl"),
        ("chelsea",             "soccer_epl"),
        ("tottenham",           "soccer_epl"),
        ("atletico madrid",     "soccer_spain_la_liga"),
        ("juventus",            "soccer_italy_serie_a"),
        ("ac milan",            "soccer_italy_serie_a"),
        ("inter milan",         "soccer_italy_serie_a"),
        ("bayern munich",       "soccer_germany_bundesliga"),
        ("borussia dortmund",   "soccer_germany_bundesliga"),
        ("paris saint-germain", "soccer_france_ligue_one"),
        ("psg",                 "soccer_france_ligue_one"),
        ("super bowl",          "americanfootball_nfl"),
        ("nfl",                 "americanfootball_nfl"),
        ("nba",                 "basketball_nba"),
        ("march madness",       "basketball_ncaab"),
        ("ncaa",                "basketball_ncaab"),
        ("mlb",                 "baseball_mlb"),
        ("world series",        "baseball_mlb"),
        ("nhl",                 "icehockey_nhl"),
        ("stanley cup",         "icehockey_nhl"),
        ("ufc",                 "mma_mixed_martial_arts"),
        ("mma",                 "mma_mixed_martial_arts"),
    ]

    sport_key = None
    for keyword, key in sports_map:
        if keyword in question_lower:
            sport_key = key
            break

    if sport_key is None:
        log.info("No sport detected for Vegas lookup: %s", question[:60])
        return {"success": False, "reason": "no_sport_detected"}

    # Strict team matching — filter stop words, require min score of 2
    STOP_WORDS = {"will", "the", "this", "that", "game", "match", "beat",
                  "win", "wins", "lose", "play", "plays", "tonight", "week",
                  "2024", "2025", "2026", "series", "over", "home", "away"}
    question_words = [
        w for w in re.findall(r"[a-z]+", question_lower)
        if len(w) >= 4 and w not in STOP_WORDS
    ]

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
                    if not games:
                        return {"success": False, "reason": "no_games_returned"}

                    best_match = None
                    best_score = 0
                    for game in games:
                        home = game.get("home_team", "").lower()
                        away = game.get("away_team", "").lower()
                        combined = home + " " + away
                        match_score = sum(1 for w in question_words if w in combined)
                        # Bonus for full team name match
                        if home in question_lower or away in question_lower:
                            match_score += 5
                        if match_score > best_score:
                            best_score = match_score
                            best_match = game

                    MIN_MATCH_SCORE = 2
                    if not best_match or best_score < MIN_MATCH_SCORE:
                        log.info("No confident game match (best=%d): %s", best_score, question[:60])
                        return {"success": False, "reason": "no_team_match"}

                    bookmakers = best_match.get("bookmakers", [])
                    if not bookmakers:
                        return {"success": False, "reason": "no_bookmakers"}

                    outcomes = bookmakers[0].get("markets", [{}])[0].get("outcomes", [])
                    odds_data = {}
                    for o in outcomes:
                        implied_prob = round(1 / float(o.get("price", 2.0)) * 100, 1)
                        odds_data[o.get("name", "")] = implied_prob

                    log.info("Vegas match (score=%d): %s vs %s",
                             best_score, best_match.get("home_team"), best_match.get("away_team"))
                    return {
                        "success": True,
                        "home_team": best_match.get("home_team", ""),
                        "away_team": best_match.get("away_team", ""),
                        "sport_key": sport_key,
                        "match_score": best_score,
                        "odds": odds_data,
                    }

                elif resp.status == 401:
                    log.error("Odds API: invalid API key")
                elif resp.status == 422:
                    log.warning("Odds API: sport not available (%s)", sport_key)
                elif resp.status == 429:
                    log.warning("Odds API: rate limited")
                else:
                    log.warning("Odds API status %d for %s", resp.status, sport_key)

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
    reason = sports_data.get("reason", "")
    if not sports_data.get("success"):
        if reason == "draw_market":
            return "Sports Research: Draw market — Vegas h2h odds not applicable"
        if reason == "no_sport_detected":
            return "Sports Research: Could not detect sport type from question"
        if reason == "no_team_match":
            return "Sports Research: Could not match question to a live game"
        return "Sports Research: Could not fetch live odds"

    lines = ["Sports Research:"]
    home = sports_data["home_team"]
    away = sports_data["away_team"]
    odds = sports_data["odds"]
    sport_key = sports_data.get("sport_key", "")
    match_score = sports_data.get("match_score", 0)
    lines.append(f"Matched: {away} vs {home} [{sport_key}, confidence:{match_score}]")

    question_lower = question.lower()
    matched_team = None
    matched_prob = None
    # Strict: require full team name words to appear in question
    for team, prob in odds.items():
        team_words = [w for w in team.lower().split() if len(w) >= 4]
        if team_words and all(w in question_lower for w in team_words):
            matched_team = team
            matched_prob = prob
            break
    # Fallback: any meaningful word
    if not matched_team:
        for team, prob in odds.items():
            if any(w in question_lower for w in team.lower().split() if len(w) >= 5):
                matched_team = team
                matched_prob = prob
                break

    for team, prob in odds.items():
        lines.append(f"Vegas: {team} {prob}%")

    if matched_team and matched_prob:
        polymarket_pct = round(yes_price * 100, 1)
        gap = round(matched_prob - polymarket_pct, 1)
        lines.append(f"Polymarket YES: {polymarket_pct}%")
        lines.append(f"Vegas implied: {matched_prob}%")
        if gap > 10:
            lines.append(f"Gap: +{gap}% — AGREE underpriced vs Vegas")
        elif gap < -10:
            lines.append(f"Gap: {gap}% — DISAGREE overpriced vs Vegas")
        else:
            lines.append(f"Gap: {gap}% — fair price, INVESTIGATE")
    else:
        lines.append("Could not match a team from the question to Vegas odds")
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