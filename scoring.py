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
)

# ── 9. CATEGORY DETECTION ─────────────────────────────────────────────────────
def detect_category(question):
    """
    Derives a category string from the market question.
    Returns one of: Crypto, Sports, Politics, Economics, Science, General.
    """
    q = question.lower()
    if any(w in q for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "xrp", "ripple"]):
        return "Crypto"

    sports_keywords = [
        # Leagues / tournaments
        "nba", "nfl", "mlb", "nhl", "ufc", "soccer", "world cup", "champions league",
        "super bowl", "march madness", "ncaa", "premier league", "mls", "pga", "masters",
        "stanley cup", "world series", "nba finals", "playoffs", "championship",
        # Generic sports phrases
        "win the game", "win tonight", "win the match", "win the series",
        "cover the spread", "over/under", "beat the", "defeat the",
        # NBA teams
        "lakers", "celtics", "warriors", "bucks", "nets", "heat", "suns", "nuggets",
        "clippers", "76ers", "sixers", "knicks", "bulls", "spurs", "mavs", "mavericks",
        # NFL teams
        "chiefs", "eagles", "cowboys", "patriots", "49ers", "packers", "ravens",
        "bills", "broncos", "steelers", "raiders", "seahawks", "rams", "chargers",
        # MLB teams
        "yankees", "dodgers", "red sox", "cubs", "astros", "braves", "mets",
        # NHL teams
        "maple leafs", "canadiens", "bruins", "rangers", "blackhawks",
        # UFC / MMA
        "knockout", "submission", "mma fight", "boxing match",
        # Soccer clubs
        "manchester", "barcelona", "real madrid", "liverpool", "arsenal", "chelsea",
    ]
    if any(w in q for w in sports_keywords):
        return "Sports"

    if any(w in q for w in ["election", "president", "senate", "congress", "vote", "poll", "party",
                             "democrat", "republican", "governor", "primary", "midterm"]):
        return "Politics"
    if any(w in q for w in ["fed", "inflation", "gdp", "cpi", "interest rate", "recession",
                             "jobs", "unemployment", "fomc", "nonfarm", "payroll"]):
        return "Economics"
    if any(w in q for w in ["fda", "drug", "vaccine", "nasa", "launch", "climate", "ai model",
                             "spacex", "cancer", "trial", "approval"]):
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

    if any(w in q for w in ["reach", "hit", "exceed", "above", "below", "dip", "drop to", "fall to"]):
        if "$" in q or any(c.isdigit() for c in q):
            return "PRICE_TARGET"

    if any(w in q for w in ["gain", "rise", "increase", "grow", "pump"]) and "%" in q:
        return "PERCENTAGE_MOVE"

    if "dominance" in q:
        return "DOMINANCE"

    if any(w in q for w in ["etf", "approve", "approved", "launch", "halving",
                              "fork", "upgrade", "ban", "regulate", "sec", "lawsuit"]):
        return "EVENT"

    if any(w in q for w in ["stay above", "remain above", "stay below", "between", "range"]):
        return "RANGE"

    if any(w in q for w in ["outperform", "beat", "higher than", "more than"]):
        return "COMPARISON"

    return "GENERAL"

# ── 12. SCORE OPPORTUNITY ─────────────────────────────────────────────────────
def score_opportunity(market, price_history_rows=None, all_markets=None,
                      crypto_data=None, upcoming_events=None, fear_greed=None,
                      sports_odds=None):
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

    # ── Spread width filter ────────────────────────────────────────────────────
    # Wide spreads mean entry cost is too high — skip these markets entirely.
    # A spread > 0.06 (~6 cents on a binary) eats most of the edge before you start.
    bid = float(market.get("bestBid", 0) or 0)
    ask = float(market.get("bestAsk", 0) or 0)
    spread = round(ask - bid, 4) if bid and ask else None
    spread_ok = True
    if spread is not None:
        if spread > 0.06:
            score -= 15
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

    # ── Fear & Greed — Crypto only, modest weight ──────────────────────────────
    # Only meaningful for crypto markets. Applied as a weak confirming/contradicting
    # signal, not a baseline modifier for all categories.
    fear_greed_signal = None
    if category == "Crypto" and fear_greed and fear_greed.get("success"):
        bonus = fear_greed.get("sentiment_bonus", 0)
        if bonus != 0:
            score += bonus
            fear_greed_signal = fear_greed.get("regime", "Unknown")
            if bonus > 0:
                confirming += 1
                flags.append(f"SENTIMENT: {fear_greed_signal} — contrarian buy signal")
            else:
                contradicting += 1
                flags.append(f"SENTIMENT: {fear_greed_signal} — caution, market euphoric")

    # ── Liquidity ──────────────────────────────────────────────────────────────
    liquidity = analyze_liquidity(market)
    if not liquidity["liquid"]:
        score -= 20
        contradicting += 1
        if liquidity["warning"]:
            flags.append(liquidity["warning"])
    else:
        score += 5
        confirming += 1

    # ── Price momentum ─────────────────────────────────────────────────────────
    momentum = analyze_price_momentum(price_history_rows)
    if momentum["signal"] in ("STRONG_RISING", "RISING"):
        score += 15
        confirming += 1
    elif momentum["signal"] in ("STRONG_FALLING", "FALLING"):
        score -= 10
        contradicting += 1

    # ── Price velocity ─────────────────────────────────────────────────────────
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

    # ── Polymarket lag (Crypto) ────────────────────────────────────────────────
    lag = detect_polymarket_lag(question, yes_price, crypto_data)
    if lag:
        score += 20
        confirming += 2
        flags.append(lag)

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
                    score += 10
                    confirming += 1
                    flags.append(f"RESOLVES SOON: {days_to_res}d — high capital efficiency")
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

    # ── Market age bonus ───────────────────────────────────────────────────────
    age_hours = get_market_age_hours(market)
    if age_hours is not None and age_hours < 24:
        score += 5
        confirming += 1

    # ── Clamp score ────────────────────────────────────────────────────────────
    score = max(0, min(100, score))

    confidence = calculate_confidence_tier(score, confirming, contradicting)
    reason = " | ".join(flags) if flags else "Score: " + str(score)
    market_type = detect_market_type(question)

    # ── Directional recommendation ─────────────────────────────────────────────
    # Tells you whether to buy YES or NO, and estimates edge percentage.
    # Priority: Vegas gap > lag detection > no edge identified
    edge_pct = None
    direction = "NO_EDGE"

    if vegas_gap is not None and abs(vegas_gap) > 10:
        edge_pct = abs(vegas_gap)
        direction = "BUY_YES" if vegas_gap > 0 else "BUY_NO"
    elif lag:
        # Lag means market hasn't caught up to real-world data — always buy YES
        direction = "BUY_YES"
        edge_pct = round(abs(0.99 - yes_price) * 100, 1)
    elif momentum["signal"] in ("STRONG_RISING", "RISING") and yes_price < 0.5:
        direction = "BUY_YES"
    elif momentum["signal"] in ("STRONG_FALLING", "FALLING") and yes_price > 0.5:
        direction = "BUY_NO"

    return {
        "score": score,
        "reason": reason,
        "category": category,
        "market_type": market_type,
        "confidence": confidence,
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
        },
    }