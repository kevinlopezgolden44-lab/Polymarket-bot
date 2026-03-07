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
    if any(w in q for w in ["nba", "nfl", "mlb", "nhl", "ufc", "soccer", "world cup", "champions league",
                             "super bowl", "march madness", "ncaa", "premier league"]):
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
                      crypto_data=None, upcoming_events=None, fear_greed=None):
    """
    Master scoring function. Aggregates all signals into a 0-100 score.

    Returns a dict:
      {
        "score": int,
        "reason": str,            # human-readable summary of flags
        "category": str,          # Crypto / Sports / Politics / Economics / Science / General
        "confidence": str,        # HIGH / MEDIUM / LOW
        "flags": [...],           # warnings / alerts
        "signals": {...},         # raw sub-results
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

    # ── Fear & Greed sentiment bonus ───────────────────────────────────────────
    if fear_greed and fear_greed.get("success"):
        bonus = fear_greed.get("sentiment_bonus", 0)
        score += bonus

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

    # ── Polymarket lag ─────────────────────────────────────────────────────────
    try:
        outcomes = market.get("outcomePrices", "[]")
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)
        yes_price = float(outcomes[0]) if outcomes else 0.5
    except Exception as e:
        log.warning("score_opportunity: could not parse outcomePrices: %s", e)
        yes_price = 0.5

    lag = detect_polymarket_lag(question, yes_price, crypto_data)
    if lag:
        score += 20
        confirming += 2
        flags.append(lag)

    # ── Event timing ───────────────────────────────────────────────────────────
    matched_events = analyze_event_timing(market, upcoming_events)
    if matched_events:
        score += 5
        confirming += 1

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

    return {
        "score": score,
        "reason": reason,
        "category": category,
        "market_type": market_type,
        "confidence": confidence,
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
        },
    }