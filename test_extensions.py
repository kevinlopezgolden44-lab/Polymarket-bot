# test_extensions.py

import unittest
from scoring_extended import detect_market_type, CATEGORY_THRESHOLDS, get_category_threshold

class TestExtendedMarketType(unittest.TestCase):
    """Test category-specific market type detection."""
    
    # ── CRYPTO ─────────────────────────────────────────────────────────────
    def test_crypto_price_target(self):
        assert detect_market_type("Will BTC reach $100k?", "Crypto") == "PRICE_TARGET"
    
    def test_crypto_range(self):
        assert detect_market_type("Will ETH stay above $3000?", "Crypto") == "RANGE"
    
    def test_crypto_dominance(self):
        assert detect_market_type("Will BTC dominance exceed 60%?", "Crypto") == "DOMINANCE"
    
    def test_crypto_event(self):
        assert detect_market_type("Will Bitcoin ETF be approved?", "Crypto") == "EVENT"
    
    def test_crypto_comparison(self):
        assert detect_market_type("Will ETH outperform BTC?", "Crypto") == "COMPARISON"
    
    # ── SPORTS ─────────────────────────────────────────────────────────────
    def test_sports_event(self):
        assert detect_market_type("Will the Lakers win the championship?", "Sports") == "EVENT"
    
    def test_sports_vs_detection(self):
        result = detect_market_type("Lakers vs Celtics: who will win?")
        assert result == "EVENT"
    
    def test_sports_comparison(self):
        assert detect_market_type("Will Lakers score more than Celtics?", "Sports") == "COMPARISON"
    
    # ── POLITICS ───────────────────────────────────────────────────────────
    def test_politics_approval(self):
        assert detect_market_type("Will Congress pass the bill?", "Politics") == "APPROVAL"
    
    def test_politics_poll_based(self):
        assert detect_market_type("Will Trump lead in 2026 polls?", "Politics") == "POLL_BASED"
    
    def test_politics_event(self):
        assert detect_market_type("Will Trump be elected president in 2024?", "Politics") == "EVENT"
    
    # ── ECONOMICS ──────────────────────────────────────────────────────────
    def test_economics_consensus_gap(self):
        assert detect_market_type("Will CPI beat consensus?", "Economics") == "CONSENSUS_GAP"
    
    def test_economics_range(self):
        assert detect_market_type("Will inflation stay between 2-3%?", "Economics") == "RANGE"
    
    def test_economics_event(self):
        assert detect_market_type("Will the Fed cut rates in March?", "Economics") == "EVENT"
    
    # ── SCIENCE ────────────────────────────────────────────────────────────
    def test_science_timing(self):
        assert detect_market_type("When will NASA launch the rover?", "Science") == "TIMING"
    
    def test_science_approval(self):
        assert detect_market_type("Will FDA approve this drug?", "Science") == "APPROVAL"
    
    def test_science_event(self):
        assert detect_market_type("Will SpaceX catch Starship?", "Science") == "EVENT"


class TestCategoryThresholds(unittest.TestCase):
    """Test category-specific scoring thresholds."""
    
    def test_crypto_threshold_lower_than_politics(self):
        """Crypto should have lower alert threshold (more opportunities)."""
        crypto_thresh = CATEGORY_THRESHOLDS["Crypto"]["score_alert_threshold"]
        politics_thresh = CATEGORY_THRESHOLDS["Politics"]["score_alert_threshold"]
        assert crypto_thresh < politics_thresh
    
    def test_science_highest_threshold(self):
        """Science should be most selective (fewer false positives)."""
        thresholds = {cat: cfg["score_alert_threshold"] 
                     for cat, cfg in CATEGORY_THRESHOLDS.items()}
        assert thresholds["Science"] == max(thresholds.values())
    
    def test_all_categories_have_thresholds(self):
        """All categories must have complete threshold config."""
        required_keys = {
            "baseline", "momentum_strong", "liquidity_good",
            "score_alert_threshold"
        }
        for category, config in CATEGORY_THRESHOLDS.items():
            for key in required_keys:
                assert key in config, f"Missing {key} in {category}"
    
    def test_sports_vegas_bonus_exists(self):
        """Sports-specific Vegas gap bonuses."""
        assert "vegas_gap_strong" in CATEGORY_THRESHOLDS["Sports"]
        assert "vegas_gap_medium" in CATEGORY_THRESHOLDS["Sports"]
    
    def test_politics_poll_bonuses_exist(self):
        """Politics-specific poll bonuses."""
        assert "poll_gap_strong" in CATEGORY_THRESHOLDS["Politics"]
        assert "poll_gap_medium" in CATEGORY_THRESHOLDS["Politics"]
    
    def test_economics_consensus_bonuses_exist(self):
        """Economics-specific consensus bonuses."""
        assert "consensus_gap_strong" in CATEGORY_THRESHOLDS["Economics"]
    
    def test_science_expert_bonus_exists(self):
        """Science-specific expert consensus bonus."""
        assert "expert_consensus_bonus" in CATEGORY_THRESHOLDS["Science"]


class TestThresholdRetrieval(unittest.TestCase):
    """Test safe threshold retrieval with fallbacks."""
    
    def test_get_category_threshold_exists(self):
        result = get_category_threshold("Crypto", "baseline")
        assert result == 50
    
    def test_get_category_threshold_fallback(self):
        result = get_category_threshold("Unknown", "baseline")
        assert result == 50  # Falls back to Crypto default
    
    def test_get_category_threshold_missing_key(self):
        result = get_category_threshold("Crypto", "nonexistent_key")
        assert result == 0


if __name__ == "__main__":
    unittest.main()