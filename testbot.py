"""
Unit tests for Polymarket bot.
Run with: python test_bot.py
"""
import json
import unittest
from datetime import datetime, timedelta

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_market(question="Will BTC reach $100k?", yes_price=0.25,
                volume=10000, volume24=500, closed=False,
                created_hours_ago=48):
    created = (datetime.utcnow() - timedelta(hours=created_hours_ago)).isoformat()
    return {
        "question": question,
        "outcomePrices": json.dumps([str(yes_price), str(round(1 - yes_price, 4))]),
        "volumeNum": volume,
        "volume24hr": volume24,
        "closed": closed,
        "resolved": False,
        "status": "active",
        "endDate": (datetime.utcnow() + timedelta(days=30)).isoformat(),
        "createdAt": created,
        "id": "test-market-123",
    }

def make_price_history(prices, hours_apart=2):
    """prices = newest first (matches DB ORDER BY recorded_at DESC)"""
    rows = []
    base = datetime.utcnow()
    for i, p in enumerate(prices):
        rows.append({
            "yes_price": p,
            "recorded_at": base - timedelta(hours=i * hours_apart)
        })
    return rows

# ── scoring.py ─────────────────────────────────────────────────────────────────

from scoring import (
    score_opportunity, detect_category,
    is_market_active, get_market_age_hours,
    analyze_price_momentum, analyze_price_velocity,
    analyze_liquidity, check_resolution_ambiguity,
    calculate_confidence_tier,
)

class TestDetectCategory(unittest.TestCase):
    def test_crypto_btc(self):
        assert detect_category("Will BTC reach $100k?") == "Crypto"

    def test_crypto_ethereum(self):
        assert detect_category("Will Ethereum hit $5000?") == "Crypto"

    def test_sports_nba(self):
        assert detect_category("Will the Lakers win the NBA championship?") == "Sports"

    def test_politics_election(self):
        assert detect_category("Who will win the 2026 US election?") == "Politics"

    def test_economics_fed(self):
        assert detect_category("Will the Fed cut interest rates in March?") == "Economics"

    def test_science_fda(self):
        assert detect_category("Will FDA approve this drug by Q2?") == "Science"

    def test_general_fallback(self):
        assert detect_category("Will it rain tomorrow?") == "General"


class TestScoreOpportunity(unittest.TestCase):
    def test_returns_dict_with_required_keys(self):
        market = make_market()
        result = score_opportunity(market)
        assert isinstance(result, dict)
        for key in ("score", "reason", "category", "confidence", "flags", "signals"):
            assert key in result, f"Missing key: {key}"

    def test_score_is_int_in_range(self):
        market = make_market()
        result = score_opportunity(market)
        assert isinstance(result["score"], int)
        assert 0 <= result["score"] <= 100

    def test_category_populated(self):
        market = make_market(question="Will BTC reach $100k?")
        result = score_opportunity(market)
        assert result["category"] == "Crypto"

    def test_low_liquidity_reduces_score(self):
        liquid_market = make_market(volume=50000, volume24=5000)
        illiquid_market = make_market(volume=50000, volume24=100)  # very low 24h
        liquid_result = score_opportunity(liquid_market)
        illiquid_result = score_opportunity(illiquid_market)
        assert liquid_result["score"] > illiquid_result["score"]

    def test_fear_greed_bonus_applied(self):
        market = make_market()
        # Fear & greed bonus is now 0 across all regimes (removed to reduce noise)
        # Test that the field is accepted without error and scores are equal
        fear_greed_fear = {
            "success": True, "score": 20, "regime": "Extreme Fear",
            "sentiment_bonus": 0, "trend": "IMPROVING"
        }
        fear_greed_greed = {
            "success": True, "score": 80, "regime": "Extreme Greed",
            "sentiment_bonus": 0, "trend": "DECLINING"
        }
        fear_result = score_opportunity(market, fear_greed=fear_greed_fear)
        greed_result = score_opportunity(market, fear_greed=fear_greed_greed)
        # Scores should now be equal since bonus is 0
        assert fear_result["score"] == greed_result["score"]

    def test_fresh_market_gets_bonus(self):
        fresh = make_market(created_hours_ago=5)
        old = make_market(created_hours_ago=200)
        fresh_result = score_opportunity(fresh)
        old_result = score_opportunity(old)
        assert fresh_result["score"] > old_result["score"]

    def test_reason_string_not_empty(self):
        market = make_market()
        result = score_opportunity(market)
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

    def test_no_crash_on_missing_fields(self):
        # Completely empty market dict should not raise
        result = score_opportunity({})
        assert isinstance(result["score"], int)


class TestIsMarketActive(unittest.TestCase):
    def test_active_market(self):
        assert is_market_active(make_market()) is True

    def test_closed_market(self):
        m = make_market()
        m["closed"] = True
        assert is_market_active(m) is False

    def test_resolved_market(self):
        m = make_market()
        m["resolved"] = True
        assert is_market_active(m) is False

    def test_expired_end_date(self):
        m = make_market()
        m["endDate"] = (datetime.utcnow() - timedelta(days=1)).isoformat()
        assert is_market_active(m) is False

    def test_future_end_date(self):
        m = make_market()
        m["endDate"] = (datetime.utcnow() + timedelta(days=30)).isoformat()
        assert is_market_active(m) is True


class TestGetMarketAgeHours(unittest.TestCase):
    def test_returns_float(self):
        m = make_market(created_hours_ago=24)
        age = get_market_age_hours(m)
        assert age is not None
        assert 23 <= age <= 25  # allow 1h tolerance

    def test_missing_created_at_returns_none(self):
        m = make_market()
        del m["createdAt"]
        assert get_market_age_hours(m) is None


# ── analysis.py ────────────────────────────────────────────────────────────────

from analysis import (
    analyze_price_momentum, analyze_price_velocity,
    analyze_liquidity, check_resolution_ambiguity,
    calculate_confidence_tier,
)

class TestAnalyzePriceMomentum(unittest.TestCase):
    def test_rising(self):
        # newest first: price went from 0.20 -> 0.30 (rising)
        history = make_price_history([0.30, 0.25, 0.22, 0.20])
        result = analyze_price_momentum(history)
        assert result["signal"] in ("RISING", "STRONG_RISING")
        assert result["direction"] == "UP"

    def test_falling(self):
        # newest first: price went from 0.80 -> 0.50 (falling)
        history = make_price_history([0.50, 0.60, 0.70, 0.80])
        result = analyze_price_momentum(history)
        assert result["signal"] in ("FALLING", "STRONG_FALLING")
        assert result["direction"] == "DOWN"

    def test_stable(self):
        history = make_price_history([0.50, 0.51, 0.50, 0.51])
        result = analyze_price_momentum(history)
        assert result["signal"] == "STABLE"

    def test_insufficient_data(self):
        result = analyze_price_momentum([])
        assert result["signal"] == "INSUFFICIENT_DATA"

    def test_single_row_insufficient(self):
        result = analyze_price_momentum(make_price_history([0.5]))
        assert result["signal"] == "INSUFFICIENT_DATA"


class TestAnalyzePriceVelocity(unittest.TestCase):
    def test_fast_move_detected(self):
        # 20% move in 30 min — well within 1h window
        now = datetime.utcnow()
        history = [
            {"yes_price": 0.50, "recorded_at": now - timedelta(minutes=90)},
            {"yes_price": 0.60, "recorded_at": now - timedelta(minutes=10)},
        ]
        result = analyze_price_velocity(history, window_hours=1)
        assert result["fast_move"] is True
        assert result["alert"] is not None

    def test_slow_move_not_flagged(self):
        now = datetime.utcnow()
        history = [
            {"yes_price": 0.50, "recorded_at": now - timedelta(minutes=90)},
            {"yes_price": 0.52, "recorded_at": now - timedelta(minutes=10)},
        ]
        result = analyze_price_velocity(history, window_hours=1)
        assert result["fast_move"] is False

    def test_insufficient_data(self):
        result = analyze_price_velocity([])
        assert result["fast_move"] is False


class TestAnalyzeLiquidity(unittest.TestCase):
    def test_liquid_market(self):
        m = make_market(volume=100000, volume24=5000)
        result = analyze_liquidity(m)
        assert result["liquid"] is True

    def test_low_24h_volume(self):
        m = make_market(volume=100000, volume24=100)
        result = analyze_liquidity(m)
        assert result["liquid"] is False
        assert result["warning"] is not None

    def test_no_volume(self):
        m = make_market(volume=0, volume24=0)
        result = analyze_liquidity(m)
        assert result["liquid"] is False


class TestCheckResolutionAmbiguity(unittest.TestCase):
    def test_ambiguous_question(self):
        result = check_resolution_ambiguity("Will something significant happen?")
        assert result is not None
        assert "AMBIGUOUS" in result or "POSSIBLY" in result

    def test_precise_question(self):
        result = check_resolution_ambiguity("Will BTC reach $100k by December 31, 2025?")
        assert result is None

    def test_short_vague_question(self):
        result = check_resolution_ambiguity("Will it probably rain?")
        assert result is not None


class TestCalculateConfidenceTier(unittest.TestCase):
    def test_high_confidence(self):
        assert calculate_confidence_tier(90, 3, 0) == "HIGH"

    def test_medium_confidence(self):
        # net = 3 - (0*2) = 3 >= 1, score 75 >= 70 → MEDIUM
        assert calculate_confidence_tier(75, 3, 0) == "MEDIUM"

    def test_low_confidence(self):
        assert calculate_confidence_tier(50, 0, 2) == "LOW"

    def test_high_score_low_signals_not_high(self):
        # Score alone isn't enough — need confirming signals too
        assert calculate_confidence_tier(90, 0, 0) != "HIGH"


# ── Cross-market consistency uses all markets ──────────────────────────────────

from analysis import check_cross_market_consistency

class TestCrossMarketConsistency(unittest.TestCase):
    def test_scans_beyond_200_markets(self):
        """Verify the 200-market limit is gone — should check market at index 250."""
        base_market = make_market(question="Will BTC reach $90000?", yes_price=0.60)
        # Fill 250 markets with dummies, then put the inconsistent one at index 250
        fillers = [make_market(question="Unrelated question " + str(i)) for i in range(250)]
        # This lower target priced lower than the higher one = inconsistency
        inconsistent = make_market(question="Will BTC reach $80000?", yes_price=0.40)
        all_markets = fillers + [inconsistent]
        result = check_cross_market_consistency(base_market, all_markets)
        assert isinstance(result, list)
        # Should find the inconsistency in the 251st market
        assert len(result) > 0, "Should detect inconsistency beyond the old 200-market limit"

    def test_no_false_positives_unrelated(self):
        base = make_market(question="Will BTC reach $90000?", yes_price=0.60)
        others = [make_market(question="Will the Lakers win the NBA?", yes_price=0.30)]
        result = check_cross_market_consistency(base, others)
        assert result == []



# ── market_type detection ──────────────────────────────────────────────────────

from scoring import detect_market_type

class TestDetectMarketType(unittest.TestCase):
    def test_price_target(self):
        assert detect_market_type("Will BTC reach $100k?") == "PRICE_TARGET"

    def test_percentage_move(self):
        assert detect_market_type("Will ETH gain 20% this month?") == "PERCENTAGE_MOVE"

    def test_dominance(self):
        assert detect_market_type("Will BTC dominance exceed 60%?") == "DOMINANCE"

    def test_event(self):
        assert detect_market_type("Will the Bitcoin ETF be approved?") == "EVENT"

    def test_score_opportunity_returns_market_type(self):
        market = make_market(question="Will BTC reach $100k?")
        result = score_opportunity(market)
        assert "market_type" in result
        assert result["market_type"] == "PRICE_TARGET"


if __name__ == "__main__":
    unittest.main()