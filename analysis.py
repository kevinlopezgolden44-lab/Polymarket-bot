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

    return None

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