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
    """
    q = question.lower()
    if any(w in q for w in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol", "xrp", "ripple"]):
        return "Crypto"

    # "Team A vs. Team B" — strongest signal for any head-to-head matchup
    # catches college games, international fixtures, any sport not in keyword list
    if " vs " in q or " vs. " in q:
        return "Sports"

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
        # College sports indicators
        "wildcats", "panthers", "bulldogs", "tigers", "bears", "wolves", "eagles",
        "a&m", "university", "college game",
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

    # DOMINANCE must be checked before PRICE_TARGET — "exceed 60%" would otherwise
    # match PRICE_TARGET since it contains digits and "exceed"
    if "dominance" in q:
        return "DOMINANCE"

    if any(w in q for w in ["reach", "hit", "exceed", "above", "below", "dip", "drop to", "fall to"]):
        if "$" in q or any(c.isdigit() for c in q):
            return "PRICE_TARGET"

    if any(w in q for w in ["gain", "rise", "increase", "grow", "pump"]) and "%" in q:
        return "PERCENTAGE_MOVE"

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
    # Entry price <10% means buying a longshot. Data shows 11.4% win rate at
    # this range vs 50% win rate at 50-75%. Not tradeable edge.
    if yes_price < 0.10:
        return {
            "score": 0,
            "reason": f"FILTERED: yes_price {round(yes_price*100,1)}% too low — longshot, no edge",
            "category": category,
            "market_type": detect_market_type(question),
            "confidence": "LOW",
            "confirming": 0,
            "contradicting": 0,
            "direction": "NO_EDGE",
            "edge_pct": None,
            "flags": ["LONGSHOT_FILTERED"],
            "signals": {},
        }

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
        score -= 20
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

    # ── Polymarket lag (Crypto) ────────────────────────────────────────────────
    # NOTE: lag signal had 0/3 win rate, -38% avg return in diagnostic.
    # Removed score bonus — keeping detection for logging only, not scoring.
    lag = detect_polymarket_lag(question, yes_price, crypto_data)
    if lag:
        flags.append(f"LAG_DETECTED (unscored): {lag}")

    # ── Binance funding rate (Crypto) ─────────────────────────────────────────────
    # Funding rate tells us whether futures traders are overcrowded on one side.
    # Strong negative funding = shorts overcrowded = potential squeeze = upside likely
    # Strong positive funding = longs overcrowded = potential dump = downside likely
    # Applied direction-aware: signal is flipped for DOWNSIDE markets.
    funding_signal = None
    if category == "Crypto" and funding_rate and funding_rate.get("success"):
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
    # Sports edge comes from Vegas gap. Without it, sentiment/timing bonuses
    # alone (Extreme Fear +10, resolves soon +10, new market +5) can push a
    # college game with no real signal to 85+ and trigger a false alert.
    # Cap at 70 so it logs as an opportunity but never alerts.
    if category == "Sports" and vegas_gap is None:
        score = min(score, 70)
        if score == 70:
            flags.append("SPORTS: no Vegas gap signal — capped at 70, not alerting")

    # ── Time-of-day gate ───────────────────────────────────────────────────────
    # Removed: sample size too small (< 3 trades/hour on avg) to trust hourly
    # win rates. Will re-enable once 300+ resolved trades available.
    # hour_utc logged via hour_of_day_utc column for future analysis.

    # ── Clamp score ────────────────────────────────────────────────────────────
    score = max(0, min(100, score))

    confidence = calculate_confidence_tier(score, confirming, contradicting)
    reason = " | ".join(flags) if flags else "Score: " + str(score)
    market_type = detect_market_type(question)

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