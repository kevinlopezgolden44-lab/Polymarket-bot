import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

def now():
    return datetime.utcnow()

# ── 1. PRICE MOMENTUM ──────────────────────────────────────────────────────────
def analyze_price_momentum(price_history_rows):
    """
    Returns momentum signal based on price history.
    Positive = price rising (YES getting more likely)
    Negative = price falling (YES getting less likely)
    """
    if not price_history_rows or len(price_history_rows) < 2:
        return {"signal": "INSUFFICIENT_DATA", "change": 0, "direction": "UNKNOWN"}

    prices = [float(r["yes_price"]) for r in reversed(price_history_rows)]
    oldest = prices[0]
    newest = prices[-1]

    if oldest == 0:
        return {"signal": "INSUFFICIENT_DATA", "change": 0, "direction": "UNKNOWN"}

    change_pct = round((newest - oldest) / oldest * 100, 1)

    if change_pct > 20:
        signal = "STRONG_RISING"
    elif change_pct > 5:
        signal = "RISING"
    elif change_pct < -20:
        signal = "STRONG_FALLING"
    elif change_pct < -5:
        signal = "FALLING"
    else:
        signal = "STABLE"

    direction = "UP" if change_pct > 0 else "DOWN" if change_pct < 0 else "FLAT"
    return {"signal": signal, "change": change_pct, "direction": direction}

# ── 2. PRICE VELOCITY ──────────────────────────────────────────────────────────
def analyze_price_velocity(price_history_rows, window_hours=1):
    """
    Detects unusually fast price movement in recent window.
    Returns True if price moved >15% in last window_hours.
    """
    if not price_history_rows or len(price_history_rows) < 2:
        return {"fast_move": False, "change": 0, "alert": None}

    cutoff = now() - timedelta(hours=window_hours)
    recent = [r for r in price_history_rows if r["recorded_at"] > cutoff]
    older = [r for r in price_history_rows if r["recorded_at"] <= cutoff]

    if not recent or not older:
        return {"fast_move": False, "change": 0, "alert": None}

    latest_price = float(recent[-1]["yes_price"])
    baseline_price = float(older[-1]["yes_price"])

    if baseline_price == 0:
        return {"fast_move": False, "change": 0, "alert": None}

    change_pct = round((latest_price - baseline_price) / baseline_price * 100, 1)
    fast_move = abs(change_pct) > 15

    alert = None
    if fast_move:
        direction = "UP" if change_pct > 0 else "DOWN"
        alert = (
            "VELOCITY ALERT: Price moved " + str(change_pct) + "% in last " +
            str(window_hours) + "h (" + direction + ") - possible news event"
        )

    return {"fast_move": fast_move, "change": change_pct, "alert": alert}

# ── 3. LIQUIDITY DEPTH ─────────────────────────────────────────────────────────
def analyze_liquidity(market):
    """
    Checks if market has genuine recent liquidity vs just historical volume.
    Returns warning if 24h volume is very low vs total volume.
    """
    try:
        total_volume = float(market.get("volumeNum", 0) or 0)
        volume_24h = float(market.get("volume24hr", 0) or 0)

        if total_volume == 0:
            return {"liquid": False, "warning": "No trading volume"}

        recency_ratio = volume_24h / total_volume if total_volume > 0 else 0

        if volume_24h < 500:
            return {
                "liquid": False,
                "warning": "Low 24h volume $" + str(round(volume_24h)) + " - hard to enter position"
            }
        if recency_ratio < 0.001 and total_volume > 100000:
            return {
                "liquid": False,
                "warning": "High total volume but stale - only $" + str(round(volume_24h)) + " in 24h"
            }
        return {"liquid": True, "warning": None}
    except Exception as e:
        log.warning("analyze_liquidity error: %s", e)
        return {"liquid": True, "warning": None}
# ── 4. CROSS-MARKET CONSISTENCY ────────────────────────────────────────────────
def check_cross_market_consistency(market, all_markets):
    """
    Detects logical price inconsistencies between related markets.
    e.g. BTC reaching $90k should always be <= probability of BTC reaching $85k.
    """
    question = market.get("question", "").lower()
    inconsistencies = []

    try:
        outcomes = market.get("outcomePrices", "[]")
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)
        if not outcomes:
            return inconsistencies
        yes_price = float(outcomes[0])

        # Find price targets in this market
        numbers = re.findall(r"\$[\d,]+", question)
        if not numbers:
            return inconsistencies
        this_target = float(numbers[0].replace("$", "").replace(",", ""))

        # Determine if this is upside or downside target
        is_upside = any(w in question for w in ["reach", "above", "exceed", "hit", "top"])
        is_downside = any(w in question for w in ["dip", "below", "drop", "fall", "bottom"])

        if not is_upside and not is_downside:
            return inconsistencies

        # Find related markets with similar questions
        for other in all_markets:  # scan all markets passed in
            try:
                other_q = other.get("question", "").lower()
                if other_q == question:
                    continue
                # Must be same asset type
                same_asset = False
                for asset in ["bitcoin", "btc", "ethereum", "eth", "solana"]:
                    if asset in question and asset in other_q:
                        same_asset = True
                        break
                if not same_asset:
                    continue

                other_outcomes = other.get("outcomePrices", "[]")
                if isinstance(other_outcomes, str):
                    import json
                    other_outcomes = json.loads(other_outcomes)
                if not other_outcomes:
                    continue
                other_price = float(other_outcomes[0])

                other_numbers = re.findall(r"\$[\d,]+", other_q)
                if not other_numbers:
                    continue
                other_target = float(other_numbers[0].replace("$", "").replace(",", ""))

                # For upside: higher target should have lower or equal probability
                if is_upside:
                    other_is_upside = any(w in other_q for w in ["reach", "above", "exceed", "hit"])
                    if other_is_upside and other_target < this_target and other_price < yes_price:
                        inconsistencies.append(
                            "Inconsistency: $" + str(round(other_target)) +
                            " target priced lower than $" + str(round(this_target)) + " target"
                        )
                # For downside: lower target should have lower or equal probability
                if is_downside:
                    other_is_downside = any(w in other_q for w in ["dip", "below", "drop", "fall"])
                    if other_is_downside and other_target > this_target and other_price < yes_price:
                        inconsistencies.append(
                            "Inconsistency: $" + str(round(other_target)) +
                            " dip target priced lower than $" + str(round(this_target)) + " target"
                        )
            except Exception as e:
                log.warning("cross_market inner loop error: %s", e)
                continue
    except Exception as e:
        log.warning("check_cross_market_consistency error: %s", e)

    return inconsistencies[:2]  # Return max 2 inconsistencies

# ── 5. POLYMARKET LAG DETECTION ────────────────────────────────────────────────
def detect_polymarket_lag(question, yes_price, crypto_data):
    """
    Compares Polymarket price to real world data to detect lag.
    Currently implemented for crypto price markets.
    """
    if not crypto_data or not crypto_data.get("success"):
        return None

    current_price = crypto_data["price"]
    numbers = re.findall(r"\$[\d,]+", question)
    if not numbers:
        return None

    try:
        target = float(numbers[0].replace("$", "").replace(",", ""))
        question_lower = question.lower()

        # Already resolved scenarios
        is_upside = any(w in question_lower for w in ["reach", "above", "exceed"])
        is_downside = any(w in question_lower for w in ["dip", "below", "drop"])

        if is_upside and current_price >= target and yes_price < 0.85:
            return (
                "LAG DETECTED: BTC already at $" + str(round(current_price)) +
                " but market only pricing YES at " + str(round(yes_price * 100)) + "% - possible update delay"
            )
        if is_downside and current_price <= target and yes_price < 0.85:
            return (
                "LAG DETECTED: BTC already at $" + str(round(current_price)) +
                " but market only pricing YES at " + str(round(yes_price * 100)) + "% - possible update delay"
            )
    except Exception as e:
        log.warning("detect_polymarket_lag error: %s", e)

# ── 6. EVENT TIMING AWARENESS ──────────────────────────────────────────────────
def analyze_event_timing(market, upcoming_events):
    """
    Checks if market correlates with a known upcoming event.
    Returns relevant event if found.
    """
    question = market.get("question", "").lower()
    matched_events = []

    for event in upcoming_events:
        keywords = event.get("relevance_keywords", "").lower().split(",")
        if any(kw.strip() in question for kw in keywords if kw.strip()):
            days_until = (event["event_date"] - now()).days
            matched_events.append({
                "name": event["event_name"],
                "days_until": days_until,
                "category": event["category"]
            })

    return matched_events[:2]  # Return top 2 matches

# ── 7. RESOLUTION AMBIGUITY ────────────────────────────────────────────────────
def check_resolution_ambiguity(question):
    """
    Detects vague resolution criteria that make a market risky to trade.
    Returns warning if criteria appear ambiguous.
    """
    question_lower = question.lower()

    vague_phrases = [
        "approximately", "around", "about", "roughly", "sometime",
        "expected to", "likely to", "probably", "may ", "might ",
        "significant", "major", "notable", "considerable"
    ]

    precise_indicators = [
        "$", "%", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december", "january", "february",
        "q1", "q2", "q3", "q4", "2024", "2025", "2026",
        "by end of", "before", "after", "on or before"
    ]

    vague_count = sum(1 for p in vague_phrases if p in question_lower)
    precise_count = sum(1 for p in precise_indicators if p in question_lower)

    if vague_count >= 2 and precise_count == 0:
        return "AMBIGUOUS: Vague resolution criteria detected - higher risk"
    if vague_count >= 1 and precise_count == 0 and len(question) < 50:
        return "POSSIBLY AMBIGUOUS: Limited precision in resolution criteria"

    return None

# ── 8. CONFIDENCE TIER ─────────────────────────────────────────────────────────
def calculate_confidence_tier(score, confirming_signals, contradicting_signals):
    """
    Assigns confidence tier based on how many signals agree.
    High = 3+ confirming signals, 0 contradictions
    Medium = 2 confirming, <=1 contradiction
    Low = 1 confirming or any contradictions
    """
    net_signals = confirming_signals - (contradicting_signals * 2)

    if score >= 85 and net_signals >= 2:
        return "HIGH"
    elif score >= 70 and net_signals >= 1:
        return "MEDIUM"
    else:
        return "LOW"


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