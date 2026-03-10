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

# ── SPORT DETECTION MAP ───────────────────────────────────────────────────────
# Ordered — specific patterns first. NBA team nicknames listed explicitly so
# "Grizzlies vs. 76ers" is caught even without "NBA" in the question.
SPORTS_DETECTION_MAP = [
    # Soccer leagues + clubs
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
    # NFL
    ("super bowl",          "americanfootball_nfl"),
    ("nfl",                 "americanfootball_nfl"),
    ("chiefs",              "americanfootball_nfl"),
    ("eagles",              "americanfootball_nfl"),
    ("cowboys",             "americanfootball_nfl"),
    ("patriots",            "americanfootball_nfl"),
    ("49ers",               "americanfootball_nfl"),
    ("packers",             "americanfootball_nfl"),
    ("ravens",              "americanfootball_nfl"),
    ("bills",               "americanfootball_nfl"),
    ("broncos",             "americanfootball_nfl"),
    ("steelers",            "americanfootball_nfl"),
    ("raiders",             "americanfootball_nfl"),
    ("seahawks",            "americanfootball_nfl"),
    ("rams",                "americanfootball_nfl"),
    ("chargers",            "americanfootball_nfl"),
    ("dolphins",            "americanfootball_nfl"),
    ("bears",               "americanfootball_nfl"),
    ("lions",               "americanfootball_nfl"),
    ("vikings",             "americanfootball_nfl"),
    ("falcons",             "americanfootball_nfl"),
    ("saints",              "americanfootball_nfl"),
    ("buccaneers",          "americanfootball_nfl"),
    ("panthers",            "americanfootball_nfl"),
    # NBA — team nicknames catch questions without explicit "NBA"
    ("nba",                 "basketball_nba"),
    ("lakers",              "basketball_nba"),
    ("celtics",             "basketball_nba"),
    ("warriors",            "basketball_nba"),
    ("bucks",               "basketball_nba"),
    ("nets",                "basketball_nba"),
    ("heat",                "basketball_nba"),
    ("suns",                "basketball_nba"),
    ("nuggets",             "basketball_nba"),
    ("clippers",            "basketball_nba"),
    ("76ers",               "basketball_nba"),
    ("sixers",              "basketball_nba"),
    ("knicks",              "basketball_nba"),
    ("bulls",               "basketball_nba"),
    ("spurs",               "basketball_nba"),
    ("mavericks",           "basketball_nba"),
    ("mavs",                "basketball_nba"),
    ("grizzlies",           "basketball_nba"),
    ("rockets",             "basketball_nba"),
    ("thunder",             "basketball_nba"),
    ("trail blazers",       "basketball_nba"),
    ("blazers",             "basketball_nba"),
    ("timberwolves",        "basketball_nba"),
    ("pistons",             "basketball_nba"),
    ("hornets",             "basketball_nba"),
    ("wizards",             "basketball_nba"),
    ("magic",               "basketball_nba"),
    ("hawks",               "basketball_nba"),
    ("jazz",                "basketball_nba"),
    ("kings",               "basketball_nba"),
    ("pelicans",            "basketball_nba"),
    ("cavaliers",           "basketball_nba"),
    ("cavs",                "basketball_nba"),
    ("raptors",             "basketball_nba"),
    ("pacers",              "basketball_nba"),
    # NCAA
    ("march madness",       "basketball_ncaab"),
    ("ncaa",                "basketball_ncaab"),
    # MLB
    ("mlb",                 "baseball_mlb"),
    ("world series",        "baseball_mlb"),
    ("yankees",             "baseball_mlb"),
    ("dodgers",             "baseball_mlb"),
    ("red sox",             "baseball_mlb"),
    ("cubs",                "baseball_mlb"),
    ("astros",              "baseball_mlb"),
    ("braves",              "baseball_mlb"),
    ("mets",                "baseball_mlb"),
    # NHL
    ("nhl",                 "icehockey_nhl"),
    ("stanley cup",         "icehockey_nhl"),
    ("maple leafs",         "icehockey_nhl"),
    ("canadiens",           "icehockey_nhl"),
    ("bruins",              "icehockey_nhl"),
    ("rangers",             "icehockey_nhl"),
    ("blackhawks",          "icehockey_nhl"),
    # Golf — futures/outright only; no per-game h2h odds available
    ("masters",             "golf_masters_tournament_winner"),
    ("pga championship",    "golf_pga_championship"),
    ("the open",            "golf_the_open_championship"),
    ("pga tour",            "golf_pga_tour"),
    # MMA/UFC
    ("ufc",                 "mma_mixed_martial_arts"),
    ("mma",                 "mma_mixed_martial_arts"),
]

# Sports that only have futures/outrights — no per-game h2h odds exist.
# Vegas divergence doesn't apply; skip the API call entirely.
# Sports with no per-game h2h odds (futures/outright only)
FUTURES_ONLY_SPORTS = {18}  # FIFA World Cup — no single-game h2h markets

# ── THERUNDOWN API — SPORT ID MAP ─────────────────────────────────────────────
# TheRundown uses numeric sport IDs. Free tier: 20,000 data points/day.
# GET /api/v2/sports/{sport_id}/events/{date}?key=KEY&market_ids=1
# market_id=1 = moneyline (h2h). Response: events[] with teams[] and lines[].
THERUNDOWN_SPORT_IDS = {
    # Soccer
    "soccer_uefa_champs_league": 16,
    "soccer_epl":                11,
    "soccer_spain_la_liga":      14,
    "soccer_italy_serie_a":      15,
    "soccer_germany_bundesliga": 13,
    "soccer_france_ligue_one":   12,
    "soccer_usa_mls":            10,
    # NFL
    "americanfootball_nfl":      2,
    # NBA
    "basketball_nba":            4,
    # NCAA
    "basketball_ncaab":          5,
    # MLB
    "baseball_mlb":              3,
    # NHL
    "icehockey_nhl":             6,
    # MMA/UFC
    "mma_mixed_martial_arts":    7,
}

# ── SPORTS ODDS SCAN CACHE ────────────────────────────────────────────────────
# Fetched ONCE per scan loop per sport, reused for all markets of that sport.
_sports_odds_cache: dict = {}
SPORTS_CACHE_TTL_SECONDS = 300  # 5 minutes — covers one full scan loop


def detect_sport_key(question: str) -> str | None:
    """Returns the internal sport key string for a question, or None."""
    q = question.lower()
    for keyword, key in SPORTS_DETECTION_MAP:
        if keyword in q:
            return key
    return None


def _parse_therundown_game(event: dict) -> dict:
    """
    Normalise a TheRundown v2 event into the same shape our matching
    logic expects: home_team, away_team, odds {team_name: implied_prob}.
    """
    teams = event.get("teams", [])
    # TheRundown: teams[0]=away, teams[1]=home (standard convention)
    away = teams[0].get("name", "") if len(teams) > 0 else ""
    home = teams[1].get("name", "") if len(teams) > 1 else ""

    # Find moneyline (market_id=1) lines
    odds_data = {}
    for line in event.get("lines", {}).values():
        ml = line.get("moneyline", {})
        for side, team_name in [("moneyline_home", home), ("moneyline_away", away)]:
            price = ml.get(side)
            if price and price != 0.0001:   # 0.0001 = TheRundown sentinel for "off the board"
                if price > 0:
                    # Positive American odds e.g. +150
                    implied = round(100 / (price + 100) * 100, 1)
                else:
                    # Negative American odds e.g. -200
                    implied = round(abs(price) / (abs(price) + 100) * 100, 1)
                if team_name and team_name not in odds_data:
                    odds_data[team_name] = implied
        if odds_data:
            break  # Use first sportsbook with valid lines

    return {"home_team": home, "away_team": away, "odds": odds_data}


async def prefetch_sports_odds(sport_key: str, odds_api_key: str) -> list:
    """
    Fetch today's games for one sport from TheRundown and cache the result.
    `odds_api_key` is repurposed as THERUNDOWN_API_KEY — rename the Railway
    env var or add THERUNDOWN_API_KEY alongside the old one.
    Returns a normalised list of game dicts (home_team, away_team, odds).
    """
    cached = _sports_odds_cache.get(sport_key)
    if cached:
        age = (now() - cached["fetched_at"]).total_seconds()
        if age < SPORTS_CACHE_TTL_SECONDS:
            log.debug("Sports odds cache hit for %s (age %.0fs)", sport_key, age)
            return cached["games"]

    sport_id = THERUNDOWN_SPORT_IDS.get(sport_key)
    if sport_id is None:
        log.warning("No TheRundown sport_id for key: %s", sport_key)
        return []

    date_str = now().strftime("%Y-%m-%d")
    url = (
        f"https://therundown.io/api/v2/sports/{sport_id}/events/{date_str}"
        f"?key={odds_api_key}&market_ids=1"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_events = data.get("events", [])
                    games = [_parse_therundown_game(e) for e in raw_events]
                    # Filter out games with no usable odds
                    games = [g for g in games if g["odds"]]
                    _sports_odds_cache[sport_key] = {"games": games, "fetched_at": now()}
                    log.info("TheRundown prefetch OK: %s (%d games with odds)", sport_key, len(games))
                    return games
                elif resp.status == 401:
                    log.error("TheRundown: 401 — invalid API key (check THERUNDOWN_API_KEY in Railway)")
                elif resp.status == 429:
                    log.warning("TheRundown: 429 — daily quota exhausted (20k/day free tier)")
                else:
                    log.warning("TheRundown status %d for %s", resp.status, sport_key)
    except Exception as e:
        log.error("TheRundown prefetch error: %s", e)
    return []


async def get_sports_odds(question, odds_api_key, prefetched_games: list | None = None):
    """
    Returns Vegas h2h odds for the game best matching `question`.
    `odds_api_key` is the THERUNDOWN_API_KEY value.
    Pass `prefetched_games` from prefetch_sports_odds() to skip the API call —
    normal path during a scan loop to avoid per-market requests.
    """
    if not odds_api_key:
        return {"success": False}

    question_lower = question.lower()

    # Draw markets — h2h moneyline doesn't cover draw probability
    draw_phrases = ["end in a draw", "in a draw", "result in a draw", "be a draw",
                    "draw?", "drawn match", "tied game", "end in a tie"]
    if any(p in question_lower for p in draw_phrases):
        return {"success": False, "reason": "draw_market"}

    sport_key = detect_sport_key(question)
    if sport_key is None:
        log.info("No sport detected for Vegas lookup: %s", question[:60])
        return {"success": False, "reason": "no_sport_detected"}

    sport_id = THERUNDOWN_SPORT_IDS.get(sport_key)
    if sport_id in FUTURES_ONLY_SPORTS:
        log.info("Futures-only sport, skipping h2h lookup: %s", sport_key)
        return {"success": False, "reason": "futures_only_sport"}

    if prefetched_games is not None:
        games = prefetched_games
    else:
        games = await prefetch_sports_odds(sport_key, odds_api_key)

    if not games:
        return {"success": False, "reason": "no_games_returned"}

    # Strict team matching — stop words filtered, min score of 2
    STOP_WORDS = {"will", "the", "this", "that", "game", "match", "beat",
                  "win", "wins", "lose", "play", "plays", "tonight", "week",
                  "2024", "2025", "2026", "series", "over", "home", "away"}
    question_words = [
        w for w in re.findall(r"[a-z]+", question_lower)
        if len(w) >= 4 and w not in STOP_WORDS
    ]

    best_match = None
    best_score = 0
    for game in games:
        home = game.get("home_team", "").lower()
        away = game.get("away_team", "").lower()
        combined = home + " " + away
        match_score = sum(1 for w in question_words if w in combined)
        if home in question_lower or away in question_lower:
            match_score += 5
        if match_score > best_score:
            best_score = match_score
            best_match = game

    MIN_MATCH_SCORE = 2
    if not best_match or best_score < MIN_MATCH_SCORE:
        log.info("No confident game match (best=%d): %s", best_score, question[:60])
        return {"success": False, "reason": "no_team_match"}

    if not best_match.get("odds"):
        return {"success": False, "reason": "no_bookmakers"}

    log.info("TheRundown match (score=%d): %s vs %s",
             best_score, best_match.get("home_team"), best_match.get("away_team"))
    return {
        "success": True,
        "home_team": best_match.get("home_team", ""),
        "away_team": best_match.get("away_team", ""),
        "sport_key": sport_key,
        "match_score": best_score,
        "odds": best_match["odds"],
    }

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
        if reason == "futures_only_sport":
            return "Sports Research: Futures/outright market — per-game h2h odds not available"
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