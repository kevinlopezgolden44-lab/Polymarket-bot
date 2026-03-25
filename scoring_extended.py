CATEGORY_THRESHOLDS = {
    "Crypto": {
        "baseline_score": 70,
        "signal_weight": 1.5,
        "alert_threshold": 85,
    },
    "Sports": {
        "baseline_score": 60,
        "signal_weight": 1.2,
        "alert_threshold": 75,
    },
    "Politics": {
        "baseline_score": 65,
        "signal_weight": 1.3,
        "alert_threshold": 80,
    },
    "Economics": {
        "baseline_score": 75,
        "signal_weight": 1.4,
        "alert_threshold": 90,
    },
    "Science": {
        "baseline_score": 80,
        "signal_weight": 1.6,
        "alert_threshold": 88,
    },
}

def detect_market_type(market_data):
    # Enhanced logic to determine the market type based on the data provided.
    # This is a placeholder for the actual implementation.
    type_detected = "Unknown"
    return type_detected

def get_category_threshold(category):
    """Get the scoring parameters for a specific category."""
    return CATEGORY_THRESHOLDS.get(category, None)

def apply_category_scoring(category, score):
    """Apply scoring logic based on the specified category."""
    thresholds = get_category_threshold(category)
    if not thresholds:
        return None
    
    baseline_score = thresholds["baseline_score"]
    signal_weight = thresholds["signal_weight"]
    adjusted_score = baseline_score + (score * signal_weight)
    
    return adjusted_score
