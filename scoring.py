import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

def now():
    return datetime.utcnow()


# ── IMPORTS FROM analysis.py (single source of truth) ────────────────────────
# These functions are defined in analysis.py. Import them here instead of
# duplicating. Any changes should be made in analysis.py only.
from analysis import (
    analyze_price_momentum,
    analyze_price_velocity,
    analyze_liquidity,
    check_cross_market_consistency,
    detect_polymarket_lag,
    analyze_event_timing,
    check_resolution_ambiguity,
    calculate_confidence_tier,
    check_resolution_sanity,
)

# ── 9. CATEGORY DETECTION ─────────────────────────────────────────────────────
def detect_category(question):
    """
    Derives a category string from the market question.
    Returns one of: Crypto, Sports, Politics, Economics, Science, General.

    Uses word boundary matching for short ambiguous tickers (eth, sol, bnb)
    to prevent false positives like "Sola" → Crypto or "console" → Crypto.
    """
    q = question.lower()

    # ── Early Politics detection for crypto-mentioning political markets ────────
    # Must run BEFORE crypto detection to catch markets like:
    # "Will Trump say Crypto this week?" — political market, not crypto price
    # These contain crypto keywords but are fundamentally political questions.
    _strong_political = [
        "will trump say", "will biden say", "will trump mention",
        "will the president say", "will trump tweet", "will trump post",
        "trump executive order", "trump sign", "trump announce",
        "will congress pass", "will senate pass", "will the fed chair",
        "diplomatic", "ceasefire", "peace deal", "invasion of",
        "war in ukraine", "war in russia", "nato summit",
        "us-iran", "us-china", "us-russia", "us-north korea",
    ]
    if any(p in q for p in _strong_political):
        return "Politics"

    # ── Crypto detection ──────────────────────────────────────────────────────
    # Long-form names: safe as substring match (unlikely to appear in other context)
    crypto_explicit = [
        "bitcoin", "btc", "ethereum", "crypto",
        "solana", "xrp", "ripple",
        # Additional coins common on Polymarket
        "dogecoin", "doge", "pepe", "avax",  # removed "avalanche" — also an NHL team, use "avax" ticker only
        "polygon", "shiba", "shib", "chainlink",
        # "matic" removed — matches "diplomatic", "automatic" etc. Use word boundary below.
        "binance coin", "bnb coin", "uniswap", "litecoin",
        "cardano", "polkadot",
        # "ada" and "dot" removed — match inside common words (Rivadavia, Granada, adopted)
        # caught by word boundary regex below instead
    ]
    if any(w in q for w in crypto_explicit):
        return "Crypto"

    # Short tickers with word boundaries — prevents false positives
    # "ada" matches "Rivadavia", "Granada", "Canada"
    # "dot" matches "adopted", "endothermic"
    # "dai" matches "daily", "median"
    if re.search(r'\b(eth|sol|bnb|link|ada|dot|dai|matic)\b', q):
        return "Crypto"

    # ── Sports detection ──────────────────────────────────────────────────────
    # "Team A vs. Team B" — strongest signal for any head-to-head matchup
    if " vs " in q or " vs. " in q:
        return "Sports"

    # Short league codes need word boundaries to avoid false matches:
    # "nfl" appears in "inflation", "nba" in "canba", "mlb" in "mlb-style" etc.
    if re.search(r'\b(nba|nfl|mlb|nhl|ufc|mls|pga|ncaa)\b', q):
        return "Sports"

    sports_keywords = [
        # Leagues / tournaments (longer phrases safe as substring)
        "soccer", "world cup", "champions league",
        "super bowl", "march madness", "ncaa", "premier league", "mls", "pga", "masters",
        "stanley cup", "world series", "nba finals", "playoffs", "championship",
        # Generic sports phrases
        "win the game", "win tonight", "win the match", "win the series",
        "cover the spread", "over/under", "beat the", "defeat the",
        # NBA teams
        "lakers", "celtics", "warriors", "bucks", "nets", "suns", "nuggets",
        "clippers", "76ers", "sixers", "knicks", "spurs", "mavs", "mavericks",
        "grizzlies", "rockets", "thunder", "blazers", "timberwolves",
        "pistons", "hornets", "wizards", "magic", "hawks", "jazz", "kings",
        "pelicans", "cavaliers", "cavs", "raptors", "pacers",
        # NFL teams
        "chiefs", "eagles", "cowboys", "patriots", "49ers", "packers", "ravens",
        "bills", "broncos", "steelers", "raiders", "seahawks", "rams", "chargers",
        "dolphins", "vikings", "falcons", "saints", "buccaneers",
        # MLB teams
        "yankees", "dodgers", "red sox", "cubs", "astros", "braves", "mets",
        # NHL teams
        "maple leafs", "canadiens", "bruins", "rangers", "blackhawks",
        # UFC / MMA
        "knockout", "submission", "mma fight", "boxing match", "fight night",
        "ufc fight", "prelims", "main card", "lightweight", "heavyweight",
        "middleweight", "welterweight", "featherweight",
        # Soccer clubs
        "manchester", "barcelona", "real madrid", "liverpool", "arsenal", "chelsea",
        # College sports
        "a&m", "college game",
        # NOTE: removed ambiguous terms "heat", "bulls", "bears", "eagles", "panthers",
        # "wolves", "wildcats", "bulldogs", "tigers" — too common in non-sports context.
        # These are caught by the "vs" check or team-specific phrases instead.
    ]
    if any(w in q for w in sports_keywords):
        return "Sports"

    # ── Politics ──────────────────────────────────────────────────────────────
    politics_keywords = [
        # US institutions
        "election", "president", "senate", "congress", "vote", "poll",
        "democrat", "republican", "governor", "primary", "midterm",
        "white house", "executive order", "supreme court", "filibuster",
        "impeach", "inauguration", "cabinet", "secretary of state",
        "trump", "biden", "harris", "pelosi", "schumer", "mcconnell",
        # International politics
        "prime minister", "chancellor", "parliament", "referendum",
        "nato", "united nations", "sanctions", "treaty", "diplomatic",
        "geopolit", "war in", "invasion", "ceasefire", "peace deal",
        "putin", "zelensky", "xi jinping", "macron", "modi",
        # Policy
        "tariff", "trade war", "immigration", "border", "deportation",
        "legislation", "bill passes", "law signed", "executive",
        "political party", "polling", "approval rating",
    ]
    if any(w in q for w in politics_keywords):
        return "Politics"

    # ── Economics ─────────────────────────────────────────────────────────────
    economics_keywords = [
        # Fed / monetary policy
        "fed", "federal reserve", "fomc", "interest rate", "rate hike",
        "rate cut", "quantitative", "monetary policy", "jerome powell",
        # Macro indicators
        "inflation", "cpi", "pce", "gdp", "recession", "stagflation",
        "unemployment", "jobs report", "nonfarm", "payroll", "labor market",
        "consumer price", "producer price", "core inflation",
        # Markets / finance
        "stock market", "s&p 500", "dow jones", "nasdaq", "treasury",
        "yield curve", "bond yield", "10-year", "debt ceiling",
        "federal deficit", "trade deficit", "current account",
        # Corporate / business
        "earnings", "ipo", "merger", "acquisition", "bankruptcy",
        "revenue", "profit", "quarterly results",
        # Commodities
        "oil price", "crude oil", "gold price", "silver price",
        "commodity", "natural gas price",
    ]
    if any(w in q for w in economics_keywords):
        return "Economics"

    # ── Science / Technology ──────────────────────────────────────────────────
    science_keywords = [
        # Health / pharma
        "fda", "drug", "vaccine", "clinical trial", "approval",
        "cancer", "disease", "pandemic", "outbreak", "virus",
        "medication", "therapy", "treatment", "cure",
        # Space / NASA
        "nasa", "spacex", "rocket launch", "space mission", "mars",
        "moon landing", "satellite", "asteroid", "telescope",
        # AI / tech
        "artificial intelligence", "ai model", "ai surpass", "ai exceed",
        "gpt", "gemini",
        "openai", "anthropic", "deepmind", "machine learning",
        "self-driving", "autonomous vehicle",
        # Climate / environment
        "climate", "global warming", "carbon", "emissions",
        "renewable energy", "solar panel", "nuclear",
        # Other science
        "earthquake", "hurricane", "natural disaster",
        "scientific", "research paper", "study finds",
    ]
    if any(w in q for w in science_keywords):
        return "Science"

    return "General"

# ── 10. MARKET AGE ─────────────────────────────────────────────────────────────
def get_market_age_hours(market):
    """
    Returns how many hours old a market is based on its creation timestamp.
    Returns None if creation time is unavailable.
    """
    try:
        created_raw = market.get("createdAt") or market.get("created_at")
        if not created_raw:
            return None
        if isinstance(created_raw, str):
            created_at = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            created_at = created_at.replace(tzinfo=None)
        elif isinstance(created_raw, datetime):
            created_at = created_raw.replace(tzinfo=None)
        else:
            return None
        return round((now() - created_at).total_seconds() / 3600, 1)
    except Exception as e:
        log.warning("get_market_age_hours error: %s", e)
        return None


# ── 10. MARKET ACTIVE CHECK ────────────────────────────────────────────────────
def is_market_active(market):
    """
    Returns True if the market is open and not resolved/closed.
    """
    try:
        if market.get("closed") or market.get("resolved"):
            return False
        status = (market.get("status") or "").lower()
        if status in ("closed", "resolved", "cancelled", "canceled"):
            return False
        end_date_raw = market.get("endDate") or market.get("end_date")
        if end_date_raw:
            if isinstance(end_date_raw, str):
                end_date = datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                end_date = end_date.replace(tzinfo=None)
            elif isinstance(end_date_raw, datetime):
                end_date = end_date_raw.replace(tzinfo=None)
            else:
                end_date = None
            if end_date and end_date < now():
                return False
        return True
    except Exception as e:
        log.warning("is_market_active error: %s", e)
        return True



# ── 9b. MARKET TYPE DETECTION ──────────────────────────────────────────────────
def detect_market_type(question):
    """
    Within a category, identifies the specific type of market.
    Most useful for Crypto where market types behave very differently.

    Returns one of:
      PRICE_TARGET    — "Will BTC reach $100k?"
      PERCENTAGE_MOVE — "Will BTC gain 20% this month?"
      DOMINANCE       — "Will BTC dominance exceed 60%?"
      EVENT           — "Will BTC ETF be approved?"
      RANGE           — "Will BTC stay above $80k?"
      COMPARISON      — "Will ETH outperform BTC?"
      GENERAL         — anything else
    """
    q = question.lower()

    # DOMINANCE must be checked before PRICE_TARGET — "exceed 60%" would otherwise
    # match PRICE_TARGET since it contains digits and "exceed"
    if "dominance" in q:
        return "DOMINANCE"

    # RANGE must be checked before PRICE_TARGET — "stay above $80k" contains
    # "above" which would otherwise match PRICE_TARGET first
    if any(w in q for w in ["stay above", "remain above", "stay below", "between", "range"]):
        return "RANGE"

    if any(w in q for w in ["reach", "hit", "exceed", "above", "below", "dip", "drop to", "fall to"]):
        if "$" in q or any(c.isdigit() for c in q):
            return "PRICE_TARGET"

    if any(w in q for w in ["gain", "rise", "increase", "grow", "pump"]) and "%" in q:
        return "PERCENTAGE_MOVE"

    if any(w in q for w in ["etf", "approve", "approved", "launch", "halving",
                              "fork", "upgrade", "ban", "regulate", "sec", "lawsuit"]):
        return "EVENT"

    if any(w in q for w in ["outperform", "beat", "higher than", "more than"]):
        return "COMPARISON"

    return "GENERAL"

# ── 12. SCORE OPPORTUNITY ─────────────────────────────────────────────────────
def score_opportunity(market, price_history_rows=None, all_markets=None,
                      crypto_data=None, upcoming_events=None, fear_greed=None,
                      sports_odds=None, funding_rate=None, clob_data=None):
    """
    Master scoring function. Aggregates all signals into a 0-100 score.

    Returns a dict:
      {
        "score": int,
        "reason": str,             # human-readable summary of flags
        "category": str,           # Crypto / Sports / Politics / Economics / Science / General
        "confidence": str,         # HIGH / MEDIUM / LOW
        "direction": str,          # BUY_YES / BUY_NO / NO_EDGE — recommended trade direction
        "edge_pct": float | None,  # estimated probability edge vs market price (Vegas or lag)
        "flags": [...],            # warnings / alerts
        "signals": {...},          # raw sub-results
      }
    """
    price_history_rows = price_history_rows or []
    all_markets = all_markets or []
    upcoming_events = upcoming_events or []

    question = market.get("question", "")
    category = detect_category(question)
    flags = []
    confirming = 0
    contradicting = 0
    score = 50  # baseline

    # ── Parse yes_price early (needed for multiple signals below) ──────────────
    try:
        outcomes = market.get("outcomePrices", "[]")
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)
        yes_price = float(outcomes[0]) if outcomes else 0.5
    except Exception as e:
        log.warning("score_opportunity: could not parse outcomePrices: %s", e)
        yes_price = 0.5

    # ── Longshot filter ────────────────────────────────────────────────────────
    # Live trade analysis (87 trades) + historical backtest (32k markets):
    #
    # Bot OUTPERFORMS historical base rate at LOW prices (10-30c):
    #   10-20c: bot 41.7% vs historical 12.2% (+29.5pp edge)
    #   20-30c: bot 50.0% vs historical 24.3% (+25.7pp edge)
    #   This is the bot's core edge — finding underpriced dip markets.
    #
    # Bot UNDERPERFORMS at HIGH prices (40-70c):
    #   40-50c: bot 16.7% vs historical 43.1% (-26.4pp)
    #   50-70c: bot ~30% vs historical 55-67% (-25-42pp)
    #   Market has efficiently priced these — no edge.
    #
    # Filter logic:
    #   <5c  — always filter (dust markets, no real edge)
    #   RANGE markets — always filter (0% live win rate, 2/2 losses)
    #   PRICE_TARGET 40-70c — filter (bot consistently underperforms here)
    #   Everything else — allow through to scoring

    market_type_early = detect_market_type(question)

    # Longshot filter — raised to 40c based on backtest (4 categories, 135k+ markets)
    # Confirmed crossover at 40c across Crypto, Politics, Economics, Science:
    #   <20c:  10-13% win rate across all categories
    #   20-40c: 21-32% win rate — still below break-even (33.3%)
    #   40-60c: 37-50% win rate — at or above break-even
    if yes_price < 0.40:
        return {
            "score": 0,
            "reason": f"FILTERED: yes_price {round(yes_price*100,1)}c below 40c floor — historical WR {round(yes_price*100,1)<20 and '~12%' or '~25%'} at this price",
            "category": category,
            "market_type": market_type_early,
            "confidence": "LOW",
            "confirming": 0,
            "contradicting": 0,
            "direction": "NO_EDGE",
            "edge_pct": None,
            "flags": ["LONGSHOT_FILTERED"],
            "signals": {},
        }

    # RANGE markets — 0% live win rate, filter entirely
    if market_type_early == "RANGE":
        return {
            "score": 0,
            "reason": "FILTERED: RANGE market — 0% live win rate",
            "category": category,
            "market_type": market_type_early,
            "confidence": "LOW",
            "confirming": 0,
            "contradicting": 0,
            "direction": "NO_EDGE",
            "edge_pct": None,
            "flags": ["RANGE_FILTERED"],
            "signals": {},
        }

    # PRICE_TARGET 40-70c filter REMOVED — was based on 12 live trades (noise).
    # 135k historical Crypto markets show 40-60c wins at 49.6% — above break-even.
    # Live bot data was too thin to override 135k historical data points.

    # ── Spread width filter ────────────────────────────────────────────────────
    # Wide spreads mean entry cost is too high — skip these markets entirely.
    # A spread > 0.06 (~6 cents on a binary) eats most of the edge before you start.
    bid = float(market.get("bestBid", 0) or 0)
    ask = float(market.get("bestAsk", 0) or 0)
    spread = round(ask - bid, 4) if bid and ask else None
    spread_ok = True
    if spread is not None:
        if spread > 0.06:
            score -= 5  # reduced from -15: live data shows wide spread markets win at 53.8% vs 52.7% — minimal impact
            contradicting += 1
            spread_ok = False
            flags.append(f"WIDE SPREAD: {round(spread*100, 1)}¢ — high entry cost")
        elif spread <= 0.02:
            score += 5
            confirming += 1  # tight spread = deep, liquid book

    # ── Orderbook depth proxy ──────────────────────────────────────────────────
    # Polymarket's gamma API doesn't expose full orderbook depth, but
    # volume24hr is the best available proxy. We already check liquidity below.
    # True orderbook depth is stored via bid/ask at alert time for future analysis.

    # ── Fear & Greed — direction-aware, Crypto only ───────────────────────────
    # The sentiment signal must know whether YES means "price goes UP" or
    # "price goes DOWN". Applying contrarian logic to a downside target market
    # is backwards — Extreme Fear confirms a dip, it doesn't contradict it.
    #
    # Market direction taxonomy:
    #   UPSIDE  — "reach $X", "above $X", "gain X%", "exceed X%" — YES = price rises
    #   DOWNSIDE — "dip to $X", "below $X", "drop to $X", "fall to $X" — YES = price falls
    #   NEUTRAL — event markets, range markets, general — sentiment less applicable
    #
    # Sentiment logic by combination:
    #   Extreme Fear  + UPSIDE   → contrarian BUY signal   (market oversold, likely to recover)
    #   Extreme Fear  + DOWNSIDE → confirming YES signal   (fear drives the dip)
    #   Extreme Greed + UPSIDE   → caution / contradicting (market overbought)
    #   Extreme Greed + DOWNSIDE → contradicting YES       (greed = less likely to dip)

    _q_lower = question.lower()
    _downside_words = ["dip", "below", "drop to", "fall to", "crash", "under"]
    _upside_words   = ["reach", "hit", "exceed", "above", "gain", "rise", "pump", "top"]
    _is_downside = any(w in _q_lower for w in _downside_words)
    _is_upside   = any(w in _q_lower for w in _upside_words)
    # Default to upside if ambiguous — safer than assuming downside
    _market_direction = "DOWNSIDE" if _is_downside and not _is_upside else "UPSIDE"

    # Detect market type early — needed for funding rate gate below
    market_type = detect_market_type(question)

    fear_greed_signal = None
    if category == "Crypto" and fear_greed and fear_greed.get("success"):
        bonus = fear_greed.get("sentiment_bonus", 0)
        regime = fear_greed.get("regime", "Unknown")

        if bonus != 0:
            fear_greed_signal = regime

            if _market_direction == "DOWNSIDE":
                # Flip the logic: Fear confirms downside targets, Greed contradicts them
                if bonus > 0:
                    # Extreme Fear — confirms dip is likely
                    score += bonus
                    confirming += 1
                    flags.append(
                        f"SENTIMENT: {regime} — fear confirms downside target likely"
                    )
                else:
                    # Extreme Greed — market unlikely to dip
                    score += bonus  # bonus is negative, so this reduces score
                    contradicting += 1
                    flags.append(
                        f"SENTIMENT: {regime} — greed reduces downside probability"
                    )
            else:
                # UPSIDE market — original contrarian logic applies
                if bonus > 0:
                    # Extreme Fear — contrarian, market likely to recover upward
                    score += bonus
                    confirming += 1
                    flags.append(
                        f"SENTIMENT: {regime} — contrarian buy signal for upside target"
                    )
                else:
                    # Extreme Greed — overbought, upside target less likely
                    score += bonus
                    contradicting += 1
                    flags.append(
                        f"SENTIMENT: {regime} — caution, market euphoric"
                    )

    # ── Liquidity ──────────────────────────────────────────────────────────────
    liquidity = analyze_liquidity(market)
    if not liquidity["liquid"]:
        score -= 5  # reduced from -20: live data shows low liquidity markets win at 52.9% — nearly same as liquid ones
        contradicting += 1
        if liquidity["warning"]:
            flags.append(liquidity["warning"])
    else:
        score += 5
        confirming += 1

    # ── Price momentum ─────────────────────────────────────────────────────────
    # Tracks YES token price movement — direction-neutral.
    # Rising YES token = market pricing outcome as more likely = confirming.
    # Falling YES token = market pricing outcome as less likely = contradicting.
    # This applies equally to UPSIDE and DOWNSIDE markets since we track the
    # YES token itself, not the underlying asset price.
    momentum = analyze_price_momentum(price_history_rows)
    if momentum["signal"] in ("STRONG_RISING", "RISING"):
        score += 15
        confirming += 1
    elif momentum["signal"] in ("STRONG_FALLING", "FALLING"):
        score -= 10
        contradicting += 1

    # ── Price velocity ─────────────────────────────────────────────────────────
    # Tracks YES token price velocity — direction-neutral.
    # Fast move in YES token = genuine market interest = confirming regardless
    # of direction, since we track the token not the underlying asset.
    velocity = analyze_price_velocity(price_history_rows)
    if velocity["fast_move"]:
        score += 10
        confirming += 1
        if velocity["alert"]:
            flags.append(velocity["alert"])

    # ── Resolution ambiguity ───────────────────────────────────────────────────
    ambiguity = check_resolution_ambiguity(question)
    if ambiguity:
        score -= 10
        contradicting += 1
        flags.append(ambiguity)

    # ── Cross-market consistency ───────────────────────────────────────────────
    inconsistencies = check_cross_market_consistency(market, all_markets)
    if inconsistencies:
        score -= 5 * len(inconsistencies)
        contradicting += 1
        flags.extend(inconsistencies)

    # ── GENERAL market type bonus ──────────────────────────────────────────────
    # Live trade data: GENERAL Crypto markets 47.1% win rate vs PRICE_TARGET 22.1%.
    # "Bitcoin Up or Down" style markets win nearly 50% — strong signal.
    # Gated to Crypto only — Sports/Politics GENERAL markets are different.
    if market_type == "GENERAL" and category == "Crypto":
        score += 8
        confirming += 1
        flags.append("GENERAL_MARKET: historically 47% win rate on live trades")

    # ── Polymarket lag (Crypto) ────────────────────────────────────────────────
    # NOTE: lag signal had 0/4 win rate, -33.7% avg return in live trades.
    # Keeping detection for logging only — not scoring, not flagging as signal.
    lag = detect_polymarket_lag(question, yes_price, crypto_data)
    if lag:
        flags.append(f"LAG_DETECTED (unscored): {lag}")

    # ── PRICE_TARGET high-price penalty ───────────────────────────────────────────
    # Live data: bot underperforms market at 40-70c PRICE_TARGET (filtered above).
    # At 70-90c PRICE_TARGET: near-certain markets, low return upside, penalise slightly.
    if market_type == "PRICE_TARGET" and yes_price > 0.70:
        score -= 8
        contradicting += 1
        flags.append(f"PRICE_TARGET: {round(yes_price*100,1)}c — near-certain, low upside")

    # ── Binance funding rate (Crypto) ─────────────────────────────────────────────
    # Funding rate tells us whether futures traders are overcrowded on one side.
    # Strong negative funding = shorts overcrowded = potential squeeze = upside likely
    # Strong positive funding = longs overcrowded = potential dump = downside likely
    # Applied direction-aware: signal is flipped for DOWNSIDE markets.
    funding_signal = None
    _funding_applicable_types = ("PRICE_TARGET", "PERCENTAGE_MOVE", "RANGE")
    if category == "Crypto" and funding_rate and funding_rate.get("success") and market_type in _funding_applicable_types:
        fr_signal = funding_rate.get("signal", "NEUTRAL")
        fr_bonus = funding_rate.get("score_bonus", 0)
        funding_signal = fr_signal

        if fr_signal != "NEUTRAL" and fr_bonus > 0:
            if _market_direction == "DOWNSIDE":
                # LONGS_OVERCROWDED = dump likely = downside target more likely = YES
                # SHORTS_OVERCROWDED = squeeze likely = downside target less likely = NO
                if fr_signal in ("LONGS_OVERCROWDED", "MILD_BULLISH_FUNDING"):
                    score += fr_bonus
                    confirming += 1
                    flags.append(f"FUNDING: {fr_signal} ({funding_rate['rate_pct']}%) — longs overcrowded, downside likely")
                elif fr_signal in ("SHORTS_OVERCROWDED", "MILD_BEARISH_FUNDING"):
                    score -= fr_bonus
                    contradicting += 1
                    flags.append(f"FUNDING: {fr_signal} ({funding_rate['rate_pct']}%) — shorts overcrowded, squeeze risk")
            else:
                # UPSIDE market
                # SHORTS_OVERCROWDED = squeeze likely = upside more likely = YES
                # LONGS_OVERCROWDED = dump likely = upside less likely = NO
                if fr_signal in ("SHORTS_OVERCROWDED", "MILD_BEARISH_FUNDING"):
                    score += fr_bonus
                    confirming += 1
                    flags.append(f"FUNDING: {fr_signal} ({funding_rate['rate_pct']}%) — shorts overcrowded, squeeze potential")
                elif fr_signal in ("LONGS_OVERCROWDED", "MILD_BULLISH_FUNDING"):
                    score -= fr_bonus
                    contradicting += 1
                    flags.append(f"FUNDING: {fr_signal} ({funding_rate['rate_pct']}%) — longs overcrowded, dump risk")

    # ── CLOB order book imbalance (all categories) ─────────────────────────────
    # Real bid/ask size imbalance from Polymarket CLOB — more accurate than
    # the price-distance proxy used in bot.py.
    # Replaces the proxy OB signal with real data when available.
    clob_signal = None
    if clob_data and clob_data.get("success"):
        clob_signal = clob_data.get("signal", "BALANCED")
        clob_bonus = clob_data.get("score_bonus", 0)
        imbalance = clob_data.get("imbalance", 0)

        if clob_signal != "BALANCED" and clob_bonus > 0:
            if _market_direction == "DOWNSIDE":
                # Sell pressure = more asks = market expects price to fall = YES for downside
                if clob_signal in ("STRONG_SELL_PRESSURE", "SELL_PRESSURE"):
                    score += clob_bonus
                    confirming += 1
                    flags.append(f"CLOB: {clob_signal} (imbalance={imbalance}) — sell pressure confirms downside")
                elif clob_signal in ("STRONG_BUY_PRESSURE", "BUY_PRESSURE"):
                    score -= clob_bonus
                    contradicting += 1
                    flags.append(f"CLOB: {clob_signal} (imbalance={imbalance}) — buy pressure contradicts downside")
            else:
                # UPSIDE market
                if clob_signal in ("STRONG_BUY_PRESSURE", "BUY_PRESSURE"):
                    score += clob_bonus
                    confirming += 1
                    flags.append(f"CLOB: {clob_signal} (imbalance={imbalance}) — buy pressure confirms upside")
                elif clob_signal in ("STRONG_SELL_PRESSURE", "SELL_PRESSURE"):
                    score -= clob_bonus
                    contradicting += 1
                    flags.append(f"CLOB: {clob_signal} (imbalance={imbalance}) — sell pressure contradicts upside")

    # ── Vegas divergence (Sports) ──────────────────────────────────────────────
    # Compare Vegas implied probability to Polymarket price.
    # A gap > 10% is genuine edge — the two most liquid prediction markets disagree.
    # This is the strongest signal in the bot for sports markets.
    vegas_gap = None
    vegas_implied = None
    # Guard: skip if YES price is near-zero/near-certain (market decided, gap is noise)
    # or if odds fetch failed.
    _yes_tradeable = 0.02 <= yes_price <= 0.98
    if category == "Sports" and sports_odds and sports_odds.get("success") and _yes_tradeable:
        odds_dict = sports_odds.get("odds", {})
        question_lower = question.lower()
        for team, implied_prob in odds_dict.items():
            if any(word in question_lower for word in team.lower().split() if len(word) > 3):
                vegas_implied = implied_prob  # already in % form e.g. 65.2
                polymarket_pct = round(yes_price * 100, 1)
                vegas_gap = round(vegas_implied - polymarket_pct, 1)
                if vegas_gap > 15:
                    score += 25
                    confirming += 2
                    flags.append(
                        f"VEGAS EDGE: Polymarket {polymarket_pct}% vs Vegas {vegas_implied}% "
                        f"(+{vegas_gap}% gap) — BUY YES"
                    )
                elif vegas_gap > 10:
                    score += 15
                    confirming += 1
                    flags.append(
                        f"VEGAS EDGE: Polymarket {polymarket_pct}% vs Vegas {vegas_implied}% "
                        f"(+{vegas_gap}% gap)"
                    )
                elif vegas_gap < -15:
                    score += 20
                    confirming += 2
                    flags.append(
                        f"VEGAS EDGE: Polymarket {polymarket_pct}% vs Vegas {vegas_implied}% "
                        f"({vegas_gap}% gap) — BUY NO"
                    )
                elif vegas_gap < -10:
                    score += 12
                    confirming += 1
                    flags.append(
                        f"VEGAS EDGE: Polymarket {polymarket_pct}% vs Vegas {vegas_implied}% "
                        f"({vegas_gap}% gap) — consider NO"
                    )
                else:
                    # Gap too small — penalise slightly, market fairly priced
                    score -= 5
                    flags.append(
                        f"VEGAS: gap only {vegas_gap}% — market fairly priced"
                    )
                break

    # ── Event timing ───────────────────────────────────────────────────────────
    matched_events = analyze_event_timing(market, upcoming_events)
    if matched_events:
        score += 5
        confirming += 1

    # ── Days to resolution weighting ───────────────────────────────────────────
    # Short-dated markets are preferred — capital efficiency.
    # Long-dated markets with thin edge lock up money for months.
    days_to_res = None
    end_date_raw = market.get("endDate") or market.get("end_date")
    if end_date_raw:
        try:
            end_dt = datetime.fromisoformat(
                str(end_date_raw).replace("Z", "+00:00")
            ).replace(tzinfo=None)
            days_to_res = round((end_dt - now()).total_seconds() / 86400, 1)
            if days_to_res is not None:
                if days_to_res <= 3:
                    if yes_price >= 0.20:
                        score += 10
                        confirming += 1
                        flags.append(f"RESOLVES SOON: {days_to_res}d — high capital efficiency")
                    else:
                        score -= 5
                        contradicting += 1
                        flags.append(f"RESOLVES SOON: {days_to_res}d — but low price suggests decaying market")
                elif days_to_res <= 14:
                    score += 5   # mild bonus
                elif days_to_res > 180:
                    score -= 10
                    contradicting += 1
                    flags.append(f"LONG-DATED: {days_to_res}d — capital locked for months")
                elif days_to_res > 60:
                    score -= 5
        except Exception:
            pass

    # ── Resolution sanity check (Crypto price targets) ───────────────────────────
    # Penalises markets where the required price move is implausible given the
    # time remaining. e.g. ETH needs -7% in 12h = unrealistic = score penalty.
    # Only runs on Crypto markets with a current price available.
    sanity = None
    if category == "Crypto" and days_to_res is not None:
        crypto_data_for_sanity = None
        # Use the crypto_data passed into score_opportunity if available
        if crypto_data and crypto_data.get("success"):
            crypto_data_for_sanity = crypto_data
        sanity = check_resolution_sanity(
            question, yes_price,
            crypto_data=crypto_data_for_sanity,
            days_to_resolution=days_to_res
        )
        if sanity:
            score -= sanity["penalty"]
            contradicting += 1
            flags.append(sanity["reason"])

    # ── Market age bonus ───────────────────────────────────────────────────────
    age_hours = get_market_age_hours(market)
    if age_hours is not None and age_hours < 24:
        score += 5
        confirming += 1

    # ── Sports without Vegas signal: cap score below alert threshold ──────────
    if category == "Sports" and vegas_gap is None:
        score = min(score, 70)
        if score == 70:
            flags.append("SPORTS: no Vegas gap signal — capped at 70, not alerting")

    # ── Category score adjustments ─────────────────────────────────────────────
    # NOW DATA-JUSTIFIED: CLOB opening price analysis (Oct 2025-Mar 2026):
    #   Economics: 153 markets, 49.7% WR at 40c+ — comfortably profitable
    #   Politics:  317 markets, 38.8% WR at 40c+ — above break-even
    #   Science:    79 markets, 39.2% WR at 40c+ — above break-even
    #
    # These categories score lower than Crypto because Fear & Greed,
    # funding rates, and CLOB signals don't apply. The boost compensates
    # for signals that are structurally absent, not for real edge differences.
    # Boost is small (+5) — just enough to help clear the 85 threshold
    # when the market has other confirming signals.
    if category == "Economics":
        score += 5
        confirming += 1
        flags.append("ECONOMICS: +5 — 49.7% historical WR at 40c+ (153 markets)")
    elif category == "Politics":
        score += 5
        confirming += 1
        flags.append("POLITICS: +5 — 38.8% historical WR at 40c+ (317 markets)")
    elif category == "Science":
        score += 5
        confirming += 1
        flags.append("SCIENCE: +5 — 39.2% historical WR at 40c+ (79 markets)")

    # ── Time-of-day gate ───────────────────────────────────────────────────────
    # Removed: sample size too small (< 3 trades/hour on avg) to trust hourly
    # win rates. Will re-enable once 300+ resolved trades available.
    # hour_utc logged via hour_of_day_utc column for future analysis.

    # ── Clamp score ────────────────────────────────────────────────────────────
    score = max(0, min(100, score))

    confidence = calculate_confidence_tier(score, confirming, contradicting)
    reason = " | ".join(flags) if flags else "Score: " + str(score)

    # ── Directional recommendation ─────────────────────────────────────────────
    # Priority: Vegas gap > momentum > velocity > no edge
    # Removed lag direction — lag signal had 0/3 win rate in diagnostic.
    # Expanded momentum: no longer requires price to be on specific side of 0.5.
    edge_pct = None
    direction = "NO_EDGE"

    # For downside markets (dip/fall/drop targets), momentum is INVERTED:
    # falling price = underlying moving toward YES resolution = BUY_YES
    # rising price  = underlying moving away from YES resolution = BUY_NO
    _downside_market = (_market_direction == "DOWNSIDE")

    if vegas_gap is not None and abs(vegas_gap) > 10:
        edge_pct = abs(vegas_gap)
        direction = "BUY_YES" if vegas_gap > 0 else "BUY_NO"

    elif momentum["signal"] in ("STRONG_RISING", "RISING"):
        if _downside_market:
            # Price rising = moving AWAY from downside target = bet NO
            direction = "BUY_NO"
            edge_pct = round(abs(0.5 - yes_price) * 100, 1) if yes_price > 0.25 else None
        else:
            direction = "BUY_YES"
            edge_pct = round(abs(yes_price - 0.5) * 100, 1) if yes_price < 0.75 else None

    elif momentum["signal"] in ("STRONG_FALLING", "FALLING"):
        if _downside_market:
            # Price falling = moving TOWARD downside target = bet YES
            direction = "BUY_YES"
            edge_pct = round(abs(yes_price - 0.5) * 100, 1) if yes_price < 0.75 else None
        else:
            direction = "BUY_NO"
            edge_pct = round(abs(0.5 - yes_price) * 100, 1) if yes_price > 0.25 else None

    elif velocity["fast_move"]:
        if _downside_market:
            # Fast move down = approaching target = BUY_YES
            direction = "BUY_YES" if yes_price < 0.5 else "BUY_NO"
        else:
            direction = "BUY_YES" if yes_price < 0.5 else "BUY_NO"
        edge_pct = round(abs(yes_price - 0.5) * 100, 1)

    return {
        "score": score,
        "reason": reason,
        "category": category,
        "market_type": market_type,
        "confidence": confidence,
        "confirming": confirming,
        "contradicting": contradicting,
        "direction": direction,
        "edge_pct": edge_pct,
        "flags": flags,
        "signals": {
            "liquidity": liquidity,
            "momentum": momentum,
            "velocity": velocity,
            "ambiguity": ambiguity,
            "inconsistencies": inconsistencies,
            "lag": lag,
            "matched_events": matched_events,
            "age_hours": age_hours,
            "yes_price": yes_price,
            "spread": spread,
            "spread_ok": spread_ok,
            "vegas_gap": vegas_gap,
            "vegas_implied": vegas_implied,
            "days_to_resolution": days_to_res,
            "fear_greed_signal": fear_greed_signal,
            "market_direction": _market_direction,  # UPSIDE / DOWNSIDE
            "funding_signal": funding_signal,
            "clob_signal": clob_signal,
        },
    }